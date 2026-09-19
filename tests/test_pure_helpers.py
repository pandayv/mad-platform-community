"""The pure functions the review named: theme.wcag_principle /
principle_counts, pattern_miner._normalize_criterion,
orchestrator._normalize_path, issue_sink._defuse_formula,
url_safety.assert_safe_target, and wcag_version.parse_wcag_versions /
split_by_release_status.

No mocking needed for any of them -- which is exactly the point: they were
untestable only because importing their modules used to require live GCP.
"""

from __future__ import annotations

import pytest

from mad_platform.agents.orchestrator import _normalize_path
from mad_platform.agents.pattern_miner import _normalize_criterion
from mad_platform.tools.issue_sink import CsvIssueSink, _defuse_formula
from mad_platform.tools.url_safety import UnsafeTargetError, assert_safe_target, is_safe_target
from mad_platform.tools.wcag_version import (
    KNOWN_RECOMMENDATIONS,
    parse_wcag_versions,
    split_by_release_status,
)
from mad_platform.web import theme

# --- theme.wcag_principle / principle_counts -------------------------------


@pytest.mark.parametrize(
    ("criterion", "principle"),
    [
        ("1.1.1", "Perceivable"),
        ("2.1.1 Keyboard", "Operable"),
        ("3.3.2", "Understandable"),
        ("4.1.2 Name, Role, Value", "Robust"),
        ("  1.4.3  ", "Perceivable"),
        ("", "Other"),
        ("unknown", "Other"),
        ("9.9.9", "Other"),
    ],
)
def test_wcag_principle(criterion, principle):
    assert theme.wcag_principle(criterion) == principle


def test_principle_counts_always_has_all_four_keys():
    counts = theme.principle_counts([])
    assert set(counts) == {"Perceivable", "Operable", "Understandable", "Robust"}
    assert sum(counts.values()) == 0


def test_principle_counts_totals_match_the_input_length():
    criteria = ["1.1.1", "1.4.3", "2.1.1", "4.1.2"]
    counts = theme.principle_counts(criteria)
    assert sum(counts.values()) == len(criteria)
    assert counts["Perceivable"] == 2


def test_principle_counts_keeps_unparseable_criteria_visible_under_other():
    """They land in an "Other" bucket rather than vanishing -- the chart
    total must not silently disagree with the headline finding count.
    """
    counts = theme.principle_counts(["1.1.1", "not-a-criterion"])
    assert sum(counts.values()) == 2
    assert counts["Other"] == 1


# --- pattern_miner._normalize_criterion ------------------------------------


@pytest.mark.parametrize(
    ("raw", "normalized"),
    [
        ("1.3.1 Info and Relationships (A)", "1.3.1"),
        ("1.3.1", "1.3.1"),
        ("  1.3.1  ", "1.3.1"),
        ("1.3.1.", "1.3.1"),
        ("4.1.2 Name, Role, Value", "4.1.2"),
    ],
)
def test_normalize_criterion_clusters_equivalent_citations(raw, normalized):
    assert _normalize_criterion(raw) == normalized


def test_normalize_criterion_makes_the_two_forms_cluster_together():
    assert _normalize_criterion("1.3.1 Info and Relationships (A)") == _normalize_criterion("1.3.1")


# --- orchestrator._normalize_path ------------------------------------------


def test_empty_path_and_root_are_the_same_page():
    assert _normalize_path("") == _normalize_path("/") == "/"


def test_normalize_path_leaves_real_paths_alone():
    assert _normalize_path("/contact") == "/contact"


# --- issue_sink._defuse_formula --------------------------------------------


@pytest.mark.parametrize("prefix", ["=", "+", "-", "@"])
def test_formula_prefixes_are_defused(prefix):
    assert _defuse_formula(f"{prefix}HYPERLINK(\"http://evil\")").startswith("'")


def test_ordinary_text_is_untouched():
    assert _defuse_formula("<img> has no alt attribute") == "<img> has no alt attribute"


def test_empty_string_is_untouched():
    assert _defuse_formula("") == ""


def test_csv_export_defuses_scanned_content():
    """Summary/Description trace back to an attacker's own page content."""
    sink = CsvIssueSink()
    sink.create_issue("=cmd|'/c calc'!A1", "-2+3")
    exported = sink.export()
    assert "'=cmd" in exported
    assert "'-2+3" in exported


def test_csv_sink_ids_are_sequential_and_opaque():
    sink = CsvIssueSink()
    assert sink.create_issue("a", "b") == "CSV-1"
    assert sink.create_issue("c", "d") == "CSV-2"


# --- url_safety.assert_safe_target -----------------------------------------


