"""rule_checks.py against tests/fixtures/violations.html.

That fixture has been sitting in the repo unused since the start -- it is
a page with one deliberate instance of each violation class the
deterministic checks look for, which makes it the obvious regression net
for the non-LLM half of Analyst.

These are the checks that carry analyst_confidence=1.0 downstream because
the rule objectively matched, so a silent change in what they detect
changes what the product claims to have proven.
"""

from __future__ import annotations

from bs4 import BeautifulSoup

from mad_platform.tools.crawler import PageSnapshot
from mad_platform.tools.rule_checks import (
    _contrast_ratio,
    check_alt_text,
    check_aria_misuse,
    check_form_labels,
    check_heading_hierarchy,
    check_tab_order,
    check_video_captions,
    run_all_rule_checks,
)


def _soup(html: str) -> BeautifulSoup:
    return BeautifulSoup(html, "html.parser")


def _snapshot(html: str, samples: list[dict] | None = None) -> PageSnapshot:
    return PageSnapshot(
        url="https://example.com/",
        html=html,
        screenshot_png=b"",
        title="Test Fixture",
        text_style_samples=samples or [],
    )


# --- alt text --------------------------------------------------------------


def test_missing_alt_is_flagged_once_in_the_fixture(violations_html):
    findings = check_alt_text(_soup(violations_html))
    assert len(findings) == 1
    assert findings[0].wcag_criterion == "1.1.1"


def test_empty_alt_is_not_flagged(violations_html):
    """alt="" is a valid, intentional choice for decorative images -- the
    fixture's icon.png carries it and must stay unflagged.
    """
    selectors = " ".join(f.selector for f in check_alt_text(_soup(violations_html)))
    assert "icon" not in selectors


def test_alt_with_text_is_not_flagged():
    assert check_alt_text(_soup('<img src="a.png" alt="A cat">')) == []


# --- heading hierarchy -----------------------------------------------------


def test_skipped_heading_level_is_flagged(violations_html):
    findings = check_heading_hierarchy(_soup(violations_html))
    assert len(findings) == 1
    assert "h1 to h3" in findings[0].message


def test_first_heading_not_h1_is_flagged():
    findings = check_heading_hierarchy(_soup("<h2>Second level first</h2>"))
    assert any("not h1" in f.message for f in findings)


def test_correct_hierarchy_is_clean():
    assert check_heading_hierarchy(_soup("<h1>A</h1><h2>B</h2><h3>C</h3><h2>D</h2>")) == []


def test_page_with_no_headings_is_not_flagged():
    assert check_heading_hierarchy(_soup("<p>No headings here</p>")) == []


# --- form labels -----------------------------------------------------------


def test_unlabeled_input_is_flagged_and_labeled_one_is_not(violations_html):
    findings = check_form_labels(_soup(violations_html))
    assert len(findings) == 1
    assert "email-field" in findings[0].selector


def test_aria_label_satisfies_the_check():
    assert check_form_labels(_soup('<input type="text" aria-label="Email">')) == []


def test_wrapping_label_satisfies_the_check():
    assert check_form_labels(_soup("<label>Email <input type='text'></label>")) == []


def test_hidden_and_submit_inputs_are_skipped():
    html = '<input type="hidden" name="t"><input type="submit" value="Go">'
    assert check_form_labels(_soup(html)) == []


# --- ARIA misuse -----------------------------------------------------------


def test_aria_hidden_focusable_and_dangling_labelledby_are_both_flagged(violations_html):
    checks = check_aria_misuse(_soup(violations_html))
    messages = " ".join(f.message for f in checks)
    assert "still focusable" in messages
    assert "does-not-exist" in messages
    assert all(f.wcag_criterion == "4.1.2" for f in checks)


def test_aria_hidden_on_a_rendered_wrapper_div_is_not_flagged():
    assert check_aria_misuse(_soup('<div aria-hidden="true">decorative</div>')) == []


def test_aria_hidden_inside_a_hidden_ancestor_is_skipped():
    """The documented real case: a closed lightbox modal (display:none)
    was confirmed at 88/100 as an active keyboard trap when tabbing the
    live page never reached it. data-mad-hidden comes from the crawler's
    real computed style, so it is ground truth, not a guess.
    """
    html = '<div data-mad-hidden="true"><button aria-hidden="true">X</button></div>'
    assert check_aria_misuse(_soup(html)) == []


