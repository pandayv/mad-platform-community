"""ScanJob checkpointing in Firestore.

Each stage writes its completion to the job record as it finishes. On
restart, a caller reads the last completed checkpoint per page and
resumes from the next incomplete stage -- it never blindly re-runs a job
from scratch.

Database is 'scan-firestore', not '(default)' -- an easy-to-miss gotcha:
forgetting the database= argument silently connects to a database that
doesn't have any of this project's data.
"""

from __future__ import annotations

import logging
import os
import secrets
import uuid
from datetime import datetime, timedelta, timezone
from functools import lru_cache
from typing import Any

from google.api_core import exceptions as gcloud_exceptions
from google.cloud import firestore

from mad_platform import config

logger = logging.getLogger("mad_platform.firestore")


@lru_cache(maxsize=1)
def get_client() -> firestore.Client:
    """The one Firestore client for this process.

    Built lazily, on first use, rather than at import: a module-level
    client makes importing anything that touches this file require live
    GCP credentials, which is what made this codebase impossible to write
    a test against. lru_cache gives the
    module-global-singleton behavior the old code had, while leaving
    import itself side-effect-free and the accessor monkeypatchable.

    rag.py calls this too rather than opening its own client to the same
    database -- there is one database, so there should be one connection
    pool and one auth flow.
    """
    return firestore.Client(project=config.project_id(), database=config.FIRESTORE_DATABASE)


def _collection(name: str) -> firestore.CollectionReference:
    return get_client().collection(name)


def _jobs() -> firestore.CollectionReference:
    return _collection("scan_jobs")


def _tickets() -> firestore.CollectionReference:
    return _collection("filed_tickets")  # idempotency_key -> ticket_id


def _escalations() -> firestore.CollectionReference:
    return _collection("escalations")  # review queue (per-job "finding" + cross-cutting admin kinds)


def _kb_version() -> firestore.DocumentReference:
    return _collection("knowledge_base_version").document("wcag")


def _learned_patterns() -> firestore.CollectionReference:
    return _collection("learned_patterns")  # SME-confirmed Analyst/Editor patterns


def _usage() -> firestore.CollectionReference:
    return _collection("usage_counters")  # anti-abuse rate limits + the monthly scan budget


def _feedback() -> firestore.CollectionReference:
    return _collection("feedback")  # immediate "was this helpful" responses, testimonial source


def _email_codes() -> firestore.CollectionReference:
    return _collection("email_verifications")  # doc id = normalized email


def _devices() -> firestore.CollectionReference:
    return _collection("verified_devices")  # doc id = sha256(device cookie token)


# Community-fork limits -- adjust here, not scattered through call sites.
#
# Environment-overridable, and that is the actual fix rather than a
# convenience. MAX_SCANS_PER_IP_PER_DAY sat at 15 in source under a
# "TEMP: raised for benchmark testing, revert to 5 after" comment that
# shipped to production and stayed there: a benchmark run needed a higher
# ceiling for an afternoon, the only way to get one was to edit the
# constant and redeploy, and the revert never happened. A comment that
# says the value is wrong is not a control. So the numbers below are the
# *intended* policy, and a temporary change is now an env var on one
# revision that disappears the moment it is rolled back -- it cannot
# silently become the permanent value in source.
#
# Read at call time, not into a module constant, so a test can set them
# and so nothing here needs an environment at import (see config.py).
_LIMIT_DEFAULTS = {
    # Per-email lower than per-IP on purpose: per-IP is the outer ceiling
    # (a shared NAT is still one real household or office), per-email is
    # the inner one (one person shouldn't need more than a handful of
    # scans in a day), so the two bound the same traffic from different
    # angles rather than duplicating one limit.
    "MAD_MAX_SCANS_PER_EMAIL_PER_DAY": 10,
    "MAD_MAX_SCANS_PER_IP_PER_DAY": 15,
    # A scan-count proxy for the $ budget, see DECISIONS_LOG.md -- raised
    # from 500 for the public launch under real cost data uncertainty
    # (BigQuery billing export was only just linked); revisit once actual
    # per-scan cost is known from real usage.
    "MAD_MAX_SCANS_PER_MONTH": 1000,
    "MAD_MAX_FEEDBACK_PER_IP_PER_DAY": 10,
    # Verification-code guesses one address may submit in a day, across
    # every email it tries. MAX_CODE_ATTEMPTS below bounds guesses per
    # *code*; this bounds them per *submitter*, so requesting a fresh code
    # (which resets `attempts` to 0 by design) is not an unlimited supply
    # of new guesses. Generous on purpose -- a real visitor needs at most a
    # handful, and a shared NAT may carry several real visitors -- while
    # still leaving an attacker 50 tries against a 1,000,000-code space
    # instead of as many as they can issue requests for.
    "MAD_MAX_CODE_ATTEMPTS_PER_IP_PER_DAY": 50,
}


def _limit(name: str) -> int:
    """An unparseable or non-positive override falls back to the default
    rather than being honored: a typo'd env var must not be able to set an
    effective limit of 0 (nobody can scan) or a negative one (nobody is
    limited).
    """
    raw = os.environ.get(name)
    if raw:
        try:
            value = int(raw)
        except ValueError:
            logger.warning("%s=%r is not an integer -- using the default", name, raw)
        else:
            if value > 0:
                return value
            logger.warning("%s=%r is not positive -- using the default", name, raw)
    return _LIMIT_DEFAULTS[name]


def max_scans_per_email_per_day() -> int:
    return _limit("MAD_MAX_SCANS_PER_EMAIL_PER_DAY")


def max_scans_per_ip_per_day() -> int:
    return _limit("MAD_MAX_SCANS_PER_IP_PER_DAY")


