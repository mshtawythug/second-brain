"""Integration tests for the ingest pipeline (extract → chunk → embed → store)."""
import pytest

from brain.ingest import ExtractedDoc, ingest_document, update_document


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


def test_ingest_produces_4096_dim_embedding(test_db, fake_embedder):
    """Regression for migration 002: every embedding row must be 4096-dim.

    The default ``FakeEmbedder()`` now returns 4096-dim vectors to match the
    new ``vector(4096)`` schema; ingesting must round-trip them intact.
    """
    doc = ExtractedDoc(
        title="Dim check",
        content="paragraph one.\n\nparagraph two.\n\nparagraph three.",
        content_type="txt",
        source_path=None,
        metadata={},
    )
    result = _ingest(test_db, fake_embedder, doc)
    assert result.document_id is not None

    rows = test_db.execute(
        "SELECT vector_dims(embedding) FROM chunks WHERE document_id=%s",
        (result.document_id,),
    ).fetchall()
    assert rows, "expected at least one chunk"
    for (dims,) in rows:
        assert dims == 4096


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


# --- update_document --------------------------------------------------------
# These tests use the shared ``seed_doc`` factory fixture from conftest.py.


def test_update_document_body_change_deletes_old_chunks(
    test_db, fake_embedder, seed_doc
):
    doc_id = seed_doc(content="paragraph one.\n\nparagraph two.")
    old_chunk_ids = {
        r[0]
        for r in test_db.execute(
            "SELECT id FROM chunks WHERE document_id=%s", (doc_id,)
        ).fetchall()
    }
    assert old_chunk_ids
    result = update_document(
        test_db,
        document_id=doc_id,
        embedder=fake_embedder,
        new_content="brand new body about company-id and person-a.",
    )
    assert result.rechunked is True
    assert "content" in result.fields_changed
    new_chunk_ids = {
        r[0]
        for r in test_db.execute(
            "SELECT id FROM chunks WHERE document_id=%s", (doc_id,)
        ).fetchall()
    }
    assert new_chunk_ids and new_chunk_ids.isdisjoint(old_chunk_ids)


def test_update_document_metadata_merge_preserves_other_keys(test_db, fake_embedder, seed_doc):
    doc_id = seed_doc(metadata={"a": 1, "b": 2})
    result = update_document(
        test_db, document_id=doc_id, metadata_patch={"b": 3, "c": 4}
    )
    assert result.fields_changed == ["metadata"]
    meta = test_db.execute(
        "SELECT metadata FROM documents WHERE id=%s", (doc_id,)
    ).fetchone()[0]
    assert meta == {"a": 1, "b": 3, "c": 4}


def test_update_document_collision_aborts(test_db, fake_embedder, seed_doc):
    a_id = seed_doc(content="alpha")
    b_id = seed_doc(content="bravo")
    with pytest.raises(ValueError, match="content collides"):
        update_document(
            test_db,
            document_id=b_id,
            embedder=fake_embedder,
            new_content="alpha",
        )
    # Original document a is unchanged.
    a_content = test_db.execute(
        "SELECT content FROM documents WHERE id=%s", (a_id,)
    ).fetchone()[0]
    assert a_content == "alpha"


def test_update_document_body_change_rolls_back_on_embedder_error(
    test_db, fake_embedder, seed_doc
):
    doc_id = seed_doc(content="original body of text.")
    old_hash = test_db.execute(
        "SELECT content_hash FROM documents WHERE id=%s", (doc_id,)
    ).fetchone()[0]
    old_chunk_count = test_db.execute(
        "SELECT count(*) FROM chunks WHERE document_id=%s", (doc_id,)
    ).fetchone()[0]
    assert old_chunk_count >= 1

    class BoomEmbedder:
        def embed(self, texts, *, input_type="document"):
            raise RuntimeError("embedder is down")

        def count_tokens(self, text):
            return fake_embedder.count_tokens(text)

    with pytest.raises(RuntimeError, match="embedder is down"):
        update_document(
            test_db,
            document_id=doc_id,
            embedder=BoomEmbedder(),
            new_content="totally different body",
        )

    after_hash = test_db.execute(
        "SELECT content_hash FROM documents WHERE id=%s", (doc_id,)
    ).fetchone()[0]
    after_chunk_count = test_db.execute(
        "SELECT count(*) FROM chunks WHERE document_id=%s", (doc_id,)
    ).fetchone()[0]
    assert after_hash == old_hash
    assert after_chunk_count == old_chunk_count


def test_update_document_empty_body_rejected(test_db, fake_embedder, seed_doc):
    doc_id = seed_doc()
    with pytest.raises(ValueError, match="content is empty"):
        update_document(
            test_db,
            document_id=doc_id,
            embedder=fake_embedder,
            new_content="   \n\n   ",
        )


def test_update_document_body_required_embedder(test_db, fake_embedder, seed_doc):
    doc_id = seed_doc()
    with pytest.raises(ValueError, match="embedder is required"):
        update_document(
            test_db, document_id=doc_id, new_content="something fresh"
        )


def test_update_document_unknown_id_raises(test_db, fake_embedder, seed_doc):
    """A bogus UUID should raise ValueError before any DB write."""
    with pytest.raises(ValueError, match="document not found"):
        update_document(
            test_db,
            document_id="00000000-0000-0000-0000-000000000000",
            new_title="x",
        )


def test_update_document_content_type_change(test_db, fake_embedder, seed_doc):
    doc_id = seed_doc()
    result = update_document(
        test_db, document_id=doc_id, new_content_type="transcript"
    )
    assert result.fields_changed == ["content_type"]
    new_type = test_db.execute(
        "SELECT content_type FROM documents WHERE id=%s", (doc_id,)
    ).fetchone()[0]
    assert new_type == "transcript"


def test_update_document_tags_change(test_db, fake_embedder, seed_doc):
    doc_id = seed_doc()
    test_db.execute(
        "UPDATE documents SET tags=%s WHERE id=%s", (["one"], doc_id)
    )
    result = update_document(
        test_db, document_id=doc_id, new_tags=["two", "three"]
    )
    assert result.fields_changed == ["tags"]
    tags = test_db.execute(
        "SELECT tags FROM documents WHERE id=%s", (doc_id,)
    ).fetchone()[0]
    assert sorted(tags) == ["three", "two"]


def test_update_document_no_op_returns_empty_fields(test_db, fake_embedder, seed_doc):
    """Re-applying current values is a successful no-op (empty fields_changed)."""
    doc_id = seed_doc(title="T", metadata={"a": 1})
    result = update_document(
        test_db,
        document_id=doc_id,
        new_title="T",
        metadata_patch={"a": 1},
    )
    assert result.fields_changed == []
    assert result.rechunked is False


def test_update_document_replace_metadata_no_op(test_db, fake_embedder, seed_doc):
    """replace_metadata with identical blob is detected as a no-op."""
    doc_id = seed_doc(metadata={"a": 1, "b": 2})
    result = update_document(
        test_db,
        document_id=doc_id,
        metadata_patch={"a": 1, "b": 2},
        replace_metadata=True,
    )
    assert result.fields_changed == []
