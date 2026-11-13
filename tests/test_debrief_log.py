"""Conformance tests for the debrief evidence writer.

The regexes and the result-consistency rules below are mirrored from
debrief's ``BuildwrightLog`` adapter (its ADR-0002): if buildwright's emitted
format ever drifts, these tests fail here — before debrief refuses the log.
"""

import re

from buildwright.debrief_log import DebriefSink, engine_build, find_open_episode, sanitize

# mirrored from debrief/src/debrief/adapters/buildwright_log.py
_STEP_RE = re.compile(
    r"^step:\s*(?P<ordinal>\d+)\s+"
    r"role=(?P<role>\S+)\s+"
    r"model=(?P<model>\S+)\s+"
    r"outcome=(?P<outcome>\S+)\s+"
    r'reason="(?P<reason>[^"]*)"\s+'
    r"evidence=(?P<evidence>\S+)$"
)
_RESULT_RE = re.compile(r"^(?P<outcome>\S+)(?:\s+failed_steps=(?P<failed_steps>\S*))?$")


def mk_sink(tmp_path, feature="subtract"):
    return DebriefSink(
        tmp_path / "evidence" / feature / "exec20261113T1200Z" / f"{feature}.log",
        feature=feature,
        engine="buildwright-0.8.5",
        roles={"planner": "remote/zai-glm-flash",
               "worker": "remote/zai-glm-flash",
               "reviewer": "remote/zai-glm-flash"},
        run_id=f"{feature}-test",
    )


class TestDebriefSink:
    def test_headers_written_at_creation(self, tmp_path):
        sink = mk_sink(tmp_path)
        lines = sink.path.read_text(encoding="utf-8").splitlines()
        assert lines[1] == "run: subtract-test"
        assert lines[2] == "engine_build: buildwright-0.8.5"
        assert lines[3] == "task: subtract"
        assert "BW_ROLE_WORKER=remote/zai-glm-flash" in lines[4]

    def test_steps_stream_and_conform(self, tmp_path):
        sink = mk_sink(tmp_path)
        sink.step("spec-author", "remote/zai-glm-flash", "pass",
                  'trio written: says "hi"\nand more', "harness/specs/subtract/design.md")
        sink.step("worker", "remote/zai-glm-flash", "fail",
                  "verify: incomplete — T2 not marked", "harness/progress/impl_subtract.md")
        lines = sink.path.read_text(encoding="utf-8").splitlines()
        steps = [line for line in lines if line.startswith("step:")]
        assert len(steps) == 2
        for line in steps:
            assert _STEP_RE.match(line), line
        # sanitized: single line, no double quotes
        assert 'reason="trio written: says \'hi\' and more"' in steps[0]

    def test_result_pass_when_no_step_failed(self, tmp_path):
        sink = mk_sink(tmp_path)
        sink.step("worker", "m", "pass", "verify ok", "evidence.md")
        assert sink.finish() == "pass"
        assert sink.path.read_text(encoding="utf-8").splitlines()[-1] == "result: pass"

    def test_result_fail_lists_exactly_the_failed_ordinals(self, tmp_path):
        sink = mk_sink(tmp_path)
        sink.step("worker", "m", "fail", "verify: incomplete", "impl.md")
        sink.step("worker", "m", "pass", "verify ok", "impl.md")
        sink.step("reviewer", "m", "fail", "review: CHANGES_REQUESTED", "review.md")
        assert sink.finish() == "fail failed_steps=1,3"
        line = sink.path.read_text(encoding="utf-8").splitlines()[-1]
        assert line == "result: fail failed_steps=1,3"
        assert _RESULT_RE.fullmatch(line.removeprefix("result: "))

    def test_finish_without_steps_writes_no_result(self, tmp_path):
        sink = mk_sink(tmp_path)
        assert sink.finish() is None
        assert not any(
            line.startswith("result:")
            for line in sink.path.read_text(encoding="utf-8").splitlines()
        )

    def test_invalid_outcome_fails_the_writer_not_the_reader(self, tmp_path):
        sink = mk_sink(tmp_path)
        try:
            sink.step("worker", "m", "PASS", "ok", "e.md")
        except ValueError:
            pass
        else:
            raise AssertionError("PASS must be rejected: debrief only knows pass/fail")

    def test_empty_reason_rejected(self, tmp_path):
        sink = mk_sink(tmp_path)
        try:
            sink.step("worker", "m", "pass", "   \n  ", "e.md")
        except ValueError:
            pass
        else:
            raise AssertionError("an empty reason would forge an evidence line")

    def test_double_finish_is_a_no_op(self, tmp_path):
        sink = mk_sink(tmp_path)
        sink.step("worker", "m", "pass", "ok", "e.md")
        assert sink.finish() == "pass"
        assert sink.finish() is None
        assert sum(1 for l in sink.path.read_text().splitlines()
                   if l.startswith("result:")) == 1


class TestResume:
    def test_resume_continues_ordinals_and_result_covers_history(self, tmp_path):
        first = mk_sink(tmp_path)
        first.step("worker", "m", "fail", "verify: incomplete", "impl.md")
        # a later invocation re-opens the same episode file (parked → approved)
        second = mk_sink(tmp_path)
        second.step("worker", "m", "pass", "verify ok", "impl.md")
        assert second.finish() == "fail failed_steps=1"
        text = second.path.read_text(encoding="utf-8")
        assert text.count("step:") == 2  # headers were not rewritten
        assert "step: 02" in text
        assert text.splitlines()[-1] == "result: fail failed_steps=1"

    def test_finished_episode_is_not_reopened(self, tmp_path):
        first = mk_sink(tmp_path)
        first.step("worker", "m", "pass", "ok", "e.md")
        first.finish()
        second = mk_sink(tmp_path)
        assert second.finish() is None
        assert sum(1 for l in second.path.read_text().splitlines()
                   if l.startswith("result:")) == 1


class TestFindOpenEpisode:
    def test_returns_the_unfinished_episode(self, tmp_path):
        base = tmp_path / "evidence"
        done = base / "proj" / "exec20261113T1200Z"
        open_ep = base / "proj" / "exec20261113T1300Z"
        done.mkdir(parents=True)
        open_ep.mkdir(parents=True)
        (done / "subtract.log").write_text("step: 01 ...\nresult: pass\n")
        (open_ep / "subtract.log").write_text("step: 01 ...\n")
        found = find_open_episode(base, "proj", "subtract")
        assert found == open_ep / "subtract.log"

    def test_none_when_every_episode_is_closed(self, tmp_path):
        base = tmp_path / "evidence"
        done = base / "proj" / "exec20261113T1200Z"
        done.mkdir(parents=True)
        (done / "subtract.log").write_text("result: pass\n")
        assert find_open_episode(base, "proj", "subtract") is None


class TestHelpers:
    def test_engine_build_is_one_token(self):
        assert " " not in engine_build()
        assert engine_build().startswith("buildwright-")

    def test_sanitize(self):
        assert sanitize('a "b"\n c\r\nd') == "a 'b' c d"
