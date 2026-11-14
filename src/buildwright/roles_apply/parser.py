"""Parse debrief's ``routing.propose`` output (its ADR-0003 diff-as-text).

The contract is debrief's rendered proposal, pinned by fixtures on both sides:
this parser only ever applies ``+ BW_ROLE_<ROLE>=<model>`` lines, so a ``-``
line can never un-apply anything. Format drift between debrief and buildwright
fails ``tests/test_roles_apply.py`` (fixture ``debrief_proposal.txt``) before
any user pastes a broken proposal.

Spec: docs/specs/2026-11-14-roles-apply-design.md §4.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

_CANDIDATE_RE = re.compile(r"^\+\s*bw_role_([A-Za-z0-9_]+)=(\S+)\s*$", re.IGNORECASE)
_PROPOSAL_RE = re.compile(r"^proposal:\s*(.*)$")

_LINE_KINDS: tuple[tuple[str, str], ...] = (
    ("minus", "- "),
    ("why", "why "),
    ("signature", "signature:"),
    ("note", "note:"),
    ("not_applied", "not applied:"),
)


@dataclass(frozen=True)
class Candidate:
    """One ``+ BW_ROLE_<ROLE>=<model>`` line, canonicalized."""

    role: str  # canonical lowercase, e.g. "worker" (matches config.ROLES)
    role_env: str  # "BW_ROLE_WORKER"
    model: str  # target registry id
    why: tuple[str, ...] = ()  # the block's `why run` / `why bench` lines (display only)


@dataclass(frozen=True)
class ParseResult:
    candidates: tuple[Candidate, ...] = ()
    conflicts: tuple[str, ...] = ()  # non-empty => refuse the whole apply
    saw: dict[str, int] = field(default_factory=dict)  # line kinds found (for refusals)


def canonical_role(raw: str) -> tuple[str, str]:
    """`bw_role_Worker` -> ("worker", "BW_ROLE_WORKER") — mirrors debrief's canonicalization."""
    upper = raw.upper().removeprefix("BW_ROLE_")
    return upper.lower(), f"BW_ROLE_{upper}"


def parse_proposals(text: str) -> ParseResult:
    """Extract the apply candidates from debrief's proposal text.

    Every other line kind is counted (``saw``) so an empty paste can be
    refused with a useful summary. Identical duplicates dedupe; the same role
    proposed with two different models is a conflict that refuses everything
    (never silently pick first or last).
    """
    saw: dict[str, int] = {}
    entries: dict[str, list] = {}  # role -> [model, role_env, why_list]
    conflicts: list[str] = []
    last_role: str | None = None

    for line in text.splitlines():
        if _PROPOSAL_RE.match(line):
            saw["proposal"] = saw.get("proposal", 0) + 1
            last_role = None  # a why line before its `+` belongs to the next block
            continue
        m = _CANDIDATE_RE.match(line)
        if m:
            saw["plus"] = saw.get("plus", 0) + 1
            role, role_env = canonical_role(m.group(1))
            model = m.group(2)
            prev = entries.get(role)
            if prev is not None and prev[0] != model:
                conflicts.append(
                    f"role {role} proposed twice with different models: {prev[0]} vs {model}"
                )
                continue
            entries.setdefault(role, [model, role_env, []])
            last_role = role
            continue
        if line.startswith("+ "):
            saw["plus_malformed"] = saw.get("plus_malformed", 0) + 1
            continue
        for kind, prefix in _LINE_KINDS:
            if line.startswith(prefix):
                saw[kind] = saw.get(kind, 0) + 1
                if kind == "why" and last_role is not None:
                    entries[last_role][2].append(line)
                break

    if conflicts:
        return ParseResult(candidates=(), conflicts=tuple(conflicts), saw=saw)
    candidates = tuple(
        Candidate(role=role, role_env=entry[1], model=entry[0], why=tuple(entry[2]))
        for role, entry in entries.items()
    )
    return ParseResult(candidates=candidates, saw=saw)
