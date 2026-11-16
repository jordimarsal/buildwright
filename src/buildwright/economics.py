"""Buildwright agent-economics aggregation (spec 2026-11-15-agent-economics).

Per feature and per role: tokens in/out, cost in $, wall time, and the completion-gate
verdict. Raw material: Langfuse observations mapped to flat trace dicts, and episode
gates. Pure core + two seams (cost price, trace fetch) so everything is testable
offline — observability must never break a run."""

from __future__ import annotations

import functools
from pathlib import Path
from typing import Any

__all__ = ["aggregate", "cost_per_token", "fetch_traces", "price"]


def cost_per_token(model: str, prompt_tokens: int, completion_tokens: int) -> float:
    """Injectable seam over litellm.cost_per_token — returns the TOTAL cost of the
    call. Tests patch THIS, not litellm. The import happens inside the function so
    ``import buildwright.economics`` never pulls litellm at module load. Raises on
    unknown model / missing package — the caller (price) turns that into None → "?".
    """
    from litellm import cost_per_token as _litellm_cost  # type: ignore[attr-defined]

    prompt_cost, completion_cost = _litellm_cost(
        model=model, prompt_tokens=prompt_tokens, completion_tokens=completion_tokens
    )
    return float(prompt_cost) + float(completion_cost)


@functools.lru_cache(maxsize=128)
def price(model: str, tokens_in: int, tokens_out: int) -> float | None:
    """Total cost of one call; None on any failure — callers render "?" (a number
    without provenance is worse than a gap)."""
    try:
        return round(
            cost_per_token(model=model, prompt_tokens=tokens_in,
                           completion_tokens=tokens_out), 6
        )
    except Exception:  # noqa: BLE001 - unknown model or missing pricing is a gap
        return None


def aggregate(
    traces: list[dict[str, Any]], episodes: list[dict[str, str]]
) -> dict[str, Any]:
    """Group flat trace dicts by (feature, role, model) and join gate verdicts.

    traces: {"feature", "role", "model", "tokens_in", "tokens_out", "seconds",
             "cost"(float|None — precomputed by the caller via price())}
    episodes: {"feature", "gate"} — LAST entry per feature wins.
    """
    gates: dict[str, str] = {}
    for e in episodes:
        gates[str(e["feature"])] = str(e["gate"])

    features: dict[str, dict[str, Any]] = {}
    for t in traces:
        feature = str(t["feature"])
        feat = features.setdefault(feature, {
            "feature": feature,
            "gate": gates.get(feature),
            "roles": [],
            "totals": {"tokens_in": 0, "tokens_out": 0, "seconds": 0.0, "cost": None},
        })
        cost = t.get("cost")
        role_row = next(
            (r for r in feat["roles"]
             if r["role"] == t["role"] and r["model"] == t["model"]),
            None,
        )
        if role_row is None:
            role_row = {"role": str(t["role"]), "model": str(t["model"]),
                        "tokens_in": 0, "tokens_out": 0, "seconds": 0.0,
                        "cost": None}
            feat["roles"].append(role_row)
        tin = int(t.get("tokens_in") or 0)
        tout = int(t.get("tokens_out") or 0)
        secs = float(t.get("seconds") or 0.0)
        role_row["tokens_in"] += tin
        role_row["tokens_out"] += tout
        role_row["seconds"] = round(role_row["seconds"] + secs, 3)
        if cost is not None:
            role_row["cost"] = round((role_row["cost"] or 0.0) + float(cost), 6)
        feat["totals"]["tokens_in"] += tin
        feat["totals"]["tokens_out"] += tout
        feat["totals"]["seconds"] = round(feat["totals"]["seconds"] + secs, 3)
        if cost is not None:
            feat["totals"]["cost"] = round((feat["totals"]["cost"] or 0.0) + float(cost), 6)

    for feat in features.values():
        feat["gate"] = gates.get(feat["feature"])

    return {"features": list(features.values())}


def fetch_traces(client: Any, run_id: str, limit: int = 500) -> list[dict[str, Any]]:
    """Pull one run's observations from a Langfuse client and map them into the flat
    trace dicts aggregate() consumes. On ANY SDK exception returns [] — observability
    must never break a run."""
    try:
        traces = client.get_traces(run_id=run_id, limit=limit)
    except Exception:  # noqa: BLE001
        return []
    out: list[dict[str, Any]] = []
    for tr in traces or []:
        for obs in tr.get("observations") or []:
            out.append({
                "model": str(obs.get("model")),
                "role": str(obs.get("role")),
                "feature": str(obs.get("feature")),
                "tokens_in": int(obs.get("tokens_in") or 0),
                "tokens_out": int(obs.get("tokens_out") or 0),
                "seconds": float(obs.get("seconds") or 0.0),
                "cost": None,
            })
    return out


def read_gates(base: Path) -> list[dict[str, str]]:
    """Parse the central debrief tree (base/<project>/exec<id>/<feature>.log) into
    episode dicts for aggregate() — the gate verdicts live in `result:` lines and the
    feature in `task:` headers. Logs without both are skipped."""
    out: list[dict[str, str]] = []
    if not Path(base).is_dir():
        return out
    for log in sorted(Path(base).glob("*/*/exec*/*.log")):
        feature: str | None = None
        gate: str | None = None
        for line in log.read_text(encoding="utf-8", errors="replace").splitlines():
            if line.startswith("task: "):
                feature = line[len("task: "):].strip()
            elif line.startswith("result: "):
                raw = line[len("result: "):].strip()
                gate = "pass" if raw == "pass" else "fail"
        if feature and gate:
            out.append({"feature": feature, "gate": gate})
    return out
