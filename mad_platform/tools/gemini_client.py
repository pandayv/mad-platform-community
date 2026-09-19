"""Thin wrapper around Vertex AI's Gemini client.

location='global' is required, not a specific region like 'us-central1':
some models appear in a region's catalog listing but 404 when actually
called there.

gemini-3.5-flash-lite for high-volume calls, gemini-3.7-flash for the
handful of judgment calls. No Pro-tier model exists at the "Gemini 3.5+"
floor this project targets, so the tiering is Flash-lite vs. Flash rather
than the more typical Flash-vs-Pro split.
"""

from __future__ import annotations

from functools import lru_cache

from google import genai
from google.genai import types

from mad_platform import config
from mad_platform.tools import retry

FLASH_LITE = "gemini-3.5-flash-lite"
FLASH = "gemini-3.7-flash"
EMBEDDING_MODEL = "gemini-embedding-001"

# Every call has an explicit timeout and one bounded retry -- no call in
# this pipeline can hang or fail silently.
_TIMEOUT_MS = 60_000
_MAX_ATTEMPTS = 2


@lru_cache(maxsize=1)
def get_client() -> genai.Client:
    """Lazily-built, process-wide Vertex AI client. Not at import time:
    see firestore_client.get_client's docstring. The project ID comes from
    config with no fallback -- the old default named the hackathon project.
    """
    return genai.Client(
        vertexai=True,
        project=config.project_id(),
        location=config.VERTEX_LOCATION,
        http_options=types.HttpOptions(timeout=_TIMEOUT_MS),
    )


# client_for_key() and PRO_MODEL used to sit here: the foundation for
# bring-your-own-Gemini-key, never wired into analyst/editor/reporter or
# the /scan form, as the comment on them said outright. An unused,
# untested key-handling path that accepts a visitor-supplied credential is
# a security-adjacent surface sitting in four deployed container images
# for no benefit. The design is recorded in DECISIONS_LOG.md, which is
# where an intention belongs until there is code that uses it.


def _with_retry(call, label: str = "gemini"):
    """One retry on a transient failure (including a timeout) -- bounded,
    not a loop, matching the retry pattern already used for page fetches
    (crawler.py) and the Orchestrator's own retry gate. A second failure
    is a real problem and should surface, not be swallowed.

    The policy itself lives in tools/retry.py now, shared with adk_client
    and crawler, because this loop used to retry *everything*: a 400 from a
    malformed prompt and a 403 from a missing IAM binding cost twice as
    much and took twice as long to report a failure that could not have
    gone any other way, and a 429 was retried after 1.5s, which is far too
    soon to help and adds to the pressure that caused it.
    """
    return retry.with_retry(call, attempts=_MAX_ATTEMPTS, label=label)


# generate_structured() used to sit here too, and it is the deletion that
# matters most: a second function with the same name and signature as the
# live one in tools/adk_client.py, differing only in retry label and
# transport, with no callers at all -- every caller imports the ADK one.
# An autocomplete away from being imported by mistake, at which point a
# judgment call would silently stop going through the agent Runner the
# rest of the pipeline uses.
#
# What remains in this module is what is genuinely used: get_client, embed,
# embed_batch and the model-name constants. Embeddings stay on the raw SDK
# deliberately -- ADK has no embedding-agent primitive, because computing a
# vector is not a judgment call (see adk_client's docstring).


def embed(text: str) -> list[float]:
    def _call():
        result = get_client().models.embed_content(model=EMBEDDING_MODEL, contents=text)
        return list(result.embeddings[0].values)

    return _with_retry(_call, label="embed")


def embed_batch(texts: list[str]) -> list[list[float]]:
    """Embeds many texts in one API call instead of one call per text --
    the embedding model accepts a list natively, so grounding N findings
    against the WCAG corpus costs one call, not N.
    """
    if not texts:
        return []

    def _call():
        result = get_client().models.embed_content(model=EMBEDDING_MODEL, contents=texts)
        return [list(e.values) for e in result.embeddings]

    return _with_retry(_call, label=f"embed_batch({len(texts)})")
