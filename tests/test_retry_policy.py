"""B12: retries have to look at what failed.

Three loops (gemini_client, adk_client, crawler) each caught bare
Exception and slept 1.5 * attempt. So a 400 from a malformed prompt and a
403 from a missing IAM binding cost two attempts and two timeouts' worth
of latency to report a failure that could not have gone any other way,
while a 429 was retried after 1.5s -- far too soon to help, and adding to
the pressure that caused it.
"""

from __future__ import annotations

import asyncio

import pytest
from google.api_core import exceptions as gcloud_exceptions

from mad_platform.tools import retry
from mad_platform.tools.retry import Disposition, classify


class _HttpError(Exception):
    """An httpx/requests-shaped error: status hangs off .response."""

    def __init__(self, status: int):
        super().__init__(f"HTTP {status}")
        self.response = type("R", (), {"status_code": status})()


@pytest.mark.parametrize(
    "exc",
    [
        gcloud_exceptions.InvalidArgument("malformed prompt"),
        gcloud_exceptions.PermissionDenied("missing IAM binding"),
        gcloud_exceptions.NotFound("no such model"),
        gcloud_exceptions.Unauthenticated("bad credentials"),
        _HttpError(400),
        _HttpError(403),
    ],
)
def test_failures_that_cannot_succeed_on_a_retry_fail_fast(exc):
    assert classify(exc) is Disposition.FAIL_FAST


@pytest.mark.parametrize(
    "exc",
    [
        gcloud_exceptions.TooManyRequests("rate limited"),
        _HttpError(429),
    ],
)
def test_rate_limits_back_off_much_harder(exc):
    assert classify(exc) is Disposition.RETRY_SLOWLY
    # A 1.5s retry into a quota wall is worse than useless.
    assert retry.backoff_seconds(1, Disposition.RETRY_SLOWLY) > 5 * retry.backoff_seconds(
        1, Disposition.RETRY
    )


@pytest.mark.parametrize(
    "exc",
    [
        gcloud_exceptions.ServiceUnavailable("503"),
        gcloud_exceptions.InternalServerError("500"),
        gcloud_exceptions.DeadlineExceeded("timeout"),
        _HttpError(408),
        _HttpError(409),
        _HttpError(503),
        TimeoutError("socket timeout"),
        ConnectionError("connection reset"),
        RuntimeError("something nobody has classified yet"),
    ],
)
def test_transient_and_unclassified_failures_are_retried(exc):
    assert classify(exc) is Disposition.RETRY


def test_a_schema_validation_error_is_still_retried():
    """Deliberate disagreement with the review, documented in retry.py:
    generation is non-deterministic, so re-sampling the same prompt is a
    genuinely different draw and a schema violation is one of the things a
    second attempt most often fixes. The certain-to-recur case is a
    malformed *request*, and that arrives as a 4xx.
    """
    from pydantic import BaseModel, ValidationError

    class M(BaseModel):
        x: int

    try:
        M.model_validate({"x": "not an int"})
    except ValidationError as exc:
        assert classify(exc) is Disposition.RETRY


def test_backoff_is_jittered_so_concurrent_retries_do_not_collide_again():
    delays = {retry.backoff_seconds(1, Disposition.RETRY) for _ in range(20)}
    assert len(delays) > 1


# --- the loops themselves ---------------------------------------------------


def test_with_retry_stops_immediately_on_a_fail_fast_error(monkeypatch):
    monkeypatch.setattr(retry.time, "sleep", lambda _s: pytest.fail("must not sleep before failing fast"))
    calls = []

    def call():
        calls.append(1)
        raise gcloud_exceptions.PermissionDenied("nope")

    with pytest.raises(gcloud_exceptions.PermissionDenied):
        retry.with_retry(call, attempts=3, label="t")
    assert len(calls) == 1, "a 403 must cost one attempt, not three"


def test_with_retry_retries_a_transient_error_then_succeeds(monkeypatch):
    monkeypatch.setattr(retry.time, "sleep", lambda _s: None)
    calls = []

    def call():
        calls.append(1)
        if len(calls) < 3:
            raise gcloud_exceptions.ServiceUnavailable("503")
        return "ok"

    assert retry.with_retry(call, attempts=3, label="t") == "ok"
    assert len(calls) == 3


def test_with_retry_raises_the_real_error_after_the_last_attempt(monkeypatch):
    monkeypatch.setattr(retry.time, "sleep", lambda _s: None)

    def call():
        raise gcloud_exceptions.ServiceUnavailable("still down")

    with pytest.raises(gcloud_exceptions.ServiceUnavailable):
        retry.with_retry(call, attempts=2, label="t")


def test_every_intermediate_failure_is_logged(monkeypatch, caplog):
    """crawler.fetch_page discarded intermediate exceptions entirely, so
    three timeouts and three DNS failures produced the same final message
    and an operator could not tell them apart.
    """
    monkeypatch.setattr(retry.time, "sleep", lambda _s: None)
    attempts = iter([gcloud_exceptions.ServiceUnavailable("first"), None])

    def call():
        exc = next(attempts)
        if exc:
            raise exc
        return "ok"

    with caplog.at_level("WARNING", logger="mad_platform.retry"):
        assert retry.with_retry(call, attempts=2, label="mylabel") == "ok"
    messages = [r.getMessage() for r in caplog.records]
    assert any("mylabel" in m and "ServiceUnavailable" in m for m in messages), messages


async def test_async_retry_follows_the_same_policy(monkeypatch):
    async def _no_sleep(_s):
        return None

    monkeypatch.setattr(asyncio, "sleep", _no_sleep)
    calls = []

    async def call():
        calls.append(1)
        raise gcloud_exceptions.InvalidArgument("bad request")

    with pytest.raises(gcloud_exceptions.InvalidArgument):
        await retry.with_retry_async(call, attempts=3, label="t")
    assert len(calls) == 1


async def test_async_retry_recovers_from_a_transient_failure(monkeypatch):
    async def _no_sleep(_s):
        return None

    monkeypatch.setattr(asyncio, "sleep", _no_sleep)
    calls = []

    async def call():
        calls.append(1)
        if len(calls) < 2:
            raise gcloud_exceptions.ServiceUnavailable("503")
        return "ok"

    assert await retry.with_retry_async(call, attempts=3, label="t") == "ok"
