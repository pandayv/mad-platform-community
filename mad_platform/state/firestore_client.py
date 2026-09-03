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

import os
import secrets
import uuid
from datetime import datetime, timezone
from typing import Any

from google.cloud import firestore

_PROJECT = os.environ.get("GOOGLE_CLOUD_PROJECT", "project-d7e6174e-cca7-4d16-9d5")
_DATABASE = "scan-firestore"

_client = firestore.Client(project=_PROJECT, database=_DATABASE)
_JOBS = _client.collection("scan_jobs")
_TICKETS = _client.collection("filed_tickets")  # idempotency_key -> ticket_id
_ESCALATIONS = _client.collection("escalations")  # review queue (per-job "finding" + cross-cutting admin kinds)
_KB_VERSION = _client.collection("knowledge_base_version").document("wcag")
_LEARNED_PATTERNS = _client.collection("learned_patterns")  # SME-confirmed Analyst/Editor patterns
_USAGE = _client.collection("usage_counters")  # anti-abuse rate limits + the monthly scan budget
_FEEDBACK = _client.collection("feedback")  # immediate "was this helpful" responses, testimonial source

# Community-fork limits -- adjust here, not scattered through call sites.
MAX_SCANS_PER_EMAIL_PER_DAY = 3
MAX_SCANS_PER_IP_PER_DAY = 5
MAX_SCANS_PER_MONTH = 500  # a scan-count proxy for the $ budget, see DECISIONS_LOG.md

# Stages, in order -- used to answer "what's the next incomplete stage".
PAGE_STAGES = ["crawled", "analyzed", "verified"]


def create_job(url: str, trigger_type: str = "one-time", owner_contact: str | None = None) -> str:
    """owner_contact is the submitter's email -- required by the community
    fork's /scan route (not enforced here, so internal/admin callers like
    the WCAG poller can still omit it). It's the one field that answers
    "who does this job belong to", used for both the report email and the
    per-job review link below.
    """
    job_id = str(uuid.uuid4())
    now = datetime.now(timezone.utc)
    _JOBS.document(job_id).set(
        {
            "url": url,
            "trigger_type": trigger_type,
            "status": "in_progress",
            "pages": {},
            "owner_contact": owner_contact,
            "review_token": secrets.token_urlsafe(24),
            "created_at": now,
            "updated_at": now,
        }
    )
    return job_id


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
    doc = _JOBS.document(job_id).get()
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
    _JOBS.document(job_id).update({"phase": phase, "updated_at": datetime.now(timezone.utc)})


def complete_job(job_id: str) -> None:
    _JOBS.document(job_id).update({"status": "completed", "updated_at": datetime.now(timezone.utc)})


def save_scan_summary(job_id: str, summary: dict[str, Any]) -> None:
    """Persists the final scan outcome (score, severity counts, report
    location, ticket/escalation counts) onto the job record -- the web UI's
    status endpoint reads this back rather than needing the in-process
    ScanResult, since the request that started the scan and the request
    that polls for its result are two different HTTP calls.
    """
    _JOBS.document(job_id).update({"summary": summary, "updated_at": datetime.now(timezone.utc)})


def fail_job(job_id: str, error: str) -> None:
    _JOBS.document(job_id).update(
        {"status": "failed", "error": error, "updated_at": datetime.now(timezone.utc)}
    )


def get_ticket_for_finding(idempotency_key: str) -> str | None:
    """Checks whether a finding has already been filed -- the idempotency
    guard that keeps a retried pipeline step from double-filing.
    """
    doc = _TICKETS.document(idempotency_key).get()
    return doc.to_dict()["ticket_id"] if doc.exists else None


