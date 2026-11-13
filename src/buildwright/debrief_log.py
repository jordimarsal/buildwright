"""Native debrief evidence: one BuildwrightLog-format file per feature episode.

debrief's ``run.inspect`` reads a ``key: value`` + ``step:`` log (its ADR-0002
adapter is the other place that knows this format); the loop is the only witness
of role outcomes, so it emits the contract lines itself — write at the source,
pin the shape with tests (the regexes below are mirrored from
``debrief/src/debrief/adapters/buildwright_log.py``; a divergence fails our
tests before it fails debrief's reader).

Human feedback keeps flowing through ``runlog.emit``: debrief ignores those
timestamped free-text lines, while every ``step:`` line here records one role
outcome the moment the loop knows it. ``result:`` is written only on a clean
finalize — a parked or aborted episode stays without it (the header is optional
in debrief's reader, and inventing an outcome for an unfinished episode would
fabricate evidence).

Not emitted (yet): gauntlet-module rejections (optional module, not installed
in any consuming project — add a ``step`` at the reject branch when one does).
"""

from __future__ import annotations

import re
import sys
from datetime import UTC, datetime
from importlib.metadata import PackageNotFoundError, version
from pathlib import Path

OUTCOMES = frozenset({"pass", "fail"})
_STEP_RE = re.compile(
    r"^step:\s*(?P<ordinal>\d+)\s+"
    r"role=(?P<role>\S+)\s+"
    r"model=(?P<model>\S+)\s+"
    r"outcome=(?P<outcome>\S+)\s+"
    r'reason="(?P<reason>[^"]*)"\s+'
    r"evidence=(?P<evidence>\S+)$"
)
_RESULT_RE = re.compile(r"^(?P<outcome>\S+)(?:\s+failed_steps=(?P<failed_steps>\S*))?$")

ARTIFACTS_RELATIVE = Path("artifacts") / "buildwright"
_HEADER = "buildwright run log — evidence of one episode; debrief reads it, never edits it"


def engine_build() -> str:
    """The writer's identity as one token: buildwright-<version>."""
    try:
        return f"buildwright-{version('buildwright')}"
    except PackageNotFoundError:  # direct src/ checkout without an install
        return "buildwright-dev"


def sanitize(text: str) -> str:
    """One line, no double quotes — the adapter's reason regex is ``[^"]*``."""
    return " ".join(text.split()).replace('"', "'").strip()


class DebriefSink:
    """Streams one feature episode into ``<root>/artifacts/buildwright/<run_id>.log``.

    Headers go down at creation (the feature and role routing are known then),
    one ``step:`` line per recorded outcome, and ``result:`` at ``finish()`` —
    computed from the recorded steps so it can never contradict them.
    """

    def __init__(
        self,
        root: Path,
        feature: str,
        engine: str,
        roles: dict[str, str],
        run_id: str | None = None,
    ) -> None:
        self.run_id = run_id or (
            f"{feature}-{datetime.now(UTC):%Y-%m-%dT%H-%MZ}"
        )
        self.path = root / ARTIFACTS_RELATIVE / f"{self.run_id}.log"
        self._feature = feature
        self._steps: list[tuple[str, str, str, str, str]] = []
        self._finished = False
        self.path.parent.mkdir(parents=True, exist_ok=True)
        roles_header = " ".join(f"BW_ROLE_{r.upper()}={m}" for r, m in roles.items())
        self.path.write_text(
            "\n".join(
                [
                    _HEADER,
                    f"run: {self.run_id}",
                    f"engine_build: {engine}",
                    f"task: {feature}",
                    f"roles: {roles_header}",
                ]
            )
            + "\n",
            encoding="utf-8",
        )

    def step(self, role: str, model: str, outcome: str, reason: str, evidence: str) -> None:
        """Record one role outcome. Only pass/fail exist — anything else is a
        writer bug that must fail here, not corrupt debrief's reader."""
        if outcome not in OUTCOMES:
            raise ValueError(f"outcome must be one of {sorted(OUTCOMES)}: {outcome!r}")
        if self._finished:
            raise ValueError("episode already finished")
        clean = sanitize(reason)
        if not clean:
            raise ValueError("an empty reason would forge an evidence line")
        self._steps.append((role, model, outcome, clean, evidence))
        with self.path.open("a", encoding="utf-8") as f:
            f.write(
                f"step: {len(self._steps):02d} role={role} model={model} "
                f'outcome={outcome} reason="{clean}" evidence={evidence}\n'
            )

    def finish(self) -> str | None:
        """Write the ``result:`` header; None when the episode logged no steps
        (a headers-only log would read as a lie by omission in debrief)."""
        if self._finished:
            return None
        self._finished = True
        if not self._steps:
            return None
        failed = [str(i) for i, s in enumerate(self._steps, 1) if s[2] == "fail"]
        result = "pass" if not failed else f"fail failed_steps={','.join(failed)}"
        if _RESULT_RE.fullmatch(result) is None:  # pragma: no cover — writer bug guard
            print(f"debrief: malformed result line {result!r}", file=sys.stderr)
            return None
        with self.path.open("a", encoding="utf-8") as f:
            f.write(f"result: {result}\n")
        return result
