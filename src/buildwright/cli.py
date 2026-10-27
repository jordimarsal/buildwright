from __future__ import annotations

import argparse
import sys
from pathlib import Path

from .agents import Wright
from .config import ROLES, Settings
from .graph import build_graph
from .harness import HarnessProject, format_status
from .observability import get_tracer
from .registry import ModelRegistry


def _wright(settings: Settings) -> Wright:
    registry = ModelRegistry.load(settings.models_file)
    return Wright(settings=settings, registry=registry)


def cmd_models(settings: Settings) -> None:
    registry = ModelRegistry.load(settings.models_file)
    for kind in ("local", "remote"):
        print(f"\n{kind.upper()}")
        for s in registry.list(kind):
            key = f"  [key: {s.api_key_env}]" if s.api_key_env else ""
            print(f"  {s.id:<24} {s.label}{key}")


def cmd_roles(settings: Settings) -> None:
    ModelRegistry.load(settings.models_file)  # validates the file
    print("\nrole         model-id                model wire")
    registry = ModelRegistry.load(settings.models_file)
    for role in ROLES:
        model_id = settings.role_model(role)
        wire = registry.spec(model_id).model or model_id.split("/", 1)[1]
        print(f"{role:<12} {model_id:<24} {wire}")


def cmd_ask(settings: Settings, task: str) -> None:
    board = _wright(settings)
    graph = build_graph(board)
    tracer = get_tracer(settings)

    with tracer.span("buildwright.ask", task=task):
        state = graph.invoke({"task": task})

    plan = state["plan"]
    print("\n--- plan", f"({settings.role_model('planner')}) ---")
    for i, step in enumerate(plan.steps, 1):
        print(f"  {i}. {step}")
    print("\n--- answer", f"({settings.role_model('worker')}) ---")
    print(state["answer"])
    review = state["review"]
    print(f"\n--- review ({settings.role_model('reviewer')}): {review.verdict} ---")
    print(f"  {review.comments}")


def cmd_harness_status(settings: Settings, path: str | None) -> None:
    project = HarnessProject.detect(Path(path) if path else None)
    registry = ModelRegistry.load(settings.models_file)
    print(format_status(project, settings, registry))


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(
        prog="buildwright",
        description="Per-agent model routing: one model per role, local and remote in the same registry.",
    )
    sub = parser.add_subparsers(dest="cmd", required=True)
    sub.add_parser("models", help="list the models in the registry")
    sub.add_parser("roles", help="show the role -> model mapping")
    ask = sub.add_parser("ask", help="run the plan -> execute -> review pipeline")
    ask.add_argument("task")

    harness = sub.add_parser(
        "harness", help="inspect a harness-initialized project (read-only)"
    )
    harness_sub = harness.add_subparsers(dest="hcmd", required=True)
    status = harness_sub.add_parser(
        "status", help="feature board + planned role -> model routing"
    )
    status.add_argument(
        "path", nargs="?", default=None, help="project root (default: cwd)"
    )

    args = parser.parse_args(argv)
    settings = Settings()

    try:
        if args.cmd == "models":
            cmd_models(settings)
        elif args.cmd == "roles":
            cmd_roles(settings)
        elif args.cmd == "harness":
            cmd_harness_status(settings, args.path)
        else:
            cmd_ask(settings, args.task)
    except (KeyError, RuntimeError, FileNotFoundError, ValueError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
