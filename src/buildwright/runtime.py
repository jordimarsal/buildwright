"""Phase 1 runtime: ONE supervised implementer batch, in a git worktree.

Human flow: `buildwright harness run` shows the plan, asks for confirmation,
runs the implementer (with tools) inside an isolated worktree, then verifies
the result on disk — checkbox states and git status. It never merges and
never flips feature status: the human reviews and decides.
"""

from __future__ import annotations

import re
import subprocess
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

import yaml
from pydantic_ai import Agent, RunContext
from pydantic_ai.models import Model

MAX_TOOL_OUTPUT = 4000
MAX_BATCH_TASKS = 4  # harness batching discipline: never 5+

# Identity used for the commits the runtime itself makes (status flips, merges).
GIT_IDENTITY: tuple[str, ...] = (
    "-c",
    "user.name=buildwright",
    "-c",
    "user.email=buildwright@localhost",
)

DEFAULT_ALLOWED_COMMANDS: tuple[str, ...] = (
    "uv run",
    "uv sync",
    "uv pip",
    "pytest",
    "python",
    "pip",
    "ruff",
    "mypy",
    "black",
    "cargo",
    "mvn",
    "gradle",
    "./gradlew",
    "npm",
    "npx",
    "node",
    "make",
    "bash harness/init.sh",
    "sh harness/init.sh",
    "harness/init.sh",
    "bash harness/tools/audit-security.sh",
    "sh harness/tools/audit-security.sh",
    "bash harness/tools/gauntlet.sh",
    "sh harness/tools/gauntlet.sh",
    "bash harness/tools/bench.sh",
    "sh harness/tools/bench.sh",
    "python3",
    "git status",
    "git diff",
    "git log",
    "ls",
    "cat",
    "grep",
    "wc",
)

IMPL_INSTRUCTIONS = """You are the implementer agent of a harness-initialized project, running \
unattended inside a git worktree.

Batch discipline: implement ONLY the tasks listed in the dispatch prompt, in \
order. Never touch tasks of another batch or another feature.

Work mode: write files before any prose. After EACH task, run the project \
quality gates via run_command (gates are defined in docs/conventions.md and \
docs/verification.md). Only when the gates are green, mark the task done by \
editing harness/specs/<feature>/tasks.md, changing `- [ ] T<n>` to `- [x] T<n>`.

Use only relative paths inside the worktree. If a task cannot be completed \
without deviating from the spec, do not improvise: stop and report \
`blocked <task>: <reason>`.

Anti-telephone-cord: record what you did (files created, gates run) in \
`harness/progress/impl_<feature>.md` — one short section per dispatch. The \
reviewer reads that file, not your chat.

Final response: exactly one line — `done <T-ids> -> <main files>` or \
`blocked <task>: <reason>`."""


@dataclass(frozen=True)
class Sandbox:
    """Filesystem + command boundary for one agent run, pinned to a worktree."""

    root: Path
    allowed_commands: tuple[str, ...] = DEFAULT_ALLOWED_COMMANDS
    timeout_s: int = 180
    log_dir: Path | None = None

    @classmethod
    def from_project(cls, root: Path, log_dir: Path | None = None) -> Sandbox:
        """Defaults + optional overrides from harness/runtime.yaml (D1: hard source)."""
        extra: list[str] = []
        timeout = 180
        cfg = root / "harness" / "runtime.yaml"
        if cfg.is_file():
            data = yaml.safe_load(cfg.read_text(encoding="utf-8")) or {}
            extra = [str(c) for c in data.get("extra_allowed_commands", [])]
            timeout = int(data.get("command_timeout_s", 180))
        return cls(
            root=root,
            allowed_commands=DEFAULT_ALLOWED_COMMANDS + tuple(extra),
            timeout_s=timeout,
            log_dir=log_dir,
        )


def resolve_in_sandbox(sandbox: Sandbox, path: str) -> Path:
    """Resolve a relative path against the worktree; refuse any escape."""
    p = (sandbox.root / path).resolve()
    if p != sandbox.root and not p.is_relative_to(sandbox.root):
        raise PermissionError(f"path escapes the worktree: {path}")
    return p


def command_allowed(command: str, allowed: tuple[str, ...]) -> bool:
    cmd = command.strip()
    return any(cmd == a or cmd.startswith(a + " ") for a in allowed)


# ---------- tools (module-level for testability; the agent wraps them) ----------


