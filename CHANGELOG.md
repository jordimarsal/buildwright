# Changelog

All notable changes to this project are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added

- **Phase 4 — cost feedback.** `models.yaml` entries accept optional
  `input_price` / `output_price` (USD per million tokens). Every agent call
  (implementer, reviewer, spec-author, gauntlet) records its token usage and
  the feature accumulates real spend in the persisted state; `finalize`'s
  retro line reports it — `retro: 1 dispatches · 0 stalls · 0 restarts ·
  $0.0123`. Entries without prices report nothing (an unpriced model must not
  silently claim $0.00).
- **Cross-repo protocol regression.** harness-standard's real
  `check-traceability.py` is vendored under `tests/fixtures/` (provenance
  dated) and a test runs both gate passes (`--all`, `--feature`) with it
  against runtime-generated spec trios and impl tables — if either repo
  changes the contract, this fails. A second variant runs the live
  `harness-standard` checkout when present (skipped in CI).

## [0.5.0] - 2026-10-28

### Added

- **Phase 3 — the feature queue.** `harness run` no longer requires a
  pre-approved spec: a `pending` feature is routed through a new
  `spec_phase → spec_gate` pair of nodes — a planner-routed spec-author writes
  the trio (`requirements.md` EARS + traceability table, `design.md`,
  `tasks.md`), the runtime verifies it on disk (three files, ≥1 `R<n>`,
  ≥1 task) and flips the board to `spec_ready`; the human gate then approves
  (→ `in_progress`, the loop picks the first batch from the fresh `tasks.md`)
  or rejects with comments (→ the spec-author reworks; `max_spec_rounds`
  consecutive rejections park the feature, and a park resume goes back through
  `pending`). After `finalize` the queue picks the next `pending` feature —
  `--features N` budgets how many features one invocation processes, one
  `in_progress` at a time; a feature waiting at a human gate stops the queue
  (resume it with `approve`/`reject`). Board flips during the spec phase are
  committed to `main` immediately, keeping `finalize`'s clean-main gate honest.
  `spec_ready` features now appear as gate-resume candidates.
