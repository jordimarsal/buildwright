# buildwright

*The craftsman who builds per the spec* — un model per agent: els passos mecànics
van al model **local** (barat, ràpid), les decisions importants al **remot**
(potent). El mapping rol→model és configuració, no codi — canviar de model no
toca ni una línia de pipeline.

## Install

```bash
uv tool install buildwright      # quan sigui publicat a PyPI
# o en dev:
uv sync
```


## Repartiment de rols (una eina, una feina)

| Peça | Rol | On viu |
|---|---|---|
| **LangGraph** | l'ORDRE dels passos (gra, estat, retries) | `graph.py` |
| **PydanticAI** | CADA PASSA (agent amb sortida estructurada) | `agents.py` |
| **LiteLLM** | QUI serveix el model (proxy opcional, fallbacks) | `litellm/config.yaml` |
| **Langfuse** | SI HA FUNCIONAT (traces, scores) | `observability.py` |

## El pipeline

```
START → plan (local)  →  execute (remot)  →  review (local)  →  END
        Plan struct.      resposta final      Review struct.
```

Cada node és un agent PydanticAI; el model el decideix el **rol** a `Settings`
(`BW_ROLE_*`), resolt contra el registry de `models.yaml`.

## Quickstart

```bash
uv sync
cp .env.example .env          # ajusta els BW_ROLE_* i claus

uv run buildwright models     # llista locals i remots
uv run buildwright roles      # mapping rol -> model
uv run buildwright ask "escriu un snake en python amb pygame"
```

Sense endpoints disponibles, `ask` fallarà amb el model i env var concrets que falten.

## Models locals i remots

Tot el registry parla **OpenAI-compatible** (`models.yaml`):

- **local/qwen3** → Ollama a `localhost:11434/v1`
- **local/llamacpp** → `llama-server` a `localhost:8080/v1` (el teu GGUF)
- **remote/gpt-4o-mini, gpt-4o** → OpenAI (clau via `OPENAI_API_KEY`)
- **remote/claude-sonnet** → capa compat OpenAI d'Anthropic
- **litellm/*** → via proxy LiteLLM (veure `docker-compose.yml`)

Resolució del registry: `BW_MODELS_FILE` > `./models.yaml` del projecte > el
`models.yaml` inclòs al paquet. Afegir un model = afegir 5 línies. Reassignar-lo
a un agent = canviar un `BW_ROLE_*` a `.env`.

## Stack complet opcional (LiteLLM + Langfuse + Ollama)

Amb Docker, `docker compose up -d` aixeca LiteLLM (routing/fallbacks), Langfuse
(traces) i Ollama. Les claus de Langfuse surten a la web d'admin; copia-les a
`.env` i el tracing s'activa sol (sense claus, el tracer és un no-op).

## Publicar a PyPI

Trusted publishing — cap token d'API:

1. Crea `github.com/jordimarsal/buildwright` i fes push de `main`.
2. A pypi.org: *Add a pending publisher* → owner `jordimarsal`, repo
   `buildwright`, workflow `release.yml`, environment `pypi`.
3. `git tag v0.1.0 && git push --tags` → el workflow fa `uv build` i publica.

## Tests

```bash
uv run pytest        # offline: TestModel de pydantic-ai, cap xarxa
```

## Regles anti-nyap

1. Tot parla OpenAI-protocol; PydanticAI mai importa litellm.
2. Un agent = un node = una feina. Sense agents dins d'agents.
3. Tracing registrat un sol cop (`observability.py`); mai escampat.
4. Evals offline a CI manden; Langfuse és monitoratge de producció.
5. Cada peça entra quan té trigger real (fallbacks→LiteLLM, flux no lineal→ja hi és LangGraph).
