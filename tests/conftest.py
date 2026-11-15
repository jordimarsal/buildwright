"""Hermetic test fixtures for buildwright.

The suite must not inherit the developer's debrief wiring: with BW_DEBRIEF_ROOT
set in the shell, every test wrote into the REAL central evidence tree
(project `demo` from make_repo) and collided on shared episode paths — the
flaky half of the test_loop failures (spec 2026-11-15-loop-episode-gate-fix §1.B).
"""

from __future__ import annotations

import os

import pytest

_SCRUBBED = ("BW_DEBRIEF_ROOT", "DEBRIEF_HOME")


@pytest.fixture(autouse=True)
def hermetic_debrief_env() -> None:
    """Drop BW_DEBRIEF_ROOT/DEBRIEF_HOME for one test, restore them after."""
    saved = {k: os.environ.pop(k) for k in _SCRUBBED if k in os.environ}
    try:
        yield
    finally:
        os.environ.update(saved)
