from __future__ import annotations

from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel
from pydantic_ai.models import Model
from pydantic_ai.models.openai import OpenAIChatModel
from pydantic_ai.providers.openai import OpenAIProvider

from buildwright.config import resolve_env


class ModelSpec(BaseModel):
    """An entry of models.yaml: a model reachable via an OpenAI-compatible endpoint."""

    id: str
    label: str
    kind: Literal["local", "remote"]
    base_url: str
    model: str | None = None  # "wire" name; defaults to the id without the prefix
    api_key_env: str | None = None
    context_window: int | None = None
    input_price: float | None = None  # USD per million input tokens
    output_price: float | None = None  # USD per million output tokens

    def cost_usd(self, input_tokens: int, output_tokens: int) -> float | None:
        """Cost of one call, or None when the entry carries no prices."""
        if self.input_price is None and self.output_price is None:
            return None
        return input_tokens / 1e6 * (self.input_price or 0.0) + output_tokens / 1e6 * (
            self.output_price or 0.0
        )


class ModelRegistry:
    def __init__(self, specs: list[ModelSpec]) -> None:
        self._specs = {s.id: s for s in specs}

    @classmethod
    def load(cls, path: Path) -> ModelRegistry:
        data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        specs = [ModelSpec.model_validate(s) for s in data.get("models", [])]
        return cls(specs)

    def spec(self, model_id: str) -> ModelSpec:
        try:
            return self._specs[model_id]
        except KeyError:
            known = ", ".join(sorted(self._specs))
            raise KeyError(f"unknown model id '{model_id}'. Known ids: {known}") from None

    def list(self, kind: str | None = None) -> list[ModelSpec]:
        return [s for s in self._specs.values() if kind is None or s.kind == kind]

    def build(self, model_id: str) -> Model:
        """Build the PydanticAI model pointing at its endpoint. No network involved."""
        s = self.spec(model_id)
        api_key = "local"  # ollama/llama.cpp ignore it; the client requires non-empty
        if s.api_key_env:
            api_key = resolve_env(s.api_key_env)
            if not api_key:
                raise RuntimeError(
                    f"model '{s.id}' requires {s.api_key_env} "
                    "(not in the environment or .env)"
                )
        return OpenAIChatModel(
            s.model or s.id.split("/", 1)[1],
            provider=OpenAIProvider(base_url=s.base_url, api_key=api_key),
        )
