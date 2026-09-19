"""F2, F3, F5, F9: the public web surface.

Driven through FastAPI's TestClient with firestore_client and the Cloud
Tasks client faked, so none of this needs GCP. The lifespan hook is
deliberately not run (TestClient is used without a context manager where
possible, and `clean_env` is not applied), because config.validate_web_config()
is a deployment concern already covered by tests/test_config.py.
"""

from __future__ import annotations

import time

import httpx
import pytest
from fastapi.testclient import TestClient
from google.api_core import exceptions as gcloud_exceptions

from mad_platform.state import firestore_client as fs
from mad_platform.web import app as app_module


# A real-shaped job id. Job ids are uuid.uuid4() strings
# (firestore_client.create_job), and the routes that take one now validate
# the shape before looking it up -- so "job-1" is not merely cosmetic here,
# it is a 404 (see app._valid_job_id and tests/test_route_validation.py).
JOB_ID = "3f1c2b8a-7d44-4e9b-9c10-2a5e6f0b1d77"


@pytest.fixture
def client() -> TestClient:
    return TestClient(app_module.app)


# --- F9: the theme stylesheet is served once and cached ---------------------


def test_the_theme_stylesheet_is_served_at_a_content_hashed_url(client):
    resp = client.get(app_module._THEME_CSS_PATH)
    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("text/css")
    assert ":root" in resp.text


def test_the_stylesheet_is_cacheable_while_pages_stay_no_store(client):
    """no-store on the HTML is a deliberate decision (active redesign,
    stale cached pages are a real risk). A content-hashed URL cannot go
    stale, so it must be exempt -- otherwise the fix does nothing.
    """
    css = client.get(app_module._THEME_CSS_PATH)
    assert "immutable" in css.headers["cache-control"]
    assert "no-store" not in css.headers["cache-control"]

    page = client.get("/")
    assert page.headers["cache-control"] == "no-store"


def test_static_assets_carry_an_explicit_cache_control(client):
    """T4/F3: this test's name promised /static coverage and never checked
    it, while the middleware exempted /static from no-store on the belief
    that StaticFiles sets its own long-lived header. It does not -- it
    emits ETag and Last-Modified only -- so ~1.7 MB of PNGs were served
    with no directive at all, leaving every browser to guess.
    """
    resp = client.get("/static/favicon.svg")
    assert resp.status_code == 200
    cache = resp.headers["cache-control"]
    assert "no-store" not in cache
    assert "max-age=" in cache
    assert "public" in cache


def test_static_assets_are_not_cached_forever(client):
    """Unlike the theme stylesheet, /static filenames are not content-
    hashed, so a replaced image has to be able to reach visitors.
    `immutable` here would strand it.
    """
    cache = client.get("/static/favicon.svg").headers["cache-control"]
    assert "immutable" not in cache


def test_the_url_changes_when_the_css_changes():
    """The whole basis for caching forever."""
    import hashlib

    from mad_platform.web import theme

    expected = hashlib.sha256(theme.THEME_CSS.encode("utf-8")).hexdigest()[:12]
    assert expected in app_module._THEME_CSS_PATH


def test_pages_link_the_stylesheet_instead_of_inlining_it(client):
    body = client.get("/").text
    assert app_module._THEME_CSS_PATH in body
    assert "<style>" not in body


# --- F4: landmarks and the skip link ---------------------------------------


@pytest.mark.parametrize("path", ["/", "/faq", "/terms", "/privacy"])
def test_every_public_page_has_one_main_landmark_and_a_skip_link(client, path):
    body = client.get(path).text
    assert body.count('<main id="main"') == 1
    assert body.count("</main>") == 1
    assert 'class="skip-link" href="#main"' in body


def test_no_decorative_svg_is_announced_as_an_unlabelled_graphic(client):
    """18 inline SVGs carried neither aria-hidden nor a label. Every one
    of them is now either hidden or a labelled role="img".
    """
    body = client.get("/").text
    for fragment in body.split("<svg")[1:]:
        tag = fragment.split(">", 1)[0]
        assert 'aria-hidden="true"' in tag or 'role="img"' in tag, tag[:120]


