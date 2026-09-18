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
from typing import TypeVar

from google import genai
from google.genai import types
from pydantic import BaseModel

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


T = TypeVar("T", bound=BaseModel)


def client_for_key(api_key: str | None):
    """Bring-your-own-Gemini-key support (see DECISIONS_LOG.md): when a
    visitor supplies their own Gemini API key, route that request through
    the plain Developer API surface instead of this project's own Vertex
    AI billing, using PRO_MODEL below instead of the free tier's
    Flash/Flash-lite. Same google-genai SDK either way, just a different
    auth mode -- this is the one place that distinction lives.

    Returns the shared Vertex-authenticated client when api_key is falsy
    (the normal, free-tier path unaffected). The caller is responsible for
    never persisting api_key anywhere (no Firestore, no logs) -- it should
    live only in memory for the one request that supplied it.

    Not yet wired into analyst.py/editor.py/reporter.py's call sites or
    the /scan form -- this is the foundation, the per-agent threading is
    still open, logged for follow-up rather than rushed into the core
    pipeline right before its first real deployment.
    """
    if not api_key:
        return get_client()
    return genai.Client(api_key=api_key, http_options=types.HttpOptions(timeout=_TIMEOUT_MS))


PRO_MODEL = "gemini-3.7-pro"  # only reachable via a user-supplied key, see client_for_key() above


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


def generate_structured(
    model: str,
    prompt: str,
    schema: type[T],
    image_bytes: bytes | None = None,
) -> T:
    """One structured-output Gemini call. Raises on malformed responses
    rather than returning something a caller might silently misuse --
    every LLM call in this pipeline is schema-validated, not free text.
    """
    parts: list = []
    if image_bytes:
        parts.append(types.Part.from_bytes(data=image_bytes, mime_type="image/png"))
    parts.append(prompt)

    def _call():
        response = get_client().models.generate_content(
            model=model,
            contents=parts,
            config=types.GenerateContentConfig(
                response_mime_type="application/json",
                response_schema=schema,
            ),
        )
        return schema.model_validate_json(response.text)

    return _with_retry(_call, label=f"generate_structured({model})")


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
