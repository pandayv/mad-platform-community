"""B5, Editor half: a finding Editor did not answer about must not vanish.

`validate_indexed` returns only what the model returned. Its own module
docstring named this failure mode -- "findings the model simply did not
mention vanish from the report, the tickets and the score" -- and the fix
that followed added a log line. The finding still vanished.

A rule-check hit is an objective markup fact: an `<img>` either carries an
`alt` attribute or it does not. One that Editor's response happens not to
enumerate was not confirmed, not dismissed, not escalated -- it was absent,
and the user was told nothing was found there. For a product whose core
claim is that every finding is independently verified before it is shown,
that is a correctness failure at the level of the claim.
"""

from __future__ import annotations

import logging

from mad_platform.agents.action_agent import needs_escalation
from mad_platform.agents.analyst import RawFinding
from mad_platform.agents.editor import VerifiedFinding, _default_for_unanswered
from mad_platform.agents.reporter import RankedFinding
from mad_platform.severity import LOW_CONFIDENCE_THRESHOLD

URL = "https://example.com/"


def _raw(index: int, source: str = "rule") -> RawFinding:
    return RawFinding(
        source=source,
        check="img_alt",
        wcag_criterion=f"1.1.{index}",
        description=f"image {index} has no alt attribute",
        selector=f"img:nth-of-type({index})",
        analyst_confidence=1.0,
    )


def _verified(index: int, confirmed: bool = True) -> VerifiedFinding:
    return VerifiedFinding(
        finding_index=index,
        confirmed=confirmed,
        wcag_criterion=f"1.1.{index}",
        rationale="checked",
        confidence=0.9,
    )


def test_an_unanswered_finding_is_kept_rather_than_dropped():
    findings = [_raw(0), _raw(1), _raw(2)]
    out = _default_for_unanswered(URL, findings, [_verified(0)])
    assert [v.finding_index for v in out] == [0, 1, 2]


def test_the_default_disposition_routes_it_to_a_person():
    """Confirmed, below the escalation threshold -- the disposition that
    neither asserts a violation nor issues a clean bill of health nothing
    verified. Same mechanism as orchestrator._force_media_to_review.
    """
    out = _default_for_unanswered(URL, [_raw(0)], [])
    assert len(out) == 1
    assert out[0].confirmed is True
    assert out[0].confidence < LOW_CONFIDENCE_THRESHOLD


def test_the_placeholder_actually_clears_the_real_escalation_gate():
    """Asserting the confidence number is not the same as asserting the
    behaviour. This runs the gate that decides whether a human ever sees
    it.
    """
    placeholder = _default_for_unanswered(URL, [_raw(0)], [])[0]
    ranked = RankedFinding(
        page_url=URL,
        wcag_criterion=placeholder.wcag_criterion,
        editor_rationale=placeholder.rationale,
        editor_confidence=placeholder.confidence,
        risk_score=50.0,
        severity="medium",
        suggested_fix="",
        risk_rationale="",
    )
    assert needs_escalation(ranked)


def test_the_rationale_says_no_judgment_was_made():
    """It must not be possible to read a placeholder as an Editor
    conclusion. The report, the ticket and the review queue all surface
    this string.
    """
    out = _default_for_unanswered(URL, [_raw(0)], [])
    assert "Not verified" in out[0].rationale
    assert "did not include this finding" in out[0].rationale


def test_analysts_own_description_is_carried_through():
    """So the person who has to review it knows what was flagged."""
    out = _default_for_unanswered(URL, [_raw(7)], [])
    assert "image 7 has no alt attribute" in out[0].rationale
    assert out[0].wcag_criterion == "1.1.7"


def test_a_complete_response_is_returned_untouched():
    verified = [_verified(0), _verified(1)]
    assert _default_for_unanswered(URL, [_raw(0), _raw(1)], verified) == verified


def test_a_dismissal_is_an_answer_and_is_left_alone():
    """Editor is allowed to dismiss. This only fills silence, never
    overrides a decision.
    """
    dismissed = _verified(0, confirmed=False)
    out = _default_for_unanswered(URL, [_raw(0)], [dismissed])
    assert out == [dismissed]


def test_the_result_stays_ordered_by_finding_index():
    """_process_page zips raw_findings and verified back together by index;
    several consumers also assume report order. An appended placeholder
    must not scramble either.
    """
    out = _default_for_unanswered(URL, [_raw(i) for i in range(5)], [_verified(3)])
    assert [v.finding_index for v in out] == [0, 1, 2, 3, 4]


def test_the_fill_is_logged_with_the_page_and_the_indices(caplog):
    with caplog.at_level(logging.WARNING, logger="mad_platform.editor"):
        _default_for_unanswered(URL, [_raw(0), _raw(1)], [_verified(0)])
    message = " ".join(r.getMessage() for r in caplog.records)
    assert URL in message
    assert "[1]" in message


def test_nothing_is_logged_when_there_is_nothing_to_fill(caplog):
    with caplog.at_level(logging.WARNING, logger="mad_platform.editor"):
        _default_for_unanswered(URL, [_raw(0)], [_verified(0)])
    assert caplog.records == []
