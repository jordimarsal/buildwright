"""Phase 2 runtime: the full feature loop — batches in chain, hard gates, human gates.

Implements the harness protocol as a LangGraph state machine ("the graph IS the
leader"): the implementer runs batch after batch with stall recovery, the
scripts this protocol already has (`init.sh`, `check-traceability.py`,
`audit-security.sh` / `bench.sh`) become hard gate nodes, the reviewer issues a
structured verdict written to `harness/progress/review_<name>.md`, and human
gates park the process via `interrupt()` with the state persisted in a sqlite
checkpointer — `buildwright harness approve | reject` resumes them, surviving
process restarts.

The runtime writes exactly the harness artifacts (APPROVAL, status `done`,
`history.md` + the `retro:` line) and only after the completion gate approves.
"""

from __future__ import annotations

import getpass
import hashlib
import json
import re
import subprocess
import threading
from collections.abc import Callable, Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Any, Literal, Protocol, TypedDict, cast

import yaml
from langgraph.checkpoint.sqlite import SqliteSaver
from langgraph.graph import END, START, StateGraph
from langgraph.graph.state import CompiledStateGraph
from langgraph.types import Command, StateSnapshot, interrupt
from pydantic import BaseModel, Field
from pydantic_ai import Agent, RunContext

from . import runlog
from .config import Settings
from .harness import Feature, FeatureList, HarnessProject
from .registry import ModelRegistry
from .runtime import (
    GIT_IDENTITY,
    Sandbox,
    Task,
    build_implementer,
    create_worktree,
    next_batch,
    parse_tasks,
    tool_list_dir,
    tool_read_file,
    tool_run_command,
    tool_write_file,
)

DEFAULT_MAX_TASKS = 3
DEFAULT_MAX_BATCHES = 10  # batch budget per invocation; re-run continues
MAX_BATCH_ATTEMPTS = 4  # dispatches on the same batch without progress → park
MAX_GATE_CYCLES = 3  # consecutive failures of one gate → park
MAX_REVIEW_ROUNDS = 2  # CHANGES_REQUESTED in a row → park blocked
MAX_STALLS = 2  # consecutive stalls → fresh agent instance (restart)
MAX_SPEC_ROUNDS = 2  # spec-gate rejections in a row → park blocked

# parallel mode: two features committing board flips to main at once would race
# on git's index.lock — every main-repo mutation takes this lock
MAIN_LOCK = threading.Lock()

LOOP_CAP_KEYS = ("max_batch_attempts", "max_gate_cycles", "max_review_rounds", "max_spec_rounds")


class LoopState(TypedDict, total=False):
    """Persisted graph state — everything the protocol counts or resumes."""

    feature: str
    batch: list[str]
    run_limit: int
    dispatch_prompt: str  # feedback for the next dispatch (consumed by implement_batch)
    fingerprint: str  # worktree fingerprint captured before the dispatch
    dispatches: int
    batch_attempts: int  # consecutive dispatches on the current batch
    stalls: int  # consecutive stalls
    stalls_total: int
    restarts: int
    gate_fails: dict[str, int]  # consecutive failures per gate
    review_rounds: int
    spec_rounds: int  # consecutive spec-gate rejections
    spec_approved: bool  # spec gate passed (skip re-authoring on resume)
    gauntlet_rounds: int  # consecutive gauntlet rejections
    batches_done: int
    cost_usd: float  # accumulated agent cost (0 when models are unpriced)
    verdict: str
    park_reason: str
    park_target: str
    approval_comments: str
    finalized: bool
    log: list[str]


class LoopNode(Protocol):
    """One graph node: reads the loop state, returns a Command (routing + update).

    Declared as a Protocol (not `Callable`) because mypy cannot match a bare
    `Callable[[LoopState], Command]` against LangGraph's `StateNode` union.
    """

    def __call__(self, state: LoopState) -> Any: ...


class ReviewVerdict(BaseModel):
    """Structured reviewer verdict (the runtime writes it to review_<name>.md)."""

    verdict: Literal["APPROVED", "CHANGES_REQUESTED"]
    summary: str = ""
    traceability: list[str] = Field(default_factory=list)
    required_changes: list[str] = Field(default_factory=list)


class GauntletVerdict(BaseModel):
    """Structured G2+G3 verdict (G1/G4 are mechanical: the node runs them)."""

    verdict: Literal["APPROVED", "REJECTED"]
    g2_findings: list[str] = Field(default_factory=list)  # adversarial: fixed|defended
    g3_notes: str = ""
    summary: str = ""


REVIEW_INSTRUCTIONS = """You are the reviewer agent of a harness-initialized project.
You never edit code — you approve or reject work against the spec.

Protocol (one pass over the WHOLE feature, never per batch):
1. Read docs/architecture.md, docs/conventions.md, harness/CHECKPOINTS.md and
   harness/specs/<feature>/ (requirements.md, design.md, tasks.md).
2. Requirement traceability: for each R<n> in requirements.md locate at least
   one concrete test that verifies it. Missing coverage → CHANGES_REQUESTED.
3. Task completion: ALL tasks in tasks.md must be [x], unless justified in
   harness/progress/impl_<feature>.md.
4. For each modified file: does it respect architecture.md and conventions.md,
   and does it have a corresponding test?
5. Run harness/init.sh yourself via run_command — it must finish green
   (last line [OK]).
6. audit_level from feature_list: basic → confirm the Security Audit Checklist
   of docs/verification.md manually (record it in your summary); standard →
   run bash harness/tools/audit-security.sh when present; strict → also
   bash harness/tools/bench.sh when present.
7. Issue the verdict.

Hard rules: never APPROVED with failing tests, failing harness/init.sh,
uncovered R<n>, or unfinished tasks. Be specific — cite files and lines; no
generic feedback. Verify everything yourself; a claim you cannot re-check on
disk is a defect."""


# ---------- context ----------


@dataclass
class LoopContext:
    """Everything one feature-loop run needs; rebuildable from disk on resume."""

    project: HarnessProject
    settings: Settings
    feature: Feature
    worktree: Path
    sandbox: Sandbox
    registry: ModelRegistry
    max_tasks: int = DEFAULT_MAX_TASKS
    max_batches: int = DEFAULT_MAX_BATCHES
    max_batch_attempts: int = MAX_BATCH_ATTEMPTS
    max_gate_cycles: int = MAX_GATE_CYCLES
    max_review_rounds: int = MAX_REVIEW_ROUNDS
    max_spec_rounds: int = MAX_SPEC_ROUNDS
    dispatch: Callable[[str], str] | None = None
    review: Callable[[str], ReviewVerdict] | None = None
    spec: Callable[[str], str] | None = None
    gauntlet: Callable[[str], GauntletVerdict] | None = None
    last_cost: float = 0.0  # cost delta of the last agent call (0 when unpriced)

    def record_usage(self, role: str, model_id: str, usage: Any) -> float:
        """Fold one call's token usage into last_cost (phase-4 cost feedback).

        Returns the delta in USD; 0.0 when the registry entry carries no
        prices — an unpriced model must not silently report $0.00 spent.
        """
        spec = self.registry.spec(model_id)
        cost = spec.cost_usd(
            getattr(usage, "input_tokens", 0) or 0,
            getattr(usage, "output_tokens", 0) or 0,
        )
        self.last_cost = cost or 0.0
        return self.last_cost

    def run_dispatch(self, prompt: str) -> str:
        """One dispatch = one fresh agent instance (no shared session)."""
        fn = self.dispatch if self.dispatch is not None else _default_dispatch(self)
        return fn(prompt)

    def run_review(self, prompt: str) -> ReviewVerdict:
        fn = self.review if self.review is not None else _default_review(self)
        return fn(prompt)

    def run_spec(self, prompt: str) -> str:
        fn = self.spec if self.spec is not None else _default_spec(self)
        return fn(prompt)

    def run_gauntlet(self, prompt: str) -> GauntletVerdict:
        fn = self.gauntlet if self.gauntlet is not None else _default_gauntlet(self)
        return fn(prompt)


