"""Platform UI: a minimal web front end over the scan pipeline.

A submission form plus a polling status page, running on the
scan-onboarding Cloud Run service. On-demand, manual scans only --
recurring/event-driven triggers (GitHub webhook, Scheduler) are a
separate, not-yet-built layer.

A scan takes 30-90s -- too long for one synchronous request -- so
POST /scan fires the pipeline as a background asyncio task and redirects
immediately to a status page that polls Firestore (which the pipeline
already checkpoints to) every couple of seconds. No new agent logic, no
new datastore -- this is a thin view over what the pipeline already writes.

Run locally: .venv/bin/uvicorn mad_platform.web.app:app --reload --port 8080
"""

from __future__ import annotations

import asyncio
import html
import logging
import os

from fastapi import FastAPI, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles

from mad_platform.agents.action_agent import resolve_escalation as resolve_finding_escalation
from mad_platform.agents.orchestrator import run_one_time_scan
from mad_platform.agents.pattern_miner import resolve_pattern_escalation
from mad_platform.agents.reporter import compute_score, score_color
from mad_platform.agents.wcag_auto_heal import resolve_kb_escalation
from mad_platform.state import firestore_client as fs
from mad_platform.state import storage_client
from mad_platform.tools.issue_sink import CsvIssueSink
from mad_platform.web import theme

# Not logging.basicConfig(): uvicorn configures its own logging on startup,
# which runs after this module is imported and silently drops INFO-level
# output from our own loggers on a cold start if we rely on basicConfig()
# alone -- confirmed in production, phase logs vanished on cold-started
# instances while uvicorn's own access logs kept working fine. Attaching a
# handler directly to the "mad_platform" namespace, independent of the
# root logger uvicorn manages, survives that.
_handler = logging.StreamHandler()
_handler.setFormatter(logging.Formatter("%(levelname)s:%(name)s:%(message)s"))
_mad_logger = logging.getLogger("mad_platform")
_mad_logger.setLevel(logging.INFO)
_mad_logger.addHandler(_handler)
_mad_logger.propagate = False

logger = logging.getLogger("mad_platform.web")

app = FastAPI(title="MAD Platform")
app.mount("/static", StaticFiles(directory=os.path.join(os.path.dirname(__file__), "static")), name="static")


def _issue_sink() -> CsvIssueSink:
    """Community fork default: no ticket-tracker credentials needed from
    anyone. Every scan gets its own CsvIssueSink instance so its rows (and
    therefore its exported CSV) stay scoped to that one scan -- see
    _run_and_store, which holds onto this instance to call .export() once
    the scan finishes.
    """
    return CsvIssueSink()

# scan-onboarding is deployed with --allow-unauthenticated -- a business
# owner has to be able to just hit the URL. That means the app itself is
# the only thing standing between this endpoint and someone using it as a
# free Gemini-calling, Playwright-fetching open relay. If MAD_ACCESS_CODE
# is set (Cloud Run deploys it from Secret Manager), a scan requires it;
# if unset (local dev), the gate is open.
_ACCESS_CODE = os.environ.get("MAD_ACCESS_CODE")

# The SME review queue is a separate trust boundary from the public scan
# form -- REQUIREMENTS §5.6 is explicit that it "must not be exposed to
# the customer/business owner". Deliberately a different code from
# MAD_ACCESS_CODE, not the same one reused, so having one doesn't imply
# having the other. The session cookie just holds the code itself rather
# than an issued token -- a reasonable simplification for this scope, not
# a production-grade session mechanism.
_REVIEW_CODE = os.environ.get("MAD_REVIEW_CODE")
_REVIEW_COOKIE = "mad_review_session"


def _is_reviewer(request: Request) -> bool:
    return not _REVIEW_CODE or request.cookies.get(_REVIEW_COOKIE) == _REVIEW_CODE

_BASE_STYLE = theme.THEME_CSS


_NAV_LINKS = [("/", "Home"), ("/faq", "FAQ"), ("/terms", "Terms"), ("/privacy", "Privacy")]


def _site_header(active: str = "/", show_cta: bool = True) -> str:
    """The one nav shared by every marketing/content page (home, faq, terms,
    privacy). Deliberately not used on the status/review/report pages --
    those are mid-task screens (watching a scan run, resolving a finding),
    where a full nav bar is a distraction from the one thing that page is
    for, not a usability win. Their existing simple "brand mark links home"
    header stays as-is on purpose.

    show_cta=False on the homepage itself: the scan form is already the
    first thing on the page there, so a second "Scan a site" button in the
    header is a redundant CTA competing with the real one. Every other page
    (FAQ, Terms, Privacy) keeps it -- that's the one way back to the form.
    """
    def _link(href: str, label: str) -> str:
        cls = ' class="active"' if href == active else ""
        return f'<a href="{href}"{cls}>{label}</a>'

    links = "".join(_link(href, label) for href, label in _NAV_LINKS)
    cta = '<a class="cta" href="/#scan">Scan a site</a>' if show_cta else ""
    return f"""<header class="site-header"><div class="site-header-inner">
  <a class="brand" href="/"><span class="dot-b"></span>MAD Platform</a>
  <nav class="site-nav">{links}{cta}</nav>
</div></header>"""


def _site_footer() -> str:
    links = "".join(f'<a href="{href}">{label}</a>' for href, label in _NAV_LINKS)
    return f"""<footer class="site-footer"><div class="site-footer-inner">
  <span>MAD Platform &middot; built during Google's All Things Agentic Hackathon, now free to use</span>
  <nav>{links}</nav>
</div></footer>"""


def _render_form(error: str | None = None) -> str:
    code_field = (
        '<label class="f-label sr-only" for="code">Access code</label>'
        '<input id="code" type="password" name="code" placeholder="Access code" required autocomplete="off">'
        if _ACCESS_CODE
        else ""
    )
    error_html = f'<div class="error-box">{html.escape(error)}</div>' if error else ""
    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>MAD Platform | Free accessibility scans for small business websites</title>
<meta name="description" content="A free, self-serve tool that scans your website for accessibility issues, verifies what it finds, and gives you real, actionable fixes, not just a report.">
{theme.FONT_LINK}
<style>{_BASE_STYLE}</style>
</head>
<body>
{_site_header("/", show_cta=False)}

