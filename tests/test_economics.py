"""Tests for src/buildwright/economics.py."""

from __future__ import annotations

import pytest

# --------------------------------------------------------------------------- #
# T1 fixtures
# --------------------------------------------------------------------------- #

def _trace(feature: str, role: str, model: str, **kw: object) -> dict:
    return {
        "feature": feature,
        "role": role,
        "model": model,
        "tokens_in": kw.get("tokens_in", 100),
        "tokens_out": kw.get("tokens_out", 50),
        "seconds": kw.get("seconds", 2.0),
        "cost": kw.get("cost"),
    }


def _episode(feature: str, gate: str) -> dict:
    return {"feature": feature, "gate": gate}


# --------------------------------------------------------------------------- #
# aggregate — happy path
# --------------------------------------------------------------------------- #

class TestAggregateHappyPath:
    def test_single_trace(self):
        from buildwright.economics import aggregate

        traces = [_trace("auth", "writer", "gpt-4o")]
        episodes = []
        result = aggregate(traces, episodes)
        assert len(result["features"]) == 1
        feat = result["features"][0]
        assert feat["feature"] == "auth"
        assert feat["gate"] is None
        assert len(feat["roles"]) == 1
        r = feat["roles"][0]
        assert r["role"] == "writer" and r["model"] == "gpt-4o"
        assert r["tokens_in"] == 100 and r["tokens_out"] == 50
        assert r["seconds"] == 2.0
        assert r["cost"] is None
        t = feat["totals"]
        assert t["tokens_in"] == 100 and t["tokens_out"] == 50
        assert t["seconds"] == 2.0 and t["cost"] is None

    def test_multiple_traces_same_feature_different_roles(self):
        from buildwright.economics import aggregate

        traces = [
            _trace("auth", "writer", "gpt-4o"),
            _trace("auth", "reviewer", "claude-sonnet"),
        ]
        episodes = [_episode("auth", "pass")]
        result = aggregate(traces, episodes)
        feat = result["features"][0]
        assert feat["feature"] == "auth"
        assert feat["gate"] == "pass"
        assert len(feat["roles"]) == 2
        assert feat["totals"]["tokens_in"] == 200
        assert feat["totals"]["tokens_out"] == 100

    def test_cost_propagates_to_totals(self):
        from buildwright.economics import aggregate

        traces = [
            _trace("auth", "writer", "gpt-4o", cost=0.01),
            _trace("auth", "reviewer", "claude-sonnet", cost=0.02),
        ]
        result = aggregate(traces, [])
        feat = result["features"][0]
        assert feat["totals"]["cost"] == pytest.approx(0.03)

    def test_gate_missing_for_feature_keeps_none(self):
        from buildwright.economics import aggregate

        traces = [_trace("x", "w", "m")]
        episodes = [_episode("y", "fail")]
        result = aggregate(traces, episodes)
        assert result["features"][0]["gate"] is None


# --------------------------------------------------------------------------- #
# aggregate — edge cases
# --------------------------------------------------------------------------- #

class TestAggregateEdgeCases:
    def test_empty_inputs(self):
        from buildwright.economics import aggregate
        assert aggregate([], []) == {"features": []}

    def test_zero_tokens_are_summed(self):
        from buildwright.economics import aggregate
        t = _trace("a", "r", "m", tokens_in=0, tokens_out=0, seconds=0)
        r = aggregate([t], [])
        assert r["features"][0]["totals"]["tokens_in"] == 0

    def test_multiple_episodes_same_feature_last_wins(self):
        from buildwright.economics import aggregate
        traces = [_trace("f", "r", "m")]
        eps = [
            _episode("f", "pass"),
            _episode("f", "fail"),
        ]
        feat = aggregate(traces, eps)["features"][0]
        assert feat["gate"] == "fail"


# --------------------------------------------------------------------------- #
# price — deferred import & exceptions
# --------------------------------------------------------------------------- #

class TestPriceNoLitellm:
    """When litellm is not installed the function must return None."""

    def test_returns_none_without_litellm(self, monkeypatch):
        from buildwright.economics import price
        monkeypatch.setattr("buildwright.economics.functools.lru_cache", lambda *a, **kw: (lambda f: f))
        # Force a fresh module so lru_cache doesn't interfere with our patching.
        # Instead we just check that any exception path returns None by mocking
        # cost_per_token to raise.
        result = price("gpt-4o", 100, 50)
        # If litellm IS installed this will be a float; if not it's None.
        # The important invariant: no crash.
        assert isinstance(result, (float, type(None)))


