"""Offline validation of parsed candidates: roles, registry ids, no-op, guards.

Pure functions only — the registry, the current mapping, the process env and
the runtime.yaml guard are injected as callables/flags, so tests need no
filesystem and the CLI stays a thin shell.

Spec: docs/specs/2026-11-14-roles-apply-design.md §5.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass

from .parser import Candidate


@dataclass(frozen=True)
class PlanChange:
    """One validated role change, ready for the confirm view and the writer."""

    role: str
    role_env: str
    current: str
    target: str
    noop: bool  # role already holds the target model — nothing to write
    env_wins: bool  # BW_ROLE_<ROLE> is exported in the process env and beats .env
    why: tuple[str, ...] = ()


def validate_candidates(
    candidates: Sequence[Candidate],
    *,
    known_roles: Sequence[str],
    model_known: Callable[[str], bool],
    known_model_ids: Callable[[], str],
    current_of: Callable[[str], str],
    env_has: Callable[[str], bool],
    runtime_roles_active: bool,
) -> tuple[list[PlanChange], list[str]]:
    """Validate every candidate; returns (changes, refusals).

    ``runtime_roles_active`` is the future-D1 guard: when a project's
    ``harness/runtime.yaml`` carries a ``roles`` key, that file — not .env — is
    the effective source, so the whole apply refuses with a pointer.
    """
    if runtime_roles_active:
        return [], [
            "harness/runtime.yaml carries a 'roles' key: it wins over .env — edit that file instead"
        ]
    refusals: list[str] = []
    changes: list[PlanChange] = []
    for c in candidates:
        if c.role not in known_roles:
            refusals.append(
                f"unknown role '{c.role}'. Known roles: {', '.join(known_roles)}"
            )
            continue
        if not model_known(c.model):
            refusals.append(
                f"unknown model id '{c.model}'. Known ids: {known_model_ids()}"
                " — add it to models.yaml first (5 lines)"
            )
            continue
        current = current_of(c.role)
        changes.append(
            PlanChange(
                role=c.role,
                role_env=c.role_env,
                current=current,
                target=c.model,
                noop=current == c.model,
                env_wins=env_has(c.role_env),
                why=c.why,
            )
        )
    return changes, refusals