<section class="view">
  <div class="hero-block">
    <span class="hero-eyebrow"><span class="dot-b"></span><span class="hero-eyebrow-text">Community Edition &middot; free &middot; no account needed</span></span>
    <h1 class="hero-title"><span class="hero-title-mark">MAD</span> Platform</h1>
    <p class="hero-tagline">Know what's exposed, before a demand letter tells you.</p>
  </div>

  <div class="scan-section" id="scan">
    <form class="scan-form glass-sheen" action="/scan" method="post" aria-label="Scan your website for accessibility issues">
      <div class="scan-field">
        <label class="sr-only" for="url">Website URL</label>
        <input id="url" type="url" name="url" placeholder="Enter your website URL" required autofocus>
      </div>
      <div class="scan-field">
        <label class="sr-only" for="email">Your email</label>
        <input id="email" type="email" name="email" placeholder="Your email" required autocomplete="email" aria-describedby="email-tip">
        <span class="info-tip" tabindex="0">
          <span class="tip-icon" aria-hidden="true">?</span>
          <span class="tip-text" id="email-tip" role="tooltip">We'll send your full report here, and use it to keep this free tool honest about whether it's actually helping. Never shared, never sold.</span>
        </span>
      </div>
      {code_field}
      <button type="submit" class="scan-submit">Scan my site &rarr;</button>
    </form>
    {error_html}
    <div class="mad-lockup centered">
      <span class="lockup-line"><span class="hl">M</span>ulti-<span class="hl">A</span>gent <span class="hl">D</span>efense <span class="hl">Platform</span></span>
      <span class="sub">for digital accessibility compliance</span>
    </div>
  </div>
</section>

<section class="view section">
  <div class="section-head">
    <h2>How it works</h2>
    <p>Two steps, no account, no setup.</p>
  </div>
  <div class="how-visual">
    <div class="how-step">
      <div class="shot-frame glass-sheen"><span class="step-badge">1</span><img src="/static/how-step1.png" alt="The scan form: enter your website URL and email"></div>
      <h3>Enter your site</h3>
    </div>
    <div class="how-step">
      <div class="shot-frame glass-sheen"><span class="step-badge">2</span><img src="/static/hero-dashboard.png" alt="A completed scan report: site score, severity breakdown, and a chart of issues by WCAG principle"></div>
      <h3>See your report</h3>
    </div>
  </div>
</section>

<section class="view section">
  <div class="section-head">
    <h2>Why it matters</h2>
    <p>The real numbers behind the risk, not marketing copy.</p>
  </div>
  <div class="stats-grid">
    <div class="stat-panel">
      <div class="pie-figure">
        <svg width="160" height="160" viewBox="0 0 140 140" role="img" aria-label="96 percent of sites fail basic accessibility tests">
          <circle cx="70" cy="70" r="58" fill="none" stroke="var(--border)" stroke-width="20"/>
          <circle class="pie-arc" cx="70" cy="70" r="58" fill="none" stroke="var(--high)" stroke-width="20"
            stroke-dasharray="349.9 364.4" stroke-dashoffset="0" transform="rotate(-90 70 70)"
            data-target="349.9" data-circumference="364.4"/>
          <text class="pie-pct" data-target="96" x="70" y="65" text-anchor="middle" font-family="Newsreader, Georgia, serif" font-size="30" font-weight="700" fill="var(--ink)">96%</text>
          <text x="70" y="86" text-anchor="middle" font-family="JetBrains Mono, monospace" font-size="9" fill="var(--muted)">FAIL</text>
        </svg>
        <p class="pie-caption">96% of the web's most visited sites fail basic accessibility tests</p>
      </div>
    </div>
    <div class="stat-panel">
      <div class="bar-figure">
        <div class="bar-col"><span class="bar-val" data-val="2452">2,452</span><div class="bar" data-h="78" style="height:78px;background:var(--border-strong)"></div><span class="bar-lbl">2024</span></div>
        <div class="bar-col"><span class="bar-val" data-val="3117">3,117</span><div class="bar" data-h="100" style="height:100px;background:var(--brand)"></div><span class="bar-lbl">2025</span></div>
      </div>
      <p class="pie-caption">Federal lawsuits over website accessibility grew 27% in one year. Including state courts, nearly 5,000 were filed in 2025 alone.</p>
    </div>
    <div class="stat-panel">
      <div class="bignum-figure">
        <div class="bn bad"><b data-val="10000">$10,000</b><span>avg. small-business settlement</span></div>
        <div class="vs">vs</div>
        <div class="bn good"><b>$0</b><span>your cost to scan</span></div>
      </div>
      <p class="pie-caption">Small businesses typically settle for $5,000&ndash;$15,000; larger companies pay $30,000&ndash;$85,000</p>
    </div>
  </div>
  <p class="stats-quote">"96% of the web's most visited sites fail basic accessibility tests. That's not a statistic, it's most of the internet simply not working for people with disabilities."</p>
</section>

