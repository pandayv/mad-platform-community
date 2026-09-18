"""Platform UI: a minimal web front end over the scan pipeline.

A submission form plus a polling status page, running on the
scan-onboarding Cloud Run service. On-demand, manual scans only --
recurring/event-driven triggers (GitHub webhook, Scheduler) are a
separate, not-yet-built layer.

A scan takes 2-3 minutes and does real memory-heavy work (Playwright,
multiple Gemini calls) -- too much to run in-process on the same
container that's serving everyone else's requests. POST /scan enqueues a
Cloud Task instead and redirects immediately to a status page that polls
Firestore (which the pipeline already checkpoints to) every couple of
seconds. The task is picked up by scan-worker (mad_platform/web/
worker_app.py), a separate Cloud Run service sized and scaled for that
workload specifically -- this service never touches Playwright itself
anymore, which is what keeps it cheap to run at high concurrency.

Run locally: .venv/bin/uvicorn mad_platform.web.app:app --reload --port 8080
"""

from __future__ import annotations

import asyncio
import hashlib
import html
import json
import logging
import os
import secrets
import time
from urllib.parse import quote, urlsplit

import hmac
from contextlib import asynccontextmanager
from functools import lru_cache

import httpx
from fastapi import FastAPI, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from google.api_core import exceptions as gcloud_exceptions
from google.cloud import tasks_v2

from mad_platform import config
from mad_platform.agents.action_agent import resolve_escalation as resolve_finding_escalation
from mad_platform.agents.pattern_miner import resolve_pattern_escalation
from mad_platform.severity import SEVERITY_ORDER
from mad_platform.state import firestore_client as fs
from mad_platform.state import storage_client
from mad_platform.tools import abuse_guard, notify
from mad_platform.tools.issue_sink import CsvIssueSink
from mad_platform.tools.url_safety import UnsafeTargetError, assert_safe_target
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

@asynccontextmanager
async def _lifespan(_app: FastAPI):
    """Refuse to serve if the deployment is missing required config.

    This is the fail-fast that used to live in module-level
    `os.environ[...]` reads below -- moved to startup rather than import
    so that importing this module (a test, a tool, `python -c "import
    mad_platform.web.app"`) does not itself require a configured GCP
    environment. That import-time requirement is what made this codebase
    untestable; see mad_platform/config.py. The deployment-safety property
    is unchanged: a revision without SCAN_WORKER_URL / SCAN_QUEUE_INVOKER_SA
    / GOOGLE_CLOUD_PROJECT still dies on startup with a message naming the
    variable, instead of silently accepting scans it can never run.
    """
    config.validate_web_config()
    yield


app = FastAPI(title="MAD Platform", lifespan=_lifespan)
app.mount("/static", StaticFiles(directory=os.path.join(os.path.dirname(__file__), "static")), name="static")


@app.middleware("http")
async def _security_headers(request: Request, call_next):
    """Baseline hardening with no functional cost -- none of these change
    behavior for a legitimate request, they only remove attack surface
    (clickjacking via iframe embedding, MIME-sniffing a served file as
    something it isn't, a browser silently downgrading to http on a stale
    bookmark/link). Deliberately not attempting a Content-Security-Policy
    here -- every page on this app relies on inline <script>/<style>
    (no nonce infrastructure exists), so a CSP strict enough to mean
    anything would break the pages it's meant to protect. Worth doing
    properly later, not as a quick add-on.
    """
    response = await call_next(request)
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["X-Frame-Options"] = "DENY"
    response.headers["Referrer-Policy"] = "strict-origin-when-cross-origin"
    response.headers["Strict-Transport-Security"] = "max-age=31536000; includeSubDomains"
    # No Cache-Control was ever set on the HTML pages, which left every
    # browser free to apply its own heuristic caching -- and this app is
    # under active, frequent redesign, so a visitor's browser silently
    # showing a stale cached page (looking "wrong" compared to what's
    # actually deployed) is a real, not theoretical, risk. /static/* assets
    # (images, fonts) are the one thing that's actually fine to cache.
    # /static/* assets and the content-hashed theme stylesheet set their own
    # long-lived Cache-Control; everything else (every HTML page) is
    # no-store. A content-hashed URL cannot go stale, so overwriting its
    # header here would throw away the whole point of F9's fix.
    if not (request.url.path.startswith("/static") or request.url.path == _THEME_CSS_PATH):
        response.headers["Cache-Control"] = "no-store"
    return response


def _issue_sink() -> CsvIssueSink:
    """Community fork default: no ticket-tracker credentials needed from
    anyone. Every scan gets its own CsvIssueSink instance so its rows (and
    therefore its exported CSV) stay scoped to that one scan -- used here
    for the (lightweight) escalation-resolve routes below; the scan
    pipeline itself runs on scan-worker now, which creates its own.
    """
    return CsvIssueSink()


# ---- Cloud Tasks: /scan enqueues here instead of running the pipeline
# in-process. The queue's configuration is still required, not defaulted
# -- a fork that forgot to configure the queue must not silently accept
# scan submissions it can never actually run -- but it is now read (and
# the client built) on first use, with _lifespan above doing the
# fail-on-startup check. See mad_platform/config.py for why nothing here
# may be read at import time.


@lru_cache(maxsize=1)
def _tasks_client() -> tasks_v2.CloudTasksClient:
    return tasks_v2.CloudTasksClient()


def _queue_path() -> str:
    return _tasks_client().queue_path(
        config.project_id(), config.scan_queue_location(), config.scan_queue_name()
    )


def _enqueue_scan(job_id: str) -> None:
    """job_id doubles as the Cloud Tasks task name: a second enqueue for
    the same job_id (a retried request) is rejected by Cloud Tasks as a
    duplicate rather than starting the same scan twice, no separate
    idempotency bookkeeping needed here.
    """
    worker_url = config.scan_worker_url()
    task = {
        "name": _tasks_client().task_path(
            config.project_id(), config.scan_queue_location(), config.scan_queue_name(), job_id
        ),
        "http_request": {
            "http_method": tasks_v2.HttpMethod.POST,
            "url": f"{worker_url}/run",
            "headers": {"Content-Type": "application/json"},
            "body": json.dumps({"job_id": job_id}).encode(),
            "oidc_token": {
                "service_account_email": config.scan_queue_invoker_sa(),
                "audience": worker_url,
            },
        },
    }
    _tasks_client().create_task(parent=_queue_path(), task=task)


# scan-onboarding is deployed with --allow-unauthenticated -- a business
# owner has to be able to just hit the URL. That means the app itself is
# the only thing standing between this endpoint and someone using it as a
# free Gemini-calling, Playwright-fetching open relay. Layered defenses,
# cheapest first: a honeypot field + minimum-fill-time check (below, no
# signup needed), then abuse_guard's email/domain checks, then
# firestore_client's per-email/per-IP/monthly quota. Cloudflare Turnstile
# is the next layer up -- opt-in via env var: if TURNSTILE_SECRET_KEY is
# unset (no site registered yet), the gate is simply open, so this is safe
# to leave wired in ahead of actually signing up for a site key. Note this
# is deliberately the OPPOSITE of MAD_REVIEW_CODE's unset behavior below,
# which fails closed: an unset key here weakens one of several stacked
# anti-abuse layers on a deliberately public form, while an unset code
# there would publish an admin queue that is not meant to be public at all.
_TURNSTILE_SITE_KEY = os.environ.get("TURNSTILE_SITE_KEY")
_TURNSTILE_SECRET_KEY = os.environ.get("TURNSTILE_SECRET_KEY")


async def _turnstile_passed(token: str) -> bool:
    if not _TURNSTILE_SECRET_KEY:
        return True
    if not token:
        return False
    try:
        async with httpx.AsyncClient(timeout=5.0) as client:
            resp = await client.post(
                "https://challenges.cloudflare.com/turnstile/v0/siteverify",
                data={"secret": _TURNSTILE_SECRET_KEY, "response": token},
            )
        resp.raise_for_status()
        return bool(resp.json().get("success"))
    except (httpx.HTTPError, ValueError) as exc:
        # Cloudflare being unreachable shouldn't take the whole scan form
        # down -- fail open here, same as an unset secret key.
        #
        # ValueError is in that tuple because of a real gap, not for
        # neatness: only httpx.HTTPError was caught, and resp.json() raises
        # json.JSONDecodeError -- a ValueError, not an httpx error --
        # whenever the body is not JSON. A Cloudflare 502/503 HTML error
        # page, a captive portal, or a WAF block would all have produced an
        # unhandled exception and a 500 on POST /scan/start for every
        # visitor, which is the exact opposite of the fail-open this
        # comment promises. raise_for_status() above turns a non-2xx into
        # an httpx error before .json() ever sees the body.
        #
        # Logged, because a permanent silent fail-open looks identical to
        # a working gate from the outside. This is currently latent
        # (TURNSTILE_SECRET_KEY is unset, so the short-circuit above
        # returns first), and arms itself the moment Turnstile is enabled.
        logger.warning("Turnstile verification failed open (%s: %s)", type(exc).__name__, exc)
        return True


# Email verification: a visitor proves they control an email once (a
# 6-digit code, see firestore_client.generate_email_code), and this
# browser is remembered for DEVICE_COOKIE_DAYS afterward -- the landing
# page itself only ever asks for a URL. The cookie carries an opaque
# random token; only its hash is ever stored server-side (never the raw
# token), same reasoning as never storing a password in plaintext -- a
# leaked Firestore doc can't be replayed as a cookie.
_DEVICE_COOKIE = "mad_verified_device"
_DEVICE_COOKIE_DAYS = fs.REMEMBER_DEVICE_DAYS


def _hash_device_token(raw: str) -> str:
    return hashlib.sha256(raw.encode()).hexdigest()


def _verified_email(request: Request) -> str | None:
    token = request.cookies.get(_DEVICE_COOKIE)
    if not token:
        return None
    return fs.get_verified_device_email(_hash_device_token(token))


