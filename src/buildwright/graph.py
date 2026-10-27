from __future__ import annotations

from typing import TypedDict

from langgraph.graph import END, START, StateGraph

from .agents import Plan, Review, buildwright

# Cada node és UN agent PydanticAI; el model el decideix el rol (Settings), no el node.
# Així muntar un CRAG o canviar el flux no toca ni una línia de codi de models.


class PipelineState(TypedDict, total=False):
    task: str
    plan: Plan
    step_outputs: list[str]
    answer: str
    review: Review


def build_graph(board: buildwright):
    planner = board.planner()
    worker = board.worker()
    reviewer = board.reviewer()

    def plan(state: PipelineState) -> dict:
        result = planner.run_sync(f"Task: {state['task']}")
        return {"plan": result.output}

    def execute(state: PipelineState) -> dict:
        outputs: list[str] = []
        for i, step in enumerate(state["plan"].steps, start=1):
            r = worker.run_sync(f"Task: {state['task']}\nStep {i}: {step}")
            outputs.append(r.output)
        return {"step_outputs": outputs, "answer": "\n\n".join(outputs)}

    def review(state: PipelineState) -> dict:
        r = reviewer.run_sync(f"Task: {state['task']}\n\nWork to review:\n{state['answer']}")
        return {"review": r.output}

    g = StateGraph(PipelineState)
    g.add_node("plan", plan)      # model barat/local
    g.add_node("execute", execute)  # model potent/remot
    g.add_node("review", review)  # model barat/local
    g.add_edge(START, "plan")
    g.add_edge("plan", "execute")
    g.add_edge("execute", "review")
    g.add_edge("review", END)
    return g.compile()
