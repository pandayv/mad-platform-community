"""Action Agent: files tickets, idempotently, and routes escalations.

- Escalation is a single gate applied once: low Editor confidence OR high
  severity (either alone is sufficient) sends a finding to the SME queue.
- Non-escalated findings are fully autonomous -- ticket filed immediately,
  no human step. Escalated findings wait: no ticket exists until SME
  confirms it ("dismiss" never becomes a ticket, never appears in the
  report) -- this is not act-then-flag for the escalated subset, only for
  the majority that clears the gate.
- Idempotency: a deterministic key per finding *per scan* means a retried
  call never double-files. Scoping it to the scan is deliberate -- see
  idempotency_key's docstring for what cross-scan sharing did to two
  users who happened to scan the same site.

Tool, not an agent -- the judgment already happened upstream (Editor's
confidence, Reporter's severity), this is deterministic routing and API
calls, no LLM judgment of its own.
"""

from __future__ import annotations

import hashlib

from mad_platform.agents.reporter import RankedFinding
from mad_platform.severity import ESCALATE_ALWAYS, LOW_CONFIDENCE_THRESHOLD, normalize
from mad_platform.state import firestore_client as fs
from mad_platform.tools.issue_sink import IssueSink

# Re-exported from mad_platform.severity, where it now lives alongside
# ESCALATE_ALWAYS -- the two halves of this module's one escalation gate.
# It moved because editor.py needs it and cannot import this module
# (action_agent -> reporter -> editor is already a chain). Kept importable
# from here because this is where the gate is, and because every existing
# caller and test refers to it by this name.
__all__ = ["LOW_CONFIDENCE_THRESHOLD", "idempotency_key", "needs_escalation", "route_and_file", "resolve_escalation"]


def idempotency_key(job_id: str, page_url: str, finding: RankedFinding) -> str:
    """A key that identifies ONE finding within ONE scan.

    It is used as the Firestore document ID for both the filed-ticket
    record and the escalation, so anything it fails to distinguish gets
    silently merged into one record. Two things were missing:

    - **job_id.** Without it, two different users scanning the same public
      site produced identical keys. User B's finding was then classified
      `already_filed` against user A's ticket (so it never reached B's
      CSV), and B's escalation overwrote A's document wholesale --
      resetting status to "pending", discarding A's disposition, reviewer
      and resolved_at, and reassigning job_id, which permanently 404'd A's
      scoped review link. It also broke the ordinary re-scan case: a site
      owner who fixed issues and re-scanned got `already_filed` for
      everything with no new tickets. Real multi-tenant data corruption on
      a live service, from one absent field.

    - **The whole rationale.** Truncating to 100 characters collided two
      genuinely distinct findings on the same page under the same
      criterion whenever Editor's rationales opened the same way -- which
      is common, e.g. three unlabeled inputs on one form. The second one
      silently never got a ticket or a CSV row.

    Both are the same root cause: the key didn't uniquely identify the
    thing it keys. Keep every component; hashing is cheap and a field
    dropped from here becomes a collision, not an error.
    """
    raw = f"{job_id}|{page_url}|{finding.wcag_criterion}|{finding.editor_rationale}"
    return hashlib.sha256(raw.encode()).hexdigest()[:16]


def needs_escalation(finding: RankedFinding) -> bool:
    """The single escalation gate: low Editor confidence OR top severity.

    The severity half compares through severity.normalize() rather than
    against a bare literal. It used to be `finding.severity == "critical"`,
    which is case-sensitive against a field the model filled in freely -- a
    returned "Critical" sailed past the one gate in this system that exists
    to keep a critical finding from being auto-filed without a human
    looking at it. RankedFinding.severity is normalized at construction
    now, so this is belt-and-braces for anything built another way (an
    escalation document read back out of Firestore, a test, a future
    caller) rather than the only defense.
    """
    return (
        finding.editor_confidence < LOW_CONFIDENCE_THRESHOLD
        or normalize(finding.severity, default="low") == ESCALATE_ALWAYS
    )


def _ticket_title(finding: RankedFinding) -> str:
    return f"[{finding.severity.upper()}] WCAG {finding.wcag_criterion} — {finding.page_url}"


def _ticket_description(finding: RankedFinding) -> str:
    return (
        f"WCAG citation: {finding.wcag_criterion}\n"
        f"Severity: {finding.severity} (risk score {finding.risk_score:.0f}/100)\n"
        f"Page: {finding.page_url}\n\n"
        f"Evidence: {finding.editor_rationale}\n\n"
        f"Why it matters: {finding.risk_rationale}\n\n"
        f"Suggested fix: {finding.suggested_fix}"
    )


