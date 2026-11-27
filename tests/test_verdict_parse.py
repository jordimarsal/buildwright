"""Unit tests: the prompted-JSON verdict fallback for local models (no network).

llama.cpp/Strata refuse `response_format` together with tools and often answer
the tool-mode `final_result` call as plain text, so the reviewer retries with a
text agent and parses the LAST JSON object of the answer itself.
"""

from __future__ import annotations

import pytest

from buildwright.loop import ReviewVerdict, _parse_verdict


def test_parse_verdict_takes_the_last_json_object():
    text = (
        'draft thought {"verdict": "APPROVED"}\n'
        'final {"verdict": "CHANGES_REQUESTED", "summary": "x"}'
    )
    verdict = _parse_verdict(text, ReviewVerdict)
    assert verdict.verdict == "CHANGES_REQUESTED"
    assert verdict.summary == "x"


def test_parse_verdict_skips_objects_that_do_not_validate():
    text = 'junk {"verdict": 1} and then {"verdict": "APPROVED", "summary": "ok"}'
    verdict = _parse_verdict(text, ReviewVerdict)
    assert verdict.verdict == "APPROVED"
    assert verdict.summary == "ok"


def test_parse_verdict_raises_when_no_valid_object():
    with pytest.raises(RuntimeError, match="no valid ReviewVerdict"):
        _parse_verdict("the model only wrote prose", ReviewVerdict)
