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
