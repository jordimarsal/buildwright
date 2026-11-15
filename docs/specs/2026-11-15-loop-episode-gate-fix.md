# Design — fix the `test_loop.py` failures: episode finalize vs the central-tree migration

**Date:** 2026-11-15
**Status:** ✅ spec gate approved 2026-11-15 — cause re-verified empirically (§1);
the original cause in this document was wrong and has been corrected.
**Scope:** `buildwright` — `loop.py` evidence staging, `debrief_log.py` exec-id
uniqueness, one test fixture. No changes to the debrief protocol.

---

## 1. Known cause (verified empirically, corrected)

The first version of this spec claimed the finalize path staged into an in-repo
`artifacts/` that `.gitignore` excludes, and that the failing tests read from there.
**Both halves were wrong** — no test reads `artifacts/`, and the blocking ignore rule
is not the repo's. The failures were reproduced and isolated by toggling one
environment variable at a time:

| # | Real cause | Evidence | Effect |
|---|---|---|---|
| **A** | `_commit_debrief_evidence` runs `git add` on an evidence file **git already ignores** → git refuses → `FinalizeError: stage debrief evidence failed` | `git check-ignore -v` → `~/.gitignore_global:179:*.log` (the fixture repo has no `.gitignore`; buildwright's own ignores `artifacts/` by design) | **2 constant failures**: `test_cli_run_parks_and_gate_approve_finalizes`, `test_cli_queue_picks_pending_after_finalize` |
| **B** | `BW_DEBRIEF_ROOT` inherited from the shell makes every test share **one** central-tree file `…/artifacts/demo/exec<minute>/<feature>.log`. `find_open_episode` returns `None` once the log holds `result:`, so a fresh sink is opened on the **same path**, `_resume()` sees `result:` and sets `_finished=True`, and the next `step()` raises | debug plugin: `step FAILED path=…/demo/exec20261115T1623Z/f2_hub.log finished=True` → `episode already finished` | **2-3 flaky failures**: `test_parallel_starts_both_features` and the two above |
| **B2** | `exec_id` is stamped to the **minute**, so two episodes for one feature can resolve to the same path and the second one inherits the first one's closed state | same trace as B; `DebriefSink.__init__` resumes whenever the path exists | makes B reachable in production too, not only under a polluted env |

Isolation matrix (3 runs each):

```
BW_DEBRIEF_ROOT unset + global gitignore active  →  2 failed   (A)
BW_DEBRIEF_ROOT set   + GIT_CONFIG_GLOBAL=/dev/null → 3 failed  (B)
both clean (env -u BW_DEBRIEF_ROOT GIT_CONFIG_GLOBAL=/dev/null) → 31 passed ×3 ✅
```

Consequence: on CI (ubuntu, no `~/.gitignore_global`) these tests already pass —
the debt is local, and **B is test pollution**: the suite inherits the developer's
debrief root and writes into the real evidence tree.

## 2. Fix direction (approved at the spec gate)

The rule the migration established is "evidence lives in the central debrief tree";
in-repo `artifacts/` is ignored **on purpose**. Following that rule means never
attempting to stage what git ignores, plus making the suite independent of the
developer's environment:

1. **A** — `_commit_debrief_evidence`: when the evidence path is *ignored* (repo
   `.gitignore`, global gitignore, or outside the worktree) it is a **no-op**, exactly
   like central mode. The dirty-main problem it exists to prevent cannot happen: an
   ignored file never shows up in `git status`.
2. **B** — a `tests/conftest.py` fixture (autouse) clears `BW_DEBRIEF_ROOT` /
   `DEBRIEF_HOME` for the whole suite, so tests resolve to their own `tmp_path`
   regardless of who launches them.
3. **B2** — `exec_id` gains uniqueness (seconds, or a collision suffix) so a second
   episode for one feature can never land on a path that already holds a closed log.
4. The `episode already finished` guard **stays** (it is correct — a second `step()`
   after `finish()` must raise). What changes is that no path collision can put a
   fresh sink on top of a closed episode.

## 3. Tasks (test-first)

- [x] T1: repro pinned to the real failure modes, environment-independent: (a) an
      ignored evidence path must not raise in `_commit_debrief_evidence`, (b) a second
      episode for the same feature in the same minute must not inherit `_finished`.
      refs: §1.A, §1.B2.
- [x] T2: the three fixes — no-op on ignored path, conftest env scrub, unique
      `exec_id`; idempotency kept (a second `finish()` is a no-op; a `step()` after
      it still raises). refs: §2.1-2.3.
- [x] T3: the named tests green in **both** environments (with and without
      `BW_DEBRIEF_ROOT`, with and without the global gitignore), full suite green,
      `mypy src` clean. refs: §2.4.

## 4. Out of scope

Langfuse/debrief readers that may still point at the in-repo path (debrief's
`run.inspect` reads run LOGS, which were already migrated); any change to the episode
state machine beyond the staging root; the global-gitignore rule itself (`*.log`) —
the tool must tolerate it, not rewrite the user's git config.

## 5. Verification

pytest green **twice**: once in the current shell (with `BW_DEBRIEF_ROOT` and the
global gitignore active) and once with
`env -u BW_DEBRIEF_ROOT GIT_CONFIG_GLOBAL=/dev/null` — both isolation rows of §1 must
read `passed`. `mypy src` clean; the named tests keep their names (they are the
regression history); no `git add` of an ignored path in any run.

## 6. Completion gate

✅ Approved 2026-11-15 (commit `f001a55`, T1-T3; 131/131 in both isolation rows
of §5 — with and without `BW_DEBRIEF_ROOT` and the global gitignore — `mypy src`
clean, original test names kept, debrief contamination removed).
