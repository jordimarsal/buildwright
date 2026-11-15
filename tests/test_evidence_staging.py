"""Repro tests for the two real causes of the test_loop failures (spec
2026-11-15-loop-episode-gate-fix §1).

Both are environment-independent by construction:

* A  — staging evidence that git already ignores must be a no-op, not a
       `FinalizeError`. The ignore rule can come from anywhere (repo
       `.gitignore`, the user's global gitignore, `*.log`), so the test
       creates its own ignore rule instead of depending on the machine.
* B2 — a second episode for the same feature must never inherit the closed
       state of the first one, even when both land in the same minute (the
       original `exec_id` was minute-precise).
"""

from __future__ import annotations

import subprocess
from pathlib import Path
from types import SimpleNamespace

from buildwright import debrief_log
from buildwright.loop import _commit_debrief_evidence


def _git(root: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", "-C", str(root), *args], capture_output=True, text=True, check=True
    )


def _make_repo(root: Path) -> Path:
    root.mkdir(parents=True, exist_ok=True)
    _git(root, "init", "-b", "main")
    _git(root, "config", "user.email", "t@t")
    _git(root, "config", "user.name", "t")
    (root / "README.md").write_text("x\n", encoding="utf-8")
    # Neutralise the machine's global gitignore (many have `*.log`): repo-local
    # core.excludesFile wins over the global one, so the ONLY ignore source here
    # is the repo's own .gitignore — the test then means the same thing on every
    # machine, which is the whole point of T1 being environment-independent.
    empty_excludes = root / ".no-global-excludes"
    empty_excludes.write_text("", encoding="utf-8")
    _git(root, "config", "core.excludesfile", str(empty_excludes))
    (root / ".gitignore").write_text("*.log\n", encoding="utf-8")
    _git(root, "add", "-A")
    _git(root, "commit", "-m", "init")
    return root


def _ctx(root: Path, evidence: Path) -> SimpleNamespace:
    """Duck-typed LoopContext: `_commit_debrief_evidence` only reads
    `.debrief.path` and `.project.root`."""
    sink = SimpleNamespace(path=evidence)
    return SimpleNamespace(debrief=sink, project=SimpleNamespace(root=root))


# --------------------------------------------------------------------------- #
# A — ignored evidence path is a no-op
# --------------------------------------------------------------------------- #


def test_ignored_evidence_path_does_not_raise(tmp_path: Path) -> None:
    root = _make_repo(tmp_path / "proj")
    evidence = root / "artifacts" / "demo" / "exec1" / "f2_hub.log"
    evidence.parent.mkdir(parents=True, exist_ok=True)
    evidence.write_text("task: f2_hub\n", encoding="utf-8")

    # the ignore rule must actually be in force, or the test proves nothing
    ignored = _git(root, "check-ignore", "artifacts/demo/exec1/f2_hub.log")
    assert ignored.returncode == 0

    # before the fix this raised FinalizeError ("stage debrief evidence failed")
    _commit_debrief_evidence(_ctx(root, evidence), "chore(f2_hub): evidence")

    # main stays clean: an ignored file never shows up in git status
    assert _git(root, "status", "--porcelain").stdout.strip() == ""


def test_ignored_evidence_leaves_no_commit(tmp_path: Path) -> None:
    root = _make_repo(tmp_path / "proj")
    before = _git(root, "rev-parse", "HEAD").stdout.strip()
    evidence = root / "artifacts" / "demo" / "exec1" / "f2_hub.log"
    evidence.parent.mkdir(parents=True, exist_ok=True)
    evidence.write_text("task: f2_hub\n", encoding="utf-8")

    _commit_debrief_evidence(_ctx(root, evidence), "chore(f2_hub): evidence")

    assert _git(root, "rev-parse", "HEAD").stdout.strip() == before


def test_non_ignored_evidence_is_still_committed(tmp_path: Path) -> None:
    """The self-contained mode keeps working for repos that do NOT ignore it —
    the fix must narrow the staging, not delete it."""
    root = _make_repo(tmp_path / "proj")
    (root / ".gitignore").write_text("nothing-at-all\n", encoding="utf-8")
    _git(root, "add", "-A")
    _git(root, "commit", "-m", "drop log ignore")
    before = _git(root, "rev-parse", "HEAD").stdout.strip()

    evidence = root / "evidence" / "f2_hub.log"
    evidence.parent.mkdir(parents=True, exist_ok=True)
    evidence.write_text("task: f2_hub\n", encoding="utf-8")

    _commit_debrief_evidence(_ctx(root, evidence), "chore(f2_hub): evidence")

    after = _git(root, "rev-parse", "HEAD").stdout.strip()
    assert after != before, "a visible evidence file must still be committed"


# --------------------------------------------------------------------------- #
# B2 — exec_id uniqueness: a second episode never inherits a closed one
# --------------------------------------------------------------------------- #


def test_second_episode_same_minute_is_not_inherited(tmp_path: Path) -> None:
    base = tmp_path / "debrief"
    roles = {"planner": "m1", "worker": "m2", "reviewer": "m3"}

    first = debrief_log._debrief_sink_at(base, "demo", "f2_hub", roles)
    first.step("worker", "m2", "pass", "first episode runs", "evidence")
    assert first.finish() == "pass"

    # same feature, same minute: must be a NEW episode, not a resume of the
    # closed one (the old minute-precise exec_id collided here)
    second = debrief_log._debrief_sink_at(base, "demo", "f2_hub", roles)
    assert second.path != first.path, "episodes must not share a path"
    assert second._finished is False, "a fresh episode must start open"
    second.step("worker", "m2", "pass", "second episode runs", "evidence")


def test_resume_still_works_for_a_truly_open_episode(tmp_path: Path) -> None:
    """The uniqueness must not break the real resume path: an episode parked at
    a human gate has NO `result:` and must be picked up again."""
    base = tmp_path / "debrief"
    roles = {"planner": "m1", "worker": "m2", "reviewer": "m3"}

    open_ep = debrief_log._debrief_sink_at(base, "demo", "f2_hub", roles)
    open_ep.step("worker", "m2", "pass", "parked mid-run", "evidence")
    # no finish(): the episode is still open

    resumed = debrief_log._debrief_sink_at(base, "demo", "f2_hub", roles)
    assert resumed.path == open_ep.path, "an open episode must be resumed"
    assert resumed._finished is False
    resumed.step("worker", "m2", "pass", "picked up after the gate", "evidence")
