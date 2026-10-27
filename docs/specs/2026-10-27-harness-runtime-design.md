# Design — buildwright as a Harness runtime

**Date:** 2026-10-27
**Status:** draft (pre-implementation)
**Scope:** `buildwright` — new `harness/` package. No changes required in `harness-standard` to start.

---

## 1. Context

`harness-standard` today is a **protocol** (agents, SDD workflow, gates, batching,
verification) executed *inside* a coding-agent TUI (OpenCode / Claude Code). The
protocol is enforced by prose ("NEVER skip the spec phase") plus scripts
(`init.sh`, `check-traceability.py`, `audit-security.sh`). The leader is an LLM
reading a decision table. Discipline exists because LLMs drift.

`buildwright` today is a per-agent model router: LangGraph orchestration,
PydanticAI agents with structured outputs, local/remote model registry, Langfuse.

**Idea:** buildwright becomes a third *host* of the harness protocol — the
unattended one. OpenCode is the interactive host (human drives). buildwright is
the production-line host (protocol drives, human approves at gates). Same
artifacts, same status machine, same gates.

Notably, the harness protocol already anticipates this host. `leader.md` says:
*"dispatch one read-only subagent … (**cheap model if the runtime allows
per-dispatch model choice**)"*. buildwright is the runtime that allows it.

## 2. Core insight — the graph IS the leader

The leader's decision table is a state machine; LangGraph is a state machine.
The LLM leader disappears; the graph assumes its table:

| Leader mandate (prose today) | buildwright runtime (structural) |
|---|---|
| "NEVER launch implementer when a feature is `pending`" | no `pending → in_progress` edge exists |
| Human approval gate at `spec_ready` | LangGraph `interrupt()` — the process parks; state persisted in a checkpointer |
| "One feature at a time" | state guard: only one `in_progress` slot in the graph state |
| Batching 2–4 tasks, never 5+ | deterministic batch builder slices `tasks.md`; the number 5 is unrepresentable |
| "Verify on disk after every batch" | code: stat files, parse `- [x]`, run gates — no chat claim is even possible |
| Stall recovery ("Nothing was written…", 2 stalls → fresh session) | stall counter in state → re-dispatch with exact message → new agent instance |
| Context ~70% → fresh session | token counter per agent instance → rotate instance |
| Anti-telephone-cord (results to files) | structured outputs are written to disk by the runtime; there is no chat |
| Effort scaling table | complexity = cheap classifier node (structured output), overridable at the spec gate |

What stays LLM-driven is exactly what needs judgment: **spec-author**
(writes specs), **implementer** (writes code), **reviewer** (judges semantics),
**gauntlet passes** (adversarial checklists), **log-reader**. Everything
mechanical becomes edges and validators that cannot forget, skip, or improvise.

## 3. Constraint enforcement map

Harness constraints, soft→hard migration:

| Constraint | TUI era | buildwright |
|---|---|---|
| SDD workflow order | prose | graph topology (**hard**) |
| `harness/init.sh` green before done | implementer told to run it | subprocess gate; exit ≠ 0 or final line ≠ `[OK]` → loop back (**hard**) |
| Feature list schema | `validate-feature-list.py` | Pydantic model at load & write (**hard**) |
| Traceability R⟷test | reviewer + script | run `check-traceability.py` as gate node; reviewer semantic pass on top (**hard + soft**) |
| `audit_level` basic/standard/strict | reviewer reads config | code reads config, runs `audit-security.sh` / `bench.sh`, rejects on non-zero (**hard**) |
| Conventional Commits | `commit-msg` hook | same hook; runtime commits via git tool (**hard**, unchanged) |
| Architecture & conventions docs | instructions + reviewer | injected as instructions from `docs/*.md` + reviewer checklist (**soft**, as today) |
| Approbation record (`APPROVAL`) | implementer writes it after human msg | runtime writes it only after completion-gate `approve` (**hard**) |

Rule of thumb: **if harness already has a script for it, it becomes a gate
node; if it only has prose, it becomes instructions + a structured-output
checklist for the reviewer.**

