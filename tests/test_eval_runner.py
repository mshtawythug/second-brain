"""Integration tests for brain.eval.runner — run_eval() and _normalize_ids()."""

import hashlib
import unittest.mock

import psycopg
import pytest

from brain.eval.corpus import EvalQuery
from brain.eval.errors import EvalCorpusError
from brain.eval.runner import EvalReport, _normalize_ids, run_eval
from brain.ingest import ExtractedDoc, ingest_document

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _seed(
    test_db: psycopg.Connection, embedder, title: str, content: str, source_kind: str = "manual"
) -> str:
    """Ingest one document and return its full UUID string."""
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
    )
    row = test_db.execute(
        "SELECT id::text FROM documents WHERE title = %s",
        (title,),
    ).fetchone()
    assert row is not None
    return str(row[0])


def _make_query(query: str, expected_id: str, category: str = "semantic", **kwargs) -> EvalQuery:
    return EvalQuery(query=query, expected_doc_ids=[expected_id], category=category, **kwargs)


# ---------------------------------------------------------------------------
# _normalize_ids — unit tests (direct calls, no run_eval)
# ---------------------------------------------------------------------------


def test_normalize_ids_passes_full_uuid_through(test_db, fake_embedder):
    """Full UUIDs (32+ chars or containing hyphens) are returned unchanged."""
    full_uuid = "12345678-1234-1234-1234-123456789abc"
    # No DB lookup for full UUIDs — a missing doc is not an error at this layer.
    result = _normalize_ids(test_db, [full_uuid])
    assert result == [full_uuid]


def test_normalize_ids_resolves_8char_prefix(test_db, fake_embedder):
    """An 8-char hex prefix is resolved to the full UUID via a LIKE query."""
    doc_id = _seed(test_db, fake_embedder, "Prefix Doc", "prefix resolution test content")
    prefix = doc_id[:8]
    resolved = _normalize_ids(test_db, [prefix])
    assert resolved == [doc_id]


def test_normalize_ids_drops_stale_prefix(test_db, fake_embedder):
    """A prefix that matches zero documents is silently dropped (stale corpus)."""
    result = _normalize_ids(test_db, ["00000000"])
    assert result == []


def test_normalize_ids_ambiguous_prefix_raises(test_db, fake_embedder):
    """A prefix matching multiple documents raises EvalCorpusError."""
    # Insert two documents with UUIDs sharing the same 4-char prefix.
    # We control the ID by inserting directly; the tsv column is generated.
    uid1 = "abcd0000-0000-0000-0000-000000000001"
    uid2 = "abcd1111-0000-0000-0000-000000000002"
    pairs = [
        (uid1, "Ambig Doc One", "content one alpha"),
        (uid2, "Ambig Doc Two", "content two beta"),
    ]
    for uid, title, extra in pairs:
        h = hashlib.md5(uid.encode()).hexdigest()
        test_db.execute(
            """INSERT INTO documents (id, title, content, content_hash, content_type)
               VALUES (%s::uuid, %s, %s, %s, 'txt')""",
            (uid, title, extra, h),
        )

    with pytest.raises(EvalCorpusError, match="ambiguous"):
        _normalize_ids(test_db, ["abcd"])


# ---------------------------------------------------------------------------
# run_eval — integration tests
# ---------------------------------------------------------------------------


def test_run_eval_returns_one_result_per_query(test_db, fake_embedder):
    """Three seeded docs + three queries → three EvalResult entries in input order."""
    id1 = _seed(test_db, fake_embedder, "Alpha Doc", "alpha topic discussion")
    id2 = _seed(test_db, fake_embedder, "Beta Doc", "beta topic discussion")
    id3 = _seed(test_db, fake_embedder, "Gamma Doc", "gamma topic discussion")

    queries = [
        _make_query("alpha topic", id1),
        _make_query("beta topic", id2),
        _make_query("gamma topic", id3),
    ]
    report = run_eval(test_db, embedder=fake_embedder, queries=queries)

    assert isinstance(report, EvalReport)
    assert len(report.results) == 3
    assert [r.query for r in report.results] == ["alpha topic", "beta topic", "gamma topic"]


