# buildwright

*The craftsman who builds per the spec* — one model per agent: mechanical steps
go to the **local** model (cheap, fast), important decisions to the **remote**
one (strong). The role→model mapping is configuration, not code — switching
models touches zero lines of pipeline.

## Install

```bash
uv tool install buildwright      # once published to PyPI
# or for development:
uv sync
```

## Division of roles (one tool, one job)

| Piece | Role | Where it lives |
|---|---|---|
| **LangGraph** | the ORDER of the steps (graph, state, retries) | `graph.py` |
| **PydanticAI** | EACH STEP (agent with structured output) | `agents.py` |
| **LiteLLM** | WHO serves the model (optional proxy, fallbacks) | `litellm/config.yaml` |
| **Langfuse** | WHETHER IT WORKED (traces, scores) | `observability.py` |

## The pipeline

```
START → plan (local)  →  execute (remote)  →  review (local)  →  END
        Plan struct.      final answer        Review struct.
```

Each node is a PydanticAI agent; the model is decided by the **role** in
`Settings` (`BW_ROLE_*`), resolved against the `models.yaml` registry.

## Quickstart

```bash
uv sync
cp .env.example .env          # adjust BW_ROLE_* and keys

uv run buildwright models     # list local and remote models
uv run buildwright roles      # role -> model mapping
uv run buildwright ask "write a snake game in python with pygame"
```

With no endpoints available, `ask` fails naming the exact model and the missing
env var.

## Local and remote models

The whole registry speaks **OpenAI-compatible** (`models.yaml`):

- **local/qwen3** → Ollama at `localhost:11434/v1`
- **local/llamacpp** → `llama-server` at `localhost:8080/v1` (your GGUF)
- **remote/gpt-4o-mini, gpt-4o** → OpenAI (key via `OPENAI_API_KEY`)
- **remote/claude-sonnet** → Anthropic's OpenAI-compat layer
- **remote/zai-glm-flash** → Z.ai / Zhipu GLM (key via `ZAI_API_KEY`) — small and
  cheap; a *coding plan* key needs the `/api/coding/paas/v4` base_url, otherwise
  inference fails with error 1113 while `/models` still lists fine
- **litellm/*** → via the LiteLLM proxy (see `docker-compose.yml`)

Registry resolution: `BW_MODELS_FILE` > project-local `./models.yaml` > the
`models.yaml` bundled with the package. Adding a model = adding 5 lines.
Reassigning one to an agent = changing a `BW_ROLE_*` in `.env`.

## Optional full stack (LiteLLM + Langfuse + Ollama)

With Docker, `docker compose up -d` brings up LiteLLM (routing/fallbacks),
Langfuse (traces) and Ollama. Langfuse keys show up in its admin UI; copy them
to `.env` and tracing turns itself on (without keys, the tracer is a no-op).

## Publishing to PyPI

Trusted publishing — no API tokens:

1. Create `github.com/jordimarsal/buildwright` and push `main`.
2. On pypi.org: *Add a pending publisher* → owner `jordimarsal`, repo
   `buildwright`, workflow `release.yml`, environment `pypi`.
3. `git tag v0.1.0 && git push --tags` → the workflow runs `uv build` and
   publishes.

## Harness runtime

In a project initialized with [harness](https://github.com/jordimarsal/harness):

```bash
buildwright harness status                          # board + planned routing (read-only)
buildwright harness run                             # the FULL feature loop (see below)
buildwright harness run --feature f3_dashboard      # specific feature
buildwright harness approve "lgtm"                  # resume a parked gate: approve
buildwright harness reject  "add tests for R1"      # resume a parked gate: reject
```

- `status` parses and schema-validates `harness/feature_list.json` (protocol v1)
  and shows the board plus which model each role would use.
- `run` is **the full feature loop** (phase 2) — the graph *is* the leader. One
  feature runs batch after batch inside an isolated **git worktree**
  (`.buildwright/`, locally excluded), each batch being the next pending slice of
  `tasks.md` (capped at 4, harness discipline):

  ```
  implement_batch → verify → init_gate → advance_batch → reviewer
                    → traceability_gate → audit_gate → completion_gate → finalize
  ```

  - **verify** re-reads `tasks.md` and diffs the worktree — a chat claim is not
    evidence. Nothing written = a **stall**: the same batch is re-dispatched with
    `Nothing was written: …`; two stalls in a row start a **fresh** agent
    session; a batch that makes no progress across 4 dispatches is **parked**.
  - **hard gates** run the scripts this protocol already has (`init.sh` must end
    `[OK]`, `check-traceability.py`, and `audit-security.sh` / `bench.sh` for
    standard/strict). A failing gate feeds its log back to the implementer and
    loops back; 3 consecutive failures park the feature.
  - the **reviewer** issues a structured `APPROVED` / `CHANGES_REQUESTED` verdict
    written to `harness/progress/review_<name>.md`; two rejections in a row park.

  All cycle counters (`max_batch_attempts`, `max_gate_cycles`,
  `max_review_rounds`) count **consecutive** failures and reset on any progress
  (a completed batch resets the batch/gate counters, an `APPROVED` verdict resets
  `review_rounds`). So a human `reject` at the completion gate that sends the
  feature back to the implementer can loop as many times as you ask it to — it
  never trips a cap that was counting an earlier, unrelated streak.
  - **completion_gate** is a human gate: the loop `interrupt()`s and parks, with
    state persisted in a sqlite checkpointer (`.buildwright/checkpoints.sqlite`)
    so it survives restarts. `approve` **merges to main**, writes
    `harness/specs/<name>/APPROVAL`, flips status to `done`, and appends the
    `retro:` line to `history.md`; `reject` turns the feature back to the
    implementer with your comments. On approval the feature's worktree is
    removed and its merged branch deleted — a re-run starts fresh off `main`.
- Command allowlist defaults are built in; extend or retime via an optional
  project-owned `harness/runtime.yaml` (also `max_batch_attempts`,
  `max_gate_cycles`, `max_review_rounds`):

```yaml
extra_allowed_commands:
  - terraform plan
command_timeout_s: 300
max_gate_cycles: 3
```

The unattended multi-phase design lives in
`docs/specs/2026-10-27-harness-runtime-design.md`.

## Tests

```bash
uv run pytest        # offline: pydantic-ai's TestModel, zero network
```

## Anti-mess rules

1. Everything speaks OpenAI protocol; PydanticAI never imports litellm.
2. One agent = one node = one job. No agents inside agents.
3. Tracing registered exactly once (`observability.py`); never scattered.
4. Offline evals in CI rule; Langfuse is production monitoring.
5. Each piece joins when it has a real trigger (fallbacks→LiteLLM,
   non-linear flow→LangGraph is already in).

## Changelog

Release history lives in [CHANGELOG.md](CHANGELOG.md).
