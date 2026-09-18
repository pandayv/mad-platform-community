"""X2: the severity donut exists twice and the two copies must agree.

`theme.severity_donut_svg` (Python, used by the stored report) and
`donutSvg` in the status page's JavaScript are two full reimplementations
of the same SVG. They render the same scan, and a visitor sees both --
the live status page, then the report it links to. Someone restyling one
has to port the change by hand, and nothing notices when they don't.

The thorough fix is to render the completed state server-side and have the
status page fetch it, which is a real refactor of a page that is currently
working. The pragmatic one is this: execute the JavaScript and diff its
output against Python's for the same input, so a divergence fails a test
instead of shipping two different-looking charts.

Skipped when node is unavailable, so this never becomes a reason the suite
cannot run.
"""

from __future__ import annotations

import json
import pathlib
import re
import shutil
import subprocess

import pytest

from mad_platform.severity import SEVERITY_ORDER
from mad_platform.web import app as app_module
from mad_platform.web import theme

pytestmark = pytest.mark.skipif(shutil.which("node") is None, reason="node is not installed")


def _js_function(name: str) -> str:
    """Pulls one top-level `function name(...) { ... }` out of the status
    page template by brace matching.
    """
    source = app_module._STATUS_PAGE
    start = source.index(f"function {name}(")
    depth = 0
    for i in range(source.index("{", start), len(source)):
        if source[i] == "{":
            depth += 1
        elif source[i] == "}":
            depth -= 1
            if depth == 0:
                return source[start : i + 1]
    raise AssertionError(f"could not find the end of function {name}")  # pragma: no cover


def _run_js(counts: dict[str, int]) -> str:
    script = "\n".join(
        [
            f"const SEV_ORDER = {json.dumps(list(SEVERITY_ORDER))};",
            f"const SEV_VAR = {json.dumps(theme.SEVERITY_VAR)};",
            _js_function("donutSvg"),
            f"process.stdout.write(donutSvg({json.dumps(counts)}));",
        ]
    )
    result = subprocess.run(
        ["node", "-e", script], capture_output=True, text=True, timeout=30, check=True
    )
    return result.stdout


def _normalize(svg: str) -> str:
    """Whitespace and attribute-order noise between two hand-written
    templates is not the divergence worth failing on; the numbers, colors
    and structure are.
    """
    return re.sub(r"\s+", " ", svg).strip()


@pytest.mark.parametrize(
    "counts",
    [
        {"critical": 0, "high": 0, "medium": 0, "low": 0},
        {"critical": 2, "high": 5, "medium": 8, "low": 3},
        {"critical": 1, "high": 0, "medium": 0, "low": 0},
        {"critical": 0, "high": 3, "medium": 0, "low": 4},
        {"critical": 7, "high": 7, "medium": 7, "low": 7},
    ],
)
def test_the_python_and_javascript_donuts_render_the_same_svg(counts):
    assert _normalize(_run_js(counts)) == _normalize(theme.severity_donut_svg(counts))


def test_both_donuts_use_the_shared_palette_and_order():
    """The JS reads SEV_ORDER/SEV_VAR injected from Python now, so this
    pins that they are still injected rather than retyped.
    """
    source = pathlib.Path(app_module.__file__).read_text()
    assert "__SEV_ORDER__" in source and "__SEV_VAR__" in source
    js = _run_js({"critical": 1, "high": 1, "medium": 1, "low": 1})
    for token in theme.SEVERITY_VAR.values():
        assert token in js
