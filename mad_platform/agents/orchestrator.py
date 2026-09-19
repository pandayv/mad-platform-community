"""Orchestrator: picks pages, sequences the cycle, checkpoints progress.

Page selection is the dynamic, LLM-driven judgment point -- not exhaustive
crawling, not a fixed list, an actual decision grounded in what's on the
entry page. Everything after that is deterministic sequencing.

A page already checkpointed all the way to "verified" is skipped entirely
on resume, not redone. A page interrupted partway through is redone from
its crawl -- crawling is cheap and idempotent, so re-fetching costs far
less than persisting large intermediate finding blobs just to save one
re-crawl.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from urllib.parse import urljoin, urlparse

from bs4 import BeautifulSoup
from pydantic import BaseModel

from mad_platform import config
from mad_platform.agents.action_agent import LOW_CONFIDENCE_THRESHOLD, route_and_file
from mad_platform.agents.analyst import RawFinding, analyze_page
from mad_platform.agents.editor import VerifiedFinding, verify_findings
from mad_platform.agents.llm_validation import validate_indexed
from mad_platform.agents.reporter import (
    RankedFinding,
    compute_score,
    draft_email_summary,
    draft_report,
    rank_and_recommend,
    score_color,
)
from mad_platform.severity import count_by_severity
from mad_platform.state import firestore_client as fs
from mad_platform.state import storage_client
from mad_platform.tools import notify, untrusted
from mad_platform.tools.adk_client import generate_structured
from mad_platform.tools.crawler import PageSnapshot, fetch_page
from mad_platform.tools.gemini_client import FLASH, FLASH_LITE
from mad_platform.tools.issue_sink import IssueSink, MockIssueSink
from mad_platform.web import theme

logger = logging.getLogger("mad_platform.orchestrator")

MAX_ADDITIONAL_PAGES = 2

# Report/review links are built from config.app_base_url(), which has no
# fallback default on purpose: the original hackathon build defaulted this
# to its own Cloud Run URL, which meant a fork that forgot to set it would
# silently generate links pointing at the wrong (frozen) deployment instead
# of failing loudly. Read at call time rather than into a module constant,
# so importing this module needs no configured environment -- the error is
# identical, it just arrives when a link is actually built.


class _PageSelection(BaseModel):
    selected_paths: list[str]  # relative paths chosen from the candidate list
    reasoning: str


_PAGE_SELECTION_PROMPT = """{untrusted_preamble}
You are coordinating an accessibility scan of a
website. You've loaded the entry page and found these candidate links to
other pages on the same site. Pick up to {max_pages} of them that are most
likely to carry real accessibility and legal risk -- prioritize primary
navigation, contact forms, checkout/cart flows, and account/login pages
over marketing or blog content. Not exhaustive crawling -- a bounded,
justified subset.

Entry page: {entry_url}
Candidate links (path: link text):
{candidates}