def route_and_file(sink: IssueSink, ranked: list[RankedFinding], job_id: str) -> dict[str, list]:
    """The single escalation gate + idempotent filing for the autonomous
    majority. Returns {"filed": [(index, finding, ticket_id)], "escalated":
    [(index, finding, escalation_id)], "already_filed": [(index, finding, ticket_id)]}
    -- index is the finding's position in `ranked`, included explicitly
    rather than left for callers to re-derive via value lookup (fragile if
    two findings ever have identical field values, e.g. near-duplicate
    findings from the same page).

    job_id scopes each escalated finding to the scan that produced it --
    see firestore_client.create_escalation's docstring -- and is also part
    of the idempotency key, so "already_filed" now means "this same scan
    already filed it" (a retry), never "somebody else's scan of this site
    filed something that looked similar". No Slack alert for
    these anymore: the scan's own owner sees a pending finding immediately
    on their report and in their email (a real, working link to their own
    scoped review page, not a page to an admin who isn't the one meant to
    resolve it).
    """
    result: dict[str, list] = {"filed": [], "escalated": [], "already_filed": []}

    for index, finding in enumerate(ranked):
        key = idempotency_key(job_id, finding.page_url, finding)
        existing_ticket = fs.get_ticket_for_finding(key)
        if existing_ticket:
            result["already_filed"].append((index, finding, existing_ticket))
            continue

        if needs_escalation(finding):
            fs.create_escalation(
                key,
                {
                    "kind": "finding",
                    "page_url": finding.page_url,
                    "wcag_criterion": finding.wcag_criterion,
                    "severity": finding.severity,
                    "risk_score": finding.risk_score,
                    "editor_rationale": finding.editor_rationale,
                    "editor_confidence": finding.editor_confidence,
                    "risk_rationale": finding.risk_rationale,
                    "suggested_fix": finding.suggested_fix,
                },
                job_id=job_id,
            )
            result["escalated"].append((index, finding, key))
            continue

        ticket_id = sink.create_issue(_ticket_title(finding), _ticket_description(finding))
        fs.record_ticket_for_finding(key, ticket_id)
        result["filed"].append((index, finding, ticket_id))

    return result


def resolve_escalation(sink: IssueSink, escalation_id: str, disposition: str, reviewer: str = "sme") -> str | None:
    """SME disposition on a pending escalation. confirm -> files the ticket
    now (idempotency-checked the same as the autonomous path); dismiss ->
    no ticket, ever, for this finding. Returns the ticket ID if one was
    filed, else None.
    """
    if disposition not in ("confirm", "dismiss"):
        raise ValueError(f"disposition must be 'confirm' or 'dismiss', got {disposition!r}")

    data = fs.resolve_escalation(escalation_id, disposition=disposition, reviewer=reviewer)

    if disposition == "dismiss":
        return None

    existing_ticket = fs.get_ticket_for_finding(escalation_id)
    if existing_ticket:
        return existing_ticket

    finding = _finding_from_escalation(data)
    description = f"{_ticket_description(finding)}\n\n[Confirmed by SME review: {reviewer}]"
    ticket_id = sink.create_issue(_ticket_title(finding), description)
    fs.record_ticket_for_finding(escalation_id, ticket_id)
    return ticket_id


def _finding_from_escalation(data: dict) -> RankedFinding:
    """Rebuilds the RankedFinding an escalation document was created from,
    so the SME-confirmed path can call `_ticket_title`/`_ticket_description`
    instead of re-implementing them.

    It re-implemented them inline, right here, and this is the drift shape
    this codebase has already been burned by (severity.py's docstring
    records five copies of four words). A field added to the ticket body
    would have been added to one of the two, and the SME-confirmed path
    would have quietly produced a different ticket format than the
    autonomous one -- for the same finding.

    `.get()` with defaults rather than `data['severity']` etc., which is
    what the inline copy did: an escalation document written by an older
    revision, or one missing a field for any other reason, raised KeyError
    -> 500 on an authenticated admin route instead of filing a ticket that
    says what is missing. `normalize` with an explicit default for the same
    reason -- a value read back out of Firestore has not been through the
    response schema that constrains it on the way in.
    """
    return RankedFinding(
        page_url=str(data.get("page_url", "(unknown page)")),
        wcag_criterion=str(data.get("wcag_criterion", "(unknown criterion)")),
        editor_rationale=str(data.get("editor_rationale", "")),
        editor_confidence=float(data.get("editor_confidence", 0.0)),
        risk_score=float(data.get("risk_score", 0.0)),
        severity=normalize(data.get("severity", ""), default="medium"),
        suggested_fix=str(data.get("suggested_fix", "")),
        risk_rationale=str(data.get("risk_rationale", "")),
    )
