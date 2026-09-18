"""B11: a resumed scan must not silently shrink its own scope.

The resume branch keyed off `existing_job["pages"]` being non-empty and
treated its keys as "the pages this job chose". But the entry URL is
checkpointed into `pages` one line BEFORE select_pages runs -- so a first
attempt that died inside the selection call (a Gemini call, which is
exactly where it dies) left a job whose `pages` had exactly one key. The
Cloud Tasks retry read that crash checkpoint as a decision, skipped
selection entirely, and completed "successfully" having scanned one page
instead of three. Nothing in the report, the status page or the logs said
the scope had been cut, while the landing page's comparison table claims
multi-page scanning.

The fix is to key resume off a field that only the selection step writes.
"""

from __future__ import annotations

import inspect
import pathlib

import pytest

from mad_platform.agents import orchestrator
from mad_platform.state import firestore_client as fs


def _orchestrator_source() -> str:
    return pathlib.Path(orchestrator.__file__).read_text()


def test_resume_is_keyed_off_selected_pages_not_the_checkpoint_map():
    source = _orchestrator_source()
    assert 'existing_job.get("selected_pages")' in source
    assert 'existing_job.get("pages")' not in source, (
        "the page checkpoint map is a crash record, not a decision -- keying resume off it "
        "is the bug"
    )


def test_the_selection_result_is_persisted_by_its_own_writer():
    assert hasattr(fs, "set_selected_pages")
    params = inspect.signature(fs.set_selected_pages).parameters
    assert list(params) == ["job_id", "pages"]


def test_selected_pages_is_written_immediately_after_selection():
    """If this write drifts away from select_pages returning, the window
    B11 describes reopens -- so the ordering is asserted, not just the
    call's existence.
    """
    source = _orchestrator_source()
    select_at = source.index("pages = await select_pages(entry_snapshot)")
    persist_at = source.index("fs.set_selected_pages(job_id, pages)")
    analyze_at = source.index('fs.set_job_phase(job_id, "analyzing_pages")')
    assert select_at < persist_at < analyze_at


@pytest.fixture
def scan_harness(monkeypatch):
    """Drives run_one_time_scan far enough to see which pages it decided
    to analyze, with every Firestore/Gemini/Playwright call stubbed. The
    scan is aborted deliberately once the page list is known -- everything
    after that is out of this finding's scope and would need a much larger
    fake.
    """

    class _Stop(Exception):
        pass

    state = {"selected": [], "analyzed": None, "selection_ran": False}

    class _Snapshot:
        url = "https://example.com/"
        html = "<html></html>"
        title = "t"
        screenshot_png = b""
        text_style_samples: list = []

    async def fake_fetch(_url, *a, **k):
        return _Snapshot()

    async def fake_select(_snapshot):
        state["selection_ran"] = True
        return [
            "https://example.com/",
            "https://example.com/contact",
            "https://example.com/shop",
        ]

    def fake_phase(_job_id, phase):
        if phase == "analyzing_pages":
            # By here the page list is settled; stop before any real work.
            raise _Stop()

    monkeypatch.setattr(orchestrator, "fetch_page", fake_fetch)
    monkeypatch.setattr(orchestrator, "select_pages", fake_select)
    monkeypatch.setattr(fs, "set_job_phase", fake_phase)
    monkeypatch.setattr(fs, "checkpoint_page_crawled", lambda *a, **k: None)
    monkeypatch.setattr(fs, "fail_job", lambda *a, **k: None)
    monkeypatch.setattr(fs, "set_selected_pages", lambda _j, pages: state["selected"].append(list(pages)))
    monkeypatch.setattr(orchestrator.logger, "info", lambda *a, **k: _capture(state, a))
    return state, _Stop


def _capture(state, args):
    """Reads the page list out of the orchestrator's own log calls, which
    is how the test sees what it settled on without reaching the analysis
    loop.
    """
    if args and isinstance(args[0], str) and "page(s)" in args[0]:
        state["analyzed"] = args[-1]


async def test_a_job_that_died_before_selection_re_runs_selection(scan_harness, monkeypatch):
    """The exact shape a crash inside select_pages leaves behind: one
    `pages` key (the entry crawl checkpoint) and no `selected_pages`.
    Before the fix this resumed with a single page and scanned it alone.
    """
    state, stop = scan_harness
    monkeypatch.setattr(
        fs, "get_job",
        lambda _j: {"url": "https://example.com/", "pages": {"https://example.com/": {"stage": "crawled"}}},
    )
    with pytest.raises(stop):
        await orchestrator.run_one_time_scan("https://example.com/", job_id="job-1")

    assert state["selection_ran"] is True, "selection must re-run when it never completed"
    assert state["selected"] == [
        ["https://example.com/", "https://example.com/contact", "https://example.com/shop"]
    ]


async def test_a_job_that_finished_selection_resumes_without_re_selecting(scan_harness, monkeypatch):
    """The property the resume branch exists for: a genuine resume must
    not re-decide scope (a fresh LLM call is not guaranteed to pick the
    same pages twice).
    """
    state, stop = scan_harness
    monkeypatch.setattr(
        fs, "get_job",
        lambda _j: {
            "url": "https://example.com/",
            "selected_pages": ["https://example.com/", "https://example.com/contact"],
            "pages": {"https://example.com/": {"stage": "crawled"}},
        },
    )
    with pytest.raises(stop):
        await orchestrator.run_one_time_scan("https://example.com/", job_id="job-1")

    assert state["selection_ran"] is False
    assert state["analyzed"] == 2