def max_scans_per_month() -> int:
    return _limit("MAD_MAX_SCANS_PER_MONTH")


def max_feedback_per_ip_per_day() -> int:
    return _limit("MAD_MAX_FEEDBACK_PER_IP_PER_DAY")


def max_code_attempts_per_ip_per_day() -> int:
    return _limit("MAD_MAX_CODE_ATTEMPTS_PER_IP_PER_DAY")


# How long a scan record (the job document: submitted URL, owner email,
# every finding) stays before Firestore's TTL sweep removes it, and the
# same for the escalation and feedback documents that hang off one.
#
# These collections had no `expires_at` at all, while usage counters,
# verification codes and device tokens all did -- so the three collections
# holding the *most* personal data were the three kept forever, and the
# privacy page's "it deletes itself on schedule" paragraph sat one
# paragraph below the list of what a scan collects. Writing the field is
# only half of it: a Firestore TTL policy on each of `scan_jobs`,
# `escalations` and `feedback` keyed to `expires_at` has to exist in the
# project for anything to actually be deleted. README.md's deploy steps
# create them; DECISIONS_LOG.md records why the retention window is what
# it is.
SCAN_RECORD_RETENTION_DAYS = 365


def _scan_record_expiry(now: datetime) -> datetime:
    return now + timedelta(days=SCAN_RECORD_RETENTION_DAYS)


# A worker's claim on a job. Long enough to cover a scan that is running
# slowly (p99 is well under 5 minutes; this is 3x that) and short enough
# that a worker killed mid-scan -- OOM, an instance eviction, a deploy --
# does not lock the job out of its remaining Cloud Tasks attempts. The
# lease is also released explicitly when the worker finishes either way,
# so this ceiling only matters when a worker dies without unwinding.
JOB_LEASE_SECONDS = 900

# Email verification -- pattern adapted from a sibling project's own
# battle-tested login-code flow (reviewed read-only for reference, not
# copied wholesale: that system tracks per-process in-memory cooldowns,
# which doesn't work here since scan-onboarding runs multiple concurrent
# Cloud Run instances with no shared memory -- Firestore is the only
# consistent place to keep this state, same reason the scan quota above
# already lives here instead of in a dict).
EMAIL_CODE_TTL_MINUTES = 10
MAX_CODE_ATTEMPTS = 5
# Required gap (seconds) before the Nth code request for the same email;
# index clamped to the last entry once it's reached. First two requests
# are instant, then the wait grows -- slows a flood without ever
# permanently locking out someone whose first email just landed in spam.
CODE_REQUEST_COOLDOWNS = [0, 0, 60, 300, 900]
CODE_REQUEST_RESET_AFTER_SECONDS = 3600
REMEMBER_DEVICE_DAYS = 30

# Stages, in order -- used to answer "what's the next incomplete stage".
PAGE_STAGES = ["crawled", "analyzed", "verified"]


def create_job(
    url: str, trigger_type: str = "one-time", owner_contact: str | None = None, status: str = "in_progress"
) -> str:
    """owner_contact is the submitter's email -- required by the community
    fork's /scan route (not enforced here, so internal/admin callers like
    the WCAG poller can still omit it). It's the one field that answers
    "who does this job belong to", used for both the report email and the
    per-job review link below.

    status defaults to "in_progress" for callers that run the pipeline
    synchronously in the same process (the WCAG poller, run_scan.py).
    /scan passes "queued": the job exists and is visible on its status
    page immediately, but the pipeline itself doesn't start until a
    worker actually picks up the Cloud Tasks dispatch -- see
    mark_job_started().
    """
    job_id = str(uuid.uuid4())
    now = datetime.now(timezone.utc)
    _jobs().document(job_id).set(
        {
            "url": url,
            "trigger_type": trigger_type,
            "status": status,
            "pages": {},
            "owner_contact": owner_contact,
            "review_token": secrets.token_urlsafe(24),
            "created_at": now,
            "updated_at": now,
            # Firestore TTL field. This document holds the submitter's
            # email and every finding of their scan; see
            # SCAN_RECORD_RETENTION_DAYS for why it now has an expiry and
            # what still has to be configured in GCP for it to fire.
            "expires_at": _scan_record_expiry(now),
        }
    )
    return job_id


def mark_job_started(job_id: str) -> None:
    """queued -> in_progress, called by the scan worker the moment it
    actually picks up a dispatched task -- distinct from create_job's
    status because the two can now happen seconds or minutes apart, with
    the job queued behind others in between. started_at (separate from
    created_at) is what the status page's elapsed-time / "taking longer
    than usual" warning anchors to -- using created_at there would count
    queue wait time as if it were scan time.
    """
    now = datetime.now(timezone.utc)
    _jobs().document(job_id).update({"status": "in_progress", "started_at": now, "updated_at": now})


def verify_review_token(job_id: str, token: str) -> bool:
    """True only if `token` matches the random secret generated for this
    job at create_job() time. This is the entire access-control mechanism
    for the per-job review link -- replaces the old single shared
    MAD_REVIEW_CODE admin cookie, which gated every job's escalations at
    once. Deliberately not a Firestore query filtered by token (that would
    let a wrong/guessed token silently match nothing and look identical to
    "no results" instead of "wrong token") -- fetch the real job and
    compare directly.
    """
    job = get_job(job_id)
    if not job:
        return False
    return secrets.compare_digest(job.get("review_token", ""), token)


def get_job(job_id: str) -> dict[str, Any] | None:
    doc = _jobs().document(job_id).get()
    return doc.to_dict() if doc.exists else None


