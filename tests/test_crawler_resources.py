"""B8 / B9: the crawler's two unbounded resources.

Neither is a correctness bug on its own; both compound on the same
1Gi, containerConcurrency=1 instance that adk_client's session leak was
also filling.

- A fresh Playwright driver and a whole Chromium were launched *inside*
  the retry closure, so every fetch attempt paid for a cold launch.
  `_process_page` can run `_run_analysis_pass` twice (the retry gate)
  across up to three pages with retries=2 on each fetch, so a worst-case
  scan paid for six.
- `page.screenshot(full_page=True)` had no height cap, and the bytes go
  straight into two Gemini requests per page. A long page is a memory
  spike, a large upload twice over, and a plausible 400 INVALID_ARGUMENT
  -- which retry.classify (correctly) treats as FAIL_FAST, so the scan
  would die on a page that is merely long.

These run against fakes rather than a real browser: the suite deliberately
needs no Chromium. Both fixes were also exercised against a real Chromium
and a local 33,288px-tall page while being written -- see
CODE_REVIEW_FIXES.md for the measurements.
"""

from __future__ import annotations

import asyncio
import struct

import pytest

from mad_platform.tools import crawler


@pytest.fixture(autouse=True)
def clean_browser_state():
    crawler._browser_state.update({"playwright": None, "browser": None, "loop": None})
    yield
    crawler._browser_state.update({"playwright": None, "browser": None, "loop": None})


class _FakeBrowser:
    def __init__(self):
        self.connected = True
        self.contexts_made = 0
        self.closed = False

    def is_connected(self):
        return self.connected

    async def new_context(self):
        self.contexts_made += 1
        return _FakeContext()

    async def close(self):
        self.closed = True
        self.connected = False


class _FakeContext:
    def __init__(self):
        self.closed = False

    async def close(self):
        self.closed = True


class _FakeChromium:
    def __init__(self, browsers):
        self._browsers = browsers
        self.launches = 0

    async def launch(self):
        self.launches += 1
        return self._browsers[min(self.launches - 1, len(self._browsers) - 1)]


class _FakePlaywright:
    def __init__(self, browsers):
        self.chromium = _FakeChromium(browsers)
        self.stopped = False

    async def start(self):
        return self

    async def stop(self):
        self.stopped = True


@pytest.fixture
def fake_playwright(monkeypatch):
    browsers = [_FakeBrowser(), _FakeBrowser()]
    fake = _FakePlaywright(browsers)
    monkeypatch.setattr(crawler, "async_playwright", lambda: fake)
    return fake, browsers


# --- B8: one browser per process, one context per fetch -----------------


def test_the_browser_is_launched_once_and_reused(fake_playwright):
    fake, _browsers = fake_playwright

    async def _run():
        first = await crawler._get_browser()
        second = await crawler._get_browser()
        third = await crawler._get_browser()
        return first is second is third

    assert asyncio.run(_run()) is True
    assert fake.chromium.launches == 1


def test_a_disconnected_browser_is_relaunched(fake_playwright):
    """A Chromium that crashed or was OOM-killed leaves a disconnected
    handle. Without this check, every subsequent fetch in the process
    would fail against it -- which is worse than the launch cost this
    change removes.
    """
    fake, browsers = fake_playwright

    async def _run():
        first = await crawler._get_browser()
        first.connected = False  # simulate the crash
        second = await crawler._get_browser()
        return first, second

    first, second = asyncio.run(_run())
    assert second is not first
    assert fake.chromium.launches == 2


def test_a_browser_cached_from_a_finished_event_loop_is_not_reused(fake_playwright):
    """A Playwright object is bound to the loop that created it, so a
    handle left over from a finished asyncio.run() is unusable.
    """
    fake, _browsers = fake_playwright
    asyncio.run(crawler._get_browser())
    asyncio.run(crawler._get_browser())  # a different loop
    assert fake.chromium.launches == 2


def test_concurrent_callers_do_not_race_into_two_launches(fake_playwright):
    fake, _browsers = fake_playwright

    async def _run():
        return await asyncio.gather(*(crawler._get_browser() for _ in range(8)))

    results = asyncio.run(_run())
    assert len({id(b) for b in results}) == 1
    assert fake.chromium.launches == 1


def test_shutdown_closes_the_browser_and_clears_the_cache(fake_playwright):
    fake, browsers = fake_playwright

    async def _run():
        browser = await crawler._get_browser()
        await crawler.shutdown_browser()
        return browser

    browser = asyncio.run(_run())
    assert browser.closed is True
    assert fake.stopped is True
    assert crawler._browser_state["browser"] is None


