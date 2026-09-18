"""Shared test fixtures.

The important thing about this file is what it does NOT need to do: no
GCP credentials, no emulator, no network. Every module under test now
builds its clients lazily (see mad_platform/config.py), so importing them
is free and a test only has to avoid calling the handful of functions that
actually talk to Firestore/GCS/Vertex.
"""

from __future__ import annotations

import pathlib

import pytest

FIXTURES = pathlib.Path(__file__).parent / "fixtures"


@pytest.fixture
def violations_html() -> str:
    """The deliberately-broken page in tests/fixtures/ -- missing alt,
    skipped heading level, unlabeled input, positive tabindex,
    aria-hidden on a focusable button, a dangling aria-labelledby.
    """
    return (FIXTURES / "violations.html").read_text()


@pytest.fixture
def clean_env(monkeypatch: pytest.MonkeyPatch) -> pytest.MonkeyPatch:
    """No configuration at all -- what a fork, a fresh shell or a
    misconfigured revision looks like. Tests use this to assert that the
    code refuses to guess (S1) rather than falling back to another
    project's resources.
    """
    for name in (
        "GOOGLE_CLOUD_PROJECT",
        "GCS_BUCKET_NAME",
        "MAD_APP_BASE_URL",
        "SCAN_WORKER_URL",
        "SCAN_QUEUE_INVOKER_SA",
        "MAD_REVIEW_CODE",
    ):
        monkeypatch.delenv(name, raising=False)
    return monkeypatch