def get_page_stage(job_id: str, page_url: str) -> str | None:
    """Returns the last completed stage for a page, or None if not started
    -- this is what a resuming caller checks before deciding to redo work.
    """
    job = get_job(job_id)
    if not job:
        return None
    return job.get("pages", {}).get(page_url, {}).get("stage")


def checkpoint_page_crawled(job_id: str, page_url: str) -> None:
    _set_page_field(job_id, page_url, {"stage": "crawled"})


def checkpoint_page_analyzed(job_id: str, page_url: str, raw_finding_count: int) -> None:
    _set_page_field(job_id, page_url, {"stage": "analyzed", "raw_finding_count": raw_finding_count})


def checkpoint_page_retry(job_id: str, page_url: str, reason: str) -> None:
    """Records that the bounded retry gate sent this page back for one
    more pass, and why. No "stage" field here on purpose -- this doesn't
    move the page forward, it just makes the decision auditable.
    """
    _set_page_field(job_id, page_url, {"retried": True, "retry_reason": reason})


def checkpoint_page_verified(job_id: str, page_url: str, verified_findings: list[dict], retried: bool = False) -> None:
    _set_page_field(job_id, page_url, {"stage": "verified", "findings": verified_findings, "retried": retried})


def set_job_phase(job_id: str, phase: str) -> None:
    """A coarser signal than the per-page stage checkpoints, for the gaps
    between them where nothing else gets written -- page selection (after
    the entry crawl, before any page-level checkpoint exists) and the
    ranking/filing/report tail (after every page hits "verified" but before
    complete_job). Without this, a status page watching only per-page
    stages goes silent during both, and a healthy multi-second wait reads
    identically to a hang.
    """
    _jobs().document(job_id).update({"phase": phase, "updated_at": datetime.now(timezone.utc)})


def set_selected_pages(job_id: str, pages: list[str]) -> None:
    """Records the page list this job's selection step actually decided on.

    Deliberately a separate field from the `pages` checkpoint map, which
    exists for a different purpose. Resume used to key off `pages` being
    non-empty and treat its keys as "the pages this job chose" -- but the
    entry URL is checkpointed into `pages` *before* selection runs, so a
    job that died in the selection call (a Gemini call: exactly where it
    dies) resumed with a page list of one. The scan then completed
    "successfully" having checked a single page instead of three, with
    nothing in the report, the status page or the logs saying the scope had
    been cut. A crash checkpoint is not a decision; this field is the
    decision, and only this field may drive resume.
    """
    _jobs().document(job_id).update(
        {"selected_pages": pages, "updated_at": datetime.now(timezone.utc)}
    )


def complete_job(job_id: str, summary: dict[str, Any]) -> None:
    """Marks the job completed AND writes its summary, in one write.

    `summary` is required, and the two fields go out together, on purpose.
    They used to be two writes from two different services -- the
    orchestrator flipped status to "completed", and the worker wrote the
    summary afterwards, with a Slack post, a Gemini call and a Resend
    upload in between. For those several seconds `/api/status/{job_id}`
    returned `{"status": "completed", "summary": null}`, which the status
    page (polling every 2s) could not render: it threw and stopped
    polling, leaving the user on a frozen "Starting..." screen for a scan
    that had actually succeeded. Worse, if the run died in that window,
    the Cloud Tasks retry short-circuited on `status == "completed"` and
    the summary was never written at all.

    A single atomic update makes `status == "completed"` mean "the summary
    is there" by construction, so that window cannot reopen -- including
    from some future third caller. Do not split this back into two writes,
    and do not add a `summary=None` default: the required argument is the
    guard rail.
    """
    _jobs().document(job_id).update(
        {"status": "completed", "summary": summary, "updated_at": datetime.now(timezone.utc)}
    )


def save_scan_summary(job_id: str, summary: dict[str, Any]) -> None:
    """Updates the summary of an ALREADY-COMPLETED job -- the post-hoc
    path only (app.py appends a CSV row when the site owner resolves a
    pending escalation after the fact). The completing write itself goes
    through complete_job() above, which writes status and summary
    together; calling this to publish a summary for the first time
    reintroduces the race that docstring describes.
    """
    _jobs().document(job_id).update({"summary": summary, "updated_at": datetime.now(timezone.utc)})


def fail_job(job_id: str, error: str) -> None:
    _jobs().document(job_id).update(
        {"status": "failed", "error": error, "updated_at": datetime.now(timezone.utc)}
    )


def get_ticket_for_finding(idempotency_key: str) -> str | None:
    """Checks whether a finding has already been filed -- the idempotency
    guard that keeps a retried pipeline step from double-filing.
    """
    doc = _tickets().document(idempotency_key).get()
    return doc.to_dict()["ticket_id"] if doc.exists else None


def record_ticket_for_finding(idempotency_key: str, ticket_id: str) -> None:
    _tickets().document(idempotency_key).set(
        {"ticket_id": ticket_id, "filed_at": datetime.now(timezone.utc)}
    )