{_site_footer()}
<script>
(function() {{
  if (window.matchMedia('(prefers-reduced-motion: reduce)').matches) return;
  var grid = document.querySelector('.stats-grid');
  if (!grid || !('IntersectionObserver' in window)) return;

  function ease(t) {{ return 1 - Math.pow(1 - t, 3); }}

  // runToken invalidates in-flight rAF loops from a previous run -- without
  // it, scrolling away and back quickly (before an earlier animation
  // finished) would leave two loops racing to write the same elements.
  var runToken = 0;

  function animateNumber(el, target, duration, fmt, token) {{
    var start = performance.now();
    function frame(now) {{
      if (token !== runToken) return;
      var p = Math.min(1, (now - start) / duration);
      var val = Math.round(ease(p) * target);
      el.textContent = fmt ? fmt(val) : val.toLocaleString();
      if (p < 1) requestAnimationFrame(frame);
    }}
    requestAnimationFrame(frame);
  }}

  function animateDonut(token) {{
    var arc = grid.querySelector('.pie-arc'), pct = grid.querySelector('.pie-pct');
    if (!arc || !pct) return;
    var target = parseFloat(arc.dataset.target), circ = parseFloat(arc.dataset.circumference);
    var targetPct = parseInt(pct.dataset.target, 10);
    arc.setAttribute('stroke-dasharray', '0 ' + circ);
    pct.textContent = '0%';
    var start = performance.now(), duration = 1400;
    function frame(now) {{
      if (token !== runToken) return;
      var p = Math.min(1, (now - start) / duration), e = ease(p);
      arc.setAttribute('stroke-dasharray', (e * target).toFixed(2) + ' ' + circ);
      pct.textContent = Math.round(e * targetPct) + '%';
      if (p < 1) requestAnimationFrame(frame);
    }}
    requestAnimationFrame(frame);
  }}

  function animateBars(token) {{
    grid.querySelectorAll('.bar[data-h]').forEach(function(bar) {{
      var targetH = parseFloat(bar.dataset.h);
      bar.style.height = '0px';
      var start = performance.now(), duration = 1100;
      function frame(now) {{
        if (token !== runToken) return;
        var p = Math.min(1, (now - start) / duration);
        bar.style.height = (ease(p) * targetH).toFixed(1) + 'px';
        if (p < 1) requestAnimationFrame(frame);
      }}
      requestAnimationFrame(frame);
    }});
    grid.querySelectorAll('.bar-val[data-val]').forEach(function(v) {{
      animateNumber(v, parseInt(v.dataset.val, 10), 1100, null, token);
    }});
  }}

  function animateBignum(token) {{
    grid.querySelectorAll('.bn b[data-val]').forEach(function(b) {{
      animateNumber(b, parseInt(b.dataset.val, 10), 1500, function(v) {{ return '$' + v.toLocaleString(); }}, token);
    }});
  }}

  // No unobserve: re-fires every time the section scrolls back into view,
  // not just once on first load.
  var observer = new IntersectionObserver(function(entries) {{
    entries.forEach(function(entry) {{
      if (entry.isIntersecting) {{
        runToken++;
        var token = runToken;
        animateDonut(token);
        animateBars(token);
        animateBignum(token);
      }}
    }});
  }}, {{threshold: 0.35}});
  observer.observe(grid);
}})();
</script>
</body>
</html>
"""


_STATUS_PAGE = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Scanning | MAD Platform</title>
__FONT_LINK__
<style>__STYLE__</style>
</head>
<body>
<div class="page">
  <div class="brand"><a href="/" style="color:inherit;text-decoration:none"><span class="dot-b"></span>MAD Platform</a></div>
  <h1 id="heading">Scanning __URL__</h1>
  <div class="tagline" id="tagline">This runs the real pipeline: page selection, parallel analysis, independent verification, ranking, ticket filing.</div>
  <div class="error-box" id="slow-warning" style="display:none;margin-bottom:16px">
    This is taking longer than usual (3+ minutes). Most scans finish in under 90s -- the
    site may be unusually heavy, or something may need attention. Feel free to keep
    waiting, or come back and check this page later.
  </div>
  <div class="card glass-sheen" id="content">
    <span class="spinner"></span> Starting...
  </div>
</div>
<script>
const jobId = __JOB_ID__;
const SEV_ORDER = ["critical", "high", "medium", "low"];
const SEV_VAR = {critical: "var(--crit)", high: "var(--high)", medium: "var(--med)", low: "var(--low)"};
const PRINCIPLE_ORDER = ["Perceivable", "Operable", "Understandable", "Robust"];

function donutSvg(counts) {
  const total = SEV_ORDER.reduce((s, k) => s + (counts[k] || 0), 0);
  const r = 40, C = 2 * Math.PI * r;
  let circles = `<circle cx="48" cy="48" r="${r}" fill="none" stroke="var(--border)" stroke-width="14"/>`;
  let offset = 0;
  for (const sev of SEV_ORDER) {
    const count = counts[sev] || 0;
    if (!count) continue;
    const len = (count / total) * C;
    circles += `<circle cx="48" cy="48" r="${r}" fill="none" stroke="${SEV_VAR[sev]}" stroke-width="14" ` +
      `stroke-dasharray="${len.toFixed(2)} ${(C - len).toFixed(2)}" stroke-dashoffset="${(-offset).toFixed(2)}"/>`;
    offset += len;
  }
  const legend = SEV_ORDER.map(sev =>
    `<li><span class="lg-dot" style="background:${SEV_VAR[sev]}"></span>${sev[0].toUpperCase()}${sev.slice(1)}<b>${counts[sev] || 0}</b></li>`
  ).join("");
  return `<div class="donut-wrap"><svg width="88" height="88" viewBox="0 0 96 96" role="img" aria-label="${total} findings by severity">` +
    `<g transform="rotate(-90 48 48)">${circles}</g>` +
    `<text x="48" y="45" text-anchor="middle" font-family="JetBrains Mono, monospace" font-size="19" font-weight="800" fill="var(--ink)">${total}</text>` +
    `<text x="48" y="59" text-anchor="middle" font-family="Public Sans, sans-serif" font-size="7.5" fill="var(--muted)" letter-spacing="0.4">FINDINGS</text></svg>` +
    `<ul class="donut-legend">${legend}</ul></div>`;
}

function catChart(counts) {
  const max = Math.max(1, ...PRINCIPLE_ORDER.map(p => counts[p] || 0));
  const rows = PRINCIPLE_ORDER.map(p => {
    const n = counts[p] || 0;
    return `<div class="cat-row"><span class="cat-lbl">${p}</span><div class="cat-bar-track">` +
      `<div class="cat-bar-fill" style="width:${(n / max * 100).toFixed(0)}%"></div></div><span class="cat-n">${n}</span></div>`;
  }).join("");
  return `<div class="cat-chart">${rows}</div>`;
}

function scoreNote(counts) {
  const c = counts.critical || 0, h = counts.high || 0;
  if (c) return `${c} critical issue${c !== 1 ? "s" : ""} ${c === 1 ? "needs" : "need"} immediate attention.`;
  if (h) return `${h} high-severity issue${h !== 1 ? "s" : ""} found, nothing critical.`;
  const total = SEV_ORDER.reduce((s, k) => s + (counts[k] || 0), 0);
  return total ? "Only medium- and low-severity issues found." : "No confirmed findings on the pages checked.";
}

// Human-readable label per orchestrator phase (mad_platform/agents/
// orchestrator.py's fs.set_job_phase calls) -- without this, the gaps
// between per-page checkpoints (page selection, then ranking/filing/
// report generation at the end) show nothing at all, and a healthy
// multi-second wait looks identical to a hang.
const PHASE_LABELS = {
  crawling_entry_page: "Loading the site...",
  selecting_pages: "Deciding which pages matter most...",
  analyzing_pages: "Analyzing pages for accessibility issues...",
  ranking_findings: "Ranking findings by real-world risk...",
  filing_tickets: "Filing tickets for confirmed findings...",
  generating_report: "Generating your report...",
};

let startTimeMs = null;
let finished = false;

function elapsedText() {
  if (!startTimeMs) return "";
  const secs = Math.max(0, Math.floor((Date.now() - startTimeMs) / 1000));
  return secs < 60 ? `${secs}s elapsed` : `${Math.floor(secs / 60)}m ${secs % 60}s elapsed`;
}

function tickElapsed() {
  if (finished) return;
  const el = document.getElementById("elapsed");
  if (el) el.textContent = elapsedText();
  const warn = document.getElementById("slow-warning");
  if (warn && startTimeMs && (Date.now() - startTimeMs) / 1000 > 180) {
    warn.style.display = "block";
  }
}
setInterval(tickElapsed, 1000);

// The scanned URL, page URLs, and any error message all trace back to
// user-supplied input (the form's url field) -- escape before innerHTML.
function esc(s) {
  const div = document.createElement("div");
  div.textContent = s == null ? "" : String(s);
  return div.innerHTML;
}

function stageDot(stage) {
  const done = stage === "verified";
  const color = done ? "var(--ok)" : (stage ? "var(--med)" : "var(--border)");
  return `<span class="stg-dot" style="background:${color}"></span>`;
}

function renderInProgress(data) {
  if (!startTimeMs && data.created_at) startTimeMs = new Date(data.created_at).getTime();
  document.getElementById("heading").textContent = "Scanning " + data.url;
  const phaseLabel = PHASE_LABELS[data.phase] || "Starting...";
  let rows = Object.entries(data.pages).map(([url, info]) => {
    const stage = info.stage || "pending";
    return `<li><span>${stageDot(info.stage)}${esc(url)}</span><span style="color:var(--muted)">${esc(stage)}</span></li>`;
  }).join("");
  document.getElementById("content").innerHTML =
    `<div style="display:flex;justify-content:space-between;align-items:center">` +
    `<span><span class="spinner"></span> ${esc(phaseLabel)}</span>` +
    `<span id="elapsed" style="color:var(--muted);font-size:13px">${elapsedText()}</span></div>` +
    (rows ? `<ul class="stage-list">${rows}</ul>` : "");
}

function renderCompleted(data) {
  finished = true;
  const s = data.summary;
  document.getElementById("heading").textContent = "Scan complete";
  document.getElementById("tagline").textContent = data.url;
  // (textContent above is inherently safe -- only the innerHTML build below needs esc())
  const counts = s.severity_counts;
  const pCounts = s.principle_counts || {};
  document.getElementById("content").innerHTML = `
    <div class="meta-line">${s.total_findings} confirmed finding(s) &middot; ${s.filed_count} ticket(s) filed &middot; ${s.escalated_count} awaiting your review</div>
    <div class="dash-row">
      <div class="dash-card"><div class="dc-title">Site score</div>
        <div class="dash-score">
          <div class="score-dial" style="border-color:${s.score_color};color:${s.score_color}"><div class="n">${s.score}</div><div class="l">Score</div></div>
          <div class="score-note">${esc(scoreNote(counts))}</div>
        </div>
      </div>
      <div class="dash-card"><div class="dc-title">By severity</div>${donutSvg(counts)}</div>
      <div class="dash-card"><div class="dc-title">By WCAG principle</div>${catChart(pCounts)}</div>
    </div>
    <div class="actions">
      <a class="btn" href="/report/${jobId}" target="_blank">View full report</a>
      <a class="btn ghost" href="/report/${jobId}?download=1">Download HTML</a>
      <a class="btn ghost" href="/">Scan another site</a>
    </div>`;
}

function renderFailed(data) {
  finished = true;
  document.getElementById("heading").textContent = "Scan failed";
  document.getElementById("content").innerHTML =
    `<div class="error-box">${esc(data.error || "Unknown error")}</div>
     <div class="actions"><a class="btn" href="/">Try again</a></div>`;
}

async function poll() {
  const res = await fetch(`/api/status/${jobId}`);
  if (!res.ok) return;
  const data = await res.json();
  if (data.status === "completed") { renderCompleted(data); return; }
  if (data.status === "failed") { renderFailed(data); return; }
  renderInProgress(data);
  setTimeout(poll, 2000);
}
poll();
</script>
</body>
</html>
"""