def test_resolvable_labelledby_is_not_flagged():
    html = '<span id="lbl">Name</span><div aria-labelledby="lbl"></div>'
    assert check_aria_misuse(_soup(html)) == []


# --- tab order -------------------------------------------------------------


def test_positive_tabindex_is_flagged(violations_html):
    findings = check_tab_order(_soup(violations_html))
    assert len(findings) == 1
    assert "tabindex=5" in findings[0].message


def test_zero_and_negative_tabindex_are_not_flagged():
    assert check_tab_order(_soup('<a href="#" tabindex="0">A</a><div tabindex="-1">B</div>')) == []


def test_non_numeric_tabindex_does_not_raise():
    assert check_tab_order(_soup('<a href="#" tabindex="abc">A</a>')) == []


# --- contrast --------------------------------------------------------------


def test_black_on_white_is_the_maximum_ratio():
    ratio = _contrast_ratio("rgb(0, 0, 0)", "rgb(255, 255, 255)")
    assert round(ratio, 1) == 21.0


def test_identical_colors_have_no_contrast():
    assert _contrast_ratio("rgb(50, 50, 50)", "rgb(50, 50, 50)") == 1.0


def test_unparseable_color_returns_none_rather_than_raising():
    assert _contrast_ratio("currentColor", "rgb(255, 255, 255)") is None


def test_low_contrast_sample_is_flagged_and_normal_text_is_not():
    from mad_platform.tools.rule_checks import check_contrast

    samples = [
        {"tag": "p", "text": "faint", "color": "rgb(153, 153, 153)",
         "backgroundColor": "rgb(170, 170, 170)", "fontSizePx": 14, "fontWeight": "400"},
        {"tag": "p", "text": "fine", "color": "rgb(0, 0, 0)",
         "backgroundColor": "rgb(255, 255, 255)", "fontSizePx": 16, "fontWeight": "400"},
    ]
    findings = check_contrast(_snapshot("<html></html>", samples))
    assert len(findings) == 1
    assert "faint" in findings[0].message


def test_large_text_uses_the_lower_three_to_one_threshold():
    from mad_platform.tools.rule_checks import check_contrast

    # 3.36:1 -- fails the 4.5 normal-text threshold, passes the 3.0 large one.
    sample = {"tag": "h2", "text": "big", "color": "rgb(140, 140, 140)",
              "backgroundColor": "rgb(255, 255, 255)", "fontSizePx": 24, "fontWeight": "400"}
    assert check_contrast(_snapshot("<html></html>", [sample])) == []

    small = {**sample, "fontSizePx": 14}
    assert len(check_contrast(_snapshot("<html></html>", [small]))) == 1


# --- video captions --------------------------------------------------------


def test_video_without_captions_is_flagged():
    findings = check_video_captions(_soup("<video src='v.mp4'></video>"))
    assert len(findings) == 1
    assert findings[0].wcag_criterion == "1.2.2"


def test_video_with_a_caption_track_is_not_flagged():
    html = '<video src="v.mp4"><track kind="captions" src="c.vtt"></video>'
    assert check_video_captions(_soup(html)) == []


def test_video_with_only_a_descriptions_track_is_still_flagged():
    html = '<video src="v.mp4"><track kind="descriptions" src="d.vtt"></video>'
    assert len(check_video_captions(_soup(html))) == 1


# --- the whole suite -------------------------------------------------------


def test_run_all_rule_checks_finds_every_seeded_violation(violations_html):
    """One deliberate instance of each class in the fixture: missing alt,
    skipped heading, unlabeled input, positive tabindex, and two ARIA
    misuses (aria-hidden focusable + dangling aria-labelledby).
    """
    found = {f.check for f in run_all_rule_checks(_snapshot(violations_html))}
    assert found == {"alt_text", "heading_hierarchy", "form_label", "tab_order", "aria_misuse"}


def test_run_all_rule_checks_is_clean_on_an_accessible_page():
    html = """<html lang="en"><body>
      <h1>Title</h1><h2>Section</h2>
      <img src="a.png" alt="A description">
      <label for="e">Email</label><input type="text" id="e">
    </body></html>"""
    assert run_all_rule_checks(_snapshot(html)) == []