def test_run_eval_threads_source_filter_through(test_db, fake_embedder):
    """A query with source_filter='krisp' ignores manual docs."""
    _seed(test_db, fake_embedder, "Manual Note", "krisp notes manual content", source_kind="manual")
    _seed(
        test_db, fake_embedder, "Krisp Meeting", "krisp notes meeting content", source_kind="krisp"
    )

    queries = [
        EvalQuery(
            query="krisp notes",
            expected_doc_ids=["00000000"],  # nil UUID — we just check source filtering
            category="meeting",
            source_filter="krisp",
        )
    ]
    report = run_eval(test_db, embedder=fake_embedder, queries=queries)
    assert len(report.results) == 1
    # All returned docs must be krisp docs (the source filter was applied).
    for doc_id in report.results[0].actual_doc_ids:
        row = test_db.execute(
            """SELECT s.kind FROM documents d
               JOIN sources s ON s.id = d.source_id
               WHERE d.id::text = %s""",
            (doc_id,),
        ).fetchone()
        if row is not None:
            assert row[0] == "krisp"


def test_run_eval_resolves_8char_prefixes(test_db, fake_embedder):
    """Expected IDs given as 8-char prefixes are resolved to full UUIDs in the result."""
    doc_id = _seed(test_db, fake_embedder, "Prefix Test Doc", "prefix resolution data here")
    prefix = doc_id[:8]

    queries = [_make_query("prefix resolution data", prefix)]
    report = run_eval(test_db, embedder=fake_embedder, queries=queries)

    assert len(report.results) == 1
    # The stored expected_doc_ids should be the resolved full UUID, not the prefix.
    assert report.results[0].expected_doc_ids == [doc_id]


def test_run_eval_handles_ollama_failure_gracefully(test_db, fake_embedder):
    """When the embedder raises OllamaEmbedError, actual_doc_ids=[] and metrics=0.0."""
    from brain.embeddings import OllamaEmbedError

    _seed(test_db, fake_embedder, "Embed Fail Doc", "embedding failure test document")
    queries = [_make_query("embedding failure test", "00000000")]

    # Patch embed on the fake_embedder to raise OllamaEmbedError for this test.
    with unittest.mock.patch.object(fake_embedder, "embed", side_effect=OllamaEmbedError("test")):
        report = run_eval(test_db, embedder=fake_embedder, queries=queries)

    assert len(report.results) == 1
    r = report.results[0]
    assert r.actual_doc_ids == []
    assert r.ndcg_at_5 == 0.0
    assert r.mrr == 0.0
    assert r.recall_at_20 == 0.0


def test_run_eval_aggregates_per_category(test_db, fake_embedder):
    """Two 'people' queries + one 'email' → per_category has correct counts."""
    id1 = _seed(test_db, fake_embedder, "Person A", "people category content alpha")
    id2 = _seed(test_db, fake_embedder, "Person B", "people category content beta")
    id3 = _seed(test_db, fake_embedder, "Email C", "email category content gamma")

    queries = [
        EvalQuery(query="people content alpha", expected_doc_ids=[id1], category="people"),
        EvalQuery(query="people content beta", expected_doc_ids=[id2], category="people"),
        EvalQuery(query="email content gamma", expected_doc_ids=[id3], category="email"),
    ]
    report = run_eval(test_db, embedder=fake_embedder, queries=queries)

    assert "people" in report.per_category
    assert "email" in report.per_category
    assert report.per_category["people"].count == 2
    assert report.per_category["email"].count == 1


def test_run_eval_config_signature_captures_kwargs(test_db, fake_embedder):
    """EvalReport.config_signature records all four kwarg fields."""
    _seed(test_db, fake_embedder, "Config Sig Doc", "config signature test content")
    queries = [_make_query("config signature test", "00000000")]

    report = run_eval(
        test_db,
        embedder=fake_embedder,
        queries=queries,
        recency_halflife_days=90.0,
        snippet_context_tokens=100,
        vector_sim_floor=0.3,
        embedder_name="arctic",
    )

    sig = report.config_signature
    assert sig["recency_halflife_days"] == 90.0
    assert sig["snippet_context_tokens"] == 100
    assert sig["vector_sim_floor"] == 0.3
    assert sig["embedder"] == "arctic"


def test_run_eval_mean_aggregates_match_per_query(test_db, fake_embedder):
    """Report-level means equal the arithmetic mean of per-query values."""
    ids = [
        _seed(test_db, fake_embedder, f"Mean Doc {i}", f"mean aggregate test content {i}")
        for i in range(3)
    ]
    queries = [
        _make_query(f"mean aggregate test content {i}", ids[i])
        for i in range(3)
    ]
    report = run_eval(test_db, embedder=fake_embedder, queries=queries)

    n = len(report.results)
    assert report.mean_ndcg_at_5 == pytest.approx(
        sum(r.ndcg_at_5 for r in report.results) / n, abs=1e-9
    )
    assert report.mean_mrr == pytest.approx(
        sum(r.mrr for r in report.results) / n, abs=1e-9
    )
    assert report.mean_recall_at_20 == pytest.approx(
        sum(r.recall_at_20 for r in report.results) / n, abs=1e-9
    )