@pytest.mark.parametrize(
    "url",
    [
        "http://127.0.0.1/",
        "http://localhost/",
        "http://10.0.0.5/",
        "http://192.168.1.1/",
        "http://169.254.169.254/",  # cloud metadata endpoint
        "http://[::1]/",
    ],
)
def test_private_and_metadata_targets_are_rejected(url):
    with pytest.raises(UnsafeTargetError):
        assert_safe_target(url)


@pytest.mark.parametrize("url", ["ftp://example.com/", "file:///etc/passwd", "javascript:alert(1)", "example.com"])
def test_non_http_schemes_are_rejected(url):
    with pytest.raises(UnsafeTargetError, match="http"):
        assert_safe_target(url)


def test_unresolvable_hostname_is_rejected():
    with pytest.raises(UnsafeTargetError):
        assert_safe_target("http://this-host-does-not-exist.invalid/")


def test_ipv4_mapped_ipv6_loopback_is_caught():
    """The common bypass -- verified to be handled by is_private."""
    with pytest.raises(UnsafeTargetError):
        assert_safe_target("http://[::ffff:127.0.0.1]/")


@pytest.mark.parametrize(
    "url",
    [
        # B10: Python's ipaddress reports is_private == False for RFC 6598
        # carrier-grade NAT space, so this slipped through every other
        # check. Verified directly below so the surprising bit is pinned.
        "http://100.64.0.1/",
        "http://100.127.255.254/",
        # RFC 6890 IETF protocol assignments.
        "http://192.0.0.8/",
        "http://0.0.0.0/",
    ],
)
def test_ranges_python_does_not_flag_as_private_are_rejected_explicitly(url):
    with pytest.raises(UnsafeTargetError):
        assert_safe_target(url)


def test_cgnat_really_is_not_is_private():
    """Pins the surprise the explicit range list exists for -- if a future
    Python starts flagging it, this test says so rather than leaving a
    redundant-looking rule nobody dares delete.
    """
    import ipaddress

    assert ipaddress.ip_address("100.64.0.1").is_private is False


def test_ordinary_public_addresses_still_pass():
    assert_safe_target("http://93.184.216.34/")  # example.com's documented address
    assert_safe_target("https://1.1.1.1/")


def test_is_safe_target_is_the_boolean_form_for_the_request_guard():
    """crawler.guard_page_requests runs inside a Playwright route handler,
    where a raised exception would leave the request hanging until the
    navigation timeout.
    """
    assert is_safe_target("http://93.184.216.34/") is True
    assert is_safe_target("http://169.254.169.254/") is False


@pytest.mark.parametrize("url", ["data:image/png;base64,AAAA", "blob:https://x/y", "about:blank"])
def test_non_network_schemes_are_left_alone_by_the_request_guard(url):
    """A normal page legitimately uses data:/blob: URLs; reporting them as
    unsafe would abort inline images and blob workers for no security gain.
    """
    assert is_safe_target(url) is True


# --- wcag_version._VERSION_LINK_RE -----------------------------------------


@pytest.mark.parametrize(
    ("href", "expected"),
    [
        # The compact 2.x form W3C has used to date.
        ("/TR/WCAG22/", ["2.2"]),
        ("/TR/WCAG21/", ["2.1"]),
        ("/TR/WCAG20/", ["2.0"]),
        # B9: the hyphenated, dotted form W3C publishes WCAG 3 under. The
        # old pattern (/TR/WCAG(\d)(\d)/) missed this on four counts at
        # once -- case, hyphen, dot, and digit count -- which made the
        # whole major-version-change path unreachable for the one event it
        # was built for.
        ("/TR/wcag-3.0/", ["3.0"]),
        ("/TR/WCAG-3.0/", ["3.0"]),
        # A bare major, should W3C ever publish one that way.
        ("/TR/wcag-4/", ["4.0"]),
    ],
)
def test_parses_both_w3c_url_spellings(href, expected):
    assert parse_wcag_versions(href) == expected


def test_finds_all_versions_in_a_page_and_3_0_outranks_2_2():
    """The ordering half of B9: matching /TR/wcag-3.0/ only helps if 3.0
    then wins the max() against the 2.x links that sit on the same page.
    """
    page = '<a href="/TR/WCAG20/">2.0</a><a href="/TR/WCAG22/">2.2</a><a href="/TR/wcag-3.0/">3.0</a>'
    versions = parse_wcag_versions(page)
    assert versions == ["2.0", "2.2", "3.0"]
    assert max(versions, key=lambda v: tuple(int(p) for p in v.split("."))) == "3.0"


def test_does_not_match_unrelated_urls():
    assert parse_wcag_versions("/TR/html52/") == []
    assert parse_wcag_versions("/TR/wcag2ict/") == []