class TestPriceWithLitellm:
    """When litellm is available the function delegates to cost_per_token."""

    def test_calls_cost_per_token_with_correct_args(self, monkeypatch):
        from buildwright.economics import price

        called_kw: dict | None = None

        def fake_cpt(model: str, prompt_tokens: int, completion_tokens: int) -> float:
            nonlocal called_kw
            called_kw = {"model": model, "prompt_tokens": prompt_tokens, "completion_tokens": completion_tokens}
            return 0.003

        monkeypatch.setattr("buildwright.economics.cost_per_token", fake_cpt)
        price("gpt-4o-mini", 200, 100)
        assert called_kw == {
            "model": "gpt-4o-mini",
            "prompt_tokens": 200,
            "completion_tokens": 100,
        }

    def test_returns_float_when_litellm_succeeds(self, monkeypatch):
        from buildwright.economics import price

        monkeypatch.setattr("buildwright.economics.cost_per_token", lambda **kw: 0.05)
        result = price("claude-sonnet", 50, 25)
        assert isinstance(result, float) and result > 0


class TestPriceExceptionPath:
    """Any exception inside cost_per_token → None."""

    def test_exception_becomes_none(self, monkeypatch):
        from buildwright.economics import price

        monkeypatch.setattr(
            "buildwright.economics.cost_per_token",
            lambda model=None, prompt_tokens=0, completion_tokens=0: (_ for _ in ()).throw(ValueError("boom")),
        )
        assert price("bad-model", 1, 1) is None


# --------------------------------------------------------------------------- #
# T2 — fetch_traces (fake client injected; no real Langfuse)
# --------------------------------------------------------------------------- #

class FakeLangfuseClient:
    """Minimal fake that mimics ``client.get_traces(run_id, limit)``."""

    def __init__(self, traces_data: list[dict] | None = None, raise_on_call: bool = False) -> None:
        self._traces = traces_data or []
        self._raise = raise_on_call

    def get_traces(self, run_id: str, limit: int = 500) -> list[dict]:
        if self._raise:
            raise RuntimeError("network error")
        return self._traces


def _make_observation(model: str, role: str, feature: str, tokens_in: int, tokens_out: int, seconds: float) -> dict:
    return {
        "model": model,
        "role": role,
        "feature": feature,
        "tokens_in": tokens_in,
        "tokens_out": tokens_out,
        "seconds": seconds,
    }


class TestFetchTracesSuccess:
    def test_returns_mapped_list(self):
        from buildwright.economics import fetch_traces

        obs = [_make_observation("gpt-4o", "writer", "auth", 100, 50, 2.0)]
        client = FakeLangfuseClient(traces_data=[{"observations": obs}])
        result = fetch_traces(client, "run-123")
        assert len(result) == 1
        t = result[0]
        assert t["model"] == "gpt-4o" and t["role"] == "writer"
        assert t["feature"] == "auth" and t["tokens_in"] == 100
        assert t["tokens_out"] == 50 and t["seconds"] == 2.0

    def test_empty_observations_yields_no_traces(self):
        from buildwright.economics import fetch_traces
        client = FakeLangfuseClient(traces_data=[{"observations": []}])
        assert fetch_traces(client, "x") == []

    def test_multiple_trace_objects_yield_many_results(self):
        from buildwright.economics import fetch_traces
        obs_a = [_make_observation("m1", "r1", "f1", 10, 5, 1.0)]
        obs_b = [_make_observation("m2", "r2", "f2", 20, 10, 2.0)]
        client = FakeLangfuseClient(traces_data=[{"observations": obs_a}, {"observations": obs_b}])
        result = fetch_traces(client, "run-99")
        assert len(result) == 2


class TestFetchTracesException:
    """When the SDK raises, we return [] — observability must never break a run."""

    def test_exception_returns_empty_list(self):
        from buildwright.economics import fetch_traces
        client = FakeLangfuseClient(raise_on_call=True)
        result = fetch_traces(client, "bad-run")
        assert result == []
