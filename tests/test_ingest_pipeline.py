"""Integration tests for the ingest pipeline (extract → chunk → embed → store)."""
import hashlib
import logging
from pathlib import Path

import pytest
from pytest_mock import MockerFixture

from brain.ingest import ExtractedDoc, IngestResult, ingest_document, update_document
from brain.vault.frontmatter import parse_frontmatter


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


# --- vault_root opt-in mirror writes ---------------------------------------
# These tests guard the regression that caused 419 vault files to be missing
# (DB rows existed but the mirror file was never written by `brain ingest*`).
# `vault_root=None` keeps the legacy DB-only behavior so existing callers,
# fixtures, and library users see no change.


def _ingest_manual_with_mirror(
    test_db, embedder, *, vault_root: Path, title: str, content: str
) -> IngestResult:
    """Test helper — manual stdin ingest with the new ``vault_root`` opt-in."""
    return ingest_document(
        test_db,
        embedder=embedder,
        doc=ExtractedDoc(
            title=title,
            content=content,
            content_type="note",
            source_path=None,
            metadata={},
        ),
        source_kind="manual",
        vault_root=vault_root,
    )


def test_ingest_document_writes_vault_mirror(
    test_db, fake_embedder, tmp_path: Path
) -> None:
    """Passing ``vault_root`` materializes the doc as a Markdown file on disk.

    Regression for the original bug: every ingest path wrote DB rows + chunks
    but skipped the vault mirror, so Quartz had no file to link to until a
    manual ``brain vault export --force`` was run.
    """
    # Setup
    vault = tmp_path / "vault"

    # Exercise
    result = _ingest_manual_with_mirror(
        test_db,
        fake_embedder,
        vault_root=vault,
        title="Smoke Note",
        content="hello brain",
    )

    # Verify
    assert result.created is True
    assert result.document_id is not None
    target = vault / "_ingested" / "manual" / "smoke-note.md"
    assert target.is_file(), f"expected mirror at {target}"
    text = target.read_text(encoding="utf-8")
    _, body = parse_frontmatter(text)
    assert body.strip() == "hello brain"


def test_ingest_document_no_mirror_when_vault_root_omitted(
    test_db, fake_embedder, tmp_path: Path
) -> None:
    """Backwards-compat: omitting ``vault_root`` leaves the filesystem alone.

    Library callers (tests, internal pipelines) that don't care about the
    mirror must continue to get the legacy DB-only behavior.
    """
    # Setup
    vault = tmp_path / "vault"
    vault.mkdir()

    # Exercise — no vault_root argument
    result = ingest_document(
        test_db,
        embedder=fake_embedder,
        doc=ExtractedDoc(
            title="No Mirror",
            content="db only please",
            content_type="note",
            source_path=None,
            metadata={},
        ),
        source_kind="manual",
    )

    # Verify
    assert result.created is True
    # Nothing written under the vault root — the _ingested tree must not
    # even exist (we never called regenerate_vault_file).
    assert not (vault / "_ingested").exists()


def test_ingest_document_skip_does_not_write_mirror(
    test_db, fake_embedder, tmp_path: Path
) -> None:
    """A no-op repeat ingest does not rewrite the mirror file.

    The first ingest creates the row + the file. A second ingest of identical
    content is a content-hash skip in the DB layer (``created=False``) and
    must not touch the on-disk file. mtime stability is the observable
    signal — the body-hash skip in ``_write_doc_file`` would also catch a
    needless rewrite, but here we additionally guarantee
    ``regenerate_vault_file`` isn't even called on the skip path.
    """
    # Setup
    vault = tmp_path / "vault"
    first = _ingest_manual_with_mirror(
        test_db,
        fake_embedder,
        vault_root=vault,
        title="Stable",
        content="unchanged body",
    )
    target = vault / "_ingested" / "manual" / "stable.md"
    assert target.is_file()
    mtime_before = target.stat().st_mtime_ns

    # Exercise — re-ingest the same content
    second = _ingest_manual_with_mirror(
        test_db,
        fake_embedder,
        vault_root=vault,
        title="Stable",
        content="unchanged body",
    )

    # Verify
    assert first.document_id == second.document_id
    assert second.created is False
    assert target.stat().st_mtime_ns == mtime_before, (
        "mtime should be unchanged on a no-op re-ingest"
    )


