"""B8: the embedded-corpus cache has to notice a knowledge-base refresh.

The two ends of this live in different Cloud Run services.
`embed_and_store_corpus()` runs in scan-wcag-poller and reset `_cache` --
but only in the poller's own process. The consumer is editor.py ->
retrieve_batch, in scan-worker. So after a WCAG refresh every warm
scan-worker instance went on grounding findings against the old
embeddings until it happened to cold-start: no signal, no TTL, and no
bound on how long a warm instance lives. The landing page advertises the
refresh as a headline feature, so the gap was user-visible.
"""

from __future__ import annotations

import pytest

from mad_platform.data.wcag_corpus import WCAGCriterion
from mad_platform.tools import rag


@pytest.fixture
def fake_kb(monkeypatch):
    """Stands in for the Firestore collection and the version document,
    counting how often each is actually read.
    """
    state = {"version": "2.2", "streams": 0, "version_reads": 0, "title": "Non-text Content"}

    class _Doc:
        def to_dict(self):
            return {
                "number": "1.1.1",
                "title": state["title"],
                "level": "A",
                "description": "d",
                "embedding": [1.0, 0.0],
            }

    class _Collection:
        def stream(self):
            state["streams"] += 1
            return [_Doc()]

    def _version():
        state["version_reads"] += 1
        return {"version": state["version"]}

    monkeypatch.setattr(rag, "_kb", _Collection)
    monkeypatch.setattr(rag, "_get_kb_version", _version)
    monkeypatch.setattr(rag, "_cache", None)
    monkeypatch.setattr(rag, "_cache_kb_version", None)
    monkeypatch.setattr(rag, "_cache_checked_at", 0.0)
    return state


def test_the_corpus_is_loaded_once_then_served_from_memory(fake_kb):
    for _ in range(5):
        rag._load_cache()
    assert fake_kb["streams"] == 1


def test_within_the_ttl_the_version_is_not_even_checked(fake_kb):
    """The revalidation must not cost a Firestore read per retrieval --
    that would be worse than the bug.
    """
    rag._load_cache()
    reads_after_load = fake_kb["version_reads"]
    for _ in range(10):
        rag._load_cache()
    assert fake_kb["version_reads"] == reads_after_load


def test_after_the_ttl_an_unchanged_version_costs_one_document_read(fake_kb, monkeypatch):
    rag._load_cache()
    streams_after_load = fake_kb["streams"]
    reads_after_load = fake_kb["version_reads"]

    monkeypatch.setattr(rag, "_cache_checked_at", -rag._CACHE_TTL_SECONDS)
    rag._load_cache()

    assert fake_kb["version_reads"] == reads_after_load + 1
    assert fake_kb["streams"] == streams_after_load, "an unchanged version must not re-stream the corpus"


def test_a_version_change_reloads_the_corpus(fake_kb, monkeypatch):
    """The actual finding: a warm worker must pick up a refresh the poller
    performed in another process.
    """
    rag._load_cache()
    assert rag._load_cache()[0][0].title == "Non-text Content"

    fake_kb["version"] = "3.0"
    fake_kb["title"] = "Text Alternatives (rewritten for 3.0)"
    monkeypatch.setattr(rag, "_cache_checked_at", -rag._CACHE_TTL_SECONDS)

    reloaded = rag._load_cache()
    assert reloaded[0][0].title == "Text Alternatives (rewritten for 3.0)"
    assert fake_kb["streams"] == 2


def test_a_firestore_blip_during_revalidation_does_not_fail_the_scan(fake_kb, monkeypatch):
    """A stale cache for one more TTL window is strictly better than a
    failed scan over a version check.
    """
    rag._load_cache()

    def boom():
        raise RuntimeError("Firestore unavailable")

    monkeypatch.setattr(rag, "_get_kb_version", boom)
    monkeypatch.setattr(rag, "_cache_checked_at", -rag._CACHE_TTL_SECONDS)

    assert rag._load_cache()[0][0].title == "Non-text Content"


def test_an_in_process_refresh_still_invalidates_immediately(fake_kb, monkeypatch):
    """embed_and_store_corpus()'s own reset must keep working -- the TTL is
    for the cross-process case, not a replacement for it.
    """
    rag._load_cache()
    monkeypatch.setattr(rag, "_embed", lambda _t: [1.0, 0.0])

    class _Writable:
        def document(self, _id):
            return self

        def set(self, _data):
            return None

        def stream(self):
            fake_kb["streams"] += 1
            return []

    monkeypatch.setattr(rag, "_kb", _Writable)
    rag.embed_and_store_corpus()
    assert rag._cache is None


def test_the_single_query_retrieve_is_gone():
    """It had no callers; retrieve_batch is what every real caller wants
    (one embedding round trip per page, not one per finding).
    """
    assert not hasattr(rag, "retrieve")
    assert hasattr(rag, "retrieve_batch")


def test_the_criterion_type_round_trips(fake_kb):
    criterion, embedding = rag._load_cache()[0]
    assert isinstance(criterion, WCAGCriterion)
    assert embedding == [1.0, 0.0]
