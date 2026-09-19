"""Crawler tool: fetches a page and captures its rendered HTML + a screenshot.

Deterministic tool, not an LLM agent — Analyst calls this directly. No
judgment happens here.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass

from playwright.async_api import async_playwright

from mad_platform.tools import retry
from mad_platform.tools.url_safety import assert_safe_target, is_safe_target_async

logger = logging.getLogger("mad_platform.crawler")


class FetchError(Exception):
    """Raised when a page could not be fetched after retries."""


@dataclass
class PageSnapshot:
    url: str
    html: str
    screenshot_png: bytes
    title: str
    text_style_samples: list[dict]


# Walks visible, text-bearing elements and records computed color/background
# — contrast checking needs actual rendered styles, not raw HTML/CSS, since
# color can come from a stylesheet, inline style, or an inherited ancestor.
# Resolves the effective background by walking up through parents past any
# transparent layers, defaulting to white (the common web default) if none
# is found, since CSS itself has no concept of "the final background".
_STYLE_SNAPSHOT_JS = """
() => {
  function getEffectiveBackground(el) {
    let node = el;
    while (node) {
      const bg = getComputedStyle(node).backgroundColor;
      if (bg && bg !== 'rgba(0, 0, 0, 0)' && bg !== 'transparent') {
        return bg;
      }
      node = node.parentElement;
    }
    return 'rgb(255, 255, 255)';
  }
  const results = [];
  const elements = document.body.querySelectorAll('*');
  for (const el of elements) {
    const directText = Array.from(el.childNodes)
      .filter(n => n.nodeType === Node.TEXT_NODE)
      .map(n => n.textContent.trim())
      .join(' ')
      .trim();
    if (!directText) continue;
    const rect = el.getBoundingClientRect();
    if (rect.width === 0 || rect.height === 0) continue;
    const style = getComputedStyle(el);
    if (style.visibility === 'hidden' || style.display === 'none') continue;
    results.push({
      tag: el.tagName.toLowerCase(),
      text: directText.slice(0, 80),
      color: style.color,
      backgroundColor: getEffectiveBackground(el),
      fontSizePx: parseFloat(style.fontSize),
      fontWeight: style.fontWeight,
    });
  }
  return results;
}
"""

# Marks every currently-not-rendered element with data-mad-hidden="true"
# directly in the DOM, before HTML is captured -- so it travels with the
# element into the HTML string every downstream check and Editor read
# (rule-based, AI-based, or Editor's own verification) already consumes,
# with no extra plumbing needed. Confirmed by hand against a real false
# positive: a closed lightbox modal (display:none) was flagged as an
# active keyboard-trap because nothing told the checks it wasn't actually
# on screen -- same display:none/visibility:hidden/zero-size test already
# proven correct in _STYLE_SNAPSHOT_JS above, just applied broadly instead
# of only to text nodes. Does NOT catch off-screen positioning (e.g.
# left:-9999px) -- that's a real, separate gap, not addressed here.
_MARK_HIDDEN_JS = """
() => {
  document.querySelectorAll('*').forEach(el => {
    const style = getComputedStyle(el);
    const rect = el.getBoundingClientRect();
    if (style.display === 'none' || style.visibility === 'hidden' || rect.width === 0 || rect.height === 0) {
      el.setAttribute('data-mad-hidden', 'true');
    }
  });
}
"""


async def guard_page_requests(page) -> None:
    """Runs the SSRF guard on every request this page makes, not just the
    URL we were handed.

    `assert_safe_target(url)` at the top of fetch_page only ever saw the
    submitted URL. Playwright follows redirects itself, so a public host
    could 302 to http://10.0.0.5/ and be fetched without the guard running
    again; and the rendered page's own scripts, images, iframes and
    fetch() calls never passed through the guard at all. Intercepting at
    the request level closes redirects, subresources and (partly) DNS
    rebinding in one place, instead of adding a separate check to each.

    Two deliberate failure directions:

    - An *unsafe target* aborts that one request. The page keeps loading;
      we want the report to reflect what a visitor sees, and a site whose
      analytics beacon points at a private address should still be
      scanned.
    - A *bug in this handler* continues the request rather than aborting.
      An exception raised inside a route handler leaves the request
      hanging until the navigation timeout, which would turn a defect here
      into "every scan fails". The guard is defense in depth on top of
      fetch_page's own up-front check, not the only thing standing between
      us and a private address.

    DNS resolution is blocking, so it runs in a thread; url_safety caches
    per host, so a page pulling fifty images from one CDN resolves once.
    """

    async def _route(route, request):
        try:
            # The dedicated DNS pool, not the default executor -- see
            # url_safety's comment. This is the highest-volume caller
            # (every subresource on every page), so it is also the one
            # most able to starve unrelated to_thread work.
            safe = await is_safe_target_async(request.url)
        except Exception:  # noqa: BLE001 - see docstring: a handler bug must not hang the page
            logger.warning("Request guard errored for %s -- allowing", request.url, exc_info=True)
            safe = True
        try:
            if safe:
                await route.continue_()
            else:
                logger.warning("Blocked request to a private/link-local target: %s", request.url)
                await route.abort()
        except Exception:  # noqa: BLE001 - the page navigated away or closed mid-flight
            # Routes still in flight when a page closes or navigates raise
            # from continue_()/abort(). That is normal teardown, not a scan
            # failure, and it must not propagate into fetch_page's retry.
            logger.debug("Route for %s could not be completed (page moved on)", request.url)

    await page.route("**/*", _route)


# How tall a screenshot may be, in CSS pixels.
#
# `page.screenshot(full_page=True)` had no cap, and the bytes go straight
# into two Gemini requests per page (ai_checks.run_visual_check and
# editor.verify_findings). An infinite-scroll listing or a long-form
# article can render tens of thousands of pixels tall, which is a memory
# spike on the memory-constrained worker, a large upload twice over, and a
# plausible 400 INVALID_ARGUMENT from the model -- which retry.classify
# (correctly) treats as FAIL_FAST, so the scan would die on a page that is
# merely long. Every other input on this path is bounded
# (untrusted.HTML_EXCERPT_CHARS, the navigation timeout, the model
# timeout); this one was not.
#
# 8000px is roughly ten viewports: generous enough that a normal marketing
# or content page is captured whole, and a contrast / focus-indicator
# review does not need 30,000px of footer below that.
MAX_SCREENSHOT_HEIGHT_PX = 8000


async def _capture_screenshot(page) -> bytes:
    """A full-page screenshot, clipped to MAX_SCREENSHOT_HEIGHT_PX.

    `full_page=True` *and* `clip`, not `clip` alone. Verified against a
    33,288px-tall test page rather than assumed: `clip` on its own is
    bounded by the viewport, so asking for an 8000px clip produced a
    720px image -- a cap that silently threw away almost the whole page
    instead of bounding it. With both, the clip is applied to the full
    scrollable area and the result is genuinely 8000px tall.

    A page shorter than the cap takes the plain full-page path, so the
    common case is unchanged.
    """
    metrics = await page.evaluate(
        "() => ({w: document.documentElement.scrollWidth,"
        "       h: document.documentElement.scrollHeight})"
    )
    width = max(1, int(metrics["w"]))
    height = max(1, int(metrics["h"]))
    if height <= MAX_SCREENSHOT_HEIGHT_PX:
        return await page.screenshot(full_page=True)
    logger.info(
        "%s is %dpx tall -- clipping the screenshot to %dpx",
        page.url, height, MAX_SCREENSHOT_HEIGHT_PX,
    )
    return await page.screenshot(
        full_page=True,
        clip={"x": 0, "y": 0, "width": width, "height": MAX_SCREENSHOT_HEIGHT_PX},
    )


# One browser per process, not one per fetch attempt.
#
# `async_playwright()` and `chromium.launch()` used to live inside the
# retry closure, so every attempt started and tore down the Playwright
# driver process and a whole browser: `_process_page` can run
# `_run_analysis_pass` twice (the retry gate) across up to three pages,
# with retries=2 on each fetch, so a worst-case scan paid for six cold
# Chromium launches and a typical one for three. Each costs roughly a
# second of wall time out of the 2-3 minutes the status page promises,
# plus an RSS spike on a 1Gi containerConcurrency=1 instance.
#
# A fresh `new_context()` per fetch gives the same isolation between
# target sites that separate launches did -- separate cookie jar, cache,
# storage and permissions -- at a fraction of the cost.
# `guard_page_requests` attaches per-page and is unaffected.
_browser_lock = asyncio.Lock()
_browser_state: dict = {"playwright": None, "browser": None, "loop": None}


async def _get_browser():
    """The shared browser, launched on first use and relaunched if it has
    gone away.

    The connection check is load-bearing, not defensive dressing: a
    Chromium that crashed or was OOM-killed leaves a disconnected handle,
    and without this every subsequent fetch in the process would fail
    against it. The loop check covers the other way this can go stale --
    a Playwright object is bound to the event loop that created it, so a
    cached one from a finished `asyncio.run()` is unusable.
    """
    loop = asyncio.get_running_loop()
    async with _browser_lock:
        browser = _browser_state["browser"]
        if browser is not None and browser.is_connected() and _browser_state["loop"] is loop:
            return browser
        await _shutdown_browser_locked()
        playwright = await async_playwright().start()
        _browser_state["playwright"] = playwright
        _browser_state["browser"] = await playwright.chromium.launch()
        _browser_state["loop"] = loop
        logger.info("Launched a shared Chromium instance for this process")
        return _browser_state["browser"]


async def _shutdown_browser_locked() -> None:
    for key, closer in (("browser", "close"), ("playwright", "stop")):
        resource = _browser_state[key]
        _browser_state[key] = None
        if resource is None:
            continue
        try:
            await getattr(resource, closer)()
        except Exception:  # noqa: BLE001 - a dead browser is what we are cleaning up
            logger.debug("Ignoring error while closing the shared %s", key, exc_info=True)
    _browser_state["loop"] = None


async def shutdown_browser() -> None:
    """Closes the shared browser. For a caller that wants to reclaim the
    memory deliberately (a test, a shutdown hook); not required, since the
    process exiting closes it either way.
    """
    async with _browser_lock:
        await _shutdown_browser_locked()


async def fetch_page(url: str, timeout_ms: int = 15000, retries: int = 2) -> PageSnapshot:
    """Render a page with a real browser and capture its HTML + a full-page screenshot.

    Retries transient failures (timeout, navigation errors) with a short
    backoff — a single flaky load must not fail the whole page, let alone
    the whole cycle.
    """
    assert_safe_target(url)

    async def _attempt() -> PageSnapshot:
        browser = await _get_browser()
        # A fresh context per attempt: its own cookie jar, cache, storage
        # and permissions, so one scanned site can never see another's
        # state. That is the isolation the per-fetch browser launch was
        # buying, at a fraction of the cost.
        context = await browser.new_context()
        try:
            page = await context.new_page()
            await guard_page_requests(page)
            # "networkidle" is a known Playwright pitfall for real-world
            # sites: any persistent connection (a chat widget, an
            # analytics beacon, a websocket) means the page never goes
            # fully idle, so it doesn't "eventually settle" -- it fails
            # the same way on every retry. Confirmed live: a real small-
            # business site (ladawnsbeauty.com) failed all 3 attempts
            # this way. "load" plus a short explicit settle window
            # catches JS-rendered content without waiting on background
            # chatter that may never stop.
            await page.goto(url, timeout=timeout_ms, wait_until="load")
            await page.wait_for_timeout(1500)
            await page.evaluate(_MARK_HIDDEN_JS)
            html = await page.content()
            title = await page.title()
            screenshot = await _capture_screenshot(page)
            style_samples = await page.evaluate(_STYLE_SNAPSHOT_JS)
            return PageSnapshot(
                url=url,
                html=html,
                screenshot_png=screenshot,
                title=title,
                text_style_samples=style_samples,
            )
        finally:
            # The context, not the browser -- closing the browser here is
            # what made every attempt pay for a cold launch.
            await context.close()

    # Shared retry policy (tools/retry.py). Two behaviours changed here:
    # every intermediate failure is now logged rather than silently
    # discarded -- three timeouts and three DNS failures produced the same
    # final message before, and an operator could not tell them apart --
    # and a failure that provably cannot succeed on a retry (an unsafe
    # redirect target, say) stops immediately instead of costing two more
    # browser launches.
    try:
        return await retry.with_retry_async(_attempt, attempts=retries + 1, label=f"fetch_page({url})")
    except Exception as exc:  # noqa: BLE001 - re-wrapped as this module's own error type
        raise FetchError(f"Failed to fetch {url!r} after {retries + 1} attempts: {exc}") from exc


# fetch_page_sync() used to sit here: an asyncio.run() wrapper with no
# callers. Every entry point into this pipeline is already async (the
# worker's FastAPI handler, run_scan.py's asyncio.run at the top), so a
# sync wrapper could only ever be called from inside a running loop, where
# asyncio.run() raises. It read as a supported alternative and was not one.
