"""Line-preserving, atomic `.env` writer for `BW_ROLE_*` keys.

Every existing line survives byte-for-byte — comments, order, quoting — except
the target key line, replaced in place; absent keys are appended. The file is
written to a sibling temp and ``os.replace``d, so a crash cannot leave a
half-`.env`. A missing file is created with a one-line header comment.

Spec: docs/specs/2026-11-14-roles-apply-design.md §6.
"""

from __future__ import annotations

import os
from collections.abc import Iterable
from pathlib import Path

from .validate import PlanChange

HEADER = "# buildwright role routing — managed by roles apply"


def apply_changes(path: Path, changes: Iterable[PlanChange]) -> tuple[int, bool]:
    """Apply every non-noop change; returns (changed_lines, created).

    Noop changes never touch the file, so re-applying an already-applied
    proposal is a clean no-op.
    """
    active = [c for c in changes if not c.noop]
    created = not path.exists()
    lines: list[str] = []
    if created:
        lines.append(HEADER)
    else:
        lines = path.read_text(encoding="utf-8").splitlines()

    changed = 0
    for ch in active:
        key = f"{ch.role_env}={ch.target}"
        idx = next(
            (i for i, ln in enumerate(lines) if ln.startswith(f"{ch.role_env}=")),
            None,
        )
        if idx is None:
            lines.append(key)
        else:
            lines[idx] = key
        changed += 1

    if changed:
        tmp = path.with_name(f"{path.name}.tmp")
        tmp.write_text("\n".join(lines) + "\n", encoding="utf-8")
        os.replace(tmp, path)
    return changed, created
