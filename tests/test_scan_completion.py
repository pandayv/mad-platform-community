"""B4: `status == "completed"` must never be observable without a summary.

The bug: the orchestrator flipped the job to "completed", then spent
several seconds posting to Slack, drafting an email summary with Gemini
and uploading attachments to Resend, and only after all that did the
worker write the summary. During that window /api/status returned
`{"status": "completed", "summary": null}`; the status page polled into it
every 2s, threw on `s.total_findings`, and -- with no catch and an early
return on any non-OK response -- never polled again. The user sat on a
frozen "Starting..." screen for a scan that had succeeded.

The fix is structural rather than a reordering that a later edit could
undo: status and summary are now one atomic Firestore update, and
complete_job cannot be called without a summary.
"""

from __future__ import annotations

import inspect

import pytest
from google.cloud.firestore_v1 import transaction as firestore_transaction
from starlette.requests import Request

from mad_platform.agents import orchestrator
from mad_platform.agents.orchestrator import build_scan_summary
from mad_platform.agents.reporter import RankedFinding
from mad_platform.state import firestore_client as fs
from mad_platform.tools.issue_sink import CsvIssueSink, MockIssueSink
from mad_platform.web import worker_app


def _finding(severity: str = "high", criterion: str = "1.1.1") -> RankedFinding:
    return RankedFinding(
        page_url="https://example.com/",
        wcag_criterion=criterion,
        editor_rationale="r",
        editor_confidence=0.9,
        risk_score=50.0,
        severity=severity,
        suggested_fix="fix",
        risk_rationale="why",
    )


def test_complete_job_cannot_be_called_without_a_summary():
    """The guard rail. If someone re-adds a default here, the two-write
    race is back and this test is the thing that notices.
    """
    params = inspect.signature(fs.complete_job).parameters
    assert "summary" in params
    assert params["summary"].default is inspect.Parameter.empty


def test_complete_job_writes_status_and_summary_in_one_update(monkeypatch):
    writes: list[dict] = []

    class _Doc:
        def update(self, data):
            writes.append(data)

    class _Jobs:
        def document(self, _job_id):
            return _Doc()

    monkeypatch.setattr(fs, "_jobs", _Jobs)
    fs.complete_job("job-1", {"score": 80})

    assert len(writes) == 1, "status and summary must go out in a single atomic update"
    assert writes[0]["status"] == "completed"
    assert writes[0]["summary"] == {"score": 80}


# --- the summary itself ----------------------------------------------------


def _filing(filed=(), escalated=(), already_filed=()):
    return {
        "filed": [(i, f, f"CSV-{i}") for i, f in enumerate(filed)],
        "escalated": [(i, f, f"esc-{i}") for i, f in enumerate(escalated)],
        "already_filed": [(i, f, f"CSV-old-{i}") for i, f in enumerate(already_filed)],
    }


def test_summary_counts_every_routed_finding():
    filing = _filing(filed=[_finding("high")], escalated=[_finding("critical")],
                     already_filed=[_finding("low")])
    summary = build_scan_summary(filing, "gs://b/r.html", MockIssueSink())

    assert summary["total_findings"] == 3
    assert summary["filed_count"] == 2  # filed + already_filed
    assert summary["escalated_count"] == 1
    assert summary["severity_counts"] == {"critical": 1, "high": 1, "medium": 0, "low": 1}


def test_summary_severity_counts_total_matches_the_headline():
    """The donut and the headline number are rendered from these two
    fields on the same screen; they must agree.
    """
    filing = _filing(filed=[_finding("high"), _finding("low"), _finding("medium")])
    summary = build_scan_summary(filing, "gs://b/r.html", MockIssueSink())
    assert sum(summary["severity_counts"].values()) == summary["total_findings"]


def test_summary_principle_counts_total_matches_the_headline():
    filing = _filing(filed=[_finding(criterion="1.1.1"), _finding(criterion="4.1.2")])
    summary = build_scan_summary(filing, "gs://b/r.html", MockIssueSink())
    assert sum(summary["principle_counts"].values()) == summary["total_findings"]


