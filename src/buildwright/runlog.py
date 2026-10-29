"""Live run feedback: phase events to stderr + `.buildwright/run.log`.

The loop log (`state["log"]`) is the structured record printed when the run
stops; `emit()` surfaces the same milestones the moment they happen so a
running pipeline is not silent for minutes while an agent thinks. stderr
keeps stdout free for the interactive gates.
"""

from __future__ import annotations

import sys
from datetime import datetime
from pathlib import Path

LOG_RELATIVE = Path(".buildwright") / "run.log"


def emit(msg: str, root: Path | None = None) -> str:
    """Print `[HH:MM:SS] msg` to stderr and append to `<root>/.buildwright/run.log`.

    Returns the bare msg so call sites can also feed `state["log"]`.
    """
    now = datetime.now()
    print(f"[{now:%H:%M:%S}] {msg}", file=sys.stderr, flush=True)
    path = (root or Path.cwd()) / LOG_RELATIVE
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as f:
        f.write(f"{now.isoformat(timespec='seconds')} {msg}\n")
    return msg