async def _run_and_store(job_id: str, url: str) -> None:
    sink = _issue_sink()
    try:
        result = await run_one_time_scan(url, job_id=job_id, issue_sink=sink)
    except Exception:
        logger.exception("Scan failed for job %s (%s)", job_id, url)
        return  # run_one_time_scan already wrote status=failed to Firestore before re-raising

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
            # new storage_client path) so the download route below can serve
            # it long after this background task has finished.
            "csv_export": sink.export(),
        },
    )


def _static_page(title: str, body_html: str, active: str = "") -> str:
    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{title} | MAD Platform</title>
{theme.FONT_LINK}
<style>{_BASE_STYLE}</style>
</head>
<body>
{_site_header(active)}
<div class="page with-site-header">
  <h1>{title}</h1>
  <div class="card glass-sheen" style="line-height:1.6">{body_html}</div>
</div>
{_site_footer()}
</body>
</html>"""


@app.get("/", response_class=HTMLResponse)
async def form_page() -> str:
    return _render_form()


@app.get("/terms", response_class=HTMLResponse)
async def terms_page() -> str:
    return _static_page(
        "Terms of Service",
        """
        <div class="trust-section-label">Read these three first</div>
        <ol class="trust-list">
          <li><h3>This is not legal advice</h3>
            <p>MAD Platform is an automated scanning tool. It looks for patterns that commonly
            indicate WCAG accessibility issues and estimates their real-world risk, it does not
            perform a legal review, does not guarantee compliance with any law or standard, and
            a clean scan is not a guarantee you are free of legal exposure. If accessibility
            compliance matters to your business in a way that carries real legal or financial
            risk, talk to a qualified attorney.</p></li>

          <li><h3>Who operates this</h3>
            <p>An independent, open-source project, not a registered company. There's no
            corporate entity standing behind these terms, only the person running it and the
            public code doing the work: <a href="https://github.com/pandayv/mad-platform-community" target="_blank" rel="noopener">github.com/pandayv/mad-platform-community</a>.
            That's also where to raise an issue or a question about how this operates.</p></li>

          <li><h3>Limitation of liability</h3>
            <p>To the fullest extent permitted by law, the operator of this tool is not liable
            for any damages, direct or indirect, arising from your use of it or reliance on its
            results, including but not limited to lost business, legal costs, or any lawsuit or
            claim related to web accessibility.</p></li>
        </ol>

        <div class="trust-section-label">The rest, for completeness</div>
        <ol class="trust-list" style="counter-reset: trust-item 3">
          <li><h3>No warranty</h3>
            <p>This tool is provided free of charge, as-is, with no warranty of any kind,
            express or implied, including accuracy, completeness, or fitness for a particular
            purpose. Automated scans can miss real issues and can flag things that aren't real
            issues.</p></li>

          <li><h3>Fair use</h3>
            <p>This is a free, self-serve, rate-limited tool intended for scanning websites you
            own or are authorized to scan. Automated abuse, attempts to bypass the rate limits,
            or use of the scan endpoint for anything other than its intended purpose is not
            permitted.</p></li>

          <li><h3>Changes</h3>
            <p>These terms may be updated as the tool evolves. Continued use after a change
            means you accept the updated terms.</p></li>
        </ol>
        """,
        active="/terms",
    )


@app.get("/privacy", response_class=HTMLResponse)
async def privacy_page() -> str:
    return _static_page(
        "Privacy Policy",
        """
        <p><strong>What we collect:</strong> the website URL you submit, the email address you
        provide, the scan results (findings, severity, suggested fixes), and, if you choose to
        leave one, your feedback on whether the report was helpful.</p>

        <p><strong>Why we collect it:</strong> the email is how your report is delivered and
        how the per-scan review link is scoped to you specifically, and it's also the only way
        we have of finding out whether this free tool is actually helping real businesses.
        The IP address of each request is used briefly for rate limiting (to keep this free
        tool usable for everyone), not stored long-term or linked to your identity beyond
        that.</p>

        <p><strong>Where it lives:</strong> on Google Cloud infrastructure (Firestore and
        Cloud Storage), in a project separate from any other project the operator runs.</p>

        <p><strong>What we don't do:</strong> we don't sell your data, and we don't share it
        with anyone outside of what's needed to run the scan itself (Google Cloud's AI models,
        used to analyze your site's public-facing pages).</p>

        <p><strong>Your control:</strong> to request deletion of your scan history or email
        address, contact the operator directly (see the FAQ for how). Feedback marked "okay to
        use as a public testimonial" may be shared publicly; anything not marked that way
        stays private.</p>
        """,
        active="/privacy",
    )


@app.get("/faq", response_class=HTMLResponse)
async def faq_page() -> str:
    return _static_page(
        "Frequently Asked Questions",
        """
        <div class="trust-section-label">Before you trust us with your URL</div>
        <ol class="trust-list">
          <li><h3>Is this actually free? What's the catch?</h3>
            <p>No catch. It's a self-funded community project, not a lead-generation funnel.
            There's an optional way to chip in once the donation option is live, but the
            scanner itself never requires it and never will.</p></li>

          <li><h3>Why do you need my email?</h3>
            <p>Three reasons, no others: to send you the report, to create your private review
            link so only you (not anyone else who uses this tool) can see your own findings,
            and as a basic safeguard against the free tool being abused. Full detail in the
            <a href="/privacy">privacy policy</a>.</p></li>

          <li><h3>Who's actually behind this?</h3>
            <p>A solo, open-source project, not a company. The code that runs this exact site
            is public: <a href="https://github.com/pandayv/mad-platform-community" target="_blank" rel="noopener">github.com/pandayv/mad-platform-community</a>.
            You can read exactly what it does with your URL and your email before you ever
            submit either.</p></li>
        </ol>

        <div class="trust-section-label">What it does and doesn't do</div>
        <ol class="trust-list" style="counter-reset: trust-item 3">
          <li><h3>Why is it called MAD Platform?</h3>
            <p>MAD is short for Multi-Agent Defense Platform. Multi-agent because it's genuinely
            a team of specialized AI agents working together, one decides what to check, one
            finds issues, one independently verifies them, one takes action, not a single model
            doing everything at once. Defense because that's the actual job: catching gaps
            before they become a legal problem, not just reporting on them after the fact.</p></li>

          <li><h3>What is WCAG?</h3>
            <p>The Web Content Accessibility Guidelines, the standard most digital
            accessibility laws and lawsuits point back to. This tool checks your site against
            it.</p></li>

          <li><h3>What does this tool actually do?</h3>
            <p>It scans the pages on your site that carry the most real risk, checks them with
            both rule-based and AI-assisted review, independently verifies every finding before
            showing it to you, and gives you a concrete fix for each confirmed issue, plus a
            downloadable, tracker-importable list.</p></li>

          <li><h3>What doesn't it do?</h3>
            <p>It doesn't replace a real accessibility audit or legal review, doesn't check
            every possible WCAG criterion, and doesn't fix your site for you, it tells you what
            to fix and how.</p></li>

          <li><h3>I need real legal help, not just a scan.</h3>
            <p>This tool can tell you what's wrong technically; it can't tell you what your
            specific legal exposure is. Talk to a qualified accessibility or ADA attorney for
            that.</p></li>
        </ol>
        """,
        active="/faq",
    )


@app.post("/scan")
async def start_scan(request: Request, url: str = Form(...), email: str = Form(...), code: str = Form("")) -> Response:
    if _ACCESS_CODE and code != _ACCESS_CODE:
        return HTMLResponse(_render_form(error="Wrong access code."), status_code=403)
    email = email.strip()
    if "@" not in email or "." not in email.split("@")[-1]:
        return HTMLResponse(_render_form(error="Please enter a valid email address."), status_code=400)
    # request.client.host is the direct connection IP -- if this ever sits
    # behind a proxy/load balancer that isn't Cloud Run's own (which already
    # gives the real client IP here), an X-Forwarded-For read would be
    # needed instead. Fine as-is for a Cloud Run deployment.
    client_ip = request.client.host if request.client else "unknown"
    allowed, reason = fs.check_and_reserve_scan_quota(email, client_ip)
    if not allowed:
        return HTMLResponse(_render_form(error=reason), status_code=429)
    job_id = fs.create_job(url, owner_contact=email)
    asyncio.create_task(_run_and_store(job_id, url))
    return RedirectResponse(f"/status/{job_id}", status_code=303)


@app.get("/status/{job_id}", response_class=HTMLResponse)
async def status_page(job_id: str) -> str:
    job = fs.get_job(job_id)
    if job is None:
        raise HTTPException(404, "No such job")
    return (
        _STATUS_PAGE.replace("__STYLE__", _BASE_STYLE)
        .replace("__FONT_LINK__", theme.FONT_LINK)
        .replace("__URL__", html.escape(job["url"]))
        .replace("__JOB_ID__", f'"{job_id}"')
    )


@app.get("/api/status/{job_id}")
async def api_status(job_id: str) -> JSONResponse:
    job = fs.get_job(job_id)
    if job is None:
        raise HTTPException(404, "No such job")
    created_at = job.get("created_at")
    return JSONResponse(
        {
            "job_id": job_id,
            "url": job["url"],
            "status": job.get("status", "in_progress"),
            "phase": job.get("phase"),
            "error": job.get("error"),
            "pages": {url: {"stage": info.get("stage")} for url, info in job.get("pages", {}).items()},
            "summary": job.get("summary"),
            "created_at": created_at.isoformat() if created_at else None,
        }
    )


@app.get("/report/{job_id}")
async def get_report(job_id: str, download: int = 0) -> Response:
    content = storage_client.read_report(job_id)
    if content is None:
        raise HTTPException(404, "Report not found (job may not be complete yet)")
    headers = {"Content-Disposition": f'attachment; filename="{job_id}.html"'} if download else {}
    return Response(content=content, media_type="text/html", headers=headers)


@app.get("/report/{job_id}/tickets.csv")
async def get_tickets_csv(job_id: str) -> Response:
    job = fs.get_job(job_id)
    if job is None or not job.get("summary"):
        raise HTTPException(404, "Report not found (job may not be complete yet)")
    csv_text = job["summary"].get("csv_export", "")
    return Response(
        content=csv_text,
        media_type="text/csv",
        headers={"Content-Disposition": f'attachment; filename="{job_id}-findings.csv"'},
    )


@app.post("/report/{job_id}/feedback")
async def submit_feedback(
    job_id: str,
    rating: int = Form(...),
    comment: str = Form(""),
    allow_testimonial: bool = Form(False),
    contact: str = Form(""),
) -> JSONResponse:
    """The immediate "was this helpful" prompt shown on the report page and
    in the report email -- asking at the moment the report is delivered
    gets meaningfully better response rates than a delayed follow-up.
    """
    if fs.get_job(job_id) is None:
        raise HTTPException(404, "No such job")
    fs.save_feedback(job_id, rating=rating, comment=comment, allow_testimonial=allow_testimonial, contact=contact or None)
    return JSONResponse({"ok": True})


@app.get("/api/escalation/{escalation_id}/status")
async def escalation_status(escalation_id: str) -> JSONResponse:
    """Public, unauthenticated on purpose -- this is what closes the loop
    for a report reopened later (see reporter.py's escalation-badge
    script). Deliberately minimal: whether it's resolved and, if so,
    whether a ticket was filed. No reviewer identity, no rationale, no
    internal deliberation -- nothing here would be a problem for a
    customer to see, unlike the review queue itself.
    """
    escalation = fs.get_escalation(escalation_id)
    if escalation is None:
        raise HTTPException(404, "No such escalation")
    resolved = escalation.get("status") == "resolved"
    ticket_id = fs.get_ticket_for_finding(escalation_id) if resolved else None
    # A saved/downloaded report is opened from whatever origin the viewer's
    # browser gives it (file://, a different host entirely) -- this only
    # works cross-origin with an explicit CORS header, and it's safe to
    # allow any origin here since the payload is already public-safe.
    return JSONResponse(
        {"resolved": resolved, "ticket_id": ticket_id},
        headers={"Access-Control-Allow-Origin": "*"},
    )


def _render_review_login(error: str | None = None) -> str:
    error_html = f'<div class="error-box">{html.escape(error)}</div>' if error else ""
    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Internal Review | MAD Platform</title>
{theme.FONT_LINK}
<style>{_BASE_STYLE}</style>
</head>
<body>
<div class="page">
  <div class="brand"><span class="dot-b"></span>MAD Platform</div>
  <h1>Internal review queue</h1>
  <div class="tagline">Not for customer access. Authorized reviewers only.</div>
  <div class="card glass-sheen">
    <form action="/review/login" method="post">
      <label class="f-label" for="rcode">Review code</label>
      <input id="rcode" type="password" name="code" required autofocus autocomplete="off">
      <div style="margin-top:14px"><button type="submit">Enter</button></div>
    </form>
    {error_html}
  </div>
</div>
</body>
</html>"""


