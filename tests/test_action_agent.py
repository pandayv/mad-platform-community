"""B1/B16: the escalation/ticket idempotency key must identify exactly one
finding within exactly one scan -- and nothing coarser.

Two users scanning the same public site used to produce identical keys.
The second user's finding was classified `already_filed` against the first
user's ticket (so it never reached their CSV), and their escalation
overwrote the first user's document wholesale: status reset to "pending",
disposition/reviewer/resolved_at discarded, job_id reassigned, and the
first user's scoped review link permanently 404'd.
"""

from __future__ import annotations

from mad_platform.agents.action_agent import (
    LOW_CONFIDENCE_THRESHOLD,
    idempotency_key,
    needs_escalation,
)
from mad_platform.agents.reporter import RankedFinding


def _finding(**kw) -> RankedFinding:
    return RankedFinding(
        page_url=kw.get("page_url", "https://example.com/contact"),
        wcag_criterion=kw.get("wcag_criterion", "1.3.1"),
        editor_rationale=kw.get("editor_rationale", "The input has no associated label."),
        editor_confidence=kw.get("editor_confidence", 0.9),
        risk_score=kw.get("risk_score", 60.0),
        severity=kw.get("severity", "high"),
        suggested_fix=kw.get("suggested_fix", "add a <label for>"),
        risk_rationale=kw.get("risk_rationale", "why"),
    )


def test_same_job_same_finding_is_stable():
    """The property the whole idempotency mechanism rests on: a retried
    pipeline step must produce the same key and therefore not double-file.
    """
    f = _finding()
    assert idempotency_key("job-a", f.page_url, f) == idempotency_key("job-a", f.page_url, f)


def test_different_jobs_do_not_collide():
    """B1. Same site, same finding, two users -- must be two keys."""
    f = _finding()
    assert idempotency_key("job-a", f.page_url, f) != idempotency_key("job-b", f.page_url, f)


def test_rationales_sharing_their_first_100_characters_do_not_collide():
    """B16. Editor's rationales for, say, three unlabeled inputs on one
    form routinely open identically; truncating at 100 chars merged them
    and the second finding silently never got a ticket or a CSV row.
    """
    shared = "The form control at this position has no programmatically associated label element, which means "
    assert len(shared) > 90
    a = _finding(editor_rationale=shared + "the email field is affected.")
    b = _finding(editor_rationale=shared + "the phone field is affected.")
    assert idempotency_key("job-a", a.page_url, a) != idempotency_key("job-a", b.page_url, b)


def test_different_pages_do_not_collide():
    f = _finding()
    assert idempotency_key("job-a", "https://example.com/a", f) != idempotency_key(
        "job-a", "https://example.com/b", f
    )


def test_different_criteria_do_not_collide():
    a = _finding(wcag_criterion="1.3.1")
    b = _finding(wcag_criterion="4.1.2")
    assert idempotency_key("job-a", a.page_url, a) != idempotency_key("job-a", b.page_url, b)


def test_key_is_a_usable_firestore_document_id():
    key = idempotency_key("job-a", "https://example.com/", _finding())
    assert len(key) == 16
    assert all(c in "0123456789abcdef" for c in key)


# --- the escalation gate ---------------------------------------------------


def test_low_confidence_escalates():
    assert needs_escalation(_finding(editor_confidence=LOW_CONFIDENCE_THRESHOLD - 0.01))


def test_confidence_exactly_at_the_threshold_does_not_escalate():
    assert not needs_escalation(_finding(editor_confidence=LOW_CONFIDENCE_THRESHOLD))


def test_critical_severity_escalates_even_at_full_confidence():
    assert needs_escalation(_finding(editor_confidence=1.0, severity="critical"))


def test_confident_non_critical_finding_is_filed_autonomously():
    assert not needs_escalation(_finding(editor_confidence=0.95, severity="high"))
