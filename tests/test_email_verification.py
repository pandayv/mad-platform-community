"""B2 / T3: the email-code gate, which had no test and no transaction.

`verify_email_code` was a plain read-modify-write: read `attempts`,
compare, write `attempts + 1`. Under concurrency that bounds *rounds*, not
guesses -- N simultaneous POSTs all read `attempts == 0`, all get a guess,
and all write `attempts == 1`. scan-onboarding is --allow-unauthenticated
at concurrency 20 across multiple instances, so generating that
concurrency is trivial, and `POST /scan/verify-code` had no rate limit of
any kind in front of it: no honeypot/timing check, no quota. The email
gate is what the privacy page, the FAQ and the scan form's own footnote
all present as *the* anti-abuse control.

The same shape was already fixed once, in `check_and_reserve_scan_quota`
-- see tests/test_quota.py, whose transaction-shape assertions these
mirror deliberately. A true concurrency test needs the Firestore
emulator, which this suite does not require; what is testable without one
is that the body is genuinely transactional, that reads precede writes,
and that the decision logic is right.
"""

from __future__ import annotations

import inspect
from datetime import datetime, timedelta, timezone

import pytest
from google.cloud.firestore_v1 import transaction as firestore_transaction

from mad_platform.state import firestore_client as fs

NOW = datetime(2026, 9, 19, 12, 0, tzinfo=timezone.utc)
LATER = NOW + timedelta(minutes=5)
EARLIER = NOW - timedelta(minutes=5)


class _FakeSnapshot:
    def __init__(self, data):
        self.exists = data is not None
        self._data = data

    def to_dict(self):
        return dict(self._data or {})


class _FakeDocRef:
    def __init__(self, data):
        self.data = data


class _FakeTransaction:
    """Records the order of reads and writes -- Firestore requires every
    read to precede every write, and violating it raises only at runtime
    under real contention.
    """

    def __init__(self):
        self.ops: list[tuple[str, dict | None]] = []

    def update(self, ref, fields):
        self.ops.append(("write", fields))
        ref.data.update(fields)

    def set(self, ref, fields, merge=False):
        self.ops.append(("write", fields))
        # merge=True creates the document when it does not exist yet, which
        # is the "first request from this address today" case.
        ref.data = {**(ref.data or {}), **fields} if merge else dict(fields)


def _read(ref, transaction=None):
    transaction.ops.append(("read", None))
    return _FakeSnapshot(ref.data)


def _consume(data, code, now=NOW):
    ref = _FakeDocRef(data)
    ref.get = lambda transaction=None: _read(ref, transaction)
    txn = _FakeTransaction()
    ok = fs._check_and_consume_code.to_wrap(txn, ref, code, now)
    return ok, ref.data, txn


def _pending(code="123456", attempts=0, expires=LATER):
    return {"code": code, "code_expires_at": expires, "attempts": attempts}


# --- the shape ------------------------------------------------------------


def test_the_body_is_wrapped_in_a_real_firestore_transaction():
    """If someone unwraps it, the read and the write stop being
    serializable and the batched-guess bypass is back.
    """
    assert isinstance(fs._check_and_consume_code, firestore_transaction._Transactional)


def test_the_public_entry_point_passes_a_real_transaction(monkeypatch):
    captured = {}

    class _FakeClient:
        def transaction(self):
            return "the-transaction"

    class _FakeCodes:
        def document(self, name):
            captured["doc"] = name
            return "the-ref"

    monkeypatch.setattr(fs, "get_client", _FakeClient)
    monkeypatch.setattr(fs, "_email_codes", _FakeCodes)
    monkeypatch.setattr(
        fs,
        "_check_and_consume_code",
        lambda txn, ref, code, now: captured.update(txn=txn, ref=ref, code=code) or True,
    )

    assert fs.verify_email_code("  Me@Example.com ", "123456") is True
    assert captured["txn"] == "the-transaction"
    assert captured["ref"] == "the-ref"
    assert captured["doc"] == "me@example.com", "the doc key must be normalized"


def test_reads_precede_writes_in_the_transaction_body():
    _ok, _data, txn = _consume(_pending(), "999999")
    kinds = [kind for kind, _ in txn.ops]
    assert kinds.index("read") < kinds.index("write")


def test_the_body_reads_through_the_transaction_not_around_it():
    """`doc_ref.get()` without `transaction=` is a non-transactional read:
    it would compile, pass every test below, and silently restore the race.
    """
    source = inspect.getsource(fs._check_and_consume_code.to_wrap)
    assert ".get(transaction=transaction)" in source
    assert "doc_ref.get()" not in source


# --- the decision ---------------------------------------------------------


def test_a_correct_code_verifies_and_is_single_use():
    ok, data, _ = _consume(_pending(), "123456")
    assert ok is True
    assert data["attempts"] == 0
    assert data["code"] is fs.firestore.DELETE_FIELD


def test_a_wrong_code_increments_the_attempt_counter():
    ok, data, _ = _consume(_pending(attempts=1), "999999")
    assert ok is False
    assert data["attempts"] == 2
    assert data["code"] == "123456", "a wrong guess must not clear a still-usable code"


