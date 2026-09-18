"""B3: the scan quota check-then-reserve must actually be atomic.

The docstring claimed atomicity; the code did all three reads first and
all three writes last with nothing tying them together, so N concurrent
requests could all read "under the limit" and all proceed. This is the
only thing between a public, unauthenticated endpoint and unbounded
Gemini/Playwright spend, on a service running at concurrency 20 across
multiple instances -- a live race, not a theoretical one.

A true concurrency test needs the Firestore emulator, which this suite
deliberately does not require. What is testable without one is the shape:
that the reserve body is genuinely wrapped in a Firestore transaction (so
removing the decorator fails a test rather than silently restoring the
race), and that the quota identity it keys on is what it should be.
"""

from __future__ import annotations

import inspect
import pathlib

import pytest
from google.cloud.firestore_v1 import transaction as firestore_transaction

from mad_platform.state import firestore_client as fs


def test_reserve_body_is_wrapped_in_a_firestore_transaction():
    """`@firestore.transactional` turns the function into a _Transactional
    object. If someone unwraps it, the reads and writes stop being
    serializable and the burst race is back.
    """
    assert isinstance(fs._check_and_reserve, firestore_transaction._Transactional)


def test_public_entry_point_passes_a_real_transaction(monkeypatch):
    """Guards the wiring, not just the decorator: the transaction handed
    to the reserve body has to come from the live client.
    """
    captured = {}

    class _FakeTransaction:
        pass

    class _FakeClient:
        def transaction(self):
            return _FakeTransaction()

    class _FakeUsage:
        def document(self, name):
            return name  # the ref is opaque to this test

    monkeypatch.setattr(fs, "get_client", _FakeClient)
    monkeypatch.setattr(fs, "_usage", _FakeUsage)

    def fake_reserve(transaction, month_ref, email_ref, ip_ref, now):
        captured.update(
            transaction=transaction, month=month_ref, email=email_ref, ip=ip_ref
        )
        return True, ""

    monkeypatch.setattr(fs, "_check_and_reserve", fake_reserve)
    allowed, reason = fs.check_and_reserve_scan_quota("Me@Example.com", "203.0.113.9")

    assert (allowed, reason) == (True, "")
    assert isinstance(captured["transaction"], _FakeTransaction)
    assert captured["month"].startswith("month_")
    assert captured["email"].startswith("email_me@example.com_")
    assert captured["ip"].startswith("ip_203.0.113.9_")


def test_all_reads_precede_all_writes_in_the_transaction_body():
    """Firestore requires it, and violating it raises only at runtime
    under real contention -- the worst possible time to find out.
    """
    source = inspect.getsource(fs._check_and_reserve.to_wrap)
    last_read = max(source.index(".get(transaction=transaction)"), source.rindex(".get(transaction=transaction)"))
    first_write = source.index("transaction.set(")
    assert last_read < first_write


# --- the quota identity ----------------------------------------------------


def test_gmail_dots_and_plus_aliases_share_one_quota():
    """Otherwise "me+1@gmail.com", "me+2@gmail.com", ... each get a fresh
    daily allowance from a single real mailbox.
    """
    assert fs.quota_email_key("m.e+1@gmail.com") == fs.quota_email_key("me@gmail.com")
    assert fs.quota_email_key("M.E+scan@googlemail.com") == fs.quota_email_key("me@googlemail.com")


def test_plus_suffix_is_stripped_for_every_domain():
    assert fs.quota_email_key("me+scan@example.com") == fs.quota_email_key("me@example.com")


def test_dots_are_kept_for_non_gmail_domains():
    """Only Gmail actually ignores dots; stripping them elsewhere would
    merge two genuinely different mailboxes into one quota.
    """
    assert fs.quota_email_key("m.e@example.com") != fs.quota_email_key("me@example.com")


def test_case_and_whitespace_are_normalized():
    assert fs.quota_email_key("  Me@Example.COM ") == "me@example.com"


def test_limits_are_defined_in_one_place():
    for limit in (
        fs.max_scans_per_email_per_day(),
        fs.max_scans_per_ip_per_day(),
        fs.max_scans_per_month(),
    ):
        assert isinstance(limit, int) and limit > 0


# --- B7: the limits are policy in source, tunable by environment -----------


def test_per_ip_default_is_the_intended_five_not_the_benchmark_fifteen(monkeypatch):
    """The regression this guards: MAX_SCANS_PER_IP_PER_DAY was raised to
    15 for a benchmark run under a "revert to 5 after" comment and shipped
    that way. A temporary ceiling now lives in an env var on one revision,
    never in source.
    """
    monkeypatch.delenv("MAD_MAX_SCANS_PER_IP_PER_DAY", raising=False)
    assert fs.max_scans_per_ip_per_day() == 5


def test_no_source_file_carries_a_temporary_limit_marker():
    """A comment saying the value is wrong is not a control -- if one shows
    up again next to a limit, it means the env var was bypassed.
    """
    source = pathlib.Path(fs.__file__).read_text()
    for line in source.splitlines():
        if "MAX_SCANS" in line and "TEMP" in line.upper():
            raise AssertionError(f"temporary limit override left in source: {line.strip()}")


def test_environment_overrides_the_limit(monkeypatch):
    monkeypatch.setenv("MAD_MAX_SCANS_PER_IP_PER_DAY", "15")
    assert fs.max_scans_per_ip_per_day() == 15


@pytest.mark.parametrize("bad", ["", "abc", "0", "-3", "5.5"])
def test_an_unusable_override_falls_back_to_the_default(monkeypatch, bad):
    """A typo'd env var must not be able to set an effective limit of 0
    (nobody can scan) or a negative one (nobody is limited).
    """
    monkeypatch.setenv("MAD_MAX_SCANS_PER_IP_PER_DAY", bad)
    assert fs.max_scans_per_ip_per_day() == 5


def test_limits_are_read_at_call_time_not_frozen_at_import(monkeypatch):
    monkeypatch.setenv("MAD_MAX_SCANS_PER_MONTH", "7")
    assert fs.max_scans_per_month() == 7
    monkeypatch.delenv("MAD_MAX_SCANS_PER_MONTH")
    assert fs.max_scans_per_month() == 500
