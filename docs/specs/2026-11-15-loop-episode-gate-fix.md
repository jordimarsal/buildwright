# Design — fix the `test_loop.py` failures: episode finalize vs the central-tree migration

**Date:** 2026-11-15
**Status:** 📝 spec (pending human spec gate) — the root cause is already identified
(see §1); this spec turns the known debt into a bounded fix.
**Scope:** `buildwright` — `loop.py` episode finalize staging, the 2 failing tests,
possibly 1 fixture. No changes to debrief, no protocol change.

---

## 1. Known cause (documented, not yet fixed)

From the `roles apply` spec of 2026-11-14, written while its verification ran into it:

> Known unrelated issue found during verification: the 2 pre-existing `test_loop.py`
> failures (loop.py `finalize` still stages in-repo `artifacts/` that `.gitignore`
> excludes since the central-tree migration) — left untouched, needs its own decision.

The migration to the central debrief tree moved run evidence out of the in-repo
`artifacts/`, and `.gitignore` now excludes that path. `loop.py`'s episode finalize
still **stages** (copies/stamps) episode output into the in-repo `artifacts/` and the
failing assertions still read it from there:

- `test_parallel_starts_both_features`
- `test_cli_queue_picks_pending_after_finalize`

Symptom on HEAD (`6cff652`): `ValueError: episode already finished` during the
`verify` task — the finalize path re-enters on an already-finalized episode when the
staged location disagrees with the migrated layout.

## 2. Fix direction (decision to confirm at the spec gate)

The migration's rule is "evidence lives in the central debrief tree"; the in-repo
`artifacts/` is a legacy write target that `.gitignore` makes invisible. The fix follows
the rule instead of re-blessing the legacy path:

1. `loop.py` finalize stages episode output to the **central tree** (the same
   `DEBRIEF_*` resolution `debrief_log.py` already uses — one resolver, not two).
2. The staged path becomes part of the episode record (a field on the episode, not a
   convention), so tests and readers stop hard-coding `artifacts/`.
3. The 2 tests assert against the resolved central path via the resolver, not via
   relative `artifacts/` paths.
4. The `episode already finished` guard stays (it is correct — a second finalize must
   raise); what changes is that the first finalize can no longer half-fail into a
   re-entry.

## 3. Tasks (test-first)

- [ ] T1: repro as a unit test pinned to the CURRENT failure mode (documents the bug
      in red), then flip to the central-tree assertion as T2 lands.
      refs: §1.
- [ ] T2: single resolver for the staging root (reuse `debrief_log.py`'s), episode
      carries its staged path; finalize writes there; idempotency test (second
      finalize raises; the record and staged files unchanged).
      refs: §2.1-2.2.
- [ ] T3: the 2 failing tests green against the resolver path; full suite 107/107;
      grep for remaining `artifacts/` writes in `loop.py` (should be zero).
      refs: §2.3-2.4.

## 4. Out of scope

Langfuse/debrief readers that may still point at the in-repo path (debrief's
`run.inspect` reads run LOGS, which were already migrated); any change to the episode
state machine beyond the staging root.

## 5. Verification

pytest 107/107 offline; `ruff` + `mypy --strict` clean; the 2 tests' names unchanged
(they are the regression history); one real `bw run` smoke to confirm the staged
episode appears in the central tree where debrief expects it.