def create_escalation(idempotency_key: str, finding_data: dict, job_id: str | None = None) -> str:
    """Adds a finding to the review queue -- no ticket is filed for it until
    resolved. Escalated findings wait; they don't act-then-flag like the
    non-escalated majority.

    job_id: pass this for "finding" kind escalations, which belong to one
    scan's owner and are what the per-job review link (verify_review_token)
    scopes access to. Leave it None for cross-cutting admin kinds
    (kb_version_change, learned_pattern) that don't belong to any one scan
    owner and must stay on the separate admin-only path -- never route
    those through a per-job token.

    create(), not set(): set() overwrote whatever was already at this
    document ID, resetting an escalation's status back to "pending" and
    discarding the reviewer's disposition. That fired for real whenever
    the same key came round twice -- a Cloud Tasks retry of a scan whose
    owner had already resolved an item, or (before the key included
    job_id) another user's scan of the same site entirely. An escalation
    document is created once and thereafter only ever moves forward
    through resolve_escalation(); nothing should be able to rewind it, so
    a duplicate create is a no-op that keeps the existing record rather
    than a silent clobber.
    """
    doc_ref = _escalations().document(idempotency_key)
    now = datetime.now(timezone.utc)
    try:
        doc_ref.create(
            {
                **finding_data,
                "job_id": job_id,
                "status": "pending",
                "created_at": now,
                # Same TTL window as the scan job it belongs to -- an
                # escalation carries the same finding detail, so keeping it
                # after its job is gone would defeat the job's own expiry.
                "expires_at": _scan_record_expiry(now),
            }
        )
    except gcloud_exceptions.AlreadyExists:
        existing = doc_ref.get().to_dict() or {}
        logger.info(
            "Escalation %s already exists (job_id=%r, status=%r) -- keeping it, not overwriting",
            idempotency_key, existing.get("job_id"), existing.get("status"),
        )
    return idempotency_key


def list_escalations_for_job(job_id: str) -> list[dict[str, Any]]:
    """The job-scoped equivalent of list_pending_escalations() -- what the
    per-job review link shows, instead of every escalation in the system.
    """
    return [
        {"id": doc.id, **doc.to_dict()}
        for doc in _escalations().where(filter=firestore.FieldFilter("job_id", "==", job_id)).stream()
    ]


def list_pending_escalations() -> list[dict[str, Any]]:
    return [{"id": doc.id, **doc.to_dict()} for doc in _escalations().where(
        filter=firestore.FieldFilter("status", "==", "pending")
    ).stream()]


def get_escalation(escalation_id: str) -> dict[str, Any] | None:
    """Fetches one escalation regardless of status -- unlike
    list_pending_escalations(), this also returns already-resolved ones,
    for callers checking on an outcome rather than building a work queue.
    """
    doc = _escalations().document(escalation_id).get()
    return {"id": doc.id, **doc.to_dict()} if doc.exists else None


def resolve_escalation(escalation_id: str, disposition: str, reviewer: str = "sme") -> dict[str, Any]:
    """disposition: 'confirm' or 'dismiss'. Returns the escalation's data
    so the caller (Action Agent) can file a ticket if confirmed.
    """
    doc_ref = _escalations().document(escalation_id)
    doc = doc_ref.get()
    if not doc.exists:
        raise ValueError(f"No escalation found with id {escalation_id!r}")
    data = doc.to_dict()
    doc_ref.update(
        {
            "status": "resolved",
            "disposition": disposition,
            "reviewer": reviewer,
            "resolved_at": datetime.now(timezone.utc),
        }
    )
    return data


def iter_dismissed_findings() -> list[dict[str, Any]]:
    """Every dismissed (confirmed=False) finding Editor has ever produced,
    across every job -- the raw substrate the pattern miner clusters. Reads
    the whole scan_jobs collection; fine at this project's volume, and
    simpler than maintaining a second denormalized index for a batch job
    that doesn't run on a tight schedule.
    """
    dismissed = []
    for job_doc in _jobs().stream():
        job = job_doc.to_dict()
        for page_url, page in job.get("pages", {}).items():
            for f in page.get("findings", []):
                if f.get("confirmed") is False:
                    dismissed.append(
                        {
                            "job_id": job_doc.id,
                            "page_url": page_url,
                            "wcag_criterion": f.get("wcag_criterion", ""),
                            "rationale": f.get("rationale", ""),
                        }
                    )
    return dismissed


def save_learned_pattern(pattern_id: str, data: dict[str, Any]) -> None:
    """Persists an SME-confirmed dismissal pattern -- the actual persistent
    memory Editor's prompt reads back on every future scan. Only ever
    written after human confirmation (see pattern_miner.resolve_pattern_escalation);
    a candidate pattern that's merely mined, not yet confirmed, lives only
    in the escalations queue.
    """
    _learned_patterns().document(pattern_id).set(
        {**data, "confirmed_at": datetime.now(timezone.utc)}
    )


def list_learned_patterns() -> list[dict[str, Any]]:
    return [{"id": doc.id, **doc.to_dict()} for doc in _learned_patterns().stream()]


def get_kb_version() -> dict[str, Any] | None:
    doc = _kb_version().get()
    return doc.to_dict() if doc.exists else None


def touch_kb_check(checked_version: str) -> None:
    """Records that a freshness check just ran, independent of whether the
    version actually changed -- so "last checked" is always accurate even
    on a no-op tick.
    """
    _kb_version().set(
        {"last_checked_version": checked_version, "last_checked_at": datetime.now(timezone.utc)},
        merge=True,
    )


def set_kb_version(version: str) -> None:
    """Called after a successful refresh (auto or SME-confirmed) -- records
    which version the currently-stored embeddings actually reflect.
    """
    _kb_version().set({"version": version, "updated_at": datetime.now(timezone.utc)}, merge=True)


def quota_email_key(email: str) -> str:
    """The per-email quota identity.

    Normalized for the quota key only, never for where the report is
    actually sent -- gmail.com/googlemail.com ignore dots and treat
    +anything as an alias of the same inbox, so without this,
    "me+1@gmail.com", "me+2@gmail.com", ... would each get their own
    fresh daily quota from a single real mailbox. Stripping a "+suffix"
    for every domain too, since it's a widely (if not universally)
    honored convention -- a cheap partial mitigation, not a complete one.

    Split out of check_and_reserve_scan_quota so it can be exercised
    directly by a test without a Firestore connection.
    """
    quota_email = email.strip().lower()
    local, _, domain = quota_email.rpartition("@")
    local = local.split("+", 1)[0]
    if domain in ("gmail.com", "googlemail.com"):
        local = local.replace(".", "")
    return f"{local}@{domain}"


