"""Tests for `roles apply`: parse debrief proposals, validate them, write .env.

The fixture ``debrief_proposal.txt`` is debrief's ``routing.propose`` render —
the same text its own tests pin. If debrief's format ever drifts, these tests
fail here before any user pastes a broken proposal (same discipline as
``test_debrief_log.py`` mirroring debrief's reader).

Spec: docs/specs/2026-11-14-roles-apply-design.md
"""

from pathlib import Path

from buildwright.roles_apply import (
    apply_changes,
    parse_proposals,
    validate_candidates,
)

FIXTURES = Path(__file__).parent / "fixtures"
PROPOSAL = (FIXTURES / "debrief_proposal.txt").read_text(encoding="utf-8")

KNOWN_ROLES = ("planner", "worker", "reviewer")
KNOWN_IDS = ("local/llamacpp", "remote/zai-glm-flash", "remote/gpt-4o-mini")
CURRENT = {"planner": "remote/gpt-4o-mini", "worker": "local/llamacpp", "reviewer": "remote/gpt-4o-mini"}


def _validate(candidates, *, env=frozenset(), runtime_roles=False):
    return validate_candidates(
        candidates,
        known_roles=KNOWN_ROLES,
        model_known=lambda mid: mid in KNOWN_IDS,
        known_model_ids=lambda: ", ".join(KNOWN_IDS),
        current_of=lambda role: CURRENT[role],
        env_has=lambda name: name in env,
        runtime_roles_active=runtime_roles,
    )


class TestParseProposals:
    def test_parses_the_fixture_plus_line_with_why_context(self):
        result = parse_proposals(PROPOSAL)
        assert not result.conflicts
        assert len(result.candidates) == 1
        c = result.candidates[0]
        assert (c.role, c.role_env, c.model) == ("worker", "BW_ROLE_WORKER", "remote/zai-glm-flash")
        assert any("failed code" in w for w in c.why)
        assert any("score 0.90" in w for w in c.why)

    def test_minus_line_is_never_a_candidate(self):
        result = parse_proposals("- BW_ROLE_WORKER=local/llamacpp\n- BW_ROLE_WORKER=other\n")
        assert result.candidates == ()
        assert result.saw["minus"] == 2

    def test_two_blocks_yield_two_candidates(self):
        text = PROPOSAL + (
            "\nproposal: BW_ROLE_PLANNER holds remote/gpt-4o-mini, which failed task agent\n"
            "- BW_ROLE_PLANNER=remote/gpt-4o-mini\n"
            "+ BW_ROLE_PLANNER=remote/zai-glm-flash\n"
            "why run: gpt-4o-mini failed agent (1/3 steps) — evidence.log\n"
            "why bench: zai-glm-flash passed agent at 0.93 — artifacts.json\n"
        )
        result = parse_proposals(text)
        assert not result.conflicts
        assert {c.role for c in result.candidates} == {"worker", "planner"}
        planner = next(c for c in result.candidates if c.role == "planner")
        assert planner.model == "remote/zai-glm-flash"
        assert len(planner.why) == 2  # why lines attach to their own block

    def test_why_lines_attach_to_the_last_block_not_the_first(self):
        text = (
            "proposal: BW_ROLE_WORKER holds a, which failed task code\n"
            "+ BW_ROLE_WORKER=b\n"
            "why run: a failed\n"
            "\n"
            "proposal: BW_ROLE_PLANNER holds c, which failed task agent\n"
            "+ BW_ROLE_PLANNER=d\n"
            "why run: c failed\n"
        )
        result = parse_proposals(text)
        worker = next(c for c in result.candidates if c.role == "worker")
        planner = next(c for c in result.candidates if c.role == "planner")
        assert worker.why == ("why run: a failed",)
        assert planner.why == ("why run: c failed",)

    def test_identical_duplicates_dedupe(self):
        text = PROPOSAL + "\n" + PROPOSAL
        result = parse_proposals(text)
        assert not result.conflicts
        assert len(result.candidates) == 1

    def test_conflicting_duplicates_refuse_everything(self):
        text = PROPOSAL + "\n+ BW_ROLE_WORKER=remote/gpt-4o-mini\n"
        result = parse_proposals(text)
        assert result.candidates == ()
        assert result.conflicts
        assert "worker" in result.conflicts[0]

    def test_role_prefix_and_case_are_canonicalized(self):
        result = parse_proposals("+ bw_role_Reviewer=remote/zai-glm-flash\n")
        c = result.candidates[0]
        assert (c.role, c.role_env) == ("reviewer", "BW_ROLE_REVIEWER")

    def test_saw_counts_line_kinds(self):
        result = parse_proposals(PROPOSAL)
        assert result.saw["proposal"] == 1
        assert result.saw["plus"] == 1
        assert result.saw["minus"] == 1
        assert result.saw["why"] == 2
        assert result.saw["signature"] == 1
        assert result.saw["not_applied"] == 1


