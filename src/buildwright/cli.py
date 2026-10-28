from __future__ import annotations

import argparse
import sys
from collections.abc import Callable
from pathlib import Path
from typing import Any

from langgraph.types import Command, RunnableConfig

from .agents import Wright
from .config import ROLES, Settings
from .graph import build_graph
from .harness import Feature, HarnessProject, format_status
from .loop import (
    LoopContext,
    LoopState,
    ReviewVerdict,
    build_loop_graph,
    loop_session,
    make_context,
    pending_interrupt,
    prepare_seed,
    retro_line,
)
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


def _select_feature(project: HarnessProject, feature: str | None) -> Feature:
    features = project.feature_list.features
    if feature:
        feat = next((f for f in features if f.name == feature), None)
        if feat is None:
            raise KeyError(f"feature '{feature}' not found in {project.root}")
    else:
        in_progress = project.by_status("in_progress")
        if not in_progress:
            raise RuntimeError(
                "no feature is in_progress — the loop executes in_progress "
                "features (the spec gate was approved interactively)"
            )
        feat = in_progress[0]
    if feat.status == "blocked":
        raise RuntimeError(
            f"feature '{feat.name}' is blocked — resume it with "
            "`buildwright harness approve` (or set it in_progress manually)"
        )
    if feat.status != "in_progress":
        raise RuntimeError(
            f"feature '{feat.name}' is '{feat.status}'; the loop only executes "
            "in_progress features"
        )
    return feat


def _confirm(ctx: LoopContext, seed: LoopState, yes: bool) -> None:
    level = ctx.project.feature_list.project.audit_level
    print(f"\nfeature       {ctx.feature.name} — {ctx.feature.title}")
    print(
        f"batch         {', '.join(str(b) for b in seed.get('batch', []))} "
        f"(size <= {ctx.max_tasks}, budget {seed.get('run_limit')} batches)"
    )
    print(f"worktree      {ctx.worktree}")
    print(f"implementer   {ctx.settings.role_model('worker')}")
    print(f"reviewer      {ctx.settings.role_model('reviewer')}")
    print(f"gates         init.sh · check-traceability.py · audit ({level})")
    if not yes and input("proceed? [y/N] ").strip().lower() != "y":
        print("aborted")
        raise SystemExit(0)


def cmd_harness_run(
    settings: Settings,
    feature: str | None,
    max_tasks: int,
    max_batches: int,
    path: str | None,
    yes: bool,
    dispatch: Callable[[str], str] | None = None,
    review: Callable[[str], ReviewVerdict] | None = None,
) -> None:
    """Phase 2: the full feature loop — batches, hard gates, human gates."""
    project = HarnessProject.detect(Path(path) if path else None)
    feat = _select_feature(project, feature)
    ctx = make_context(
        project,
        settings,
        feat.name,
        max_tasks=max_tasks,
        max_batches=max_batches,
        dispatch=dispatch,
        review=review,
    )
    tracer = get_tracer(settings)
    with loop_session(project.root) as saver, tracer.span(
        "buildwright.harness.loop", feature=feat.name
    ):
        graph = build_loop_graph(ctx, saver)
        cfg: RunnableConfig = {"configurable": {"thread_id": feat.name}}
        snap = graph.get_state(cfg)
        if pending_interrupt(snap):
            raise RuntimeError(
                f"feature '{feat.name}' is parked at a human gate — resume with "
                "`buildwright harness approve` or `buildwright harness reject`"
            )
        if snap.next:
            print("resuming an interrupted run (previous invocation did not finish)")
            out: dict[str, Any] = graph.invoke(None, cfg)
        else:
            seed = prepare_seed(ctx, dict(snap.values) if snap.values else None)
            _confirm(ctx, seed, yes)
            out = graph.invoke(seed, cfg)
    _print_result(ctx, out)


def cmd_harness_gate(
    settings: Settings,
    decision: str,
    comments: str,
    feature: str | None,
    path: str | None,
) -> None:
    """Resume a parked human gate: approve continues, reject turns back."""
    project = HarnessProject.detect(Path(path) if path else None)
    worktrees = project.root / ".buildwright" / "worktrees"
    candidates = [
        f
        for f in project.feature_list.features
        if f.status in ("in_progress", "blocked")
        and (worktrees / f.name).is_dir()
        and (feature is None or f.name == feature)
    ]
    if not candidates:
        raise RuntimeError(
            "no feature with a buildwright worktree to resume"
            + (f" (feature '{feature}')" if feature else "")
        )
    tracer = get_tracer(settings)
    with loop_session(project.root) as saver, tracer.span(
        "buildwright.harness.gate", decision=decision
    ):
        parked: list[tuple[LoopContext, Any]] = []
        for feat in candidates:
            ctx = make_context(project, settings, feat.name)
            graph = build_loop_graph(ctx, saver)
            snap = graph.get_state({"configurable": {"thread_id": feat.name}})
            if pending_interrupt(snap):
                parked.append((ctx, graph))
        if not parked:
            raise RuntimeError(f"no parked gate — nothing to {decision}")
        if len(parked) > 1:
            names = ", ".join(c.feature.name for c, _ in parked)
            raise RuntimeError(f"multiple parked gates ({names}) — pass --feature")
        ctx, graph = parked[0]
        cfg: RunnableConfig = {"configurable": {"thread_id": ctx.feature.name}}
        out: dict[str, Any] = graph.invoke(
            Command(resume={"decision": decision, "comments": comments}), cfg
        )
    _print_result(ctx, out)