def make_context(
    project: HarnessProject,
    settings: Settings,
    feature_name: str,
    *,
    max_tasks: int = DEFAULT_MAX_TASKS,
    max_batches: int = DEFAULT_MAX_BATCHES,
    dispatch: Callable[[str], str] | None = None,
    review: Callable[[str], ReviewVerdict] | None = None,
    spec: Callable[[str], str] | None = None,
    gauntlet: Callable[[str], GauntletVerdict] | None = None,
) -> LoopContext:
    """Resolve the feature, (re)create its worktree and load runtime caps (D1)."""
    feat = next((f for f in project.feature_list.features if f.name == feature_name), None)
    if feat is None:
        raise KeyError(f"feature '{feature_name}' not found in {project.root}")
    worktree = create_worktree(project.root, feat.name)
    sandbox = Sandbox.from_project(
        worktree, log_dir=worktree / "harness" / "logs" / feat.name
    )
    return LoopContext(
        project=project,
        settings=settings,
        feature=feat,
        worktree=worktree,
        sandbox=sandbox,
        registry=ModelRegistry.load(settings.models_file),
        max_tasks=max_tasks,
        max_batches=max_batches,
        **_loop_caps(project.root),
        dispatch=dispatch,
        review=review,
        spec=spec,
        gauntlet=gauntlet,
    )


def _loop_caps(root: Path) -> dict[str, int]:
    """Optional cycle caps in harness/runtime.yaml (D1: project-owned config)."""
    cfg = root / "harness" / "runtime.yaml"
    if not cfg.is_file():
        return {}
    data = yaml.safe_load(cfg.read_text(encoding="utf-8")) or {}
    caps: dict[str, int] = {}
    for key in LOOP_CAP_KEYS:
        if key in data:
            caps[key] = int(data[key])
    return caps


def _default_dispatch(ctx: LoopContext) -> Callable[[str], str]:
    def dispatch(prompt: str) -> str:
        model_id = ctx.settings.role_model("worker")
        model = ctx.registry.build(model_id)
        agent = build_implementer(model, ctx.sandbox)
        runlog.emit(f"worker ({model_id}): working…", ctx.project.root)
        result = agent.run_sync(prompt, deps=ctx.sandbox)
        ctx.record_usage("worker", model_id, result.usage)
        runlog.emit(f"worker → {_short(result.output)}", ctx.project.root)
        return result.output

    return dispatch


SPEC_INSTRUCTIONS = """You are the spec-author agent of a harness-initialized project.
You write the spec trio for ONE feature, following docs/specs.md (Spec Driven
Development). You never touch src/ or tests/ — only the spec files.

Write exactly these files inside harness/specs/<feature>/ :
1. requirements.md — EARS notation. One `R<n>` per requirement (R1, R2, …),
   each a single SHALL statement, stable ids, no should/may. End with a
   Traceability section: a markdown table with one row per requirement
   (`| R<n> | | | pending |`), cells filled in later by the implementer.
2. design.md — context, approach, files to create/modify, test strategy.
   Keep it short: only decisions a stranger could not reinvent.
3. tasks.md — 2–6 `T<n>` checkboxes, each `- [ ] T<n>: <what> (refs R<a>, R<b>)`.
   Every requirement must be referenced by at least one task; every task must
   be small enough for one batch (a change plus its tests).

Hard rules: requirements come from the feature's acceptance criteria — do not
invent scope. Use the write_file tool for each file; verify nothing else.
Reply DONE when the three files are written."""


def _default_spec(ctx: LoopContext) -> Callable[[str], str]:
    """Spec-author: planner-routed agent with file tools (writes only specs)."""

    def spec(prompt: str) -> str:
        model_id = ctx.settings.role_model("planner")
        model = ctx.registry.build(model_id)
        agent: Agent[Sandbox, str] = Agent(
            model, deps_type=Sandbox, instructions=SPEC_INSTRUCTIONS
        )
        runlog.emit(f"spec-author ({model_id}): writing the trio…", ctx.project.root)

        @agent.tool
        def read_file(run_ctx: RunContext[Sandbox], path: str) -> str:
            """Read a file from the worktree."""
            return tool_read_file(run_ctx.deps, path)

        @agent.tool
        def write_file(run_ctx: RunContext[Sandbox], path: str, content: str) -> str:
            """Create or overwrite a file in the worktree."""
            return tool_write_file(run_ctx.deps, path, content)

        @agent.tool
        def list_dir(run_ctx: RunContext[Sandbox], path: str = ".") -> str:
            """List a directory of the worktree."""
            return tool_list_dir(run_ctx.deps, path)

        result = agent.run_sync(prompt, deps=ctx.sandbox)
        ctx.record_usage("planner", model_id, result.usage)
        runlog.emit(f"spec-author → {len(result.output)} chars", ctx.project.root)
        return result.output

    return spec


GAUNTLET_INSTRUCTIONS = """You are the gauntlet reviewer of a harness-initialized project
(module: gauntlet). G1 (mechanical correctness) and G4 (evidence) are verified by the
runtime — your passes are:

- **G2 — Adversarial**: walk the diff with hostile eyes — malicious input, empty/null/
  negative values, races, brittle tests (docs/iteration-protocol.md checklist). Every
  finding is either FIXED by you (never: you cannot edit) or must be REJECTED with the
  finding listed in g2_findings.
- **G3 — Maintainability**: architecture.md and conventions.md respected, no blanket
  `except`, I/O at the edges, no speculative abstraction. Note conclusions in g3_notes.

Read docs/gauntlet-protocol.md first, then the diff (`git diff main` content via
list_dir/read_file) and the spec trio. Hard rule: REJECT when any G2 finding is real
and unfixed — do not negotiate with quality. APPROVED means you would bet production
traffic on this diff."""


def _default_gauntlet(ctx: LoopContext) -> Callable[[str], GauntletVerdict]:
    def gauntlet(prompt: str) -> GauntletVerdict:
        model_id = ctx.settings.role_model("reviewer")
        model = ctx.registry.build(model_id)
        agent: Agent[Sandbox, GauntletVerdict] = Agent(
            model,
            output_type=GauntletVerdict,
            deps_type=Sandbox,
            instructions=GAUNTLET_INSTRUCTIONS,
        )
        runlog.emit(f"gauntlet reviewer ({model_id}): G2+G3 pass…", ctx.project.root)

        @agent.tool
        def read_file(run_ctx: RunContext[Sandbox], path: str) -> str:
            """Read a file from the worktree."""
            return tool_read_file(run_ctx.deps, path)

        @agent.tool
        def list_dir(run_ctx: RunContext[Sandbox], path: str = ".") -> str:
            """List a directory of the worktree."""
            return tool_list_dir(run_ctx.deps, path)

        @agent.tool
        def run_command(run_ctx: RunContext[Sandbox], command: str) -> str:
            """Run an allowlisted command (tests, gates) in the worktree."""
            return tool_run_command(run_ctx.deps, command)

        result = agent.run_sync(prompt, deps=ctx.sandbox)
        ctx.record_usage("reviewer", model_id, result.usage)
        runlog.emit(
            f"gauntlet → {result.output.verdict} "
            f"({len(result.output.g2_findings)} G2 findings)",
            ctx.project.root,
        )
        return result.output

    return gauntlet


