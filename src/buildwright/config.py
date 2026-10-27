from __future__ import annotations

from functools import lru_cache
from importlib import resources
from pathlib import Path

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict

ROLES = ("planner", "worker", "reviewer")


def default_models_file() -> Path:
    """Resolució del registry: ./models.yaml del projecte > models.yaml del paquet."""
    local = Path("models.yaml")
    if local.is_file():
        return local
    return Path(str(resources.files("buildwright").joinpath("models.yaml")))


class Settings(BaseSettings):
    """Configuració central. El mapping rol->model és la 'buildwright' pròpiament dita."""

    model_config = SettingsConfigDict(
        env_prefix="BW_",
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    models_file: Path = Field(default_factory=default_models_file)

    # rol -> id del registry (models.yaml)
    role_planner: str = "local/qwen3"
    role_worker: str = "remote/gpt-4o-mini"
    role_reviewer: str = "local/qwen3"

    # Langfuse: sense claus -> tracing desactivat (NullTracer)
    langfuse_public_key: str | None = None
    langfuse_secret_key: str | None = None
    langfuse_host: str = "http://localhost:3000"

    def role_model(self, role: str) -> str:
        value = getattr(self, f"role_{role}", None)
        if value is None:
            raise KeyError(f"unknown role '{role}'. Known: {', '.join(ROLES)}")
        return value


@lru_cache
def get_settings() -> Settings:
    return Settings()