def _review_item_row(e: dict) -> str:
    eid = html.escape(e["id"])
    kind = e.get("kind")
    if kind == "kb_version_change":
        return (
            f'<tr><td><span class="badge sev-low">KB version</span></td>'
            f"<td>WCAG {html.escape(str(e.get('old_version')))} → {html.escape(str(e.get('new_version')))}, "
            f"classified {html.escape(str(e.get('change_type')))}</td>"
            f'<td class="mono">{e.get("confidence", 0):.2f}</td>'
            f'<td><a class="btn btn-secondary" href="/review/{eid}" style="padding:6px 14px;font-size:12.5px">Review →</a></td></tr>'
        )
    if kind == "learned_pattern":
        return (
            f'<tr><td><span class="badge sev-low">Learned pattern</span></td>'
            f"<td>WCAG {html.escape(str(e.get('wcag_criterion', '?')))}, seen {e.get('occurrence_count', 0)} time(s)</td>"
            f'<td class="mono">{e.get("confidence", 0):.2f}</td>'
            f'<td><a class="btn btn-secondary" href="/review/{eid}" style="padding:6px 14px;font-size:12.5px">Review →</a></td></tr>'
        )
    sev = str(e.get("severity", "medium")).lower()
    return (
        f'<tr><td><span class="badge sev-{sev}">Finding</span></td>'
        f"<td>WCAG {html.escape(str(e.get('wcag_criterion', '?')))} &middot; {html.escape(str(e.get('page_url', '')))}</td>"
        f'<td class="mono">{e.get("editor_confidence", 0):.2f}</td>'
        f'<td><a class="btn btn-secondary" href="/review/{eid}" style="padding:6px 14px;font-size:12.5px">Review →</a></td></tr>'
    )