def _set_device_cookie(resp: Response, request: Request, email: str) -> None:
    raw_token = secrets.token_urlsafe(32)
    fs.set_verified_device(_hash_device_token(raw_token), email)
    resp.set_cookie(
        _DEVICE_COOKIE,
        raw_token,
        max_age=_DEVICE_COOKIE_DAYS * 86400,
        httponly=True,
        samesite="lax",
        secure=_is_https(request),
    )


# The SME review queue is a separate trust boundary from the public scan
# form -- REQUIREMENTS §5.6 is explicit that it "must not be exposed to
# the customer/business owner". It gates /review, which lists EVERY
# pending escalation across every scan and every user, and whose resolve
# action on a learned_pattern permanently changes what Editor flags on
# every future scan for everyone (DECISIONS_LOG.md: the one flow that must
# keep a human gate).
#
# So: closed unless explicitly opened. `not _REVIEW_CODE or ...` used to
# make _is_reviewer return True unconditionally when MAD_REVIEW_CODE was
# unset -- the exact opposite of the docstring above, and one missing
# secret binding (a new revision, a rotation, a fork, local dev) away from
# publishing the whole queue. An unset code now means nobody gets in,
# which is the safe direction to fail: the worst case is an admin who has
# to set the variable, not an anonymous visitor poisoning the detection
# pipeline.
_REVIEW_COOKIE = "mad_review_session"


def _review_session_value(code: str) -> str:
    """What goes in the session cookie: a value derived from the review
    code, not the code itself. Same single-shared-secret model as before
    (a stolen cookie is still a credential, and this is still not a
    production-grade session mechanism) -- but the shared secret itself no
    longer leaves the server, so a leaked cookie can't be replayed at the
    login form or anywhere else the code is used.
    """
    return hmac.new(code.encode(), b"mad-review-session", hashlib.sha256).hexdigest()


def _is_reviewer(request: Request) -> bool:
    code = config.review_code()
    if not code:
        return False  # fail CLOSED -- see the comment above
    presented = request.cookies.get(_REVIEW_COOKIE)
    if not presented:
        return False
    return hmac.compare_digest(presented, _review_session_value(code))


def _is_https(request: Request) -> bool:
    """secure=True only over https. Cloud Run terminates TLS at its own edge
    and forwards plain http to the container, and uvicorn isn't started with
    --proxy-headers, so request.url.scheme itself would always read "http"
    here even in production -- checking X-Forwarded-Proto (which Cloud Run
    always sets, regardless of that flag) is what actually distinguishes
    production from local dev (uvicorn --reload on plain http, where this
    header is simply absent). Shared by every cookie-setting route.
    """
    return request.headers.get("x-forwarded-proto") == "https" or request.url.scheme == "https"

# ---- The theme stylesheet, served once and cached forever.
#
# It used to be inlined into all nine page templates, on every response,
# under the blanket `Cache-Control: no-store` below -- so every page view
# re-downloaded ~24KB of CSS, most of it rules that page cannot use. The
# no-store decision is right for the HTML (this app is under active
# redesign and a stale cached page is a real risk), it just meant the CSS
# rode along uncached with it.
#
# The URL carries a hash of the content, so "cache forever" is safe by
# construction: any edit to THEME_CSS produces a different URL, and a
# browser can never be looking at a stale stylesheet for the page it is
# rendering. That is also why this may skip the no-store rule below.
#
# The stored report deliberately keeps its CSS inline (reporter.py). It has
# to open as a standalone document from GCS, from a downloaded file, from
# an email attachment -- a linked stylesheet would leave it unstyled
# everywhere except this origin.
_THEME_CSS_HASH = hashlib.sha256(theme.THEME_CSS.encode("utf-8")).hexdigest()[:12]
_THEME_CSS_PATH = f"/theme.{_THEME_CSS_HASH}.css"
_BASE_STYLE_LINK = f'<link rel="stylesheet" href="{_THEME_CSS_PATH}">'


@app.get(_THEME_CSS_PATH, include_in_schema=False)
async def theme_css() -> Response:
    return Response(
        content=theme.THEME_CSS,
        media_type="text/css",
        headers={"Cache-Control": "public, max-age=31536000, immutable"},
    )


# Shared first child of every <body>: the skip link, then the page's own
# <main id="main"> landmark. Neither existed on any of the nine pages, so a
# screen-reader or keyboard user had to traverse the header on every page
# with no way past it, and no landmark to jump to (WCAG 2.4.1). For a
# product whose comparison table advertises catching exactly this class of
# problem, it is worth more than its severity suggests.
_SKIP_LINK = '<a class="skip-link" href="#main">Skip to main content</a>'


# Footer gets the full sitemap-style list -- it's not space-constrained the
# way the header is, and "every link, always available somewhere" is exactly
# what a footer is for.
_FOOTER_NAV_LINKS = [("/", "Home"), ("/faq", "FAQ"), ("/terms", "Terms"), ("/privacy", "Privacy")]

# FAQ plus the homepage's own section anchors -- brought back after being
# emptied out entirely in an earlier pass. That pass's actual problem was
# never "too many links," it was that the header wrapped to two lines on
# narrow phones with no way to collapse it; removing links was a patch on
# that specific symptom, not a fix for it. The real fix is the .nav-toggle
# / hamburger menu below, which collapses this list on narrow viewports
# regardless of how many items it holds -- so the link count can be
# whatever's actually useful again. Terms and Privacy deliberately stay
# footer-only, not here: they're not something a visitor mid-read jumps to,
# unlike these. Anchors use "/#..." (not bare "#...") so they resolve
# correctly from every page this header renders on, not just the homepage.
_HEADER_NAV_LINKS: list[tuple[str, str]] = [
    ("/#why-it-matters", "Why it matters"),
    ("/#how-it-works", "How it works"),
    ("/#under-the-hood", "Under the hood"),
    ("/#how-we-compare", "How we compare"),
    ("/faq", "FAQ"),
]


def _site_header(active: str = "/") -> str:
    """The one nav shared by every marketing/content page (home, faq, terms,
    privacy). Deliberately not used on the status/review/report pages --
    those are mid-task screens (watching a scan run, resolving a finding),
    where a full nav bar is a distraction from the one thing that page is
    for, not a usability win. Their existing simple "brand mark links home"
    header stays as-is on purpose.

    No CTA button here, on any page: the brand mark already links to "/",
    and "/" opens on the scan form -- so a "Scan a site" button next to it
    would just be a second way to do what the logo already does, styled to
    look like a distinct action.

    Below 860px (.nav-toggle's own breakpoint, see theme.py) the nav
    collapses behind a hamburger button rather than wrapping to a second
    row or disappearing -- both icons and the panel markup always render;
    which one is visible is CSS/JS, not two different server responses, so
    this works identically with JS disabled except the panel can't be
    toggled (a link-only degrade, not a broken one: every link is still
    real markup, not injected after the fact). The toggle script is
    inlined here rather than in a page-level <script> block so it ships
    automatically on every page this header renders on, including the
    plain static pages (FAQ/Terms/Privacy) that otherwise have no JS at
    all.
    """
    def _link(href: str, label: str) -> str:
        cls = ' class="active"' if href == active else ""
        return f'<a href="{href}"{cls}>{label}</a>'

    links = "".join(_link(href, label) for href, label in _HEADER_NAV_LINKS)
    return f"""<header class="site-header"><div class="site-header-inner">
  <a class="brand" href="/">{theme.BRAND_MARK}MAD Platform</a>
  <button class="nav-toggle" type="button" aria-expanded="false" aria-controls="site-nav" aria-label="Menu">
    <svg class="icon-menu" aria-hidden="true" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round"><path d="M4 7h16M4 12h16M4 17h16"/></svg>
    <svg class="icon-close" aria-hidden="true" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round"><path d="M6 6l12 12M18 6L6 18"/></svg>
  </button>
  <nav class="site-nav" id="site-nav">{links}</nav>
</div></header>
<script>
(function(){{
  var btn = document.querySelector(".nav-toggle"), nav = document.getElementById("site-nav");
  if (!btn || !nav) return;
  function close(){{ btn.setAttribute("aria-expanded", "false"); nav.classList.remove("is-open"); }}
  function open(){{ btn.setAttribute("aria-expanded", "true"); nav.classList.add("is-open"); }}
  btn.addEventListener("click", function(){{
    btn.getAttribute("aria-expanded") === "true" ? close() : open();
  }});
  nav.addEventListener("click", function(e){{ if (e.target.tagName === "A") close(); }});
  document.addEventListener("keydown", function(e){{ if (e.key === "Escape") close(); }});
}})();
</script>"""


def _site_footer() -> str:
    links = "".join(f'<a href="{href}">{label}</a>' for href, label in _FOOTER_NAV_LINKS)
    links += (
        '<a href="https://buymeacoffee.com/madplatform" target="_blank" rel="noopener" class="support-link">'
        '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" '
        'stroke-linejoin="round" aria-hidden="true"><path d="M17 8h1a4 4 0 1 1 0 8h-1"/>'
        '<path d="M3 8h14v9a4 4 0 0 1-4 4H7a4 4 0 0 1-4-4Z"/><line x1="6" y1="2" x2="6" y2="4"/>'
        '<line x1="10" y1="2" x2="10" y2="4"/><line x1="14" y1="2" x2="14" y2="4"/></svg>Support the project</a>'
    )
    return f"""<footer class="site-footer"><div class="site-footer-inner">
  <span>MAD Platform &middot; built during Google's All Things Agentic Hackathon, now free to use</span>
  <nav>{links}</nav>
</div></footer>"""


