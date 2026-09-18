"""F2, F3, F5, F9: the public web surface.

Driven through FastAPI's TestClient with firestore_client and the Cloud
Tasks client faked, so none of this needs GCP. The lifespan hook is
deliberately not run (TestClient is used without a context manager where
possible, and `clean_env` is not applied), because config.validate_web_config()
is a deployment concern already covered by tests/test_config.py.
"""

from __future__ import annotations

import httpx
import pytest
from fastapi.testclient import TestClient
from google.api_core import exceptions as gcloud_exceptions

from mad_platform.state import firestore_client as fs
from mad_platform.web import app as app_module


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
    body = client.get("/status/job-1").text
    content = body.split('id="content"', 1)[1].split(">", 1)[0]
    assert 'role="status"' in content
    assert 'aria-live="polite"' in content


def test_the_status_page_has_no_unreplaced_placeholders(client, monkeypatch):
    """_STATUS_PAGE is a plain string, so a missed placeholder renders to
    the visitor verbatim rather than raising (this happened for real with
    the brand mark).
    """
    monkeypatch.setattr(fs, "get_job", lambda _j: {"url": "https://example.com/"})
    body = client.get("/status/job-1").text
    assert "__" not in body.replace("__pycache__", "")


# --- F2: Turnstile fails open on a non-JSON response ------------------------


async def test_turnstile_fails_open_when_cloudflare_returns_html(monkeypatch, caplog):
    """Only httpx.HTTPError was caught. resp.json() raises
    json.JSONDecodeError -- a ValueError -- on a Cloudflare 502/503 HTML
    error page, a captive portal or a WAF block, which meant an unhandled
    exception and a 500 on POST /scan/start for every visitor: the exact
    opposite of the fail-open the comment promises.
    """
    monkeypatch.setattr(app_module, "_TURNSTILE_SECRET_KEY", "secret")

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
    monkeypatch.setattr(app_module, "_TURNSTILE_SECRET_KEY", "secret")

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
    monkeypatch.setattr(app_module, "_TURNSTILE_SECRET_KEY", "secret")

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
    monkeypatch.setattr(fs, "create_job", lambda *a, **k: "job-xyz")
    monkeypatch.setattr(fs, "fail_job", lambda job_id, msg: state["failed"].append((job_id, msg)))
    monkeypatch.setattr(fs, "refund_scan_quota", lambda email, ip: state["refunded"].append((email, ip)))
    return state


async def _start(email="a@b.com", url="https://example.com/"):
    class _Req:
        client = type("C", (), {"host": "1.2.3.4"})()

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
    assert start_scan_harness["failed"] and start_scan_harness["failed"][0][0] == "job-xyz"
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
    assert resp.headers["location"] == "/status/job-xyz"
    assert start_scan_harness["refunded"] == []


async def test_a_successful_enqueue_does_not_refund(monkeypatch, start_scan_harness):
    monkeypatch.setattr(app_module, "_enqueue_scan", lambda _j: None)
    resp = await _start()
    assert resp.status_code == 303
    assert start_scan_harness["refunded"] == []


# --- F5: the feedback endpoint ---------------------------------------------


@pytest.fixture
def feedback_harness(monkeypatch):
    saved = []
    monkeypatch.setattr(fs, "verify_review_token", lambda job_id, token: token == "good-token")
    monkeypatch.setattr(fs, "has_feedback", lambda _j: False)
    monkeypatch.setattr(fs, "save_feedback", lambda *a, **k: saved.append((a, k)))
    return saved


def _post(client, **fields):
    data = {"token": "good-token", "rating": 5}
    data.update(fields)
    return client.post("/report/job-1/feedback", data=data)


def test_feedback_requires_the_jobs_own_review_token(client, feedback_harness):
    """Any valid job ID used to be enough, so one leaked ID was an
    unlimited Firestore write channel -- and allow_testimonial is
    caller-controlled, so an attacker could mark their own text
    publishable.
    """
    assert _post(client, token="wrong").status_code == 404
    assert feedback_harness == []


def test_a_wrong_token_looks_the_same_as_a_missing_job(client, feedback_harness):
    """A guessed token must not confirm that a job ID is real."""
    assert _post(client, token="wrong").status_code == 404


def test_a_valid_token_is_accepted(client, feedback_harness):
    assert _post(client).status_code == 200
    assert len(feedback_harness) == 1


@pytest.mark.parametrize("rating", [0, -1, -999999, 6, 2**40])
def test_out_of_range_ratings_are_rejected(client, feedback_harness, rating):
    assert _post(client, rating=rating).status_code == 422
    assert feedback_harness == []


@pytest.mark.parametrize("rating", [1, 2, 3, 4, 5])
def test_every_in_range_rating_is_accepted(client, feedback_harness, rating):
    assert _post(client, rating=rating).status_code == 200


def test_an_oversized_comment_is_rejected(client, feedback_harness):
    assert _post(client, comment="x" * 5000).status_code == 422
    assert feedback_harness == []


def test_an_oversized_contact_is_rejected(client, feedback_harness):
    assert _post(client, contact="x" * 500).status_code == 422


def test_a_second_submission_for_the_same_job_is_a_no_op(client, feedback_harness, monkeypatch):
    monkeypatch.setattr(fs, "has_feedback", lambda _j: True)
    resp = _post(client)
    # Idempotent, not an error: a double-click should look like success.
    assert resp.status_code == 200
    assert resp.json()["already_submitted"] is True
    assert feedback_harness == []