def _render_review_list(pending: list[dict]) -> str:
    if not pending:
        items_html = '<div class="tagline" style="margin:0">Nothing pending. The queue is empty.</div>'
    else:
        rows = "".join(_review_item_row(e) for e in pending)
        items_html = (
            '<table class="q-list"><thead><tr><th>Type</th><th>Detail</th><th>Confidence</th><th></th></tr>'
            f"</thead><tbody>{rows}</tbody></table>"
        )
    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Internal Review | MAD Platform</title>
{theme.FONT_LINK}
<style>{_BASE_STYLE}</style>
</head>
<body>
<div class="page wide">
  <div class="brand"><span class="dot-b"></span>MAD Platform</div>
  <h1>Internal review queue</h1>
  <div class="tagline">{len(pending)} item(s) awaiting disposition.</div>
  <div class="card glass-sheen">{items_html}</div>
</div>
</body>
</html>"""


def _render_review_detail(e: dict, message: str | None = None) -> str:
    eid = e["id"]
    message_html = f'<div class="success-box">{html.escape(message)}</div>' if message else ""

    if e.get("kind") == "kb_version_change":
        body = f"""
          <div class="field"><b>WCAG version change</b> <span class="badge sev-low">KB version</span></div>
          <div class="field">{html.escape(str(e.get('old_version')))} → {html.escape(str(e.get('new_version')))}</div>
          <div class="field">Classified: {html.escape(str(e.get('change_type')))} (confidence {e.get('confidence', 0):.2f})</div>
          <div class="field">{html.escape(str(e.get('reasoning', '')))}</div>
        """
    elif e.get("kind") == "learned_pattern":
        samples = "".join(
            f'<div class="fix-cell" style="max-width:none;margin-bottom:6px">{html.escape(str(r))}</div>'
            for r in e.get("sample_rationales", [])
        )
        body = f"""
          <div class="field"><b>Learned dismissal pattern</b> <span class="badge sev-low">Learned pattern</span></div>
          <div class="field"><b>WCAG criterion:</b> {html.escape(str(e.get('wcag_criterion', '')))}</div>
          <div class="field"><b>Seen:</b> {e.get('occurrence_count', 0)} time(s) across independent scans</div>
          <div class="field"><b>Confidence:</b> <span class="mono">{e.get('confidence', 0):.2f}</span></div>
          <div class="field"><b>Pattern:</b> {html.escape(str(e.get('pattern_description', '')))}</div>
          <div class="field"><b>Sample dismissal rationales:</b></div>
          {samples}
          <div class="field" style="margin-top:10px;color:var(--muted);font-size:12.5px">
            Confirming adds this to Editor's grounding on every future scan. Dismissing discards it -- the miner won't re-propose this exact pattern.
          </div>
        """
    else:
        sev = str(e.get("severity", "medium")).lower()
        body = f"""
          <div class="field"><b>WCAG {html.escape(str(e.get('wcag_criterion', '')))}</b> <span class="badge sev-{sev}">{html.escape(str(e.get('severity', '')))}</span></div>
          <div class="field"><b>Page:</b> {html.escape(str(e.get('page_url', '')))}</div>
          <div class="field"><b>Confidence:</b> <span class="mono">{e.get('editor_confidence', 0):.2f}</span></div>
          <div class="field"><b>Evidence:</b> {html.escape(str(e.get('editor_rationale', '')))}</div>
          <div class="field"><b>Suggested fix:</b></div>
          <div class="fix-cell" style="max-width:none">{html.escape(str(e.get('suggested_fix', '')))}</div>
        """

    resolved = e.get("status") == "resolved"
    if resolved:
        actions = f'<div class="tagline" style="margin:0">Already resolved: {html.escape(str(e.get("disposition")))}.</div>'
    else:
        actions = f"""
          <form action="/review/{eid}/resolve" method="post" style="display:inline-block;margin-right:10px">
            <button type="submit" name="disposition" value="confirm">Confirm</button>
          </form>
          <form action="/review/{eid}/resolve" method="post" style="display:inline-block">
            <button type="submit" name="disposition" value="dismiss" class="btn-secondary">Dismiss</button>
          </form>
        """

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Internal Review | MAD Platform</title>
{theme.FONT_LINK}
<style>{_BASE_STYLE}</style>
</head>
<body>
<div class="page">
  <div class="brand"><a href="/review" style="color:inherit;text-decoration:none"><span class="dot-b"></span>MAD Platform · Review Queue</a></div>
  <h1>Review item</h1>
  <div class="card glass-sheen">
    {body}
    <div style="margin-top:20px">{actions}</div>
  </div>
  {message_html}
</div>
</body>
</html>"""