def test_the_last_allowed_wrong_guess_clears_the_code_entirely():
    """Otherwise an exhausted-but-technically-still-correct code sits
    there waiting.
    """
    ok, data, _ = _consume(_pending(attempts=fs.MAX_CODE_ATTEMPTS - 1), "999999")
    assert ok is False
    assert data["attempts"] == fs.MAX_CODE_ATTEMPTS
    assert data["code"] is fs.firestore.DELETE_FIELD


def test_a_correct_code_is_refused_once_attempts_are_exhausted():
    ok, _data, _ = _consume(_pending(attempts=fs.MAX_CODE_ATTEMPTS), "123456")
    assert ok is False


def test_an_expired_code_is_refused_even_when_correct():
    ok, _data, _ = _consume(_pending(expires=EARLIER), "123456")
    assert ok is False


def test_a_code_with_no_expiry_field_is_refused():
    ok, _data, _ = _consume({"code": "123456", "attempts": 0}, "123456")
    assert ok is False


@pytest.mark.parametrize("guess", ["", None])
def test_an_empty_guess_never_verifies(guess):
    """Guards against a document whose `code` field was deleted: "" == ""
    would otherwise be a match.
    """
    ok, _data, _ = _consume({"code": "", "code_expires_at": LATER, "attempts": 0}, guess)
    assert ok is False


def test_a_missing_document_is_simply_false():
    ok, _data, txn = _consume(None, "123456")
    assert ok is False
    assert [kind for kind, _ in txn.ops] == ["read"], "nothing to increment, nothing to write"


# --- the per-address ceiling in front of it -------------------------------


def test_the_ip_attempt_quota_body_is_transactional():
    """Unlike the feedback counter, whose racy-but-cheap trade is stated
    and reasonable, this one exists specifically to resist a burst.
    """
    assert isinstance(fs._reserve_code_attempt, firestore_transaction._Transactional)


def _reserve(count, limit=50):
    ref = _FakeDocRef({"count": count} if count is not None else None)
    ref.get = lambda transaction=None: _read(ref, transaction)
    txn = _FakeTransaction()
    return fs._reserve_code_attempt.to_wrap(txn, ref, NOW, limit), ref.data


def test_the_first_attempt_from_a_new_address_is_allowed():
    allowed, data = _reserve(None)
    assert allowed is True
    assert data["count"] == 1


def test_an_address_at_the_ceiling_is_refused_without_incrementing():
    allowed, data = _reserve(50, limit=50)
    assert allowed is False
    assert data["count"] == 50


def test_the_ceiling_has_a_sane_default_and_an_env_override(monkeypatch):
    monkeypatch.delenv("MAD_MAX_CODE_ATTEMPTS_PER_IP_PER_DAY", raising=False)
    assert fs.max_code_attempts_per_ip_per_day() == 50
    monkeypatch.setenv("MAD_MAX_CODE_ATTEMPTS_PER_IP_PER_DAY", "7")
    assert fs.max_code_attempts_per_ip_per_day() == 7


def test_the_ceiling_is_well_above_what_a_real_visitor_needs():
    """A person gets MAX_CODE_ATTEMPTS guesses per code and may reasonably
    request a second code. If this ever dropped near that, a legitimate
    visitor on a shared NAT would be locked out.
    """
    assert fs.max_code_attempts_per_ip_per_day() > fs.MAX_CODE_ATTEMPTS * 3


# --- the route enforces it ------------------------------------------------


@pytest.fixture
def verify_client(monkeypatch):
    from fastapi.testclient import TestClient

    from mad_platform.web import app as app_module

    async def _safe(url):
        return url, None

    monkeypatch.setattr(app_module, "_safe_url_or_error", _safe)
    return TestClient(app_module.app), monkeypatch


def _submit(client, code="123456"):
    return client.post(
        "/scan/verify-code",
        data={"url": "https://example.com/", "email": "a@b.com", "code": code},
        headers={"X-Forwarded-For": "198.51.100.4"},
        follow_redirects=False,
    )


def test_the_route_refuses_once_the_address_is_over_its_daily_ceiling(verify_client):
    client, monkeypatch = verify_client
    compared: list[str] = []
    monkeypatch.setattr(
        fs,
        "check_and_reserve_code_attempt_quota",
        lambda ip: (False, "Too many verification attempts from this network today."),
    )
    monkeypatch.setattr(
        fs, "verify_email_code", lambda e, c: compared.append(c) or True
    )

    resp = _submit(client)
    assert resp.status_code == 429
    assert compared == [], "a refused guess must not be compared against the code at all"


def test_the_route_reserves_against_the_visitors_forwarded_address(verify_client):
    client, monkeypatch = verify_client
    seen: list[str] = []
    monkeypatch.setattr(
        fs,
        "check_and_reserve_code_attempt_quota",
        lambda ip: (seen.append(ip), (True, ""))[1],
    )
    monkeypatch.setattr(fs, "verify_email_code", lambda e, c: False)

    _submit(client)
    assert seen == ["198.51.100.4"]


def test_a_wrong_code_under_the_ceiling_still_gets_the_ordinary_error(verify_client):
    client, monkeypatch = verify_client
    monkeypatch.setattr(fs, "check_and_reserve_code_attempt_quota", lambda ip: (True, ""))
    monkeypatch.setattr(fs, "verify_email_code", lambda e, c: False)

    resp = _submit(client, code="000000")
    assert resp.status_code == 400
    assert "invalid or has expired" in resp.text
