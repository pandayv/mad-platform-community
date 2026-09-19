"""T3 / B10: the escalation gate's actual routing, and the two ticket
formats that had drifted apart.

`needs_escalation` and `idempotency_key` were each tested in isolation;
their *composition* -- `route_and_file`, the function that decides whether
a finding is auto-filed or held for a human -- had no test touching it.
Neither did `resolve_escalation`, which re-implemented the ticket title
and body inline against the Firestore document instead of calling the two
helpers ten lines above it, so a field added to one would silently have
produced a different ticket format on the SME-confirmed path than on the
autonomous one.
"""

from __future__ import annotations

import pytest

from mad_platform.agents import action_agent
from mad_platform.agents.action_agent import resolve_escalation, route_and_file
from mad_platform.agents.reporter import RankedFinding
from mad_platform.severity import LOW_CONFIDENCE_THRESHOLD
from mad_platform.tools.issue_sink import MockIssueSink

JOB = "3f1c2b8a-7d44-4e9b-9c10-2a5e6f0b1d77"


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


@pytest.fixture
def fake_fs(monkeypatch):
    """A stand-in for the Firestore module, in the style
    tests/test_scan_completion.py already uses.
    """
    state = {"tickets": {}, "escalations": {}}
    monkeypatch.setattr(action_agent.fs, "get_ticket_for_finding", lambda key: state["tickets"].get(key))
    monkeypatch.setattr(
        action_agent.fs, "record_ticket_for_finding",
        lambda key, ticket_id: state["tickets"].__setitem__(key, ticket_id),
    )
    monkeypatch.setattr(
        action_agent.fs, "create_escalation",
        lambda key, data, job_id: state["escalations"].__setitem__(key, {**data, "job_id": job_id}),
    )
    return state


# --- route_and_file -------------------------------------------------------


def test_a_confident_non_critical_finding_is_filed_autonomously(fake_fs):
    sink = MockIssueSink()
    result = route_and_file(sink, [_finding()], JOB)
    assert len(result["filed"]) == 1
    assert result["escalated"] == []
    assert len(sink.created) == 1


def test_a_low_confidence_finding_is_held_for_a_human(fake_fs):
    sink = MockIssueSink()
    finding = _finding(editor_confidence=LOW_CONFIDENCE_THRESHOLD - 0.01)
    result = route_and_file(sink, [finding], JOB)
    assert len(result["escalated"]) == 1
    assert result["filed"] == []
    assert sink.created == [], "an escalated finding must not produce a ticket yet"


def test_a_critical_finding_is_held_even_at_full_confidence(fake_fs):
    """The severity half of the gate -- the one thing standing between a
    critical finding and an auto-filed ticket nobody looked at.
    """
    sink = MockIssueSink()
    result = route_and_file(sink, [_finding(severity="critical", editor_confidence=1.0)], JOB)
    assert len(result["escalated"]) == 1
    assert sink.created == []


def test_the_returned_index_matches_the_position_in_the_input(fake_fs):
    """Callers use it to pair a result back with its finding; re-deriving
    by value would be wrong for two near-identical findings on one page.
    """
    sink = MockIssueSink()
    findings = [_finding(), _finding(severity="critical"), _finding(wcag_criterion="1.1.1")]
    result = route_and_file(sink, findings, JOB)
    indices = [i for bucket in result.values() for (i, _f, _x) in bucket]
    assert sorted(indices) == [0, 1, 2]


def test_a_rerun_of_the_same_scan_does_not_double_file(fake_fs):
    """The idempotency half of the composition. Cloud Tasks retries a
    dispatch-deadline expiry, so this path is reached for real.
    """
    sink = MockIssueSink()
    findings = [_finding()]
    first = route_and_file(sink, findings, JOB)
    second = route_and_file(sink, findings, JOB)
    assert len(first["filed"]) == 1
    assert first["already_filed"] == []
    assert second["filed"] == []
    assert len(second["already_filed"]) == 1
    assert len(sink.created) == 1, "the ticket must be created exactly once"


def test_two_scans_of_the_same_site_do_not_share_a_ticket(fake_fs):
    """job_id is part of the key. Without it, user B's finding was
    classified already_filed against user A's ticket and never reached B's
    report or CSV.
    """
    sink = MockIssueSink()
    route_and_file(sink, [_finding()], JOB)
    other = route_and_file(sink, [_finding()], "8c7d6e5f-4a3b-42c1-9d0e-1f2a3b4c5d6e")
    assert len(other["filed"]) == 1
    assert len(sink.created) == 2


# --- B10: one ticket format, not two ------------------------------------


