import subprocess
from pathlib import Path

import pytest

from buildwright.runtime import (
    Sandbox,
    command_allowed,
    create_worktree,
    next_batch,
    parse_tasks,
    resolve_in_sandbox,
    tool_edit_file,
    tool_list_dir,
    tool_read_file,
    tool_run_command,
    tool_write_file,
    verify_on_disk,
)

TASKS_MD = """# Tasks

- [x] T1: existing helper
- [ ] T2: add helper
- [ ] T3: test helper
- [ ] T4: docs
- [ ] T5: extra
- [ ] T6: more
"""


def test_command_allowed_matches_prefixes():
    allowed = ("uv run", "git status")
    assert command_allowed("uv run pytest -q", allowed)
    assert command_allowed("git status", allowed)
    assert not command_allowed("uv sync", allowed)
    assert not command_allowed("rm -rf /", allowed)
    assert not command_allowed("git statusx", allowed)


def test_resolve_in_sandbox_refuses_escape(tmp_path):
    sandbox = Sandbox(root=tmp_path)
    assert resolve_in_sandbox(sandbox, "src/a.py") == (tmp_path / "src/a.py").resolve()
    with pytest.raises(PermissionError, match="escapes"):
        resolve_in_sandbox(sandbox, "../outside.txt")
    with pytest.raises(PermissionError, match="escapes"):
        resolve_in_sandbox(sandbox, "/etc/passwd")


def test_parse_tasks_and_next_batch_cap():
    tasks = parse_tasks(TASKS_MD)
    assert [t.task_id for t in tasks] == ["T1", "T2", "T3", "T4", "T5", "T6"]
    assert tasks[0].done and not tasks[1].done
    batch = next_batch(tasks, max_tasks=3)
    assert [t.task_id for t in batch] == ["T2", "T3", "T4"]
    # harness discipline: never 5+, whatever the ask
    assert len(next_batch(tasks, max_tasks=99)) == 4


def test_tool_file_roundtrip(tmp_path):
    sandbox = Sandbox(root=tmp_path)
    assert tool_write_file(sandbox, "src/mod.py", "x = 1\n") == "wrote src/mod.py (6 chars)"
    assert "x = 1" in tool_read_file(sandbox, "src/mod.py")
    assert tool_edit_file(sandbox, "src/mod.py", "x = 1", "x = 2") == "edited src/mod.py"
    assert "x = 2" in tool_read_file(sandbox, "src/mod.py")
    assert "mod.py" in tool_list_dir(sandbox, "src")
    # edit must be unambiguous: two occurrences are refused
    tool_write_file(sandbox, "dup.txt", "a\na\n")
    assert "exactly once" in tool_edit_file(sandbox, "dup.txt", "a", "b")


def test_tool_run_command_allow_and_deny(tmp_path):
    sandbox = Sandbox(root=tmp_path, allowed_commands=("echo",))
    out = tool_run_command(sandbox, "echo hello")
    assert out.startswith("exit=0")
    assert "hello" in out
    out = tool_run_command(sandbox, "curl http://evil.example")
    assert out.startswith("ERROR: command not allowed")


def test_sandbox_reads_runtime_yaml(tmp_path):
    (tmp_path / "harness").mkdir()
    (tmp_path / "harness" / "runtime.yaml").write_text(
        "extra_allowed_commands:\n  - terraform plan\ncommand_timeout_s: 30\n",
        encoding="utf-8",
    )
    sandbox = Sandbox.from_project(tmp_path)
    assert "terraform plan" in sandbox.allowed_commands
    assert "uv run" in sandbox.allowed_commands  # defaults still hold
    assert sandbox.timeout_s == 30


def git(cwd: Path, *args: str) -> None:
    subprocess.run(
        ["git", "-C", str(cwd), "-c", "user.name=t", "-c", "user.email=t@t", *args],
        check=True,
        capture_output=True,
    )


@pytest.fixture
def harness_repo(tmp_path: Path) -> Path:
    root = tmp_path / "proj"
    (root / "harness" / "specs" / "f1_api").mkdir(parents=True)
    (root / "harness" / "feature_list.json").write_text(
        '{"project": {"name": "p", "parallel": false, "modules": [], '
        '"audit_level": "basic"}, "features": []}',
        encoding="utf-8",
    )
    (root / "harness" / "specs" / "f1_api" / "tasks.md").write_text(
        "- [ ] T1: one\n- [ ] T2: two\n", encoding="utf-8"
    )
    git(root, "init", "-b", "main")
    git(root, "add", "-A")
    git(root, "commit", "-m", "init")
    return root


def test_worktree_and_verify_on_disk(harness_repo: Path):
    batch = parse_tasks("- [ ] T1: one\n- [ ] T2: two\n")
    wt = create_worktree(harness_repo, "f1_api")
    assert (wt / "harness" / "feature_list.json").is_file()
    assert ".buildwright/" in (harness_repo / ".git" / "info" / "exclude").read_text()

    # the agent marks T1 done inside the worktree
    tasks = wt / "harness" / "specs" / "f1_api" / "tasks.md"
    tasks.write_text("- [x] T1: one\n- [ ] T2: two\n", encoding="utf-8")

    report = verify_on_disk(wt, "f1_api", batch)
    assert report.marked == ("T1",)
    assert report.batch == ("T1", "T2")
    assert "harness/specs/f1_api/tasks.md" in report.changed_files
    # reuse: second call must not create a second worktree
    assert create_worktree(harness_repo, "f1_api") == wt
