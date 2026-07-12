"""Integration tests for hybrid search (FTS + vector via RRF)."""
import os
from typing import Any

from brain.db import connect
from brain.ingest import ExtractedDoc, ingest_document
from brain.search import hybrid_search

TEST_DATABASE_URL = os.environ.get(
    "TEST_DATABASE_URL",
    "postgresql://brain:brain@localhost:5434/second_brain_test",
)


def _seed(test_db, embedder, items):
    """Seed the test DB with (title, content, tags, source_kind) tuples.

    Two small adjustments keep the suite honest without changing test intent:

    * A unique ``source_external_id`` is supplied per row so the ingest
      pipeline always creates a source row — ``_upsert_source`` otherwise
      skips insertion for manual ingests with no external id, and the
      ``source_kind`` filter can never match.
    * The title is prefixed onto the stored content so two rows sharing the
      same test content string (e.g. both "common term") hash to different
      ``content_hash`` values and don't collapse via dedup.
    """
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


def test_search_finds_documents_by_keyword(test_db, fake_embedder):
    _seed(test_db, fake_embedder, [
        ("Doc A", "company-id was a great company to work at", [], "manual"),
        ("Doc B", "krisp meeting transcript about pizza", [], "manual"),
    ])
    results = hybrid_search(test_db, embedder=fake_embedder, query="company-id", limit=5)
    titles = [r.title for r in results]
    assert "Doc A" in titles


def test_search_returns_snippet(test_db, fake_embedder):
    _seed(test_db, fake_embedder, [
        ("Long doc", "alpha beta gamma. company-id was great. delta epsilon.", [], "manual"),
    ])
    results = hybrid_search(test_db, embedder=fake_embedder, query="company-id", limit=5)
    assert results
    assert results[0].snippet


def test_search_filters_by_tag(test_db, fake_embedder):
    _seed(test_db, fake_embedder, [
        ("A", "company-id stuff", ["interview"], "manual"),
        ("B", "company-id stuff", ["personal"], "manual"),
    ])
    results = hybrid_search(
        test_db, embedder=fake_embedder, query="company-id", limit=5, tag="interview"
    )
    titles = [r.title for r in results]
    assert "A" in titles
    assert "B" not in titles


def test_search_filters_by_source(test_db, fake_embedder):
    _seed(test_db, fake_embedder, [
        ("A", "common term", [], "manual"),
        ("B", "common term", [], "krisp"),
    ])
    results = hybrid_search(
        test_db, embedder=fake_embedder, query="common", limit=5, source_kind="krisp"
    )
    titles = [r.title for r in results]
    assert titles == ["B"]


def test_search_respects_limit(test_db, fake_embedder):
    _seed(test_db, fake_embedder, [
        (f"Doc{i}", f"shared keyword {i}", [], "manual") for i in range(5)
    ])
    results = hybrid_search(test_db, embedder=fake_embedder, query="keyword", limit=2)
    assert len(results) <= 2


def test_search_fts_only_skips_embedding(test_db, fake_embedder):
    _seed(test_db, fake_embedder, [
        ("A", "company-id term", [], "manual"),
    ])
    results = hybrid_search(
        test_db, embedder=fake_embedder, query="company-id", limit=5, fts_only=True
    )
    assert results


def test_search_since_days_filter(test_db, fake_embedder):
    """since_days=1 includes documents ingested just now (within last day)."""
    _seed(test_db, fake_embedder, [
        ("Recent", "company-id term", [], "manual"),
    ])
    results = hybrid_search(
        test_db, embedder=fake_embedder, query="company-id", limit=5, since_days=1
    )
    assert [r.title for r in results] == ["Recent"]


def test_search_since_days_excludes_old(test_db, fake_embedder):
    """since_days=1 excludes a document whose ingested_at is pushed back 10 days."""
    _seed(test_db, fake_embedder, [
        ("Old", "company-id term", [], "manual"),
    ])
    test_db.execute("UPDATE documents SET ingested_at = NOW() - INTERVAL '10 days'")
    results = hybrid_search(
        test_db, embedder=fake_embedder, query="company-id", limit=5, since_days=1
    )
    assert results == []


