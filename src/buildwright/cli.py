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
from .runtime import (
    Sandbox,
    build_implementer,
    create_worktree,
    next_batch,
    parse_tasks,
    verify_on_disk,
)


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


def cmd_harness_run(
    settings: Settings, feature: str | None, max_tasks: int, path: str | None, yes: bool
) -> None:
    """Phase 1: one supervised implementer batch in a git worktree."""
    project = HarnessProject.detect(Path(path) if path else None)
    fl = project.feature_list

    if feature:
        feat = next((f for f in fl.features if f.name == feature), None)
        if feat is None:
            raise KeyError(f"feature '{feature}' not found in {project.root}")
    else:
        in_progress = project.by_status("in_progress")
        if not in_progress:
            raise RuntimeError(
                "no feature is in_progress — approve a spec first "
                "(phase 1 does not run the spec phase)"
            )
        feat = in_progress[0]
    if feat.status != "in_progress":
        raise RuntimeError(
            f"feature '{feat.name}' is '{feat.status}'; phase 1 only executes "
            "in_progress features (with an approved spec)"
        )

    tasks_path = project.root / "harness" / "specs" / feat.name / "tasks.md"
    if not tasks_path.is_file():
        raise FileNotFoundError(
            f"missing {tasks_path} (spec = requirements.md + design.md + tasks.md)"
        )
    batch = next_batch(parse_tasks(tasks_path.read_text(encoding="utf-8")), max_tasks)
    if not batch:
        raise RuntimeError(f"feature '{feat.name}' has no pending tasks")

    print(f"\nfeature   {feat.name} — {feat.title}")
    print(f"batch     {', '.join(t.task_id for t in batch)}")
    for t in batch:
        print(f"  {t.task_id}: {t.text}")
    print(f"implementer model  {settings.role_model('worker')}")
    if not yes and input("proceed? [y/N] ").strip().lower() != "y":
        print("aborted")
        return

    worktree = create_worktree(project.root, feat.name)
    print(f"worktree  {worktree}")

    sandbox = Sandbox.from_project(
        worktree, log_dir=worktree / "harness" / "logs" / feat.name
    )
    registry = ModelRegistry.load(settings.models_file)
    agent = build_implementer(registry.build(settings.role_model("worker")), sandbox)

    prompt = (
        f"Feature: {feat.name} — {feat.title}\n"
        f"Spec: harness/specs/{feat.name}/ (requirements.md, design.md, tasks.md)\n"
        f"Acceptance: {'; '.join(feat.acceptance)}\n"
        "Your batch: "
        + "; ".join(f"{t.task_id}: {t.text}" for t in batch)
    )
    result = agent.run_sync(prompt, deps=sandbox)
    print(f"\n--- implementer ({settings.role_model('worker')}) ---")
    print(result.output)

    report = verify_on_disk(worktree, feat.name, batch)
    print("\n--- verified on disk ---")
    print(f"tasks marked   {', '.join(report.marked) or '-'}  "
          f"({len(report.marked)}/{len(report.batch)})")
    print(f"changed files  {len(report.changed_files)}")
    for f in report.changed_files:
        print(f"  {f}")
    print(f"\nreview with: git -C {worktree} diff")
    if len(report.marked) != len(report.batch):
        print("result: INCOMPLETE — tasks claimed but not marked on disk", file=sys.stderr)
        sys.exit(1)


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
        "harness", help="inspect/execute a harness-initialized project"
    )
    harness_sub = harness.add_subparsers(dest="hcmd", required=True)
    status = harness_sub.add_parser(
        "status", help="feature board + planned role -> model routing (read-only)"
    )
    status.add_argument(
        "path", nargs="?", default=None, help="project root (default: cwd)"
    )
    run = harness_sub.add_parser(
        "run", help="run ONE supervised implementer batch in a git worktree"
    )
    run.add_argument("--feature", default=None, help="feature name (default: first in_progress)")
    run.add_argument("--max-tasks", type=int, default=3, help="batch size cap (1-4)")
    run.add_argument("--yes", action="store_true", help="skip the confirmation prompt")
    run.add_argument("path", nargs="?", default=None, help="project root (default: cwd)")

    args = parser.parse_args(argv)
    settings = Settings()

    try:
        if args.cmd == "models":
            cmd_models(settings)
        elif args.cmd == "roles":
            cmd_roles(settings)
        elif args.cmd == "harness":
            if args.hcmd == "status":
                cmd_harness_status(settings, args.path)
            else:
                cmd_harness_run(
                    settings, args.feature, args.max_tasks, args.path, args.yes
                )
        else:
            cmd_ask(settings, args.task)
    except (KeyError, RuntimeError, FileNotFoundError, ValueError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