- **`depends_on` + `parallel: true`.** A pending feature is only runnable when
  every feature in its `depends_on` list is `done`. With `parallel: true` the
  queue starts up to `--features N` runnable features together, one thread and
  one worktree each; every main-repo mutation (board flips, finalize's merge)
  goes through a process-wide lock so concurrent `git add/commit/merge` on main
  can never race on git's `index.lock`. Human gates still stop their own
  feature only.
- **Gauntlet as a runtime node.** When the gauntlet module is installed
  (`harness/tools/gauntlet.sh` present), the loop inserts a G1–G4 pass between
  the audit and the completion gate: G1 runs the script mechanically, G2
  (adversarial) and G3 (maintainability) are a single reviewer-model pass with
  a structured verdict (`GauntletVerdict`), and G4 writes
  `harness/progress/gauntlet_<name>.md` with the protocol's four PASS lines.
  A reject feeds the findings back to the implementer and re-runs from G1
  (two consecutive rejects park). Without the module the node logs
  `gauntlet: not installed — skipped`. `gauntlet.sh` joins the default
  command allowlist.

## [0.4.3] - 2026-10-28

### Fixed

- **The traceability gate now also checks the feature being completed.**
  `run_trace_gate` only ran `check-traceability.py --all`, and `--all` gates
  the `done` features — the feature under completion is still `in_progress`
  (it flips to `done` at finalize), so its traceability table was never
  mechanically verified and relied solely on the reviewer. The gate now runs a
  second pass, `check-traceability.py --feature <name>`. Harness standards
  whose script predates the `--feature` flag degrade to the `--all` pass
  (the flag is probed in the script text, since unknown arguments exit 2).

### Changed

- **`finalize` cleans up after itself.** Approving the completion gate now also
  removes the feature worktree (`.buildwright/worktrees/<name>`) and deletes
  the merged `buildwright/<name>` branch; previously every completed feature
  left both behind. Best-effort: untracked leftovers (gate logs, `.venv`) are
  force-discarded, but a failed cleanup only logs — it never fails a run whose
  work is already merged. A later re-run of the feature starts from a fresh
  worktree off `main`.

## [0.4.2] - 2026-10-28

### Added

- `remote/zai-glm-flash` in the bundled registry — Z.ai / Zhipu GLM (key via
  `ZAI_API_KEY`), small and cheap. The entry documents the two endpoints: a
  *coding plan* key needs `…/api/coding/paas/v4`, otherwise inference fails
  with error 1113 ("insufficient balance") even though `/models` lists fine.
  A test now guards the `models.yaml` shipped inside the wheel.

## [0.4.1] - 2026-10-28

### Fixed

- A **reused feature worktree is now synced with `main`** before the next
  dispatch. `create_worktree()` returned an existing worktree as-is, so a
  branch created earlier stayed pinned to its original commit: specs, docs or
  source committed to main afterwards were invisible to the implementer, which
  then reported `blocked` against a tree missing its own spec. The worktree now
  fast-forwards when it has no commits of its own, merges when it does, and
  raises a clear error when git refuses (local changes in the way) instead of
  silently dispatching against a stale tree.
- Provider keys documented in `.env` (`OPENAI_API_KEY`, `ZAI_API_KEY`, …) are
  now actually read. `registry.build()` resolved them from `os.environ` only,
  while pydantic-settings loads `.env` for the `BW_*` settings alone — so a key
  written to `.env` failed with `requires env var … (not set)`. The process
  environment still wins; the project `.env` is the fallback, and the error
  message now says both sources.

## [0.4.0] - 2026-10-27

### Added

- `buildwright harness run` is now **the full feature loop** (phase 2): the
  implementer runs batch after batch with stall recovery, the protocol's own
  scripts become hard gates, the reviewer issues a structured verdict, and a
  human gate parks the run — resumed with `approve` / `reject`, surviving
  process restarts.
- `buildwright harness approve` / `buildwright harness reject` — resume a parked
  human gate. `approve` merges the feature branch to main, writes
  `harness/specs/<name>/APPROVAL`, flips status to `done`, and appends the
  machine-generated `retro:` line to `history.md`; `reject` turns the feature
  back to the implementer with your comments.
- On-disk stall detection via a **content-aware worktree fingerprint**
  (porcelain + diff + untracked-file hashes): nothing written = re-dispatch the
  same batch with `Nothing was written: …`; two stalls start a fresh agent
  session; a batch with no progress across 4 dispatches is parked.
- Hard gates as graph nodes: `init.sh` (must end `[OK]`), `check-traceability.py`,
  and `audit-security.sh` / `bench.sh` (standard/strict). A failing gate feeds
  its log back to the implementer; 3 consecutive failures park the feature.
- Structured reviewer verdict (`APPROVED` / `CHANGES_REQUESTED`) written to
  `harness/progress/review_<name>.md`; two rejections in a row park.
- Sqlite checkpointer (`.buildwright/checkpoints.sqlite`, thread_id = feature
  name) so human gates survive restarts.
- Project-owned loop caps in `harness/runtime.yaml` (D1): `max_batch_attempts`,
  `max_gate_cycles`, `max_review_rounds`.

## [0.3.0] - 2026-10-27

### Added

- `buildwright harness run` — **phase 1 of the harness runtime**: one supervised
  implementer batch executed in an isolated git worktree.
- Implementer agent with `read_file` / `write_file` / `edit_file` / `list_dir`
  tools and an **allowlisted** `run_command` (prefix matching, timeout, large
  outputs archived to `harness/logs/<feature>/`).
- Worktree isolation: `.buildwright/worktrees/<feature>` on branch
  `buildwright/<feature>`, kept out of `git status` via `.git/info/exclude`
  (the tracked `.gitignore` is never touched).
- On-disk verification after every batch: checkbox states re-read from
  `tasks.md` and `git status --porcelain` diffed — a chat claim is not evidence.
- Project-owned `harness/runtime.yaml` (hard source of truth, D1):
  `extra_allowed_commands` and `command_timeout_s` extend the safe built-ins.
- Batching discipline: the next pending slice of `tasks.md`, capped at 4 tasks
  (never 5+), with a confirmation prompt before anything runs.
- `run` never merges and never flips feature status: review with
  `git -C <worktree> diff`.

## [0.2.0] - 2026-10-27

### Added

- `buildwright harness status` — **phase 0 of the harness runtime** (read-only):
  detects the nearest harness-initialized project (walks up from the cwd),
  schema-validates `harness/feature_list.json` against Pydantic models that
  mirror the official protocol-v1 JSON schema, and renders the feature board
  plus the planned role→model routing (registry entries missing from
  `models.yaml` are flagged, not fatal).

## [0.1.0] - 2026-10-27

### Added

- Per-agent model routing: the role→model mapping (`BW_ROLE_*`, defaults
  `planner=local/qwen3`, `worker=remote/gpt-4o-mini`,
  `reviewer=local/qwen3`) is configuration, not code.
- `models.yaml` registry with bundled defaults; resolution order
  `BW_MODELS_FILE` > project `./models.yaml` > bundled.
- `buildwright ask` — the plan → execute → review pipeline (LangGraph graph,
  PydanticAI agents with structured outputs, one model per role).
- `buildwright models` and `buildwright roles` — inspect the registry and the
  planned routing.
- Optional Langfuse tracing (no keys → tracing disabled).
- PyPI packaging with trusted publishing, CI and release workflows.

[Unreleased]: https://github.com/jordimarsal/buildwright/compare/v0.4.2...HEAD
[0.4.2]: https://github.com/jordimarsal/buildwright/compare/v0.4.1...v0.4.2
[0.4.1]: https://github.com/jordimarsal/buildwright/compare/v0.4.0...v0.4.1
[0.4.0]: https://github.com/jordimarsal/buildwright/compare/v0.3.0...v0.4.0
[0.3.0]: https://github.com/jordimarsal/buildwright/compare/v0.2.0...v0.3.0
[0.2.0]: https://github.com/jordimarsal/buildwright/compare/v0.1.0...v0.2.0
[0.1.0]: https://github.com/jordimarsal/buildwright/releases/tag/v0.1.0