@app.get("/review", response_class=HTMLResponse)
async def review_list(request: Request) -> Response:
    if not _is_reviewer(request):
        return HTMLResponse(_render_review_login())
    return HTMLResponse(_render_review_list(fs.list_pending_escalations()))


@app.post("/review/login")
async def review_login(code: str = Form(...)) -> Response:
    if _REVIEW_CODE and code != _REVIEW_CODE:
        return HTMLResponse(_render_review_login(error="Wrong review code."), status_code=403)
    resp = RedirectResponse("/review", status_code=303)
    resp.set_cookie(_REVIEW_COOKIE, _REVIEW_CODE or "", httponly=True, samesite="lax")
    return resp


@app.get("/review/{escalation_id}", response_class=HTMLResponse)
async def review_detail(escalation_id: str, request: Request) -> Response:
    if not _is_reviewer(request):
        return HTMLResponse(_render_review_login())
    escalation = fs.get_escalation(escalation_id)
    if escalation is None:
        raise HTTPException(404, "No such escalation")
    return HTMLResponse(_render_review_detail(escalation))


@app.post("/review/{escalation_id}/resolve")
async def review_resolve(escalation_id: str, request: Request, disposition: str = Form(...)) -> Response:
    if not _is_reviewer(request):
        return HTMLResponse(_render_review_login())
    escalation = fs.get_escalation(escalation_id)
    if escalation is None:
        raise HTTPException(404, "No such escalation")
    if escalation.get("status") == "resolved":
        return HTMLResponse(_render_review_detail(escalation, message="Already resolved."))

    kind = escalation.get("kind")
    if kind == "kb_version_change":
        resolve_kb_escalation(escalation_id, disposition=disposition, reviewer="web-review")
    elif kind == "learned_pattern":
        resolve_pattern_escalation(escalation_id, disposition=disposition, reviewer="web-review")
    else:
        resolve_finding_escalation(_issue_sink(), escalation_id, disposition=disposition, reviewer="web-review")

    updated = fs.get_escalation(escalation_id)
    return HTMLResponse(_render_review_detail(updated, message=f"Marked {disposition}."))


# ---- Per-scan review link: replaces the shared admin queue above for the
# community fork's "finding" kind escalations. Anyone with this exact
# job_id + token combination can see and resolve only that one scan's
# pending items -- no admin code, no visibility into any other scan.
# kb_version_change and learned_pattern escalations are cross-cutting
# admin concerns (no owning job), and deliberately stay on the /review
# path above, never routed through this one. See firestore_client.py's
# create_escalation()/verify_review_token() docstrings for the reasoning.


