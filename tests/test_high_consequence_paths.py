"""T3: the functions with the highest consequence per line and no test.

Each of these was confirmed untested by grep across tests/ before this
file existed. They are grouped here because what they have in common is
the shape of their failure, not their subject: each one is the only thing
standing between a bad input and a bad outcome, and none of them had
anything that would notice if it stopped working.
"""

from __future__ import annotations

import asyncio
import logging

import pytest

from mad_platform.agents import editor as editor_module
from mad_platform.agents import orchestrator
from mad_platform.agents.analyst import RawFinding
from mad_platform.agents.editor import VerifiedFinding
from mad_platform.agents.reporter import RankedFinding, draft_report
from mad_platform.severity import LOW_CONFIDENCE_THRESHOLD
from mad_platform.tools import url_safety
from mad_platform.web import app as app_module


def _raw(check: str = "img_alt", criterion: str = "1.1.1", source: str = "rule") -> RawFinding:
    return RawFinding(
        source=source,
        check=check,
        wcag_criterion=criterion,
        description="d",
        selector="img",
        analyst_confidence=1.0,
    )


def _verified(index: int, criterion: str = "1.1.1", confirmed: bool = False, confidence: float = 0.95):
    return VerifiedFinding(
        finding_index=index,
        confirmed=confirmed,
        wcag_criterion=criterion,
        rationale="Editor's reasoning",
        confidence=confidence,
    )


# --- orchestrator._force_media_to_review ---------------------------------
#
# Its own 20-line comment calls this high-stakes and "not a pattern to
# casually extend", and describes a real case: a muted background video
# dismissed outright twice in a row, even after the general "confirm gray
# areas at low confidence instead of dismissing" instruction was added to
# Editor's prompt. Nothing tested it.


@pytest.mark.parametrize("criterion", ["1.2.1", "1.2.2"])
def test_a_dismissed_media_finding_is_forced_to_human_review(criterion):
    out = orchestrator._force_media_to_review(
        [_raw(check="video_captions", criterion=criterion)],
        [_verified(0, criterion=criterion, confirmed=False, confidence=0.95)],
    )
    assert out[0].confirmed is True
    assert out[0].confidence < LOW_CONFIDENCE_THRESHOLD
    assert "Routed to human review automatically" in out[0].rationale


@pytest.mark.parametrize("check", sorted(orchestrator._ALWAYS_REVIEW_CHECKS))
def test_the_carve_out_triggers_on_the_check_name_as_well_as_the_criterion(check):
    """Two independent signals, because Editor may correct Analyst's
    citation away from 1.2.x while the check that found it is still a
    media check.
    """
    out = orchestrator._force_media_to_review(
        [_raw(check=check, criterion="4.1.2")],
        [_verified(0, criterion="4.1.2", confirmed=False)],
    )
    assert out[0].confirmed is True


def test_a_confirmed_media_finding_is_left_completely_alone():
    """The comment is explicit that this only touches DISMISSED media
    findings -- forcing an already-correct confirmation through an extra
    review step is unjustified friction with no failure behind it.
    """
    original = _verified(0, criterion="1.2.2", confirmed=True, confidence=0.9)
    out = orchestrator._force_media_to_review([_raw(check="video_captions", criterion="1.2.2")], [original])
    assert out == [original]


def test_a_non_media_dismissal_is_left_alone():
    """The carve-out must not become general. Every other check keeps its
    full autonomy.
    """
    original = _verified(0, criterion="1.4.3", confirmed=False, confidence=0.95)
    out = orchestrator._force_media_to_review([_raw(check="contrast", criterion="1.4.3")], [original])
    assert out == [original]


def test_the_forced_confidence_is_never_raised():
    """min(), not a flat assignment: a media finding Editor dismissed at
    0.1 must not be promoted to 0.59.
    """
    out = orchestrator._force_media_to_review(
        [_raw(check="video_captions", criterion="1.2.2")],
        [_verified(0, criterion="1.2.2", confirmed=False, confidence=0.1)],
    )
    assert out[0].confidence == 0.1


def test_the_carve_out_list_has_not_quietly_grown():
    """"Not a pattern to casually extend" is a rule a comment cannot
    enforce. This is the thing that notices.
    """
    assert orchestrator._ALWAYS_REVIEW_CHECKS == {"video_captions", "ai_media"}
    assert orchestrator._ALWAYS_REVIEW_CRITERIA == ("1.2.1", "1.2.2")


# --- app._safe_url_or_error ----------------------------------------------
#
# assert_safe_target is well tested; this wrapper's own scheme/"://"
# normalization and its timeout branches are the SSRF entry point and were
# not.


