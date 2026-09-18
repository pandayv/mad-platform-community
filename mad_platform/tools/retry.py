"""One retry policy, shared by every bounded retry in this codebase.

There were three independent retry loops -- `gemini_client._with_retry`,
the inline loop in `adk_client.generate_structured`, and `crawler.fetch_page`
-- and none of them looked at *what* had failed. All three caught bare
`Exception` and slept `1.5 * attempt`. So:

- A `400 INVALID_ARGUMENT` (malformed prompt, oversized image) or a
  `403 PERMISSION_DENIED` (a missing IAM binding) was retried exactly like
  a transient timeout, doubling the cost and the latency of a failure that
  was certain to recur identically.
- A `429 RESOURCE_EXHAUSTED` was retried after 1.5 seconds -- far too soon
  to help, and adding to the very quota pressure that caused it.
- `crawler.fetch_page` discarded every intermediate exception without
  logging, so when all three attempts failed the operator saw only the
  last one and could not tell whether the three failures shared a cause.

`classify()` is the single place that decides. Adding a new failure mode
means teaching one function, not three.

Deliberately hand-rolled rather than `tenacity` (which is already a
dependency and unused): the whole policy is the forty lines below, the
two callers need a sync and an async variant of the same thing, and a
declarative retry library would put the interesting part -- the
classification -- behind a predicate argument anyway.
"""

from __future__ import annotations

import asyncio
import logging
import random
import time
from enum import Enum
from typing import Awaitable, Callable, TypeVar

logger = logging.getLogger("mad_platform.retry")

T = TypeVar("T")

# Base backoff, matching the 1.5s-per-attempt the three loops used before.
_BASE_BACKOFF_S = 1.5
# 429 means the thing we are waiting on is already oversubscribed. Retrying
# in 1.5s adds to the pressure without plausibly clearing it.
_RATE_LIMIT_BACKOFF_S = 20.0
# Jitter spreads concurrent retries instead of having them collide again in
# lockstep -- which matters most for exactly the 429 case.
_JITTER = 0.25


class Disposition(Enum):
    RETRY = "retry"
    RETRY_SLOWLY = "retry_slowly"  # rate-limited: back off much harder
    FAIL_FAST = "fail_fast"  # cannot succeed on a retry; surface it now


def _status_code(exc: BaseException) -> int | None:
    """HTTP status from whichever SDK raised, without importing any of them
    at module scope. google-api-core exceptions expose `.code`, httpx and
    requests expose `.response.status_code`.
    """
    code = getattr(exc, "code", None)
    if isinstance(code, int):
        return code
    response = getattr(exc, "response", None)
    status = getattr(response, "status_code", None)
    return status if isinstance(status, int) else None


def classify(exc: BaseException) -> Disposition:
    """What to do about this failure.

    The default is RETRY, matching the previous behavior: an exception
    class nobody has classified yet should not become a new way for a scan
    to fail on its first blip. Only failures that are *provably* not going
    to change on a retry are told to fail fast.
    """
    if isinstance(exc, asyncio.CancelledError):  # pragma: no cover - not a failure
        raise exc

    status = _status_code(exc)
    if status == 429:
        return Disposition.RETRY_SLOWLY
    if status is not None and 400 <= status < 500:
        # A malformed request, a missing permission, a 404: the identical
        # request will fail identically. 408 (timeout) and 409 (conflict)
        # are the two 4xx that genuinely can change, so they stay retryable.
        if status in (408, 409):
            return Disposition.RETRY
        return Disposition.FAIL_FAST

    # Deliberately NOT fail-fast: a pydantic ValidationError on a model
    # response. The review suggested treating it as certain to recur, but
    # generation is non-deterministic -- re-sampling the same prompt is a
    # genuinely different draw, and a schema violation is one of the things
    # a second attempt most often fixes. A malformed *request* is the
    # certain-to-recur case, and that arrives as a 4xx above.
    return Disposition.RETRY


def backoff_seconds(attempt: int, disposition: Disposition) -> float:
    """attempt is 1-based: the delay *after* the attempt'th failure."""
    base = _RATE_LIMIT_BACKOFF_S if disposition is Disposition.RETRY_SLOWLY else _BASE_BACKOFF_S * attempt
    return base * (1 + random.uniform(-_JITTER, _JITTER))


def _log_attempt(label: str, attempt: int, attempts: int, exc: BaseException, disposition: Disposition) -> None:
    """Every intermediate failure is logged, not just the last one. Three
    timeouts and three 403s produce the same final exception message but
    mean completely different things to whoever is reading the logs.
    """
    logger.warning(
        "%s: attempt %d/%d failed (%s: %s) -- %s",
        label, attempt, attempts, type(exc).__name__, exc, disposition.value,
    )


def with_retry(call: Callable[[], T], *, attempts: int, label: str) -> T:
    """Synchronous bounded retry under the shared policy."""
    last_error: BaseException | None = None
    for attempt in range(1, attempts + 1):
        try:
            return call()
        except Exception as exc:  # noqa: BLE001 - classified below, not swallowed
            last_error = exc
            disposition = classify(exc)
            _log_attempt(label, attempt, attempts, exc, disposition)
            if disposition is Disposition.FAIL_FAST or attempt == attempts:
                raise
            time.sleep(backoff_seconds(attempt, disposition))
    raise last_error  # pragma: no cover - unreachable, the loop always returns or raises


async def with_retry_async(call: Callable[[], Awaitable[T]], *, attempts: int, label: str) -> T:
    """Async twin of with_retry, same policy."""
    last_error: BaseException | None = None
    for attempt in range(1, attempts + 1):
        try:
            return await call()
        except Exception as exc:  # noqa: BLE001 - classified below, not swallowed
            last_error = exc
            disposition = classify(exc)
            _log_attempt(label, attempt, attempts, exc, disposition)
            if disposition is Disposition.FAIL_FAST or attempt == attempts:
                raise
            await asyncio.sleep(backoff_seconds(attempt, disposition))
    raise last_error  # pragma: no cover - unreachable