Return the paths you selected (not full URLs) and a short reasoning.
"""


def _normalize_path(path: str) -> str:
    """Empty path (a bare "https://example.com" entry URL) and "/" are the
    same page -- treat them identically everywhere paths get compared.
    Without this, a nav link back to "/" on the entry page is treated as a
    distinct candidate from the entry URL itself, causing the same page to
    be crawled and analyzed twice under two URL forms, duplicating every
    finding on it in the final report.
    """
    return path or "/"


def _extract_candidate_links(snapshot: PageSnapshot, max_candidates: int = 30) -> dict[str, str]:
    soup = BeautifulSoup(snapshot.html, "html.parser")
    base = urlparse(snapshot.url)
    entry_path = _normalize_path(base.path)
    candidates: dict[str, str] = {}
    for a in soup.find_all("a", href=True):
        href = a["href"]
        absolute = urljoin(snapshot.url, href)
        parsed = urlparse(absolute)
        if parsed.netloc != base.netloc:
            continue  # same-domain only
        if parsed.scheme not in ("http", "https"):
            continue
        path = _normalize_path(parsed.path)
        if path == entry_path:
            continue  # just a link back to the entry page itself, not a new candidate
        text = a.get_text(strip=True)[:60]
        candidates.setdefault(path, text)
        if len(candidates) >= max_candidates:
            break
    return candidates


async def select_pages(entry_snapshot: PageSnapshot) -> list[str]:
    """Returns absolute URLs: the entry page plus up to MAX_ADDITIONAL_PAGES
    chosen by an LLM call over same-domain links found on it.
    """
    candidates = _extract_candidate_links(entry_snapshot)
    if not candidates:
        return [entry_snapshot.url]

    # Paths and link text both come off the scanned page, so they are
    # delimited like any other untrusted span. Output-side containment was
    # already here and stays: the loop below only accepts a path that is a
    # key of `candidates`, so the model cannot invent a URL to visit even
    # if the page talks it into trying.
    candidate_lines = "\n".join(f"{path}: {text or '(no link text)'}" for path, text in candidates.items())
    prompt = _PAGE_SELECTION_PROMPT.format(
        untrusted_preamble=untrusted.UNTRUSTED_PREAMBLE,
        max_pages=MAX_ADDITIONAL_PAGES,
        entry_url=entry_snapshot.url,
        candidates=untrusted.wrap(candidate_lines),
    )
    selection = await generate_structured(FLASH_LITE, prompt, _PageSelection)

    base = urlparse(entry_snapshot.url)
    selected_urls = [entry_snapshot.url]
    seen_paths = {_normalize_path(base.path)}
    for path in selection.selected_paths[:MAX_ADDITIONAL_PAGES]:
        if path in candidates and path not in seen_paths:
            selected_urls.append(f"{base.scheme}://{base.netloc}{path}")
            seen_paths.add(path)
    return selected_urls


class _RetryDecision(BaseModel):
    proceed: bool  # True = good enough, move on. False = use the one retry allowance.
    reasoning: str


_RETRY_GATE_PROMPT = """{untrusted_preamble}
You are the Orchestrator overseeing an accessibility
scan. Editor has just verified Analyst's findings for a page. Decide: is
this analysis good enough to proceed, or does the page warrant one more,
deeper look from Analyst?

Send it back for another pass only if there's a real reason to -- e.g.
confidence on confirmed findings is low across the board, or several
dismissals reflect genuine uncertainty rather than a confident correction,
suggesting the first pass didn't have enough to go on. Do NOT send it back
just because the page had few or zero findings -- a clean, well-built page
is a valid, complete result, not evidence of an insufficient pass.

This decision is capped at one retry maximum regardless of your answer --
you are deciding whether to use that single allowance, not opening a loop.