def _default_review(ctx: LoopContext) -> Callable[[str], ReviewVerdict]:
    def review(prompt: str) -> ReviewVerdict:
        model_id = ctx.settings.role_model("reviewer")
        model = ctx.registry.build(model_id)
        agent: Agent[Sandbox, ReviewVerdict] = Agent(
            model,
            output_type=ReviewVerdict,
            deps_type=Sandbox,
            instructions=REVIEW_INSTRUCTIONS,
        )
        runlog.emit(f"reviewer ({model_id}): reviewing…", ctx.project.root)

        @agent.tool
        def read_file(run_ctx: RunContext[Sandbox], path: str) -> str:
            """Read a file from the worktree."""
            return tool_read_file(run_ctx.deps, path)

        @agent.tool
        def list_dir(run_ctx: RunContext[Sandbox], path: str = ".") -> str:
            """List a directory of the worktree."""
            return tool_list_dir(run_ctx.deps, path)

        @agent.tool
        def run_command(run_ctx: RunContext[Sandbox], command: str) -> str:
            """Run an allowlisted command (tests, gates) in the worktree."""
            return tool_run_command(run_ctx.deps, command)

        result = agent.run_sync(prompt, deps=ctx.sandbox)
        ctx.record_usage("reviewer", model_id, result.usage)
        runlog.emit(
            f"reviewer → {result.output.verdict} "
            f"({len(result.output.required_changes)} required changes)",
            ctx.project.root,
        )
        return result.output

    return review


# ---------- worktree fingerprint (stall detection) ----------


def _git(cwd: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", "-C", str(cwd), *args],
        check=False,
        capture_output=True,
        text=True,
    )


def _hash_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


def worktree_fingerprint(worktree: Path) -> str:
    """Content-aware fingerprint of the worktree's uncommitted state.

    `git status` alone misses the content of untracked files (rewriting one
    keeps the same `?? path` line), so untracked files are hashed too.
    """
    porcelain = _git(worktree, "status", "--porcelain").stdout
    parts = [porcelain, _git(worktree, "diff").stdout]
    for line in porcelain.splitlines():
        if not line.startswith("??"):
            continue
        target = worktree / line[3:].strip()
        if target.is_file():
            parts.append(_hash_file(target))
        elif target.is_dir():
            for f in sorted(p for p in target.rglob("*") if p.is_file()):
                parts.append(f"{f.relative_to(worktree)}:{_hash_file(f)}")
    return hashlib.sha256("\n".join(parts).encode("utf-8", "replace")).hexdigest()


# ---------- artifact writes (only reachable through the gates) ----------


def set_feature_status(root: Path, feature_name: str, status: str) -> None:
    """Flip one feature's status in harness/feature_list.json (validated at write)."""
    path = root / "harness" / "feature_list.json"
    data = json.loads(path.read_text(encoding="utf-8"))
    for entry in data.get("features", []):
        if entry.get("name") == feature_name:
            entry["status"] = status
            break
    else:
        raise KeyError(f"feature '{feature_name}' not in {path}")
    FeatureList.model_validate(data)  # hard schema at write — never corrupt the board
    path.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")


def retro_line(state: Mapping[str, Any]) -> str:
    """The protocol's retro line, machine-generated (cost when priced)."""
    line = (
        f"retro: {state.get('dispatches', 0)} dispatches · "
        f"{state.get('stalls_total', 0)} stalls · {state.get('restarts', 0)} restarts"
    )
    cost = state.get("cost_usd") or 0.0
    if cost > 0:
        line += f" · ${cost:.4f}"
    return line


class FinalizeError(RuntimeError):
    """Completion-gate bookkeeping failed; the loop parks and retries on approve."""


def _run_or_raise(cwd: Path, args: list[str], what: str) -> None:
    proc = _git(cwd, *args)
    if proc.returncode != 0:
        raise FinalizeError(f"{what} failed: {(proc.stderr or proc.stdout).strip()}")


def _human() -> str:
    try:
        return getpass.getuser()
    except Exception:  # noqa: BLE001 — no passwd entry in some sandboxes
        return "unknown"


def _commit_main_status(root: Path, feature_name: str, status: str) -> None:
    """Flip status on main AND commit it, leaving main clean (harness hard rule).

    The runtime owns `feature_list.json`; a status flip is a protocol event, so
    it is committed immediately. This keeps `finalize`'s "main must be clean"
    gate honest — an uncommitted flip would otherwise block the merge forever.
    Parallel mode serializes main-repo mutations through MAIN_LOCK.
    """
    with MAIN_LOCK:
        set_feature_status(root, feature_name, status)
        if not _git(root, "status", "--porcelain").stdout.strip():
            return  # nothing to commit (e.g. status already matched)
        _run_or_raise(root, ["add", "-A"], "stage the status flip")
        _run_or_raise(
            root,
            [*GIT_IDENTITY, "commit", "-m", f"chore({feature_name}): status {status}"],
            "commit the status flip",
        )


