from importlib import resources
from pathlib import Path

import pytest

from buildwright.config import resolve_env
from buildwright.registry import ModelRegistry

YAML = """
models:
  - id: local/qwen3
    label: Qwen3 (Ollama)
    kind: local
    base_url: http://localhost:11434/v1
    model: qwen3:8b
  - id: remote/gpt-4o-mini
    label: GPT-4o mini
    kind: remote
    base_url: https://api.openai.com/v1
    model: gpt-4o-mini
    api_key_env: TEST_MODEL_KEY
"""


def write_registry(tmp_path):
    path = tmp_path / "models.yaml"
    path.write_text(YAML, encoding="utf-8")
    return ModelRegistry.load(path)


def test_load_and_lookup(tmp_path):
    registry = write_registry(tmp_path)
    assert registry.spec("local/qwen3").model == "qwen3:8b"
    assert [s.id for s in registry.list("local")] == ["local/qwen3"]


def test_unknown_id_lists_known(tmp_path):
    registry = write_registry(tmp_path)
    with pytest.raises(KeyError, match="remote/gpt-4o-mini"):
        registry.spec("remote/missing")


def test_build_local_needs_no_key(tmp_path, monkeypatch):
    monkeypatch.delenv("TEST_MODEL_KEY", raising=False)
    registry = write_registry(tmp_path)
    model = registry.build("local/qwen3")
    assert model.model_name == "qwen3:8b"


def test_build_remote_without_key_fails(tmp_path, monkeypatch):
    monkeypatch.delenv("TEST_MODEL_KEY", raising=False)
    registry = write_registry(tmp_path)
    with pytest.raises(RuntimeError, match="TEST_MODEL_KEY"):
        registry.build("remote/gpt-4o-mini")


def test_build_remote_with_key(tmp_path, monkeypatch):
    monkeypatch.setenv("TEST_MODEL_KEY", "sk-test")
    registry = write_registry(tmp_path)
    model = registry.build("remote/gpt-4o-mini")
    assert model.model_name == "gpt-4o-mini"


def test_build_remote_key_comes_from_dotenv(tmp_path, monkeypatch):
    """`.env` documents provider keys — they must reach the client (D: config)."""
    monkeypatch.delenv("TEST_MODEL_KEY", raising=False)
    monkeypatch.chdir(tmp_path)
    (tmp_path / ".env").write_text("# provider keys\nTEST_MODEL_KEY = 'sk-dotenv'\n")
    assert resolve_env("TEST_MODEL_KEY") == "sk-dotenv"  # no RuntimeError on build
    model = write_registry(tmp_path).build("remote/gpt-4o-mini")
    assert model.model_name == "gpt-4o-mini"


def test_process_env_wins_over_dotenv(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    (tmp_path / ".env").write_text("TEST_MODEL_KEY=sk-dotenv\n")
    monkeypatch.setenv("TEST_MODEL_KEY", "sk-process")
    assert resolve_env("TEST_MODEL_KEY") == "sk-process"
    assert resolve_env("MISSING_KEY") == ""


def test_bundled_registry_parses():
    """The registry shipped inside the wheel must stay valid YAML."""
    path = Path(str(resources.files("buildwright").joinpath("models.yaml")))
    ids = {s.id for s in ModelRegistry.load(path).list()}
    assert {"local/qwen3", "remote/gpt-4o-mini", "remote/zai-glm-flash"} <= ids