def _safe(url: str) -> tuple[str | None, str | None]:
    return asyncio.run(app_module._safe_url_or_error(url))


@pytest.fixture(autouse=True)
def no_real_dns(monkeypatch):
    """assert_safe_target resolves the host. Stub it out so these tests
    exercise this wrapper's logic and nothing else.

    Patched on url_safety, not app_module: _safe_url_or_error calls
    url_safety.assert_safe_target_async, which runs the module's own
    assert_safe_target (looked up by name inside url_safety at call time,
    not imported into app.py) on the dedicated DNS pool.
    """
    monkeypatch.setattr(url_safety, "assert_safe_target", lambda _url: None)


@pytest.mark.parametrize(
    "typed,expected",
    [
        ("cahm.org", "https://cahm.org"),
        ("  cahm.org  ", "https://cahm.org"),
        ("cahm.org/about/team?x=1#y", "https://cahm.org/about/team?x=1#y"),
        ("http://cahm.org", "http://cahm.org"),
        ("https://cahm.org", "https://cahm.org"),
    ],
)
def test_a_bare_domain_gets_a_scheme_and_everything_else_survives_untouched(typed, expected):
    """The url field is deliberately type="text" -- a bare domain typed
    without a scheme is how most non-technical visitors type an address,
    and type="url" rejects it in the browser before submission.
    """
    assert _safe(typed) == (expected, None)


def test_a_bare_host_with_a_port_is_not_mistaken_for_a_scheme():
    """urlsplit("cahm.org:8080").scheme is "cahm.org", not "" -- RFC 3986
    genuinely cannot tell "scheme:opaque" from "host:port" without more
    context, which is why this checks for "://" instead.
    """
    assert _safe("cahm.org:8080") == ("https://cahm.org:8080", None)


@pytest.mark.parametrize("url", ["javascript://evil", "file://etc/passwd", "ftp://host/x", "data://x"])
def test_a_non_http_scheme_is_refused(url):
    result, error = _safe(url)
    assert result is None
    assert "http" in error


def test_the_scheme_check_is_case_insensitive():
    assert _safe("JAVASCRIPT://evil")[0] is None
    assert _safe("HTTPS://cahm.org") == ("HTTPS://cahm.org", None)


def test_an_unsafe_target_gets_a_message_that_leaks_nothing(monkeypatch):
    from mad_platform.tools.url_safety import UnsafeTargetError

    def _raise(_url):
        raise UnsafeTargetError("169.254.169.254 is the GCP metadata address")

    monkeypatch.setattr(url_safety, "assert_safe_target", _raise)
    result, error = _safe("http://169.254.169.254/")
    assert result is None
    assert "169.254" not in error


def test_a_slow_resolution_becomes_a_message_not_a_hang(monkeypatch):
    """wait_for is a response deadline here. The visitor gets an answer
    either way.
    """
    async def _timeout(*_a, **_k):
        raise asyncio.TimeoutError

    monkeypatch.setattr(app_module.asyncio, "wait_for", _timeout)
    result, error = _safe("cahm.org")
    assert result is None
    assert "in time" in error


# --- reporter.draft_report ------------------------------------------------
#
# Every LLM-authored string in the stored report is escaped by _esc and the
# report is served from the app's own origin. That boundary had no test.


def _ranked(**kw) -> RankedFinding:
    return RankedFinding(
        page_url=kw.get("page_url", "https://example.com/"),
        wcag_criterion=kw.get("wcag_criterion", "1.1.1"),
        editor_rationale=kw.get("editor_rationale", "r"),
        editor_confidence=kw.get("editor_confidence", 0.9),
        risk_score=kw.get("risk_score", 50.0),
        severity=kw.get("severity", "high"),
        suggested_fix=kw.get("suggested_fix", "fix"),
        risk_rationale=kw.get("risk_rationale", "why"),
    )


XSS = '<script>alert("xss")</script>'


@pytest.fixture
def offline_report(monkeypatch):
    async def _summary(*_a, **_k):
        return "A summary."

    monkeypatch.setattr("mad_platform.agents.reporter.generate_executive_summary", _summary)
    monkeypatch.setenv("MAD_APP_BASE_URL", "https://example.test")


@pytest.mark.parametrize(
    "field", ["suggested_fix", "risk_rationale", "wcag_criterion", "page_url"]
)
def test_every_rendered_llm_authored_field_reaches_the_report_escaped(offline_report, field):
    """The stored report is served from the app's own origin with
    X-Frame-Options: DENY -- so an unescaped model-authored string here
    would be stored XSS on this site, not on the scanned one.
    """
    html, _summary, _score, _counts = asyncio.run(
        draft_report("https://example.com/", [_ranked(**{field: XSS})])
    )
    assert "<script>alert" not in html
    assert "&lt;script&gt;" in html, f"{field} did not reach the report at all"