def _finalize(ctx: LoopContext, state: LoopState) -> list[str]:
    """Merge, then APPROVAL → status done → history.md + retro, then commit clean."""
    root, worktree, feat = ctx.project.root, ctx.worktree, ctx.feature
    lines: list[str] = []
    identity = GIT_IDENTITY

    # 1. commit the worktree (the runtime is the committer; the commit-msg hook
    #    still runs and enforces Conventional Commits)
    if _git(worktree, "status", "--porcelain").stdout.strip():
        _run_or_raise(worktree, ["add", "-A"], "stage worktree changes")
        _run_or_raise(
            worktree,
            [*identity, "commit", "-m", f"feat({feat.name}): {feat.title}"],
            "commit the worktree",
        )
        lines.append(f"worktree committed: feat({feat.name}): {feat.title}")

    # steps 2-8 mutate main: parallel mode serializes them through MAIN_LOCK
    with MAIN_LOCK:
        # 2. main must be clean before the completion gate may merge
        dirty = _git(root, "status", "--porcelain").stdout.strip()
        if dirty:
            raise FinalizeError(
                "main has uncommitted changes — commit or stash them, then approve "
                "again:\n" + dirty
            )

        # 3. merge-or-discard is the completion gate's decision; approve = merge
        merge = _git(
            root,
            *identity,
            "merge",
            f"buildwright/{feat.name}",
        )
        if merge.returncode != 0:
            raise FinalizeError(
                "merge into main failed — resolve it in the main worktree "
                "(finish the merge or `git merge --abort`), then approve again:\n"
                + (merge.stderr or merge.stdout).strip()
            )
        lines.append(f"merged buildwright/{feat.name} into main")

        # 4. APPROVAL record — only after an explicit completion-gate approval (EARS)
        approval = root / "harness" / "specs" / feat.name / "APPROVAL"
        approval.parent.mkdir(parents=True, exist_ok=True)
        approval.write_text(
            f"# APPROVAL — {feat.name}\n\n"
            f"- human: {_human()}\n"
            f"- date: {date.today().isoformat()}\n"
            f"- gate: completion gate (`buildwright harness approve`)\n"
            f"- comments: {state.get('approval_comments', '') or '-'}\n",
            encoding="utf-8",
        )
        lines.append(f"APPROVAL written: {approval.relative_to(root)}")

        # 5. status done — unreachable without the APPROVAL written above
        set_feature_status(root, feat.name, "done")
        lines.append(f"status: {feat.name} → done")

        # 6. history.md + retro line (append-only; marker keeps retries idempotent)
        history = root / "harness" / "progress" / "history.md"
        history.parent.mkdir(parents=True, exist_ok=True)
        marker = f"### {feat.name} —"
        existing = history.read_text(encoding="utf-8") if history.exists() else ""
        if marker not in existing:
            entry = (
                f"\n{marker} {feat.title} ({date.today().isoformat()})\n\n"
                f"- approved by {_human()} · reviewer: {state.get('verdict', 'APPROVED')} · "
                f"batches: {state.get('batches_done', 0)}\n"
                f"{retro_line(state)}\n"
            )
            with history.open("a", encoding="utf-8") as fh:
                fh.write(entry)
            lines.append(f"history.md updated — {retro_line(state)}")

        # 7. commit the bookkeeping so main is left clean (harness hard rule)
        if _git(root, "status", "--porcelain").stdout.strip():
            _run_or_raise(root, ["add", "-A"], "stage the bookkeeping")
            _run_or_raise(
                root,
                [*identity, "commit", "-m", f"chore({feat.name}): approval record + status done"],
                "commit the bookkeeping",
            )
            lines.append("bookkeeping committed to main")

        # 8. cleanup: everything of value is committed (1) and merged (3), so the
        #    worktree is disk residue and the branch a dangling ref. Best-effort —
        #    the feature is already done; a failed cleanup must not fail the run.
        removal = _git(root, "worktree", "remove", str(worktree))
        if removal.returncode != 0:
            # untracked gate logs / .venv leftovers block a clean removal; --force
            # only discards what step 1 did not consider worth committing
            _git(root, "worktree", "remove", "--force", str(worktree))
        if worktree.exists():
            lines.append(f"worktree kept: {worktree.relative_to(root)} (remove failed)")
        else:
            lines.append(f"worktree removed: {worktree.relative_to(root)}")
            _git(root, "worktree", "prune")
            branch = f"buildwright/{feat.name}"
            if _git(root, "branch", "-d", branch).returncode == 0:
                lines.append(f"branch deleted: {branch} (merged)")
            else:
                lines.append(f"branch kept: {branch}")
    return lines


# ---------- prompts + review artifact ----------


def _read_tasks(ctx: LoopContext) -> list[Task]:
    path = ctx.worktree / "harness" / "specs" / ctx.feature.name / "tasks.md"
    if not path.is_file():
        raise FileNotFoundError(
            f"missing {path} (spec = requirements.md + design.md + tasks.md)"
        )
    return parse_tasks(path.read_text(encoding="utf-8"))


def _dispatch_prompt(ctx: LoopContext, state: LoopState, batch: list[Task]) -> str:
    """The dispatch names the batch and the gates — and nothing else."""
    feat = ctx.feature
    task_lines = "\n".join(f"  {t.task_id}: {t.text}" for t in batch) or "  (none)"
    prompt = (
        f"Feature: {feat.name} — {feat.title}\n"
        f"Spec: harness/specs/{feat.name}/ (requirements.md, design.md, tasks.md)\n"
        f"Acceptance: {'; '.join(feat.acceptance) or '-'}\n"
        f"Your batch: {', '.join(t.task_id for t in batch)}\n"
        f"{task_lines}\n"
        "Work mode: write files before any prose. After EACH task run the quality "
        "gates via run_command (harness/init.sh must end [OK]); mark `- [x]` in "
        f"harness/specs/{feat.name}/tasks.md only when green.\n"
        f"Append what you did to harness/progress/impl_{feat.name}.md.\n"
    )
    feedback = state.get("dispatch_prompt", "")
    if feedback:
        prompt += f"\nPrevious attempt needs fixing:\n{feedback}\n"
    prompt += (
        "\nFinal response: exactly one line — "
        "`done <T-ids> -> <main files>` or `blocked <task>: <reason>`."
    )
    return prompt


def _review_prompt(ctx: LoopContext) -> str:
    feat = ctx.feature
    level = ctx.project.feature_list.project.audit_level
    return (
        f"Review the WHOLE feature {feat.name} — {feat.title} in one pass.\n"
        f"Spec: harness/specs/{feat.name}/requirements.md (R<n>), design.md, tasks.md.\n"
        "Also read: docs/architecture.md, docs/conventions.md, harness/CHECKPOINTS.md, "
        f"harness/progress/impl_{feat.name}.md.\n"
        f"Acceptance: {'; '.join(feat.acceptance) or '-'}\n"
        f"audit_level: {level}\n"
        "Verify yourself with read_file / list_dir / run_command — never trust "
        "chat claims."
    )


def _write_review(ctx: LoopContext, verdict: ReviewVerdict) -> Path:
    progress = ctx.worktree / "harness" / "progress"
    progress.mkdir(parents=True, exist_ok=True)
    path = progress / f"review_{ctx.feature.name}.md"
    lines = [
        f"# Review — feature {ctx.feature.name}",
        "",
        f"**Verdict:** {verdict.verdict}",
        "",
    ]
    if verdict.summary:
        lines += [verdict.summary, ""]
    if verdict.traceability:
        lines += ["## Requirement traceability ↔ tests"]
        lines += [f"- {item}" for item in verdict.traceability]
        lines.append("")
    if verdict.required_changes:
        lines += ["## Required changes"]
        lines += [
            f"{i}. {item}" for i, item in enumerate(verdict.required_changes, 1)
        ]
        lines.append("")
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path


# ---------- hard gates (scripts this protocol already has) ----------


@dataclass(frozen=True)
class GateOutcome:
    ok: bool
    feedback: str  # what the implementer gets when the gate fails
    log: str  # line for the loop log


def _run_gate(
    ctx: LoopContext, name: str, command: str, *, require_ok_marker: bool = False
) -> GateOutcome:
    """Run a gate command in the worktree; archive failures to harness/logs."""
    out = tool_run_command(ctx.sandbox, command)
    if out.startswith("ERROR:"):
        return GateOutcome(False, f"{name} gate: {out}", f"{name}: {out}")
    head, _, rest = out.partition("\n")
    try:
        code = int(head.removeprefix("exit="))
    except ValueError:
        return GateOutcome(False, f"{name} gate: bad output {out!r}", f"{name}: bad output")
    lines = [line for line in rest.splitlines() if line.strip()]
    last = lines[-1] if lines else ""
    if code == 0 and (not require_ok_marker or "[OK]" in last):
        return GateOutcome(True, "", f"{name}: green ({command})")
    log_path = ctx.worktree / "harness" / "logs" / ctx.feature.name / f"gate_{name}.log"
    log_path.parent.mkdir(parents=True, exist_ok=True)
    log_path.write_text(out, encoding="utf-8")
    rel = log_path.relative_to(ctx.worktree)
    tail = "\n".join(lines[-15:])
    feedback = (
        f"{name} gate FAILED — `{command}` (exit {code}):\n{tail}\nfull log: {rel}"
    )
    return GateOutcome(False, feedback, f"{name}: FAILED exit {code} (log {rel})")


