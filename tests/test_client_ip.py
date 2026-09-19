"""F1: the per-IP rate limits have to be keyed on the visitor's address.

`request.client.host` is the peer of the TCP connection uvicorn accepted.
On Cloud Run the container never sees the visitor on the socket -- the
address arrives in `X-Forwarded-For`, and none of the four Dockerfiles
start uvicorn with `--proxy-headers`. The codebase already diagnosed this
once, in `_is_https`, and fixed it for the scheme only; the two per-IP
counters (`MAD_MAX_SCANS_PER_IP_PER_DAY`, `MAD_MAX_FEEDBACK_PER_IP_PER_DAY`)
kept reading the peer, so they either lumped the whole internet into one
counter or enforced nothing at all.

The direction these pin most carefully is *which* forwarded entry is used.
Cloud Run's front end appends the connecting address to whatever the
caller sent, so the leftmost entry is attacker-chosen and the rightmost is
Google's. A rate-limit key the abuser picks is worse than no key.
"""

from __future__ import annotations

import time

import pytest
from fastapi.testclient import TestClient

from mad_platform.state import firestore_client as fs
from mad_platform.web import app as app_module


class _Req:
    """The two attributes `_client_ip` reads, and nothing else."""

    def __init__(self, forwarded: str | None = None, peer: str | None = None):
        self.headers = {"x-forwarded-for": forwarded} if forwarded is not None else {}
        self.client = type("C", (), {"host": peer})() if peer is not None else None


def test_the_forwarded_address_is_preferred_over_the_socket_peer():
    req = _Req(forwarded="203.0.113.9", peer="169.254.8.1")
    assert app_module._client_ip(req) == "203.0.113.9"


def test_a_forged_leading_entry_cannot_become_the_rate_limit_key():
    """The whole point. Cloud Run appends the real address to whatever the
    caller already sent, so "203.0.113.9, 198.51.100.4" means the client
    claimed 203.0.113.9 and Google observed 198.51.100.4. Taking the
    leftmost -- which is what uvicorn's own --forwarded-allow-ips="*"
    does -- would let one attacker mint a fresh daily quota per request.
    """
    req = _Req(forwarded="203.0.113.9, 198.51.100.4", peer="169.254.8.1")
    assert app_module._client_ip(req) == "198.51.100.4"


def test_many_forged_entries_still_resolve_to_the_last_one():
    req = _Req(forwarded="1.1.1.1, 2.2.2.2, 3.3.3.3, 198.51.100.4")
    assert app_module._client_ip(req) == "198.51.100.4"


def test_the_socket_peer_is_used_when_nothing_is_forwarded():
    """Local dev: uvicorn --reload with no proxy in front, where the peer
    genuinely is the visitor.
    """
    assert app_module._client_ip(_Req(peer="127.0.0.1")) == "127.0.0.1"


def test_ipv6_is_handled():
    req = _Req(forwarded="2001:db8::1")
    assert app_module._client_ip(req) == "2001:db8::1"


@pytest.mark.parametrize(
    "forwarded",
    [
        "not-an-ip",
        "../../admin",
        "a/b",
        "",
        "   ",
        "<script>alert(1)</script>",
    ],
)
def test_a_value_that_is_not_an_ip_never_becomes_a_key(forwarded):
    """These values become Firestore document IDs in `_quota_refs`
    ("ip_<value>_<day>"). A document ID must never be something a header
    can shape -- a "/" alone makes the path invalid and turns a rate-limit
    check into a 500.
    """
    assert app_module._client_ip(_Req(forwarded=forwarded)) == "unknown"


def test_garbage_in_the_forwarded_chain_falls_through_to_a_real_address():
    req = _Req(forwarded="198.51.100.4, not-an-ip")
    assert app_module._client_ip(req) == "198.51.100.4"


def test_no_client_and_no_header_is_not_an_error():
    assert app_module._client_ip(_Req()) == "unknown"


def test_the_address_is_normalized_so_two_spellings_share_one_counter():
    """"2001:db8::0:1" and "2001:db8::1" are the same address; without
    normalization they would be two separate quota documents.
    """
    a = app_module._client_ip(_Req(forwarded="2001:db8::0:1"))
    b = app_module._client_ip(_Req(forwarded="2001:db8::1"))
    assert a == b


# --- the routes actually use it --------------------------------------------


@pytest.fixture
def client() -> TestClient:
    return TestClient(app_module.app)


def test_the_feedback_quota_is_keyed_on_the_forwarded_address(client, monkeypatch):
    seen: list[str] = []
    monkeypatch.setattr(
        fs, "check_and_reserve_feedback_quota", lambda ip: (seen.append(ip), (True, ""))[1]
    )
    monkeypatch.setattr(fs, "save_feedback", lambda *a, **k: None)
    monkeypatch.setattr(fs, "get_job", lambda _j: None)

    client.post(
        "/feedback",
        data={"rating": 5, "form_ts": time.time() - 10, "website": ""},
        headers={"X-Forwarded-For": "203.0.113.9, 198.51.100.4"},
        follow_redirects=False,
    )
    assert seen == ["198.51.100.4"]


def test_the_scan_quota_is_keyed_on_the_forwarded_address(monkeypatch):
    seen: list[str] = []
    monkeypatch.setattr(
        fs,
        "check_and_reserve_scan_quota",
        lambda email, ip: (seen.append(ip), (False, "nope"))[1],
    )

    import asyncio

    asyncio.run(
        app_module._start_scan(
            _Req(forwarded="203.0.113.9, 198.51.100.4", peer="169.254.8.1"),
            "https://example.com/",
            "a@b.com",
        )
    )
    assert seen == ["198.51.100.4"]
