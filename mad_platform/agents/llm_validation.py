"""Validation for LLM responses that reference our own lists by index.

Two agents ask Gemini to answer "about item N of this list" and get back
objects carrying a `finding_index`: Editor (`VerifiedFinding`) and
Reporter (`_Recommendation`). Those integers were then used directly as
Python list subscripts. A Pydantic model validates that the field is an
`int` -- it cannot validate that the int addresses anything real, because
the model has no idea how long our list was.

Three separate failure modes came out of that, all of them live:

1. **Out of range** -> `IndexError`, which propagates all the way up and
   fails the whole scan with the user-visible message "list index out of
   range". Cloud Tasks then retries into the same non-deterministic model
   output.
2. **Negative** -> no error at all. Python wraps, so `finding_index: -1`
   silently attaches a recommendation to the *last* finding. The report
   renders cleanly with a fix that belongs to a different issue. This is
   the dangerous one: it is invisible.
3. **Short or duplicated response** -> the caller iterates the model's
   list, not ours, so findings the model simply did not mention vanish
   from the report, the tickets and the score, with no log line anywhere.
   For a product whose core claim is "every finding is independently
   verified before you see it", silently dropping findings is a
   correctness failure at the level of the claim.

The fix belongs here, once, at the boundary where model output becomes
data -- not as a bounds check at each of the six subscript sites, which is
how this class of bug grows back in a new place. Every caller of
`generate_structured` that returns index-bearing items runs its result
through `validate_indexed` before anything downstream touches it.
"""

from __future__ import annotations

import logging
from typing import Protocol, TypeVar

logger = logging.getLogger("mad_platform.llm_validation")


class _HasFindingIndex(Protocol):
    finding_index: int


T = TypeVar("T", bound=_HasFindingIndex)


def validate_indexed(items: list[T], source_len: int, *, label: str) -> list[T]:
    """Returns only the items whose `finding_index` really addresses the
    source list, at most one per index, and logs everything it had to do.

    - An index outside `range(source_len)` is dropped (this covers
      negatives -- a negative index is *not* a valid reference here, even
      though Python would happily resolve it).
    - A repeated index keeps the first occurrence; later ones are dropped,
      so two recommendations can never fight over one finding.
    - A returned count that does not match `source_len` is logged at
      WARNING even when every index was individually valid, because
      "the model answered about 4 of the 6 things we asked about" is a
      real event that should be visible in logs rather than absorbed.

    `label` identifies the call site (and, where the caller has one, the
    job) in those log lines.
    """
    valid: list[T] = []
    seen: set[int] = set()
    out_of_range: list[int] = []
    duplicates: list[int] = []

    for item in items:
        index = item.finding_index
        if not (0 <= index < source_len):
            out_of_range.append(index)
            continue
        if index in seen:
            duplicates.append(index)
            continue
        seen.add(index)
        valid.append(item)

    if out_of_range:
        logger.warning(
            "%s: dropped %d item(s) with an index outside 0..%d: %s",
            label, len(out_of_range), source_len - 1, out_of_range,
        )
    if duplicates:
        logger.warning(
            "%s: dropped %d duplicate index reference(s): %s", label, len(duplicates), duplicates
        )
    if len(valid) != source_len:
        logger.warning(
            "%s: model returned %d usable item(s) for %d input item(s) -- %d input item(s) "
            "went unanswered and will not appear downstream",
            label, len(valid), source_len, max(0, source_len - len(valid)),
        )

    return valid
