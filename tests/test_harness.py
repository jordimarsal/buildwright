import json
from pathlib import Path

import pytest

from buildwright.config import Settings
from buildwright.harness import HarnessProject, format_status
from buildwright.registry import ModelRegistry, ModelSpec

FEATURES = {
    "project": {
        "name": "demo",
        "parallel": False,
        "modules": ["gauntlet", "security-audit"],
        "audit_level": "standard",
        "workflow": "full",
    },
    "features": [
        {
            "id": 1,
            "name": "f1_core",
            "title": "Core domain",
            "description": "hexagonal core",
            "acceptance": ["A1"],
            "status": "done",
        },
        {
            "id": 2,
            "name": "f2_api",
            "title": "REST API",
            "description": "fastapi endpoints",
            "acceptance": ["A2"],
            "status": "in_progress",
        },
        {
            "id": 3,
            "name": "f3_auth",
            "title": "Auth module",
            "description": "token auth",
            "acceptance": ["A3"],
            "status": "pending",
        },
    ],
}


def make_project(tmp_path: Path) -> Path:
    (tmp_path / "harness").mkdir()
    (tmp_path / "harness" / "feature_list.json").write_text(
        json.dumps(FEATURES), encoding="utf-8"
    )
    return tmp_path


def small_registry() -> ModelRegistry:
    return ModelRegistry(
        [
            ModelSpec(
                id="local/qwen3",
                label="Qwen3",
                kind="local",
                base_url="http://localhost:11434/v1",
            )
        ]
    )


def test_detect_walks_up_to_nearest_harness_project(tmp_path):
    root = make_project(tmp_path)
    nested = root / "src" / "pkg"
    nested.mkdir(parents=True)
    project = HarnessProject.detect(nested)
    assert project.root == root
    assert project.feature_list.project.name == "demo"


def test_detect_without_harness_project_fails_clean(tmp_path):
    with pytest.raises(FileNotFoundError, match="no harness project"):
        HarnessProject.detect(tmp_path)


def test_invalid_status_is_rejected(tmp_path):
    root = make_project(tmp_path)
    bad = {
        "project": FEATURES["project"],
        "features": [
            {
                "id": 1,
                "name": "x",
                "title": "t",
                "description": "d",
                "acceptance": [],
                "status": "shipit",
            }
        ],
    }
    (root / "harness" / "feature_list.json").write_text(json.dumps(bad), encoding="utf-8")
    with pytest.raises(ValueError, match="invalid .*feature_list.json"):
        HarnessProject.load(root)


def test_counts_and_next_pending(tmp_path):
    project = HarnessProject.detect(make_project(tmp_path))
    counts = project.counts()
    assert counts["done"] == 1
    assert counts["in_progress"] == 1
    assert counts["pending"] == 1
    assert project.next_pending() is not None
    assert project.next_pending().name == "f3_auth"  # type: ignore[union-attr]


def test_format_status_shows_board_and_planned_routing(tmp_path):
    project = HarnessProject.detect(make_project(tmp_path))
    text = format_status(project, Settings(models_file="/dev/null"), small_registry())
    assert "demo" in text
    assert "f1_core" in text and "f3_auth" in text
    assert "ROLE -> MODEL" in text
    assert "local/qwen3" in text
    # worker -> remote/gpt-4o-mini is not in the small registry: flagged, not fatal
    assert "MISSING-IN-REGISTRY" in text
