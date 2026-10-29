"""runlog.emit: live stderr line + append to .buildwright/run.log."""

from __future__ import annotations

from buildwright import runlog
from buildwright.runlog import LOG_RELATIVE


def test_emit_returns_bare_msg_and_prints_stderr(tmp_path, capsys) -> None:
    returned = runlog.emit("spec: authoring", tmp_path)
    assert returned == "spec: authoring"
    err = capsys.readouterr().err
    assert err.startswith("[")
    assert "spec: authoring" in err


def test_emit_appends_iso_stamped_line(tmp_path) -> None:
    runlog.emit("dispatch #1 → done T1", tmp_path)
    path = tmp_path / LOG_RELATIVE
    content = path.read_text(encoding="utf-8")
    line = content.strip()
    # ISO stamp prefix (YYYY-MM-DDTHH:MM:SS) + the message
    assert line.endswith("dispatch #1 → done T1")
    date_part = line.split(" ", 1)[0]
    assert date_part[:4].isdigit() and date_part.count("-") == 2 and "T" in date_part


def test_emit_creates_missing_dirs(tmp_path) -> None:
    deep = tmp_path / "a" / "b"
    runlog.emit("run: start", deep)
    assert (deep / LOG_RELATIVE).is_file()