Editor's verification results for this page:
{summary}
"""


def _format_verification_summary(verified: list[VerifiedFinding]) -> str:
    """The rationale is Editor's prose about a stranger's page and quotes
    it verbatim by design, so it is delimited here for the same reason
    reporter._format_findings delimits it -- see tools/untrusted.py.
    """
    if not verified:
        return "(no findings at all -- Analyst flagged nothing on this page)"
    lines = []
    for v in verified:
        status = "CONFIRMED" if v.confirmed else "DISMISSED"
        lines.append(
            f"- [{status}] WCAG {v.wcag_criterion}, confidence {v.confidence:.2f}: "
            f"{untrusted.inline(v.rationale)}"
        )
    return "\n".join(lines)


async def evaluate_retry_gate(verified: list[VerifiedFinding]) -> _RetryDecision:
    prompt = _RETRY_GATE_PROMPT.format(
        untrusted_preamble=untrusted.UNTRUSTED_PREAMBLE,
        summary=_format_verification_summary(verified),
    )
    return await generate_structured(FLASH, prompt, _RetryDecision)


# WCAG 1.2.x findings (video captions, audio transcripts) are a narrow,
# deliberate exception to trusting Editor's own stated confidence -- not a
# general escape hatch, and it must not become one. Whether a muted,
# decorative background video needs captions is a genuine values judgment
# call, not a verifiable fact the way most of Editor's dismissals are
# ("this img has role=presentation" is a fact you can check; "does this
# specific silent video count as informational media" is a real judgment
# call). In practice, Editor's own stated confidence on this one category
# doesn't reliably track how gray the call actually is: the same real
# case (a muted background video on a real site) was dismissed outright
# twice in a row, even after the general "confirm gray areas at low
# confidence instead of dismissing" instruction was added to Editor's own
# prompt -- a prompt-level fix that works for other categories didn't
# move this one, because the model's own confidence in its reasoning was
# apparently already high. So this one narrow, high-stakes, and
# demonstrably-not-prompt-fixable category is forced into human review
# deterministically, in code, regardless of what Editor decided. This is
# NOT a statement that Editor can't be trusted to reason -- every other
# check keeps its full autonomy untouched; this is one specific, justified
# carve-out, not a pattern to casually extend.
_ALWAYS_REVIEW_CHECKS = {"video_captions", "ai_media"}
_ALWAYS_REVIEW_CRITERIA = ("1.2.1", "1.2.2")


def _force_media_to_review(
    raw_findings: list[RawFinding], verified: list[VerifiedFinding]
) -> list[VerifiedFinding]:
    """Only touches DISMISSED media findings -- the actual failure mode
    found tonight (a real finding silently vanishing). A media finding
    Editor already CONFIRMED, at any confidence, is left completely alone
    and flows through the exact same path every other check already uses
    (auto-filed if confident, escalated if low-confidence or critical via
    the existing general gate) -- that path was never broken, so this
    override doesn't touch it. Forcing already-correct confirmations
    through an extra review step too would just be unjustified friction
    with no failure it's actually fixing.
    """
    forced = []
    for v in verified:
        raw = raw_findings[v.finding_index]
        is_media = raw.check in _ALWAYS_REVIEW_CHECKS or v.wcag_criterion.strip().startswith(
            _ALWAYS_REVIEW_CRITERIA
        )
        if is_media and not v.confirmed:
            forced.append(
                v.model_copy(
                    update={
                        "confirmed": True,
                        "confidence": min(v.confidence, LOW_CONFIDENCE_THRESHOLD - 0.01),
                        "rationale": v.rationale
                        + " [Routed to human review automatically: audio/video accessibility "
                        "judgment calls always get a human close, regardless of Editor's own "
                        "confidence.]",
                    }
                )
            )
        else:
            forced.append(v)
    return forced


async def _run_analysis_pass(url: str) -> tuple[PageSnapshot, list[RawFinding], list[VerifiedFinding]]:
    snapshot = await fetch_page(url)
    raw_findings = await analyze_page(snapshot)
    verified = await verify_findings(snapshot, raw_findings)
    # _process_page is the one place raw_findings and verified get zipped
    # back together by index (_force_media_to_review, and the checkpoint
    # write). verify_findings already guarantees every finding_index is in
    # range; re-checking here -- on the single path that produces both
    # lists, so it covers the retry pass too -- costs nothing and means a
    # future second producer of VerifiedFinding can't quietly reintroduce
    # the IndexError / negative-index-wraps bug at those subscripts.
    verified = validate_indexed(verified, len(raw_findings), label=f"{url} verified findings")
    return snapshot, raw_findings, verified


async def _process_page(job_id: str, url: str) -> list[VerifiedFinding]:
    snapshot, raw_findings, verified = await _run_analysis_pass(url)
    fs.checkpoint_page_crawled(job_id, url)
    fs.checkpoint_page_analyzed(job_id, url, raw_finding_count=len(raw_findings))

    decision = await evaluate_retry_gate(verified)
    retried = False
    if not decision.proceed:
        fs.checkpoint_page_retry(job_id, url, reason=decision.reasoning)
        snapshot, raw_findings, verified = await _run_analysis_pass(url)  # one more pass, capped -- no loop
        retried = True

    # Applied after the retry gate has already made its call on Editor's
    # real, unmodified decisions -- this override shouldn't skew that
    # heuristic, it should only affect what gets persisted and shown.
    verified = _force_media_to_review(raw_findings, verified)

    fs.checkpoint_page_verified(
        job_id,
        url,
        verified_findings=[
            {**v.model_dump(), "raw": raw_findings[v.finding_index].__dict__} for v in verified
        ],
        retried=retried,
    )
    return verified


def _findings_from_stored(stored: list[dict]) -> list[VerifiedFinding]:
    """Firestore stores verified findings as plain dicts (plus a "raw"
    field checkpoint_page_verified adds) -- converts back to VerifiedFinding
    so resumed results are the same type as freshly-produced ones, since
    Reporter and Action Agent downstream expect that type strictly.
    """
    return [VerifiedFinding(**{k: v for k, v in d.items() if k != "raw"}) for d in stored]


@dataclass
class ScanResult:
    job_id: str
    findings_by_page: dict[str, list[VerifiedFinding]]
    report: str
    report_uri: str
    report_folder_url: str
    filed: list[tuple[int, RankedFinding, str]]
    escalated: list[tuple[int, RankedFinding, str]]
    already_filed: list[tuple[int, RankedFinding, str]]
    summary: dict  # exactly what was written to the job record by complete_job()


def build_scan_summary(
    filing: dict[str, list], report_uri: str, issue_sink: IssueSink | None
) -> dict:
    """The final scan outcome the status page and report download read back.

    This lives here, next to the code that produces the numbers, rather
    than in worker_app.py where it used to. That split was the cause of
    The bug this shape exists to prevent: the orchestrator marked the job
    completed, then handed a result
    object back to the worker, which computed this dict and wrote it
    afterwards -- so "completed" and "has a summary" were two writes from
    two services with several seconds of Slack/Gemini/Resend work in
    between. Building it here lets complete_job() write status and summary
    in a single update; see firestore_client.complete_job.

    csv_export: the CSV rows only exist in memory on this one sink
    instance during this one scan, so they are persisted here (Firestore,
    not a new storage_client path) for the download route to serve long
    after this worker instance is gone. getattr keeps it optional rather
    than coupling this to one concrete IssueSink implementation --
    MockIssueSink has nothing to export.
    """
    all_ranked = [f for _, f, _ in filing["filed"] + filing["escalated"] + filing["already_filed"]]
    # count_by_severity, not a local dict built with counts.get(sev, 0) + 1:
    # that pattern silently created a phantom key for any off-vocabulary
    # severity, so the donut (which iterates the four known tiers) and the
    # total_findings headline below it disagreed on the same screen.
    counts = count_by_severity([r.severity for r in all_ranked])
    score = compute_score(all_ranked)
    export_fn = getattr(issue_sink, "export", None)

    return {
        "score": score,
        "score_color": score_color(score),
        "severity_counts": counts,
        "principle_counts": theme.principle_counts([r.wcag_criterion for r in all_ranked]),
        "total_findings": len(all_ranked),
        "filed_count": len(filing["filed"]) + len(filing["already_filed"]),
        "escalated_count": len(filing["escalated"]),
        "report_uri": report_uri,
        "csv_export": export_fn() if export_fn else "",
    }


async def run_one_time_scan(
    url: str, job_id: str | None = None, issue_sink: IssueSink | None = None, owner_contact: str | None = None
) -> ScanResult:
    """The full core, end to end: site -> findings -> recommendations ->
    report -> escalation. Pass an existing job_id to resume it -- pages
    already fully verified are skipped, everything else is (re)run from
    its crawl. issue_sink defaults to a mock ticket sink so this runs
    without real ticketing credentials configured. owner_contact is the
    submitter's email (required by the community fork's /scan route) --
    only used on a fresh job; a resumed job already has its own.
    """
    issue_sink = issue_sink or MockIssueSink()
    existing_job = fs.get_job(job_id) if job_id else None
    logger.info("Scan started: %s (resume=%s)", url, bool(existing_job))

    try:
        if existing_job and existing_job.get("selected_pages"):
            # True resume: reuse the page list this job's selection step
            # actually decided on, rather than re-running page selection (a
            # fresh LLM call isn't guaranteed to pick the same pages twice,
            # and doesn't need to -- resuming means continuing the same job,
            # not re-deciding its scope).
            #
            # Keyed off `selected_pages`, NOT off `pages` being non-empty,
            # and that distinction is the whole point here. `pages` is the
            # per-page checkpoint map, and the entry URL is checkpointed
            # into it one line BEFORE select_pages runs. So a first attempt
            # that died inside the selection call -- a Gemini call, which is
            # exactly where it dies -- left a job whose `pages` had one key,
            # and the resume branch read that crash checkpoint as "this job
            # chose one page". The retry then skipped selection entirely and
            # completed "successfully" having scanned one page instead of
            # three, with nothing in the report, the status page or the logs
            # saying so -- while the landing page's comparison table claims
            # multi-page scanning. A checkpoint is not a decision.
            pages = list(existing_job["selected_pages"])
            logger.info("[%s] Resuming with %d previously selected page(s)", job_id, len(pages))
        else:
            if job_id is None:
                job_id = fs.create_job(url, owner_contact=owner_contact)
            logger.info("[%s] Phase: crawling_entry_page", job_id)
            fs.set_job_phase(job_id, "crawling_entry_page")
            entry_snapshot = await fetch_page(url)
            fs.checkpoint_page_crawled(job_id, url)
            logger.info("[%s] Phase: selecting_pages (Gemini call)", job_id)
            fs.set_job_phase(job_id, "selecting_pages")
            pages = await select_pages(entry_snapshot)
            # Persisted immediately, before any page is analyzed: this write
            # is what makes the resume branch above safe, so it must not
            # drift away from select_pages returning.
            fs.set_selected_pages(job_id, pages)
            logger.info("[%s] Selected %d page(s) to analyze", job_id, len(pages))

        logger.info("[%s] Phase: analyzing_pages", job_id)
        fs.set_job_phase(job_id, "analyzing_pages")
        results: dict[str, list[VerifiedFinding]] = {}
        for page_url in pages:
            if fs.get_page_stage(job_id, page_url) == "verified":
                job = fs.get_job(job_id)
                results[page_url] = _findings_from_stored(job["pages"][page_url]["findings"])
                continue
            verified = await _process_page(job_id, page_url)
            results[page_url] = verified
            confirmed_n = sum(1 for v in verified if v.confirmed)
            logger.info(
                "[%s] %s: %d finding(s) verified, %d confirmed",
                job_id, page_url, len(verified), confirmed_n,
            )

        logger.info("[%s] Phase: ranking_findings (Gemini call)", job_id)
        fs.set_job_phase(job_id, "ranking_findings")
        confirmed_by_page = {
            page_url: [f for f in findings if f.confirmed] for page_url, findings in results.items()
        }
        ranked = await rank_and_recommend(confirmed_by_page)

        logger.info("[%s] Phase: filing_tickets (%d ranked finding(s))", job_id, len(ranked))
        fs.set_job_phase(job_id, "filing_tickets")
        filing = route_and_file(issue_sink, ranked, job_id)
        logger.info(
            "[%s] Filed %d ticket(s), %d awaiting owner review",
            job_id, len(filing["filed"]) + len(filing["already_filed"]), len(filing["escalated"]),
        )
        for _index, finding, ticket_id in filing["filed"]:
            # Not "Jira": this sink is CsvIssueSink and the id is a "CSV-n"
            # row (DECISIONS_LOG.md records the CSV-only decision; this log
            # line was the one Jira reference the sweep missed).
            logger.info("[%s] Ticket filed: %s (WCAG %s)", job_id, ticket_id, finding.wcag_criterion)
        for _index, finding, escalation_id in filing["escalated"]:
            logger.info(
                "[%s] Awaiting owner review: %s (WCAG %s)", job_id, escalation_id, finding.wcag_criterion
            )

        # Build finding index -> ticket (or None if pending owner review),
        # so the report reflects what actually happened rather than a
        # stale "not filed yet" placeholder. Escalated findings also get
        # their escalation id, so the report can check on the outcome
        # later instead of freezing "pending" in place forever.
        ticket_by_finding: dict[int, str | None] = {}
        escalation_by_finding: dict[int, str] = {}
        for index, _finding, ticket_id in filing["filed"] + filing["already_filed"]:
            ticket_by_finding[index] = ticket_id
        for index, _finding, escalation_id in filing["escalated"]:
            ticket_by_finding[index] = None
            escalation_by_finding[index] = escalation_id

        job_record = fs.get_job(job_id) or {}
        review_token = job_record.get("review_token")

        logger.info("[%s] Phase: generating_report (Gemini call)", job_id)
        fs.set_job_phase(job_id, "generating_report")
        report, exec_summary, score, counts = await draft_report(
            url, ranked, ticket_by_finding, escalation_by_finding, job_id=job_id, review_token=review_token
        )
        report_uri = storage_client.save_report(job_id, report)
        logger.info("[%s] Report saved: %s", job_id, report_uri)

        # Status and summary go out together, in one write, and nothing
        # that can fail or block sits between building the summary and
        # writing it. Everything below this line (Slack, the email draft,
        # the Resend upload) is post-completion notification: the user's
        # status page is already fully renderable before any of it runs.
        # Do not move complete_job() back above this -- the gap is the bug.
        summary = build_scan_summary(filing, report_uri, issue_sink)
        fs.complete_job(job_id, summary)
        logger.info("[%s] Scan complete: %s", job_id, url)

        # Post-completion notification, and it does not re-raise.
        #
        # This block used to be bare, so any failure in it propagated. The
        # handler below correctly refused to downgrade the job -- the scan
        # genuinely succeeded -- and then re-raised anyway, which
        # worker_app.run_scan turns into a 500 and Cloud Tasks into a
        # retry. That retry then hits worker_app's own
        # `status == "completed" and summary` short-circuit and returns
        # {"already_completed": true} without attempting the email again.
        # So a transient failure here -- notify.summary, the Gemini call in
        # draft_email_summary (60s timeout, two attempts), the attachment
        # build -- permanently lost the report email while the queued-state
        # status page had already promised "we'll email your full report to
        # the address you submitted as soon as it's ready", and burned a
        # second Cloud Tasks attempt that could never do anything.
        #
        # A 500 also misrepresents the outcome: the report is saved, the
        # summary is written, the status page renders. If email delivery
        # should be genuinely retryable it needs to be its own Cloud Task,
        # not a tail on the scan task.
        try:
            app_base_url = config.app_base_url()
            notify.summary(
                f"Scan complete: {url}",
                [
                    f"{len(ranked)} confirmed finding(s) across {len(pages)} page(s)",
                    f"Filed automatically: {len(filing['filed']) + len(filing['already_filed'])}",
                    f"Awaiting owner review: {len(filing['escalated'])}",
                    f"Report: {app_base_url}/report/{job_id}",
                ],
            )

            recipient = job_record.get("owner_contact")
            if recipient:
                review_lines = [
                    f"WCAG {ranked[index].wcag_criterion} on {ranked[index].page_url}"
                    for index, _finding, _escalation_id in filing["escalated"]
                ]
                review_url = (
                    f"{app_base_url}/review/link/{job_id}/{review_token}" if review_lines and review_token else None
                )
                email_summary = draft_email_summary(
                    url,
                    ranked,
                    score,
                    counts,
                    exec_summary,
                    report_url=f"{app_base_url}/report/{job_id}",
                    csv_url=f"{app_base_url}/report/{job_id}/tickets.csv",
                    job_id=job_id,
                )
                # Reuses the CSV already exported into the summary above rather
                # than calling sink.export() a second time -- one export, one
                # set of rows, so the attachment and the download route can
                # never disagree.
                attachments = [("report.html", report.encode("utf-8"))]
                if summary["csv_export"]:
                    attachments.append(("tickets.csv", summary["csv_export"].encode("utf-8")))
                notify.send_report_email(
                    recipient, url, email_summary, review_lines=review_lines, review_url=review_url, attachments=attachments
                )
        except Exception:  # noqa: BLE001 - see the comment above: the scan succeeded
            logger.exception(
                "[%s] Scan completed and the report is saved, but post-completion "
                "notification failed -- the visitor may not have received their email",
                job_id,
            )
    except Exception:  # noqa: BLE001
        if job_id is not None:  # only unset if fs.create_job itself is what failed
            # Never downgrade an already-completed job. Everything after
            # complete_job() is post-completion notification (Slack, the
            # Gemini email draft, the Resend upload with its 10s timeout);
            # a failure there means the user did not get an email, not that
            # their scan failed -- the report is saved, the summary is
            # written, the status page renders. Marking it "failed" would
            # show "Scan failed: <a Resend timeout>" over a scan that
            # actually succeeded, and would overwrite the status the
            # completion write just published. Still re-raised, so Cloud
            # Tasks and the logs see the real failure.
            if (fs.get_job(job_id) or {}).get("status") == "completed":
                logger.exception("[%s] Scan completed, but post-completion notification failed", job_id)
            else:
                # An operator-facing message, not str(exc). The `error`
                # field is returned verbatim by GET /api/status/{job_id}
                # and rendered on the public, unauthenticated status page
                # by renderFailed. It is correctly HTML-escaped there, so
                # this is not XSS -- it is information disclosure: the
                # exception may be a MissingConfigError naming an env var,
                # a google.api_core error naming the GCP project and a
                # resource path, or a FetchError carrying the full internal
                # Playwright message. The enqueue path in app._start_scan
                # already does exactly this. The real text is not lost --
                # logger.exception below emits it with the traceback.
                logger.exception("[%s] Scan failed", job_id)
                fs.fail_job(
                    job_id,
                    "Something went wrong while scanning this site. This is on our "
                    "side, not yours -- please try again in a few minutes.",
                )
        raise

    return ScanResult(
        job_id=job_id,
        findings_by_page=results,
        report=report,
        report_uri=report_uri,
        report_folder_url=storage_client.console_folder_url(),
        filed=filing["filed"],
        escalated=filing["escalated"],
        already_filed=filing["already_filed"],
        summary=summary,
    )
