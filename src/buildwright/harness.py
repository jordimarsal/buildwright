"""Read-only bridge to harness-initialized projects (phase 0 of the runtime).

Contract: the artifacts ARE the interface — this module only parses and
validates `harness/feature_list.json` (protocol v1) and renders the board
plus the planned role->model routing. It never writes.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from .config import ROLES, Settings
from .registry import ModelRegistry

STATUSES: tuple[Status, ...] = ("pending", "spec_ready", "in_progress", "blocked", "done")

Status = Literal["pending", "spec_ready", "in_progress", "blocked", "done"]


class Project(BaseModel):
    """`project` section of harness/feature_list.json (protocol v1)."""

    model_config = ConfigDict(extra="allow")

    name: str = Field(min_length=1)
    parallel: bool
    modules: list[str]
    audit_level: Literal["basic", "standard", "strict"]
    harness_version: str | None = None
    architecture: str | None = None
    workflow: Literal["full", "hybrid"] | None = None


class Feature(BaseModel):
    """One feature entry of harness/feature_list.json (protocol v1)."""

    model_config = ConfigDict(extra="allow")

    id: int
    name: str = Field(pattern=r"^[a-z0-9_]+$")
    title: str
    description: str
    acceptance: list[str]
    status: Status
    wekan_card: str | None = None
    part_of: str | None = None
    depends_on: list[str] = Field(default_factory=list)

    def runnable(self, done: set[str]) -> bool:
        """True when every dependency is `done` (a feature alone is runnable)."""
        return all(dep in done for dep in self.depends_on)


class FeatureList(BaseModel):
    """Validated harness/feature_list.json (protocol v1)."""

    model_config = ConfigDict(extra="forbid")

    project: Project
    features: list[Feature]


class HarnessProject:
    """A harness-initialized project on disk (read-only view)."""

    def __init__(self, root: Path, feature_list: FeatureList) -> None:
        self.root = root
        self.feature_list = feature_list

    @classmethod
    def detect(cls, start: Path | None = None) -> HarnessProject:
        """Find the nearest harness project walking up from `start` (default: cwd)."""
        current = (start or Path.cwd()).resolve()
        for candidate in (current, *current.parents):
            if (candidate / "harness" / "feature_list.json").is_file():
                return cls.load(candidate)
        raise FileNotFoundError(
            f"no harness project found at or above {current} "
            "(looking for harness/feature_list.json)"
        )

    @classmethod
    def load(cls, root: Path) -> HarnessProject:
        path = root / "harness" / "feature_list.json"
        data = json.loads(path.read_text(encoding="utf-8"))
        try:
            feature_list = FeatureList.model_validate(data)
        except ValidationError as exc:
            raise ValueError(f"invalid {path}: {exc}") from exc
        return cls(root=root, feature_list=feature_list)

    def by_status(self, status: Status) -> list[Feature]:
        return [f for f in self.feature_list.features if f.status == status]

    def counts(self) -> dict[str, int]:
        return {s: len(self.by_status(s)) for s in STATUSES}

    def next_pending(self) -> Feature | None:
        pending = self.by_status("pending")
        return pending[0] if pending else None


def format_status(
    project: HarnessProject, settings: Settings, registry: ModelRegistry
) -> str:
    """Render the board plus the planned role->model routing. Pure function."""
    fl = project.feature_list
    p = fl.project
    lines = [
        f"buildwright harness status — {p.name}",
        f"root: {project.root}",
        (
            f"workflow: {p.workflow or 'full'}   audit: {p.audit_level}   "
            f"parallel: {p.parallel}   modules: {', '.join(p.modules) or '-'}"
        ),
        "",
        "BOARD",
    ]
    for status in STATUSES:
        feats = project.by_status(status)  # type: ignore[arg-type]
        if not feats:
            continue
        lines.append(f"  {status:<12} ({len(feats)})")
        for f in feats:
            extra = f"  [part_of: {f.part_of}]" if f.part_of else ""
            lines.append(f"    {f.id:>3}  {f.name:<20} {f.title}{extra}")
    if not fl.features:
        lines.append("  (no features)")
    lines += ["", "ROLE -> MODEL (planned routing)"]
    for role in ROLES:
        model_id = settings.role_model(role)
        kind: str
        try:
            kind = registry.spec(model_id).kind
        except KeyError:
            kind = "MISSING-IN-REGISTRY"
        lines.append(f"  {role:<10} {model_id:<24} {kind}")
    return "\n".join(lines)
