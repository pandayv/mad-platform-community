"""Scan worker: the Cloud Tasks push target that actually runs the
pipeline. Split out of app.py so the public-facing scan-onboarding
service can stay thin (no Playwright/Chromium, high concurrency, cheap)
while this service does the one heavy, memory-hungry thing -- one scan
at a time, per instance (deploy with containerConcurrency=1).

Not publicly reachable: only the Cloud Tasks queue's dedicated invoker
identity can call /run, the same OIDC-invoker pattern already used for
scan-wcag-poller and pattern-miner.

Run locally: .venv/bin/uvicorn mad_platform.web.worker_app:app --reload --port 8081
"""

from __future__ import annotations

import logging
import uuid

from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from pydantic import BaseModel

from mad_platform import config
from mad_platform.agents.orchestrator import run_one_time_scan
from mad_platform.logging_setup import configure_logging
from mad_platform.state import firestore_client as fs
from mad_platform.tools.issue_sink import CsvIssueSink

# See mad_platform/logging_setup.py for why this is not basicConfig().
configure_logging()

logger = logging.getLogger("mad_platform.worker")

@asynccontextmanager
async def _lifespan(_app: FastAPI):
    """Refuse to start if the pipeline's required config is missing, so a
    misconfigured revision fails visibly at startup instead of at the
    first scan. At startup, not at import -- see app.py's own _lifespan
    and mad_platform/config.py for why import must stay side-effect-free.
    """
    config.validate_pipeline_config()
    yield


app = FastAPI(title="MAD Platform Scan Worker", lifespan=_lifespan)


class RunRequest(BaseModel):
    job_id: str


# Must match scan-queue's own --max-attempts (setup.sh / SETUP.md step 7);
# test_route_validation.py checks the two stay in sync the same way it
# already does for the retention-window constant. Cloud Tasks' retry-count
# header is 0 on the first delivery, so the last allowed delivery is
# max_attempts - 1.
SCAN_QUEUE_MAX_ATTEMPTS = 3


@app.get("/")
async def health() -> dict:
    return {"status": "ok"}


@app.post("/run")
async def run_scan(req: RunRequest, request: Request) -> dict:
    job_id = req.job_id
    # Missing/unparseable outside real Cloud Tasks delivery -- treated as
    # "no retry coming," the safe default that matches every caller other
    # than the real queue (tests use fake_request(), see conftest.py).
    try:
        retry_count = int(request.headers.get("X-CloudTasks-TaskRetryCount", "0"))
    except ValueError:
        retry_count = SCAN_QUEUE_MAX_ATTEMPTS - 1
    is_final_attempt = retry_count >= SCAN_QUEUE_MAX_ATTEMPTS - 1
    job = fs.get_job(job_id)
    if job is None:
        # No such job: retrying this dispatch can never succeed, so
        # return 2xx (not 404/500) to tell Cloud Tasks not to keep trying.
        logger.error("[%s] No such job -- dropping dispatch", job_id)
        return {"ok": False, "reason": "no such job"}
    if job.get("status") == "completed" and job.get("summary"):
        # A retried dispatch landing after an earlier attempt already
        # finished (e.g. it failed only on the post-completion email
        # step) -- idempotent no-op rather than re-running the whole scan.
        #
        # "and job.get('summary')" is load-bearing, not belt-and-braces. A
        # job marked completed WITHOUT a summary is not actually finished
        # from any consumer's point of view: the status page can't render
        # it and /report/{job_id}/tickets.csv 404s. Short-circuiting on
        # status alone meant such a job could never be repaired -- every
        # retry returned 2xx and the summary was never written. Jobs
        # completed by the current code always have one (complete_job
        # writes both in a single update), so this only ever catches
        # records left behind by the older two-write path; letting them
        # fall through re-runs from the last checkpoint and completes them
        # properly.
        logger.info("[%s] Already completed -- skipping re-run", job_id)
        return {"ok": True, "already_completed": True}
    if job.get("status") == "completed":
        logger.warning("[%s] Completed but has no summary -- re-running to repair it", job_id)

    # Claim the job before doing anything billable. The completed-check
    # above catches a retry that arrives after a previous attempt finished;
    # it cannot catch one that arrives while a previous attempt is still
    # running, which is exactly what Cloud Tasks produces when a scan
    # outlives the queue's dispatch deadline -- deadline expiry does not
    # require the first attempt to have stopped. Two live workers on one
    # job duplicate every Gemini call, race each other's page checkpoints,
    # both reach route_and_file, and both email the owner a report.
    # containerConcurrency=1 does not help: the two are on different
    # instances out of the max-instances pool.
    lease_owner = str(uuid.uuid4())
    if not fs.claim_job_lease(job_id, lease_owner):
        # 2xx, not an error: another worker holds this job and is making
        # progress, so this dispatch should be dropped rather than retried
        # into the same collision.
        logger.warning("[%s] Another worker holds this job -- dropping duplicate dispatch", job_id)
        return {"ok": True, "already_running": True}

    fs.mark_job_started(job_id)
    url = job["url"]
    sink = CsvIssueSink()

    try:
        await run_one_time_scan(
            url,
            job_id=job_id,
            issue_sink=sink,
            owner_contact=job.get("owner_contact"),
            is_final_attempt=is_final_attempt,
        )
    except Exception:
        # Let this propagate as a 500: unlike the old in-process
        # fire-and-forget task, Cloud Tasks needs the failure signal to
        # decide whether to retry (bounded by the queue's own
        # max-attempts). run_one_time_scan only wrote status=failed to
        # Firestore if is_final_attempt was True; a retry resumes from the
        # last completed checkpoint rather than starting over.
        logger.exception("[%s] Scan failed (%s), retry_count=%d, final=%s", job_id, url, retry_count, is_final_attempt)
        raise
    finally:
        # Released either way, so a retry of a genuinely failed scan starts
        # immediately instead of waiting out JOB_LEASE_SECONDS. The lease's
        # own expiry is the fallback for a worker that dies without getting
        # here at all.
        fs.release_job_lease(job_id, lease_owner)

    # No summary write here any more. It used to happen at this point --
    # after run_one_time_scan had already flipped the job to "completed"
    # and then spent several seconds posting to Slack, drafting an email
    # summary with Gemini and uploading attachments to Resend. During that
    # window /api/status returned status "completed" with summary null,
    # which froze the status page permanently. The orchestrator now writes
    # both fields together, atomically, before any of that work starts
    # (see firestore_client.complete_job).
    return {"ok": True}
