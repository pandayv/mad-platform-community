"""Pure scoring/ranking functions from reporter.py, plus the B2 boundary
validation as rank_and_recommend actually applies it.
"""

from __future__ import annotations

import pytest

from mad_platform.agents import reporter
from mad_platform.agents.editor import VerifiedFinding
from mad_platform.agents.reporter import RankedFinding, compute_score, score_color
from mad_platform.web import theme


def _finding(severity: str, **kw) -> RankedFinding:
    return RankedFinding(
        page_url=kw.get("page_url", "https://example.com/"),
        wcag_criterion=kw.get("wcag_criterion", "1.1.1"),
        editor_rationale=kw.get("editor_rationale", "rationale"),
        editor_confidence=kw.get("editor_confidence", 0.9),
        risk_score=kw.get("risk_score", 50.0),
        severity=severity,
        suggested_fix=kw.get("suggested_fix", "fix"),
        risk_rationale=kw.get("risk_rationale", "why"),
    )


def test_perfect_score_with_no_findings():
    assert compute_score([]) == 100


def test_score_is_clamped_to_zero_not_negative():
    assert compute_score([_finding("critical") for _ in range(50)]) == 0


def test_sqrt_weighting_flattens_after_the_first_finding():
    """The documented point of the sqrt curve: 4 criticals cost about 2x
    one critical, not 4x -- which is what makes "fix these 2 and you're at
    90" an honest claim rather than a score that only reads as broken or
    perfect.
    """
    one = 100 - compute_score([_finding("critical")])
    four = 100 - compute_score([_finding("critical") for _ in range(4)])
    assert four == pytest.approx(2 * one, abs=1)


def test_severity_case_is_normalized():
    assert compute_score([_finding("CRITICAL")]) == compute_score([_finding("critical")])


def test_unknown_severity_is_counted_as_critical_not_dropped():
    """B13: an off-vocabulary severity must not crash scoring, must not
    vanish (which would make the ring total disagree with the headline
    count), and must not be quietly treated as something milder than it
    might be. It is counted as critical -- over-reporting severity is
    recoverable, under-reporting it is the failure that matters.
    """
    assert compute_score([_finding("severe")]) == compute_score([_finding("critical")])


@pytest.mark.parametrize(
    ("score", "expected"),
    [
        (100, "var(--ok)"),
        (80, "var(--ok)"),
        (79, "var(--med)"),
        (50, "var(--med)"),
        (49, "var(--crit)"),
        (0, "var(--crit)"),
    ],
)
def test_score_color_thresholds(score, expected):
    """F6: a CSS custom property, not a literal hex. The hex version was a
    fourth copy of the palette that matched none of the theme tokens and
    kept its light-mode value in dark mode -- on the largest number in the
    report.
    """
    assert score_color(score) == expected


@pytest.mark.parametrize(
    ("score", "token"), [(100, "--ok"), (79, "--med"), (0, "--crit")]
)
def test_email_score_color_is_the_light_hex_of_the_same_token(score, token):
    """The email needs a literal (Gmail strips <style>), but it must be the
    literal *of the same token* the web dial uses -- not a second palette.
    """
    assert reporter.score_color_hex(score) == theme.LIGHT_HEX[token]
    assert score_color(score) == f"var({token})"


def test_email_severity_colors_come_from_the_shared_palette():
    for sev, token in theme.SEVERITY_TOKEN.items():
        assert reporter._EMAIL_SEVERITY_COLOR[sev] == theme.LIGHT_HEX[token]


def test_score_color_boundaries_are_inclusive_at_80_and_50():
    assert score_color(80) != score_color(79)
    assert score_color(50) != score_color(49)


# --- B2 at the Reporter boundary -------------------------------------------


class _FakeResponse:
    def __init__(self, recommendations):
        self.recommendations = recommendations


def _recommendation(index: int, severity: str = "high"):
    return reporter._Recommendation(
        finding_index=index,
        risk_score=70.0,
        severity=severity,
        suggested_fix="add alt text",
        risk_rationale="screen reader users can't tell what this is",
    )


def _confirmed(n: int) -> dict[str, list[VerifiedFinding]]:
    return {
        "https://example.com/": [
            VerifiedFinding(
                finding_index=i, confirmed=True, wcag_criterion=f"1.1.{i}", rationale=f"r{i}", confidence=0.9
            )
            for i in range(n)
        ]
    }


async def test_rank_and_recommend_drops_out_of_range_index(monkeypatch):
    """Used to raise IndexError, fail the whole scan, and show the user
    "Scan failed: list index out of range".
    """
    async def fake_generate(*_args, **_kwargs):
        return _FakeResponse([_recommendation(0), _recommendation(99)])

    monkeypatch.setattr(reporter, "generate_structured", fake_generate)
    ranked = await reporter.rank_and_recommend(_confirmed(2))
    assert len(ranked) == 1
    assert ranked[0].wcag_criterion == "1.1.0"


async def test_rank_and_recommend_drops_negative_index(monkeypatch):
    """Used to silently attach this recommendation to the LAST finding."""
    async def fake_generate(*_args, **_kwargs):
        return _FakeResponse([_recommendation(-1)])

    monkeypatch.setattr(reporter, "generate_structured", fake_generate)
    assert await reporter.rank_and_recommend(_confirmed(3)) == []


async def test_rank_and_recommend_dedupes_repeated_index(monkeypatch):
    async def fake_generate(*_args, **_kwargs):
        return _FakeResponse([_recommendation(0), _recommendation(0, severity="low")])

    monkeypatch.setattr(reporter, "generate_structured", fake_generate)
    ranked = await reporter.rank_and_recommend(_confirmed(2))
    assert [r.severity for r in ranked] == ["high"]


async def test_rank_and_recommend_sorts_by_risk_score_descending(monkeypatch):
    async def fake_generate(*_args, **_kwargs):
        low = _recommendation(0)
        low.risk_score = 10.0
        high = _recommendation(1)
        high.risk_score = 90.0
        return _FakeResponse([low, high])

    monkeypatch.setattr(reporter, "generate_structured", fake_generate)
    ranked = await reporter.rank_and_recommend(_confirmed(2))
    assert [r.risk_score for r in ranked] == [90.0, 10.0]


async def test_rank_and_recommend_short_circuits_with_no_findings(monkeypatch):
    async def fail(*_args, **_kwargs):  # pragma: no cover - must never run
        raise AssertionError("should not call the model with nothing to rank")

    monkeypatch.setattr(reporter, "generate_structured", fail)
    assert await reporter.rank_and_recommend({}) == []