def _render_scoped_review_list(job_id: str, token: str, pending: list[dict]) -> str:
    if not pending:
        items_html = '<div class="tagline" style="margin:0">Nothing pending. Every finding from this scan has been filed or is still being confirmed.</div>'
    else:
        rows = "".join(
            f'<tr><td><span class="badge sev-{html.escape(str(e.get("severity", "medium")).lower())}">Finding</span></td>'
            f"<td>WCAG {html.escape(str(e.get('wcag_criterion', '?')))} &middot; {html.escape(str(e.get('page_url', '')))}</td>"
            f'<td class="mono">{e.get("editor_confidence", 0):.2f}</td>'
            f'<td><a class="btn btn-secondary" href="/review/link/{job_id}/{token}/{html.escape(e["id"])}" style="padding:6px 14px;font-size:12.5px">Review →</a></td></tr>'
            for e in pending
        )
        items_html = (
            '<table class="q-list"><thead><tr><th>Type</th><th>Detail</th><th>Confidence</th><th></th></tr>'
            f"</thead><tbody>{rows}</tbody></table>"
        )
    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Your Review Queue | MAD Platform</title>
{theme.FONT_LINK}
<style>{_BASE_STYLE}</style>
</head>
<body>
<div class="page wide">
  <div class="brand"><span class="dot-b"></span>MAD Platform</div>
  <h1>Your review queue</h1>
  <div class="tagline">Findings from your scan that need a quick judgment call -- only you can see this.</div>
  <div class="card glass-sheen">{items_html}</div>
</div>
</body>
</html>"""


def _render_scoped_review_detail(job_id: str, token: str, e: dict, message: str | None = None) -> str:
    eid = e["id"]
    message_html = f'<div class="success-box">{html.escape(message)}</div>' if message else ""
    sev = str(e.get("severity", "medium")).lower()
    body = f"""
      <div class="field"><b>WCAG {html.escape(str(e.get('wcag_criterion', '')))}</b> <span class="badge sev-{sev}">{html.escape(str(e.get('severity', '')))}</span></div>
      <div class="field"><b>Page:</b> {html.escape(str(e.get('page_url', '')))}</div>
      <div class="field"><b>Confidence:</b> <span class="mono">{e.get('editor_confidence', 0):.2f}</span></div>
      <div class="field"><b>Evidence:</b> {html.escape(str(e.get('editor_rationale', '')))}</div>
      <div class="field"><b>Suggested fix:</b></div>
      <div class="fix-cell" style="max-width:none">{html.escape(str(e.get('suggested_fix', '')))}</div>
    """
    resolved = e.get("status") == "resolved"
    if resolved:
        actions = f'<div class="tagline" style="margin:0">Already resolved: {html.escape(str(e.get("disposition")))}.</div>'
    else:
        actions = f"""
          <form action="/review/link/{job_id}/{token}/{eid}/resolve" method="post" style="display:inline-block;margin-right:10px">
            <button type="submit" name="disposition" value="confirm">Confirm</button>
          </form>
          <form action="/review/link/{job_id}/{token}/{eid}/resolve" method="post" style="display:inline-block">
            <button type="submit" name="disposition" value="dismiss" class="btn-secondary">Dismiss</button>
          </form>
        """
    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Your Review Queue | MAD Platform</title>
{theme.FONT_LINK}
<style>{_BASE_STYLE}</style>
</head>
<body>
<div class="page">
  <div class="brand"><a href="/review/link/{job_id}/{token}" style="color:inherit;text-decoration:none"><span class="dot-b"></span>MAD Platform · Your Review Queue</a></div>
  <h1>Review item</h1>
  <div class="card glass-sheen">
    {body}
    <div style="margin-top:20px">{actions}</div>
  </div>
  {message_html}
</div>
</body>
</html>"""


def _scoped_escalation_or_404(job_id: str, token: str, escalation_id: str) -> dict:
    if not fs.verify_review_token(job_id, token):
        # Same 404 whether the job doesn't exist or the token's wrong --
        # a wrong-token guess should look identical to a nonexistent job,
        # not confirm the job is real.
        raise HTTPException(404, "Not found")
    escalation = fs.get_escalation(escalation_id)
    if escalation is None or escalation.get("job_id") != job_id:
        raise HTTPException(404, "Not found")
    return escalation


@app.get("/review/link/{job_id}/{token}", response_class=HTMLResponse)
async def scoped_review_list(job_id: str, token: str) -> Response:
    if not fs.verify_review_token(job_id, token):
        raise HTTPException(404, "Not found")
    pending = [e for e in fs.list_escalations_for_job(job_id) if e.get("status") == "pending"]
    return HTMLResponse(_render_scoped_review_list(job_id, token, pending))


@app.get("/review/link/{job_id}/{token}/{escalation_id}", response_class=HTMLResponse)
async def scoped_review_detail(job_id: str, token: str, escalation_id: str) -> Response:
    escalation = _scoped_escalation_or_404(job_id, token, escalation_id)
    return HTMLResponse(_render_scoped_review_detail(job_id, token, escalation))


@app.post("/review/link/{job_id}/{token}/{escalation_id}/resolve")
async def scoped_review_resolve(job_id: str, token: str, escalation_id: str, disposition: str = Form(...)) -> Response:
    escalation = _scoped_escalation_or_404(job_id, token, escalation_id)
    if escalation.get("status") == "resolved":
        return HTMLResponse(_render_scoped_review_detail(job_id, token, escalation, message="Already resolved."))

    # A throwaway sink just for this one resolution -- its row would
    # otherwise be lost, since the scan's own CsvIssueSink instance is long
    # gone by the time a reviewer clicks Confirm. If this adds a row
    # (confirm, not dismiss), append it into the job's already-persisted
    # CSV export so the download stays complete after the fact.
    sink = _issue_sink()
    resolve_finding_escalation(sink, escalation_id, disposition=disposition, reviewer="site-owner")
    if sink.rows:
        job = fs.get_job(job_id) or {}
        summary = dict(job.get("summary") or {})
        existing_csv = summary.get("csv_export", "")
        new_row_csv = sink.export().split("\n", 1)[1] if "\n" in sink.export() else ""  # drop the header row
        summary["csv_export"] = existing_csv.rstrip("\n") + "\n" + new_row_csv if existing_csv else sink.export()
        fs.save_scan_summary(job_id, summary)

    updated = _scoped_escalation_or_404(job_id, token, escalation_id)
    return HTMLResponse(_render_scoped_review_detail(job_id, token, updated, message=f"Marked {disposition}."))
