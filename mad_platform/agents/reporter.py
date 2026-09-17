"""Reporter: ranks confirmed findings by real-world risk, recommends fixes,
and drafts the report.

rank_and_recommend ranks by WCAG conformance level, real-world litigation
pattern frequency, and estimated user impact -- not raw technical severity
alone. The LLM assigns a risk score per finding; sorting by that score is
deterministic Python, not another judgment call -- code handles the
mechanical part, the model handles the actual judgment.

draft_report renders one fixed template, not a freshly generated structure
per run. The only genuinely LLM-appropriate part of the report itself is
the short executive summary; everything else is templated data fill.

Uses the higher-capability model tier -- ranking/synthesis is a judgment
call worth spending that on, unlike the high-volume per-page checks.
"""

from __future__ import annotations

import html as html_lib
import math
import os
from dataclasses import dataclass
from datetime import datetime, timezone

from pydantic import BaseModel

from mad_platform.agents.editor import VerifiedFinding
from mad_platform.tools.adk_client import generate_structured
from mad_platform.tools.gemini_client import FLASH, FLASH_LITE
from mad_platform.web import theme

# The report can be opened outside the app's own origin (downloaded, saved
# locally, reopened later) so the live-status check below needs an
# absolute URL, not a relative fetch that only works when served from
# the app itself. No fallback default -- see orchestrator.py's own read
# of this variable for why a stale hardcoded URL is worse than failing loudly.
_APP_BASE_URL = os.environ["MAD_APP_BASE_URL"]


@dataclass
class RankedFinding:
    page_url: str
    wcag_criterion: str
    editor_rationale: str
    editor_confidence: float
    risk_score: float  # 0-100, Reporter's judgment
    severity: str  # "critical" | "high" | "medium" | "low"
    suggested_fix: str
    risk_rationale: str


_SCORE_WEIGHT = {"critical": 20.0, "high": 12.0, "medium": 6.0, "low": 2.0}


def compute_score(ranked: list[RankedFinding]) -> int:
    """A single 0-100 "site health" number for the UI's headline display --
    not a WCAG-official metric, just 100 minus a severity-weighted penalty,
    clamped to [0, 100]. Deterministic Python over the LLM's already-
    assigned severities, not another judgment call.

    Penalty scales with the square root of how many findings are in each
    severity tier, not linearly with the count. The old version (a flat
    per-finding subtraction) meant 4 critical findings alone -- a real but
    fixable handful of issues -- zeroed the score outright, and nobody
    shown a 0 believes it reflects a "fix these and you're in good shape"
    site. Square-root weighting gives the first finding in a tier close to
    its full weight but flattens fast after that: 4 criticals cost about
    2x one critical (sqrt(4) = 2), not 4x. This is what makes "you're at
    65, fix your 2 critical issues and you're at 90" an honest, calculable
    claim instead of a number that can only ever read as "broken" or
    "perfect." A genuinely riddled site (dozens of findings across tiers)
    still drives the score to 0 -- sqrt keeps growing, just slower.
    """
    counts: dict[str, int] = {}
    for finding in ranked:
        sev = finding.severity.lower()
        counts[sev] = counts.get(sev, 0) + 1

    penalty = sum(_SCORE_WEIGHT.get(sev, 4.0) * math.sqrt(n) for sev, n in counts.items())
    return max(0, min(100, round(100 - penalty)))


def score_color(score: int) -> str:
    """Shared between the report template and the web UI so the same score
    always reads as the same color in both places."""
    if score >= 80:
        return "#15803D"  # green
    if score >= 50:
        return "#A16207"  # amber
    return "#B91C1C"  # red


class _Recommendation(BaseModel):
    finding_index: int
    risk_score: float
    severity: str
    suggested_fix: str
    risk_rationale: str


class _RecommendationResponse(BaseModel):
    recommendations: list[_Recommendation]