def _render_form(error: str | None = None, device_verified: bool = False) -> str:
    error_html = f'<div class="error-box">{html.escape(error)}</div>' if error else ""
    turnstile_script = (
        '<script src="https://challenges.cloudflare.com/turnstile/v0/api.js" async defer></script>'
        if _TURNSTILE_SITE_KEY
        else ""
    )
    turnstile_widget = f'<div class="cf-turnstile" data-sitekey="{_TURNSTILE_SITE_KEY}"></div>' if _TURNSTILE_SITE_KEY else ""
    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>MAD Platform | Free accessibility scans for small business websites</title>
<meta name="description" content="A free, self-serve tool that scans your website for accessibility issues, verifies what it finds, and gives you real, actionable fixes, not just a report.">
{theme.FONT_LINK}
{turnstile_script}
{_BASE_STYLE_LINK}
</head>
<body>
{_SKIP_LINK}
{_site_header("/")}

<main id="main">
<section class="view">
  <div class="hero-outer"><div class="hero-grid">
    <div class="hero-copy" id="scan">
      <span class="hero-eyebrow"><span class="dot-b"></span><span class="hero-eyebrow-text">Community Edition</span></span>
      <h1 class="hero-title">Is your website accessible? <strong>Find out before it costs you.</strong></h1>
      <p class="hero-tagline">Check your website's accessibility and get exactly what to fix, in plain English. Protect your business from expensive accessibility lawsuits.</p>

      <div class="scan-section">
        <form class="scan-form" action="/scan/start" method="post" aria-label="Scan your website for accessibility issues">
          <div class="scan-bar">
            {theme.BRAND_MARK}
            <label class="sr-only" for="url">Website URL</label>
            <input id="url" type="text" inputmode="url" name="url" placeholder="Enter your website URL" autocapitalize="off" autocorrect="off" spellcheck="false" required autofocus>
            <button type="submit" class="scan-submit">Scan</button>
          </div>
          <input type="text" name="website" tabindex="-1" autocomplete="off" aria-hidden="true" style="position:absolute;left:-9999px;width:1px;height:1px;opacity:0">
          <input type="hidden" name="form_ts" value="{int(time.time())}">
          {turnstile_widget}
        </form>
        {error_html}
        {
          '<p class="scan-hint"><svg aria-hidden="true" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5"><path d="M5 13l4 4L19 7"/></svg>Device recognized. Your scan starts instantly.</p>'
          if device_verified else
          '<p class="scan-hint scan-hint-tip"><svg aria-hidden="true" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M9 18h6"/><path d="M10 22h4"/><path d="M12 2a7 7 0 0 0-4 12.7c.6.5 1 1.3 1 2.1v.2h6v-.2c0-.8.4-1.6 1-2.1A7 7 0 0 0 12 2Z"/></svg>First scan requires email verification to prevent abuse.</p>'
        }
      </div>

      <div class="trust-row">
        <span><svg aria-hidden="true" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5"><path d="M5 13l4 4L19 7"/></svg>Open source</span>
        <span><svg aria-hidden="true" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5"><path d="M5 13l4 4L19 7"/></svg>Dedicated to community</span>
        <span><svg aria-hidden="true" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5"><path d="M5 13l4 4L19 7"/></svg>100% free</span>
      </div>
    </div>

    <div class="hero-visual-col">
      <!-- Illustrative only: the numbers below ("72", "18 issues across 6
           pages", the severity counts) are a made-up example report, not a
           scan of anything. The mock browser chrome makes that obvious to
           a sighted visitor; nothing conveyed it non-visually, so a screen
           reader announced concrete-sounding scan results on a page where
           the visitor has not run a scan. aria-hidden on the whole visual
           plus the sr-only line below is what says so. -->
      <p class="sr-only">Illustration: an example accessibility report, showing made-up results for a fictional site. Not a real scan.</p>
      <div class="hero-visual" aria-hidden="true">
        <div class="grid-lines"></div>
        <div class="example-card">
          <div class="browser-bar"><i></i><i></i><i></i><span class="url">yoursite.com</span></div>
          <div class="card-body">
            <div class="example-score-row">
              <div class="example-score-ring">
                <svg aria-hidden="true" viewBox="0 0 36 36">
                  <circle class="ring-bg" cx="18" cy="18" r="15.9155" fill="none" stroke-width="3"/>
                  <circle class="ring-fg" cx="18" cy="18" r="15.9155" fill="none" stroke-width="3" stroke-dasharray="72 100" stroke-linecap="round" transform="rotate(-90 18 18)"/>
                </svg>
                <span class="example-score-num">72</span>
              </div>
              <div class="example-score-meta">
                <b>Accessibility score</b>
                <span>18 issues across 6 pages</span>
              </div>
            </div>
            <div class="example-sev-rows">
              <div class="example-sev-row"><span class="dot" style="background:var(--crit)"></span>Critical<b>2</b></div>
              <div class="example-sev-row"><span class="dot" style="background:var(--high)"></span>High<b>5</b></div>
              <div class="example-sev-row"><span class="dot" style="background:var(--med)"></span>Medium<b>8</b></div>
              <div class="example-sev-row"><span class="dot" style="background:var(--low)"></span>Low<b>3</b></div>
            </div>
          </div>
        </div>
        <div class="example-chip ok example-chip-1"><svg aria-hidden="true" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="3"><path d="M5 13l4 4L19 7"/></svg>Alt text found</div>
        <div class="example-chip warn example-chip-2"><svg aria-hidden="true" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5"><path d="M12 9v4M12 17h.01M10.29 3.86L1.82 18a2 2 0 001.71 3h16.94a2 2 0 001.71-3L13.71 3.86a2 2 0 00-3.42 0z"/></svg>Low contrast &middot; 4 spots</div>
      </div>
      <p class="lockup-caption"><span class="hl">M</span>ulti-<span class="hl">A</span>gent <span class="hl">D</span>efense <span class="hl">Platform</span> for digital accessibility compliance</p>
    </div>
  </div></div>
  <a class="scroll-hint" href="#why-it-matters" aria-label="Scroll to see more">
    <svg aria-hidden="true" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M12 5v14M5 12l7 7 7-7"/></svg>
  </a>
</section>

<section class="section" id="why-it-matters">
  <div class="section-head">
    <div class="section-eyebrow"><span class="line"></span><span>The stakes</span><span class="line"></span></div>
    <h2>Why it matters</h2>
    <p>The real numbers behind the risk, not marketing copy.</p>
  </div>
  <div class="stats-grid">
    <div class="stat-panel">
      <div class="stat-topline"></div>
      <div class="stat-visual">
        <div class="pie-figure">
          <svg width="160" height="160" viewBox="0 0 140 140" role="img" aria-label="96 percent of sites fail basic accessibility tests">
            <circle cx="70" cy="70" r="58" fill="none" stroke="var(--border)" stroke-width="20"/>
            <circle class="pie-arc" cx="70" cy="70" r="58" fill="none" stroke="var(--high)" stroke-width="20"
              stroke-dasharray="349.9 364.4" stroke-dashoffset="0" transform="rotate(-90 70 70)"
              data-target="349.9" data-circumference="364.4"/>
            <text class="pie-pct" data-target="96" x="70" y="65" text-anchor="middle" font-family="Newsreader, Georgia, serif" font-size="30" font-weight="700" fill="var(--ink)">96%</text>
            <text x="70" y="86" text-anchor="middle" font-family="JetBrains Mono, monospace" font-size="9" fill="var(--muted)">FAIL</text>
          </svg>
        </div>
      </div>
      <p class="pie-caption">96% of the web's most visited sites fail basic accessibility tests</p>
    </div>
    <div class="stat-panel">
      <div class="stat-topline"><span class="bar-growth">&uarr; 23% year-over-year</span></div>
      <div class="stat-visual">
        <div class="bar-figure">
          <div class="bar-col"><span class="bar-val" data-val="4000">4,000</span><div class="bar" data-h="97" style="height:97px;background:var(--border-strong)"></div><span class="bar-lbl">2024</span></div>
          <div class="bar-col"><span class="bar-val" data-val="4928">4,928</span><div class="bar" data-h="120" style="height:120px;background:var(--brand)"></div><span class="bar-lbl">2025</span></div>
        </div>
      </div>
      <p class="pie-caption">Website accessibility lawsuits across federal and state courts grew 23% in one year, reaching nearly 5,000 in 2025 -- plus an estimated 7&ndash;10 demand letters for every one that actually reaches court.</p>
    </div>
    <div class="stat-panel">
      <div class="stat-topline"></div>
      <div class="stat-visual">
        <div class="bignum-figure">
          <div class="bn bad"><b data-val="10000">$10,000</b><span>avg. small-business settlement</span></div>
          <div class="vs">vs</div>
          <div class="bn good"><b>$0</b><span>your cost to scan</span></div>
        </div>
      </div>
      <p class="pie-caption">Small businesses typically settle for $5,000&ndash;$15,000; larger companies pay $30,000&ndash;$85,000</p>
    </div>
  </div>
  <p class="stats-quote">"96% of the web's most visited sites fail basic accessibility tests. That's not a statistic, it's most of the internet simply not working for people with disabilities."</p>
</section>

<section class="section" id="how-it-works">
  <div class="section-head">
    <div class="section-eyebrow"><span class="line"></span><span>Process</span><span class="line"></span></div>
    <h2>How it works</h2>
    <p>Paste your URL. No account, no setup<sup>1</sup>, no catch.</p>
  </div>
  <div class="how-visual">
    <div class="how-step">
      <div class="shot-frame glass-sheen"><span class="step-badge">1</span><img src="/static/how-step1.png?v=20260917c" alt="The scan form: enter your website URL"></div>
      <h3>Enter your site</h3>
    </div>
    <div class="how-step">
      <div class="shot-frame glass-sheen"><span class="step-badge">2</span><img src="/static/hero-dashboard.png?v=20260917c" alt="A completed scan report: site score, severity breakdown, and a chart of issues by WCAG principle"></div>
      <h3>See your report</h3>
    </div>
  </div>
  <p class="how-footnote"><sup>1</sup> First-time visitors may need a one-time email verification to prevent abuse.</p>
</section>



<section class="section" id="under-the-hood">
  <div class="section-head">
    <div class="section-eyebrow"><span class="line"></span><span>Under the hood</span><span class="line"></span></div>
    <h2>Why the report holds up</h2>
    <p>Four specialized agents, not one model guessing.</p>
  </div>
  <div class="pipeline-flow">
    <div class="pipeline-stage">
      <div class="pipeline-badge"><svg aria-hidden="true" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><circle cx="12" cy="12" r="8"/><circle cx="12" cy="12" r="2.5" fill="currentColor" stroke="none"/></svg></div>
      <h3>Select</h3>
      <p>Finds the pages that carry real risk.</p>
    </div>
    <div class="pipeline-arrow"><svg aria-hidden="true" viewBox="0 0 24 14" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M1 7h20M14 1l7 6-7 6"/></svg></div>
    <div class="pipeline-stage">
      <div class="pipeline-badge"><svg aria-hidden="true" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M12 3 3 8l9 5 9-5-9-5Z"/><path d="M3 16l9 5 9-5"/><path d="M3 12l9 5 9-5"/></svg></div>
      <h3>Analyze</h3>
      <p>Rules plus AI, across visual, structural, and media checks.</p>
    </div>
    <div class="pipeline-arrow"><svg aria-hidden="true" viewBox="0 0 24 14" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M1 7h20M14 1l7 6-7 6"/></svg></div>
    <div class="pipeline-stage">
      <div class="pipeline-badge"><svg aria-hidden="true" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M12 3 4 6v6c0 5 3.4 7.8 8 9 4.6-1.2 8-4 8-9V6l-8-3Z"/><path d="m9 12 2 2 4-4"/></svg></div>
      <h3>Verify</h3>
      <p>Independently re-checked. False alarms dropped.</p>
    </div>
    <div class="pipeline-arrow"><svg aria-hidden="true" viewBox="0 0 24 14" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M1 7h20M14 1l7 6-7 6"/></svg></div>
    <div class="pipeline-stage">
      <div class="pipeline-badge"><svg aria-hidden="true" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M20 14v4a2 2 0 0 1-2 2H6a2 2 0 0 1-2-2v-4"/><path d="M7 9l5 5 5-5"/><path d="M12 14V2"/></svg></div>
      <h3>Act</h3>
      <p>Ranked by risk, exported as a ready-to-use fix list.</p>
    </div>
  </div>
  <div class="arch-notes">
    <div class="arch-note">
      <svg aria-hidden="true" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M2 4.5A2.5 2.5 0 0 1 4.5 2H12v18H4.5A2.5 2.5 0 0 0 2 22.5V4.5Z"/><path d="M22 4.5A2.5 2.5 0 0 0 19.5 2H12v18h7.5a2.5 2.5 0 0 1 2.5 2.5V4.5Z"/></svg>
      <div><b>Grounded</b><span>Findings are checked against the real accessibility standard (WCAG) by retrieval, not the model's memory alone.</span></div>
    </div>
    <div class="arch-note">
      <svg aria-hidden="true" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M3 12a9 9 0 1 0 3-6.7"/><path d="M3 3v5h5"/></svg>
      <div><b>Crash-safe</b><span>A scan interrupted mid-way resumes exactly where it left off, never duplicating work.</span></div>
    </div>
    <div class="arch-note">
      <svg aria-hidden="true" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M21 12a9 9 0 1 1-3-6.7"/><path d="M21 3v5h-5"/></svg>
      <div><b>Self-healing</b><span>Checks whether WCAG itself has changed and refreshes the ruleset automatically.</span></div>
    </div>
  </div>
</section>

<section class="section" id="how-we-compare">
  <div class="section-head">
    <div class="section-eyebrow"><span class="line"></span><span>The comparison</span><span class="line"></span></div>
    <h2>How we compare</h2>
    <p>What we actually checked, not marketing copy.</p>
  </div>
  <p class="compare-hint" id="compare-hint">Swipe or use the arrow keys to see all columns &rarr;</p>
  <!-- tabindex="0" is load-bearing, not decoration: this wrapper is
       overflow-x:auto around a min-width:560px table, so below 560px a
       keyboard-only user could reach the first column and nothing else --
       a WCAG 2.1.1 (Keyboard) failure, in the very table whose first row
       advertises catching this class of problem. A scrollable region needs
       to be focusable to be scrollable by keyboard. role="region" +
       aria-label give it a name once it is focusable, and the hint text
       (previously pointer-only, "Swipe...") now names the keyboard route
       too. -->
  <!-- aria-label, not aria-labelledby="compare-hint": that hint is
       display:none above 640px, and a name that only exists at phone
       widths is not a name. -->
  <div class="compare-wrap" tabindex="0" role="region" aria-label="Feature comparison: MAD Platform, free scanners and paid audit tools">
    <table class="compare-table">
      <thead>
        <tr>
          <th scope="col" rowspan="2"></th>
          <th scope="col" class="mad-col">MAD Platform</th>
          <th scope="col">Free scanners</th>
          <th scope="col">Paid audit tools</th>
        </tr>
        <tr>
          <th scope="col" class="mad-col"><span class="col-sub">Free, always</span></th>
          <th scope="col"><span class="col-sub">Free tier only<sup>1</sup></span></th>
          <th scope="col"><span class="col-sub">$29&ndash;$249+/mo</span></th>
        </tr>
      </thead>
      <tbody>
        <tr>
          <th scope="row">Multi-page scan</th>
          <td class="mad-col"><span class="mark-yes" aria-hidden="true">&check;</span><span class="sr-only">Yes</span></td>
          <td><span class="mark-no" aria-hidden="true">&cross;</span><span class="sr-only">No</span></td>
          <td><span class="mark-yes" aria-hidden="true">&check;</span><span class="sr-only">Yes</span></td>
        </tr>
        <tr>
          <th scope="row">Reviews actual screenshots</th>
          <td class="mad-col"><span class="mark-yes" aria-hidden="true">&check;</span><sup>2</sup><span class="sr-only">Yes</span></td>
          <td><span class="mark-no" aria-hidden="true">&cross;</span><span class="sr-only">No</span></td>
          <td><span class="mark-partial">Varies by vendor</span></td>
        </tr>
        <tr>
          <th scope="row">Audio/video captions</th>
          <td class="mad-col"><span class="mark-yes" aria-hidden="true">&check;</span><span class="sr-only">Yes</span></td>
          <td><span class="mark-no" aria-hidden="true">&cross;</span><span class="sr-only">No</span></td>
          <td><span class="mark-no" aria-hidden="true">&cross;</span><span class="sr-only">Rarely</span></td>
        </tr>
        <tr>
          <th scope="row">Filters out false positives</th>
          <td class="mad-col"><span class="mark-yes" aria-hidden="true">&check;</span><span class="sr-only">Yes, automatic</span></td>
          <td><span class="mark-no" aria-hidden="true">&cross;</span><span class="sr-only">No</span></td>
          <td><span class="mark-partial">Add-on only<sup>3</sup></span></td>
        </tr>
        <tr>
          <th scope="row">Provides a fix</th>
          <td class="mad-col"><span class="mark-yes" aria-hidden="true">&check;</span><span class="sr-only">Yes</span></td>
          <td><span class="mark-yes" aria-hidden="true">&check;</span><span class="sr-only">Yes</span></td>
          <td><span class="mark-partial">Often upsells a widget<sup>4</sup></span></td>
        </tr>
        <tr>
          <th scope="row">Follows latest standards</th>
          <td class="mad-col"><span class="mark-yes" aria-hidden="true">&check;</span><sup>5</sup><span class="sr-only">Yes, automatic</span></td>
          <td><span class="mark-partial">Often outdated</span></td>
          <td><span class="mark-partial">Varies by vendor</span></td>
        </tr>
      </tbody>
    </table>
  </div>
  <p class="compare-footnote"><sup>1</sup> Most free checkers cap what's included and require a paid tier for full scans. <sup>2</sup> Rule-based scanners check the underlying code; they don't evaluate what the page actually looks like once it renders. <sup>3</sup> Automated scanners are well known for flagging non-issues; catching what they get wrong is typically a separate paid add-on for these tools. <sup>4</sup> The FTC fined a major overlay-widget vendor $1M in 2025 for overstating what its auto-fix could actually do. <sup>5</sup> MAD Platform checks the accessibility standard (WCAG) for changes and updates its rules automatically.</p>
</section>
</main>

{_site_footer()}
<script>
(function() {{
  // Cross-page navigation to /#scan (the header's "Scan a site" link from
  // FAQ/Terms/Privacy) was landing well past the form -- the browser's
  // native fragment-scroll fires on the initial layout pass, but web
  // fonts loading afterward reflow the page (different text metrics),
  // leaving the anchor scrolled to a since-stale position. Re-aligning
  // after fonts actually finish (and again on window load, as a fallback
  // for browsers/cases where document.fonts isn't reliable) fixes it
  // without needing to guess a hardcoded delay.
  if (location.hash !== '#scan') return;
  function jump() {{
    var el = document.getElementById('scan');
    if (el) el.scrollIntoView({{block: 'start'}});
  }}
  jump();
  if (window.document && document.fonts && document.fonts.ready) {{
    document.fonts.ready.then(jump);
  }}
  window.addEventListener('load', jump);
}})();
</script>
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


def _verification_page(
    title: str, tagline: str, body_html: str, error: str | None = None, footnote_html: str = ""
) -> str:
    """Shared chrome for the email/code interstitial screens -- same
    header/footer as every other page (see the landing-page nav-
    consistency fix), just a narrower single-purpose card instead of the
    homepage's full marketing layout.

    footnote_html renders below the card, not inside it -- the same
    "spacing separates it, not a border" treatment as the landing page's
    .how-footnote, for the same reason: it's a footnote explaining the
    step above, not part of the step itself.
    """
    error_html = f'<div class="error-box">{html.escape(error)}</div>' if error else ""
    footnote_block = f'<p class="verify-footnote">{footnote_html}</p>' if footnote_html else ""
    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{title} | MAD Platform</title>
{theme.FONT_LINK}
{_BASE_STYLE_LINK}
</head>
<body>
{_SKIP_LINK}
{_site_header("/")}
<main id="main" class="page with-site-header" style="max-width:440px">
  <h1>{title}</h1>
  <p class="tagline">{tagline}</p>
  <div class="card glass-sheen">{body_html}</div>
  {footnote_block}
  {error_html}
</main>
{_site_footer()}
</body>
</html>"""


def _render_email_step(url: str, error: str | None = None) -> str:
    body = f"""
      <form action="/scan/request-code" method="post">
        <input type="hidden" name="url" value="{html.escape(url)}">
        <div class="scan-field">
          <label class="sr-only" for="email">Your email</label>
          <input id="email" type="email" name="email" placeholder="Your email" required autocomplete="email" autofocus>
        </div>
        <input type="text" name="website" tabindex="-1" autocomplete="off" aria-hidden="true" style="position:absolute;left:-9999px;width:1px;height:1px;opacity:0">
        <input type="hidden" name="form_ts" value="{int(time.time())}">
        <button type="submit" class="scan-submit">Send verification code &rarr;</button>
      </form>
    """
    footnote = (
        "This is anti-abuse protection, not marketing. It's what keeps bots from draining a free "
        f"tool that isn't free to run. It also delivers your report and scopes your private review "
        f"link. Verified once, skipped for the next {_DEVICE_COOKIE_DAYS} days."
    )
    return _verification_page(
        "Verify your email", "A one-time anti-bot verification to prevent abuse.", body, error, footnote
    )


def _render_code_step(url: str, email: str, error: str | None = None) -> str:
    body = f"""
      <p class="tagline" style="margin:0 0 16px">Code sent to <b>{html.escape(email)}</b>.</p>
      <form action="/scan/verify-code" method="post">
        <input type="hidden" name="url" value="{html.escape(url)}">
        <input type="hidden" name="email" value="{html.escape(email)}">
        <div class="scan-field">
          <label class="sr-only" for="code">Verification code</label>
          <input id="code" type="text" name="code" placeholder="6-digit code" inputmode="numeric" pattern="[0-9]*" maxlength="6" required autofocus autocomplete="one-time-code">
        </div>
        <button type="submit" class="scan-submit">Verify &amp; scan &rarr;</button>
      </form>
      <div class="tagline" style="margin:16px 0 0;display:flex;justify-content:space-between">
        <form action="/scan/request-code" method="post" style="display:inline">
          <input type="hidden" name="url" value="{html.escape(url)}">
          <input type="hidden" name="email" value="{html.escape(email)}">
          <input type="text" name="website" tabindex="-1" autocomplete="off" aria-hidden="true" style="position:absolute;left:-9999px;width:1px;height:1px;opacity:0">
          <input type="hidden" name="form_ts" value="{int(time.time())}">
          <button type="submit" class="link-btn">Resend code</button>
        </form>
        <a href="/scan/email?url={quote(url)}">Use a different email</a>
      </div>
    """
    return _verification_page("Enter your code", "Expires in 10 minutes.", body, error)


_STATUS_PAGE = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Scanning | MAD Platform</title>
__FONT_LINK__
__STYLE_LINK__
</head>
<body>
<a class="skip-link" href="#main">Skip to main content</a>
<div class="scan-beam" id="scan-beam" aria-hidden="true" style="display:none"></div>
<main id="main" class="page">
  <div class="brand"><a href="/" style="color:inherit;text-decoration:none">__BRAND_MARK__MAD Platform</a></div>
  <h1 id="heading">__URL__</h1>
  <div class="tagline" id="tagline">This runs the real pipeline: page selection, parallel analysis, independent verification, ranking, exporting fixes.</div>
  <div class="error-box" id="slow-warning" style="display:none;margin-bottom:16px">
    This is taking longer than usual (4+ minutes). Most scans finish in 2-3 minutes -- the
    site may be unusually heavy, or something may need attention. Feel free to keep
    waiting, or come back and check this page later.
  </div>
  <!-- role="status" + aria-live="polite": this element's innerHTML is
       rewritten on every poll, and without a live region a screen-reader
       user got no announcement at all as the scan progressed or finished.
       The page simply went quiet and changed underneath them, on the one
       screen whose entire purpose is reporting progress. "polite" rather
       than "assertive" because a scan update should queue behind whatever
       the user is doing, not interrupt it. -->
  <div class="card glass-sheen" id="content" role="status" aria-live="polite">
    <span class="spinner"></span> Starting...
  </div>
</main>
<script>
const jobId = __JOB_ID__;
// Injected from mad_platform/severity.py and theme.SEVERITY_VAR rather
// than retyped here. This was the fifth independent copy of the severity
// vocabulary, and the one furthest from the others -- a tier added in
// Python would have rendered in the report and silently vanished from this
// page's donut, whose ring total would then disagree with the
// total_findings headline printed directly above it.
const SEV_ORDER = __SEV_ORDER__;
const SEV_VAR = __SEV_VAR__;
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
  filing_tickets: "Exporting fixes for confirmed findings...",
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
  if (warn && startTimeMs && (Date.now() - startTimeMs) / 1000 > 240) {
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

function renderQueued(data) {
  document.getElementById("scan-beam").style.display = "none";
  document.getElementById("heading").textContent = "Queued: " + data.url;
  document.getElementById("tagline").textContent =
    "Waiting for a scan slot to free up -- this happens automatically, usually within a couple of minutes.";
  document.getElementById("content").innerHTML =
    `<div style="display:flex;align-items:center;gap:10px"><span class="spinner"></span> In queue...</div>` +
    `<div class="tagline" style="margin:12px 0 0">No need to keep this tab open -- we'll email your full ` +
    `report to the address you submitted as soon as it's ready. This same link will always show the ` +
    `current status, so it's safe to close this and check back later.</div>`;
}

function renderInProgress(data) {
  document.getElementById("scan-beam").style.display = "block";
  if (!startTimeMs && (data.started_at || data.created_at)) startTimeMs = new Date(data.started_at || data.created_at).getTime();
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

// Returns false (and renders nothing final) if the summary isn't there
// yet, so the caller knows to keep polling. The server writes status and
// summary in one atomic update now, so this should not happen for any job
// completed by current code -- but jobs completed by the older two-write
// path exist, and a renderer that throws on missing data is exactly what
// turned that race into a permanently frozen page. Degrade, don't throw.
function renderCompleted(data) {
  const s = data.summary;
  if (!s || !s.severity_counts) {
    document.getElementById("content").innerHTML =
      `<div style="display:flex;align-items:center;gap:10px"><span class="spinner"></span> ` +
      `Scan complete -- loading your results...</div>`;
    return false;
  }
  document.getElementById("scan-beam").style.display = "none";
  finished = true;
  document.getElementById("heading").textContent = "Scan complete";
  document.getElementById("tagline").textContent = data.url;
  // (textContent above is inherently safe -- only the innerHTML build below needs esc())
  const counts = s.severity_counts;
  const pCounts = s.principle_counts || {};
  document.getElementById("content").innerHTML = `
    <div class="meta-line">${s.total_findings} confirmed finding(s) &middot; ${s.filed_count} fix(es) exported &middot; ${s.escalated_count} awaiting your review</div>
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
    </div>
    <p style="margin:16px 0 0;font-size:12.5px;line-height:1.6;color:var(--muted)">If this
      scan just saved you the cost of a demand letter, <a href="https://buymeacoffee.com/madplatform"
      target="_blank" rel="noopener">a coffee helps keep it free</a> for the next business that needs it.</p>`;
  return true;
}

function renderFailed(data) {
  document.getElementById("scan-beam").style.display = "none";
  finished = true;
  document.getElementById("heading").textContent = "Scan failed";
  document.getElementById("content").innerHTML =
    `<div class="error-box">${esc(data.error || "Unknown error")}</div>
     <div class="actions"><a class="btn" href="/">Try again</a></div>`;
}

// A scan takes minutes; this page has to survive a bad couple of seconds
// in the middle of it. Every previous exit from poll() was permanent --
// `if (!res.ok) return;` ended polling forever on one transient 500, one
// 429 or one cold-start blip, a network error rejected the fetch with no
// catch at all, and a throw inside a renderer did the same. The user was
// left on a frozen "Starting..." screen for a scan that was still running
// or had already succeeded, with nothing on screen to say so. So: the
// ONLY ways out of this loop now are a terminal state (completed with a
// summary, or failed) and the retry ceiling below.
const POLL_MS = 2000;
const QUEUED_POLL_MS = 3000;
const MAX_CONSECUTIVE_ERRORS = 15;  // ~2 minutes of backed-off retries before giving up
let consecutiveErrors = 0;

function renderPollError() {
  document.getElementById("content").innerHTML =
    `<div class="error-box">We lost contact with the server while checking on your scan. ` +
    `The scan itself is still running -- reload this page to pick the status back up.</div>
     <div class="actions"><a class="btn" href="">Reload</a></div>`;
}

async function poll() {
  let nextDelay = POLL_MS;
  try {
    const res = await fetch(`/api/status/${jobId}`);
    if (!res.ok) throw new Error(`status ${res.status}`);
    const data = await res.json();
    consecutiveErrors = 0;
    if (data.status === "completed") {
      if (renderCompleted(data)) return;  // false = summary not written yet, keep polling
    } else if (data.status === "failed") {
      renderFailed(data);
      return;
    } else if (data.status === "queued") {
      renderQueued(data);
      nextDelay = QUEUED_POLL_MS;
    } else {
      renderInProgress(data);
    }
  } catch (err) {
    consecutiveErrors += 1;
    if (consecutiveErrors >= MAX_CONSECUTIVE_ERRORS) {
      finished = true;
      renderPollError();
      return;
    }
    // Linear backoff, capped: brief blips recover in ~2s, a longer outage
    // stops hammering an already-struggling server.
    nextDelay = Math.min(POLL_MS * consecutiveErrors, 15000);
  }
  setTimeout(poll, nextDelay);
}
poll();
</script>
</body>
</html>
"""


def _static_page(title: str, body_html: str, active: str = "") -> str:
    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{title} | MAD Platform</title>
{theme.FONT_LINK}
{_BASE_STYLE_LINK}
</head>
<body>
{_SKIP_LINK}
{_site_header(active)}
<main id="main" class="page with-site-header">
  <h1>{title}</h1>
  <div style="line-height:1.6">{body_html}</div>
</main>
{_site_footer()}
</body>
</html>"""


@app.get("/", response_class=HTMLResponse)
async def form_page(request: Request) -> str:
    return _render_form(device_verified=bool(_verified_email(request)))


@app.get("/terms", response_class=HTMLResponse)
async def terms_page() -> str:
    return _static_page(
        "Terms of Service",
        """
        <div class="trust-section-label">Read these three first</div>
        <ol class="trust-list">
          <li><h3>This is not legal advice, and it never will be</h3>
            <p>MAD Platform is an automated scanning tool. It looks for patterns that commonly
            indicate WCAG accessibility issues and estimates their real-world risk. It does not
            perform a legal review, does not guarantee compliance with any law or standard, and
            a clean scan is not proof you're free of legal exposure. Think of it as a smoke
            detector, not a fire inspector: it's built to catch what it can catch, reliably and
            for free, not to certify anything. If accessibility compliance carries real legal or
            financial stakes for your business, that's exactly the point where you bring in a
            qualified attorney, not this tool.</p></li>

          <li><h3>Who operates this, and who doesn't</h3>
            <p>An independent, open-source project (AGPL-3.0 licensed), run by one person, not a
            company: no support team, no legal department, just the person running it and the
            public code doing the work:
            <a href="https://github.com/pandayv/mad-platform-community" target="_blank" rel="noopener">github.com/pandayv/mad-platform-community</a>.
            That's also where to raise an issue or ask a question about how this operates, or
            email <a href="mailto:hello@mad-platform.org">hello@mad-platform.org</a> directly.
            Nothing here is, or should be read as, the output of a company with legal counsel on
            staff.</p></li>

          <li><h3>You use this at your own risk</h3>
            <p>This tool is provided free, "as is" and "as available," with no warranty of any
            kind. To the fullest extent permitted by law, the operator isn't liable for any
            damages, direct or indirect, arising from your use of it or reliance on its results,
            including lost business, legal costs, or any claim related to web accessibility. By
            using this tool, you agree that any decision you make based on its output is yours
            alone, and you won't hold the operator responsible for that decision.</p></li>
        </ol>

        <div class="trust-section-label">The rest, for completeness</div>
        <ol class="trust-list" style="counter-reset: trust-item 3">
          <li><h3>No contract, no obligation</h3>
            <p>Nothing here creates a binding agreement between you and the operator, and using
            this tool doesn't obligate either of you to anything beyond what's written on this
            page. There's no service-level commitment and no ongoing duty to keep this running.
            There's no negotiation on offer, either: these terms are take-it-or-leave-it. If
            that doesn't work for you, don't use the tool. If it does, using it means you accept
            these terms exactly as written.</p></li>

          <li><h3>No warranty, no guaranteed uptime</h3>
            <p>Provided as-is, with no warranty of any kind, express or implied, including
            accuracy, completeness, or fitness for a particular purpose. Automated scans can miss
            real issues and can flag things that aren't real issues. This is a self-funded,
            one-person project with no SLA: it may be slow, may be temporarily unavailable, or
            may change or shut down without notice. Free tools built and run by one person come
            with that tradeoff; it's the honest deal being offered here.</p></li>

          <li><h3>Fair use</h3>
            <p>This is a free, self-serve tool intended for scanning websites you own or are
            authorized to scan. It's rate-limited and gated by a one-time email verification.
            Attempts to bypass either, automated abuse, or using the scan endpoint for anything
            other than its intended purpose is not permitted, and may get your access blocked
            without warning.</p></li>

          <li><h3>Governing law</h3>
            <p>These terms are governed by the laws of the State of Texas, USA, without regard
            to its conflict-of-law principles. If any part of these terms turns out to be
            unenforceable, the rest still stands.</p></li>

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
        <p>Short version: we collect the minimum needed to run your scan and get you the
        report, we never sell it, and you can ask to have it deleted whenever you want. The
        long version is below, but that's the whole policy in one sentence.</p>

        <p><strong>What we collect:</strong> the website URL you submit, the email address you
        verify, the scan results (findings, severity, suggested fixes), and, if you choose to
        leave one, your feedback on whether the report was helpful. When you verify your email,
        we briefly store a 6-digit code against it, and once verified, a random token (not your
        email itself) in a cookie on your own browser so you're not asked to re-verify on your
        next scan.</p>

        <p><strong>Why we collect it:</strong> most importantly, a verified email is how we make
        sure this free tool is actually helping real people, not being drained by bots or
        scammers running up a scan bill on our end for nothing. Confirming it with a one-time
        code, rather than just taking your word for it, is what makes that verification real:
        anyone can type an address; a code sent to that inbox is what proves someone's actually
        there to receive it. A couple of quieter, invisible checks run alongside it too
        (confirming a submission wasn't a script), before a scan is even queued. Beyond
        anti-abuse, the email is also how your report reaches you and how the per-scan review
        link is scoped to you specifically, so no one else who uses this tool can see your
        findings. The IP address of each request is used briefly for rate limiting, the same
        reason any free public tool has to, to keep it usable and not overwhelmed.</p>

        <p><strong>How long we keep the technical bookkeeping:</strong> this is enforced
        automatically by the database itself (a Firestore TTL policy, for anyone checking), not
        just written here as a promise. Verification codes are gone within about an hour of being
        issued, whether or not you used them. Rate-limiting counters expire within a few days. The
        "remember this device" token expires after 30 days, after which you'll verify again. None
        of this needs any action from you. It deletes itself on schedule.</p>

        <p><strong>How long we keep your scan itself:</strong> a scan record (the URL you
        submitted, the findings, and the email address the report went to) is kept so your report
        link and your review link keep working when you come back to them, and so you can compare
        a re-scan later. We keep these for up to 12 months and then remove them. That window is
        longer than the bookkeeping above because the record is the thing you actually came here
        for; if you'd rather it went sooner, just ask (see "Your control" below) and it's
        deleted.</p>

        <p><strong>Where it lives:</strong> on Google Cloud infrastructure (Firestore and Cloud
        Storage), in a project separate from any other project the operator runs. Report emails
        and verification codes are sent via Resend, and if you email us, that lands in a Zoho
        mailbox. Both are the operator's own accounts, not shared with anyone else.</p>

        <p><strong>What we don't do:</strong> we don't sell your data, we don't use it for
        advertising, and we don't share it with anyone outside of what's strictly needed to run
        the scan itself (Google Cloud's AI models, used to analyze your site's public-facing
        pages) or deliver it to you (Resend, for email).</p>

        <p><strong>Your control:</strong> to request deletion of your scan history or email
        address before its automatic expiry, email <a href="mailto:hello@mad-platform.org">hello@mad-platform.org</a>
        directly. No form to fill out, no waiting period, just ask. Feedback marked "okay to use
        as a public testimonial" may be shared publicly; anything not marked that way stays
        private, full stop.</p>
        """,
        active="/privacy",
    )


@app.get("/faq", response_class=HTMLResponse)
async def faq_page() -> str:
    return _static_page(
        "Frequently Asked Questions",
        """
        <p class="tagline" style="margin-top:0">The short version: MAD Platform finds and
        explains accessibility problems in easy-to-understand language, and gives you a
        recommended fix for each one. It doesn't touch your code, and it isn't a law firm. More
        details below.</p>

        <div class="trust-section-label">What this is</div>
        <ol class="trust-list">
          <li><h3>What does this tool actually do?</h3>
            <p>It scans the pages on your site that carry the most real risk, checks them with
            both rule-based and AI-assisted review, independently confirms every finding before
            it's ever shown to you, and gives you a concrete fix for each one, plus a
            downloadable checklist you can hand straight to whoever fixes your
            site. "MAD" is short for Multi-Agent Defense Platform: one agent decides what to
            check, one finds issues, one independently confirms them, one takes action. Not a
            single model skimming your site once and guessing.</p></li>

          <li><h3>What doesn't it do?</h3>
            <p>It won't replace a full legal audit or genuine assistive-technology testing by a
            real user. Some of what the standard asks for is a judgment call, not a strict
            pass/fail, and no automated tool can fully substitute for that. What it does do is go
            well beyond a typical scanner: it reviews actual screenshots and video content, not
            just code, and every finding is independently verified before it ever reaches you. It
            tells you exactly what to fix and how. It doesn't touch your code directly: that
            would mean access to your site's actual codebase, which this tool deliberately never
            asks for.</p></li>

          <li><h3>Is this actually free? What's the catch?</h3>
            <p>No catch, and there isn't a paid tier waiting behind a paywall. This is a
            self-funded community project, not a lead-generation funnel in disguise. Nobody's
            selling your contact info to an accessibility consultant after you scan.</p></li>
        </ol>

        <div class="trust-section-label">Why this exists</div>
        <ol class="trust-list" style="counter-reset: trust-item 3">
          <li><h3>Why does this exist?</h3>
            <p>To make the internet a little more usable for everyone. This free tool exists so a
            small business finds out about an accessibility gap from a proactive scan, not a
            demand letter, and can fix it before it becomes a legal problem. Every page fixed this way is one
            more page a screen-reader user, a keyboard-only user, or someone with low vision can
            actually get through.</p></li>

          <li><h3>What is WCAG?</h3>
            <p>Short for the Web Content Accessibility Guidelines: think of it as the building
            code for websites, the same idea as a wheelchair ramp next to a curb, just for the
            web. It's the standard nearly every digital accessibility law and lawsuit points back
            to. If your site doesn't meet it, that's the gap that shows up in a demand letter.
            This tool checks your site against it, so you find out from a scan instead.</p></li>

          <li><h3>Does it work with WordPress, Shopify, Wix, or Squarespace?</h3>
            <p>Yes. It scans your live site the same way a visitor's browser does, so it works
            regardless of what platform built it, whether that's WordPress, Shopify, Wix,
            Squarespace, or something custom.</p></li>
        </ol>

        <div class="trust-section-label">Trust &amp; privacy</div>
        <ol class="trust-list" style="counter-reset: trust-item 6">
          <li><h3>Can I really trust an automated tool with something this important?</h3>
            <p>Yes. Here's why: every finding goes through an independent verification step
            before it's ever shown to you, and anything that doesn't hold up gets dropped, not
            left in as a maybe. The rules themselves are checked against the real accessibility
            standard, not an AI's unaided memory of it, so it can't misquote a rule or make one
            up. And since this is open source, you don't have to take any of that on faith. You
            can read exactly how it works.</p></li>

          <li><h3>Why do you need my email, and why a verification code?</h3>
            <p>Mainly to keep this a tool for real people, not something bots quietly drain for
            free. A scan costs real money to run even when it's free to you, and a code is what
            proves an address is real, not just typed in. It also delivers your report and makes
            sure only you can see your own findings, through a private link nobody else can
            access. Verify once, and this browser remembers you for 30 days. Full detail in the
            <a href="/privacy">privacy policy</a>.</p></li>

          <li><h3>I need real legal help, not just a scan.</h3>
            <p>Here's exactly where the line sits: this scan tells you what's technically wrong
            and how severe it is. It can't tell you your specific legal exposure; that's what a
            qualified accessibility or ADA attorney is for. What it gives you is the evidence to
            walk into that conversation already prepared, not starting from zero.</p></li>
        </ol>

        <div class="trust-section-label">About the project</div>
        <ol class="trust-list" style="counter-reset: trust-item 9">
          <li><h3>Who's actually behind this?</h3>
            <p>An independent, open-source project, not a company; built and maintained in the
            open by one person. The code running this exact site is public:
            <a href="https://github.com/pandayv/mad-platform-community" target="_blank" rel="noopener">github.com/pandayv/mad-platform-community</a>.
            That's not a marketing claim. You can read exactly what it does with your URL and
            your email before you ever submit either, line by line.</p></li>

          <li><h3>How do I actually reach someone?</h3>
            <p>Email <a href="mailto:hello@mad-platform.org">hello@mad-platform.org</a>: questions,
            bug reports, deletion requests, or just feedback on whether this was useful. A real
            person reads it, not a ticket queue.</p></li>

          <li><h3>How can I support this project?</h3>
            <p>You can support the project and the community in one or more ways:</p>
            <ul style="margin:8px 0 0;padding-left:20px;line-height:1.9">
              <li><a href="https://github.com/pandayv/mad-platform-community" target="_blank" rel="noopener">Contribute</a>
              to the project on GitHub.</li>
              <li><a href="mailto:hello@mad-platform.org">Provide a testimonial or feedback</a>,
              even a sentence.</li>
              <li>Spread the word: share it with friends, family, and on social media.</li>
              <li><a href="https://buymeacoffee.com/madplatform" target="_blank" rel="noopener">Donate</a>
              to help cover infrastructure costs.</li>
            </ul></li>
        </ol>
        """,
        active="/faq",
    )


def _honeypot_or_timing_error(website: str, form_ts: str) -> bool:
    """True if this submission trips the honeypot or the minimum-fill-time
    check -- shared by every form in the URL -> email -> code funnel, since
    a bot can hit any of these routes directly without ever loading the
    page before it.
    """
    if website.strip():
        return True
    try:
        return time.time() - float(form_ts) < abuse_guard.MIN_FORM_FILL_SECONDS
    except ValueError:
        return True


async def _safe_url_or_error(url: str) -> tuple[str | None, str | None]:
    """Validates and normalizes a submitted URL. Returns (url, None) on
    success or (None, error_message) on failure -- run at every step that
    carries a URL forward (not just the first), since it arrives as a
    plain form/query value each time and nothing stops it being tampered
    with between steps.

    The url field is deliberately type="text", not type="url" -- a bare
    domain like "cahm.org" typed without a scheme is exactly how most
    non-technical visitors actually type a website address (with or
    without "www.", and whether or not they're pointing at a specific
    page like "cahm.org/about"), and type="url"'s native browser
    validation rejects that outright before the form is ever submitted
    (a real bug: it showed a generic "enter a URL" browser tooltip on a
    perfectly real address).

    "://" presence, not urlsplit(url).scheme, decides whether a scheme is
    missing -- urlsplit alone is ambiguous on a bare "host:port" with no
    scheme (urlsplit("cahm.org:8080").scheme comes back "cahm.org", not
    ""; RFC 3986 genuinely can't tell "scheme:opaque" from "host:port"
    without more context), which would wrongly reject a bare domain that
    happens to specify a port. When a scheme prefix IS present, the whole
    original string is used completely untouched apart from that -- never
    parsed apart and reconstructed -- so a path/query/fragment
    ("cahm.org/about/team?x=1#y") survives exactly as typed either way.
    """
    url = url.strip()
    if url and "://" not in url:
        url = f"https://{url}"
    elif url:
        scheme = urlsplit(url).scheme.lower()
        if scheme not in ("http", "https"):
            return None, "Please enter an http:// or https:// website URL."
    try:
        await asyncio.wait_for(asyncio.to_thread(assert_safe_target, url), timeout=3.0)
    except UnsafeTargetError:
        return None, "Please enter a public website URL we can actually reach."
    except asyncio.TimeoutError:
        return None, "We couldn't verify that URL in time. Please double-check it and try again."
    return url, None


async def _start_scan(request: Request, url: str, email: str) -> Response:
    """Common tail of the funnel, reached two ways: a returning visitor
    whose device is already verified (skips straight here from
    POST /scan/start), or a first-time visitor right after a correct code
    (POST /scan/verify-code). Same quota check either path -- verification
    proves an inbox is real, it isn't a bypass for the scan budget.
    """
    client_ip = request.client.host if request.client else "unknown"
    allowed, reason = fs.check_and_reserve_scan_quota(email, client_ip)
    if not allowed:
        return HTMLResponse(_render_form(error=reason), status_code=429)
    job_id = fs.create_job(url, owner_contact=email, status="queued")

    # The enqueue is the one step here that can fail after quota has
    # already been consumed and a job document already exists. It used to
    # be a bare call: an IAM misconfiguration, a paused queue, a queue
    # depth limit or a transient Cloud Tasks error gave the visitor a raw
    # 500, kept their quota unit (the reservation is deliberately
    # non-refundable by default), and left a `status: "queued"` job that no
    # worker would ever pick up -- whose status page then told them "we'll
    # email your full report as soon as it's ready", a promise nothing
    # would keep.
    try:
        _enqueue_scan(job_id)
    except gcloud_exceptions.AlreadyExists:
        # The task name is the job_id, so this means this exact job is
        # already queued. That is success, not failure -- the dedup guard
        # doing its job.
        logger.info("[%s] Task already queued -- treating as enqueued", job_id)
    except Exception as exc:  # noqa: BLE001 - every failure mode here needs the same cleanup
        logger.exception("[%s] Could not enqueue scan", job_id)
        fs.fail_job(job_id, "We couldn't start this scan. Nothing was charged against your daily limit.")
        fs.refund_scan_quota(email, client_ip)
        return HTMLResponse(
            _render_form(
                error="We couldn't start your scan just now -- this is on our side, not yours. "
                "Please try again in a few minutes; your daily scan allowance hasn't been used."
            ),
            status_code=503,
        )
    return RedirectResponse(f"/status/{job_id}", status_code=303)


@app.post("/scan/start")
async def scan_start(
    request: Request,
    url: str = Form(...),
    website: str = Form(""),
    form_ts: str = Form(""),
    turnstile_token: str = Form("", alias="cf-turnstile-response"),
) -> Response:
    if _honeypot_or_timing_error(website, form_ts):
        return HTMLResponse(_render_form(error="Something went wrong. Please try again."), status_code=400)
    if not await _turnstile_passed(turnstile_token):
        return HTMLResponse(_render_form(error="Please complete the verification and try again."), status_code=400)

    url, error = await _safe_url_or_error(url)
    if error:
        return HTMLResponse(_render_form(error=error), status_code=400)

    email = _verified_email(request)
    if email:
        return await _start_scan(request, url, email)
    return RedirectResponse(f"/scan/email?url={quote(url)}", status_code=303)


@app.get("/scan/email", response_class=HTMLResponse)
async def scan_email_step(url: str) -> str:
    return _render_email_step(url)


@app.post("/scan/request-code")
async def scan_request_code(
    url: str = Form(...),
    email: str = Form(...),
    website: str = Form(""),
    form_ts: str = Form(""),
) -> Response:
    url, url_error = await _safe_url_or_error(url)
    if url_error:
        # Shouldn't normally happen (already checked at /scan/start) unless
        # the hidden url field was tampered with between steps -- back to
        # the top rather than showing a URL error on the email screen.
        return HTMLResponse(_render_form(error=url_error), status_code=400)

    if _honeypot_or_timing_error(website, form_ts):
        return HTMLResponse(_render_email_step(url, error="Something went wrong. Please try again."), status_code=400)

    email = email.strip()
    try:
        email_ok, email_reason = await asyncio.wait_for(
            asyncio.to_thread(abuse_guard.email_looks_valid, email), timeout=3.0
        )
    except asyncio.TimeoutError:
        return HTMLResponse(
            _render_email_step(url, error="We couldn't verify that email domain in time. Please double-check it and try again."),
            status_code=400,
        )
    if not email_ok:
        return HTMLResponse(_render_email_step(url, error=email_reason), status_code=400)

    wait = fs.code_request_cooldown_remaining(email)
    if wait > 0:
        return HTMLResponse(
            _render_email_step(url, error=f"Please wait about {int(wait) + 1} seconds before requesting another code."),
            status_code=429,
        )

    code = fs.generate_email_code(email)
    if not notify.send_verification_code_email(email, code):
        return HTMLResponse(
            _render_email_step(url, error="We couldn't send that code right now. Please try again in a moment."),
            status_code=502,
        )
    return RedirectResponse(f"/scan/verify?url={quote(url)}&email={quote(email)}", status_code=303)


@app.get("/scan/verify", response_class=HTMLResponse)
async def scan_verify_step(url: str, email: str) -> str:
    return _render_code_step(url, email)


@app.post("/scan/verify-code")
async def scan_verify_code(request: Request, url: str = Form(...), email: str = Form(...), code: str = Form(...)) -> Response:
    url, url_error = await _safe_url_or_error(url)
    if url_error:
        return HTMLResponse(_render_form(error=url_error), status_code=400)

    email = email.strip()
    if not fs.verify_email_code(email, code.strip()):
        return HTMLResponse(
            _render_code_step(url, email, error="That code is invalid or has expired. Request a new one below."),
            status_code=400,
        )

    resp = await _start_scan(request, url, email)
    _set_device_cookie(resp, request, email)
    return resp


@app.get("/status/{job_id}", response_class=HTMLResponse)
async def status_page(job_id: str) -> str:
    job = fs.get_job(job_id)
    if job is None:
        raise HTTPException(404, "No such job")
    # _STATUS_PAGE is a plain string, not an f-string -- it can't be one,
    # the page is mostly JavaScript full of literal { } and ${...}. So
    # every interpolation into it has to be a __PLACEHOLDER__ replace, and
    # a stray f-string-style {theme.X} in that template renders to the
    # visitor verbatim rather than raising (which is exactly what the brand
    # mark did here).
    return (
        _STATUS_PAGE.replace("__STYLE_LINK__", _BASE_STYLE_LINK)
        .replace("__FONT_LINK__", theme.FONT_LINK)
        .replace("__BRAND_MARK__", theme.BRAND_MARK)
        .replace("__SEV_ORDER__", json.dumps(list(SEVERITY_ORDER)))
        .replace("__SEV_VAR__", json.dumps(theme.SEVERITY_VAR))
        .replace("__URL__", html.escape(job["url"]))
        .replace("__JOB_ID__", f'"{job_id}"')
    )


@app.get("/api/status/{job_id}")
async def api_status(job_id: str) -> JSONResponse:
    job = fs.get_job(job_id)
    if job is None:
        raise HTTPException(404, "No such job")
    created_at = job.get("created_at")
    started_at = job.get("started_at")
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
            "started_at": started_at.isoformat() if started_at else None,
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


# Feedback input limits. Generous enough that nobody with something real to
# say hits them, small enough that the endpoint is not a place to store
# arbitrary content.
_MIN_RATING = 1
_MAX_RATING = 5
_MAX_FEEDBACK_COMMENT = 2000
_MAX_FEEDBACK_CONTACT = 254  # RFC 5321's maximum email address length


@app.post("/report/{job_id}/feedback")
async def submit_feedback(
    job_id: str,
    token: str = Form(...),
    rating: int = Form(...),
    comment: str = Form(""),
    allow_testimonial: bool = Form(False),
    contact: str = Form("")
) -> JSONResponse:
    """The immediate "was this helpful" prompt shown on the report page and
    in the report email -- asking at the moment the report is delivered
    gets meaningfully better response rates than a delayed follow-up.

    Authorized by the job's own review_token, the same capability that
    already scopes /review/link/... to one scan's owner. This route
    previously required only that the job exist, so anyone holding any
    valid job ID could write unlimited Firestore documents with arbitrary
    content, and `allow_testimonial` is caller-controlled -- so an attacker
    could mark their own text publishable (app.py's privacy page says
    testimonial-flagged feedback may be published). Firestore writes are
    also billed.

    Three further limits, none of which existed: `rating` was coerced to
    int but never bounds-checked (a stored 2**40 skews anything that reads
    it), `comment`/`contact` had no length cap, and one job could be
    submitted against repeatedly.
    """
    if not fs.verify_review_token(job_id, token):
        # Same 404 for a missing job and a wrong token -- matching
        # _scoped_escalation_or_404, so a guessed token cannot be used to
        # confirm that a job ID is real.
        raise HTTPException(404, "Not found")
    if not _MIN_RATING <= rating <= _MAX_RATING:
        raise HTTPException(422, f"rating must be between {_MIN_RATING} and {_MAX_RATING}")
    if len(comment) > _MAX_FEEDBACK_COMMENT or len(contact) > _MAX_FEEDBACK_CONTACT:
        raise HTTPException(422, "Feedback is too long")
    if fs.has_feedback(job_id):
        # Idempotent rather than an error: a double-submit from an impatient
        # click should look like success to the person clicking.
        return JSONResponse({"ok": True, "already_submitted": True})
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
{_BASE_STYLE_LINK}
</head>
<body>
{_SKIP_LINK}
<main id="main" class="page">
  <div class="brand">{theme.BRAND_MARK}MAD Platform</div>
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
</main>
</body>
</html>"""


# The "kb_version_change" branches that used to sit in this renderer, in
# _render_review_detail and in review_resolve are gone: nothing has created
# an escalation of that kind since the WCAG refresh stopped waiting on a
# human gate (DECISIONS_LOG.md). Leaving them in described a workflow the
# system no longer has.
def _review_item_row(e: dict) -> str:
    eid = html.escape(e["id"])
    kind = e.get("kind")
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
{_BASE_STYLE_LINK}
</head>
<body>
{_SKIP_LINK}
<main id="main" class="page wide">
  <div class="brand">{theme.BRAND_MARK}MAD Platform</div>
  <h1>Internal review queue</h1>
  <div class="tagline">{len(pending)} item(s) awaiting disposition.</div>
  <div class="card glass-sheen">{items_html}</div>
</main>
</body>
</html>"""


def _render_review_detail(e: dict, message: str | None = None) -> str:
    eid = e["id"]
    message_html = f'<div class="success-box">{html.escape(message)}</div>' if message else ""

    if e.get("kind") == "learned_pattern":
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
          <div class="field" style="margin-top:10px;color:var(--muted);font-size:12.5px">
            Confirming files a ticket for this finding. Dismissing discards it -- no ticket, ever.
          </div>
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
{_BASE_STYLE_LINK}
</head>
<body>
{_SKIP_LINK}
<main id="main" class="page">
  <div class="brand"><a href="/review" style="color:inherit;text-decoration:none">{theme.BRAND_MARK}MAD Platform · Review Queue</a></div>
  <h1>Review item</h1>
  <div class="card glass-sheen">
    {body}
    <div style="margin-top:20px">{actions}</div>
  </div>
  {message_html}
</main>
</body>
</html>"""


@app.get("/review", response_class=HTMLResponse)
async def review_list(request: Request) -> Response:
    if not _is_reviewer(request):
        return HTMLResponse(_render_review_login())
    return HTMLResponse(_render_review_list(fs.list_pending_escalations()))


@app.post("/review/login")
async def review_login(request: Request, code: str = Form(...)) -> Response:
    review_code = config.review_code()
    if not review_code:
        # Fail closed, matching _is_reviewer: with no code configured there
        # is no way to be authorized, so there is no way to log in either.
        # (Previously `if _REVIEW_CODE and ...` let ANY submitted code
        # through in this state and handed out a session cookie.)
        logger.warning("Review login attempted while MAD_REVIEW_CODE is unset -- denying")
        return HTMLResponse(
            _render_review_login(error="The review queue is not configured on this deployment."),
            status_code=403,
        )
    if not hmac.compare_digest(code, review_code):
        return HTMLResponse(_render_review_login(error="Wrong review code."), status_code=403)
    resp = RedirectResponse("/review", status_code=303)
    resp.set_cookie(
        _REVIEW_COOKIE,
        _review_session_value(review_code),
        httponly=True,
        samesite="lax",
        secure=_is_https(request),
    )
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
    if kind == "learned_pattern":
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
{_BASE_STYLE_LINK}
</head>
<body>
{_SKIP_LINK}
<main id="main" class="page wide">
  <div class="brand">{theme.BRAND_MARK}MAD Platform</div>
  <h1>Your review queue</h1>
  <div class="tagline">Findings from your scan that need a quick judgment call -- only you can see this.</div>
  <div class="card glass-sheen">{items_html}</div>
</main>
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
          <div class="tagline" style="margin:0 0 12px">
            <b>Confirm</b> if this is a real problem on your site: it gets added to your ticket
            list so it actually gets fixed. <b>Dismiss</b> if it isn't (a false positive, or something
            you've decided not to address); no ticket gets created for it.
          </div>
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
{_BASE_STYLE_LINK}
</head>
<body>
{_SKIP_LINK}
<main id="main" class="page">
  <div class="brand"><a href="/review/link/{job_id}/{token}" style="color:inherit;text-decoration:none">{theme.BRAND_MARK}MAD Platform · Your Review Queue</a></div>
  <h1>Review item</h1>
  <div class="card glass-sheen">
    {body}
    <div style="margin-top:20px">{actions}</div>
  </div>
  {message_html}
</main>
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
