"""Integration tests for hybrid search (FTS + vector via RRF)."""
from brain.ingest import ExtractedDoc, ingest_document
from brain.search import hybrid_search


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
