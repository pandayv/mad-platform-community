"""Fetches the current WCAG version from the canonical W3C source.

Deterministic tool, not an LLM call. Parses the version out of the W3C
WAI overview page's links to the current standard rather than free-text
prose -- that URL pattern is W3C's own stable convention for versioned
recommendations, far less likely to drift than page copy.

The pattern has to cover two W3C spellings, which is the whole of B9:

- the compact, undelimited form used through the 2.x line --
  `/TR/WCAG22/`, `/TR/WCAG21/`, `/TR/WCAG20/`
- the hyphenated, dotted form W3C uses for WCAG 3 -- `/TR/wcag-3.0/`

The old pattern was `r"/TR/WCAG(\\d)(\\d)/"`: uppercase-only, exactly two
digits, no separator, no dot. It cannot match `/TR/wcag-3.0/` on any of
those four counts. The consequence was not a parse warning -- it was that
`fetch_current_wcag_version` would go on returning "2.2" forever after
WCAG 3.0 shipped, `run_wcag_freshness_check` would see stored == current
and return "no_change" every tick, and the entire
classify_version_change -> embed_and_store_corpus -> major-change alert
path (written specifically with a WCAG 3.0 jump in mind) would never run
for the one event it exists for. A dead code path that looks alive is
worse than a missing one, because the landing page advertises it.
"""

from __future__ import annotations

import re

import requests

from mad_platform.tools import retry

_OVERVIEW_URL = "https://www.w3.org/WAI/standards-guidelines/wcag/"

# Case-insensitive, optional separator, optional dotted minor. The two
# capture groups are deliberately NOT "major" and "minor" -- in the
# compact form the whole version lives in the first group ("22"), and
# `_normalize` below is what resolves that. Keep the normalization and the
# pattern together; splitting them is how the old two-single-digit
# assumption got baked in silently.
_VERSION_LINK_RE = re.compile(r"/TR/wcag[-_]?(\d+)(?:\.(\d+))?/", re.IGNORECASE)

_FETCH_ATTEMPTS = 3


class WCAGVersionFetchError(Exception):
    """Raised when the current version can't be determined from the page."""


def _normalize(first: str, second: str | None) -> str | None:
    """Turns one regex match into a "major.minor" string.

    - ("3", "0")  -> "3.0"   the dotted form, as written
    - ("22", None) -> "2.2"  the compact form: two digits, major then minor
    - ("3", None)  -> "3.0"  a bare major, e.g. a future "/TR/wcag-4/"

    Anything else (three or more undelimited digits) returns None rather
    than guessing where the decimal point goes -- an unparseable link
    should not quietly become a version number that drives a re-embed.
    """
    if second is not None:
        return f"{int(first)}.{int(second)}"
    if len(first) == 1:
        return f"{int(first)}.0"
    if len(first) == 2:
        return f"{first[0]}.{first[1]}"
    return None


def parse_wcag_versions(text: str) -> list[str]:
    """Every WCAG version referenced by a /TR/ link in `text`, normalized.

    Public (not underscore-prefixed) because this is the part worth
    testing directly: the regex's raw groups mean different things in the
    two URL forms, so asserting on them pins the wrong thing.
    """
    versions = []
    for first, second in _VERSION_LINK_RE.findall(text):
        normalized = _normalize(first, second or None)
        if normalized is not None:
            versions.append(normalized)
    return versions


def fetch_current_wcag_version() -> str:
    """Returns the highest WCAG version number found (e.g. "2.2")."""

    def _get() -> str:
        response = requests.get(_OVERVIEW_URL, timeout=15)
        response.raise_for_status()
        return response.text

    # Bounded retry with the same classification every other external call
    # in this codebase uses. This was the one bare `requests.get` left: a
    # single transient W3C blip failed the whole scheduled tick, and since
    # the tick is scheduled rather than user-triggered, nobody would see
    # the failure until the next one.
    page = retry.with_retry(_get, attempts=_FETCH_ATTEMPTS, label="fetch_current_wcag_version")

    versions = parse_wcag_versions(page)
    if not versions:
        # Raise rather than return a stale version: a silent "2.2" here is
        # indistinguishable from a real no-change result, which is exactly
        # how the WCAG 3.0 path stayed unreachable without anyone noticing.
        raise WCAGVersionFetchError(
            f"Could not find a WCAG version link (pattern {_VERSION_LINK_RE.pattern}) on {_OVERVIEW_URL}"
        )

    return max(versions, key=lambda v: tuple(int(part) for part in v.split(".")))
