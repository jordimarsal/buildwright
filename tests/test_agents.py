from buildwright.agents import Plan, Review, buildwright
from buildwright.config import Settings
from buildwright.registry import ModelRegistry


def make_board() -> buildwright:
    # sense registry: la factory fa de "model" i no toca xarxa
    return buildwright(settings=Settings(models_file="/dev/null"))


def test_unknown_role_rejected():
    board = make_board()
    try:
        board.model_for("ceo")
    except KeyError as exc:
        assert "unknown role" in str(exc)
    else:
        raise AssertionError("expected KeyError")


def test_role_mapping_from_settings():
    settings = Settings(
        models_file="/dev/null",
        role_planner="local/llamacpp",
        role_worker="remote/gpt-4o",
        role_reviewer="local/llamacpp",
    )
    board = buildwright(settings=settings)
    assert settings.role_model("planner") == "local/llamacpp"
    assert settings.role_model("worker") == "remote/gpt-4o"
    assert board.registry is None  # construït per defecte sense registry


def test_registry_load_roundtrip(tmp_path):
    path = tmp_path / "models.yaml"
    path.write_text(
        """
models:
  - id: local/llamacpp
    label: llama.cpp
    kind: local
    base_url: http://localhost:8080/v1
""",
        encoding="utf-8",
    )
    registry = ModelRegistry.load(path)
    assert registry.spec("local/llamacpp").kind == "local"


def test_plan_schema():
    plan = Plan(steps=["a"])
    assert plan.needs_strong_model is False


def test_review_schema():
    review = Review(verdict="pass", comments="ok")
    assert review.verdict == "pass"
