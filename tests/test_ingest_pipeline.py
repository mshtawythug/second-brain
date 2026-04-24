"""Integration tests for the ingest pipeline (extract → chunk → embed → store)."""
from brain.ingest import ExtractedDoc, ingest_document


def _ingest(test_db, embedder, doc, *, force=False):
    return ingest_document(
        test_db, embedder=embedder, doc=doc, source_kind="manual", force=force
    )


def test_ingest_creates_document_and_chunks(test_db, fake_embedder):
    doc = ExtractedDoc(
        title="Test",
        content="Hello world.\n\nThis is a paragraph.",
        content_type="txt",
        source_path="/tmp/x.txt",
        metadata={},
    )
    result = _ingest(test_db, fake_embedder, doc)
    assert result.created is True
    assert result.document_id is not None
    chunks = test_db.execute(
        "SELECT count(*) FROM chunks WHERE document_id=%s", (result.document_id,)
    ).fetchone()[0]
    assert chunks >= 1


def test_ingest_is_idempotent_by_content_hash(test_db, fake_embedder):
    doc = ExtractedDoc(
        title="T",
        content="same content",
        content_type="txt",
        source_path=None,
        metadata={},
    )
    first = _ingest(test_db, fake_embedder, doc)
    second = _ingest(test_db, fake_embedder, doc)
    assert first.created is True
    assert second.created is False
    assert first.document_id == second.document_id


def test_ingest_force_replaces_existing(test_db, fake_embedder):
    doc = ExtractedDoc(
        title="T",
        content="same content",
        content_type="txt",
        source_path=None,
        metadata={},
    )
    first = _ingest(test_db, fake_embedder, doc)
    second = _ingest(test_db, fake_embedder, doc, force=True)
    assert second.created is True
    assert second.document_id != first.document_id


def test_ingest_with_external_id_creates_source_row(test_db, fake_embedder):
    doc = ExtractedDoc(
        title="T",
        content="x",
        content_type="transcript",
        source_path=None,
        metadata={},
    )
    result = ingest_document(
        test_db,
        embedder=fake_embedder,
        doc=doc,
        source_kind="krisp",
        source_external_id="meeting-123",
        source_metadata={"date": "2026-04-01"},
    )
    src = test_db.execute(
        "SELECT kind, external_id, metadata FROM sources WHERE id="
        "(SELECT source_id FROM documents WHERE id=%s)",
        (result.document_id,),
    ).fetchone()
    assert src[0] == "krisp"
    assert src[1] == "meeting-123"
    assert src[2] == {"date": "2026-04-01"}


def test_ingest_reuses_source_on_repeat(test_db, fake_embedder):
    doc1 = ExtractedDoc(
        title="T1", content="c1", content_type="email", source_path=None, metadata={}
    )
    doc2 = ExtractedDoc(
        title="T2", content="c2", content_type="email", source_path=None, metadata={}
    )
    r1 = ingest_document(
        test_db,
        embedder=fake_embedder,
        doc=doc1,
        source_kind="gmail",
        source_external_id="msg-1",
    )
    r2 = ingest_document(
        test_db,
        embedder=fake_embedder,
        doc=doc2,
        source_kind="gmail",
        source_external_id="msg-1",
    )
    src_ids = test_db.execute(
        "SELECT source_id FROM documents WHERE id IN (%s, %s)",
        (r1.document_id, r2.document_id),
    ).fetchall()
    assert src_ids[0][0] == src_ids[1][0]


def test_ingest_empty_content_is_noop(test_db, fake_embedder):
    """Empty/whitespace-only content yields no chunks and creates no document."""
    doc = ExtractedDoc(
        title="Empty",
        content="   \n\n  ",
        content_type="txt",
        source_path=None,
        metadata={},
    )
    result = _ingest(test_db, fake_embedder, doc)
    assert result.created is False
    assert result.document_id is None
    rows = test_db.execute("SELECT count(*) FROM documents").fetchone()[0]
    assert rows == 0


def test_ingest_with_tags(test_db, fake_embedder):
    doc = ExtractedDoc(
        title="T", content="x", content_type="txt", source_path=None, metadata={}
    )
    result = ingest_document(
        test_db,
        embedder=fake_embedder,
        doc=doc,
        source_kind="manual",
        tags=["interview", "company-id"],
    )
    tags = test_db.execute(
        "SELECT tags FROM documents WHERE id=%s", (result.document_id,)
    ).fetchone()[0]
    assert sorted(tags) == ["company-id", "interview"]
