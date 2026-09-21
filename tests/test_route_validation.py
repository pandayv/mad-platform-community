"""F10 / B12: a malformed identifier or disposition is a 4xx, not a 500.

Both were noise-level on their own and both were one-line validations.
They are worth closing because 500s from a public route pollute the error
budget you would otherwise use to spot real failures -- and because in one
case the 500 was itself a signal: a bad disposition on the scoped review
link raised *after* `_scoped_escalation_or_404` succeeded, so it confirmed
to the caller that their token was valid.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from mad_platform.state import firestore_client as fs
from mad_platform.web import app as app_module

# The shapes this system actually generates.
JOB_ID = "3f1c2b8a-7d44-4e9b-9c10-2a5e6f0b1d77"  # uuid.uuid4()
ESCALATION_ID = "a1b2c3d4e5f60718"  # sha256 hexdigest[:16]


@pytest.fixture
def client() -> TestClient:
    return TestClient(app_module.app, raise_server_exceptions=False)


# --- the shapes themselves ------------------------------------------------


def test_a_real_job_id_is_accepted():
    assert app_module._valid_job_id(JOB_ID) == JOB_ID


def test_a_real_escalation_id_is_accepted():
    assert app_module._valid_escalation_id(ESCALATION_ID) == ESCALATION_ID


def test_the_generators_still_produce_the_shapes_these_regexes_expect():
    """The validators encode what two other modules generate. If either
    changes its id format, this is what notices -- rather than every route
    starting to 404 in production.
    """
    import hashlib
    import uuid

    assert app_module._JOB_ID_RE.match(str(uuid.uuid4()))
    assert app_module._ESCALATION_ID_RE.match(hashlib.sha256(b"x").hexdigest()[:16])


@pytest.mark.parametrize(
    "bad",
    [
        "a/b",  # the actual reported case: Firestore rejects an odd segment count
        "",
        "..",
        ".",
        "not-a-uuid",
        "3f1c2b8a7d444e9b9c102a5e6f0b1d77",  # right characters, no hyphens
        "3f1c2b8a-7d44-4e9b-9c10-2a5e6f0b1d77x",
        "../../etc/passwd",
        "3f1c2b8a-7d44-4e9b-9c10-2a5e6f0b1d77/../other",
    ],
)
def test_a_malformed_job_id_is_refused_before_any_lookup(bad):
    from fastapi import HTTPException

    with pytest.raises(HTTPException) as exc:
        app_module._valid_job_id(bad)
    assert exc.value.status_code == 404


@pytest.mark.parametrize("bad", ["a/b", "", "ZZZZ", "a1b2c3d4e5f6071", "a1b2c3d4e5f607189"])
def test_a_malformed_escalation_id_is_refused_before_any_lookup(bad):
    from fastapi import HTTPException

    with pytest.raises(HTTPException) as exc:
        app_module._valid_escalation_id(bad)
    assert exc.value.status_code == 404


# --- through the routes ---------------------------------------------------


@pytest.mark.parametrize(
    "path",
    [
        "/status/a%2Fb",
        "/api/status/a%2Fb",
        "/report/a%2Fb",
        "/report/a%2Fb/tickets.csv",
        "/api/escalation/a%2Fb/status",
    ],
)
def test_a_percent_encoded_slash_is_a_404_not_a_500(client, path, monkeypatch):
    """Starlette percent-decodes path params, so `%2F` arrives as a literal
    "/" and CollectionReference.document() raises ValueError on it.
    """
    def _boom(*_a, **_k):
        raise AssertionError("the id should never have reached a lookup")

    monkeypatch.setattr(fs, "get_job", _boom)
    monkeypatch.setattr(fs, "get_escalation", _boom)
    assert client.get(path).status_code == 404


def test_a_well_formed_but_unknown_job_id_still_reaches_the_lookup(client, monkeypatch):
    """The validation must not become the only thing answering -- an id of
    the right shape has to be looked up and 404 on its own merits.
    """
    looked_up = []
    monkeypatch.setattr(fs, "get_job", lambda j: looked_up.append(j) or None)
    assert client.get(f"/status/{JOB_ID}").status_code == 404
    assert looked_up == [JOB_ID]


def test_a_malformed_feedback_job_param_degrades_instead_of_erroring(client, monkeypatch):
    """This route already degrades to the plain form for a stale or
    tampered job param, so a malformed one should degrade identically
    rather than 500 -- the same answer, by the same path.
    """
    def _boom(*_a, **_k):
        raise AssertionError("the id should never have reached a lookup")

    monkeypatch.setattr(fs, "get_job", _boom)
    resp = client.get("/feedback?job=a/b")
    assert resp.status_code == 200
    assert "rating" in resp.text


# --- B12: the disposition field -------------------------------------------


def test_an_unknown_disposition_is_rejected_before_the_handler_runs(client, monkeypatch):
    """`disposition: str` reached a `raise ValueError` inside
    resolve_escalation -- a 500 where 400/422 belongs. As a Literal,
    FastAPI refuses it at the boundary.
    """
    monkeypatch.setattr(app_module, "_is_reviewer", lambda _r: True)

    def _boom(*_a, **_k):
        raise AssertionError("a bad disposition must never reach the resolver")

    monkeypatch.setattr(fs, "get_escalation", _boom)
    resp = client.post(f"/review/{ESCALATION_ID}/resolve", data={"disposition": "delete-everything"})
    assert resp.status_code == 422


def test_the_scoped_resolve_route_refuses_it_too_without_confirming_the_token(client):
    """The scoped 500 was worse than the admin one: it fired *after*
    _scoped_escalation_or_404 had already passed, so it told the caller
    their token was valid.
    """
    resp = client.post(
        f"/review/link/{JOB_ID}/some-token/{ESCALATION_ID}/resolve",
        data={"disposition": "delete-everything"},
    )
    assert resp.status_code == 422


@pytest.mark.parametrize("disposition", ["confirm", "dismiss"])
def test_both_real_dispositions_are_still_accepted(client, monkeypatch, disposition):
    """The forms only ever emit these two; rejecting one would break the
    review queue outright.
    """
    monkeypatch.setattr(app_module, "_is_reviewer", lambda _r: True)
    monkeypatch.setattr(fs, "get_escalation", lambda _e: None)
    resp = client.post(f"/review/{ESCALATION_ID}/resolve", data={"disposition": disposition})
    assert resp.status_code == 404, "reached the handler and 404'd on the unknown escalation"


# --- S1: a stored report must not outlive its scan record ----------------


def test_a_report_whose_job_record_is_gone_is_no_longer_served(client, monkeypatch):
    """The report HTML *is* the scan record: the submitted URL, every
    finding, the suggested fixes, and the scan's review token. This route
    fetched the blob directly and never consulted the job, so after
    Firestore's TTL removed the job document the report kept being served
    -- and a deletion request handled by removing the Firestore document
    left the report live. The privacy page says these are removed after 12
    months.
    """
    served = []
    monkeypatch.setattr(fs, "get_job", lambda _j: None)
    monkeypatch.setattr(
        app_module.storage_client, "read_report",
        lambda j: served.append(j) or "<html>the report</html>",
    )
    resp = client.get(f"/report/{JOB_ID}")
    assert resp.status_code == 404
    assert served == [], "the blob must not even be fetched once the record is gone"


def test_a_report_with_a_live_job_record_is_still_served(client, monkeypatch):
    monkeypatch.setattr(fs, "get_job", lambda _j: {"url": "https://example.com/"})
    monkeypatch.setattr(app_module.storage_client, "read_report", lambda j: "<html>the report</html>")
    resp = client.get(f"/report/{JOB_ID}")
    assert resp.status_code == 200
    assert "the report" in resp.text


def test_the_retention_window_is_one_constant_shared_with_the_deploy_steps():
    """365 appears in firestore_client, in SETUP.md step 3's lifecycle rule
    and in setup.sh. The constant is the one the code actually uses; this
    checks the deploy scripts still agree with it.
    """
    import pathlib
    import re

    root = pathlib.Path(app_module.__file__).parents[2]
    days = fs.SCAN_RECORD_RETENTION_DAYS
    for name in ("setup.sh", "SETUP.md"):
        text = (root / name).read_text()
        rule = re.search(r'"age":\s*(\d+),\s*"matchesPrefix":\s*\["reports/"\]', text)
        assert rule, f"{name} no longer configures a reports/ lifecycle rule"
        assert int(rule.group(1)) == days, f"{name} says {rule.group(1)} days, the code says {days}"
