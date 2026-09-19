"""B3: ADK sessions were created per call and never deleted.

`_runner_cache` keeps one InMemoryRunner -- and therefore one
InMemorySessionService -- for the life of the process, while `_run_once`
created a fresh session per call. The service holds sessions in a plain
dict with no eviction (its own class docstring says it is "not suitable
for multi-threaded production environments"), and a session keeps the
full event history, including the full-page screenshot that
`run_visual_check` and `verify_findings` attach. Roughly fifteen sessions
per scan, six carrying a multi-megabyte PNG, on a 1Gi
containerConcurrency=1 instance that survives many scans: the footprint
grew until the instance was OOM-killed mid-scan, which is exactly the
condition `claim_job_lease` exists to contain.
"""

from __future__ import annotations

import pytest

from mad_platform.tools import adk_client


class _FakeEvent:
    def __init__(self, text):
        self.content = type(
            "C", (), {"parts": [type("P", (), {"text": text})()]}
        )()


class _FakeSessionService:
    """Models the part that actually leaked: a dict with no eviction."""

    def __init__(self):
        self.sessions: dict[str, str] = {}
        self.delete_calls = 0

    async def create_session(self, *, app_name, user_id, session_id):
        self.sessions[session_id] = "resident"

    async def delete_session(self, *, app_name, user_id, session_id):
        self.delete_calls += 1
        self.sessions.pop(session_id, None)


class _FakeRunner:
    def __init__(self, behaviour="ok"):
        self.session_service = _FakeSessionService()
        self.behaviour = behaviour

    async def run_async(self, *, user_id, session_id, new_message):
        if self.behaviour == "raise":
            raise RuntimeError("the model call failed")
        if self.behaviour == "silent":
            return
        yield _FakeEvent('{"ok": true}')


async def test_a_successful_call_leaves_no_session_behind():
    runner = _FakeRunner()
    assert await adk_client._run_once(runner, "prompt", None) == '{"ok": true}'
    assert runner.session_service.sessions == {}
    assert runner.session_service.delete_calls == 1


async def test_many_calls_on_one_cached_runner_do_not_accumulate():
    """The leak was monotonic growth across calls on a runner that is
    cached for the life of the process, not a single stray session.
    """
    runner = _FakeRunner()
    for _ in range(20):
        await adk_client._run_once(runner, "prompt", None)
    assert runner.session_service.sessions == {}


async def test_a_failed_call_still_cleans_up_and_still_raises():
    """The failure path is the one that matters most: retry.with_retry_async
    calls this again after an exception, so a session leaked per failure
    would leak fastest exactly when the worker is already struggling.
    """
    runner = _FakeRunner(behaviour="raise")
    with pytest.raises(RuntimeError, match="the model call failed"):
        await adk_client._run_once(runner, "prompt", None)
    assert runner.session_service.sessions == {}


async def test_an_empty_model_response_also_cleans_up():
    runner = _FakeRunner(behaviour="silent")
    with pytest.raises(RuntimeError, match="no text output"):
        await adk_client._run_once(runner, "prompt", None)
    assert runner.session_service.sessions == {}


async def test_a_failing_delete_does_not_turn_a_good_call_into_a_bad_one():
    """Cleanup is housekeeping. Letting it raise would convert a completed
    scan step into a failure -- and, on the exception path, would mask the
    real error with a bookkeeping one.
    """
    runner = _FakeRunner()

    async def _boom(**_kwargs):
        raise RuntimeError("session service is unhappy")

    runner.session_service.delete_session = _boom
    assert await adk_client._run_once(runner, "prompt", None) == '{"ok": true}'


async def test_a_failing_delete_does_not_mask_the_original_exception():
    runner = _FakeRunner(behaviour="raise")

    async def _boom(**_kwargs):
        raise RuntimeError("session service is unhappy")

    runner.session_service.delete_session = _boom
    with pytest.raises(RuntimeError, match="the model call failed"):
        await adk_client._run_once(runner, "prompt", None)