def run_init_gate(ctx: LoopContext) -> GateOutcome:
    """EARS: non-zero exit OR last line without [OK] → back to implement_batch."""
    if not (ctx.worktree / "harness" / "init.sh").is_file():
        return GateOutcome(True, "", "init.sh not present — gate skipped")
    return _run_gate(ctx, "init", "bash harness/init.sh", require_ok_marker=True)


def run_trace_gate(ctx: LoopContext) -> GateOutcome:
    """`--all` gates the `done` features only, so the feature being completed
    (still `in_progress`: it flips to `done` at finalize) would never be
    mechanically checked — its table would rely solely on the reviewer. Run a
    second pass for it. Older harness standards without `--feature` degrade
    to the `--all` pass (feature name is schema-validated `^[a-z0-9_]+$`, so
    interpolating it into the command is safe)."""
    if not (ctx.worktree / "harness" / "tools" / "check-traceability.py").is_file():
        return GateOutcome(True, "", "check-traceability.py not present — gate skipped")
    checks: list[tuple[str, str]] = [
        ("traceability", "python3 harness/tools/check-traceability.py --all")
    ]
    # Old harness standards have no `--feature` flag: probe the script text
    # (its parser lists known flags) instead of running it, since it exits 2
    # for any unknown argument — indistinguishable from a real gap.
    script = ctx.worktree / "harness" / "tools" / "check-traceability.py"
    if "--feature" in script.read_text(encoding="utf-8"):
        feature_cmd = (
            f"python3 harness/tools/check-traceability.py --feature {ctx.feature.name}"
        )
        checks.append(("traceability-feature", feature_cmd))
    done: list[str] = []
    for name, command in checks:
        outcome = _run_gate(ctx, name, command)
        if not outcome.ok:
            return GateOutcome(
                False, outcome.feedback, " | ".join([*done, outcome.log])
            )
        done.append(outcome.log)
    return GateOutcome(True, "", " | ".join(done))


def run_audit_gate(ctx: LoopContext) -> GateOutcome:
    """EARS: standard/strict run their scripts and reject on non-zero exit."""
    level = ctx.project.feature_list.project.audit_level
    if level == "basic":
        return GateOutcome(
            True, "", "audit: basic — manual checklist (docs/verification.md)"
        )
    checks = [("audit-security", "bash harness/tools/audit-security.sh")]
    if level == "strict":
        checks.append(("bench", "bash harness/tools/bench.sh"))
    done: list[str] = []
    for name, command in checks:
        if not (ctx.worktree / command.split()[-1]).is_file():
            done.append(f"{name}: not present — skipped")
            continue
        outcome = _run_gate(ctx, name, command)
        if not outcome.ok:
            return GateOutcome(
                False, outcome.feedback, "audit: " + " | ".join([*done, outcome.log])
            )
        done.append(outcome.log)
    return GateOutcome(True, "", "audit: " + " | ".join(done))


# ---------- spec phase (pending → spec_ready → in_progress) ----------


def verify_spec(ctx: LoopContext) -> tuple[bool, str]:
    """The spec trio exists and parses: requirements with R ids, tasks with T ids."""
    base = ctx.worktree / "harness" / "specs" / ctx.feature.name
    missing = [
        name
        for name in ("requirements.md", "design.md", "tasks.md")
        if not (base / name).is_file()
    ]
    if missing:
        return False, f"missing spec files: {', '.join(missing)}"
    if not _read_tasks(ctx):
        return False, "tasks.md has no `- [ ] T<n>:` tasks"
    requirements = (base / "requirements.md").read_text(encoding="utf-8")
    if not re.search(r"\bR\d+\b", requirements):
        return False, "requirements.md has no R<n> requirements"
    return True, ""


def _spec_prompt(ctx: LoopContext, feedback: str) -> str:
    feat = ctx.feature
    acceptance = "\n".join(f"- {a}" for a in feat.acceptance)
    prompt = (
        f"Write the spec trio for feature '{feat.name}' — {feat.title}.\n"
        f"\nDescription:\n{feat.description}\n"
        f"\nAcceptance criteria:\n{acceptance}\n"
    )
    if feedback:
        prompt += f"\nThe human rejected the previous spec:\n{feedback}\nRevise it.\n"
    return prompt


def _spec_node(ctx: LoopContext) -> LoopNode:
    """pending → spec_author → verify → spec_gate. Already in_progress → skip."""

    def node(state: LoopState) -> Command:
        if ctx.feature.status == "in_progress":
            # phase-2 entry: the spec was approved interactively before the run
            return Command(goto="implement_batch", update={"spec_approved": True})
        # a stall re-dispatch opens with what was missing (state-borne feedback)
        out = ctx.run_spec(_spec_prompt(ctx, state.get("dispatch_prompt", "")))
        cost = round(state.get("cost_usd", 0.0) + ctx.last_cost, 6)
        log = [
            *state.get("log", []),
            f"spec: authored ({len(out)} chars)",
            *([state["dispatch_prompt"]] if state.get("dispatch_prompt") else []),
        ]
        ok, reason = verify_spec(ctx)
        if not ok:
            # stall protocol: same dispatch again, opened with what was missing
            attempts = state.get("batch_attempts", 0) + 1
            if attempts >= ctx.max_batch_attempts:
                return Command(
                    goto="park",
                    update={
                        "park_reason": f"spec incomplete {attempts}× in a row: {reason}",
                        "park_target": "spec_phase",
                        "cost_usd": cost,
                        "log": [*log, f"spec: INCOMPLETE — {reason}"],
                    },
                )
            return Command(
                goto="spec_phase",
                update={
                    "batch_attempts": attempts,
                    "dispatch_prompt": f"Nothing was written: {reason}. Create the "
                    "spec files now.",
                    "cost_usd": cost,
                    "log": [*log, f"spec: INCOMPLETE — {reason}"],
                },
            )
        set_feature_status(ctx.project.root, ctx.feature.name, "spec_ready")
        _commit_main_status(ctx.project.root, ctx.feature.name, "spec_ready")
        return Command(
            goto="spec_gate",
            update={
                "batch_attempts": 0,
                "dispatch_prompt": "",
                "cost_usd": cost,
                "log": [*log, "spec: verified → spec_ready"],
            },
        )

    return node