def tool_read_file(sandbox: Sandbox, path: str) -> str:
    try:
        p = resolve_in_sandbox(sandbox, path)
    except PermissionError as exc:
        return f"ERROR: {exc}"
    if not p.is_file():
        return f"ERROR: not a file: {path}"
    text = p.read_text(encoding="utf-8", errors="replace")
    if len(text) > 20_000:
        return text[:20_000] + f"\n… (truncated at 20000 chars: {path})"
    return text


def tool_write_file(sandbox: Sandbox, path: str, content: str) -> str:
    try:
        p = resolve_in_sandbox(sandbox, path)
    except PermissionError as exc:
        return f"ERROR: {exc}"
    if len(content) > 500_000:
        return "ERROR: content over 500000 chars — split the file"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(content, encoding="utf-8")
    return f"wrote {path} ({len(content)} chars)"


def tool_edit_file(sandbox: Sandbox, path: str, old: str, new: str) -> str:
    try:
        p = resolve_in_sandbox(sandbox, path)
    except PermissionError as exc:
        return f"ERROR: {exc}"
    if not p.is_file():
        return f"ERROR: not a file: {path}"
    text = p.read_text(encoding="utf-8")
    if text.count(old) != 1:
        return f"ERROR: 'old' must occur exactly once in {path} (found {text.count(old)})"
    p.write_text(text.replace(old, new), encoding="utf-8")
    return f"edited {path}"


def tool_list_dir(sandbox: Sandbox, path: str = ".") -> str:
    try:
        p = resolve_in_sandbox(sandbox, path)
    except PermissionError as exc:
        return f"ERROR: {exc}"
    if not p.is_dir():
        return f"ERROR: not a directory: {path}"
    entries = sorted(
        (f"{e.name}/" if e.is_dir() else e.name) for e in p.iterdir()
    )
    return "\n".join(entries) or "(empty)"


