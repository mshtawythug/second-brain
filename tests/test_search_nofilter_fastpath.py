"""No-filter fast-path equivalence + cache-integration tests (perf F3/F5/F2/F1).

The unfiltered search path was restructured to (F3) compute ``ts_rank`` once via
a two-level CTE, (F5) omit the ``documents`` JOIN when no metadata filter is
active, and (F2) prepare the now-static SQL. None of these may change ranked
output. The query embed is also wrapped in an in-process LRU cache (F1).

Strategy: seed a synthetic corpus with distinct term frequencies (so every
``ts_rank`` / cosine score is distinct and row order is deterministic), then
assert the fast (no-JOIN) path returns byte-identical ranked documents + scores
to the JOIN path forced by a no-op ``without_tag`` filter. All queries are
synthetic — no PII.
"""
from __future__ import annotations

import psycopg
import pytest

from brain.ingest import ExtractedDoc, ingest_document
from brain.search import _cached_query_embed, _embedder_registry, hybrid_search


def _seed(test_db: psycopg.Connection, embedder: object, items: list) -> None:
    for title, content, tags, source_kind in items:
        ingest_document(
            test_db,
            embedder=embedder,
            doc=ExtractedDoc(
                title=title,
                content=f"{title}: {content}",
                content_type="txt",
                source_path=None,
                metadata={},
            ),
            source_kind=source_kind,
            source_external_id=f"{source_kind}:{title}",
            tags=tags or [],
        )


# Distinct term frequencies → distinct ts_rank / cosine scores → deterministic
# row order, so before/after comparisons are byte-stable.
_CORPUS = [
    ("Alpha", "roadmap roadmap roadmap planning quarterly goals", [], "manual"),
    ("Bravo", "roadmap planning sprint backlog", [], "manual"),
    ("Charlie", "planning offsite agenda notes", [], "manual"),
    ("Delta", "roadmap", [], "manual"),
    ("Echo", "unrelated cooking recipe content", [], "manual"),
]

# A tag no seeded document carries: keeps every document (so the result set is
# unchanged) while forcing the filtered (JOIN) code path on for comparison.
_NOOP_TAG = "zzz-nonexistent-tag"


def _payload(results: list) -> list[tuple]:
    return [(r.document_id, r.title, r.score, r.snippet) for r in results]


def test_nofilter_fastpath_matches_join_path_hybrid(
    test_db: psycopg.Connection, fake_embedder: object
) -> None:
    _seed(test_db, fake_embedder, _CORPUS)
    fast = hybrid_search(
        test_db, embedder=fake_embedder, query="roadmap planning", limit=5
    )
    forced_join = hybrid_search(
        test_db,
        embedder=fake_embedder,
        query="roadmap planning",
        limit=5,
        without_tag=_NOOP_TAG,
    )
    assert _payload(fast) == _payload(forced_join)
    assert fast, "expected at least one hit so the comparison is meaningful"


def test_nofilter_fastpath_matches_join_path_fts_only(
    test_db: psycopg.Connection, fake_embedder: object
) -> None:
    _seed(test_db, fake_embedder, _CORPUS)
    fast = hybrid_search(
        test_db,
        embedder=fake_embedder,
        query="roadmap planning",
        limit=5,
        fts_only=True,
    )
    forced_join = hybrid_search(
        test_db,
        embedder=fake_embedder,
        query="roadmap planning",
        limit=5,
        fts_only=True,
        without_tag=_NOOP_TAG,
    )
    assert _payload(fast) == _payload(forced_join)


def test_nofilter_fastpath_matches_join_path_with_recency_and_context(
    test_db: psycopg.Connection, fake_embedder: object
) -> None:
    """Recency boost + snippet expansion read documents only via the separate
    ``doc_rows`` fetch, so eliding the leg JOIN must not perturb them.

    The recency multiplier is a function of ``datetime.now()`` computed fresh
    inside each call, so absolute scores drift by the wall-clock delta between
    the two searches (~1e-11). We therefore assert ranked doc order + snippets
    are byte-identical and scores match within tolerance — the JOIN elision is
    proven equivalent; only the time-of-call differs.
    """
    _seed(test_db, fake_embedder, _CORPUS)
    common = dict(
        embedder=fake_embedder,
        query="roadmap planning",
        limit=5,
        recency_halflife_days=180.0,
        snippet_context_tokens=200,
    )
    fast = hybrid_search(test_db, **common)  # type: ignore[arg-type]
    forced_join = hybrid_search(test_db, without_tag=_NOOP_TAG, **common)  # type: ignore[arg-type]
    # Ranked identity (doc id + title + snippet) is byte-identical.
    assert [(r.document_id, r.title, r.snippet) for r in fast] == [
        (r.document_id, r.title, r.snippet) for r in forced_join
    ]
    # Scores agree up to wall-clock drift in the recency factor.
    for a, b in zip(fast, forced_join, strict=True):
        assert a.score == pytest.approx(b.score, rel=1e-6)


def test_nofilter_fastpath_stable_doc_set(
    test_db: psycopg.Connection, fake_embedder: object
) -> None:
    """Lock the matched-doc set for a known corpus (ts_rank-once restructure)."""
    _seed(test_db, fake_embedder, _CORPUS)
    results = hybrid_search(
        test_db, embedder=fake_embedder, query="roadmap", limit=5, fts_only=True
    )
    titles = {r.title for r in results}
    # Only documents that actually contain 'roadmap' in an fts_only search.
    assert titles == {"Alpha", "Bravo", "Delta"}


def test_hybrid_search_reuses_cached_query_embed(
    test_db: psycopg.Connection, counting_embedder: object
) -> None:
    """Two identical in-process searches embed the query exactly once (F1)."""
    _cached_query_embed.cache_clear()
    _embedder_registry.clear()
    _seed(test_db, counting_embedder, _CORPUS)
    counting_embedder.embed_calls = 0  # type: ignore[attr-defined]  # ignore seeding embeds
    hybrid_search(
        test_db, embedder=counting_embedder, query="roadmap planning", limit=5
    )
    hybrid_search(
        test_db, embedder=counting_embedder, query="roadmap planning", limit=5
    )
    # First search computes + caches the query embed; the second is a cache hit.
    assert counting_embedder.embed_calls == 1  # type: ignore[attr-defined]
    _cached_query_embed.cache_clear()
    _embedder_registry.clear()
