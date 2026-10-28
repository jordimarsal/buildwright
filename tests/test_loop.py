"""Phase 2 tests: the full feature loop — batches, hard gates, human gates.

Everything runs offline: the implementer and the reviewer are injected fakes
(`make_context(..., dispatch=..., review=...)`), git is a local fixture repo.
"""

from __future__ import annotations

import json
import re
import subprocess
from collections.abc import Callable
from pathlib import Path

import pytest
from langgraph.types import Command, RunnableConfig
from pydantic import ValidationError

from buildwright.cli import cmd_harness_gate, cmd_harness_run
from buildwright.config import Settings
from buildwright.harness import HarnessProject
from buildwright.loop import (
    LoopContext,
    ReviewVerdict,
    build_loop_graph,
    initial_seed,
    loop_session,
    make_context,
    pending_interrupt,
    prepare_seed,
    retro_line,
    run_audit_gate,
    run_init_gate,
    run_trace_gate,
    set_feature_status,
    worktree_fingerprint,
)

APPROVED = ReviewVerdict(verdict="APPROVED", summary="looks good", traceability=["R1"])
CHANGES = ReviewVerdict(
    verdict="CHANGES_REQUESTED", summary="not yet", required_changes=["cover R2"]
)


def git(cwd: Path, *args: str) -> None:
    subprocess.run(
        ["git", "-C", str(cwd), "-c", "user.name=t", "-c", "user.email=t@t", *args],
        check=True,
        capture_output=True,
        text=True,
    )


