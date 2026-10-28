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
import subprocess
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

from .config import Settings
from .harness import Feature, FeatureList, HarnessProject
from .registry import ModelRegistry
from .runtime import (
    Sandbox,
    Task,
    build_implementer,
    create_worktree,
    next_batch,
    parse_tasks,
    tool_list_dir,
    tool_read_file,
    tool_run_command,
)

DEFAULT_MAX_TASKS = 3
DEFAULT_MAX_BATCHES = 10  # batch budget per invocation; re-run continues
MAX_BATCH_ATTEMPTS = 4  # dispatches on the same batch without progress → park
MAX_GATE_CYCLES = 3  # consecutive failures of one gate → park
MAX_REVIEW_ROUNDS = 2  # CHANGES_REQUESTED in a row → park blocked
MAX_STALLS = 2  # consecutive stalls → fresh agent instance (restart)

LOOP_CAP_KEYS = ("max_batch_attempts", "max_gate_cycles", "max_review_rounds")


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
    batches_done: int
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
    dispatch: Callable[[str], str] | None = None
    review: Callable[[str], ReviewVerdict] | None = None

    def run_dispatch(self, prompt: str) -> str:
        """One dispatch = one fresh agent instance (no shared session)."""
        fn = self.dispatch if self.dispatch is not None else _default_dispatch(self)
        return fn(prompt)

    def run_review(self, prompt: str) -> ReviewVerdict:
        fn = self.review if self.review is not None else _default_review(self)
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
        model = ctx.registry.build(ctx.settings.role_model("worker"))
        agent = build_implementer(model, ctx.sandbox)
        return agent.run_sync(prompt, deps=ctx.sandbox).output

    return dispatch


def _default_review(ctx: LoopContext) -> Callable[[str], ReviewVerdict]:
    def review(prompt: str) -> ReviewVerdict:
        model = ctx.registry.build(ctx.settings.role_model("reviewer"))
        agent: Agent[Sandbox, ReviewVerdict] = Agent(
            model,
            output_type=ReviewVerdict,
            deps_type=Sandbox,
            instructions=REVIEW_INSTRUCTIONS,
        )

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

        return agent.run_sync(prompt, deps=ctx.sandbox).output

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
    """The protocol's retro line, machine-generated."""
    return (
        f"retro: {state.get('dispatches', 0)} dispatches · "
        f"{state.get('stalls_total', 0)} stalls · {state.get('restarts', 0)} restarts"
    )


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


_IDENTITY = ["-c", "user.name=buildwright", "-c", "user.email=buildwright@localhost"]


def _commit_main_status(root: Path, feature_name: str, status: str) -> None:
    """Flip status on main AND commit it, leaving main clean (harness hard rule).

    The runtime owns `feature_list.json`; a status flip is a protocol event, so
    it is committed immediately. This keeps `finalize`'s "main must be clean"
    gate honest — an uncommitted flip would otherwise block the merge forever.
    """
    set_feature_status(root, feature_name, status)
    if not _git(root, "status", "--porcelain").stdout.strip():
        return  # nothing to commit (e.g. status already matched)
    _run_or_raise(root, ["add", "-A"], "stage the status flip")
    _run_or_raise(
        root,
        [*_IDENTITY, "commit", "-m", f"chore({feature_name}): status {status}"],
        "commit the status flip",
    )


def _finalize(ctx: LoopContext, state: LoopState) -> list[str]:
    """Merge, then APPROVAL → status done → history.md + retro, then commit clean."""
    root, worktree, feat = ctx.project.root, ctx.worktree, ctx.feature
    lines: list[str] = []
    identity = ["-c", "user.name=buildwright", "-c", "user.email=buildwright@localhost"]

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
    if not (ctx.worktree / "harness" / "tools" / "check-traceability.py").is_file():
        return GateOutcome(True, "", "check-traceability.py not present — gate skipped")
    return _run_gate(
        ctx, "traceability", "python3 harness/tools/check-traceability.py --all"
    )


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
        fingerprint = worktree_fingerprint(ctx.worktree)  # captured BEFORE the dispatch
        output = ctx.run_dispatch(prompt)
        dispatches = state.get("dispatches", 0) + 1
        return Command(
            goto="verify",
            update={
                "dispatches": dispatches,
                "batch_attempts": attempts + 1,
                "fingerprint": fingerprint,
                "dispatch_prompt": "",
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
        update: LoopState = {"verdict": verdict.verdict, "log": log}
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
            _commit_main_status(ctx.project.root, ctx.feature.name, "in_progress")
            update: LoopState = {
                "park_reason": "",
                "park_target": "",
                "stalls": 0,
                "batch_attempts": 0,
                "gate_fails": {},
                "review_rounds": 0,
                "log": log,
            }
            if comments:
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

        implement_batch -> verify -> init_gate -> advance_batch -> reviewer
          -> traceability_gate -> audit_gate -> completion_gate -> finalize

    with `verify`/gates looping back to `implement_batch` on failure and any
    unrecoverable condition routing to `park` (a human gate via `interrupt()`).
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
        _gate_node(ctx, "audit", run_audit_gate, on_pass="completion_gate"),
    )
    graph.add_node("completion_gate", _completion_node(ctx))
    graph.add_node("park", _park_node(ctx))
    graph.add_node("finalize", _finalize_node(ctx))

    # The single static edge: entry. Every other hop is a Command(goto=...).
    graph.add_edge(START, "implement_batch")
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
    """Fresh-run state: the first pending batch of the worktree's tasks.md."""
    batch = next_batch(_read_tasks(ctx), ctx.max_tasks)
    if not batch:
        raise RuntimeError(f"feature '{ctx.feature.name}' has no pending tasks")
    return {
        "feature": ctx.feature.name,
        "batch": [t.task_id for t in batch],
        "log": [],
        "dispatches": 0,
        "batch_attempts": 0,
        "stalls": 0,
        "stalls_total": 0,
        "restarts": 0,
        "gate_fails": {},
        "review_rounds": 0,
        "batches_done": 0,
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