def record_ticket_for_finding(idempotency_key: str, ticket_id: str) -> None:
    _TICKETS.document(idempotency_key).set(
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
    """
    doc_ref = _ESCALATIONS.document(idempotency_key)
    doc_ref.set(
        {
            **finding_data,
            "job_id": job_id,
            "status": "pending",
            "created_at": datetime.now(timezone.utc),
        }
    )
    return idempotency_key


def list_escalations_for_job(job_id: str) -> list[dict[str, Any]]:
    """The job-scoped equivalent of list_pending_escalations() -- what the
    per-job review link shows, instead of every escalation in the system.
    """
    return [
        {"id": doc.id, **doc.to_dict()}
        for doc in _ESCALATIONS.where(filter=firestore.FieldFilter("job_id", "==", job_id)).stream()
    ]


def list_pending_escalations() -> list[dict[str, Any]]:
    return [{"id": doc.id, **doc.to_dict()} for doc in _ESCALATIONS.where(
        filter=firestore.FieldFilter("status", "==", "pending")
    ).stream()]


def get_escalation(escalation_id: str) -> dict[str, Any] | None:
    """Fetches one escalation regardless of status -- unlike
    list_pending_escalations(), this also returns already-resolved ones,
    for callers checking on an outcome rather than building a work queue.
    """
    doc = _ESCALATIONS.document(escalation_id).get()
    return {"id": doc.id, **doc.to_dict()} if doc.exists else None


def resolve_escalation(escalation_id: str, disposition: str, reviewer: str = "sme") -> dict[str, Any]:
    """disposition: 'confirm' or 'dismiss'. Returns the escalation's data
    so the caller (Action Agent) can file a ticket if confirmed.
    """
    doc_ref = _ESCALATIONS.document(escalation_id)
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
    for job_doc in _JOBS.stream():
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
    _LEARNED_PATTERNS.document(pattern_id).set(
        {**data, "confirmed_at": datetime.now(timezone.utc)}
    )


def list_learned_patterns() -> list[dict[str, Any]]:
    return [{"id": doc.id, **doc.to_dict()} for doc in _LEARNED_PATTERNS.stream()]


def get_kb_version() -> dict[str, Any] | None:
    doc = _KB_VERSION.get()
    return doc.to_dict() if doc.exists else None


def touch_kb_check(checked_version: str) -> None:
    """Records that a freshness check just ran, independent of whether the
    version actually changed -- so "last checked" is always accurate even
    on a no-op tick.
    """
    _KB_VERSION.set(
        {"last_checked_version": checked_version, "last_checked_at": datetime.now(timezone.utc)},
        merge=True,
    )


def set_kb_version(version: str) -> None:
    """Called after a successful refresh (auto or SME-confirmed) -- records
    which version the currently-stored embeddings actually reflect.
    """
    _KB_VERSION.set({"version": version, "updated_at": datetime.now(timezone.utc)}, merge=True)


def check_and_reserve_scan_quota(email: str, ip: str) -> tuple[bool, str]:
    """The anti-abuse + budget gate, called once per /scan submission,
    before create_job(). Three independent checks, all must pass:
    per-email daily cap, per-IP daily cap, and the global monthly scan
    budget. Each check both reads and, if it passes, atomically increments
    in the same call -- so this doubles as the reservation, not just a
    check, a caller that gets `(True, "")` back has already consumed one
    unit of quota, it should not increment again separately.

    Returns (allowed, reason). reason is empty when allowed=True, and a
    short human-readable string when False, meant to be shown directly to
    the visitor (e.g. "You've reached today's scan limit for this email").
    """
    now = datetime.now(timezone.utc)
    day_key = now.strftime("%Y-%m-%d")
    month_key = now.strftime("%Y-%m")

    month_ref = _USAGE.document(f"month_{month_key}")
    month_doc = month_ref.get()
    month_count = month_doc.to_dict().get("scans", 0) if month_doc.exists else 0
    if month_count >= MAX_SCANS_PER_MONTH:
        return False, "We've hit our free capacity for this month. Please check back next month."

    email_key = f"email_{email.strip().lower()}_{day_key}"
    email_ref = _USAGE.document(email_key)
    email_doc = email_ref.get()
    email_count = email_doc.to_dict().get("count", 0) if email_doc.exists else 0
    if email_count >= MAX_SCANS_PER_EMAIL_PER_DAY:
        return False, "You've reached today's scan limit for this email address. Please try again tomorrow."

    ip_key = f"ip_{ip}_{day_key}"
    ip_ref = _USAGE.document(ip_key)
    ip_doc = ip_ref.get()
    ip_count = ip_doc.to_dict().get("count", 0) if ip_doc.exists else 0
    if ip_count >= MAX_SCANS_PER_IP_PER_DAY:
        return False, "Too many scans from this network today. Please try again tomorrow."

    # All three checks passed -- reserve the quota now, atomically, so a
    # burst of concurrent requests can't all read "under the limit" and
    # all proceed before any of them increments.
    email_ref.set({"count": firestore.Increment(1), "updated_at": now}, merge=True)
    ip_ref.set({"count": firestore.Increment(1), "updated_at": now}, merge=True)
    month_ref.set({"scans": firestore.Increment(1), "updated_at": now}, merge=True)
    return True, ""


def save_feedback(job_id: str, rating: int, comment: str = "", allow_testimonial: bool = False, contact: str | None = None) -> None:
    """The immediate "was this helpful" prompt shown on the report page and
    in the report email. Deliberately separate from scan_jobs (this can be
    submitted well after a job document might reasonably change shape) and
    from a delayed outreach flow -- asking at the moment the report is
    delivered gets meaningfully better response rates than a cold follow-up
    days later.
    """
    _FEEDBACK.add(
        {
            "job_id": job_id,
            "rating": rating,
            "comment": comment,
            "allow_testimonial": allow_testimonial,
            "contact": contact,
            "created_at": datetime.now(timezone.utc),
        }
    )


def _set_page_field(job_id: str, page_url: str, fields: dict) -> None:
    """Merges the given fields into a page's record -- but never regresses
    its stage backward. Firestore's merge=True is field-path-recursive, not
    a whole-object replace, so writing {"stage": "crawled"} onto a page
    already at "verified" would otherwise silently downgrade it -- exactly
    the kind of bug that defeats resumability while looking correct at a
    glance, causing full reprocessing on every "resume" instead of none.
    """
    new_stage = fields.get("stage")
    if new_stage in PAGE_STAGES:
        current_stage = get_page_stage(job_id, page_url)
        if current_stage in PAGE_STAGES and PAGE_STAGES.index(current_stage) > PAGE_STAGES.index(new_stage):
            fields = {k: v for k, v in fields.items() if k != "stage"}
            if not fields:
                return

    doc_ref = _JOBS.document(job_id)
    doc_ref.set({"pages": {page_url: fields}, "updated_at": datetime.now(timezone.utc)}, merge=True)