def test_ingest_document_mirror_failure_does_not_roll_back_db(
    test_db,
    fake_embedder,
    tmp_path: Path,
    mocker: MockerFixture,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A vault mirror OSError is logged and swallowed — DB state survives.

    The mirror write runs OUTSIDE the DB transaction precisely so a transient
    filesystem error (read-only disk, permission denied, full disk) cannot
    abort an otherwise-successful ingest. Recovery is via
    ``brain vault export --force``, not by re-running every failed ingest.
    """
    # Setup — replace the imported regenerate_vault_file with one that fails.
    mocker.patch(
        "brain.ingest.regenerate_vault_file",
        side_effect=OSError("simulated disk failure"),
    )
    vault = tmp_path / "vault"

    # Exercise
    with caplog.at_level(logging.WARNING, logger="brain.ingest"):
        result = _ingest_manual_with_mirror(
            test_db,
            fake_embedder,
            vault_root=vault,
            title="Survives FS Failure",
            content="DB write must persist",
        )

    # Verify — DB succeeded, function returned normally, warning was logged.
    assert result.created is True
    assert result.document_id is not None
    db_row = test_db.execute(
        "SELECT id, content FROM documents WHERE id=%s", (result.document_id,)
    ).fetchone()
    assert db_row is not None
    assert db_row[1] == "DB write must persist"
    assert any(
        "vault mirror write failed" in r.message
        and "simulated disk failure" in r.message
        for r in caplog.records
    ), "expected a WARNING-level mirror failure record"


def test_update_document_mirror_failure_does_not_roll_back_db(
    test_db,
    fake_embedder,
    tmp_path: Path,
    mocker: MockerFixture,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """An ``OSError`` from the update-side mirror write is logged + swallowed.

    Symmetric counterpart to
    :func:`test_ingest_document_mirror_failure_does_not_roll_back_db`: the
    mirror write happens OUTSIDE the DB transaction so a transient filesystem
    failure on a body or frontmatter edit cannot abort an otherwise-successful
    DB update. Recovery is via ``brain vault export --force``.
    """
    # Setup — initial ingest succeeds with a real mirror; capture the doc id
    # and pre-update content_hash to compare against post-update state.
    vault = tmp_path / "vault"
    initial = _ingest_manual_with_mirror(
        test_db,
        fake_embedder,
        vault_root=vault,
        title="Update Survives FS Failure",
        content="original body",
    )
    assert initial.document_id is not None
    pre_hash_row = test_db.execute(
        "SELECT content_hash FROM documents WHERE id=%s", (initial.document_id,)
    ).fetchone()
    assert pre_hash_row is not None
    pre_hash = pre_hash_row[0]

    # Now patch regenerate_vault_file so the next call (from update_document)
    # raises. The earlier ingest has already returned, so the patch only
    # affects the update path.
    mocker.patch(
        "brain.ingest.regenerate_vault_file",
        side_effect=OSError("simulated disk failure"),
    )

    # Exercise — body change so we know the mirror write would be triggered.
    with caplog.at_level(logging.WARNING, logger="brain.ingest"):
        result = update_document(
            test_db,
            document_id=initial.document_id,
            embedder=fake_embedder,
            new_content="updated body",
            vault_root=vault,
        )

    # Verify — update_document returned normally with the body change applied,
    # the DB row reflects the new content, and exactly one WARNING was logged.
    assert result.rechunked is True
    assert "content" in result.fields_changed
    db_row = test_db.execute(
        "SELECT content, content_hash FROM documents WHERE id=%s",
        (initial.document_id,),
    ).fetchone()
    assert db_row is not None
    assert db_row[0] == "updated body"
    assert db_row[1] != pre_hash, "content_hash must reflect the new body"
    expected_hash = hashlib.sha256(b"updated body").hexdigest()
    assert db_row[1] == expected_hash
    mirror_failures = [
        r for r in caplog.records
        if r.levelno >= logging.WARNING
        and "vault mirror write failed" in r.message
        and str(initial.document_id) in r.message
        and "simulated disk failure" in r.message
    ]
    assert len(mirror_failures) == 1, (
        f"expected exactly one mirror-failure WARNING; got "
        f"{[r.message for r in mirror_failures]}"
    )


def test_update_document_body_change_rewrites_mirror(
    test_db, fake_embedder, tmp_path: Path
) -> None:
    """A body edit propagates to the on-disk mirror file.

    Without this, ``brain edit`` (and the MCP ``brain_edit`` tool) would
    silently drift: the DB knows the new content, the vault file still
    shows the old body to Quartz.
    """
    # Setup — initial ingest with mirror.
    vault = tmp_path / "vault"
    initial = _ingest_manual_with_mirror(
        test_db,
        fake_embedder,
        vault_root=vault,
        title="Editable Note",
        content="version one",
    )
    assert initial.document_id is not None
    target = vault / "_ingested" / "manual" / "editable-note.md"
    assert "version one" in target.read_text(encoding="utf-8")

    # Exercise — edit the body via update_document.
    update_document(
        test_db,
        document_id=initial.document_id,
        embedder=fake_embedder,
        new_content="version two — rewritten body",
        vault_root=vault,
    )

    # Verify — file body reflects the new content.
    text = target.read_text(encoding="utf-8")
    _, body = parse_frontmatter(text)
    assert body.strip() == "version two — rewritten body"


@pytest.mark.parametrize(
    ("field", "old_value", "new_value"),
    [
        ("title", "old-name", "new-name"),
        ("tags", ["alpha"], ["alpha", "beta"]),
        # ``metadata`` projects to the frontmatter via the ``aliases`` key
        # (see ``_aliases_from_metadata`` in ``brain.vault.export``); using
        # it here exercises both the ``metadata`` branch of
        # ``_MIRROR_FRONTMATTER_FIELDS`` and the metadata→aliases mapping.
        ("metadata", {"aliases": ["alpha"]}, {"aliases": ["alpha", "beta"]}),
        ("content_type", "note", "transcript"),
    ],
)
def test_update_document_frontmatter_only_rewrites_mirror(
    test_db,
    fake_embedder,
    tmp_path: Path,
    field: str,
    old_value: object,
    new_value: object,
) -> None:
    """A frontmatter-field-only edit refreshes the mirror's frontmatter.

    Frontmatter is derived from documents.title/tags/metadata/content_type,
    so any of those changes must trigger a mirror rewrite even when the body
    is untouched. Parametrized across all four fields so dropping one from
    ``_MIRROR_FRONTMATTER_FIELDS`` would fail the matching invocation
    instead of slipping through a single-branch test.

    Regression for the body-hash-skip bug discovered during phase review:
    ``_write_doc_file``'s body-hash check fingerprinted the body only, so a
    frontmatter-only edit with no slug change would silently skip the
    rewrite. Fixed by routing the per-doc ``regenerate_vault_file`` call
    through ``force=True``.
    """
    # Setup — ingest with the field's old value.
    vault = tmp_path / "vault"
    base_title = "fm-only-mirror" if field != "title" else old_value
    base_tags = old_value if field == "tags" else []
    base_meta = old_value if field == "metadata" else {}
    base_type = old_value if field == "content_type" else "note"
    initial = ingest_document(
        test_db,
        embedder=fake_embedder,
        doc=ExtractedDoc(
            title=base_title,
            content="body stays the same",
            content_type=base_type,
            source_path=None,
            metadata=dict(base_meta),
        ),
        source_kind="manual",
        tags=list(base_tags),
        vault_root=vault,
    )
    assert initial.document_id is not None
    # Slug derives from the title — capture the initial mirror so we can
    # confirm it exists pre-update.
    initial_slug = "old-name" if field == "title" else "fm-only-mirror"
    original_target = vault / "_ingested" / "manual" / f"{initial_slug}.md"
    assert original_target.is_file()

    # Exercise — change only the targeted field.
    update_kwargs: dict[str, object] = {
        "document_id": initial.document_id,
        "vault_root": vault,
    }
    if field == "title":
        update_kwargs["new_title"] = new_value
    elif field == "tags":
        update_kwargs["new_tags"] = new_value
    elif field == "metadata":
        update_kwargs["metadata_patch"] = new_value
        update_kwargs["replace_metadata"] = True
    elif field == "content_type":
        update_kwargs["new_content_type"] = new_value
    update_document(test_db, **update_kwargs)  # type: ignore[arg-type]

    # Verify — the mirror stays at the ORIGINAL slug for every field
    # (including ``title``, post the populate-vault_path fix). Rotating the
    # file on title edits would orphan the old mirror at the same UUID.
    # Frontmatter is regenerated in place to reflect the new value.
    target = original_target
    assert target.is_file(), (
        "regenerate_vault_file must materialize the mirror after a "
        f"{field} change"
    )
    if field == "title":
        # Pre-fix bug: a title rename rotated the slug and created a file at
        # the new-name path. Post-fix, no orphan should exist there.
        assert not (vault / "_ingested" / "manual" / "new-name.md").exists(), (
            "title edit must not rotate the slug — pre-fix bug created an "
            "orphan at the new-title path while leaving the original behind"
        )
    fm, _ = parse_frontmatter(target.read_text(encoding="utf-8"))
    if field == "title":
        assert fm["title"] == new_value
    elif field == "tags":
        assert sorted(fm.get("tags") or []) == sorted(new_value)  # type: ignore[arg-type]
    elif field == "metadata":
        # metadata → aliases is the only metadata→frontmatter projection.
        assert fm.get("aliases") == new_value["aliases"]  # type: ignore[index]
    elif field == "content_type":
        assert fm["content_type"] == new_value


def test_update_document_vault_kind_skipped(
    test_db, fake_embedder, tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """Vault-tier rows (``kind='vault'``) are skipped via DB pre-check.

    Vault-tier files are file-source-of-truth — the DB copy may lag behind
    user edits on disk. The pre-check on ``documents.kind`` (read in the
    same SELECT used for the rest of the update) guarantees we never even
    call ``regenerate_vault_file``, so there's no warning to log and no
    string-matched ValueError handler that could silently break if the
    error message is rephrased.
    """
    # Setup — normal ingest, then promote the row to kind='vault'.
    vault = tmp_path / "vault"
    initial = _ingest_manual_with_mirror(
        test_db,
        fake_embedder,
        vault_root=vault,
        title="Vault Authored",
        content="body owned by the vault file",
    )
    assert initial.document_id is not None
    target = vault / "_ingested" / "manual" / "vault-authored.md"
    target_mtime_before = target.stat().st_mtime_ns
    test_db.execute(
        "UPDATE documents SET kind='vault', vault_path=%s WHERE id=%s",
        ("projects/notes/vault-authored.md", initial.document_id),
    )

    # Exercise — must not raise; must not log a WARNING.
    with caplog.at_level(logging.WARNING, logger="brain.ingest"):
        result = update_document(
            test_db,
            document_id=initial.document_id,
            new_title="Renamed",
            vault_root=vault,
        )

    # Verify — DB title was updated; the original mirror was not rewritten
    # (regenerate was never called); no WARNING was emitted (pre-check
    # short-circuits before the try-block).
    assert "title" in result.fields_changed
    assert target.stat().st_mtime_ns == target_mtime_before
    new_title = test_db.execute(
        "SELECT title FROM documents WHERE id=%s", (initial.document_id,)
    ).fetchone()
    assert new_title is not None
    assert new_title[0] == "Renamed"
    mirror_warnings = [
        r for r in caplog.records
        if r.levelno >= logging.WARNING
        and "vault mirror write failed" in r.message
    ]
    assert mirror_warnings == [], (
        f"vault-tier skip must not log a mirror-failure WARNING; got "
        f"{[r.message for r in mirror_warnings]}"
    )
