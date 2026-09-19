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


# --- F11: config is read through config.py, not at module scope ----------


def test_no_module_reads_the_environment_at_import_time():
    """config.py's docstring is explicit -- "Reading config must not happen
    at import time. Every value is behind a *function*, not a module-level
    constant" -- and four modules still did it, including one added with
    the SEO work for a variable config.app_base_url() already owned.

    The import-time part was mild (they all had defaults, so nothing
    crashed). The real cost was drift: MAD_APP_BASE_URL had three readers
    with three different behaviours, so a deployment that set it with a
    trailing slash produced "https://host.com//review/..." from the
    pattern miner and correct URLs from the other two.

    Parses the AST rather than grepping, so a comment mentioning
    os.environ.get -- and several now do, describing this bug -- does not
    register as a violation.
    """
    import ast
    import pathlib

    root = pathlib.Path(__file__).parents[1] / "mad_platform"
    offenders = []
    for path in sorted(root.rglob("*.py")):
        if path.name == "config.py":
            continue  # the one module whose job this is
        tree = ast.parse(path.read_text())
        for node in tree.body:
            # Module scope only. A read inside a def is exactly what
            # config.py asks for -- deferred to call time -- so descending
            # into function bodies would flag the correct pattern.
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                continue
            for sub in ast.walk(node):
                if not isinstance(sub, ast.Attribute) or sub.attr != "environ":
                    continue
                if isinstance(sub.value, ast.Name) and sub.value.id == "os":
                    offenders.append(f"{path.relative_to(root)}:{sub.lineno}")
    assert not offenders, (
        "module-level os.environ access outside config.py: " + ", ".join(offenders)
    )


def test_the_production_origin_is_one_literal_not_several():
    """The origin used for building URLs had become a literal in three
    Python files, with no test pinning them together -- which is how the
    three of them ended up disagreeing about the trailing slash.

    Only the URL form is checked. "hello@mad-platform.org" in the FAQ copy
    and the privacy page is a contact address a reader is meant to see, not
    a base URL anything is constructed from.
    """
    import ast
    import pathlib

    root = pathlib.Path(__file__).parents[1] / "mad_platform"
    hits = []
    for path in sorted(root.rglob("*.py")):
        if path.name == "config.py":
            continue
        tree = ast.parse(path.read_text())
        for node in ast.walk(tree):
            if isinstance(node, ast.Constant) and isinstance(node.value, str):
                if "//mad-platform.org" in node.value:
                    hits.append(f"{path.relative_to(root)}:{node.lineno}")
    assert not hits, (
        "the production origin belongs in config.DEFAULT_CANONICAL_ORIGIN, not in: "
        + ", ".join(hits)
    )


# --- X3: one logging bootstrap, not three copies and one omission -------


def test_the_logging_bootstrap_exists_in_exactly_one_place():
    """The identical seven-line handler setup, comment and all, lived in
    web/app.py, web/worker_app.py and web/poller_app.py -- and
    mine_patterns.py had no copy at all, so the pattern miner's output in
    a Cloud Run Job depended on whatever the default root logger did.
    """
    import pathlib

    root = pathlib.Path(__file__).parents[1]
    marker = 'logging.getLogger("mad_platform")'
    offenders = [
        str(path.relative_to(root))
        for path in list((root / "mad_platform").rglob("*.py")) + list(root.glob("*.py"))
        if path.name != "logging_setup.py" and marker in path.read_text()
    ]
    assert not offenders, offenders


@pytest.mark.parametrize(
    "entry_point",
    [
        "mad_platform.web.app",
        "mad_platform.web.worker_app",
        "mad_platform.web.poller_app",
    ],
)
def test_every_service_entry_point_configures_logging(entry_point):
    import pathlib

    root = pathlib.Path(__file__).parents[1]
    source = (root / (entry_point.replace(".", "/") + ".py")).read_text()
    assert "configure_logging()" in source


def test_the_pattern_miner_entry_point_configures_logging_too():
    """It is the one that did not, which is the drift that made this worth
    consolidating rather than just deduplicating.
    """
    import pathlib

    source = (pathlib.Path(__file__).parents[1] / "mine_patterns.py").read_text()
    assert "configure_logging()" in source


def test_configuring_twice_does_not_duplicate_every_log_line():
    """An entry point that imports another one would otherwise attach two
    handlers and emit everything twice.
    """
    from mad_platform.logging_setup import configure_logging

    logger = configure_logging()
    before = len(logger.handlers)
    configure_logging()
    assert len(logger.handlers) == before