def test_the_comparison_table_can_be_reached_and_named_by_keyboard(client):
    """WCAG 2.1.1: the wrapper is overflow-x:auto around a 560px-min table,
    so below 560px a keyboard-only user could reach the first column and
    nothing else.
    """
    body = client.get("/").text
    wrapper = body.split('class="compare-wrap"', 1)[1].split(">", 1)[0]
    assert 'tabindex="0"' in wrapper
    assert 'role="region"' in wrapper
    assert "aria-label=" in wrapper


def test_no_comparison_cell_announces_something_other_than_its_mark(client):
    """F8 / WCAG 1.3.1: the accessible name must convey the same
    information as the visual presentation. Three cells did not -- a plain
    cross announced as "Rarely", and two plain checks announced as "Yes,
    automatic" -- so a screen-reader user heard a materially different
    claim than a sighted one read. Notable beyond its severity because
    this table is the product's own accessibility credential.
    """
    import re

    body = client.get("/").text
    table = body.split('class="compare-wrap"', 1)[1].split("</table>", 1)[0]
    allowed = {"&check;": {"yes"}, "&cross;": {"no"}}
    for cell in re.findall(r"<td[^>]*>(.*?)</td>", table, re.DOTALL):
        mark = re.search(r'class="mark-(?:yes|no)"[^>]*>(&\w+;)</span>', cell)
        if not mark:
            continue  # .mark-partial cells: the visible text IS the name
        announced = re.search(r'class="sr-only">(.*?)</span>', cell)
        assert announced, cell
        assert announced.group(1).strip().lower() in allowed[mark.group(1)], cell


def test_the_scroll_hint_is_not_pointer_only(client):
    body = client.get("/").text
    hint = body.split('class="compare-hint"', 1)[1].split("</p>", 1)[0]
    assert "arrow keys" in hint


def test_the_illustrative_hero_report_is_hidden_from_assistive_tech(client):
    """It renders "Accessibility score 72", "18 issues across 6 pages" and
    a severity breakdown for a fictional site. Obvious to a sighted
    visitor from the mock browser chrome; nothing said so otherwise.
    """
    body = client.get("/").text
    visual = body.split('class="hero-visual"', 1)[1].split(">", 1)[0]
    assert 'aria-hidden="true"' in visual
    assert "Illustration: an example accessibility report" in body


def test_the_status_page_announces_its_own_updates(client, monkeypatch):
    monkeypatch.setattr(fs, "get_job", lambda _j: {"url": "https://example.com/"})
    body = client.get(f"/status/{JOB_ID}").text
    content = body.split('id="content"', 1)[1].split(">", 1)[0]
    assert 'role="status"' in content
    assert 'aria-live="polite"' in content


def test_the_status_page_has_no_unreplaced_placeholders(client, monkeypatch):
    """_STATUS_PAGE is a plain string, so a missed placeholder renders to
    the visitor verbatim rather than raising (this happened for real with
    the brand mark).
    """
    monkeypatch.setattr(fs, "get_job", lambda _j: {"url": "https://example.com/"})
    body = client.get(f"/status/{JOB_ID}").text
    assert "__" not in body.replace("__pycache__", "")


# --- F2: Turnstile fails open on a non-JSON response ------------------------


async def test_turnstile_fails_open_when_cloudflare_returns_html(monkeypatch, caplog):
    """Only httpx.HTTPError was caught. resp.json() raises
    json.JSONDecodeError -- a ValueError -- on a Cloudflare 502/503 HTML
    error page, a captive portal or a WAF block, which meant an unhandled
    exception and a 500 on POST /scan/start for every visitor: the exact
    opposite of the fail-open the comment promises.
    """
    monkeypatch.setenv("TURNSTILE_SECRET_KEY", "secret")

    class _FakeClient:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def post(self, *a, **k):
            return httpx.Response(200, text="<html>Cloudflare is having a moment</html>",
                                  request=httpx.Request("POST", "https://x/"))

    monkeypatch.setattr(httpx, "AsyncClient", lambda **k: _FakeClient())
    with caplog.at_level("WARNING", logger="mad_platform.web"):
        assert await app_module._turnstile_passed("token") is True
    assert caplog.records, "a permanent silent fail-open looks identical to a working gate"


