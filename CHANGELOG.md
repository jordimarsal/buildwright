# Changelog

All notable changes to this project are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

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

[Unreleased]: https://github.com/jordimarsal/buildwright/compare/v0.4.0...HEAD
[0.4.0]: https://github.com/jordimarsal/buildwright/compare/v0.3.0...v0.4.0
[0.3.0]: https://github.com/jordimarsal/buildwright/compare/v0.2.0...v0.3.0
[0.2.0]: https://github.com/jordimarsal/buildwright/compare/v0.1.0...v0.2.0
[0.1.0]: https://github.com/jordimarsal/buildwright/releases/tag/v0.1.0