@firestore.transactional
def _check_and_reserve(transaction, month_ref, email_ref, ip_ref, now) -> tuple[bool, str]:
    """The read-check-write body, run inside a real Firestore transaction.

    Firestore requires every read in a transaction to precede every write,
    which is exactly the shape this needs anyway. If any of the three
    documents changes between the reads and the commit, the transaction
    retries with fresh reads -- which is the whole point: two concurrent
    submissions can no longer both read "one under the limit" and both
    proceed.

    Counts are written as explicit values rather than firestore.Increment
    transforms. Increment is atomic per-field, which is what made the old
    version *look* safe, but atomic increments say nothing about the gap
    between the read that authorized the write and the write itself. Inside
    a serializable transaction the value we read is the value we are
    incrementing, so writing it out plainly is both correct and honest
    about the mechanism doing the work.
    """
    month_doc = month_ref.get(transaction=transaction)
    month_count = month_doc.to_dict().get("scans", 0) if month_doc.exists else 0
    email_doc = email_ref.get(transaction=transaction)
    email_count = email_doc.to_dict().get("count", 0) if email_doc.exists else 0
    ip_doc = ip_ref.get(transaction=transaction)
    ip_count = ip_doc.to_dict().get("count", 0) if ip_doc.exists else 0

    if month_count >= max_scans_per_month():
        return False, "We've hit our free capacity for this month. Please check back next month."
    if email_count >= max_scans_per_email_per_day():
        return False, "You've reached today's scan limit for this email address. Please try again tomorrow."
    if ip_count >= max_scans_per_ip_per_day():
        return False, "Too many scans from this network today. Please try again tomorrow."

    day_expiry = now + timedelta(days=2)
    transaction.set(email_ref, {"count": email_count + 1, "updated_at": now, "expires_at": day_expiry}, merge=True)
    transaction.set(ip_ref, {"count": ip_count + 1, "updated_at": now, "expires_at": day_expiry}, merge=True)
    transaction.set(
        month_ref,
        {"scans": month_count + 1, "updated_at": now, "expires_at": now + timedelta(days=35)},
        merge=True,
    )
    return True, ""


def check_and_reserve_scan_quota(email: str, ip: str) -> tuple[bool, str]:
    """The anti-abuse + budget gate, called once per /scan submission,
    before create_job(). Three independent checks, all must pass:
    per-email daily cap, per-IP daily cap, and the global monthly scan
    budget. Check and reservation happen together inside one Firestore
    transaction -- so this doubles as the reservation, not just a check: a
    caller that gets `(True, "")` back has already consumed one unit of
    quota and should not increment again separately.

    The transaction is load-bearing, not decoration. This function used to
    claim atomicity in this docstring while doing all three reads first and
    all three writes last, with nothing tying them together -- so N
    concurrent requests could all read "under the limit" and all proceed.
    This is the only thing between a public, unauthenticated endpoint and
    unbounded Gemini/Playwright spend, and scan-onboarding runs at
    concurrency 20 across multiple instances, so that was a live race.

    Returns (allowed, reason). reason is empty when allowed=True, and a
    short human-readable string when False, meant to be shown directly to
    the visitor (e.g. "You've reached today's scan limit for this email").

    Every document written here gets an `expires_at` a little past its own
    natural relevance window -- daily counters at +2 days, the monthly
    counter at +35 days -- matched by a Firestore TTL policy on this
    collection (see DECISIONS_LOG.md). Without that, raw IPs and emails
    would sit in these documents indefinitely, which is a real mismatch
    with the privacy policy's "used briefly... not stored long-term" claim,
    not just a theoretical one.
    """
    now = datetime.now(timezone.utc)
    month_ref, email_ref, ip_ref = _quota_refs(email, ip, now)
    return _check_and_reserve(get_client().transaction(), month_ref, email_ref, ip_ref, now)


def _quota_refs(email: str, ip: str, now: datetime):
    """The three counter documents one submission touches.

    Extracted so reserve and refund cannot drift on how a key is built --
    a refund that computed a different document ID would silently credit
    a counter nobody is reading.
    """
    day_key = now.strftime("%Y-%m-%d")
    month_key = now.strftime("%Y-%m")
    usage = _usage()
    return (
        usage.document(f"month_{month_key}"),
        usage.document(f"email_{quota_email_key(email)}_{day_key}"),
        usage.document(f"ip_{ip}_{day_key}"),
    )


@firestore.transactional
def _release_reservation(transaction, month_ref, email_ref, ip_ref, now) -> None:
    month_doc = month_ref.get(transaction=transaction)
    email_doc = email_ref.get(transaction=transaction)
    ip_doc = ip_ref.get(transaction=transaction)

    for ref, doc, field in (
        (month_ref, month_doc, "scans"),
        (email_ref, email_doc, "count"),
        (ip_ref, ip_doc, "count"),
    ):
        if not doc.exists:
            continue
        current = doc.to_dict().get(field, 0)
        # Floor at 0: a refund must never be able to push a counter
        # negative and hand out free quota later in the day.
        transaction.set(ref, {field: max(0, current - 1), "updated_at": now}, merge=True)


