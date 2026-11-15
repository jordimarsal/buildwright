# Design — agent economics: cost, tokens and gates per feature and role

**Date:** 2026-11-15
**Status:** 📝 spec (pending human spec gate)
**Scope:** `buildwright` — one new report command (`economics`), a price-table module,
tests, docs. Reads Langfuse traces and run logs; writes to the central debrief tree.
No changes in `debrief-mcp` (its artifacts are the *input*; a later enrichment of
`routing.propose` is out of scope here and belongs to debrief).

---

## 1. Context

Routing decisions today are quality-driven: ggufbench says what a model CAN do,
debrief proposes `BW_ROLE_*` from that, the human gates. What the loop never answers
is the question that decides between two candidates in real operation:

> **what did feature X cost, in tokens and money, through role R — and did it pass?**

The raw material already exists and is already being written:

- **Langfuse traces** — every LLM call of a run (model, usage in/out, latency).
- **Run logs** — episode structure: feature → tasks → episodes → roles that executed.
- **The completion gate** — the pass/fail verdict per feature.
- **LiteLLM** — `cost_per_token(model, prompt_tokens, completion_tokens)` already
  ships pricing for the models the registry uses; no home-made price table needed.

The receipts culture of the rig (ggufbench's context receipts, debrief's evidence
packs) stops one step short: the run burns the tokens, nobody totals them.

## 2. Core insight — the report is an aggregation, the artifact is the contract

No new collection, no new instrumentation: every number is already in Langfuse or the
run log. The feature is a **deterministic aggregation** with one output contract, so
debrief (and the human) can consume it like any other receipt:

```
bw economics --run <run-id>            # the run's features, human table to stdout
bw economics --run <run-id> --json     # machine artifact → central debrief tree
bw economics --feature <slug> --all    # across runs, per role: the routing lens
```

## 3. Output contract

Markdown (stdout, human) and JSON (artifact, machine) with the same numbers:

| feature | role | model | in tok | out tok | cost $ | wall | gate |
|---|---|---|---|---|---|---|---|
| add_feature | BW_IMPLEMENTER | `<id>` | 12.4k | 3.1k | 0.0142 | 3m41s | ✅ |
| add_feature | BW_REVIEWER | `<id>` | 5.8k | 0.9k | 0.0061 | 1m12s | ✅ |
| **feature total** | | | **18.2k** | **4.0k** | **0.0203** | 4m53s | **pass** |

JSON adds: per-episode breakdown, per-call rows (trace ids, for Langfuse cross-ref),
price source note (`litellm:<date>`), and a `cheaper_candidate` hint **only** when the
same role's ggufbench row scores within the routing tolerance at a lower unit price —
computed from the catalog, never written to `models.yaml`.

## 4. Design decisions

- **Prices from LiteLLM's table, versioned in the artifact.** A model missing from the
  table → cost cell `?` and a warning line; the report never guesses (the receipts
  rule: a number without provenance is worse than a gap).
- **Cost is per-call summed, never extrapolated**: retries, failed calls and the
  completion gate's review passes all count. An episode that died costs what it burned.
- **Artifact lands in the central debrief tree** (`artifacts/` per the migration), so
  `debrief bench_query`-style readers can cite it later — same rule that moved
  ggufbench evidence out of the in-repo `artifacts/`.
- **No budget enforcement, no auto-switching.** v1 reports. If a `--max-cost` guard is
  ever wanted, it is a gate policy decision (harness), not a reporting one.

## 5. Tasks (test-first, offline)

- [x] T1: `economics.py` aggregation core — pure functions over a captured trace/JSON
      fixture (no Langfuse in tests): per feature/role/model totals, gate join, price
      lookup with the `?`-and-warn path. Tests: totals, missing-price, empty run.
      refs: §3, §4.
- [x] T2: Langfuse adapter — pull the run's traces (paged), map to episodes; adapter
      stubbed in tests; integration marked and skipped when no server.
      refs: §1, §3.
- [x] T3: `bw economics` CLI — `--run/--feature/--json`, stdout table, artifact write
      to the debrief tree with the receipt header (tool version, price source, host).
      Tests: CLI exit codes, artifact shape, tree placement.
      refs: §2, §3, §4.
- [x] T4: docs — README section + an example artifact under `docs/examples/`; the
      debrief-citation note (what a future `routing.propose` enrichment could read).
      refs: §2.

## 6. Verification

pytest offline green (suite currently 105/107 — the 2 `test_loop` failures are a
separate spec, `2026-11-15-loop-episode-gate-fix.md`); mypy `--strict` clean; one real
run's artifact eyeballed against the Langfuse UI totals (±1 rounding on the sum).

## 7. Completion gate

✅ Approved 2026-11-15 (commit `c971c29`, 18 economics tests, suite 124/126,
mypy src clean). T1-T4 done.
