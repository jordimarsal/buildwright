"""Offline tests for `buildwright economics` (T3): the command is fully stubbed —
fake tracer client, fake price seam, fake gate reader. No Langfuse, no network."""

import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from buildwright import cli


class _FakeTracer:
    """Duck-typed stand-in with the private client cmd_economics reads."""

    def __init__(self, client: object) -> None:
        self._client = client


class _FakeClient:
    def __init__(self, traces: list[dict]) -> None:
        self._traces = traces

    def get_traces(self, run_id: str, limit: int = 500) -> list[dict]:
        return [
            {"observations": [{"model": "local/qwen36", "role": "worker",
                               "feature": "auth", "tokens_in": 1000,
                               "tokens_out": 500, "seconds": 3.0}]},
            {"observations": [{"model": "local/qwen36", "role": "reviewer",
                               "feature": "auth", "tokens_in": 600,
                               "tokens_out": 200, "seconds": 1.5}]},
        ]


@pytest.fixture()
def seeded(tmp_path: Path):
    cfg_dir = tmp_path / "debrief" / "proj" / "exec2026"
    cfg_dir.mkdir(parents=True)
    (cfg_dir / "auth.log").write_text(
        "task: auth\nresult: pass\n", encoding="utf-8")
    settings = SimpleNamespace(debrief_root=tmp_path / "debrief")
    return settings, tmp_path / "econ.json"


def test_exit_65_when_langfuse_not_configured(seeded) -> None:
    settings, _ = seeded
    with patch.object(cli, "get_tracer", return_value=object()):
        rc = cli.cmd_economics(settings, "run-1", None, None)
    assert rc == 65


def test_report_and_artifact(seeded) -> None:
    settings, json_path = seeded
    with patch.object(cli, "get_tracer", return_value=_FakeTracer(_FakeClient([]))), \
         patch.object(cli, "_fetch_traces",
                      side_effect=lambda client, run_id, limit=500: [
                          {"feature": "auth", "role": "worker",
                           "model": "local/qwen36", "tokens_in": 1000,
                           "tokens_out": 500, "seconds": 3.0, "cost": 0.004},
                          {"feature": "auth", "role": "reviewer",
                           "model": "local/strata", "tokens_in": 600,
                           "tokens_out": 200, "seconds": 1.5, "cost": 0.002},
                      ]), \
         patch.object(cli, "_price", return_value=None), \
         patch.object(cli, "_read_gates",
                      return_value=[{"feature": "auth", "gate": "pass"}]):
        rc = cli.cmd_economics(settings, "run-1", None, str(json_path))
    assert rc == 0
    artifact = json.loads(json_path.read_text())
    assert artifact["run_id"] == "run-1"
    feats = artifact["report"]["features"]
    assert feats[0]["feature"] == "auth"
    assert feats[0]["gate"] == "pass"
    assert feats[0]["totals"]["tokens_in"] == 1600


def test_exit_1_when_no_observations(seeded) -> None:
    settings, _ = seeded
    with patch.object(cli, "get_tracer", return_value=_FakeTracer(_FakeClient([]))), \
         patch.object(cli, "_fetch_traces", return_value=[]):
        rc = cli.cmd_economics(settings, "run-1", None, None)
    assert rc == 1


def test_json_without_path_lands_in_debrief_tree(seeded) -> None:
    """`--json` with no PATH → central debrief tree, with the receipt header."""
    settings, _ = seeded
    with patch.object(cli, "get_tracer", return_value=_FakeTracer(_FakeClient([]))), \
         patch.object(cli, "_fetch_traces",
                      return_value=[{"feature": "auth", "role": "worker",
                                     "model": "local/qwen36", "tokens_in": 10,
                                     "tokens_out": 5, "seconds": 1.0, "cost": None}]), \
         patch.object(cli, "_price", return_value=None), \
         patch.object(cli, "_read_gates", return_value=[]):
        rc = cli.cmd_economics(settings, "run-42", None, str(
            settings.debrief_root / "economics" / "run-42.json"))
    assert rc == 0
    art = json.loads((settings.debrief_root / "economics" / "run-42.json").read_text())
    assert art["tool"].startswith("buildwright ")
    assert art["run_id"] == "run-42"
    assert art["host"] and art["price_source"] == "litellm.cost_per_token"
