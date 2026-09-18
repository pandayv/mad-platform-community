"""S2/T1: importing any module must not need a configured GCP environment.

This is the test that unblocks every other test in this directory. Before
the lazy-client change, `import mad_platform.web.app` raised KeyError on
SCAN_WORKER_URL, `import mad_platform.agents.orchestrator` raised on
MAD_APP_BASE_URL, and anything touching firestore_client or rag attempted
live credential discovery at import time -- so there was no way to write a
unit test at all without a real GCP environment. That, not developer
neglect, was the reason this codebase had zero tests.

Each module is imported in a clean subprocess with every relevant
environment variable stripped, because an import that has already happened
in this process would not re-run its module body.
"""

from __future__ import annotations

import os
import subprocess
import sys

import pytest

MODULES = [
    "mad_platform.config",
    "mad_platform.state.firestore_client",
    "mad_platform.state.storage_client",
    "mad_platform.tools.gemini_client",
    "mad_platform.tools.rag",
    "mad_platform.tools.adk_client",
    "mad_platform.agents.orchestrator",
    "mad_platform.agents.reporter",
    "mad_platform.agents.editor",
    "mad_platform.agents.action_agent",
    "mad_platform.web.app",
    "mad_platform.web.worker_app",
]

_STRIPPED = (
    "GOOGLE_CLOUD_PROJECT",
    "GCS_BUCKET_NAME",
    "MAD_APP_BASE_URL",
    "SCAN_WORKER_URL",
    "SCAN_QUEUE_INVOKER_SA",
    "GOOGLE_APPLICATION_CREDENTIALS",
    "MAD_REVIEW_CODE",
)


@pytest.mark.parametrize("module", MODULES)
def test_imports_without_any_configuration(module):
    env = {k: v for k, v in os.environ.items() if k not in _STRIPPED}
    result = subprocess.run(
        [sys.executable, "-c", f"import {module}"],
        env=env,
        capture_output=True,
        text=True,
        timeout=180,
    )
    assert result.returncode == 0, (
        f"import {module} failed with no environment configured:\n{result.stderr}"
    )


def test_adk_client_import_does_not_mutate_environment():
    """adk_client used to os.environ.setdefault GOOGLE_CLOUD_PROJECT to the
    hackathon project's ID at import time -- so merely importing part of
    the agent pipeline silently pointed ADK at a project this repo must
    never touch.
    """
    env = {k: v for k, v in os.environ.items() if k not in _STRIPPED}
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            "import os; import mad_platform.tools.adk_client; "
            "print(os.environ.get('GOOGLE_CLOUD_PROJECT'))",
        ],
        env=env,
        capture_output=True,
        text=True,
        timeout=180,
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "None", (
        f"importing adk_client set GOOGLE_CLOUD_PROJECT to {result.stdout.strip()!r}"
    )