_REPORTER_PROMPT = """You are the Reporter for an accessibility scan. Editor
has confirmed the findings below as real violations. For each one, assess
its real-world risk -- not just technical severity -- and recommend a
concrete fix.

Weigh three things when scoring risk (0-100): the WCAG conformance level
implied by the criterion (Level A violations are generally higher-risk
than AAA), how often this type of issue shows up in real accessibility
litigation (missing alt text, unlabeled form fields, and low contrast on
key interactions are common targets; obscure AAA-only issues rarely are),
and estimated impact on actual users trying to complete a task (a broken
checkout form field is worse than a decorative image on a footer link).

Assign severity as one of: critical, high, medium, low.

Write risk_rationale in plain language for a small-business owner, not a
developer -- describe who's affected and what breaks for them (e.g. "a
blind visitor using a screen reader can't tell what this button does
before clicking it"), not WCAG terminology or technical jargon. One
sentence. suggested_fix stays technical (the actual markup/attribute
change) -- that split is deliberate, this field is the "why it matters"
a non-technical reader needs, the fix is for whoever implements it.

Give a concrete suggested fix for each -- not "fix the alt text" but the
actual text/attribute/markup change that would resolve it, inferred from
the finding's description.

Confirmed findings (index: page, WCAG citation, Editor's rationale, confidence):
{findings_list}
"""


def _format_findings(findings: list[tuple[str, VerifiedFinding]]) -> str:
    lines = []
    for i, (page_url, f) in enumerate(findings):
        lines.append(
            f"{i}: [{page_url}] WCAG {f.wcag_criterion} (confidence {f.confidence:.2f}) -- {f.rationale}"
        )
    return "\n".join(lines)


async def rank_and_recommend(confirmed_by_page: dict[str, list[VerifiedFinding]]) -> list[RankedFinding]:
    """confirmed_by_page: page URL -> its CONFIRMED VerifiedFinding list
    (dismissed findings don't need a recommendation, so filter before calling).
    Returns findings sorted by risk_score, highest first.
    """
    flat: list[tuple[str, VerifiedFinding]] = [
        (page_url, f) for page_url, findings in confirmed_by_page.items() for f in findings
    ]
    if not flat:
        return []

    prompt = _REPORTER_PROMPT.format(findings_list=_format_findings(flat))
    result = await generate_structured(FLASH, prompt, _RecommendationResponse)

    ranked = [
        RankedFinding(
            page_url=flat[rec.finding_index][0],
            wcag_criterion=flat[rec.finding_index][1].wcag_criterion,
            editor_rationale=flat[rec.finding_index][1].rationale,
            editor_confidence=flat[rec.finding_index][1].confidence,
            risk_score=rec.risk_score,
            severity=rec.severity,
            suggested_fix=rec.suggested_fix,
            risk_rationale=rec.risk_rationale,
        )
        for rec in result.recommendations
    ]
    ranked.sort(key=lambda r: r.risk_score, reverse=True)
    return ranked


# ---------------------------------------------------------------------------
# Step 3: the report artifact itself -- one fixed template, per section 5.5.
# ---------------------------------------------------------------------------

class _ExecutiveSummary(BaseModel):
    summary: str  # 2-3 plain-English sentences, for a non-technical reader


_EXEC_SUMMARY_PROMPT = """Write a 2-3 sentence executive summary of this
accessibility scan for a non-technical small business owner. Plain
English, no jargon, no WCAG citation numbers. Mention the overall risk
level and the single most important thing to act on first.

Site scanned: {url}
Findings, highest risk first (severity, WCAG topic, one-line description):
{summary_lines}
"""


async def generate_executive_summary(url: str, ranked: list[RankedFinding]) -> str:
    if not ranked:
        return (
            "This scan didn't find any confirmed accessibility violations on the "
            "pages checked. That's a good sign, not a guarantee -- only a subset "
            "of WCAG criteria and pages were covered."
        )
    lines = "\n".join(f"- [{r.severity.upper()}] {r.wcag_criterion}: {r.editor_rationale[:100]}" for r in ranked)
    prompt = _EXEC_SUMMARY_PROMPT.format(url=url, summary_lines=lines)
    result = await generate_structured(FLASH_LITE, prompt, _ExecutiveSummary)
    return result.summary


def _esc(text: str) -> str:
    # Findings text comes from an LLM and has, in practice, contained literal
    # HTML snippets (e.g. a suggested fix quoting `<img alt="...">`) -- escape
    # everything interpolated into the template or it renders as markup
    # instead of visible text, or worse, breaks the page structure.
    return html_lib.escape(str(text))


