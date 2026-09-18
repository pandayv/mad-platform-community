"""S1: no module may fall back to another GCP project's resources.

The regression these guard against is specific and was live: five modules
carried `os.environ.get("GOOGLE_CLOUD_PROJECT", "project-d7e6174e-cca7-4d16-9d5")`
-- the *hackathon* project's ID, which CLAUDE.md names as a hard boundary
this code must never touch -- plus a bucket default pointing into it. With
the variable unset the clients constructed fine and quietly read and wrote
there.

So these tests assert two things: that an unset variable raises, and that
the specific forbidden strings appear nowhere in the package at all. The
second one is the durable guard: it fails no matter which new file
someone re-introduces the fallback in.
"""

from __future__ import annotations

import pathlib

import pytest

from mad_platform import config

# The two values that must never appear in this codebase again.
_HACKATHON_PROJECT_ID = "project-d7e6174e-cca7-4d16-9d5"
_HACKATHON_BUCKET = "scan-storage-9747"

_PACKAGE_ROOT = pathlib.Path(__file__).resolve().parent.parent / "mad_platform"


@pytest.mark.parametrize(
    "getter",
    [
        config.project_id,
        config.gcs_bucket_name,
        config.app_base_url,
        config.scan_worker_url,
        config.scan_queue_invoker_sa,
    ],
)
def test_required_config_raises_when_unset(clean_env, getter):
    with pytest.raises(config.MissingConfigError):
        getter()


def test_error_message_names_the_variable(clean_env):
    with pytest.raises(config.MissingConfigError, match="GOOGLE_CLOUD_PROJECT"):
        config.project_id()


def test_required_config_returns_the_env_value(clean_env):
    clean_env.setenv("GOOGLE_CLOUD_PROJECT", "mad-platform-community-10681")
    clean_env.setenv("GCS_BUCKET_NAME", "mad-platform-community-10681-reports")
    assert config.project_id() == "mad-platform-community-10681"
    assert config.gcs_bucket_name() == "mad-platform-community-10681-reports"


def test_empty_string_counts_as_unset(clean_env):
    """An env var set to "" is a misconfiguration, not a project name --
    `os.environ[...]` would happily have returned it.
    """
    clean_env.setenv("GOOGLE_CLOUD_PROJECT", "")
    with pytest.raises(config.MissingConfigError):
        config.project_id()


def test_optional_queue_settings_have_defaults(clean_env):
    assert config.scan_queue_location() == "us-central1"
    assert config.scan_queue_name() == "scan-queue"


def test_review_code_is_none_when_unset(clean_env):
    assert config.review_code() is None


@pytest.mark.parametrize("forbidden", [_HACKATHON_PROJECT_ID, _HACKATHON_BUCKET])
def test_no_source_file_references_the_hackathon_project(forbidden):
    offenders = [
        str(path.relative_to(_PACKAGE_ROOT))
        for path in _PACKAGE_ROOT.rglob("*.py")
        if forbidden in path.read_text()
    ]
    assert offenders == [], (
        f"{forbidden!r} is the hackathon project's resource name and must never appear "
        f"in this codebase (CLAUDE.md hard boundary). Found in: {offenders}"
    )


def test_validate_web_config_raises_when_incomplete(clean_env):
    clean_env.setenv("GOOGLE_CLOUD_PROJECT", "mad-platform-community-10681")
    # SCAN_WORKER_URL / SCAN_QUEUE_INVOKER_SA still unset.
    with pytest.raises(config.MissingConfigError):
        config.validate_web_config()


def test_validate_pipeline_config_passes_when_complete(clean_env):
    clean_env.setenv("GOOGLE_CLOUD_PROJECT", "mad-platform-community-10681")
    clean_env.setenv("GCS_BUCKET_NAME", "mad-platform-community-10681-reports")
    clean_env.setenv("MAD_APP_BASE_URL", "https://mad-platform.org")
    config.validate_pipeline_config()  # must not raise
