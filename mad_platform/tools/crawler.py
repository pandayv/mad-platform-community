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
from mad_platform.tools.url_safety import assert_safe_target, is_safe_target

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
            safe = await asyncio.to_thread(is_safe_target, request.url)
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


async def fetch_page(url: str, timeout_ms: int = 15000, retries: int = 2) -> PageSnapshot:
    """Render a page with a real browser and capture its HTML + a full-page screenshot.

    Retries transient failures (timeout, navigation errors) with a short
    backoff — a single flaky load must not fail the whole page, let alone
    the whole cycle.
    """
    assert_safe_target(url)

    async def _attempt() -> PageSnapshot:
        async with async_playwright() as p:
            browser = await p.chromium.launch()
            try:
                page = await browser.new_page()
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
                screenshot = await page.screenshot(full_page=True)
                style_samples = await page.evaluate(_STYLE_SNAPSHOT_JS)
                return PageSnapshot(
                    url=url,
                    html=html,
                    screenshot_png=screenshot,
                    title=title,
                    text_style_samples=style_samples,
                )
            finally:
                await browser.close()

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


def fetch_page_sync(url: str, timeout_ms: int = 15000, retries: int = 2) -> PageSnapshot:
    return asyncio.run(fetch_page(url, timeout_ms, retries))
