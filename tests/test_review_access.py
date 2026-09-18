"""F1: the admin review queue must fail CLOSED.

`_is_reviewer` was `not _REVIEW_CODE or cookie == _REVIEW_CODE`, so with
MAD_REVIEW_CODE unset it returned True for everyone. That gates /review
(every pending escalation across every scan and every user) and
/review/{id}/resolve, whose learned_pattern branch permanently changes
what Editor flags on every future scan for everyone -- the one flow
DECISIONS_LOG.md says must keep a human gate.

It is currently set in Secret Manager, so this was latent rather than
live. One missing secret binding -- a new revision, a rotation, a fork,
local dev -- and it would not have been.
"""

from __future__ import annotations

import pytest

from mad_platform.web import app as web_app


class _FakeRequest:
    """_is_reviewer only ever reads request.cookies."""

    def __init__(self, **cookies):
        self.cookies = cookies


@pytest.fixture
def review_code(clean_env):
    clean_env.setenv("MAD_REVIEW_CODE", "s3cret-code")
    return "s3cret-code"


def test_unset_code_denies_everyone(clean_env):
    assert web_app._is_reviewer(_FakeRequest()) is False


def test_unset_code_denies_even_a_forged_cookie(clean_env):
    assert web_app._is_reviewer(_FakeRequest(mad_review_session="anything")) is False


def test_unset_code_denies_an_empty_cookie(clean_env):
    """The old code also wrote `_REVIEW_CODE or ""` into the cookie, so an
    empty-string session used to compare equal to an unset code.
    """
    assert web_app._is_reviewer(_FakeRequest(mad_review_session="")) is False


def test_no_cookie_is_denied(review_code):
    assert web_app._is_reviewer(_FakeRequest()) is False


def test_wrong_cookie_is_denied(review_code):
    assert web_app._is_reviewer(_FakeRequest(mad_review_session="wrong")) is False


def test_correct_session_value_is_allowed(review_code):
    session = web_app._review_session_value(review_code)
    assert web_app._is_reviewer(_FakeRequest(mad_review_session=session)) is True


def test_the_raw_code_is_not_a_valid_session_value(review_code):
    """The cookie carries a value derived from the code, not the code
    itself, so the shared secret never leaves the server.
    """
    assert web_app._is_reviewer(_FakeRequest(mad_review_session=review_code)) is False


def test_session_value_is_deterministic_and_code_specific():
    assert web_app._review_session_value("a") == web_app._review_session_value("a")
    assert web_app._review_session_value("a") != web_app._review_session_value("b")


def test_rotating_the_code_invalidates_old_sessions(clean_env):
    clean_env.setenv("MAD_REVIEW_CODE", "old")
    old_session = web_app._review_session_value("old")
    clean_env.setenv("MAD_REVIEW_CODE", "new")
    assert web_app._is_reviewer(_FakeRequest(mad_review_session=old_session)) is False


def test_review_code_is_read_at_call_time_not_import_time(clean_env):
    """A module-level read would have frozen whatever the environment held
    when app.py was first imported -- and is also what made this module
    impossible to import in a test at all.
    """
    assert web_app._is_reviewer(_FakeRequest()) is False
    clean_env.setenv("MAD_REVIEW_CODE", "now-set")
    session = web_app._review_session_value("now-set")
    assert web_app._is_reviewer(_FakeRequest(mad_review_session=session)) is True