class TestValidateCandidates:
    def test_happy_path_builds_the_plan(self):
        result = parse_proposals(PROPOSAL)
        changes, refusals = _validate(result.candidates)
        assert not refusals
        change = changes[0]
        assert (change.role_env, change.current, change.target) == (
            "BW_ROLE_WORKER",
            "local/llamacpp",
            "remote/zai-glm-flash",
        )
        assert not change.noop

    def test_unknown_role_refuses_naming_known_roles(self):
        result = parse_proposals("+ BW_ROLE_TECHLEAD=remote/zai-glm-flash\n")
        _, refusals = _validate(result.candidates)
        assert len(refusals) == 1
        assert "planner, worker, reviewer" in refusals[0]

    def test_unknown_model_refuses_with_registry_hint(self):
        result = parse_proposals("+ BW_ROLE_WORKER=remote/claude-opus\n")
        _, refusals = _validate(result.candidates)
        assert len(refusals) == 1
        assert "remote/claude-opus" in refusals[0]
        assert "models.yaml" in refusals[0]

    def test_noop_when_role_already_holds_the_model(self):
        result = parse_proposals("+ BW_ROLE_WORKER=local/llamacpp\n")
        changes, refusals = _validate(result.candidates)
        assert not refusals
        assert changes[0].noop is True

    def test_env_precedence_flag_when_variable_is_exported(self):
        result = parse_proposals(PROPOSAL)
        changes, _ = _validate(result.candidates, env=frozenset({"BW_ROLE_WORKER"}))
        assert changes[0].env_wins is True

    def test_runtime_roles_guard_refuses_the_whole_apply(self):
        result = parse_proposals(PROPOSAL)
        changes, refusals = _validate(result.candidates, runtime_roles=True)
        assert changes == []
        assert "runtime.yaml" in refusals[0]


class TestApplyChanges:
    def test_replaces_in_place_preserving_every_other_line(self, tmp_path):
        env = tmp_path / ".env"
        env.write_text(
            "# --- buildwright: role -> model ---\n"
            "BW_ROLE_PLANNER=local/qwen3\n"
            "BW_ROLE_WORKER=local/llamacpp\n"
            "\n"
            "# --- provider keys ---\n"
            "ZAI_API_KEY=secret\n",
            encoding="utf-8",
        )
        result = parse_proposals(PROPOSAL)
        changes, _ = _validate(result.candidates)
        changed, created = apply_changes(env, changes)
        assert (changed, created) == (1, False)
        lines = env.read_text(encoding="utf-8").splitlines()
        assert lines == [
            "# --- buildwright: role -> model ---",
            "BW_ROLE_PLANNER=local/qwen3",
            "BW_ROLE_WORKER=remote/zai-glm-flash",
            "",
            "# --- provider keys ---",
            "ZAI_API_KEY=secret",
        ]

    def test_appends_when_key_absent(self, tmp_path):
        env = tmp_path / ".env"
        env.write_text("# only keys\nZAI_API_KEY=x\n", encoding="utf-8")
        result = parse_proposals("+ BW_ROLE_WORKER=remote/zai-glm-flash\n")
        changes, _ = _validate(result.candidates)
        changed, created = apply_changes(env, changes)
        assert (changed, created) == (1, False)
        assert env.read_text(encoding="utf-8").splitlines()[-1] == "BW_ROLE_WORKER=remote/zai-glm-flash"

    def test_creates_missing_env_with_header(self, tmp_path):
        env = tmp_path / ".env"
        result = parse_proposals(PROPOSAL)
        changes, _ = _validate(result.candidates)
        changed, created = apply_changes(env, changes)
        assert (changed, created) == (1, True)
        lines = env.read_text(encoding="utf-8").splitlines()
        assert lines[0].startswith("# buildwright role routing")
        assert "BW_ROLE_WORKER=remote/zai-glm-flash" in lines

    def test_noop_changes_write_nothing(self, tmp_path):
        env = tmp_path / ".env"
        before = "BW_ROLE_WORKER=local/llamacpp\n"
        env.write_text(before, encoding="utf-8")
        result = parse_proposals("+ BW_ROLE_WORKER=local/llamacpp\n")
        changes, _ = _validate(result.candidates)
        changed, created = apply_changes(env, changes)
        assert (changed, created) == (0, False)
        assert env.read_text(encoding="utf-8") == before

    def test_leaves_no_tmp_file_behind(self, tmp_path):
        env = tmp_path / ".env"
        env.write_text("BW_ROLE_WORKER=local/llamacpp\n", encoding="utf-8")
        result = parse_proposals(PROPOSAL)
        changes, _ = _validate(result.candidates)
        apply_changes(env, changes)
        assert list(tmp_path.iterdir()) == [env]
