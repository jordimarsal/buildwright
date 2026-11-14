# Design — `roles apply`: the human gate that closes the debrief loop

**Date:** 2026-11-14
**Status:** ✅ **implemented** (Completion Gate approved 2026-11-14) — commits
`d7946e8` (spec) · `1e3c951` (T1-T3: parser, validate, envfile) · `30f2205`
(T4-T5: CLI, docs). Gates: pytest 31/31 (feature), ruff clean, mypy --strict
clean, e2e demo verified (`roles apply - --yes` → `.env` written → `roles`
shows the new mapping). Known unrelated issue found during verification: the
2 pre-existing `test_loop.py` failures (loop.py `finalize` still stages
in-repo `artifacts/` that `.gitignore` excludes since the central-tree
migration) — left untouched, needs its own decision.
**Scope:** `buildwright` — one new module (`roles_apply.py`), two CLI subcommands
(`roles apply`, `roles set`), tests, docs. **No changes in `debrief-mcp`**: its
`routing.propose` output *is* the input contract.

---

## 1. Context

The evidence loop today:

```
ggufbench  measures  ──►  catalog            (artifacts/ggufbench/, via debrief_export.py)
buildwright runs     ──►  episode + roles    (run log, via debrief_log.py)
debrief reads both   ──►  proposal as TEXT   (routing.propose: a BW_ROLE_* diff, ADR-0003)
        │
        └── "not applied: debrief never writes; paste the + line
             into the buildwright role env" (its ADR-0001)
```

The loop ends at a **manual copy-paste into `.env`**. That paste is unvalidated
(typos reach the first run and fail there), unconfirmed, and silent about the
env-vs-`.env` precedence trap. The missing piece is an **applier** — and by
debrief's ADR-0001/0003 it cannot live in debrief. It belongs here: buildwright
owns the `BW_ROLE_*` semantics (pydantic `Settings`, `env_prefix="BW_"`),
the registry (`ModelRegistry.spec` refuses unknown ids by name), and the
established pattern of **human gates as CLI commands** (`harness approve /
reject / fix`).

## 2. Core insight — the paste is the contract, the gate is the CLI

- debrief's rendered proposal is already a machine-parseable format: exactly one
  kind of line means "apply this" — the `+` line. Nothing new is invented; the
  applier consumes **debrief's output verbatim**.
- The applier is a **gate**, not an automation: it shows the human what will
  change and *why* (the `why run` / `why bench` evidence), and writes only on
  explicit confirmation. Same philosophy as `harness approve`, one level lower:
  gate decisions about *features* exist; this is the gate for *routing*.