def _status_badge(ticket: str | None, escalation_id: str | None, review_url: str | None = None) -> str:
    if ticket:
        return f'<span class="badge sev-ok">Filed: {_esc(ticket)}</span>'
    if escalation_id:
        # Not resolved yet as far as the report knows at generation time --
        # the small script at the end of this page checks the live status
        # on load and updates this badge in place, so a report reopened
        # later reflects what actually happened instead of freezing here.
        if review_url:
            return (
                f'<a class="badge sev-pending escalation-badge" style="text-decoration:none" '
                f'data-escalation-id="{_esc(escalation_id)}" href="{_esc(review_url)}">Review this &rarr;</a>'
            )
        return (
            f'<span class="badge sev-pending escalation-badge" '
            f'data-escalation-id="{_esc(escalation_id)}">Awaiting review</span>'
        )
    return '<span class="badge sev-pending">Awaiting review</span>'


def _finding_row(
    index: int,
    r: RankedFinding,
    ticket: str | None,
    escalation_id: str | None = None,
    review_url: str | None = None,
) -> str:
    sev = r.severity.lower()
    rail_color = theme.SEVERITY_VAR.get(sev, "var(--muted)")
    return f"""<tr>
  <td class="rail"><span style="background:{rail_color}"></span></td>
  <td class="num mono">{index + 1}</td>
  <td>
    <div class="finding-title">WCAG {_esc(r.wcag_criterion)} <span class="badge sev-{sev}" style="margin-left:6px">{_esc(r.severity)}</span></div>
    <div class="finding-detail">{_esc(r.risk_rationale)}</div>
  </td>
  <td>{_esc(r.page_url)}</td>
  <td class="num">{_esc(r.severity.capitalize())} <span class="mono" style="color:var(--muted);font-size:11px">{r.risk_score:.0f}/100</span></td>
  <td class="fix-cell">{_esc(r.suggested_fix)}</td>
  <td>{_status_badge(ticket, escalation_id, review_url)}</td>
</tr>"""


_HTML_TEMPLATE = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Accessibility Report | {title_url}</title>
{font_link}
<style>{theme_css}
.page {{ max-width: 900px; padding: 40px 24px 0; }}
header {{ display: flex; justify-content: space-between; align-items: flex-start; gap: 20px; margin-bottom: 24px; }}
header .meta {{ color: var(--muted); font-size: 13.5px; margin-top: 4px; }}
</style>
</head>
<body>
<div class="page">
  <header>
    <div>
      <a class="brand" href="{app_base_url}" style="text-decoration:none">{brand_mark}MAD Platform · Accessibility Report</a>
      <h1>{title_url}</h1>
      <div class="meta">Generated {generated_at} &middot; <a href="{app_base_url}">Scan another site</a></div>
    </div>
    {score_dial}
  </header>

  {dashboard_row}

  <div class="summary-box">
    <div class="lbl">Executive summary</div>
    <p>{exec_summary}</p>
  </div>

  {findings_section}

</div>
<footer class="note">
  MAD Platform is autonomous, AI-assisted WCAG accessibility scanning with independent
  verification before anything is reported. Findings are sorted by real-world risk,
  not raw technical severity alone.
  <p style="margin-top:10px">This scan was free, no account needed. If it saved you the
  cost of a manual audit, you can <a href="https://buymeacoffee.com/madplatform"
  target="_blank" rel="noopener">buy the project a coffee</a>.</p>
