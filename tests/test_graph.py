from pydantic_ai.models.test import TestModel

from buildwright.agents import buildwright
from buildwright.config import Settings
from buildwright.graph import build_graph


def make_board() -> buildwright:
    return buildwright(
        settings=Settings(models_file="/dev/null"),
        model_factory=lambda role: TestModel(),
    )


def test_graph_runs_offline():
    graph = build_graph(make_board())
    state = graph.invoke({"task": "write fizzbuzz in python"})
    assert state["plan"].steps
    assert state["answer"]
    assert state["review"].verdict in ("pass", "fail")
    assert len(state["step_outputs"]) == len(state["plan"].steps)
