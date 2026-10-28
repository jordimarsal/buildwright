# Changelog

All notable changes to this project are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

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

[Unreleased]: https://github.com/jordimarsal/buildwright/compare/v0.3.0...HEAD
[0.3.0]: https://github.com/jordimarsal/buildwright/compare/v0.2.0...v0.3.0
[0.2.0]: https://github.com/jordimarsal/buildwright/compare/v0.1.0...v0.2.0
[0.1.0]: https://github.com/jordimarsal/buildwright/releases/tag/v0.1.0
