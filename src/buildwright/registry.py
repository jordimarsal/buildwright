from __future__ import annotations

import os
from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel
from pydantic_ai.models import Model
from pydantic_ai.models.openai import OpenAIChatModel
from pydantic_ai.providers.openai import OpenAIProvider


class ModelSpec(BaseModel):
    """An entry of models.yaml: a model reachable via an OpenAI-compatible endpoint."""

    id: str
    label: str
    kind: Literal["local", "remote"]
    base_url: str
    model: str | None = None  # "wire" name; defaults to the id without the prefix
    api_key_env: str | None = None
    context_window: int | None = None


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
            api_key = os.environ.get(s.api_key_env, "")
            if not api_key:
                raise RuntimeError(
                    f"model '{s.id}' requires env var {s.api_key_env} (not set)"
                )
        return OpenAIChatModel(
            s.model or s.id.split("/", 1)[1],
            provider=OpenAIProvider(base_url=s.base_url, api_key=api_key),
        )