def refund_scan_quota(email: str, ip: str) -> None:
    """Gives back the unit that check_and_reserve_scan_quota consumed.

    Called only when the scan the reservation paid for provably never
    started -- today that is exactly one case: the Cloud Tasks enqueue
    raised, so no worker will ever pick the job up (see app._start_scan).
    Without this, a queue misconfiguration billed the visitor a scan they
    never got, and their next attempt could be refused for a scan that
    never ran.

    Deliberately best-effort and non-raising: the caller is already on an
    error path showing the visitor a failure, and a failed refund must not
    turn that into a 500 on top. It is logged instead -- a refund that
    silently does nothing is a quota leak worth seeing in the logs.

    Same day/month key derivation as the reservation (`_quota_refs`), so
    this is only correct when called in the same UTC day as the reserve.
    That is true for its one caller, which refunds inline, milliseconds
    later; a delayed or batched refund would need the original keys
    carried forward rather than recomputed.
    """
    now = datetime.now(timezone.utc)
    month_ref, email_ref, ip_ref = _quota_refs(email, ip, now)
    try:
        _release_reservation(get_client().transaction(), month_ref, email_ref, ip_ref, now)
    except Exception:  # noqa: BLE001 - see docstring: never worsen an error path
        logger.exception("Failed to refund scan quota for %r / %r", quota_email_key(email), ip)


def check_and_reserve_feedback_quota(ip: str) -> tuple[bool, str]:
    """The feedback form's own, separate rate limit -- not the scan quota.

    Feedback is deliberately open (no review token, no proof you ever
    scanned anything: an earlier review pass closed the token-gated
    version's real bug, which was unbounded writes with a caller-controlled
    allow_testimonial flag, not the absence of a token as such). Openness
    still needs *some* ceiling on write volume, just a much more generous
    one than scanning: a Firestore write here costs nothing like a
    Playwright render plus a dozen Gemini calls, so this is a simple
    non-transactional per-IP daily counter, not the transactional
    reserve-before-spend machinery check_and_reserve_scan_quota needs to
    protect real spend. A race under concurrent submissions could let a
    couple of extra writes through; that is an acceptable trade against the
    complexity of a transaction for a resource this cheap.
    """
    now = datetime.now(timezone.utc)
    day_key = now.strftime("%Y-%m-%d")
    ref = _usage().document(f"feedback_ip_{ip}_{day_key}")
    doc = ref.get()
    count = doc.to_dict().get("count", 0) if doc.exists else 0
    if count >= max_feedback_per_ip_per_day():
        return False, "You've reached today's feedback limit. Please try again tomorrow."
    ref.set({"count": count + 1, "updated_at": now, "expires_at": now + timedelta(days=2)}, merge=True)
    return True, ""


def save_feedback(
    job_id: str | None,
    rating: int,
    comment: str = "",
    allow_testimonial: bool = False,
    contact: str | None = None,
    url: str | None = None,
) -> None:
    """The "how did it go" prompt, reachable from the report page, the
    report email, and the FAQ alike -- not scoped to one scan (job_id is
    optional: someone reading the FAQ has nothing to reference yet). When
    it is known, it's carried along for context, not as an authorization
    check. Deliberately separate from scan_jobs (this can be submitted well
    after a job document might reasonably change shape) and from a delayed
    outreach flow -- asking at the moment the report is delivered gets
    meaningfully better response rates than a cold follow-up days later.

    url is independent of job_id: the form field is editable (pre-filled
    from the job's own URL when one is known, but the visitor can change
    or clear it), so what's stored is what they actually said the
    feedback was about, not necessarily what a linked job says.
    """
    now = datetime.now(timezone.utc)
    _feedback().add(
        {
            "job_id": job_id,
            "url": url,
            "rating": rating,
            "comment": comment,
            "allow_testimonial": allow_testimonial,
            "contact": contact,
            "created_at": now,
            # TTL, same window as a scan record. `contact` is a free-text
            # value the submitter typed, so this is personal data with no
            # reason to outlive the window any other personal data here
            # gets, whether or not it references a particular scan.
            "expires_at": _scan_record_expiry(now),
        }
    )


def list_feedback(limit: int = 200) -> list[dict[str, Any]]:
    """Most recent first, for the internal review page -- there is no
    "pending" state to filter on here the way escalations have one, since
    nothing about feedback needs a disposition; it is read, not resolved.
    Capped rather than unbounded: this collection has no cursor/pagination
    UI yet, and a year of retention (SCAN_RECORD_RETENTION_DAYS) is enough
    time to accumulate more rows than one page should try to render at
    once.
    """
    docs = _feedback().order_by("created_at", direction=firestore.Query.DESCENDING).limit(limit).stream()
    return [{"id": doc.id, **doc.to_dict()} for doc in docs]


@firestore.transactional
def _merge_page_field(transaction, job_ref, page_url: str, fields: dict, now) -> None:
    snapshot = job_ref.get(transaction=transaction)
    current_stage = None
    if snapshot.exists:
        current_stage = (snapshot.to_dict() or {}).get("pages", {}).get(page_url, {}).get("stage")

    new_stage = fields.get("stage")
    if new_stage in PAGE_STAGES and current_stage in PAGE_STAGES:
        if PAGE_STAGES.index(current_stage) > PAGE_STAGES.index(new_stage):
            fields = {k: v for k, v in fields.items() if k != "stage"}
            if not fields:
                return

    transaction.set(job_ref, {"pages": {page_url: fields}, "updated_at": now}, merge=True)