## 4. Role → model routing (the original buildwright value)

```
[roles.harness]
spec-author   = remote/claude-sonnet    # highest-leverage text in the pipeline
implementer   = remote/gpt-4o           # or big local coder model
reviewer      = remote/gpt-4o-mini      # semantic pass, medium model
gauntlet-g*   = local/qwen3             # mechanical checklists, high volume
log-reader    = local/qwen3             # as leader.md explicitly anticipates
```

Langfuse gives the missing feedback loop: per feature — cost per role, cycles,
stalls, review verdicts. The `retro: <n> dispatches · <s> stalls · <r> restarts`
line becomes machine-generated, plus `· $<cost> · <model per role>`.

`harness/runtime.yaml` is per-project, so **language routing needs no extra
mechanism**: the Java project pins `implementer: remote/claude-sonnet`, the
Python project pins a python-strong model — each in its own file, which is the
hard source of truth. buildwright `BW_ROLE_*` stay as global defaults; the
project file always wins.

## 5. Graph sketch

```
load_project → select_feature(pending) → classify_complexity(cheap)
    → spec_author → spec_gate ⏈ interrupt(approve/reject)
    → [approved] mark in_progress
    → implement_batch(next batch from tasks.md, with tools, in worktree)
    → verify_on_disk  ── fail ──→ implement_batch (stall/cycle counters)
    → reviewer        ── CHANGES_REQUESTED (≤2) ──→ implement_batch
    → traceability_gate (check-traceability.py) ── fail → implement_batch
    → audits (audit_level) ── fail → implement_batch
    → completion_gate ⏈ interrupt(approve/reject)
    → finalize: APPROVAL record, status done, history.md, retro line
```

Tools per agent mirror the frontmatter the templates already declare
(`reviewer.md` / `leader.md`: `tools: write: false`) — **the runtime parses that
frontmatter and wires tool availability accordingly.** The templates become config.

- `implementer`: `read_file`, `write_file`, `edit_file`, `run_command` (allowlist)
- `reviewer`, `gauntlet`, `log-reader`, `spec-author`: read-only + `run_command`
  (gates/scripts only)
- `run_command`: cwd pinned to the feature worktree, command allowlist read from
  **`harness/runtime.yaml`** — project-owned, hard source of truth; safe built-in
  defaults when absent — plus timeouts and output > ~20 lines → archived to
  `harness/logs/<feature>/T<n>.log`, returns path + tail (the existing
  big-outputs protocol, now enforced by the tool itself)

Isolation: one **git worktree per feature**; the completion gate decides
merge-or-discard. Mirrors OpenCode's own worktree strategy.

## 6. Artifacts are the interface

buildwright reads and writes **exactly** the harness artifacts — no fork:

- `harness/feature_list.json` (schema-validated), `harness/specs/<name>/{requirements,design,tasks}.md`
- `harness/progress/{current,history,impl_<name>,review_<name>}.md`, `CHECKPOINTS.md`, `APPROVAL`, `harness/logs/`

Consequences: a feature can be specced interactively in OpenCode and implemented
overnight by buildwright; or specced by buildwright, approved by a human, and
implemented in OpenCode. `harness-standard` stays runtime-agnostic — buildwright
is just a second, stricter interpreter of the same protocol.

Source of truth for specs remains **markdown** (`tasks.md` checkboxes). The
runtime parses it (reusing `check-traceability.py` conventions). A machine
readable `tasks.json` mirror is a possible future harness evolution — explicitly
*not* a requirement, to avoid forking the protocol.

## 7. CLI surface

```
buildwright harness status                 # board + role→model plan (read-only)
buildwright harness run [--feature F3] [--max-batches N]
buildwright harness approve [comments]     # resumes the parked graph (checkpointer)
buildwright harness reject "comments"      # back to spec_author / implement_batch
```

LangGraph sqlite checkpointer ⇒ gates survive process restarts; `run` is
resumable, like the harness "Interrupted session" case in the leader table.

## 8. Requirements (EARS)

