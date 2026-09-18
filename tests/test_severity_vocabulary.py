"""B13 / X2: one severity vocabulary, validated at the model boundary.

`severity` was a plain `str` the model filled in freely. An off-vocabulary
value did four things, none of them visible:

1. Created a phantom key in the counts dict, which the donut and legend
   (which iterate the four known tiers) ignored -- so the ring total and
   the total_findings headline disagreed on the same screen.
2. Took the silent `_SCORE_WEIGHT.get(sev, 4.0)` fallback weight.
3. Walked past `action_agent.needs_escalation`'s case-sensitive
   `== "critical"`, auto-filing a critical finding with no human review --
   the one thing that gate exists to prevent.
4. Fell out of theme.SEVERITY_VAR's lookup into a muted grey rail.

And the four words themselves existed in five independent lists.
"""

from __future__ import annotations

import pathlib

import pytest
from pydantic import ValidationError

from mad_platform import severity
from mad_platform.agents import reporter
from mad_platform.agents.action_agent import needs_escalation
from mad_platform.agents.reporter import RankedFinding
from mad_platform.web import theme


def _finding(sev: str, confidence: float = 0.95) -> RankedFinding:
    return RankedFinding(
        page_url="https://example.com/",
        wcag_criterion="1.1.1",
        editor_rationale="r",
        editor_confidence=confidence,
        risk_score=50.0,
        severity=sev,
        suggested_fix="f",
        risk_rationale="why",
    )


# --- the schema boundary ----------------------------------------------------


def test_the_model_response_field_is_constrained_not_free_text():
    """A Literal both constrains generation (the SDK builds the response
    schema from it) and rejects anything else at parse time.
    """
    with pytest.raises(ValidationError):
        reporter._Recommendation(
            finding_index=0, risk_score=50.0, severity="severe",
            suggested_fix="f", risk_rationale="w",
        )


@pytest.mark.parametrize("sev", severity.SEVERITY_ORDER)
def test_every_vocabulary_value_is_accepted(sev):
    rec = reporter._Recommendation(
        finding_index=0, risk_score=50.0, severity=sev, suggested_fix="f", risk_rationale="w"
    )
    assert rec.severity == sev


def test_a_capitalized_value_is_rejected_at_the_boundary():
    """"Critical" is the specific value that used to slip past the
    escalation gate.
    """
    with pytest.raises(ValidationError):
        reporter._Recommendation(
            finding_index=0, risk_score=50.0, severity="Critical",
            suggested_fix="f", risk_rationale="w",
        )


# --- normalize --------------------------------------------------------------


@pytest.mark.parametrize("raw", ["critical", "Critical", "CRITICAL", "  critical  "])
def test_normalize_folds_case_and_whitespace(raw):
    assert severity.normalize(raw) == "critical"


def test_normalize_refuses_to_guess_by_default():
    """Silently mapping an unknown string onto a real tier is how a
    critical finding ends up counted as medium.
    """
    with pytest.raises(severity.UnknownSeverityError):
        severity.normalize("severe")


def test_normalize_honors_an_explicit_default():
    assert severity.normalize("severe", default="low") == "low"


# --- counts -----------------------------------------------------------------


def test_counts_always_carry_every_tier_even_at_zero():
    """A renderer reading counts["high"] must not depend on whether this
    particular scan happened to find one.
    """
    assert set(severity.empty_counts()) == set(severity.SEVERITY_ORDER)
    assert set(severity.count_by_severity(["low"])) == set(severity.SEVERITY_ORDER)


def test_an_unknown_value_is_counted_as_critical_not_dropped():
    counts = severity.count_by_severity(["critical", "severe", "low"])
    assert sum(counts.values()) == 3, "a finding must never vanish from the counts"
    assert counts["critical"] == 2


def test_the_counts_total_always_equals_the_number_of_findings():
    """The invariant the ring/headline disagreement violated."""
    values = ["critical", "HIGH", "medium", "low", "Critical", "nonsense", ""]
    assert sum(severity.count_by_severity(values).values()) == len(values)


# --- the escalation gate ----------------------------------------------------


def test_a_capitalized_critical_still_escalates():
    assert needs_escalation(_finding("Critical")) is True


def test_a_lowercase_critical_escalates():
    assert needs_escalation(_finding("critical")) is True


def test_a_confident_non_critical_finding_does_not_escalate():
    assert needs_escalation(_finding("high")) is False


def test_low_confidence_escalates_regardless_of_severity():
    assert needs_escalation(_finding("low", confidence=0.1)) is True


def test_an_unknown_severity_does_not_accidentally_escalate_everything():
    """The gate's default is deliberately "low", not ESCALATE_ALWAYS: an
    unparseable severity should not route every finding to human review
    and drown the queue. The counts path defaults the other way, and both
    choices are about which error is recoverable.
    """
    assert needs_escalation(_finding("nonsense")) is False


# --- one list, not five -----------------------------------------------------


def test_the_theme_reads_the_shared_vocabulary():
    assert theme._SEVERITY_ORDER == list(severity.SEVERITY_ORDER)
    assert set(theme.SEVERITY_VAR) == set(severity.SEVERITY_ORDER)


def test_the_score_weights_cover_exactly_the_vocabulary():
    assert set(reporter._SCORE_WEIGHT) == set(severity.SEVERITY_ORDER)


def test_the_status_page_javascript_does_not_carry_its_own_copy():
    """The fifth copy, and the one furthest from the others: a tier added
    in Python would have rendered in the report and silently vanished from
    the status page's donut.
    """
    source = pathlib.Path(
        __import__("mad_platform.web.app", fromlist=["x"]).__file__
    ).read_text()
    assert 'const SEV_ORDER = __SEV_ORDER__;' in source
    assert '["critical", "high", "medium", "low"]' not in source