</footer>
<script>
// Findings under internal review show "Awaiting internal review" as of
// when this report was generated. If this page is reopened later, this
// checks whether each one has since been resolved and updates the badge
// in place -- so a stored report doesn't freeze in a stale state forever.
(function () {{
  document.querySelectorAll(".escalation-badge").forEach(function (el) {{
    var id = el.getAttribute("data-escalation-id");
    fetch("{app_base_url}/api/escalation/" + encodeURIComponent(id) + "/status")
      .then(function (r) {{ return r.ok ? r.json() : null; }})
      .then(function (data) {{
        if (!data || !data.resolved) return;
        el.classList.remove("escalation-badge", "sev-pending");
        if (data.ticket_id) {{
          el.textContent = "Filed: " + data.ticket_id;
          el.classList.add("sev-ok");
        }} else {{
          el.textContent = "Reviewed: dismissed";
        }}
      }})
      .catch(function () {{}});
  }});
}})();
</script>
</body>
</html>
"""


async def draft_report(
    url: str,
    ranked: list[RankedFinding],
    ticket_by_finding: dict[int, str | None] | None = None,
    escalation_by_finding: dict[int, str] | None = None,
    job_id: str | None = None,
    review_token: str | None = None,
) -> tuple[str, str, int, dict[str, int]]:
    """The fixed report template -- same structure every run, only the data
    changes. Single format (HTML): easiest to generate reliably, opens
    anywhere, and is the one genuinely user-friendly format a business
    owner would actually read. The template itself is fixed; only the
    executive summary is LLM-generated.

    job_id + review_token, when both given, turn each pending finding's
    badge into a real link to that scan's own scoped review page (see
    firestore_client.verify_review_token) -- the scan's owner reviews
    their own uncertain findings immediately, not an admin on their
    behalf. Either being None (e.g. no owner_contact on the job) falls
    back to a plain non-clickable badge.

    Returns (html, exec_summary, score, counts) rather than just html --
    the caller (orchestrator.py) needs exec_summary/score/counts again to
    build the separate email summary (draft_email_summary, below), and
    exec_summary specifically is an LLM call: returning it here instead of
    having the caller regenerate it avoids paying for that twice.
    """
    ticket_by_finding = ticket_by_finding or {}
    escalation_by_finding = escalation_by_finding or {}
    generated_at = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    exec_summary = await generate_executive_summary(url, ranked)
    score = compute_score(ranked)

    counts = {"critical": 0, "high": 0, "medium": 0, "low": 0}
    for r in ranked:
        counts[r.severity.lower()] = counts.get(r.severity.lower(), 0) + 1
    p_counts = theme.principle_counts([r.wcag_criterion for r in ranked])

    def _review_url(index: int) -> str | None:
        escalation_id = escalation_by_finding.get(index)
        if not (escalation_id and job_id and review_token):
            return None
        return f"{_APP_BASE_URL}/review/link/{job_id}/{review_token}/{escalation_id}"

    if not ranked:
        findings_section = '<div class="empty">No confirmed findings on the pages checked.</div>'
    else:
        rows = "".join(
            _finding_row(i, r, ticket_by_finding.get(i), escalation_by_finding.get(i), _review_url(i))
            for i, r in enumerate(ranked)
        )
        findings_section = f"""<table class="findings-table">
  <thead><tr><th></th><th class="num">#</th><th>Finding</th><th>Page</th><th class="num">Risk</th><th>Suggested fix</th><th>Status</th></tr></thead>
  <tbody>{rows}</tbody>
