"""Minimal RAG over the WCAG corpus -- grounds citations against the real
standard instead of the model's unaided claim. Freshness/refresh logic
lives separately in wcag_auto_heal.py; this module only embeds and
retrieves.

Storage is Firestore, not a dedicated vector database, since the corpus is
small (~18 curated criteria) and mostly static -- a dedicated always-on
vector index isn't justified at this size. Retrieval caches the embedded
corpus in memory after first load rather than re-fetching from Firestore
on every call -- an implementation detail, not a deviation from Firestore
being the durable source of truth. The cache revalidates itself against
the stored knowledge-base version on a timer (see _CACHE_TTL_SECONDS),
because the process that refreshes the corpus and the process that reads
it are in two different Cloud Run services.

Only retrieve_batch() is public. There used to be a single-query
retrieve() next to it with no callers anywhere; batching is what every
real caller wants (one embedding round trip for a whole page's findings
instead of one per finding), so the unused singular form is gone rather
than left as a second way to do the same thing more expensively.
"""

from __future__ import annotations

import logging
import math
import time

from mad_platform.data.wcag_corpus import WCAG_CORPUS, WCAGCriterion
from mad_platform.state.firestore_client import get_client as _get_firestore_client
from mad_platform.state.firestore_client import get_kb_version as _get_kb_version
from mad_platform.tools.gemini_client import embed as _embed
from mad_platform.tools.gemini_client import embed_batch as _embed_batch

logger = logging.getLogger("mad_platform.rag")


def _kb():
    """The WCAG knowledge-base collection, on the SAME Firestore client
    firestore_client.py uses. This module used to construct its own second
    firestore.Client against the same database at import time -- two
    connection pools and two auth flows for one database, and an import
    that could not happen without live credentials. There is one database,
    so there is one client; get it from the module that owns it.
    """
    return _get_firestore_client().collection("wcag_knowledge_base")


_cache: list[tuple[WCAGCriterion, list[float]]] | None = None
_cache_kb_version: str | None = None
_cache_checked_at: float = 0.0

# How long a warm process may serve its cached embeddings before
# re-checking which knowledge-base version they reflect.
#
# The cache had no invalidation of any kind across processes, and the two
# ends of that live in different services: embed_and_store_corpus() runs
# in scan-wcag-poller (via wcag_auto_heal.run_wcag_freshness_check) and
# resets `_cache` -- but only in the poller's own process. The consumer is
# editor.py -> retrieve_batch, in scan-worker. So after a WCAG refresh,
# every warm scan-worker instance went on grounding findings against the
# *old* embeddings until it happened to cold-start, with no signal, no TTL
# and no bound on how long a warm instance lives. The landing page
# advertises this refresh as a headline feature ("refreshes the ruleset
# automatically"), so the gap was user-visible, not just internal.
#
# 15 minutes is a deliberate trade: one Firestore get against a single
# document per worker per quarter-hour, against a refresh that lands
# within a quarter-hour of the poller running it instead of never.
_CACHE_TTL_SECONDS = 900


def _cosine_similarity(a: list[float], b: list[float]) -> float:
    dot = sum(x * y for x, y in zip(a, b))
    norm_a = math.sqrt(sum(x * x for x in a))
    norm_b = math.sqrt(sum(y * y for y in b))
    return dot / (norm_a * norm_b) if norm_a and norm_b else 0.0


def embed_and_store_corpus() -> int:
    """One-time setup: embeds every criterion and stores it in Firestore.

    Safe to re-run -- overwrites existing entries by document id (the
    criterion number), doesn't duplicate.
    """
    count = 0
    for criterion in WCAG_CORPUS:
        text = f"{criterion.number} {criterion.title}: {criterion.description}"
        embedding = _embed(text)
        _kb().document(criterion.number.replace(".", "_")).set(
            {
                "number": criterion.number,
                "title": criterion.title,
                "level": criterion.level,
                "description": criterion.description,
                "embedding": embedding,
            }
        )
        count += 1
    global _cache
    _cache = None  # force reload in THIS process; other processes use the version check below
    return count


def _stored_kb_version() -> str | None:
    """One cheap get against a single document. Failures are swallowed on
    purpose: a Firestore blip during a freshness check must not take down
    a scan that has perfectly serviceable embeddings already in memory.
    The consequence of a miss is a stale cache for one more TTL window,
    which is strictly better than a failed scan.
    """
    try:
        stored = _get_kb_version()
    except Exception:  # noqa: BLE001 - see docstring
        logger.warning("Could not read the KB version to revalidate the corpus cache", exc_info=True)
        return _cache_kb_version  # treat as unchanged
    return stored.get("version") if stored else None


def _load_cache() -> list[tuple[WCAGCriterion, list[float]]]:
    global _cache, _cache_kb_version, _cache_checked_at
    now = time.monotonic()

    if _cache is not None:
        if now - _cache_checked_at < _CACHE_TTL_SECONDS:
            return _cache
        # TTL elapsed: revalidate rather than blindly reload. The common
        # case by far is "nothing changed", and that costs one document
        # read rather than streaming the whole corpus back.
        _cache_checked_at = now
        current_version = _stored_kb_version()
        if current_version == _cache_kb_version:
            return _cache
        logger.info(
            "WCAG knowledge base moved %s -> %s -- reloading the embedded corpus",
            _cache_kb_version, current_version,
        )
        _cache = None

    loaded = []
    for doc in _kb().stream():
        d = doc.to_dict()
        criterion = WCAGCriterion(
            number=d["number"], title=d["title"], level=d["level"], description=d["description"]
        )
        loaded.append((criterion, d["embedding"]))
    _cache = loaded
    _cache_kb_version = _stored_kb_version()
    _cache_checked_at = time.monotonic()
    return _cache


def retrieve_batch(queries: list[str], top_k: int = 1) -> list[list[WCAGCriterion]]:
    """Same as retrieve(), for many queries at once -- one embedding call
    instead of one per query, so grounding a whole page's findings costs a
    single round trip rather than scaling with the number of findings.
    """
    corpus = _load_cache()
    if not corpus or not queries:
        return [[] for _ in queries]
    query_embeddings = _embed_batch(queries)
    return [_score_and_rank(qe, top_k) for qe in query_embeddings]


def _score_and_rank(query_embedding: list[float], top_k: int) -> list[WCAGCriterion]:
    corpus = _load_cache()
    scored = [(_cosine_similarity(query_embedding, emb), criterion) for criterion, emb in corpus]
    scored.sort(key=lambda x: x[0], reverse=True)
    return [criterion for _, criterion in scored[:top_k]]