@pytest.fixture
def resolve_harness(monkeypatch):
    state = {"tickets": {}, "resolved": []}

    def _resolve(escalation_id, disposition, reviewer):
        state["resolved"].append((escalation_id, disposition, reviewer))
        return {
            "kind": "finding",
            "page_url": "https://example.com/contact",
            "wcag_criterion": "1.3.1",
            "severity": "high",
            "risk_score": 60.0,
            "editor_rationale": "The input has no associated label.",
            "editor_confidence": 0.5,
            "risk_rationale": "why",
            "suggested_fix": "add a <label for>",
        }

    monkeypatch.setattr(action_agent.fs, "resolve_escalation", _resolve)
    monkeypatch.setattr(action_agent.fs, "get_ticket_for_finding", lambda key: state["tickets"].get(key))
    monkeypatch.setattr(
        action_agent.fs, "record_ticket_for_finding",
        lambda key, ticket_id: state["tickets"].__setitem__(key, ticket_id),
    )
    return state


def test_a_confirmed_escalation_files_the_same_ticket_the_autonomous_path_would(
    fake_fs, resolve_harness
):
    """The actual B10 assertion. Both paths now go through
    _ticket_title/_ticket_description, so the only difference in the body
    is the SME suffix -- and if someone re-inlines one of them, this fails.
    """
    auto_sink = MockIssueSink()
    route_and_file(auto_sink, [_finding(editor_confidence=0.9)], JOB)
    _auto_id, auto_title, auto_body = auto_sink.created[0]

    sme_sink = MockIssueSink()
    resolve_escalation(sme_sink, "abc123", disposition="confirm", reviewer="alice")
    _sme_id, sme_title, sme_body = sme_sink.created[0]

    assert sme_title == auto_title
    assert sme_body.startswith(auto_body)
    assert sme_body == f"{auto_body}\n\n[Confirmed by SME review: alice]"


def test_a_dismissed_escalation_never_becomes_a_ticket(resolve_harness):
    sink = MockIssueSink()
    assert resolve_escalation(sink, "abc123", disposition="dismiss") is None
    assert sink.created == []


def test_confirming_twice_returns_the_existing_ticket(resolve_harness):
    sink = MockIssueSink()
    first = resolve_escalation(sink, "abc123", disposition="confirm")
    second = resolve_escalation(sink, "abc123", disposition="confirm")
    assert first == second
    assert len(sink.created) == 1


def test_an_unknown_disposition_is_refused(resolve_harness):
    with pytest.raises(ValueError):
        resolve_escalation(MockIssueSink(), "abc123", disposition="delete")


def test_a_legacy_escalation_document_files_a_ticket_instead_of_raising(monkeypatch):
    """The inline copy subscripted data['severity'], data['risk_score'] and
    four more directly, so an escalation document written by an older
    revision -- or malformed for any other reason -- raised KeyError, i.e.
    a 500 on an authenticated admin route, rather than a handled outcome.
    """
    monkeypatch.setattr(
        action_agent.fs, "resolve_escalation",
        lambda escalation_id, disposition, reviewer: {"kind": "finding", "page_url": "https://example.com/"},
    )
    monkeypatch.setattr(action_agent.fs, "get_ticket_for_finding", lambda key: None)
    monkeypatch.setattr(action_agent.fs, "record_ticket_for_finding", lambda key, t: None)

    sink = MockIssueSink()
    ticket_id = resolve_escalation(sink, "abc123", disposition="confirm")
    assert ticket_id
    _ticket, title, body = sink.created[0]
    assert "https://example.com/" in title
    assert "unknown criterion" in body


def test_an_off_vocabulary_severity_read_back_from_firestore_is_normalized(monkeypatch):
    """A value round-tripped through Firestore has not been through the
    response schema that constrains it on the way in.
    """
    monkeypatch.setattr(
        action_agent.fs, "resolve_escalation",
        lambda escalation_id, disposition, reviewer: {
            "kind": "finding", "page_url": "https://example.com/",
            "wcag_criterion": "1.3.1", "severity": "Critical", "risk_score": 90.0,
            "editor_rationale": "r", "editor_confidence": 0.4,
            "risk_rationale": "w", "suggested_fix": "f",
        },
    )
    monkeypatch.setattr(action_agent.fs, "get_ticket_for_finding", lambda key: None)
    monkeypatch.setattr(action_agent.fs, "record_ticket_for_finding", lambda key, t: None)

    sink = MockIssueSink()
    resolve_escalation(sink, "abc123", disposition="confirm")
    _ticket, title, body = sink.created[0]
    assert title.startswith("[CRITICAL]")
    assert "Severity: critical" in body