def make_repo(
    tmp_path: Path,
    *,
    audit_level: str = "basic",
    tasks: tuple[str, ...] = ("T1", "one", "T2", "two", "T3", "three"),
    init_sh: str = "#!/bin/bash\necho 'tests green'\necho '[OK]'\n",
) -> Path:
    """A harness-initialized git project with one in_progress feature."""
    root = tmp_path / "proj"
    spec = root / "harness" / "specs" / "f2_hub"
    spec.mkdir(parents=True)
    (root / "harness" / "feature_list.json").write_text(
        json.dumps(
            {
                "project": {
                    "name": "demo",
                    "parallel": False,
                    "modules": ["api"],
                    "audit_level": audit_level,
                },
                "features": [
                    {
                        "id": 1,
                        "name": "f2_hub",
                        "title": "Hub endpoint",
                        "description": "one hub",
                        "acceptance": ["A1: it hubs"],
                        "status": "in_progress",
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    (spec / "requirements.md").write_text("R1: the hub exists\n", encoding="utf-8")
    (spec / "design.md").write_text("# Design\n", encoding="utf-8")
    rows = "\n".join(f"- [ ] {tasks[i]}: {tasks[i + 1]}" for i in range(0, len(tasks), 2))
    (spec / "tasks.md").write_text(f"# Tasks\n\n{rows}\n", encoding="utf-8")
    (root / "harness" / "init.sh").write_text(init_sh, encoding="utf-8")
    (root / "docs").mkdir()
    (root / "docs" / "architecture.md").write_text("# Arch\n", encoding="utf-8")
    git(root, "init", "-b", "main")
    git(root, "add", "-A")
    git(root, "commit", "-m", "init: harness scaffold")
    return root


class FakeDispatch:
    """Offline implementer: parses `Your batch: T<n>`, marks boxes, writes files."""

    def __init__(self, root: Path, feature: str = "f2_hub", *, writes: bool = True):
        self.worktree = root / ".buildwright" / "worktrees" / feature
        self.feature = feature
        self.writes = writes
        self.calls = 0
        self.prompts: list[str] = []

    def __call__(self, prompt: str) -> str:
        self.prompts.append(prompt)
        self.calls += 1
        m = re.search(r"Your batch: ([^\n]+)", prompt)
        batch = m.group(1).split(", ") if m else []
        if not self.writes:
            return "done (nothing written)"
        # append the impl log — every dispatch must change the worktree
        log = self.worktree / "harness" / "progress" / f"impl_{self.feature}.md"
        log.parent.mkdir(parents=True, exist_ok=True)
        with log.open("a", encoding="utf-8") as fh:
            fh.write(f"\n## dispatch {self.calls}\ndid: {', '.join(batch)}\n")
        for tid in batch:
            src = self.worktree / "src" / f"impl_{tid}.py"
            src.parent.mkdir(parents=True, exist_ok=True)
            src.write_text(f"# impl for {tid}\nVALUE = {tid!r}\n", encoding="utf-8")
        tasks = self.worktree / "harness" / "specs" / self.feature / "tasks.md"
        text = tasks.read_text(encoding="utf-8")
        for tid in batch:
            text = re.sub(rf"- \[ \] {re.escape(tid)}:", f"- [x] {tid}:", text)
        tasks.write_text(text, encoding="utf-8")
        return f"done {', '.join(batch)} -> src/impl_{batch[0]}.py" if batch else "done"


class ScriptedReview:
    """Reviewer with a scripted verdict list (last one repeats)."""

    def __init__(self, *verdicts: ReviewVerdict):
        self.verdicts = list(verdicts)
        self.prompts: list[str] = []

    def __call__(self, prompt: str) -> ReviewVerdict:
        self.prompts.append(prompt)
        return self.verdicts.pop(0) if len(self.verdicts) > 1 else self.verdicts[0]


def ctx_for(
    root: Path,
    dispatch: Callable[[str], str],
    review: Callable[[str], ReviewVerdict],
    *,
    max_tasks: int = 3,
    max_batches: int = 10,
) -> LoopContext:
    project = HarnessProject.load(root)
    return make_context(
        project,
        Settings(),
        "f2_hub",
        max_tasks=max_tasks,
        max_batches=max_batches,
        dispatch=dispatch,
        review=review,
    )


CFG: RunnableConfig = {"configurable": {"thread_id": "f2_hub"}}


def invoke(ctx: LoopContext, seed: object):
    with loop_session(ctx.project.root) as saver:
        graph = build_loop_graph(ctx, saver)
        return graph.invoke(seed, CFG)  # type: ignore[arg-type]


def resume(ctx: LoopContext, decision: str, comments: str = "") -> object:
    """Resume a parked gate in a *fresh* session — state survives restarts."""
    with loop_session(ctx.project.root) as saver:
        graph = build_loop_graph(ctx, saver)
        assert pending_interrupt(graph.get_state(CFG))  # type: ignore[arg-type]
        return graph.invoke(
            Command(resume={"decision": decision, "comments": comments}), CFG  # type: ignore[arg-type]
        )


def interrupt_payload(out: object) -> dict:
    assert isinstance(out, dict)
    parked = out.get("__interrupt__")
    assert parked, f"expected an interrupt, got keys {list(out)}"
    return parked[0].value  # type: ignore[attr-defined]


# ---------- full loop ----------


def test_full_loop_approve_end_to_end(tmp_path: Path):
    root = make_repo(tmp_path)
    dispatch = FakeDispatch(root)
    ctx = ctx_for(root, dispatch, ScriptedReview(APPROVED))

    out = invoke(ctx, prepare_seed(ctx, None))
    payload = interrupt_payload(out)
    assert payload["gate"] == "completion"
    assert payload["summary"]["verdict"] == "APPROVED"
    assert payload["summary"]["batches"] == 1

    out2 = resume(ctx, "approve", "ship it")
    assert isinstance(out2, dict) and out2["finalized"] is True

    # the worktree landed on main
    assert (root / "src" / "impl_T1.py").is_file()
    assert subprocess.run(
        ["git", "-C", str(root), "merge-base", "--is-ancestor", "buildwright/f2_hub", "main"],
        check=False,
        capture_output=True,
    ).returncode == 0
    # APPROVAL only exists because the completion gate approved
    approval = (root / "harness" / "specs" / "f2_hub" / "APPROVAL").read_text()
    assert "ship it" in approval and "completion gate" in approval
    # board + history
    fl = json.loads((root / "harness" / "feature_list.json").read_text())
    assert fl["features"][0]["status"] == "done"
    history = (root / "harness" / "progress" / "history.md").read_text()
    assert "### f2_hub —" in history and "retro: 1 dispatches · 0 stalls · 0 restarts" in history
    # main left clean (harness hard rule)
    status = subprocess.run(
        ["git", "-C", str(root), "status", "--porcelain"],
        check=False,
        capture_output=True,
        text=True,
    )
    assert status.stdout.strip() == ""


def test_completion_gate_reject_loops_back(tmp_path: Path):
    root = make_repo(tmp_path)
    dispatch = FakeDispatch(root)
    ctx = ctx_for(root, dispatch, ScriptedReview(APPROVED))

    out = invoke(ctx, prepare_seed(ctx, None))
    assert interrupt_payload(out)["gate"] == "completion"

    out2 = resume(ctx, "reject", "add tests for R1")
    assert interrupt_payload(out2)["gate"] == "completion"  # second pass reached it again
    # the reject feedback reached the implementer
    assert any("add tests for R1" in p for p in dispatch.prompts)

    out3 = resume(ctx, "approve", "now good")
    assert isinstance(out3, dict) and out3["finalized"] is True
    assert "now good" in (root / "harness" / "specs" / "f2_hub" / "APPROVAL").read_text()


def test_reviewer_changes_twice_parks_then_recovers(tmp_path: Path):
    root = make_repo(tmp_path)
    dispatch = FakeDispatch(root)
    review = ScriptedReview(CHANGES, CHANGES, APPROVED)
    ctx = ctx_for(root, dispatch, review)

    out = invoke(ctx, prepare_seed(ctx, None))
    payload = interrupt_payload(out)
    assert payload["gate"] == "parked"
    assert "reviewer requested changes 2×" in payload["reason"]
    # park persists `blocked` to disk BEFORE parking
    fl = json.loads((root / "harness" / "feature_list.json").read_text())
    assert fl["features"][0]["status"] == "blocked"
    # the reviewer verdict artifact exists for every round
    assert (root / ".buildwright" / "worktrees" / "f2_hub" / "harness" / "progress" / "review_f2_hub.md").is_file()

    out2 = resume(ctx, "approve", "focus on R2")
    assert interrupt_payload(out2)["gate"] == "completion"
    assert any("Human resume note: focus on R2" in p for p in dispatch.prompts)

    out3 = resume(ctx, "approve")
    assert isinstance(out3, dict) and out3["finalized"] is True
    fl = json.loads((root / "harness" / "feature_list.json").read_text())
    assert fl["features"][0]["status"] == "done"


def test_stall_protocol_reaches_cycle_cap(tmp_path: Path):
    root = make_repo(tmp_path)
    dispatch = FakeDispatch(root, writes=False)  # claims done, writes nothing
    ctx = ctx_for(root, dispatch, ScriptedReview(APPROVED))

    out = invoke(ctx, prepare_seed(ctx, None))
    payload = interrupt_payload(out)
    assert payload["gate"] == "parked"
    assert "cycle cap" in payload["reason"] and "no progress in 4 dispatches" in payload["reason"]

    with loop_session(root) as saver:
        graph = build_loop_graph(ctx, saver)
        state = graph.get_state(CFG).values  # type: ignore[arg-type]
    assert state["dispatches"] == 4
    assert state["restarts"] == 2  # two consecutive stalls -> fresh instance, twice
    assert state["stalls_total"] == 4
    # rejecting the parked gate leaves the feature blocked
    out2 = resume(ctx, "reject")
    assert isinstance(out2, dict) and "__interrupt__" not in out2
    fl = json.loads((root / "harness" / "feature_list.json").read_text())
    assert fl["features"][0]["status"] == "blocked"


def test_init_gate_failures_park_with_gate_fails(tmp_path: Path):
    root = make_repo(tmp_path, init_sh="#!/bin/bash\necho 'boom'\nexit 1\n")
    dispatch = FakeDispatch(root)
    ctx = ctx_for(root, dispatch, ScriptedReview(APPROVED))

    out = invoke(ctx, prepare_seed(ctx, None))
    payload = interrupt_payload(out)
    assert payload["gate"] == "parked"
    assert "init gate failed 3×" in payload["reason"]

    with loop_session(root) as saver:
        graph = build_loop_graph(ctx, saver)
        state = graph.get_state(CFG).values  # type: ignore[arg-type]
    assert state["gate_fails"] == {"init": 3}
    # each failed attempt fed the implementer the gate log tail
    assert any("init gate FAILED" in p for p in dispatch.prompts)


def test_run_limit_stops_clean_then_re_run_continues(tmp_path: Path):
    root = make_repo(
        tmp_path,
        tasks=("T1", "one", "T2", "two", "T3", "three", "T4", "four", "T5", "five"),
    )
    dispatch = FakeDispatch(root)
    ctx = ctx_for(root, dispatch, ScriptedReview(APPROVED), max_tasks=3, max_batches=1)

    out = invoke(ctx, prepare_seed(ctx, None))
    assert isinstance(out, dict) and "__interrupt__" not in out and not out.get("finalized")
    assert out["batch"] == ["T4", "T5"]

    # a fresh invocation continues from the persisted state
    with loop_session(root) as saver:
        graph = build_loop_graph(ctx, saver)
        snap = graph.get_state(CFG)  # type: ignore[arg-type]
        assert not pending_interrupt(snap)
        seed = prepare_seed(ctx, dict(snap.values))
        assert seed["run_limit"] == 2
        out2 = graph.invoke(seed, CFG)  # type: ignore[arg-type]
    assert interrupt_payload(out2)["gate"] == "completion"
    assert dispatch.prompts[0].count("T1") >= 1
    assert "T4, T5" in dispatch.prompts[-1]
    out3 = resume(ctx, "approve")
    assert isinstance(out3, dict) and out3["finalized"] is True


# ---------- gates (unit) ----------


def test_run_init_gate_ok_fail_and_marker(tmp_path: Path):
    root = make_repo(tmp_path)
    ctx = ctx_for(root, FakeDispatch(root), ScriptedReview(APPROVED))

    ok = run_init_gate(ctx)
    assert ok.ok and "green" in ok.log

    init = ctx.worktree / "harness" / "init.sh"
    init.write_text("#!/bin/bash\necho 'tests failed'\nexit 1\n", encoding="utf-8")
    fail = run_init_gate(ctx)
    assert not fail.ok and "tests failed" in fail.feedback
    assert (ctx.worktree / "harness" / "logs" / "f2_hub" / "gate_init.log").is_file()

    # exit 0 but no [OK] marker is still a failure (EARS)
    init.write_text("#!/bin/bash\necho 'hmm'\n", encoding="utf-8")
    assert not run_init_gate(ctx).ok

    # absent script -> gate skipped, not fatal
    init.unlink()
    assert run_init_gate(ctx).ok


def test_run_trace_gate_skips_and_fails(tmp_path: Path):
    root = make_repo(tmp_path)
    ctx = ctx_for(root, FakeDispatch(root), ScriptedReview(APPROVED))
    assert run_trace_gate(ctx).ok  # not present -> skipped

    tools = ctx.worktree / "harness" / "tools"
    tools.mkdir(parents=True, exist_ok=True)
    script = tools / "check-traceability.py"
    script.write_text("import sys\nsys.exit(1)\n", encoding="utf-8")
    fail = run_trace_gate(ctx)
    assert not fail.ok and "traceability gate FAILED" in fail.feedback

    script.write_text("print('all covered')\n", encoding="utf-8")
    assert run_trace_gate(ctx).ok


def test_run_audit_gate_levels(tmp_path: Path):
    root = make_repo(tmp_path)  # audit_level basic
    ctx = ctx_for(root, FakeDispatch(root), ScriptedReview(APPROVED))
    basic = run_audit_gate(ctx)
    assert basic.ok and "basic" in basic.log

    root = make_repo(tmp_path / "std", audit_level="standard")
    ctx = ctx_for(root, FakeDispatch(root), ScriptedReview(APPROVED))
    # missing script -> skipped
    assert run_audit_gate(ctx).ok
    tools = ctx.worktree / "harness" / "tools"
    tools.mkdir(parents=True, exist_ok=True)
    (tools / "audit-security.sh").write_text("#!/bin/bash\nexit 1\n", encoding="utf-8")
    fail = run_audit_gate(ctx)
    assert not fail.ok and "audit-security" in fail.feedback
    (tools / "audit-security.sh").write_text("#!/bin/bash\nexit 0\n", encoding="utf-8")
    assert run_audit_gate(ctx).ok

    root = make_repo(tmp_path / "strict", audit_level="strict")
    ctx = ctx_for(root, FakeDispatch(root), ScriptedReview(APPROVED))
    tools = ctx.worktree / "harness" / "tools"
    tools.mkdir(parents=True, exist_ok=True)
    (tools / "audit-security.sh").write_text("#!/bin/bash\nexit 0\n", encoding="utf-8")
    # bench.sh missing is noted, not fatal
    strict = run_audit_gate(ctx)
    assert strict.ok and "bench: not present" in strict.log
    (tools / "bench.sh").write_text("#!/bin/bash\nexit 2\n", encoding="utf-8")
    assert not run_audit_gate(ctx).ok


# ---------- artifact writers + fingerprint ----------


def test_set_feature_status_validates_schema(tmp_path: Path):
    root = make_repo(tmp_path)
    set_feature_status(root, "f2_hub", "done")
    fl = json.loads((root / "harness" / "feature_list.json").read_text())
    assert fl["features"][0]["status"] == "done"
    with pytest.raises(ValidationError):
        set_feature_status(root, "f2_hub", "finished")  # not a protocol status
    with pytest.raises(KeyError):
        set_feature_status(root, "nope", "done")


def test_worktree_fingerprint_is_content_aware(tmp_path: Path):
    root = make_repo(tmp_path)
    ctx = ctx_for(root, FakeDispatch(root), ScriptedReview(APPROVED))
    wt = ctx.worktree
    base = worktree_fingerprint(wt)
    target = wt / "notes.txt"
    target.write_text("v1\n", encoding="utf-8")
    v1 = worktree_fingerprint(wt)
    assert v1 != base
    # same path, same `?? notes.txt` line, different content -> different hash
    target.write_text("v2 — completely different\n", encoding="utf-8")
    assert worktree_fingerprint(wt) != v1
    target.unlink()
    assert worktree_fingerprint(wt) == base


def test_seed_guards(tmp_path: Path):
    root = make_repo(tmp_path)
    ctx = ctx_for(root, FakeDispatch(root), ScriptedReview(APPROVED))
    assert initial_seed(ctx)["batch"] == ["T1", "T2", "T3"]
    with pytest.raises(RuntimeError, match="already finalized"):
        prepare_seed(ctx, {"finalized": True})
    # no pending tasks -> nothing to run
    tasks = ctx.worktree / "harness" / "specs" / "f2_hub" / "tasks.md"
    tasks.write_text("- [x] T1: one\n", encoding="utf-8")
    with pytest.raises(RuntimeError, match="no pending tasks"):
        initial_seed(ctx)


def test_retro_line_counts():
    assert retro_line({"dispatches": 5, "stalls_total": 2, "restarts": 1}) == (
        "retro: 5 dispatches · 2 stalls · 1 restarts"
    )


# ---------- CLI ----------


def test_cli_run_parks_and_gate_approve_finalizes(tmp_path: Path, capsys):
    root = make_repo(tmp_path)
    dispatch = FakeDispatch(root)
    settings = Settings()

    cmd_harness_run(settings, None, 3, 10, str(root), True, dispatch=dispatch, review=ScriptedReview(APPROVED))
    printed = capsys.readouterr().out
    assert "[completion gate] f2_hub" in printed

    cmd_harness_gate(settings, "approve", "lgtm after review", None, str(root))
    approval = (root / "harness" / "specs" / "f2_hub" / "APPROVAL").read_text()
    assert "lgtm after review" in approval
    fl = json.loads((root / "harness" / "feature_list.json").read_text())
    assert fl["features"][0]["status"] == "done"


def test_cli_refuses_non_in_progress_features(tmp_path: Path):
    root = make_repo(tmp_path)
    set_feature_status(root, "f2_hub", "blocked")
    settings = Settings()
    # explicit --feature reaches the per-feature status guard
    with pytest.raises(RuntimeError, match="blocked"):
        cmd_harness_run(settings, "f2_hub", 3, 10, str(root), True)
    set_feature_status(root, "f2_hub", "done")
    with pytest.raises(RuntimeError, match="in_progress"):
        cmd_harness_run(settings, "f2_hub", 3, 10, str(root), True)
    # without --feature, a board with no in_progress feature is refused up front
    with pytest.raises(RuntimeError, match="no feature is in_progress"):
        cmd_harness_run(settings, None, 3, 10, str(root), True)


def test_cli_gate_without_worktree_errors(tmp_path: Path):
    root = make_repo(tmp_path)
    with pytest.raises(RuntimeError, match="no feature with a buildwright worktree"):
        cmd_harness_gate(Settings(), "approve", "", None, str(root))
