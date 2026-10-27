from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Literal

from pydantic import BaseModel, Field
from pydantic_ai import Agent
from pydantic_ai.models import Model

from .config import ROLES, Settings
from .registry import ModelRegistry


class Plan(BaseModel):
    """Sortida estructurada del planner."""

    steps: list[str] = Field(min_length=1, max_length=5)
    needs_strong_model: bool = Field(
        default=False, description="true només si la feina demana el model remot potent"
    )


class Review(BaseModel):
    """Sortida estructurada del reviewer."""

    verdict: Literal["pass", "fail"]
    comments: str


ModelFactory = Callable[[str], Model]  # role -> Model (per tests: TestModel)


@dataclass
class buildwright:
    """Fàbrica d'agents: cada rol amb el model que li toca segons Settings."""

    settings: Settings
    registry: ModelRegistry | None = None
    model_factory: ModelFactory | None = None

    def model_for(self, role: str) -> Model:
        if role not in ROLES:
            raise KeyError(f"unknown role '{role}'. Known: {', '.join(ROLES)}")
        if self.model_factory is not None:
            return self.model_factory(role)
        assert self.registry is not None, "registry required without model_factory"
        return self.registry.build(self.settings.role_model(role))

    def planner(self) -> Agent[None, Plan]:
        return Agent(
            self.model_for("planner"),
            output_type=Plan,
            instructions=(
                "You are a task planner. Break the user task into 1-5 concrete steps. "
                "Set needs_strong_model=true only for hard reasoning or code-heavy tasks."
            ),
        )

    def worker(self) -> Agent[None, str]:
        return Agent(
            self.model_for("worker"),
            instructions="You are a meticulous engineer. Execute the given step. Be concise.",
        )

    def reviewer(self) -> Agent[None, Review]:
        return Agent(
            self.model_for("reviewer"),
            output_type=Review,
            instructions=(
                "You are a strict reviewer. Judge the work against the task; "
                "reply verdict pass or fail with short comments."
            ),
        )