def _set_page_field(job_id: str, page_url: str, fields: dict) -> None:
    """Merges the given fields into a page's record -- but never regresses
    its stage backward. Firestore's merge=True is field-path-recursive, not
    a whole-object replace, so writing {"stage": "crawled"} onto a page
    already at "verified" would otherwise silently downgrade it -- exactly
    the kind of bug that defeats resumability while looking correct at a
    glance, causing full reprocessing on every "resume" instead of none.

    That guard is now inside a transaction. It used to be a plain
    read-modify-write: get_page_stage() read the job, the decision was
    made, and only then did the merge go out -- so two workers on the same
    job (which Cloud Tasks can produce, see worker_app's lease) could both
    read "crawled", both decide their write was a step forward, and one
    could still land after the other's "verified". Reading the stage in the
    same transaction that writes it closes that window: a concurrent change
    to the job document aborts and retries with a fresh read.
    """
    _merge_page_field(
        get_client().transaction(),
        _jobs().document(job_id),
        page_url,
        fields,
        datetime.now(timezone.utc),
    )


@firestore.transactional
def _claim_lease(transaction, job_ref, owner: str, now, lease_seconds: int) -> bool:
    snapshot = job_ref.get(transaction=transaction)
    if not snapshot.exists:
        return False
    data = snapshot.to_dict() or {}
    held_by = data.get("lease_owner")
    held_until = data.get("lease_expires_at")
    if held_by and held_by != owner and held_until is not None and now < held_until:
        return False
    transaction.update(
        job_ref,
        {
            "lease_owner": owner,
            "lease_expires_at": now + timedelta(seconds=lease_seconds),
            "updated_at": now,
        },
    )
    return True


def claim_job_lease(job_id: str, owner: str, lease_seconds: int = JOB_LEASE_SECONDS) -> bool:
    """Tries to claim exclusive execution of this job. True if claimed.

    Cloud Tasks retries on dispatch-deadline expiry, and deadline expiry
    does not require the first attempt to have *stopped* -- so a scan that
    runs long gets a second worker while the first is still running. The
    existing "already completed?" check does not catch that: neither
    attempt has completed anything yet. Two live workers on one job
    duplicate every Gemini call (double spend), race each other's page
    checkpoints, both reach route_and_file, and both send the owner a
    report email.

    containerConcurrency=1 does not help here: it serializes requests
    within an instance, and these two are on different instances out of
    the max-instances pool.

    A transaction is what makes this a lock rather than a suggestion --
    read-then-write without one has exactly the race it is meant to
    prevent. The claim is re-entrant for the same owner so a worker that
    re-claims its own job is not locked out by itself.
    """
    return _claim_lease(
        get_client().transaction(), _jobs().document(job_id), owner, datetime.now(timezone.utc), lease_seconds
    )


@firestore.transactional
def _clear_lease(transaction, job_ref, owner: str) -> None:
    snapshot = job_ref.get(transaction=transaction)
    if not snapshot.exists:
        return
    if (snapshot.to_dict() or {}).get("lease_owner") != owner:
        return  # someone else's lease (ours already expired and was taken) -- leave it alone
    transaction.update(
        job_ref,
        {"lease_owner": firestore.DELETE_FIELD, "lease_expires_at": firestore.DELETE_FIELD},
    )


def release_job_lease(job_id: str, owner: str) -> None:
    """Drops this worker's claim so a retry can start immediately instead
    of waiting out JOB_LEASE_SECONDS.

    Only clears a lease this owner still holds -- if ours already expired
    and another worker took it, clearing it would hand that worker's job to
    a third. Best-effort and non-raising: this runs in a `finally`, and a
    failure here must not mask the real outcome of the scan. The lease
    expiring on its own is the fallback.
    """
    try:
        _clear_lease(get_client().transaction(), _jobs().document(job_id), owner)
    except Exception:  # noqa: BLE001 - see docstring
        logger.warning("[%s] Could not release job lease (it will expire on its own)", job_id, exc_info=True)


def code_request_cooldown_remaining(email: str) -> float:
    """Seconds to wait before another verification-code request for this
    email is allowed (0 if allowed now). See CODE_REQUEST_COOLDOWNS above --
    a quiet period longer than CODE_REQUEST_RESET_AFTER_SECONDS resets the
    schedule back to the start, so this only ever slows a burst, never
    locks someone out permanently.
    """
    key = email.strip().lower()
    doc = _email_codes().document(key).get()
    timestamps = doc.to_dict().get("request_log", []) if doc.exists else []
    if not timestamps:
        return 0
    now = datetime.now(timezone.utc)
    last = timestamps[-1]
    if (now - last).total_seconds() > CODE_REQUEST_RESET_AFTER_SECONDS:
        return 0
    idx = min(len(timestamps), len(CODE_REQUEST_COOLDOWNS) - 1)
    required_gap = CODE_REQUEST_COOLDOWNS[idx]
    return max(0.0, required_gap - (now - last).total_seconds())


def generate_email_code(email: str) -> str:
    """Creates a fresh 6-digit code for this email, replacing any previous
    one and resetting the wrong-attempt counter -- a new request always
    gets a clean slate. Also records this request in the cooldown log
    (trimmed to what CODE_REQUEST_COOLDOWNS can ever reference; older
    entries are dead weight once the schedule range is exceeded).

    expires_at doubles as this doc's Firestore TTL field (see
    DECISIONS_LOG.md) -- a verification code has no reason to outlive its
    own 10-minute relevance window, let alone the request log next to it.
    """
    key = email.strip().lower()
    now = datetime.now(timezone.utc)
    code = f"{secrets.randbelow(1_000_000):06d}"
    expires_at = now + timedelta(minutes=EMAIL_CODE_TTL_MINUTES)

    doc_ref = _email_codes().document(key)
    doc = doc_ref.get()
    timestamps = doc.to_dict().get("request_log", []) if doc.exists else []
    if timestamps and (now - timestamps[-1]).total_seconds() > CODE_REQUEST_RESET_AFTER_SECONDS:
        timestamps = []
    timestamps = (timestamps + [now])[-len(CODE_REQUEST_COOLDOWNS) :]

    doc_ref.set(
        {
            "code": code,
            "code_expires_at": expires_at,
            "attempts": 0,
            "request_log": timestamps,
            "expires_at": now + timedelta(hours=1),
        }
    )
    return code


