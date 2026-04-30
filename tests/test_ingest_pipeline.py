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


def test_file_ingest_creates_manual_source_by_default(test_db, fake_embedder):
    """File-based ingests (``doc.source_path`` set) default to a "manual"
    source row with ``external_id = doc.source_path``. Regression for the
    bug where 691 manual files had ``source_id = NULL`` because the CLI
    passed ``source_kind="manual"`` but no external id, so
    :func:`_upsert_source` returned ``None``."""
    doc = ExtractedDoc(
        title="Project plan",
        content="manual notes body",
        content_type="markdown",
        source_path="/tmp/manual-default.md",
        metadata={},
    )
    result = ingest_document(test_db, embedder=fake_embedder, doc=doc)
    src = test_db.execute(
        "SELECT kind, external_id FROM sources WHERE id="
        "(SELECT source_id FROM documents WHERE id=%s)",
        (result.document_id,),
    ).fetchone()
    assert src is not None, "documents.source_id must reference a sources row"
    assert src[0] == "manual"
    assert src[1] == "/tmp/manual-default.md"


def test_explicit_source_kind_overrides_manual_default(test_db, fake_embedder):
    """An explicit ``source_kind`` wins over the file-ingest "manual" default,
    so a file-based ingest can still be tagged as e.g. ``krisp``."""
    doc = ExtractedDoc(
        title="krisp-as-file",
        content="explicit override body",
        content_type="markdown",
        source_path="/tmp/explicit-krisp.md",
        metadata={},
    )
    result = ingest_document(
        test_db, embedder=fake_embedder, doc=doc, source_kind="krisp"
    )
    src = test_db.execute(
        "SELECT kind FROM sources WHERE id="
        "(SELECT source_id FROM documents WHERE id=%s)",
        (result.document_id,),
    ).fetchone()
    assert src is not None
    assert src[0] == "krisp"


def test_stdin_ingest_unchanged(test_db, fake_embedder):
    """Stdin ingests (no ``source_path``) keep the prior behavior — caller
    must pass ``source_kind`` and ``source_external_id`` explicitly, and the
    resulting source row reflects exactly those values."""
    doc = ExtractedDoc(
        title="stdin no-regression",
        content="stdin body",
        content_type="transcript",
        source_path=None,
        metadata={},
    )
    result = ingest_document(
        test_db,
        embedder=fake_embedder,
        doc=doc,
        source_kind="krisp",
        source_external_id="meeting-99",
    )
    src = test_db.execute(
        "SELECT kind, external_id FROM sources WHERE id="
        "(SELECT source_id FROM documents WHERE id=%s)",
        (result.document_id,),
    ).fetchone()
    assert src is not None
    assert src[0] == "krisp"
    assert src[1] == "meeting-99"


def test_stdin_ingest_without_source_kind_raises(test_db, fake_embedder):
    """A stdin ingest (``source_path is None``) without ``source_kind`` is a
    programming error — the file-ingest default does not apply, so we
    surface a clear ``ValueError`` instead of silently dropping the source."""
    doc = ExtractedDoc(
        title="bad call",
        content="stdin body",
        content_type="transcript",
        source_path=None,
        metadata={},
    )
    with pytest.raises(ValueError, match="source_kind is required"):
        ingest_document(test_db, embedder=fake_embedder, doc=doc)


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


def test_file_ingest_unchanged_content_is_idempotent_noop(test_db, fake_embedder):
    """Re-ingesting the same file unchanged returns the existing id, no replace."""
    doc = ExtractedDoc(
        title="Notes", content="some body bytes", content_type="markdown",
        source_path="/tmp/notes.md", metadata={},
    )
    first = _ingest(test_db, fake_embedder, doc)
    second = _ingest(test_db, fake_embedder, doc)
    assert first.created is True
    assert second.created is False
    assert first.document_id == second.document_id


def test_file_ingest_replaces_in_place_when_content_changes(test_db, fake_embedder):
    """File ingest at the same ``source_path`` with new content replaces the row
    in place — DELETE + INSERT yields a new ``documents.id`` and a new
    ``content_hash``, and a single row remains at that ``source_path``."""
    path = "/tmp/notes/career.md"
    first = _ingest(
        test_db,
        fake_embedder,
        ExtractedDoc(
            title="Career", content="version one", content_type="markdown",
            source_path=path, metadata={},
        ),
    )
    assert first.created is True

    second = _ingest(
        test_db,
        fake_embedder,
        ExtractedDoc(
            title="Career", content="version two — updated", content_type="markdown",
            source_path=path, metadata={},
        ),
    )
    assert second.created is True
    assert second.document_id != first.document_id  # row replaced (delete + insert)

    rows = test_db.execute(
        "SELECT id, content, content_hash FROM documents WHERE source_path=%s",
        (path,),
    ).fetchall()
    assert len(rows) == 1
    surviving_id, content, content_hash = rows[0]
    assert str(surviving_id) == second.document_id
    assert content == "version two — updated"
    # The hash for the new body must differ from the original content's hash.
    import hashlib
    assert content_hash == hashlib.sha256(b"version two \xe2\x80\x94 updated").hexdigest()


def test_file_ingest_with_force_replaces_even_when_content_unchanged(
    test_db, fake_embedder
):
    """``force=True`` reinserts even when content (and hence hash) is unchanged."""
    doc = ExtractedDoc(
        title="Resume", content="same body bytes", content_type="markdown",
        source_path="/tmp/resume.md", metadata={},
    )
    first = _ingest(test_db, fake_embedder, doc)
    second = _ingest(test_db, fake_embedder, doc, force=True)
    assert first.created is True
    assert second.created is True
    assert second.document_id != first.document_id

    count = test_db.execute(
        "SELECT count(*) FROM documents WHERE source_path=%s", (doc.source_path,)
    ).fetchone()[0]
    assert count == 1


def test_stdin_ingest_still_dedups_by_content_hash(test_db, fake_embedder):
    """Stdin ingests (no ``source_path``) keep the prior content-hash dedup."""
    doc = ExtractedDoc(
        title="Krisp transcript", content="speaker A: hi\n\nspeaker B: hello",
        content_type="transcript", source_path=None, metadata={},
    )
    first = _ingest(test_db, fake_embedder, doc)
    second = _ingest(test_db, fake_embedder, doc)
    assert first.created is True
    assert second.created is False
    assert first.document_id == second.document_id


def test_two_files_with_same_content_at_different_paths_are_separate_docs(
    test_db, fake_embedder
):
    """Byte-identical content at distinct ``source_path``s yields two rows."""
    body = "shared boilerplate body"
    a = _ingest(
        test_db,
        fake_embedder,
        ExtractedDoc(
            title="A", content=body, content_type="markdown",
            source_path="/tmp/a.md", metadata={},
        ),
    )
    b = _ingest(
        test_db,
        fake_embedder,
        ExtractedDoc(
            title="B", content=body, content_type="markdown",
            source_path="/tmp/b.md", metadata={},
        ),
    )
    assert a.created is True
    assert b.created is True
    assert a.document_id != b.document_id

    rows = test_db.execute(
        "SELECT count(*) FROM documents WHERE source_path IN (%s, %s)",
        ("/tmp/a.md", "/tmp/b.md"),
    ).fetchone()[0]
    assert rows == 2


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