def _spec_gate_node(ctx: LoopContext) -> LoopNode:
    """Human gate on the spec: approve → in_progress → implement_batch."""

    def node(state: LoopState) -> Command:
        answer = interrupt(
            {
                "gate": "spec",
                "prompt": (
                    f"{ctx.feature.name} — {ctx.feature.title}: spec trio written to "
                    f"harness/specs/{ctx.feature.name}/. Read it, then approve to "
                    "start implementation or reject with changes for the spec-author."
                ),
                "summary": {"feature": ctx.feature.name, "rounds": state.get("spec_rounds", 0)},
            }
        )
        decision, comments = _decision(answer)
        log = [*state.get("log", []), f"spec gate: {decision}"]
        if decision == "approve":
            set_feature_status(ctx.project.root, ctx.feature.name, "in_progress")
            _commit_main_status(ctx.project.root, ctx.feature.name, "in_progress")
            batch = next_batch(_read_tasks(ctx), ctx.max_tasks)
            if not batch:
                return Command(
                    goto="park",
                    update={
                        "park_reason": (
                            "spec approved but tasks.md has no pending tasks — "
                            "fix it and approve again"
                        ),
                        "park_target": "spec_phase",
                        "log": [*log, "spec gate: no pending tasks after approval"],
                    },
                )
            return Command(
                goto="implement_batch",
                update={
                    "spec_approved": True,
                    "spec_rounds": 0,
                    "batch": [t.task_id for t in batch],
                    "log": log,
                },
            )
        rounds = state.get("spec_rounds", 0) + 1
        log = [*log, f"spec gate: rejected (round {rounds})"]
        update: LoopState = {"spec_rounds": rounds, "log": log}
        if rounds >= ctx.max_spec_rounds:
            update["park_reason"] = (
                f"spec rejected {rounds}× — human gate (revise harness/specs/"
                f"{ctx.feature.name}/ yourself, then approve)"
            )
            update["park_target"] = "spec_phase"
            return Command(goto="park", update=update)
        update["dispatch_prompt"] = (
            f"Spec rejected by the human (round {rounds}):\n{comments or '(no details)'}"
        )
        return Command(goto="spec_phase", update=update)

    return node


# ---------- nodes ----------


def _implement_node(ctx: LoopContext) -> LoopNode:
    def node(state: LoopState) -> Command:
        batch_ids = state.get("batch", [])
        attempts = state.get("batch_attempts", 0)
        if attempts >= ctx.max_batch_attempts:
            # the previous dispatches made no progress: park, do not burn another
            return Command(
                goto="park",
                update={
                    "park_reason": (
                        f"batch [{', '.join(batch_ids)}] made no progress in "
                        f"{attempts} dispatches — cycle cap ({ctx.max_batch_attempts})"
                    ),
                    "park_target": "implement_batch",
                    "log": [
                        *state.get("log", []),
                        (
                            f"cycle cap: batch [{', '.join(batch_ids)}] parked after "
                            f"{attempts} dispatches"
                        ),
                    ],
                },
            )
        by_id = {t.task_id: t for t in _read_tasks(ctx)}
        batch = [by_id[i] for i in batch_ids if i in by_id]
        prompt = _dispatch_prompt(ctx, state, batch)
        dispatches = state.get("dispatches", 0) + 1
        runlog.emit(
            f"dispatch #{dispatches} [{', '.join(batch_ids)}] → "
            f"{ctx.settings.role_model('worker')}",
            ctx.project.root,
        )
        fingerprint = worktree_fingerprint(ctx.worktree)  # captured BEFORE the dispatch
        output = ctx.run_dispatch(prompt)
        runlog.emit(
            f"dispatch #{dispatches} → {_short(output)}", ctx.project.root
        )
        return Command(
            goto="verify",
            update={
                "dispatches": dispatches,
                "batch_attempts": attempts + 1,
                "fingerprint": fingerprint,
                "dispatch_prompt": "",
                "cost_usd": round(state.get("cost_usd", 0.0) + ctx.last_cost, 6),
                "log": [
                    *state.get("log", []),
                    f"dispatch #{dispatches} [{', '.join(batch_ids)}] → {_short(output)}",
                ],
            },
        )

    return node


def _short(text: str, limit: int = 160) -> str:
    clean = " ".join(text.split())
    return clean if len(clean) <= limit else clean[: limit - 1] + "…"


def _verify_node(ctx: LoopContext) -> LoopNode:
    def node(state: LoopState) -> Command:
        batch_ids = state.get("batch", [])
        tasks_path = ctx.worktree / "harness" / "specs" / ctx.feature.name / "tasks.md"
        if not tasks_path.is_file():
            return Command(
                goto="park",
                update={
                    "park_reason": f"spec disappeared: {tasks_path}",
                    "park_target": "implement_batch",
                    "log": [*state.get("log", []), f"verify: missing {tasks_path}"],
                },
            )
        by_id = {t.task_id: t for t in parse_tasks(tasks_path.read_text(encoding="utf-8"))}
        batch = [by_id[i] for i in batch_ids if i in by_id]
        marked = {t.task_id for t in batch if t.done}
        log = list(state.get("log", []))

        if worktree_fingerprint(ctx.worktree) == state.get("fingerprint", ""):
            # stall: nothing was written — re-dispatch the SAME batch with the
            # protocol's message; two in a row start a fresh agent instance
            stalls = state.get("stalls", 0) + 1
            restarts = state.get("restarts", 0)
            unmarked = [t.task_id for t in batch if not t.done] or list(batch_ids)
            first = unmarked[0] if unmarked else "the task"
            msg = (
                f"Nothing was written: {', '.join(unmarked)}. "
                f"Create the first file for {first} now."
            )
            if stalls >= MAX_STALLS:
                restarts += 1
                stalls = 0
                msg += " (fresh session — the previous attempts produced nothing)"
            log.append(
                f"stall {state.get('stalls_total', 0) + 1}: "
                f"[{', '.join(batch_ids)}] wrote nothing — re-dispatch"
            )
            return Command(
                goto="implement_batch",
                update={
                    "stalls": stalls,
                    "stalls_total": state.get("stalls_total", 0) + 1,
                    "restarts": restarts,
                    "dispatch_prompt": msg,
                    "log": log,
                },
            )

        if len(marked) != len(batch):
            missing = [t.task_id for t in batch if not t.done]
            log.append(f"verify: incomplete — {', '.join(missing)} not marked on disk")
            runlog.emit(log[-1], ctx.project.root)
            return Command(
                goto="implement_batch",
                update={
                    "dispatch_prompt": (
                        f"Batch incomplete on disk: {', '.join(missing)} still `[ ]`. "
                        "Finish those tasks and mark them [x] only when the gates "
                        "are green."
                    ),
                    "log": log,
                },
            )

        log.append(f"verify: [{', '.join(batch_ids)}] marked on disk, worktree changed")
        runlog.emit(log[-1], ctx.project.root)
        return Command(
            goto="init_gate",
            update={"stalls": 0, "batch_attempts": 0, "log": log},
        )

    return node


def _gate_node(
    ctx: LoopContext,
    name: str,
    run: Callable[[LoopContext], GateOutcome],
    on_pass: str,
) -> LoopNode:
    def node(state: LoopState) -> Command:
        outcome = run(ctx)
        runlog.emit(outcome.log, ctx.project.root)
        fails = dict(state.get("gate_fails", {}))
        log = [*state.get("log", []), outcome.log]
        if outcome.ok:
            fails.pop(name, None)
            return Command(goto=on_pass, update={"gate_fails": fails, "log": log})
        fails[name] = fails.get(name, 0) + 1
        update: LoopState = {
            "gate_fails": fails,
            "dispatch_prompt": outcome.feedback,
            "log": log,
        }
        if fails[name] >= ctx.max_gate_cycles:
            update["park_reason"] = (
                f"{name} gate failed {fails[name]}× in a row — "
                f"cycle cap ({ctx.max_gate_cycles})"
            )
            update["park_target"] = "implement_batch"
            return Command(goto="park", update=update)
        return Command(goto="implement_batch", update=update)

    return node


