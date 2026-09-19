"""The severity vocabulary, defined once.

Before this module the same four words existed as five independent lists:
`theme._SEVERITY_ORDER`,
`reporter._SCORE_WEIGHT`, `reporter._EMAIL_SEVERITY_COLOR`, the counts
dict rebuilt in `orchestrator.build_scan_summary`, and a `SEV_ORDER`
array in the status page's JavaScript. Adding or renaming a tier meant
finding all five, and the charts silently disagreed with the headline
count whenever one was missed.

It also had no *validation*, and that was the deeper bug: `severity`
was a plain `str` filled in by Gemini, so an off-vocabulary value like
"Critical" or "severe" produced a phantom key in the counts dict that the
donut and legend ignored (ring total != headline total), took the silent
fallback weight in `compute_score`, and -- worst -- slipped past
`action_agent.needs_escalation`'s case-sensitive `== "critical"` check,
auto-filing a genuinely critical finding without the human review that
gate exists to force.

Two halves to the fix, and both are needed:

- `Severity` is a `Literal`, used as the model's response-schema field
  type. The SDK constrains generation to those four strings, and Pydantic
  rejects anything else instead of passing it through.
- `normalize()` for everything that crosses a boundary the schema does
  not cover -- values read back out of Firestore, written by an older
  revision, or typed by a reviewer. Case and whitespace are folded; an
  unrecognized value is NOT silently coerced to a tier, it raises unless
  the caller passes an explicit default, because guessing is how a
  critical finding ends up counted as medium.
"""

from __future__ import annotations

from typing import Literal

Severity = Literal["critical", "high", "medium", "low"]

# Ordered most severe first. Every consumer that ranks, iterates or
# renders severities reads this, so the donut, the legend, the email and
# the status page cannot disagree about order or membership.
SEVERITY_ORDER: tuple[str, ...] = ("critical", "high", "medium", "low")

# The one severity that forces human review regardless of the model's own
# confidence (see action_agent.needs_escalation).
ESCALATE_ALWAYS: str = "critical"

# The other half of that same gate: an Editor confidence below this sends
# a finding to human review whatever its severity.
#
# It lives here, next to ESCALATE_ALWAYS, rather than in action_agent where
# it started, because it is now read by three modules on both sides of an
# import cycle -- action_agent owns the gate, but editor and orchestrator
# both need to construct a finding that is guaranteed to land on the review
# side of it, and editor cannot import action_agent (action_agent imports
# reporter, which imports editor). This module deliberately imports nothing
# from the package, which is what makes it a safe home for shared policy
# constants; the alternative was a second literal 0.6 somewhere, which is
# the exact duplication this module's docstring exists to describe.
LOW_CONFIDENCE_THRESHOLD: float = 0.6


class UnknownSeverityError(ValueError):
    """A severity value outside the vocabulary above."""


def normalize(value: object, default: str | None = None) -> str:
    """Folds a severity to its canonical lowercase form.

    Pass `default` only where a wrong tier is genuinely harmless (a chart
    color, say). Leave it unset anywhere the value drives a decision:
    silently mapping an unknown string onto a real tier is exactly the
    failure this module exists to stop.
    """
    text = str(value or "").strip().lower()
    if text in SEVERITY_ORDER:
        return text
    if default is not None:
        return default
    raise UnknownSeverityError(
        f"{value!r} is not one of {', '.join(SEVERITY_ORDER)}"
    )


def empty_counts() -> dict[str, int]:
    """A zeroed counts dict with every tier present.

    Every tier is present even at zero on purpose: a renderer that reads
    `counts["high"]` must not depend on whether this scan happened to find
    a high-severity issue.
    """
    return {sev: 0 for sev in SEVERITY_ORDER}


def count_by_severity(values: list[str]) -> dict[str, int]:
    """Counts a list of severity strings into `empty_counts()`.

    Unknown values are folded into the nearest safe tier ("critical"):
    over-reporting severity is recoverable, under-reporting it is the
    failure mode that matters, and dropping the finding entirely would
    make the ring total disagree with the headline count -- the exact
    symptom described above.
    """
    counts = empty_counts()
    for value in values:
        counts[normalize(value, default=ESCALATE_ALWAYS)] += 1
    return counts
