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

## Harness runtime (phase 0)

In a project initialized with [harness-standard](https://github.com/jordimarsal/harness):

```bash
buildwright harness status          # feature board + planned role -> model routing
buildwright harness status ~/src/kafka-adapter-telemetry
```

Read-only: parses and schema-validates `harness/feature_list.json` (protocol v1),
shows the board and which model each role would use. Phases 1+ (unattended
implement→verify→review in a worktree) are designed in
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
