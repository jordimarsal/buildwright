"""`roles apply` / `roles set` — the human gate closing the debrief loop.

debrief proposes routing changes as text (its ADR-0001: it never writes);
this package is the write side that owns the `BW_ROLE_*` semantics. The gate
shows the evidence (`why run` / `why bench`), asks, and only then rewrites
`.env` — line-preserving and atomic.

Spec: docs/specs/2026-11-14-roles-apply-design.md
"""

from .envfile import apply_changes
from .parser import Candidate, ParseResult, canonical_role, parse_proposals
from .validate import PlanChange, validate_candidates

__all__ = [
    "Candidate",
    "ParseResult",
    "PlanChange",
    "apply_changes",
    "canonical_role",
    "parse_proposals",
    "validate_candidates",
]
