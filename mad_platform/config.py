"""The single place required environment configuration is read.

Two rules this module exists to enforce, both of which were previously
violated in five separate files:

1. **No fallback default for anything that names a cloud resource.** The
   modules that construct GCP clients each used to carry
   `os.environ.get("GOOGLE_CLOUD_PROJECT", <the original hackathon
   project's ID>)`, plus a bucket default pointing at that same project's
   bucket. (The literal values are recorded in CODE_REVIEW_FINDINGS.md S1
   and deliberately not repeated here -- tests/test_config.py asserts they
   appear nowhere under mad_platform/, and that guard is only absolute if
   it has no exceptions.) With the variable unset (a local run, a
   developer shell, a revision missing the env var, a fork of the public
   repo) the client constructed fine and quietly read and wrote another
   project's data. `CLAUDE.md` names that project as a hard boundary this
   codebase must never touch. A missing variable must be an error, never a
   guess -- so every getter here raises `MissingConfigError` naming the
   variable rather than substituting anything.

2. **Reading config must not happen at import time.** Every value is
   behind a *function*, not a module-level constant. A module-level read
   makes `import mad_platform.web.app` fail without a configured
   environment, which is what made this codebase untestable (see
   CODE_REVIEW_FINDINGS.md T1/S2) -- the import cost the same as a live
   GCP deployment. Lazy reads keep import side-effect-free while still
   failing loudly the first time a value is actually needed.

   Fail-at-startup for a misconfigured deployment (the property the old
   import-time `os.environ[...]` reads in app.py were protecting) is kept
   deliberately, by `validate_web_config()` running from the FastAPI
   lifespan hook -- startup, not import.

Deliberately not cached: tests monkeypatch `os.environ`, and an env read
is free. Caching belongs on the *clients* built from these values, not on
the values themselves.
"""

from __future__ import annotations

import os

# The Firestore database name is a constant, not configuration -- it is
# baked into where this project's data lives, and getting it wrong
# silently connects to an empty '(default)' database (see
# firestore_client's module docstring). It lives here so the two modules
# that open a client cannot drift apart on it.
FIRESTORE_DATABASE = "scan-firestore"

# Vertex AI location. 'global', not a region: some models appear in a
# region's catalog listing but 404 when actually called there.
VERTEX_LOCATION = "global"

_QUEUE_LOCATION_DEFAULT = "us-central1"
_QUEUE_NAME_DEFAULT = "scan-queue"


class MissingConfigError(RuntimeError):
    """A required environment variable is unset.

    Deliberately not KeyError: a KeyError from deep inside a client
    constructor reads as a bug in the SDK. This names the variable and
    says what sets it.
    """


def _require(name: str, purpose: str) -> str:
    value = os.environ.get(name)
    if not value:
        raise MissingConfigError(
            f"{name} is not set. {purpose} There is deliberately no default: "
            f"a wrong default here points this code at another GCP project. "
            f"See README.md for the deployment env vars."
        )
    return value


def project_id() -> str:
    return _require("GOOGLE_CLOUD_PROJECT", "It is the GCP project every client in this app connects to.")


def gcs_bucket_name() -> str:
    return _require("GCS_BUCKET_NAME", "It is the bucket generated reports are written to.")


def app_base_url() -> str:
    """The app's own public base URL, used to build report and review links
    that get emailed to real people. A stale hardcoded default here sends a
    user to someone else's deployment, so this fails loudly instead.
    """
    return _require("MAD_APP_BASE_URL", "It is the base of every report/review link emailed to a user.")


def scan_worker_url() -> str:
    return _require("SCAN_WORKER_URL", "It is the scan-worker service Cloud Tasks dispatches scans to.")


def scan_queue_invoker_sa() -> str:
    return _require("SCAN_QUEUE_INVOKER_SA", "It is the service account Cloud Tasks uses to call the worker.")


def scan_queue_location() -> str:
    """Optional: the queue's own region, which is not necessarily the
    project's. Defaulted rather than required because a wrong value here
    fails immediately and visibly (the queue path simply does not exist),
    unlike a wrong project ID, which succeeds against the wrong data.
    """
    return os.environ.get("SCAN_QUEUE_LOCATION") or _QUEUE_LOCATION_DEFAULT


def scan_queue_name() -> str:
    return os.environ.get("SCAN_QUEUE_NAME") or _QUEUE_NAME_DEFAULT


def review_code() -> str | None:
    """The admin review queue's shared access code. Optional here on
    purpose -- unset is a valid state, it just means the queue is closed.
    `app._is_reviewer` fails *closed* on None; see its comment.
    """
    return os.environ.get("MAD_REVIEW_CODE") or None


def validate_web_config() -> None:
    """Fails loudly if anything the public web service needs is unset.

    Called from app.py's startup lifespan, not at import: a revision
    deployed without its queue configuration should refuse to serve (the
    behavior the old import-time reads gave us), but importing the module
    -- which a test does -- must not require a configured environment.
    """
    project_id()
    scan_worker_url()
    scan_queue_invoker_sa()


def validate_pipeline_config() -> None:
    """Same, for the scan worker: everything the pipeline needs before it
    starts doing billable work.
    """
    project_id()
    gcs_bucket_name()
    app_base_url()
