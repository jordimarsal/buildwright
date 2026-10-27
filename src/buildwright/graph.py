from __future__ import annotations

from typing import TypedDict

from langgraph.graph import END, START, StateGraph

from .agents import Plan, Review, Wright

# Each node is ONE PydanticAI agent; the model is decided by the role
# (Settings), never by the node. Assembling a CRAG or changing the flow
# therefore touches zero lines of model code.


class PipelineState(TypedDict, total=False):
    task: str
    plan: Plan
    step_outputs: list[str]
    answer: str
    review: Review


def build_graph(board: Wright):
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
    g.add_node("plan", plan)  # cheap/local model
    g.add_node("execute", execute)  # strong/remote model
    g.add_node("review", review)  # cheap/local model
    g.add_edge(START, "plan")
    g.add_edge("plan", "execute")
    g.add_edge("execute", "review")
    g.add_edge("review", END)
    return g.compile()
