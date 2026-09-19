"""B2: LLM-returned list indices must never reach a list subscript unchecked.

The three failure modes these cover, all of which were live in production:
an out-of-range index crashed the whole scan with "list index out of
range"; a negative index silently wrapped and stapled a recommendation
onto the wrong finding; and a short response silently dropped findings
from the report, the tickets and the score with no log line at all.
"""

from __future__ import annotations

import logging

from pydantic import BaseModel

from mad_platform.agents.llm_validation import unanswered_indices, validate_indexed


class _Item(BaseModel):
    finding_index: int
    payload: str = ""


def _items(*indices: int) -> list[_Item]:
    return [_Item(finding_index=i, payload=f"p{i}") for i in indices]


def test_all_valid_indices_pass_through_in_order():
    items = _items(0, 1, 2)
    assert validate_indexed(items, 3, label="t") == items


def test_index_at_or_past_the_end_is_dropped():
    kept = validate_indexed(_items(0, 3, 1, 99), 3, label="t")
    assert [i.finding_index for i in kept] == [0, 1]


def test_negative_index_is_dropped_not_wrapped():
    """The invisible one: Python resolves flat[-1] happily, so this used
    to attach a fix to the LAST finding and render a perfectly clean
    report describing the wrong issue.
    """
    kept = validate_indexed(_items(-1, 0), 3, label="t")
    assert [i.finding_index for i in kept] == [0]


def test_duplicate_index_keeps_only_the_first():
    kept = validate_indexed(
        [_Item(finding_index=1, payload="first"), _Item(finding_index=1, payload="second")],
        3,
        label="t",
    )
    assert len(kept) == 1
    assert kept[0].payload == "first"


def test_empty_source_list_drops_everything():
    assert validate_indexed(_items(0), 0, label="t") == []


def test_empty_response_returns_empty():
    assert validate_indexed([], 3, label="t") == []


def test_short_response_is_logged_as_unanswered(caplog):
    with caplog.at_level(logging.WARNING, logger="mad_platform.llm_validation"):
        kept = validate_indexed(_items(0, 1), 6, label="job-abc")
    assert len(kept) == 2
    messages = " ".join(r.getMessage() for r in caplog.records)
    assert "job-abc" in messages
    assert "unanswered" in messages


def test_out_of_range_is_logged_with_the_bad_index(caplog):
    with caplog.at_level(logging.WARNING, logger="mad_platform.llm_validation"):
        validate_indexed(_items(0, 7), 2, label="job-abc")
    messages = " ".join(r.getMessage() for r in caplog.records)
    assert "7" in messages


def test_exact_match_logs_nothing(caplog):
    with caplog.at_level(logging.WARNING, logger="mad_platform.llm_validation"):
        validate_indexed(_items(0, 1, 2), 3, label="t")
    assert caplog.records == []


# --- B5: a log line was not enough; the caller has to be able to act ------


def test_unanswered_indices_reports_the_gap():
    """The first fix for a short response was a WARNING. The finding still
    vanished -- from the report, the CSV and the score -- and nobody reads
    Cloud Run WARNING logs on a free tool. This is what lets the caller
    give each one a deliberate disposition instead.
    """
    kept = validate_indexed(_items(0, 2), 4, label="t")
    assert unanswered_indices(kept, 4) == [1, 3]


def test_an_item_dropped_for_being_out_of_range_counts_as_unanswered():
    """It has to: the source item still has no answer, whatever the reason
    the model's item was discarded.
    """
    kept = validate_indexed(_items(0, 99), 2, label="t")
    assert unanswered_indices(kept, 2) == [1]


def test_an_item_dropped_as_a_duplicate_counts_as_unanswered_too():
    kept = validate_indexed(_items(0, 0), 2, label="t")
    assert unanswered_indices(kept, 2) == [1]


def test_a_complete_response_has_no_unanswered_indices():
    kept = validate_indexed(_items(0, 1, 2), 3, label="t")
    assert unanswered_indices(kept, 3) == []