def test_search_no_matches_returns_empty(test_db, fake_embedder):
    """FTS-only search with no matches returns []."""
    _seed(test_db, fake_embedder, [
        ("A", "alpha term", [], "manual"),
    ])
    results = hybrid_search(
        test_db,
        embedder=fake_embedder,
        query="nonexistentkeyword",
        limit=5,
        fts_only=True,
    )
    assert results == []


class _MidSearchDeleteConn:
    """Connection test-double that lands a concurrent DELETE in a precise window.

    ``hybrid_search`` ranks chunks first (building ``by_doc``), then fetches
    per-document metadata via a second query (``... FROM documents d ...
    d.id = ANY(%s)``). This wrapper delegates every call to the real connection
    but, the first time it sees that metadata query, deletes ``victim_id`` on a
    *separate* connection first — reproducing the exact race an in-flight
    ``brain rm`` opens. Composition over inheritance; NOT monkey-patching
    (CLAUDE.md rule 13) — a purpose-built stand-in whose only extra behavior is
    the interleaved delete.
    """

    def __init__(self, real: Any, victim_id: str) -> None:
        self._real = real
        self._victim_id = victim_id
        self.delete_fired = False

    def execute(self, sql: str, *args: Any, **kwargs: Any) -> Any:
        if (
            not self.delete_fired
            and "FROM documents d" in sql
            and "d.id = ANY" in sql
        ):
            self.delete_fired = True
            with connect(TEST_DATABASE_URL) as other:
                other.autocommit = True
                other.execute(
                    "DELETE FROM documents WHERE id = %s", (self._victim_id,)
                )
        return self._real.execute(sql, *args, **kwargs)

    def __getattr__(self, name: str) -> Any:
        return getattr(self._real, name)


def test_hybrid_search_skips_doc_deleted_mid_search(test_db, fake_embedder):
    """A doc deleted between chunk ranking and metadata fetch is skipped, not KeyError.

    Regression for overhaul Task 2.2. The ranked-chunk phase can surface a
    ``document_id`` that a concurrent ``brain rm`` removes before the
    per-document metadata SELECT runs, leaving ``docs[doc_id]`` a ``KeyError``
    that blew up the whole search. The fix skips the now-orphaned doc.
    """
    _seed(test_db, fake_embedder, [
        ("Keeper", "company-id shared keyword", [], "manual"),
        ("Victim", "company-id shared keyword", [], "manual"),
    ])
    victim_id = str(
        test_db.execute(
            "SELECT id FROM documents WHERE title = %s", ("Victim",)
        ).fetchone()[0]
    )

    race = _MidSearchDeleteConn(test_db, victim_id)
    results = hybrid_search(
        race, embedder=fake_embedder, query="company-id shared keyword", limit=5
    )

    assert race.delete_fired, "the mid-search delete window was never hit"
    titles = [r.title for r in results]
    assert "Victim" not in titles
    assert "Keeper" in titles


def test_hybrid_search_negative_limit_does_not_silently_truncate(
    test_db, fake_embedder
):
    """A negative ``limit`` must not silently slice the tail off the ranked list.

    Regression for overhaul Task 2.10. ``results[:limit]`` with ``limit=-2``
    returned all-but-the-last-2 docs (silent wrong data). The defensive floor
    clamps a non-positive limit to 1 instead.
    """
    _seed(test_db, fake_embedder, [
        (f"Doc{i}", "shared keyword term", [], "manual") for i in range(5)
    ])
    results = hybrid_search(
        test_db, embedder=fake_embedder, query="shared keyword term", limit=-2
    )
    # Old code returned len(all_docs) - 2 == 3 (tail silently dropped); the
    # floor clamps to exactly 1.
    assert len(results) == 1