def test_summary_has_every_field_the_status_page_reads():
    """renderCompleted() reads exactly these. A missing one is a TypeError
    in the browser, which is how this whole finding started.
    """
    summary = build_scan_summary(_filing(), "gs://b/r.html", MockIssueSink())
    for field in (
        "score", "score_color", "severity_counts", "principle_counts",
        "total_findings", "filed_count", "escalated_count", "report_uri", "csv_export",
    ):
        assert field in summary


def test_summary_carries_the_csv_export_for_the_download_route():
    sink = CsvIssueSink()
    sink.create_issue("Summary text", "Description text")
    summary = build_scan_summary(_filing(), "gs://b/r.html", sink)
    assert "Summary text" in summary["csv_export"]


def test_summary_tolerates_a_sink_with_no_export():
    """MockIssueSink (local runs, the WCAG poller) has nothing to export --
    that must produce an empty string, not an AttributeError.
    """
    assert build_scan_summary(_filing(), "gs://b/r.html", MockIssueSink())["csv_export"] == ""


def test_summary_tolerates_no_sink_at_all():
    assert build_scan_summary(_filing(), "gs://b/r.html", None)["csv_export"] == ""


# --- the worker's retry short-circuit --------------------------------------


def _fake_request(retry_count: int | None = None) -> Request:
    """A bare Starlette Request carrying only the one header run_scan
    reads. retry_count=None omits it entirely, matching a real dispatch's
    first delivery (Cloud Tasks sends the header from the first retry
    onward, not on delivery zero).
    """
    headers = [] if retry_count is None else [(b"x-cloudtasks-taskretrycount", str(retry_count).encode())]
    return Request(scope={"type": "http", "headers": headers})


async def _run(job_id: str = "job-1", retry_count: int | None = None):
    return await worker_app.run_scan(worker_app.RunRequest(job_id=job_id), _fake_request(retry_count))


@pytest.fixture
def granted_lease(monkeypatch):
    """B5: /run now claims the job before doing billable work. Every test
    below that expects the scan to proceed has to be granted that claim;
    the lease's own behaviour is tested separately at the end of this file.
    """
    released = []
    monkeypatch.setattr(fs, "claim_job_lease", lambda *_a, **_k: True)
    monkeypatch.setattr(fs, "release_job_lease", lambda job_id, owner: released.append((job_id, owner)))
    return released


async def test_completed_job_with_a_summary_is_not_re_run(monkeypatch):
    monkeypatch.setattr(fs, "get_job", lambda _j: {"status": "completed", "summary": {"score": 90}, "url": "u"})
    monkeypatch.setattr(
        worker_app, "run_one_time_scan",
        lambda *a, **k: pytest.fail("must not re-run a genuinely finished job"),
    )
    assert await _run() == {"ok": True, "already_completed": True}


async def test_completed_job_without_a_summary_is_re_run_to_repair_it(monkeypatch, granted_lease):
    """Short-circuiting on status alone left such a job permanently
    broken: every Cloud Tasks retry returned 2xx and the summary was never
    written, so the status page stayed frozen and tickets.csv 404'd
    forever. Falling through resumes from the last checkpoint.
    """
    monkeypatch.setattr(fs, "get_job", lambda _j: {"status": "completed", "url": "https://example.com/"})
    monkeypatch.setattr(fs, "mark_job_started", lambda _j: None)
    ran = []

    async def fake_scan(*_a, **_k):
        ran.append(True)
        return None

    monkeypatch.setattr(worker_app, "run_one_time_scan", fake_scan)
    assert await _run() == {"ok": True}
    assert ran == [True]


async def test_missing_job_is_dropped_rather_than_retried(monkeypatch):
    monkeypatch.setattr(fs, "get_job", lambda _j: None)
    result = await _run()
    assert result["ok"] is False


async def test_worker_no_longer_writes_the_summary_itself(monkeypatch, granted_lease):
    """The summary write belongs to complete_job now. A second writer here
    is what created the gap in the first place.
    """
    monkeypatch.setattr(fs, "get_job", lambda _j: {"status": "queued", "url": "https://example.com/"})
    monkeypatch.setattr(fs, "mark_job_started", lambda _j: None)
    monkeypatch.setattr(
        fs, "save_scan_summary",
        lambda *a, **k: pytest.fail("the worker must not publish the summary separately"),
    )

    async def fake_scan(*_a, **_k):
        return None

    monkeypatch.setattr(worker_app, "run_one_time_scan", fake_scan)
    assert await _run() == {"ok": True}


