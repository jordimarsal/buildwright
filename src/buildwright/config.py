from __future__ import annotations

import os
from functools import lru_cache
from importlib import resources
from pathlib import Path

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict

ROLES = ("planner", "worker", "reviewer")


def parse_env_file(path: Path) -> dict[str, str]:
    """Minimal `.env` reader: `KEY=VALUE`, `#` comments, optional quotes."""
    values: dict[str, str] = {}
    if not path.is_file():
        return values
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, raw = line.partition("=")
        values[key.strip()] = raw.strip().strip("\"'")
    return values


def resolve_env(name: str) -> str:
    """Resolve a provider key: the process env wins, then the project `.env`.

    pydantic-settings only reads `.env` for the `BW_*` settings, so a provider
    key documented in `.env.example` (e.g. `OPENAI_API_KEY=`) would otherwise
    never reach the model client. Process env stays authoritative.
    """
    value = os.environ.get(name, "")
    return value or parse_env_file(Path(".env")).get(name, "")


def default_models_file() -> Path:
    """Registry resolution: project-local ./models.yaml > the bundled models.yaml."""
    local = Path("models.yaml")
    if local.is_file():
        return local
    return Path(str(resources.files("buildwright").joinpath("models.yaml")))


class Settings(BaseSettings):
    """Central configuration. The role->model mapping is the switchboard itself."""

    model_config = SettingsConfigDict(
        env_prefix="BW_",
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    models_file: Path = Field(default_factory=default_models_file)

    # role -> registry id (models.yaml)
    role_planner: str = "remote/zai-glm-flash"
    role_worker: str = "local/llamacpp"
    role_reviewer: str = "remote/zai-glm-flash"

    # Langfuse: no keys -> tracing disabled (NullTracer)
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