</table>"""

    html = _HTML_TEMPLATE.format(
        title_url=_esc(url),
        generated_at=_esc(generated_at),
        exec_summary=_esc(exec_summary),
        font_link=theme.FONT_LINK,
        theme_css=theme.THEME_CSS,
        score_dial=theme.score_dial(score, score_color(score)),
        dashboard_row=theme.dashboard_row(score, score_color(score), counts, p_counts),
        findings_section=findings_section,
        app_base_url=_APP_BASE_URL,
        brand_mark=theme.BRAND_MARK,
    )
    return html, exec_summary, score, counts


# Literal hex, not CSS custom properties -- email clients (Gmail especially)
# strip <style> blocks from HTML pasted into a message body, so a value like
# var(--crit) would resolve to nothing. These mirror theme.py's light-mode
# palette (the only one that makes sense for email -- no reliable dark-mode
# media query support across clients).
_EMAIL_SEVERITY_COLOR = {
    "critical": "#C0152B",
    "high": "#C2570A",
    "medium": "#A67C00",
    "low": "#47566B",
}


def draft_email_summary(
    url: str,
    ranked: list[RankedFinding],
    score: int,
    counts: dict[str, int],
    exec_summary: str,
    report_url: str,
    csv_url: str,
) -> str:
    """A separate, deliberately much simpler rendering for the email body --
    not draft_report()'s template reused. That template is a full standalone
    document (its own <html>/<head>/<style>), and nesting one HTML document
    inside another (the email's own body) is invalid; Gmail and most other
    clients respond by stripping the inner <style>/<head> entirely, which is
    exactly the unstyled wall of text this replaces. Table-based layout with
    inline style="" attributes throughout is what actually survives across
    email clients -- no <style> block, no flexbox/grid.

    Deliberately not the full findings table (dense code-snippet fix cells
    don't work at email width/without real styling) -- just the score, the
    severity breakdown, and the top few findings by risk, with a prominent
    link to the real, fully-styled report for anyone who wants the rest.
    """
    color = score_color(score)
    sev_cells = "".join(
        f'<td style="padding:10px 4px;text-align:center">'
        f'<div style="font-size:20px;font-weight:700;color:{_EMAIL_SEVERITY_COLOR[sev]}">{counts.get(sev, 0)}</div>'
        f'<div style="font-size:10px;letter-spacing:0.05em;color:#5B6B6A;text-transform:uppercase">{sev}</div></td>'
        for sev in ("critical", "high", "medium", "low")
    )

    top = sorted(ranked, key=lambda r: r.risk_score, reverse=True)[:3]
    top_rows = "".join(
        f'<tr><td style="padding:12px 0;border-top:1px solid #E5E7EB">'
        f'<span style="display:inline-block;background:{_EMAIL_SEVERITY_COLOR.get(r.severity.lower(), "#47566B")}22;'
        f'color:{_EMAIL_SEVERITY_COLOR.get(r.severity.lower(), "#47566B")};font-size:11px;font-weight:700;'
        f'padding:2px 8px;border-radius:10px;text-transform:uppercase">{_esc(r.severity)}</span> '
        f'<span style="font-size:13px;color:#5B6B6A">WCAG {_esc(r.wcag_criterion)}</span>'
        f'<div style="font-size:14px;color:#12181A;margin-top:4px;line-height:1.5">{_esc(r.risk_rationale)}</div>'
        f"</td></tr>"
        for r in top
    )
    more_note = (
        f'<p style="font-size:13px;color:#5B6B6A;margin:12px 0 0">+ {len(ranked) - 3} more finding(s) in the full report.</p>'
        if len(ranked) > 3
        else ""
    )

    return f"""
<div style="font-family:-apple-system,Segoe UI,Roboto,Arial,sans-serif;max-width:560px;margin:0 auto">
  <div style="padding-bottom:20px">
    <div style="font-size:12px;letter-spacing:0.05em;text-transform:uppercase;color:#0B6E66;font-weight:700">MAD Platform &middot; Accessibility Report</div>
    <div style="font-size:19px;font-weight:700;color:#12181A;margin-top:4px">{_esc(url)}</div>
  </div>

  <table role="presentation" width="100%" cellpadding="0" cellspacing="0" style="background:#F5F7F7;border-radius:10px;margin-bottom:20px">
    <tr>
      <td style="padding:20px;width:76px;vertical-align:top">
        <div style="width:64px;height:64px;border-radius:50%;border:5px solid {color};text-align:center;line-height:54px;font-size:22px;font-weight:800;color:{color}">{score}</div>
      </td>
      <td style="padding:20px 20px 20px 0;vertical-align:top">
        <div style="font-size:11px;letter-spacing:0.05em;color:#5B6B6A;text-transform:uppercase;margin-bottom:6px">Site score</div>
        <div style="font-size:14px;color:#12181A;line-height:1.5">{_esc(exec_summary)}</div>
      </td>
    </tr>
  </table>

  <table role="presentation" width="100%" cellpadding="0" cellspacing="0" style="margin-bottom:8px">
    <tr>{sev_cells}</tr>
  </table>

  <table role="presentation" width="100%" cellpadding="0" cellspacing="0" style="margin-bottom:20px">
    {top_rows}
  </table>
  {more_note}

  <table role="presentation" cellpadding="0" cellspacing="0" style="margin:24px 0">
    <tr>
      <td style="padding-right:10px">
        <a href="{report_url}" style="display:inline-block;background:#0B6E66;color:#ffffff;font-size:14px;font-weight:600;padding:12px 22px;border-radius:7px;text-decoration:none">View full report &rarr;</a>
      </td>
      <td>
        <a href="{csv_url}" style="display:inline-block;background:#ffffff;color:#0B6E66;font-size:14px;font-weight:600;padding:12px 22px;border-radius:7px;text-decoration:none;border:1px solid #0B6E66">Download CSV</a>
      </td>
    </tr>
  </table>
</div>
"""