def tool_run_command(sandbox: Sandbox, command: str) -> str:
    if not command_allowed(command, sandbox.allowed_commands):
        return f"ERROR: command not allowed: {command}"
    try:
        proc = subprocess.run(
            command,
            shell=True,
            check=False,
            cwd=sandbox.root,
            capture_output=True,
            text=True,
            timeout=sandbox.timeout_s,
        )
    except subprocess.TimeoutExpired:
        return f"ERROR: timeout after {sandbox.timeout_s}s: {command}"
    out = proc.stdout
    if proc.stderr:
        out += ("\n[stderr]\n" if out else "[stderr]\n") + proc.stderr
    out = out.strip() or f"(exit {proc.returncode}, no output)"
    if len(out) > MAX_TOOL_OUTPUT and sandbox.log_dir is not None:
        sandbox.log_dir.mkdir(parents=True, exist_ok=True)
        name = f"cmd_{abs(hash(command)) % 10**8}.log"
        (sandbox.log_dir / name).write_text(out, encoding="utf-8")
        out = out[-MAX_TOOL_OUTPUT // 2 :] + f"\n… (full output archived: {name})"
    return f"exit={proc.returncode}\n{out}"


def build_implementer(model: Model, sandbox: Sandbox) -> Agent[Sandbox, str]:
    """Implementer agent with harness work-mode tools bound to the sandbox."""

    agent: Agent[Sandbox, str] = Agent(
        model, deps_type=Sandbox, instructions=IMPL_INSTRUCTIONS
    )

    @agent.tool
    def read_file(ctx: RunContext[Sandbox], path: str) -> str:
        """Read a file from the worktree."""
        return tool_read_file(ctx.deps, path)

    @agent.tool
    def write_file(ctx: RunContext[Sandbox], path: str, content: str) -> str:
        """Create or overwrite a file in the worktree."""
        return tool_write_file(ctx.deps, path, content)

    @agent.tool
    def edit_file(ctx: RunContext[Sandbox], path: str, old: str, new: str) -> str:
        """Replace one exact occurrence of `old` with `new` in a worktree file."""
        return tool_edit_file(ctx.deps, path, old, new)

    @agent.tool
    def list_dir(ctx: RunContext[Sandbox], path: str = ".") -> str:
        """List a directory of the worktree."""
        return tool_list_dir(ctx.deps, path)

    @agent.tool
    def run_command(ctx: RunContext[Sandbox], command: str) -> str:
        """Run an allowlisted command in the worktree root (quality gates, tests)."""
        return tool_run_command(ctx.deps, command)

    return agent


# ---------- tasks.md parsing + batch selection ----------

TASK_RE = re.compile(r"^-\s\[( |x)\]\s(T\d+):?\s*(.*)$")


@dataclass(frozen=True)
class Task:
    task_id: str
    text: str
    done: bool


def parse_tasks(tasks_md: str) -> list[Task]:
    tasks: list[Task] = []
    for line in tasks_md.splitlines():
        m = TASK_RE.match(line.strip())
        if m:
            tasks.append(Task(task_id=m.group(2), text=m.group(3), done=m.group(1) == "x"))
    return tasks


def next_batch(tasks: list[Task], max_tasks: int = 3) -> list[Task]:
    """Next pending tasks, capped by the harness batch discipline (2-4, never 5+)."""
    size = max(1, min(max_tasks, MAX_BATCH_TASKS))
    return [t for t in tasks if not t.done][:size]


# ---------- worktree isolation ----------


def _git(cwd: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", "-C", str(cwd), *args],
        check=False,
        capture_output=True,
        text=True,
    )


def _current_branch(cwd: Path) -> str | None:
    proc = _git(cwd, "symbolic-ref", "--short", "HEAD")
    return proc.stdout.strip() or None


def sync_worktree(root: Path, worktree: Path) -> None:
    """Bring a reused worktree up to date with main before the next dispatch.

    A worktree stays pinned to the commit it branched from, so anything committed
    to main afterwards — specs, docs, source — would be invisible to the
    implementer, which then reports `blocked` against a tree missing its own
    spec. Fast-forward when the branch has no commits of its own, merge when it
    does; a merge git refuses (local changes in the way) is raised instead of
    silently dispatching against a stale tree.
    """
    branch = _current_branch(worktree)
    main = _current_branch(root)
    if branch is None or main is None or main == branch:
        return
    if _git(worktree, "merge", "--ff-only", main).returncode == 0:
        return
    merge = _git(worktree, *GIT_IDENTITY, "merge", main)
    if merge.returncode == 0:
        return
    raise RuntimeError(
        f"worktree {worktree} cannot be synced with {main}: "
        f"{(merge.stderr or merge.stdout).strip()}\n"
        "Finish the merge in the worktree (or `git merge --abort`) and re-run."
    )


def create_worktree(root: Path, feature_name: str) -> Path:
    """Create (or reuse) a git worktree for the feature, kept out of git status."""
    wt = root / ".buildwright" / "worktrees" / feature_name
    if (wt / ".git").exists():
        sync_worktree(root, wt)
        return wt
    branch = f"buildwright/{feature_name}"
    exists = subprocess.run(
        ["git", "-C", str(root), "rev-parse", "--verify", f"refs/heads/{branch}"],
        check=False,
        capture_output=True,
        text=True,
    ).returncode
    argv = ["git", "-C", str(root), "worktree", "add"]
    argv += ["-b", branch, str(wt)] if exists != 0 else [str(wt), branch]
    subprocess.run(argv, check=True, capture_output=True, text=True)
    # local ignore (never touches the tracked .gitignore)
    exclude = root / ".git" / "info" / "exclude"
    content = exclude.read_text(encoding="utf-8") if exclude.exists() else ""
    if ".buildwright/" not in content:
        sep = "" if not content or content.endswith("\n") else "\n"
        exclude.parent.mkdir(parents=True, exist_ok=True)
        exclude.write_text(content + sep + ".buildwright/\n", encoding="utf-8")
    return wt


# ---------- on-disk verification ----------


@dataclass(frozen=True)
class BatchReport:
    feature: str
    batch: tuple[str, ...]
    marked: tuple[str, ...]
    changed_files: tuple[str, ...]


def verify_on_disk(
    worktree: Path, feature_name: str, batch: Sequence[Task]
) -> BatchReport:
    """A chat claim is not evidence: re-read tasks.md and git status ourselves."""
    tasks_path = worktree / "harness" / "specs" / feature_name / "tasks.md"
    done: set[str] = set()
    if tasks_path.is_file():
        done = {t.task_id for t in parse_tasks(tasks_path.read_text(encoding="utf-8")) if t.done}
    marked = tuple(t.task_id for t in batch if t.task_id in done)
    proc = subprocess.run(
        ["git", "-C", str(worktree), "status", "--porcelain"],
        check=False,
        capture_output=True,
        text=True,
    )
    changed = tuple(
        line[3:].strip() for line in proc.stdout.splitlines() if line.strip()
    )
    return BatchReport(
        feature=feature_name,
        batch=tuple(t.task_id for t in batch),
        marked=marked,
        changed_files=changed,
    )