- WHEN a feature is `pending` THE SYSTEM SHALL route it to `spec_author` and SHALL NOT grant write tools to any agent before the spec gate approves.
- WHEN `harness/init.sh` exits non-zero or its last line is not `[OK]` THE SYSTEM SHALL return control to `implement_batch` with the log path, up to a configured cycle cap.
- WHEN a batch produces no file changes THE SYSTEM SHALL count a stall, re-dispatch with `Nothing was written: <paths>…`, and after 2 stalls start a fresh agent instance.
- WHEN the reviewer issues `CHANGES_REQUESTED` twice THE SYSTEM SHALL park the feature `blocked` at a human gate.
- WHEN a human gate is reached THE SYSTEM SHALL persist graph state and wait; `approve`/`reject` resumes it.
- THE SYSTEM SHALL NOT write status `done` unless an `APPROVAL` record exists, written after an explicit completion-gate approval.
- WHEN `audit_level` is `standard`/`strict` THE SYSTEM SHALL run the corresponding scripts and reject on non-zero exit.
- WHEN `"parallel": true` THE SYSTEM MAY run independent features concurrently, maintaining exactly one `in_progress` per worktree and honoring `depends_on`.

## 9. Risks & honest limits

1. **Agency = blast radius.** File writes + shell on a real repo. → worktree isolation, allowlisted commands, gates decide merge-or-discard, human completion gate before touching `main`.
2. **Not for ambiguous work.** Taste, UX, exploratory refactors stay in the TUI with a human driving. buildwright executes *specified* work; the human gates exist to catch wrong specs.
3. **Cycle cost.** implement→verify→review loops burn tokens. → cheap/local models on high-frequency nodes, cycle caps, Langfuse cost telemetry per feature.
4. **Spec parsing fragility.** Markdown conventions are parseable but not contractual (mitigated by reusing `check-traceability.py` logic and by schema-validating everything that *is* JSON).
5. **Leader judgment gaps.** Complexity classification and slicing are approximated by a cheap classifier node; the spec gate is the human override point.

## 10. Phases

| Phase | Deliverable | Risk |
|---|---|---|
| **0** | `buildwright harness status` — detect harness project, schema-validate `feature_list.json`, show board + planned role→model mapping. Read-only. | none |
| **1** | Single batch, supervised: implementer with tools in a worktree → `verify_on_disk` → diff report. Human reviews every step. | low |
| **2** | Full feature loop: batching queue, stall detection, reviewer + audits + traceability gate, human gates via checkpointer, APPROVAL/history/retro written by runtime. | medium |
| **3** | Multi-feature & parallel mode (`depends_on`), gauntlet as cheap-local nodes, wekan mirror if configured. | medium |
| **4** | Feedback: cost/quality per role-model from Langfuse, golden fixtures (harness `evals-fixtures/`) as regression tests for the runtime itself, routing tuned by measured pass-rate per node. | low |

## 11. Decisions (2026-10-27)

- **D1 — Allowlist config: `harness/runtime.yaml`.** Accepted as the project-level
  hard source of truth: command allowlist, timeouts, and role→model overrides.
  buildwright `BW_ROLE_*` remain global defaults; the project file always wins.
- **D2 — Per-language implementer routing.** No dedicated mechanism needed: the
  *target* project declares its stack-appropriate implementer model in its own
  `runtime.yaml` (Java project → java-strong model, Python project → python-strong).
  Revisit only if Langfuse telemetry shows language-specific failure patterns.
- **D3 — Packaging: standalone installable tool.** Not a harness module (hosts
  install once globally, like OpenCode itself — heavy deps don't belong in every
  target repo) and not primarily a compose stack. A `docker-compose` stays as an
  optional deployment mode for unattended/overnight runs; `harness-standard`
  gets a docs pointer only, so the contract remains artifacts-only.
  Name (decided 2026-10-27): **buildwright** — "the craftsman who builds per the
  spec" (PyPI free, no notable GitHub project). License: MIT, matching
  harness-standard. Hosted on GitHub (portfolio, Pages, trusted publishing).
