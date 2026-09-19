"""The one place this project's logging is configured.

Not `logging.basicConfig()`. uvicorn configures its own logging on
startup, which runs *after* these modules are imported, and silently drops
INFO-level output from our own loggers on a cold start if we rely on
basicConfig alone -- confirmed in production, where phase logs vanished on
cold-started instances while uvicorn's own access logs kept working.
Attaching a handler directly to the "mad_platform" namespace, independent
of the root logger uvicorn manages, survives that.

This existed as an identical seven-line block, comment and all, in
`web/app.py`, `web/worker_app.py` and `web/poller_app.py`. That is the
same duplicate-constant shape this codebase has deliberately eliminated
elsewhere (severity.py, theme.LIGHT_HEX, untrusted.HTML_EXCERPT_CHARS,
config.FIRESTORE_DATABASE) -- and the drift had already happened:
`mine_patterns.py` had no copy at all, so the pattern miner's own
logger.info and notify output depended on whatever the default root
logger happened to do in a Cloud Run Job. It now calls this too.

Imports nothing from this package, so any entry point can call it first.
"""

from __future__ import annotations

import logging

NAMESPACE = "mad_platform"
_FORMAT = "%(levelname)s:%(name)s:%(message)s"


def configure_logging(level: int = logging.INFO) -> logging.Logger:
    """Attaches one StreamHandler to the "mad_platform" logger and returns
    it. Idempotent: calling it twice does not double every log line, which
    matters because an entry point that imports another one would
    otherwise do exactly that.
    """
    logger = logging.getLogger(NAMESPACE)
    logger.setLevel(level)
    logger.propagate = False
    if not any(isinstance(h, logging.StreamHandler) for h in logger.handlers):
        handler = logging.StreamHandler()
        handler.setFormatter(logging.Formatter(_FORMAT))
        logger.addHandler(handler)
    return logger