async def test_turnstile_fails_open_on_a_non_2xx_response(monkeypatch):
    monkeypatch.setenv("TURNSTILE_SECRET_KEY", "secret")

    class _FakeClient:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def post(self, *a, **k):
            return httpx.Response(503, text="<html>503</html>", request=httpx.Request("POST", "https://x/"))

    monkeypatch.setattr(httpx, "AsyncClient", lambda **k: _FakeClient())
    assert await app_module._turnstile_passed("token") is True


async def test_turnstile_still_rejects_a_genuine_failure(monkeypatch):
    monkeypatch.setenv("TURNSTILE_SECRET_KEY", "secret")

    class _FakeClient:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def post(self, *a, **k):
            return httpx.Response(200, json={"success": False}, request=httpx.Request("POST", "https://x/"))

    monkeypatch.setattr(httpx, "AsyncClient", lambda **k: _FakeClient())
    assert await app_module._turnstile_passed("token") is False


# --- F3: a failed enqueue must not charge the visitor -----------------------


@pytest.fixture
def start_scan_harness(monkeypatch):
    state = {"refunded": [], "failed": [], "reserved": 0}
    monkeypatch.setattr(
        fs, "check_and_reserve_scan_quota",
        lambda email, ip: (state.__setitem__("reserved", state["reserved"] + 1), (True, ""))[1],
    )
    monkeypatch.setattr(fs, "create_job", lambda *a, **k: JOB_ID)
    monkeypatch.setattr(fs, "fail_job", lambda job_id, msg: state["failed"].append((job_id, msg)))
    monkeypatch.setattr(fs, "refund_scan_quota", lambda email, ip: state["refunded"].append((email, ip)))
    return state


async def _start(email="a@b.com", url="https://example.com/"):
    class _Req:
        # Both are needed: the per-IP quota key comes from
        # app._client_ip, which reads X-Forwarded-For first and only falls
        # back to the socket peer. See test_client_ip.py.
        client = type("C", (), {"host": "1.2.3.4"})()
        headers: dict[str, str] = {}

    return await app_module._start_scan(_Req(), url, email)


async def test_a_failed_enqueue_refunds_the_quota_and_fails_the_job(monkeypatch, start_scan_harness):
    """Before: a bare 500, the quota unit kept, and a `status: "queued"`
    job no worker would ever pick up -- whose status page promised "we'll
    email your full report as soon as it's ready".
    """
    def boom(_job_id):
        raise gcloud_exceptions.FailedPrecondition("queue is paused")

    monkeypatch.setattr(app_module, "_enqueue_scan", boom)

    resp = await _start()
    assert resp.status_code == 503
    assert start_scan_harness["refunded"] == [("a@b.com", "1.2.3.4")]
    assert start_scan_harness["failed"] and start_scan_harness["failed"][0][0] == JOB_ID
    assert b"hasn&#x27;t been used" in resp.body or b"hasn't been used" in resp.body


async def test_an_already_queued_task_is_treated_as_success(monkeypatch, start_scan_harness):
    """The task name IS the job_id, so AlreadyExists means this exact job
    is already queued -- the dedup guard working, not a failure.
    """
    def already(_job_id):
        raise gcloud_exceptions.AlreadyExists("task exists")

    monkeypatch.setattr(app_module, "_enqueue_scan", already)

    resp = await _start()
    assert resp.status_code == 303
    assert resp.headers["location"] == f"/status/{JOB_ID}"
    assert start_scan_harness["refunded"] == []


async def test_a_successful_enqueue_does_not_refund(monkeypatch, start_scan_harness):
    monkeypatch.setattr(app_module, "_enqueue_scan", lambda _j: None)
    resp = await _start()
    assert resp.status_code == 303
    assert start_scan_harness["refunded"] == []


# --- F5: the feedback form (now open, not token-gated -- see app.py's
# _render_feedback_page/submit_feedback docstrings for why removing the
# token isn't a regression of the F5 fix) -----------------------------------