@firestore.transactional
def _check_and_consume_code(transaction, doc_ref, code: str, now: datetime) -> bool:
    """The read-check-write body of verify_email_code, inside a real
    Firestore transaction.

    The transaction is load-bearing, exactly as it is in
    `_check_and_reserve` above, and for the same reason. This used to be a
    plain read-modify-write: read `attempts`, compare, write `attempts + 1`.
    Under concurrency that bounds *rounds*, not guesses -- N simultaneous
    POSTs all read `attempts == 0`, all get a guess, and all write
    `attempts == 1`, so MAX_CODE_ATTEMPTS against a 6-digit space could be
    defeated by batching. scan-onboarding runs --allow-unauthenticated at
    concurrency 20 across multiple instances, so generating that
    concurrency is trivial. Inside the transaction, the value read is the
    value incremented, and a conflicting commit retries with fresh reads.

    Reads must precede writes (Firestore's rule, and the shape this wants
    anyway). `now` is passed in rather than read here so a test can pin it.
    """
    snapshot = doc_ref.get(transaction=transaction)
    if not snapshot.exists:
        return False
    data = snapshot.to_dict()
    attempts = data.get("attempts", 0)
    expires_at = data.get("code_expires_at")
    valid = (
        attempts < MAX_CODE_ATTEMPTS
        and bool(code)
        and code == data.get("code")
        and expires_at is not None
        and now < expires_at
    )
    cleared = {
        "code": firestore.DELETE_FIELD,
        "code_expires_at": firestore.DELETE_FIELD,
    }
    if not valid:
        new_attempts = attempts + 1
        if new_attempts >= MAX_CODE_ATTEMPTS:
            transaction.update(doc_ref, {**cleared, "attempts": new_attempts})
        else:
            transaction.update(doc_ref, {"attempts": new_attempts})
        return False

    transaction.update(doc_ref, {**cleared, "attempts": 0})
    return True


def verify_email_code(email: str, code: str) -> bool:
    """Checks a submitted code against the pending one for this email.
    Wrong guesses increment the attempt counter; hitting MAX_CODE_ATTEMPTS
    clears the code entirely (forces a fresh request rather than leaving
    an exhausted-but-technically-still-correct code sitting there). A
    correct guess clears it too -- single-use, same as the reference this
    was adapted from.

    See `_check_and_consume_code` for why the counter has to be
    transactional, and `check_and_reserve_code_attempt_quota` for the
    per-address ceiling that sits in front of this on the route.
    """
    key = email.strip().lower()
    doc_ref = _email_codes().document(key)
    return _check_and_consume_code(
        get_client().transaction(), doc_ref, code, datetime.now(timezone.utc)
    )


@firestore.transactional
def _reserve_code_attempt(transaction, ref, now: datetime, limit: int) -> bool:
    doc = ref.get(transaction=transaction)
    count = doc.to_dict().get("count", 0) if doc.exists else 0
    if count >= limit:
        return False
    transaction.set(
        ref,
        {"count": count + 1, "updated_at": now, "expires_at": now + timedelta(days=2)},
        merge=True,
    )
    return True


def check_and_reserve_code_attempt_quota(ip: str) -> tuple[bool, str]:
    """Daily ceiling on verification-code guesses from one address.

    POST /scan/verify-code had no rate limit of any kind on it: no
    honeypot/timing check, no quota, nothing but the per-code attempt
    counter -- which a fresh code request resets to zero by design. So the
    effective bound on guessing was "however many requests you can send",
    and the email gate is what the privacy page, the FAQ and the scan
    form's own footnote all present as *the* anti-abuse control.

    Transactional, unlike `check_and_reserve_feedback_quota`, and
    deliberately so: the trade that makes a racy counter acceptable there
    (a couple of extra cheap writes) does not hold for a counter whose
    entire purpose is to resist a concurrent burst.
    """
    now = datetime.now(timezone.utc)
    ref = _usage().document(f"code_ip_{ip}_{now.strftime('%Y-%m-%d')}")
    allowed = _reserve_code_attempt(
        get_client().transaction(), ref, now, max_code_attempts_per_ip_per_day()
    )
    if allowed:
        return True, ""
    return False, "Too many verification attempts from this network today. Please try again tomorrow."


def set_verified_device(token_hash: str, email: str) -> None:
    """Remembers that this device (identified by the hash of an opaque
    cookie token -- the raw token itself never touches Firestore, same
    reasoning as never storing a password in plaintext) has verified this
    specific email. Deliberately keyed by token hash, not email -- a
    second device verifying the same email gets its own row instead of
    evicting the first (matches the reference: two browsers/devices for
    one person shouldn't fight over a single remembered slot).
    """
    expires_at = datetime.now(timezone.utc) + timedelta(days=REMEMBER_DEVICE_DAYS)
    _devices().document(token_hash).set({"email": email.strip().lower(), "expires_at": expires_at})


def get_verified_device_email(token_hash: str) -> str | None:
    """Returns the email this device last verified, or None if there's no
    record or it's expired. Expired-but-undeleted rows are harmless here
    (a Firestore TTL policy reaps them) -- this just also checks the
    timestamp directly so expiry takes effect immediately, not only once
    the TTL sweep gets to it.
    """
    doc = _devices().document(token_hash).get()
    if not doc.exists:
        return None
    data = doc.to_dict()
    expires_at = data.get("expires_at")
    if expires_at is None or datetime.now(timezone.utc) >= expires_at:
        return None
    return data.get("email")