def _print_stats(state: dict[str, Any]) -> None:
    print(
        f"  batches {state.get('batches_done', 0)} · "
        f"dispatches {state.get('dispatches', 0)} · "
        f"stalls {state.get('stalls_total', 0)} · "
        f"restarts {state.get('restarts', 0)}"
    )


def _print_gate(ctx: LoopContext, payload: Any) -> None:
    if not isinstance(payload, dict):
        print(f"\n[parked] {ctx.feature.name}: {payload}")
        return
    gate = payload.get("gate")
    if gate == "completion":
        summary = payload.get("summary", {})
        print(f"\n[completion gate] {ctx.feature.name} — {ctx.feature.title}")
        print(
            f"  reviewer: {summary.get('verdict', '?')} · "
            f"batches {summary.get('batches', 0)} · "
            f"dispatches {summary.get('dispatches', 0)} · "
            f"stalls {summary.get('stalls', 0)} · "
            f"restarts {summary.get('restarts', 0)}"
        )
        print("  approve → buildwright harness approve [comments]")
        print('  reject  → buildwright harness reject "what to change"')
    elif gate == "parked":
        print(f"\n[parked] {ctx.feature.name} is blocked")
        print(f"  reason: {payload.get('reason', '?')}")
        print("  approve → buildwright harness approve   (resumes the loop)")
        print('  reject  → buildwright harness reject    (leaves it blocked)')
    else:
        print(f"\n[parked] unknown gate {gate!r}")


def _print_result(ctx: LoopContext, out: dict[str, Any]) -> None:
    parked = out.get("__interrupt__")
    state = {k: v for k, v in out.items() if k != "__interrupt__"}
    log = state.get("log") or []
    if log:
        print("\n--- loop log ---")
        for line in log:
            print(f"  {line}")
    if parked:
        _print_gate(ctx, parked[0].value)
    elif state.get("finalized"):
        print(
            f"\n[done] {ctx.feature.name} — merged to main, APPROVAL written, "
            "status done"
        )
        print(f"  {retro_line(state)}")
    else:
        print("\n[stopped] run limit reached — re-run `buildwright harness run` to continue")
        _print_stats(state)


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
        "run", help="run the feature loop: batches -> hard gates -> review -> human gate"
    )
    run.add_argument("--feature", default=None, help="feature name (default: first in_progress)")
    run.add_argument("--max-tasks", type=int, default=3, help="batch size cap (1-4)")
    run.add_argument(
        "--max-batches", type=int, default=10, help="batch budget for this invocation"
    )
    run.add_argument("--yes", action="store_true", help="skip the confirmation prompt")
    run.add_argument("path", nargs="?", default=None, help="project root (default: cwd)")

    approve = harness_sub.add_parser(
        "approve", help="resume a parked human gate with approval"
    )
    approve.add_argument("comments", nargs="*", help="recorded in APPROVAL / passed on")
    approve.add_argument("--feature", default=None, help="which parked feature (when several)")
    approve.add_argument("--path", default=None, help="project root (default: cwd)")

    reject = harness_sub.add_parser(
        "reject", help="resume a parked human gate with changes requested"
    )
    reject.add_argument(
        "comments", nargs="+", help="what to change (fed back to the implementer)"
    )
    reject.add_argument("--feature", default=None, help="which parked feature (when several)")
    reject.add_argument("--path", default=None, help="project root (default: cwd)")

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
            elif args.hcmd == "run":
                cmd_harness_run(
                    settings,
                    args.feature,
                    args.max_tasks,
                    args.max_batches,
                    args.path,
                    args.yes,
                )
            elif args.hcmd == "approve":
                cmd_harness_gate(
                    settings,
                    "approve",
                    " ".join(args.comments),
                    args.feature,
                    args.path,
                )
            else:
                cmd_harness_gate(
                    settings,
                    "reject",
                    " ".join(args.comments),
                    args.feature,
                    args.path,
                )
        else:
            cmd_ask(settings, args.task)
    except (KeyError, RuntimeError, FileNotFoundError, ValueError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