@pytest.fixture
def feedback_harness(monkeypatch):
    saved = []
    monkeypatch.setattr(fs, "check_and_reserve_feedback_quota", lambda ip: (True, ""))
    monkeypatch.setattr(fs, "save_feedback", lambda *a, **k: saved.append((a, k)))
    monkeypatch.setattr(fs, "get_job", lambda _j: None)
    return saved


def _post(client, **fields):
    data = {"rating": 5, "form_ts": time.time() - 10, "website": ""}
    data.update(fields)
    return client.post("/feedback", data=data, follow_redirects=False)


def test_a_real_looking_submission_is_accepted(client, feedback_harness):
    resp = _post(client, job_id=JOB_ID)
    assert resp.status_code == 303
    assert resp.headers["location"] == "/feedback/thanks"
    assert len(feedback_harness) == 1


def test_the_submitted_url_is_stored_independent_of_job_id(client, feedback_harness):
    """The URL field is editable, not just a display of the linked job's
    own URL -- what the visitor actually typed is what gets stored.
    """
    _post(client, job_id=JOB_ID, url="https://a-different-site.example")
    (_args, kwargs) = feedback_harness[0]
    assert kwargs["url"] == "https://a-different-site.example"


def test_feedback_needs_no_job_id_at_all(client, feedback_harness):
    """The FAQ links here with no scan behind it -- job_id must be
    genuinely optional, not just tolerant of an empty string.
    """
    resp = _post(client, job_id="")
    assert resp.status_code == 303
    (args, kwargs) = feedback_harness[0]
    assert args[0] is None or kwargs.get("job_id") is None


def test_the_honeypot_field_silently_rejects(client, feedback_harness):
    """A filled decoy field, not a visible error -- same contract as every
    other form in the scan funnel (_honeypot_or_timing_error).
    """
    resp = _post(client, website="I am a bot")
    assert resp.status_code == 400
    assert feedback_harness == []


def test_a_too_fast_submission_is_rejected(client, feedback_harness):
    resp = _post(client, form_ts=time.time())
    assert resp.status_code == 400
    assert feedback_harness == []


@pytest.mark.parametrize("rating", [0, -1, -999999, 6, 2**40])
def test_out_of_range_ratings_are_rejected(client, feedback_harness, rating):
    assert _post(client, rating=rating).status_code == 422
    assert feedback_harness == []


@pytest.mark.parametrize("rating", [1, 2, 3, 4, 5])
def test_every_in_range_rating_is_accepted(client, feedback_harness, rating):
    assert _post(client, rating=rating).status_code == 303


def test_an_oversized_comment_is_rejected(client, feedback_harness):
    assert _post(client, comment="x" * 5000).status_code == 422
    assert feedback_harness == []


def test_an_oversized_contact_is_rejected(client, feedback_harness):
    assert _post(client, contact="x" * 500).status_code == 422
    assert feedback_harness == []


def test_the_per_ip_feedback_quota_is_enforced(client, feedback_harness, monkeypatch):
    monkeypatch.setattr(fs, "check_and_reserve_feedback_quota", lambda ip: (False, "You've reached today's feedback limit. Please try again tomorrow."))
    resp = _post(client)
    assert resp.status_code == 429
    assert feedback_harness == []


def test_the_feedback_page_loads_with_no_job_context(client):
    resp = client.get("/feedback")
    assert resp.status_code == 200
    assert "rating" in resp.text


def test_the_feedback_page_shows_the_scans_url_when_a_real_job_is_given(client, monkeypatch):
    monkeypatch.setattr(fs, "get_job", lambda _j: {"url": "https://example.com"})
    resp = client.get(f"/feedback?job={JOB_ID}")
    assert "example.com" in resp.text


def test_an_unknown_job_in_the_query_string_degrades_to_the_plain_form(client, monkeypatch):
    """A stale or tampered job param must not error the page -- it just
    stops showing scan-specific context.
    """
    monkeypatch.setattr(fs, "get_job", lambda _j: None)
    resp = client.get("/feedback?job=00000000-0000-4000-8000-000000000000")
    assert resp.status_code == 200