def _gauntlet_node(ctx: LoopContext) -> LoopNode:
    """G1–G4 adversarial gauntlet (module: gauntlet). Skipped when not installed.

    G1 (gauntlet.sh) and G4 (init.sh green + report on disk) are mechanical;
    G2 (adversarial) + G3 (maintainability) are one agent pass with a structured
    verdict. Any reject loops back to the implementer — the gauntlet re-runs
    from G1 (protocol §2).
    """

    def node(state: LoopState) -> Command:
        script = ctx.worktree / "harness" / "tools" / "gauntlet.sh"
        if not script.is_file():
            return Command(
                goto="completion_gate",
                update={
                    "log": [*state.get("log", []), "gauntlet: not installed — skipped"]
                },
            )
        log = [*state.get("log", [])]
        fails = dict(state.get("gate_fails", {}))

        # G1 — mechanical correctness (hard gate, log archived on failure)
        g1 = _run_gate(
            ctx, "gauntlet-g1", f"bash harness/tools/gauntlet.sh --feature {ctx.feature.name}"
        )
        log.append(g1.log)

        # G2+G3 — one adversarial pass by the reviewer model
        verdict = ctx.run_gauntlet(
            f"Run the G2 (adversarial) and G3 (maintainability) passes for feature "
            f"'{ctx.feature.name}' — {ctx.feature.title}. G1 result: "
            f"{'PASS' if g1.ok else 'FAIL'}."
        )
        cost = round(state.get("cost_usd", 0.0) + ctx.last_cost, 6)

        # G4 — evidence: report on disk with the four PASS lines
        report = ctx.worktree / "harness" / "progress" / f"gauntlet_{ctx.feature.name}.md"
        g4_ok = g1.ok and verdict.verdict == "APPROVED"
        lines_report = [
            f"# Gauntlet — {ctx.feature.name}",
            "",
            f"- G1 correctness: {'PASS' if g1.ok else 'FAIL'} (harness/tools/gauntlet.sh)",
            (
                f"- G2 adversarial: "
                f"{'PASS' if verdict.verdict == 'APPROVED' else 'REJECT'}"
                f" ({len(verdict.g2_findings)} findings)"
            ),
            (
                f"- G3 maintainability: "
                f"{'PASS' if verdict.verdict == 'APPROVED' else 'REJECT'}"
                f" — {verdict.g3_notes or 'checked'}"
            ),
            f"- G4 evidence: {'PASS' if g4_ok else 'FAIL'} (this report, gate logs)",
            "",
            "## Summary",
            "",
            verdict.summary or "(none)",
        ]
        for finding in verdict.g2_findings:
            lines_report += ["", f"- [G2] {finding}"]
        report.parent.mkdir(parents=True, exist_ok=True)
        report.write_text("\n".join(lines_report) + "\n", encoding="utf-8")
        log.append(f"gauntlet: {verdict.verdict} → {report.relative_to(ctx.worktree)}")

        if g1.ok and verdict.verdict == "APPROVED":
            fails.pop("gauntlet", None)
            return Command(
                goto="completion_gate",
                update={
                    "gate_fails": fails,
                    "gauntlet_rounds": 0,
                    "cost_usd": cost,
                    "log": log,
                },
            )

        rounds = state.get("gauntlet_rounds", 0) + 1
        update: LoopState = {
            "gauntlet_rounds": rounds,
            "cost_usd": cost,
            "log": log,
        }
        findings = (
            "\n".join(f"- {f}" for f in verdict.g2_findings) or verdict.summary or "(none)"
        )
        update["dispatch_prompt"] = (
            f"Gauntlet {'G1' if not g1.ok else 'G2/G3'} rejected (round {rounds}):\n{findings}\n"
            "Fix every finding; the gauntlet re-runs from G1 afterwards."
        )
        if rounds >= ctx.max_review_rounds:
            update["park_reason"] = (
                f"gauntlet rejected {rounds}× in a row — human gate"
            )
            update["park_target"] = "implement_batch"
            update["gate_fails"] = fails
            return Command(goto="park", update=update)
        return Command(goto="implement_batch", update=update)

    return node


def _advance_node(ctx: LoopContext) -> LoopNode:
    def node(state: LoopState) -> Command:
        remaining = next_batch(_read_tasks(ctx), ctx.max_tasks)
        batches_done = state.get("batches_done", 0) + 1
        log = [*state.get("log", []), f"batch complete ({batches_done} done)"]
        # progress resets every cycle counter: caps count CONSECUTIVE failures
        reset: LoopState = {"batches_done": batches_done, "batch_attempts": 0, "gate_fails": {}}
        if not remaining:
            log.append("all batches complete → reviewer")
            return Command(goto="reviewer", update={**reset, "log": log})
        batch = [t.task_id for t in remaining]
        if batches_done >= state.get("run_limit", ctx.max_batches):
            log.append(
                f"run limit reached ({batches_done}/{state.get('run_limit', ctx.max_batches)}) "
                "— re-run to continue"
            )
            return Command(
                goto=END,
                update={**reset, "batch": batch, "log": log},
            )
        log.append(f"next batch: {', '.join(batch)}")
        return Command(
            goto="implement_batch",
            update={**reset, "batch": batch, "log": log},
        )

    return node


def _reviewer_node(ctx: LoopContext) -> LoopNode:
    def node(state: LoopState) -> Command:
        verdict = ctx.run_review(_review_prompt(ctx))
        path = _write_review(ctx, verdict)
        log = [
            *state.get("log", []),
            f"review: {verdict.verdict} → {path.relative_to(ctx.worktree)}",
        ]
        update: LoopState = {
            "verdict": verdict.verdict,
            "log": log,
            "cost_usd": round(state.get("cost_usd", 0.0) + ctx.last_cost, 6),
        }
        if verdict.verdict == "APPROVED":
            update["review_rounds"] = 0
            return Command(goto="traceability_gate", update=update)
        rounds = state.get("review_rounds", 0) + 1
        update["review_rounds"] = rounds
        changes = (
            "\n".join(f"{i}. {c}" for i, c in enumerate(verdict.required_changes, 1))
            or verdict.summary
            or "(no details given)"
        )
        update["dispatch_prompt"] = (
            f"Reviewer CHANGES_REQUESTED (round {rounds}):\n{changes}"
        )
        if rounds >= ctx.max_review_rounds:
            update["park_reason"] = (
                f"reviewer requested changes {rounds}× in a row — human gate"
            )
            update["park_target"] = "implement_batch"
            return Command(goto="park", update=update)
        return Command(goto="implement_batch", update=update)

    return node


def _decision(answer: Any) -> tuple[str, str]:
    if isinstance(answer, dict):
        return str(answer.get("decision", "")).lower(), str(answer.get("comments", ""))
    return str(answer).lower(), ""