def test_an_unparseable_digit_run_is_dropped_not_guessed():
    """Three undelimited digits have no unambiguous decimal point, so the
    link is ignored rather than becoming a version number that could
    trigger a re-embed.
    """
    assert parse_wcag_versions("/TR/WCAG222/") == []


def test_regex_is_case_insensitive():
    """W3C's own URLs use both spellings (WCAG22 vs wcag-3.0), so a
    case-sensitive pattern is a latent version of the same bug.
    """
    assert parse_wcag_versions("/TR/wcag22/") == ["2.2"]
    assert parse_wcag_versions("/TR/WcAg22/") == ["2.2"]


# --- B6: a draft must not be able to nominate itself as the standard ------


def test_a_draft_version_is_not_treated_as_the_current_standard():
    """`max()` over every /TR/wcag link had no notion of Recommendation vs
    draft, while the module's docstring claimed it parsed links to "the
    current standard". W3C publishes Working Drafts under /TR/ at the same
    "latest version" URL a Recommendation eventually gets, so the first
    time this overview page links WCAG 3 draft material the check would
    have re-embedded, written "3.0" as the stored knowledge-base version,
    and fired the major-change alert -- leaving the pointer wrong and
    making the real 2.2 -> 3.0 transition a no-op when it lands.
    """
    released, unrecognized = split_by_release_status(["2.0", "2.2", "3.0"])
    assert released[-1] == "2.2"
    assert unrecognized == ["3.0"]


def test_an_unrecognized_version_is_reported_rather_than_silently_dropped():
    """Ignoring the draft link would swap one invisible failure for
    another. The scheduled check alerts a person on this.
    """
    _released, unrecognized = split_by_release_status(["2.2", "3.0", "4.0"])
    assert unrecognized == ["3.0", "4.0"]


def test_todays_real_page_shape_still_resolves_to_2_2():
    """The live page (checked 2026-09-19) links WCAG 2.2, 2.1 and 2.0, and
    reaches WCAG 3 through /WAI/standards-guidelines/wcag/wcag3-intro/,
    which this pattern does not match. The fix must not disturb that.
    """
    page = (
        '<a href="https://www.w3.org/TR/WCAG22/">WCAG 2.2 Standard</a>'
        '<a href="https://www.w3.org/TR/WCAG21/">WCAG 2.1 Standard</a>'
        '<a href="https://www.w3.org/TR/WCAG20/">WCAG 2.0</a>'
        '<a href="/WAI/standards-guidelines/wcag/wcag3-intro/">WCAG 3 Draft</a>'
        '<a href="https://www.w3.org/TR/2018/REC-WCAG21-20180605/">dated</a>'
    )
    released, unrecognized = split_by_release_status(parse_wcag_versions(page))
    assert released[-1] == "2.2"
    assert unrecognized == []


def test_the_allowlist_holds_the_versions_w3c_has_actually_published():
    assert KNOWN_RECOMMENDATIONS == ("2.0", "2.1", "2.2")


def test_a_page_with_only_unrecognized_versions_raises_rather_than_guessing(monkeypatch):
    """The safe direction. Nominating a draft as current is the failure
    this whole split exists to prevent, so if nothing known is on the page
    the scheduled tick fails loudly instead.
    """
    from mad_platform.tools import wcag_version

    monkeypatch.setattr(wcag_version.retry, "with_retry", lambda fn, **_k: '<a href="/TR/wcag-3.0/">x</a>')
    with pytest.raises(wcag_version.WCAGVersionFetchError) as exc:
        wcag_version.read_wcag_versions()
    assert "3.0" in str(exc.value)
    assert "KNOWN_RECOMMENDATIONS" in str(exc.value)


def test_a_reading_carries_both_the_current_version_and_the_drafts(monkeypatch):
    from mad_platform.tools import wcag_version

    page = '<a href="/TR/WCAG22/">a</a><a href="/TR/wcag-3.0/">b</a>'
    monkeypatch.setattr(wcag_version.retry, "with_retry", lambda fn, **_k: page)
    reading = wcag_version.read_wcag_versions()
    assert reading.current == "2.2"
    assert reading.unrecognized == ("3.0",)


def test_fetch_current_wcag_version_still_returns_a_plain_string(monkeypatch):
    """Its callers pass the result straight into fs.set_kb_version."""
    from mad_platform.tools import wcag_version

    monkeypatch.setattr(wcag_version.retry, "with_retry", lambda fn, **_k: '<a href="/TR/WCAG22/">a</a>')
    assert wcag_version.fetch_current_wcag_version() == "2.2"