def test_shutdown_survives_an_already_dead_browser(fake_playwright):
    """It is cleaning up after a crash; raising here would mask it."""
    _fake, browsers = fake_playwright

    async def _boom():
        raise RuntimeError("the browser is already gone")

    async def _run():
        browser = await crawler._get_browser()
        browser.close = _boom
        await crawler.shutdown_browser()

    asyncio.run(_run())
    assert crawler._browser_state["browser"] is None


def test_the_fetch_path_closes_the_context_not_the_browser():
    """Closing the browser per attempt is exactly what made every attempt
    pay for a cold launch. The context is what has to go.
    """
    import inspect

    source = inspect.getsource(crawler.fetch_page)
    assert "await context.close()" in source
    assert "await browser.close()" not in source
    assert "browser.new_context()" in source


def test_the_browser_is_no_longer_launched_inside_the_retry_closure():
    """The shape, not just the behaviour: if `chromium.launch()` moves
    back inside `_attempt`, the cost comes straight back with it.
    """
    import inspect

    source = inspect.getsource(crawler.fetch_page)
    assert "chromium.launch" not in source
    assert "async_playwright()" not in source


# --- B9: the screenshot has a height cap --------------------------------


class _FakePage:
    """Just enough of a Playwright page for _capture_screenshot."""

    def __init__(self, width: int, height: int):
        self._w, self._h = width, height
        self.url = "https://example.com/"
        self.calls: list[dict] = []

    async def evaluate(self, _script):
        return {"w": self._w, "h": self._h}

    async def screenshot(self, **kwargs):
        self.calls.append(kwargs)
        height = kwargs.get("clip", {}).get("height", self._h)
        return _png_header(self._w, int(height))


def _png_header(width: int, height: int) -> bytes:
    return b"\x89PNG\r\n\x1a\n" + b"\x00" * 8 + struct.pack(">II", width, height)


def test_a_normal_page_is_captured_whole_with_no_clip():
    page = _FakePage(1280, 3000)
    asyncio.run(crawler._capture_screenshot(page))
    assert page.calls == [{"full_page": True}]


def test_a_page_exactly_at_the_cap_is_still_not_clipped():
    page = _FakePage(1280, crawler.MAX_SCREENSHOT_HEIGHT_PX)
    asyncio.run(crawler._capture_screenshot(page))
    assert "clip" not in page.calls[0]


def test_a_very_long_page_is_clipped_to_the_cap():
    page = _FakePage(1280, 33288)
    data = asyncio.run(crawler._capture_screenshot(page))
    clip = page.calls[0]["clip"]
    assert clip["height"] == crawler.MAX_SCREENSHOT_HEIGHT_PX
    assert clip["width"] == 1280
    assert struct.unpack(">II", data[16:24])[1] == crawler.MAX_SCREENSHOT_HEIGHT_PX


def test_the_clip_is_paired_with_full_page():
    """Verified against a real browser: `clip` alone is bounded by the
    viewport, so an 8000px clip produced a 720px image -- a cap that threw
    away almost the whole page instead of bounding it.
    """
    page = _FakePage(1280, 33288)
    asyncio.run(crawler._capture_screenshot(page))
    assert page.calls[0]["full_page"] is True


def test_a_clipped_capture_is_logged(caplog):
    """A silently truncated screenshot is indistinguishable from a short
    page in the findings it produces.
    """
    import logging

    page = _FakePage(1280, 33288)
    with caplog.at_level(logging.INFO, logger="mad_platform.crawler"):
        asyncio.run(crawler._capture_screenshot(page))
    message = " ".join(r.getMessage() for r in caplog.records)
    assert "33288" in message
    assert str(crawler.MAX_SCREENSHOT_HEIGHT_PX) in message


def test_a_zero_height_page_does_not_produce_an_invalid_clip():
    page = _FakePage(0, 0)
    asyncio.run(crawler._capture_screenshot(page))
    assert page.calls == [{"full_page": True}]


def test_the_cap_is_generous_enough_for_an_ordinary_page():
    """Roughly ten viewports. Set too low, this would start cutting the
    bottom off normal marketing pages, which is a worse failure than the
    one it prevents.
    """
    assert crawler.MAX_SCREENSHOT_HEIGHT_PX >= 5000
