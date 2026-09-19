"""F6 / X2: one palette, mirrored -- not four independent copies.

The severity/score colors existed in four places: the CSS custom properties
in THEME_CSS (the source of truth), theme.SEVERITY_VAR (correctly
indirecting to them), reporter._EMAIL_SEVERITY_COLOR (literal hex, and
legitimately so -- Gmail strips <style> blocks), and reporter.score_color
(literal hex, with no such justification, and a shade off on all three
values: #15803D against --ok's #157A4F). Being a literal also meant the
score dial kept its light-mode green on a near-black dark-mode background,
on the largest number in the report.

The email one still has to be literal hex, so a duplicate is unavoidable.
What is avoidable is an *unchecked* duplicate. These tests parse the :root
block out of THEME_CSS and assert theme.LIGHT_HEX matches it, so editing a
token in the CSS without editing the mirror fails here instead of shipping
two slightly different greens.
"""

from __future__ import annotations

import re

import pytest

from mad_platform.agents import reporter
from mad_platform.severity import SEVERITY_ORDER
from mad_platform.web import theme


def _root_block() -> str:
    """The first :root { ... } block -- the light-mode palette. The dark
    overrides live in a later, media-query-guarded block.
    """
    return theme.THEME_CSS.split(":root {", 1)[1].split("}", 1)[0]


@pytest.mark.parametrize("token", sorted(theme.LIGHT_HEX))
def test_light_hex_mirrors_the_css_token_exactly(token):
    match = re.search(re.escape(token) + r":\s*([^;]+);", _root_block())
    assert match, f"{token} is in theme.LIGHT_HEX but not defined in THEME_CSS's :root block"
    assert match.group(1).strip().upper() == theme.LIGHT_HEX[token].upper()


def test_every_severity_has_a_token_and_a_hex():
    assert set(theme.SEVERITY_TOKEN) == set(SEVERITY_ORDER)
    for token in theme.SEVERITY_TOKEN.values():
        assert token in theme.LIGHT_HEX


def test_severity_var_is_derived_from_the_tokens_not_retyped():
    for sev, token in theme.SEVERITY_TOKEN.items():
        assert theme.SEVERITY_VAR[sev] == f"var({token})"


def test_score_color_returns_a_custom_property_not_a_hex():
    """The regression: a literal hex here does not react to dark mode."""
    for score in (100, 80, 79, 50, 49, 0):
        value = reporter.score_color(score)
        assert value.startswith("var(--") and value.endswith(")")
        assert "#" not in value


def test_the_score_dial_and_the_email_agree_on_the_band():
    """Two renderings of the same number must not disagree about which
    band it falls in, which is what four independent copies risked.
    """
    for score in range(0, 101):
        token = reporter.score_color(score)[4:-1]
        assert reporter.score_color_hex(score) == theme.LIGHT_HEX[token]


def test_no_hardcoded_severity_hex_left_in_the_theme_helpers():
    """theme.py's chart helpers must emit var(...) so a downloaded report
    follows the reader's theme; a literal would freeze it light-mode.
    """
    donut = theme.severity_donut_svg({"critical": 1, "high": 0, "medium": 2, "low": 0})
    assert "#" not in donut, "the donut must render from custom properties, not literal hex"


# --- F5: the fixed-background surfaces must not use theme-reactive tokens --


def _dark_block() -> str:
    """The media-query-guarded dark override block."""
    return theme.THEME_CSS.split('@media (prefers-color-scheme: dark)', 1)[1].split("}", 1)[0]


@pytest.mark.parametrize("token", sorted(theme.DARK_HEX))
def test_dark_hex_mirrors_the_css_token_exactly(token):
    """Same mirror guarantee LIGHT_HEX has. Without it the hero's severity
    dots -- which now carry these values as literals, because the panel
    they sit on is fixed dark in both themes -- would silently drift from
    the palette the rest of the site uses.
    """
    match = re.search(re.escape(token) + r":\s*([^;]+);", _dark_block())
    assert match, f"{token} is in theme.DARK_HEX but not defined in the dark override block"
    assert match.group(1).strip().upper() == theme.DARK_HEX[token].upper()


def test_every_severity_has_a_dark_hex_too():
    for token in theme.SEVERITY_TOKEN.values():
        assert token in theme.DARK_HEX


def _relative_luminance(hex_value: str) -> float:
    channels = [int(hex_value.lstrip("#")[i : i + 2], 16) / 255 for i in (0, 2, 4)]
    linear = [c / 12.92 if c <= 0.04045 else ((c + 0.055) / 1.055) ** 2.4 for c in channels]
    return 0.2126 * linear[0] + 0.7152 * linear[1] + 0.0722 * linear[2]


def _contrast(a: str, b: str) -> float:
    la, lb = _relative_luminance(a), _relative_luminance(b)
    hi, lo = max(la, lb), min(la, lb)
    return (hi + 0.05) / (lo + 0.05)