# --- B5: the worker lease ---------------------------------------------------


async def test_a_job_already_held_by_another_worker_is_dropped(monkeypatch):
    """Cloud Tasks retries on dispatch-deadline expiry, which does not
    require the first attempt to have stopped -- so a long scan gets a
    second worker while the first is still running. The completed-check
    cannot catch that (neither attempt has completed anything yet).
    """
    monkeypatch.setattr(fs, "get_job", lambda _j: {"status": "in_progress", "url": "https://example.com/"})
    monkeypatch.setattr(fs, "claim_job_lease", lambda *_a, **_k: False)
    monkeypatch.setattr(
        worker_app, "run_one_time_scan",
        lambda *a, **k: pytest.fail("must not run a scan another worker holds"),
    )
    monkeypatch.setattr(fs, "mark_job_started", lambda _j: pytest.fail("must not touch a job it did not claim"))

    result = await _run()
    # 2xx, not an error: retrying this dispatch would only collide again.
    assert result == {"ok": True, "already_running": True}


async def test_the_lease_is_released_when_the_scan_succeeds(monkeypatch, granted_lease):
    monkeypatch.setattr(fs, "get_job", lambda _j: {"status": "queued", "url": "https://example.com/"})
    monkeypatch.setattr(fs, "mark_job_started", lambda _j: None)

    async def fake_scan(*_a, **_k):
        return None

    monkeypatch.setattr(worker_app, "run_one_time_scan", fake_scan)
    await _run("job-9")
    assert [job_id for job_id, _owner in granted_lease] == ["job-9"]


async def test_the_lease_is_released_when_the_scan_fails(monkeypatch, granted_lease):
    """Otherwise a failed scan would block its own Cloud Tasks retries for
    the whole JOB_LEASE_SECONDS window.
    """
    monkeypatch.setattr(fs, "get_job", lambda _j: {"status": "queued", "url": "https://example.com/"})
    monkeypatch.setattr(fs, "mark_job_started", lambda _j: None)

    async def boom(*_a, **_k):
        raise RuntimeError("scan blew up")

    monkeypatch.setattr(worker_app, "run_one_time_scan", boom)
    with pytest.raises(RuntimeError):
        await _run("job-9")
    assert [job_id for job_id, _owner in granted_lease] == ["job-9"]


# --- the launch-day bug: a mid-retry failure must not read as terminal ----


async def test_a_mid_retry_failure_does_not_mark_the_job_failed(monkeypatch, granted_lease):
    """The actual bug a real visitor hit at launch: a transient Gemini 500
    during a scan Cloud Tasks still had retries left for flipped status to
    "failed" -- the status page saw that, stopped polling, and showed
    "Scan failed" permanently, while the retry (seconds later) completed
    the scan and emailed the report anyway. Cloud Tasks' own retry-count
    header is what tells the worker more attempts are coming.
    """
    monkeypatch.setattr(fs, "get_job", lambda _j: {"status": "queued", "url": "https://example.com/"})
    monkeypatch.setattr(fs, "mark_job_started", lambda _j: None)

    async def boom(_url):
        raise RuntimeError("transient upstream error")

    monkeypatch.setattr(orchestrator, "fetch_page", boom)
    failed = []
    monkeypatch.setattr(fs, "fail_job", lambda *a, **k: failed.append(a))

    with pytest.raises(RuntimeError):
        await _run("job-9", retry_count=0)  # first delivery -- two retries still allowed

    assert failed == [], "a mid-retry failure must not mark the job failed for a visitor to see"


async def test_the_last_allowed_attempt_does_mark_the_job_failed(monkeypatch, granted_lease):
    """The other half of the same fix: once Cloud Tasks has no retries
    left, a failure has to become the real, visible "Scan failed" -- not
    silently vanish.
    """
    monkeypatch.setattr(fs, "get_job", lambda _j: {"status": "queued", "url": "https://example.com/"})
    monkeypatch.setattr(fs, "mark_job_started", lambda _j: None)

    async def boom(_url):
        raise RuntimeError("transient upstream error")

    monkeypatch.setattr(orchestrator, "fetch_page", boom)
    failed = []
    monkeypatch.setattr(fs, "fail_job", lambda *a, **k: failed.append(a))

    with pytest.raises(RuntimeError):
        await _run("job-9", retry_count=worker_app.SCAN_QUEUE_MAX_ATTEMPTS - 1)  # last allowed delivery

    assert len(failed) == 1