def _completion_node(ctx: LoopContext) -> LoopNode:
    def node(state: LoopState) -> Command:
        answer = interrupt(
            {
                "gate": "completion",
                "prompt": (
                    f"{ctx.feature.name} — {ctx.feature.title}: all tasks marked, "
                    "hard gates green, reviewer APPROVED. Approve for completion "
                    "(merge to main + APPROVAL + status done) or reject back to "
                    "the implementer."
                ),
                "summary": {
                    "feature": ctx.feature.name,
                    "verdict": state.get("verdict", "?"),
                    "batches": state.get("batches_done", 0),
                    "dispatches": state.get("dispatches", 0),
                    "stalls": state.get("stalls_total", 0),
                    "restarts": state.get("restarts", 0),
                },
            }
        )
        decision, comments = _decision(answer)
        log = [*state.get("log", []), f"completion gate: {decision}"]
        if decision == "approve":
            return Command(
                goto="finalize",
                update={"approval_comments": comments, "log": log},
            )
        return Command(
            goto="implement_batch",
            update={
                "dispatch_prompt": (
                    f"Human rejected at the completion gate: "
                    f"{comments or '(no comments)'}"
                ),
                "log": log,
            },
        )

    return node


def _park_node(ctx: LoopContext) -> LoopNode:
    def node(state: LoopState) -> Command:
        reason = state.get("park_reason") or "parked at a human gate"
        target = state.get("park_target", "implement_batch")
        # persist the block BEFORE parking: a crashed process must leave it blocked
        _commit_main_status(ctx.project.root, ctx.feature.name, "blocked")
        answer = interrupt(
            {
                "gate": "parked",
                "reason": reason,
                "prompt": (
                    f"Feature {ctx.feature.name} is blocked: {reason}. "
                    "Approve to resume, reject to leave it blocked."
                ),
            }
        )
        decision, comments = _decision(answer)
        log = [*state.get("log", []), f"human gate: {decision} ({reason})"]
        if decision == "approve":
            # a spec-phase park resumes as pending (the spec is re-authored and
            # re-gated); implementation parks resume as in_progress
            resume_status = "pending" if target == "spec_phase" else "in_progress"
            _commit_main_status(ctx.project.root, ctx.feature.name, resume_status)
            update: LoopState = {
                "park_reason": "",
                "park_target": "",
                "stalls": 0,
                "batch_attempts": 0,
                "gate_fails": {},
                "review_rounds": 0,
                "spec_rounds": 0,  # human intervention = progress; caps count streaks
                "log": log,
            }
            if comments and target != "spec_phase":
                update["dispatch_prompt"] = f"Human resume note: {comments}"
            return Command(goto=target or "implement_batch", update=update)
        log.append("aborted by human — feature left blocked")
        return Command(goto=END, update={"log": log})

    return node


def _finalize_node(ctx: LoopContext) -> LoopNode:
    def node(state: LoopState) -> Command:
        try:
            lines = _finalize(ctx, state)
        except FinalizeError as exc:
            return Command(
                goto="park",
                update={
                    "park_reason": str(exc),
                    "park_target": "finalize",
                    "log": [*state.get("log", []), f"finalize FAILED: {exc}"],
                },
            )
        return Command(
            goto=END,
            update={"finalized": True, "log": [*state.get("log", []), *lines]},
        )

    return node


# ---------- graph ----------


def build_loop_graph(
    ctx: LoopContext, checkpointer: SqliteSaver
) -> CompiledStateGraph[LoopState]:
    """The feature loop as a state machine — the graph IS the leader.

    Routing is expressed ONLY through `Command(goto=...)` returned by each node
    (no internal static edges): in LangGraph 1.x a `Command(goto=...)` *adds* a
    branch rather than replacing an edge, so a static edge would fan out in
    parallel and every node would race to write the same state keys. The happy
    path, for readers, is:

        spec_phase -> spec_gate ⏈ -> implement_batch -> verify -> init_gate
          -> advance_batch -> reviewer -> traceability_gate -> audit_gate
          -> gauntlet -> completion_gate ⏈ -> finalize

    with `verify`/gates looping back to `implement_batch` on failure and any
    unrecoverable condition routing to `park` (a human gate via `interrupt()`).
    For an `in_progress` feature, `spec_phase` skips straight to
    `implement_batch` (the spec was approved before the run).
    """
    graph: StateGraph[LoopState] = StateGraph(LoopState)
    graph.add_node("implement_batch", _implement_node(ctx))
    graph.add_node("verify", _verify_node(ctx))
    graph.add_node(
        "init_gate", _gate_node(ctx, "init", run_init_gate, on_pass="advance_batch")
    )
    graph.add_node("advance_batch", _advance_node(ctx))
    graph.add_node("reviewer", _reviewer_node(ctx))
    graph.add_node(
        "traceability_gate",
        _gate_node(ctx, "traceability", run_trace_gate, on_pass="audit_gate"),
    )
    graph.add_node(
        "audit_gate",
        _gate_node(ctx, "audit", run_audit_gate, on_pass="gauntlet"),
    )
    graph.add_node("gauntlet", _gauntlet_node(ctx))
    graph.add_node("completion_gate", _completion_node(ctx))
    graph.add_node("park", _park_node(ctx))
    graph.add_node("finalize", _finalize_node(ctx))
    graph.add_node("spec_phase", _spec_node(ctx))
    graph.add_node("spec_gate", _spec_gate_node(ctx))

    # The single static edge: entry. Every other hop is a Command(goto=...).
    # `spec_phase` routes straight to `implement_batch` for in_progress features
    # (phase-2 behavior) and authors the spec first for pending ones (phase 3).
    graph.add_edge(START, "spec_phase")
    return graph.compile(checkpointer=checkpointer)


# ---------- session + seeding ----------


def checkpoint_path(root: Path) -> Path:
    return root / ".buildwright" / "checkpoints.sqlite"


@contextmanager
def loop_session(root: Path) -> Iterator[SqliteSaver]:
    """One sqlite checkpointer per project — human gates survive restarts."""
    path = checkpoint_path(root)
    path.parent.mkdir(parents=True, exist_ok=True)
    with SqliteSaver.from_conn_string(str(path)) as saver:
        yield saver


def pending_interrupt(snap: StateSnapshot) -> bool:
    return any(task.interrupts for task in snap.tasks)


def initial_seed(ctx: LoopContext) -> LoopState:
    """Fresh-run state. `in_progress` features start at their first pending
    batch; `pending` features start at the spec phase (the first batch is
    picked when the spec gate approves, from the tasks.md just authored)."""
    if ctx.feature.status == "in_progress":
        batch = next_batch(_read_tasks(ctx), ctx.max_tasks)
        if not batch:
            raise RuntimeError(f"feature '{ctx.feature.name}' has no pending tasks")
        batch_ids = [t.task_id for t in batch]
    else:
        batch_ids = []
    return {
        "feature": ctx.feature.name,
        "batch": batch_ids,
        "log": [],
        "dispatches": 0,
        "batch_attempts": 0,
        "stalls": 0,
        "stalls_total": 0,
        "restarts": 0,
        "gate_fails": {},
        "review_rounds": 0,
        "spec_rounds": 0,
        "gauntlet_rounds": 0,
        "batches_done": 0,
        "cost_usd": 0.0,
    }


def prepare_seed(ctx: LoopContext, previous: Mapping[str, Any] | None) -> LoopState:
    """Resume the previous state with a fresh batch budget, or start over."""
    if previous:
        if previous.get("finalized"):
            raise RuntimeError(f"feature '{ctx.feature.name}' is already finalized")
        seed = cast(LoopState, dict(previous))
    else:
        seed = initial_seed(ctx)
    seed["run_limit"] = int(seed.get("batches_done", 0)) + ctx.max_batches
    return seed