# The hero panel's row background: rgba(255,255,255,0.05) composited over
# the panel's fixed #12181A. Computed once here rather than re-derived.
_HERO_ROW_BG = "#1E2425"
_HERO_CHIP_BG = "#FFFFFF"
# WCAG 1.4.11: non-text UI components need 3:1 against adjacent colour.
_MIN_UI_CONTRAST = 3.0


@pytest.mark.parametrize("severity", sorted(SEVERITY_ORDER))
def test_the_hero_severity_dots_are_legible_on_their_fixed_dark_panel(severity):
    """The regression: these were var(--crit)/(--high)/(--med)/(--low) on a
    panel that is #12181A whatever the page theme, so in the DEFAULT light
    theme they resolved to the light palette and measured 2.11:1 (low) and
    2.54:1 (critical) -- under the minimum, in the hero of an
    accessibility product, three sections above a comparison table
    claiming the tool catches contrast problems the competition misses.
    """
    token = theme.SEVERITY_TOKEN[severity]
    assert _contrast(theme.DARK_HEX[token], _HERO_ROW_BG) >= _MIN_UI_CONTRAST


@pytest.mark.parametrize("token,expected_min", [("--ok", 3.0), ("--crit", 3.0)])
def test_the_hero_chip_icons_are_legible_on_their_fixed_white_chip(token, expected_min):
    """`.example-chip.ok svg` used var(--ok), which is #57D79A in dark mode
    -- 1.81:1 on this permanently-white chip, i.e. an effectively invisible
    green check on "Alt text found" for every dark-mode visitor. The exact
    symptom the comment directly above that rule describes as fixed.
    """
    assert _contrast(theme.LIGHT_HEX[token], _HERO_CHIP_BG) >= expected_min


def test_no_theme_reactive_token_is_used_in_the_fixed_hero_css_rules():
    """A test on the values alone would not stop a fifth one growing back.
    This one pins the rule: no `.example-*` declaration may resolve through
    var(). Comments are stripped first -- they discuss var(--ink) and
    var(--ok) at length precisely because those were the bugs.
    """
    css = re.sub(r"/\*.*?\*/", "", theme.THEME_CSS, flags=re.DOTALL)
    offenders = [
        rule.strip()
        for rule in css.split("}")
        if ".example-" in rule.split("{", 1)[0] and "var(--" in rule
    ]
    assert not offenders, offenders


def test_the_hero_markup_carries_no_var_tokens_either():
    """The other half of the same rule: the dots are inline styles in
    app.py, not CSS, so the CSS-side assertion above cannot see them.
    """
    import pathlib

    from mad_platform.web import app as app_module

    source = pathlib.Path(app_module.__file__).read_text()
    hero = source.split('<div class="hero-visual"', 1)[1].split("lockup-caption", 1)[0]
    hero = re.sub(r"<!--.*?-->", "", hero, flags=re.DOTALL)
    assert "var(--" not in hero, "the fixed hero panel must not resolve theme-reactive tokens"


# --- F4: the brand button styling is opt-in, not inherited by every button -


def test_the_base_button_rule_is_scoped_to_a_class():
    """A bare `button` element selector in a 1,119-line shared stylesheet
    reaches every <button> on every surface. It leaked a drop shadow, an
    inset highlight and a white gradient wash onto .link-btn (the "Resend
    code" control, meant to look like a plain link) and .nav-toggle (the
    hamburger on every page's mobile nav). Two of the four consumers had
    been given a reset; the leak itself was never closed.
    """
    for selector in ("\nbutton, .btn {", "\nbutton::before,", "\nbutton:hover,"):
        assert selector not in theme.THEME_CSS, f"{selector.strip()} is back"
    assert "\n.btn {" in theme.THEME_CSS
    assert "\n.btn::before {" in theme.THEME_CSS


def test_no_bare_button_element_selector_anywhere_in_the_stylesheet():
    """The general form. Matches a rule head that starts with the bare
    element name, so `.nav-toggle svg` and `.btn` are fine.
    """
    offenders = [
        line.strip()
        for line in theme.THEME_CSS.splitlines()
        if re.match(r"^\s*button[\s,:{]", line)
    ]
    assert not offenders, offenders


def test_every_button_in_the_app_opts_in_or_resets_deliberately():
    """The other half: a <button> that wants the brand look now has to say
    so. The two that deliberately do not (.link-btn, .nav-toggle) are the
    ones this whole finding was about.
    """
    import pathlib

    from mad_platform.web import app as app_module

    source = pathlib.Path(app_module.__file__).read_text()
    for tag in re.findall(r"<button[^>]*>", source):
        classes = re.search(r'class="([^"]*)"', tag)
        names = set(classes.group(1).split()) if classes else set()
        assert names & {"btn", "link-btn", "nav-toggle"}, tag


def test_the_scan_submit_overrides_no_longer_need_important():
    """Both `!important`s existed only to out-shout the global button rule
    -- and the second existed only to out-shout the first. With the base
    rule scoped and the override carrying .btn in its selector, specificity
    does the work and there is nothing left for the next override to have
    to escalate past.
    """
    for line in theme.THEME_CSS.splitlines():
        if "scan-submit" in line or "border-radius: 999px" in line:
            assert "!important" not in line, line.strip()
