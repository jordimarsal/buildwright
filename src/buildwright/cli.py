from __future__ import annotations

import argparse
import sys
from collections.abc import Callable
from pathlib import Path
from typing import Any

from langgraph.types import Command, RunnableConfig

from . import runlog
from .agents import Wright
from .config import ROLES, Settings
from .graph import build_graph
from .harness import Feature, HarnessProject, format_status
from .loop import (
    GauntletVerdict,
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
        if in_progress:
            feat = in_progress[0]
        else:
            pending = project.by_status("pending")
            if not pending:
                raise RuntimeError(
                    "no feature to run — the queue is empty (all features done?)"
                )
            feat = pending[0]
    if feat.status == "blocked":
        raise RuntimeError(
            f"feature '{feat.name}' is blocked — resume it with "
            "`buildwright harness approve` (or set it in_progress manually)"
        )
    if feat.status == "spec_ready":
        raise RuntimeError(
            f"feature '{feat.name}' is spec_ready — its spec gate is parked; "
            "resume it with `buildwright harness approve` or `reject`"
        )
    if feat.status not in ("in_progress", "pending"):
        raise RuntimeError(
            f"feature '{feat.name}' is '{feat.status}'; the loop only executes "
            "in_progress or pending features"
        )
    return feat


def _confirm(ctx: LoopContext, seed: LoopState, yes: bool) -> None:
    level = ctx.project.feature_list.project.audit_level
    print(f"\nfeature       {ctx.feature.name} — {ctx.feature.title}")
    if ctx.feature.status == "pending":
        print("phase         spec — the spec-author writes the trio, then a human gate")
        print(f"spec-author   {ctx.settings.role_model('planner')}")
        print(f"worktree      {ctx.worktree}")
        print(f"gates         spec gate (human) · init.sh · traceability · audit ({level})")
        if not yes and input("proceed? [y/N] ").strip().lower() != "y":
            print("aborted")
            raise SystemExit(0)
        return
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


def _select_features(project: HarnessProject, feature: str | None, budget: int) -> list[Feature]:
    """Features to run this invocation.

    Sequential (default): the loop re-selects after every finalize, so one
    feature at a time. Parallel (`parallel: true`): up to `budget` runnable
    features start together — `in_progress` first, then `pending` whose
    `depends_on` are all done.
    """
    if feature:
        found = _select_feature(project, feature)
        return [found]
    if not project.feature_list.project.parallel:
        return [_select_feature(project, None)]
    done = {f.name for f in project.feature_list.features if f.status == "done"}
    runnable = [
        f
        for f in project.feature_list.features
        if f.status == "in_progress" or (f.status == "pending" and f.runnable(done))
    ]
    if not runnable:
        raise RuntimeError("no feature to run — the queue is empty (all features done?)")
    return runnable[:budget]


def _run_one_feature(
    settings: Settings,
    project: HarnessProject,
    feat: Feature,
    max_tasks: int,
    max_batches: int,
    yes: bool,
    dispatch: Callable[[str], str] | None,
    review: Callable[[str], ReviewVerdict] | None,
    spec: Callable[[str], str] | None,
    gauntlet: Callable[[str], GauntletVerdict] | None,
) -> bool:
    """Run one feature to its next stopping point; True when it finalized."""
    ctx = make_context(
        project,
        settings,
        feat.name,
        max_tasks=max_tasks,
        max_batches=max_batches,
        dispatch=dispatch,
        review=review,
        spec=spec,
        gauntlet=gauntlet,
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
            runlog.emit(f"run: resuming interrupted run — {feat.name}", project.root)
            out: dict[str, Any] = graph.invoke(None, cfg)
        else:
            seed = prepare_seed(ctx, dict(snap.values) if snap.values else None)
            _confirm(ctx, seed, yes)
            runlog.emit(
                f"run: {feat.name} — planner {settings.role_model('planner')} · "
                f"worker {settings.role_model('worker')} · "
                f"reviewer {settings.role_model('reviewer')}",
                project.root,
            )
            out = graph.invoke(seed, cfg)
    _print_result(ctx, out)
    return bool(isinstance(out, dict) and out.get("finalized"))


def cmd_harness_run(
    settings: Settings,
    feature: str | None,
    max_tasks: int,
    max_batches: int,
    features: int,
    path: str | None,
    yes: bool,
    dispatch: Callable[[str], str] | None = None,
    review: Callable[[str], ReviewVerdict] | None = None,
    spec: Callable[[str], str] | None = None,
    gauntlet: Callable[[str], GauntletVerdict] | None = None,
) -> None:
    """Phase 3: the feature queue — spec gate, batches, hard gates, human gates.

    One invocation processes up to `features` features. Sequential by default
    (re-select after every finalize, stopping at any human gate); with
    `parallel: true` the selected features start together, one worktree each —
    main-repo mutations are serialized through MAIN_LOCK.
    """
    project = HarnessProject.detect(Path(path) if path else None)
    selected = _select_features(project, feature, max(1, features))

    if len(selected) == 1 or not project.feature_list.project.parallel:
        done = 0
        while done < max(1, features):
            project = HarnessProject.detect(Path(path) if path else None)
            (feat,) = _select_features(project, feature, 1)
            finalized = _run_one_feature(
                settings,
                project,
                feat,
                max_tasks,
                max_batches,
                yes,
                dispatch,
                review,
                spec,
                gauntlet,
            )
            done += 1
            if feature:
                break  # explicit --feature: exactly one, even if it just finished
            if not finalized:
                print(f"\nqueue: '{feat.name}' waits at a human gate — stopping here")
                break
        return

    # parallel: independent features, one thread each (one worktree per feature)
    from concurrent.futures import ThreadPoolExecutor

    print(f"parallel: {len(selected)} features starting together")
    with ThreadPoolExecutor(max_workers=len(selected)) as pool:
        futures = {
            pool.submit(
                _run_one_feature,
                settings,
                project,
                feat,
                max_tasks,
                max_batches,
                yes,
                dispatch,
                review,
                spec,
                gauntlet,
            ): feat
            for feat in selected
        }
        for future, feat in futures.items():
            if not future.result():
                print(f"\nqueue: '{feat.name}' waits at a human gate — stopping here")


def cmd_harness_gate(
    settings: Settings,
    decision: str,
    comments: str,
    feature: str | None,
    path: str | None,
    dispatch: Callable[[str], str] | None = None,
    review: Callable[[str], ReviewVerdict] | None = None,
    spec: Callable[[str], str] | None = None,
    gauntlet: Callable[[str], GauntletVerdict] | None = None,
) -> None:
    """Resume a parked gate: approve/fix continue (comments steer the
    implementer), reject aborts (at the spec gate it re-authors instead)."""
    project = HarnessProject.detect(Path(path) if path else None)
    worktrees = project.root / ".buildwright" / "worktrees"
    candidates = [
        f
        for f in project.feature_list.features
        if f.status in ("in_progress", "blocked", "spec_ready")
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
            ctx = make_context(
                project,
                settings,
                feat.name,
                dispatch=dispatch,
                review=review,
                spec=spec,
                gauntlet=gauntlet,
            )
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
        print('  fix     → buildwright harness fix "what to change"  (back to the implementer)')
    elif gate == "spec":
        print(f"\n[spec gate] {ctx.feature.name} — {ctx.feature.title}")
        print(f"  spec trio at harness/specs/{ctx.feature.name}/ (read it yourself)")
        print("  approve → buildwright harness approve [comments]")
        runlog.emit(
            f"run: {ctx.feature.name} parked at spec gate — review the trio, "
            "then approve/reject",
            ctx.project.root,
        )
        print('  reject  → buildwright harness reject "what to change in the spec"')
        print('  fix     → buildwright harness fix "what to change"  (= reject + re-author)')
    elif gate == "parked":
        print(f"\n[parked] {ctx.feature.name} is blocked")
        print(f"  reason: {payload.get('reason', '?')}")
        print("  approve → buildwright harness approve   (resumes the loop)")
        print('  reject  → buildwright harness reject    (leaves it blocked)')
        print(
            '  fix     → buildwright harness fix [comments]  '
            '(comments go to the implementer; caps reset)'
        )
        runlog.emit(
            f"run: {ctx.feature.name} parked — {payload.get('reason', '?')}",
            ctx.project.root,
        )
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
        runlog.emit(f"run: {ctx.feature.name} done — merged to main", ctx.project.root)
    else:
        print("\n[stopped] run limit reached — re-run `buildwright harness run` to continue")
        runlog.emit("run: stopped — run limit reached", ctx.project.root)
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
        "harness",
        help="inspect/execute a harness-initialized project",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        description=(
            "Spec-driven feature loop. run: the spec trio is authored and parked at\n"
            "the spec gate (you review it); approved, it implements batch by batch\n"
            "with hard gates (init.sh, traceability, audit), a reviewer verdict and\n"
            "the adversarial gauntlet, ending at a completion gate you approve\n"
            "(merge to main + APPROVAL + status done).\n\n"
            "Parked gates are resumed with:\n"
            "  approve  continue — optional comments steer the implementer\n"
            "  fix      order a fix — comments (or the parked findings) are\n"
            "           dispatched to the implementer; cycle caps reset\n"
            "  reject   abort — leaves the feature blocked (at the spec gate:\n"
            "           re-author with your comments)"
        ),
    )
    harness_sub = harness.add_subparsers(dest="hcmd", required=True)
    status = harness_sub.add_parser(
        "status", help="feature board + planned role -> model routing (read-only)"
    )
    status.add_argument(
        "path", nargs="?", default=None, help="project root (default: cwd)"
    )
    run = harness_sub.add_parser(
        "run", help="run the feature queue: spec gate -> batches -> gates -> human gate"
    )
    run.add_argument("--feature", default=None, help="feature name (default: first in_progress/pending)")
    run.add_argument("--max-tasks", type=int, default=3, help="batch size cap (1-4)")
    run.add_argument(
        "--max-batches", type=int, default=10, help="batch budget for this invocation"
    )
    run.add_argument(
        "--features",
        type=int,
        default=1,
        help="how many features to process in this invocation (default 1)",
    )
    run.add_argument("--yes", action="store_true", help="skip the confirmation prompt")
    run.add_argument("path", nargs="?", default=None, help="project root (default: cwd)")

    approve = harness_sub.add_parser(
        "approve",
        help="resume a parked gate — optional comments steer the implementer",
    )
    approve.add_argument(
        "comments",
        nargs="*",
        help="steering note, passed to the implementer; recorded in APPROVAL",
    )
    approve.add_argument("--feature", default=None, help="which parked feature (when several)")
    approve.add_argument("--path", default=None, help="project root (default: cwd)")

    reject = harness_sub.add_parser(
        "reject",
        help="abort — leaves the feature blocked (spec gate: re-author with comments)",
    )
    reject.add_argument(
        "comments", nargs="+", help="what to change (fed back to the implementer)"
    )
    reject.add_argument("--feature", default=None, help="which parked feature (when several)")
    reject.add_argument("--path", default=None, help="project root (default: cwd)")

    fix = harness_sub.add_parser(
        "fix",
        help=(
            "resume a parked gate ordering a fix — comments go to the implementer; "
            "no comments = fix the parked findings"
        ),
    )
    fix.add_argument(
        "comments",
        nargs="*",
        help="what to fix (default: the findings the park is about)",
    )
    fix.add_argument("--feature", default=None, help="which parked feature (when several)")
    fix.add_argument("--path", default=None, help="project root (default: cwd)")

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
                    args.features,
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
            elif args.hcmd == "fix":
                cmd_harness_gate(
                    settings,
                    "fix",
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
