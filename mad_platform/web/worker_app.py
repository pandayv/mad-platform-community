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

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel

from mad_platform.agents.orchestrator import run_one_time_scan
from mad_platform.agents.reporter import compute_score, score_color
from mad_platform.state import firestore_client as fs
from mad_platform.tools.issue_sink import CsvIssueSink
from mad_platform.web import theme

_handler = logging.StreamHandler()
_handler.setFormatter(logging.Formatter("%(levelname)s:%(name)s:%(message)s"))
_mad_logger = logging.getLogger("mad_platform")
_mad_logger.setLevel(logging.INFO)
_mad_logger.addHandler(_handler)
_mad_logger.propagate = False

logger = logging.getLogger("mad_platform.worker")

app = FastAPI(title="MAD Platform Scan Worker")


class RunRequest(BaseModel):
    job_id: str


@app.get("/")
async def health() -> dict:
    return {"status": "ok"}


@app.post("/run")
async def run_scan(req: RunRequest) -> dict:
    job_id = req.job_id
    job = fs.get_job(job_id)
    if job is None:
        # No such job: retrying this dispatch can never succeed, so
        # return 2xx (not 404/500) to tell Cloud Tasks not to keep trying.
        logger.error("[%s] No such job -- dropping dispatch", job_id)
        return {"ok": False, "reason": "no such job"}
    if job.get("status") == "completed":
        # A retried dispatch landing after an earlier attempt already
        # finished (e.g. it failed only on the post-completion email
        # step) -- idempotent no-op rather than re-running the whole scan.
        logger.info("[%s] Already completed -- skipping re-run", job_id)
        return {"ok": True, "already_completed": True}

    fs.mark_job_started(job_id)
    url = job["url"]
    sink = CsvIssueSink()

    try:
        result = await run_one_time_scan(url, job_id=job_id, issue_sink=sink, owner_contact=job.get("owner_contact"))
    except Exception:
        # Let this propagate as a 500: unlike the old in-process
        # fire-and-forget task, Cloud Tasks needs the failure signal to
        # decide whether to retry (bounded by the queue's own
        # max-attempts). run_one_time_scan already wrote status=failed to
        # Firestore before re-raising; a retry resumes from the last
        # completed checkpoint rather than starting over.
        logger.exception("[%s] Scan failed (%s)", job_id, url)
        raise

    all_ranked = [f for _, f, _ in result.filed + result.escalated + result.already_filed]
    counts = {"critical": 0, "high": 0, "medium": 0, "low": 0}
    for r in all_ranked:
        counts[r.severity.lower()] = counts.get(r.severity.lower(), 0) + 1
    score = compute_score(all_ranked)

    fs.save_scan_summary(
        job_id,
        {
            "score": score,
            "score_color": score_color(score),
            "severity_counts": counts,
            "principle_counts": theme.principle_counts([r.wcag_criterion for r in all_ranked]),
            "total_findings": len(all_ranked),
            "filed_count": len(result.filed) + len(result.already_filed),
            "escalated_count": len(result.escalated),
            "report_uri": result.report_uri,
            # CSV rows only exist in-memory on this one sink instance during
            # this one scan -- exported and persisted here (Firestore, not a
            # new storage_client path) so the download route can serve it
            # long after this worker instance is gone.
            "csv_export": sink.export(),
        },
    )
    return {"ok": True}
