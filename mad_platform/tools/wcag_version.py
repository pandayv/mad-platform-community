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

**Widening that pattern re-opened the same hole from the other side.** The
module said it parsed links to "the current standard" while the code had
no notion of Recommendation-versus-draft: it collected every `/TR/wcag…/`
link on the page and took `max()`. W3C publishes Working Drafts under
`/TR/` at the same "latest version" URL a Recommendation eventually gets,
so the moment this overview page links WCAG 3 draft material at
`/TR/wcag-3.0/`, `max()` returns "3.0" on the strength of a draft:
`run_wcag_freshness_check` sees a version change, classifies it major,
re-embeds, writes "3.0" as the stored knowledge-base version and fires the
major-change alert. The stored pointer is then wrong, and the real 2.2→3.0
transition, when it comes, reads as no change at all.

Checked against the live page (2026-09-19) before fixing: it currently
links only `/TR/WCAG22/`, `/TR/WCAG21/` and `/TR/WCAG20/`, and reaches
WCAG 3 through `/WAI/standards-guidelines/wcag/wcag3-intro/`, which this
pattern does not match. So the defect is latent, not firing today -- it
arms itself the moment W3C links the draft from here, which is a page-copy
change entirely outside this project's control.

The gate is `KNOWN_RECOMMENDATIONS` below rather than anything parsed out
of the page's structure, because page structure is exactly what cannot be
relied on across that change. A version this project has not recorded as
released is reported to the caller as unrecognized and never becomes the
current version -- which also happens to match what
`wcag_auto_heal.py`'s own docstring already says is true: a real
conformance-model shift needs `wcag_corpus.py`'s *content* rewritten by a
person, so a human has to be in that loop regardless.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass

import requests

from mad_platform.tools import retry

logger = logging.getLogger("mad_platform.wcag_version")

_OVERVIEW_URL = "https://www.w3.org/WAI/standards-guidelines/wcag/"

# Case-insensitive, optional separator, optional dotted minor. The two
# capture groups are deliberately NOT "major" and "minor" -- in the
# compact form the whole version lives in the first group ("22"), and
# `_normalize` below is what resolves that. Keep the normalization and the
# pattern together; splitting them is how the old two-single-digit
# assumption got baked in silently.
_VERSION_LINK_RE = re.compile(r"/TR/wcag[-_]?(\d+)(?:\.(\d+))?/", re.IGNORECASE)

_FETCH_ATTEMPTS = 3

# WCAG versions W3C has actually published as a Recommendation, as last
# confirmed by a person. Anything on the page above the highest of these is
# a draft (or something this project has not heard of yet) and must not be
# allowed to become "the current standard" on its own.
#
# Adding a version here is a deliberate act, and deliberately so: it is the
# same human step wcag_auto_heal.py already requires for the corpus content
# itself. Until it happens, the freshness check alerts instead of
# auto-refreshing, which is the safe direction -- a missed alert costs a
# late update, a wrong one silently re-points the knowledge base at a
# version that does not exist yet and makes the real transition a no-op.
KNOWN_RECOMMENDATIONS: tuple[str, ...] = ("2.0", "2.1", "2.2")


class WCAGVersionFetchError(Exception):
    """Raised when the current version can't be determined from the page."""


@dataclass(frozen=True)
class WCAGVersionReading:
    """What one look at the overview page found.

    `current` is the highest version known to be a Recommendation.
    `unrecognized` is every higher version the page referenced that this
    project has not recorded as released -- drafts, in practice. It is
    carried separately rather than dropped so the scheduled check can alert
    a person about it; silently ignoring a `/TR/wcag-3.0/` link would swap
    one invisible failure for another.
    """

    current: str
    unrecognized: tuple[str, ...] = ()


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


def _sort_key(version: str) -> tuple[int, ...]:
    return tuple(int(part) for part in version.split("."))


def split_by_release_status(versions: list[str]) -> tuple[list[str], list[str]]:
    """Splits parsed versions into (released, unrecognized).

    Public and separate from the fetch so the classification can be tested
    without a network call -- it is the part that carries the judgment.
    """
    released = sorted({v for v in versions if v in KNOWN_RECOMMENDATIONS}, key=_sort_key)
    unrecognized = sorted({v for v in versions if v not in KNOWN_RECOMMENDATIONS}, key=_sort_key)
    return released, unrecognized


def read_wcag_versions() -> WCAGVersionReading:
    """Fetches the overview page and classifies what it links to."""

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

    released, unrecognized = split_by_release_status(versions)
    if not released:
        # Every version on the page is one this project has not recorded as
        # released. Raising is the safe direction: the alternative is
        # nominating a draft as the current standard, which is the whole
        # failure this split exists to prevent.
        raise WCAGVersionFetchError(
            f"{_OVERVIEW_URL} references only WCAG version(s) {unrecognized}, none of which "
            f"is a known Recommendation {KNOWN_RECOMMENDATIONS}. If W3C has published a new "
            "version, add it to wcag_version.KNOWN_RECOMMENDATIONS -- and read "
            "agents/wcag_auto_heal.py first, because a conformance-model shift also needs "
            "data/wcag_corpus.py rewritten by hand."
        )
    if unrecognized:
        logger.warning(
            "%s references WCAG version(s) %s that are not known Recommendations -- "
            "treating %s as current. If one of these has actually shipped, add it to "
            "wcag_version.KNOWN_RECOMMENDATIONS.",
            _OVERVIEW_URL, unrecognized, released[-1],
        )
    return WCAGVersionReading(current=released[-1], unrecognized=tuple(unrecognized))


def fetch_current_wcag_version() -> str:
    """The highest WCAG version the overview page links that this project
    knows to be a Recommendation (e.g. "2.2").

    Thin wrapper over `read_wcag_versions` for the callers that only need
    the one value; the scheduled freshness check uses the full reading so
    it can alert on a draft rather than ignore it.
    """
    return read_wcag_versions().current