async def test_each_dispatch_claims_under_its_own_owner_id(monkeypatch):
    """The owner has to be unique per dispatch, or two workers would each
    see "my own lease" and both proceed -- the re-entrancy that makes a
    retry by the same worker safe would defeat the lock entirely.
    """
    owners = []
    monkeypatch.setattr(fs, "get_job", lambda _j: {"status": "queued", "url": "https://example.com/"})
    monkeypatch.setattr(fs, "mark_job_started", lambda _j: None)
    monkeypatch.setattr(fs, "release_job_lease", lambda *_a: None)

    def claim(job_id, owner, *_a, **_k):
        owners.append(owner)
        return True

    monkeypatch.setattr(fs, "claim_job_lease", claim)

    async def fake_scan(*_a, **_k):
        return None

    monkeypatch.setattr(worker_app, "run_one_time_scan", fake_scan)
    await _run()
    await _run()
    assert len(set(owners)) == 2


def test_the_lease_claim_is_a_real_firestore_transaction():
    """Read-then-write without one has exactly the race the lease exists to
    prevent, and losing the decorator would not otherwise fail anything.
    """
    assert isinstance(fs._claim_lease, firestore_transaction._Transactional)
    assert isinstance(fs._clear_lease, firestore_transaction._Transactional)
    assert isinstance(fs._merge_page_field, firestore_transaction._Transactional)


# --- B4: a failure after complete_job() must not cost the report email ----


def _run_scan_source() -> str:
    import inspect as _inspect

    from mad_platform.agents import orchestrator

    return _inspect.getsource(orchestrator.run_one_time_scan)


def test_the_post_completion_block_swallows_its_own_failures():
    """Everything after complete_job() is notification: Slack, the Gemini
    email draft, the attachment build, the Resend upload. It used to be
    bare, so a transient failure there propagated -- the handler correctly
    refused to downgrade the job and then re-raised anyway, which
    worker_app turns into a 500 and Cloud Tasks into a retry that hits the
    `already_completed` short-circuit and never tries the email again. The
    promise on the queued status page ("we'll email your full report as
    soon as it's ready") went silently unkept for exactly the class of
    failure the retry machinery exists to absorb.
    """
    source = _run_scan_source()
    tail = source.split("fs.complete_job(", 1)[1]
    inner = tail.split("notify.summary(", 1)[0]
    assert "try:" in inner, "the notification tail is no longer guarded"

    # And the guard must not re-raise.
    guarded = tail.split("except Exception", 1)[1].split("except Exception", 1)[0]
    assert "raise" not in guarded, "the post-completion handler re-raises again"
    assert "logger.exception" in guarded, "a swallowed failure must still be visible"


def test_the_notification_tail_runs_after_the_job_is_marked_complete():
    """The ordering B4's original fix established, which the new try block
    must not have disturbed: status and summary are written first, so the
    status page renders whatever happens below.
    """
    source = _run_scan_source()
    assert source.index("fs.complete_job(") < source.index("notify.summary(")
    assert source.index("fs.complete_job(") < source.index("send_report_email(")


# --- F6: the visitor never sees raw internal exception text ---------------


def test_a_failed_scan_records_an_operator_written_message_not_the_exception():
    """The `error` field is returned verbatim by GET /api/status/{job_id}
    and rendered on the public status page. It is escaped there, so this is
    information disclosure rather than XSS -- a MissingConfigError naming
    an env var, a google.api_core error naming the GCP project and a
    resource path, or a FetchError carrying the internal Playwright
    message.
    """
    source = _run_scan_source()
    # Comments stripped: the one above the fix names `str(exc)` precisely
    # because that was the bug.
    code = "\n".join(
        line for line in source.splitlines() if not line.lstrip().startswith("#")
    )
    assert "str(exc)" not in code, "no exception text may reach the job record"
    # The real text still has to go somewhere.
    handler = code.split("except Exception", 1)[1]
    assert "logger.exception" in handler