- No package dependency in either direction: debrief does not import
  buildwright, buildwright does not import debrief. The contract is pinned by a
  **shared fixture** — the same discipline `debrief_log.py` already uses
  (its regexes mirror debrief's reader; divergence fails buildwright's tests
  before it fails debrief's).

## 3. CLI surface

```bash
# consume debrief's proposal (file, stdin, or a pasted argument):
buildwright roles apply proposals.txt --yes
debrief ... | buildwright roles apply -
buildwright roles apply                       # reads stdin when no path given

# direct form, no proposal needed:
buildwright roles set worker=remote/zai-glm-flash
buildwright roles set BW_ROLE_WORKER=local/llamacpp   # prefix tolerated
```

Output on success (one block per change, then the write result):

```
change 1/1  role=worker
  local/llamacpp → remote/zai-glm-flash
  why run:  local/llamacpp failed code (2/8 tasks) — artifacts/…/exec….log
  why bench: remote/zai-glm-flash passed code at score 0.90 — artifacts/ggufbench/…
.env: 1 line updated (comments preserved)
```

Exit codes: `0` applied / no-op / declined; `2` refusal (bad input, unknown
role or model, ambiguous proposal). A decline is a human decision, not an
error (D4).

## 4. The parser contract

Input: debrief's full `routing.propose` text — possibly several proposal blocks
in one paste, possibly `note:` lines.

- **Only** lines matching `^\+\s*BW_ROLE_([A-Za-z0-9_]+)=(\S+)\s*$` are
  candidates. Explicitly ignored: `-` lines, `proposal:`, `why run:` /
  `why bench:` (captured for display only), `signature:`, `note:`,
  `not applied:`.
- Role canonicalization mirrors debrief's `_canonical_role`: strip a leading
  `BW_ROLE_` (case-insensitive), uppercase for the env name, lowercase to match
  `ROLES = ("planner", "worker", "reviewer")`.
- **Duplicates**: same role proposed twice with the same value → dedupe; same
  role with **different** values → refuse the whole apply (ambiguous proposal;
  never silently pick first or last).
- Zero candidates → refusal listing what was seen (protects against pasting
  the wrong buffer).
- A `- BW_ROLE_X=old` line is **never** a candidate — the parser cannot be
  used to un-apply.

## 5. Validation (offline only)

1. Role ∈ `ROLES`, else refuse naming the three known roles.
2. Model id exists in the registry (`ModelRegistry.load(settings.models_file)`
   → `.spec(id)`), with the standard resolution
   (`BW_MODELS_FILE` > `./models.yaml` > bundled). Refusal names the known ids
   and adds the hint: *"add it to models.yaml first (5 lines)"*. **The applier
   never writes `models.yaml`** (same spirit as debrief's ADR-0003).
3. **No-op**: target equals the current value (`settings.role_model(role)`) →
   `role worker already holds local/llamacpp`, exit 0, nothing written.
4. **Env precedence**: pydantic-settings makes the process env win over
   `.env`. If `BW_ROLE_<ROLE>` is exported in the environment, the applier
   still writes `.env` but warns: *"\.env updated, but BW_ROLE_WORKER is set in
   your environment and wins — unset it"* (D5).
5. **Future D1 guard**: if a project's `harness/runtime.yaml` ever carries a
   `roles:` key (anticipated in the runtime design, §11-D1, not implemented
   today — verified), `.env` would not be the effective source for that
   project → refuse with a pointer to that file. Cheap guard now, honest error
   later.

## 6. Write semantics

- Target: `./.env` relative to cwd (`Settings.env_file`). Missing file →
  created with a one-line header comment (`# buildwright role routing —
  managed by roles apply`), because the mapping has to live somewhere and this
  is the documented place (README quickstart: `cp .env.example .env`).
- **Line-preserving rewrite**: every existing line is kept byte-for-byte —
  comments, order, quoting — except the target key line, replaced in place.
  Absent keys are appended at the end. No dict round-trip.
- **Atomic**: write `.env.tmp`, then `os.replace` — a crash mid-write cannot
  leave a half-`.env`.
- Traceability needs no new store: every run log `debrief_log.py` emits already
  carries the `BW_ROLE_*=…` header, so the effective routing of each episode is
  self-documenting. (`.env` is gitignored — the durable record is the evidence
  tree, not the config file.)

## 7. What it does NOT do (scope out)

- No write to `models.yaml`, ever — adding a model stays manual (5 lines).
- No live endpoint ping: validation is registry-level, offline. A wrong
  endpoint fails loudly at first use, naming the model and the missing env
  var (existing buildwright behavior).
- No MCP tool, no daemon, no scheduler. One command, invoked by a human.
- No changes to debrief-mcp — its text output is frozen as the contract.
- No "un-apply"/rollback command: routing history lives in the run logs; going
  back is `roles set` with the old value.

## 8. Requirements (EARS)

- WHEN the input text contains one or more `+ BW_ROLE_<ROLE>=<model>` lines THE SYSTEM SHALL parse each as a candidate, canonicalize the role, and ignore every other line kind.
- WHEN candidates for the same role disagree on the target model THE SYSTEM SHALL refuse the whole apply (exit 2) and SHALL NOT write anything.
- WHEN the input yields zero candidates THE SYSTEM SHALL refuse (exit 2) showing the line kinds it saw.
- WHEN a candidate's role is not in `ROLES` THE SYSTEM SHALL refuse naming the known roles.
- WHEN a candidate's model id is not in the registry THE SYSTEM SHALL refuse naming the known ids and the models.yaml hint.
- WHEN the target model equals the role's current model THE SYSTEM SHALL report a no-op (exit 0) and SHALL NOT rewrite `.env`.
- WHEN `BW_ROLE_<ROLE>` is present in the process environment THE SYSTEM SHALL write `.env` and warn that the environment wins until the variable is unset.
- WHEN `harness/runtime.yaml` contains a `roles:` key THE SYSTEM SHALL refuse, pointing at that file as the effective source.
- WHEN the user confirms (or `--yes` is given) THE SYSTEM SHALL rewrite `.env` atomically, preserving all other lines byte-for-byte, and print one summary block per change.
- WHEN the file `.env` does not exist THE SYSTEM SHALL create it with a header comment and the role line.
- WHEN the user declines the confirmation THE SYSTEM SHALL exit 0 with "nothing written (declined)".
- THE SYSTEM SHALL NOT modify `models.yaml`, `harness/runtime.yaml`, or any file other than `./.env`.
- THE `roles set` subcommand SHALL accept `<role>=<model>` with an optional `BW_ROLE_` prefix and SHALL follow the same validation, confirmation and write path.

## 9. Risks & honest limits

1. **Contract drift debrief ↔ buildwright.** Mitigated the way
   `debrief_log.py` does it: a fixture holding debrief's rendered proposal is
   pinned in both repos' test suites; a format change turns a test red before
   any user sees it.
2. **`.env` is gitignored** — an applied change is machine-local. The durable
   history is the run-log headers (already shipped). Putting routing in git
   would be a different, deliberate decision (out of scope).
3. **Env-vs-file confusion.** Documented shells that export `BW_ROLE_*` make
   `.env` edits ineffective; the applier warns (D5) but cannot force the
   environment.
4. **Proposals only exist for failures.** `routing.propose` says
   *"no proposal: every logged step passed"* — the applier is inert without
   fresh evidence, by design.
5. **Offline validation** cannot catch a dead endpoint or a missing provider
   key; the first real call surfaces both, with buildwright's existing
   error style.

## 10. Phases / tasks (one slice, test-first)

| Task | Deliverable | Tests |
|---|---|---|
| **T1** | `roles_apply/parser.py`: `parse_proposals(text) -> list[Candidate]` + refusal reasons. Fixture `tests/fixtures/debrief_proposal.txt` (debrief's exact render, real ids). | candidate extraction, line-kind ignores, `-` never parsed, dedupe vs conflict, empty-input refusal |
| **T2** | `roles_apply/validate.py`: role check, registry check, no-op detection, env-precedence detection, runtime.yaml guard — pure functions, registry/settings injected | each refusal message; no-op; precedence flag; guard triggers only on `roles:` key |
| **T3** | `roles_apply/envfile.py`: line-preserving atomic writer, create-with-header | replace in place preserving bytes; append; missing file; failure leaves no partial state |
| **T4** | CLI: `roles apply [path|--stdin] [--yes]`, `roles set <role>=<model> [--yes]`; confirm prompt rendering `why run`/`why bench`; exit codes | e2e via `cli.main` in tmp cwd: apply → `.env` updated → `roles` shows new mapping; decline → exit 0; refusals → exit 2 |
| **T5** | README (Quickstart + a "Close the loop" section), CHANGELOG entry | docs build, example commands run in tests where cheap |

Demoable end-to-end at T4: `debrief propose | buildwright roles apply` →
`buildwright roles` shows the new mapping.

## 11. Decisions (2026-11-14)

- **D1 — Home: buildwright CLI.** Not debrief MCP (ADR-0001 *never writes*;
  ADR-0003 *roles, not registry*), not a standalone script (env precedence and
  registry semantics live in buildwright), not an agent tool (the human gate is
  the point).
- **D2 — Contract: debrief's text, verbatim.** No new interchange format, no
  cross-package import; the shared fixture is the pin.
- **D3 — Write target: `./.env` only.** `models.yaml` untouched;
  `harness/runtime.yaml` with role overrides (future) refuses with a pointer.
- **D4 — Decline is exit 0.** A human decision, not an error; refusals are
  exit 2.
- **D5 — Precedence conflict: warn and write.** The `.env` becomes correct as
  soon as the export is unset; refusing would block the common "fix the file,
  I'll deal with the shell" flow. The warning names the exact variable.