@pytest.mark.parametrize(
    "field",
    ["suggested_fix", "risk_rationale", "editor_rationale", "wcag_criterion", "page_url"],
)
def test_no_llm_authored_field_can_reach_the_report_as_live_markup(offline_report, field):
    """Broader than the test above and deliberately so: editor_rationale is
    not rendered into the report today (it feeds the exec-summary prompt
    and the ticket body instead), so asserting it appears escaped would
    assert the wrong thing. Asserting it never appears as markup holds
    either way, and keeps holding if someone starts rendering it.
    """
    html, *_ = asyncio.run(draft_report("https://example.com/", [_ranked(**{field: XSS})]))
    assert "<script>alert" not in html


def test_the_scanned_url_itself_is_escaped(offline_report):
    """It is the one string on the page the visitor chose directly."""
    html, *_ = asyncio.run(draft_report(f"https://example.com/{XSS}", [_ranked()]))
    assert "<script>alert" not in html


def test_the_executive_summary_is_escaped_too(offline_report, monkeypatch):
    async def _summary(*_a, **_k):
        return XSS

    monkeypatch.setattr("mad_platform.agents.reporter.generate_executive_summary", _summary)
    html, summary, *_ = asyncio.run(draft_report("https://example.com/", [_ranked()]))
    assert summary == XSS, "the raw summary is still returned for the email path"
    assert "<script>alert" not in html


def test_a_report_with_no_findings_still_renders(offline_report):
    html, _summary, score, counts = asyncio.run(draft_report("https://example.com/", []))
    assert score == 100
    assert sum(counts.values()) == 0
    assert "<html" in html.lower()


# --- editor._warn_if_every_rule_hit_was_dismissed ------------------------
#
# The detector for the shape a successful prompt injection would produce.
# It is the thing watching the channel B1 closed.


def test_a_clean_sweep_of_rule_hits_is_logged(caplog):
    findings = [_raw(), _raw(check="label"), _raw(check="heading_order")]
    verified = [_verified(i, confirmed=False) for i in range(3)]
    with caplog.at_level(logging.WARNING, logger="mad_platform.editor"):
        editor_module._warn_if_every_rule_hit_was_dismissed("https://evil.example/", findings, verified)
    message = " ".join(r.getMessage() for r in caplog.records)
    assert "https://evil.example/" in message
    assert "untrusted.py" in message


def test_one_dismissal_is_an_ordinary_correction_not_a_pattern(caplog):
    """A decorative image with role=presentation is the documented case
    for Editor overruling a rule hit. Warning on it would train the
    operator to ignore the warning.
    """
    with caplog.at_level(logging.WARNING, logger="mad_platform.editor"):
        editor_module._warn_if_every_rule_hit_was_dismissed(
            "https://example.com/", [_raw()], [_verified(0, confirmed=False)]
        )
    assert caplog.records == []


def test_one_surviving_rule_hit_is_enough_to_stay_quiet(caplog):
    findings = [_raw(), _raw(check="label")]
    verified = [_verified(0, confirmed=False), _verified(1, confirmed=True)]
    with caplog.at_level(logging.WARNING, logger="mad_platform.editor"):
        editor_module._warn_if_every_rule_hit_was_dismissed("https://example.com/", findings, verified)
    assert caplog.records == []


def test_ai_findings_do_not_count_toward_the_sweep(caplog):
    """Only deterministic rule checks carry the "objectively matched the
    markup" property the detector rests on -- an AI finding being
    dismissed is Analyst and Editor working as designed.
    """
    findings = [_raw(source="ai_visual"), _raw(source="ai_semantic")]
    verified = [_verified(0, confirmed=False), _verified(1, confirmed=False)]
    with caplog.at_level(logging.WARNING, logger="mad_platform.editor"):
        editor_module._warn_if_every_rule_hit_was_dismissed("https://example.com/", findings, verified)
    assert caplog.records == []


def test_it_warns_rather_than_blocking():
    """Deliberately a warning: Editor is allowed to overrule a rule hit,
    and turning a legitimate correction into a failed scan would be worse
    than the thing this watches for. Returns None, raises nothing.
    """
    findings = [_raw(), _raw(check="label")]
    verified = [_verified(i, confirmed=False) for i in range(2)]
    assert editor_module._warn_if_every_rule_hit_was_dismissed("u", findings, verified) is None
