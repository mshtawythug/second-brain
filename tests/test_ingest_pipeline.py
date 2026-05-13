"""Integration tests for the ingest pipeline (extract → chunk → embed → store)."""
import hashlib
import logging
import uuid
from dataclasses import dataclass
from pathlib import Path

import psycopg
import pytest
from pytest_mock import MockerFixture

from brain.enrichment import SummaryResult
from brain.errors import IngestAmbiguousSource
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
    """Re-ingesting the same external_id reuses the source row AND updates the doc in place.

    Post-fix behavior: the second ingest with the same (kind, external_id) finds the
    existing document and UPDATEs it in place. Both r1 and r2 reference the same doc UUID.
    The source row is reused (same source_id before and after).
    """
    doc1 = ExtractedDoc(
        title="T1", content="c1", content_type="email", source_path=None, metadata={}
    )
    r1 = ingest_document(
        test_db,
        embedder=fake_embedder,
        doc=doc1,
        source_kind="gmail",
        source_external_id="msg-1",
    )
    # Capture source_id of the first doc.
    src_id_before = test_db.execute(
        "SELECT source_id FROM documents WHERE id=%s", (r1.document_id,)
    ).fetchone()[0]

    doc2 = ExtractedDoc(
        title="T2", content="c2", content_type="email", source_path=None, metadata={}
    )
    r2 = ingest_document(
        test_db,
        embedder=fake_embedder,
        doc=doc2,
        source_kind="gmail",
        source_external_id="msg-1",
    )
    # Post-fix: UPDATE-in-place — doc UUID is stable, source row is the same.
    assert r2.document_id == r1.document_id
    src_id_after = test_db.execute(
        "SELECT source_id FROM documents WHERE id=%s", (r1.document_id,)
    ).fetchone()[0]
    assert src_id_after == src_id_before  # source row reused


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
    """File ingest at the same ``source_path`` with new content UPDATEs the row
    in place — the document UUID is preserved and body_changed=True."""
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
    # Post-fix: UPDATE-in-place — UUID is stable, created=False, body_changed=True.
    assert second.created is False
    assert second.body_changed is True
    assert second.document_id == first.document_id  # UUID preserved across content change

    rows = test_db.execute(
        "SELECT id, content, content_hash FROM documents WHERE source_path=%s",
        (path,),
    ).fetchall()
    assert len(rows) == 1
    surviving_id, content, content_hash = rows[0]
    assert str(surviving_id) == first.document_id
    assert content == "version two — updated"
    # The hash for the new body must differ from the original content's hash.
    assert content_hash == hashlib.sha256(b"version two \xe2\x80\x94 updated").hexdigest()


def test_file_ingest_with_force_replaces_even_when_content_unchanged(
    test_db, fake_embedder
):
    """``force=True`` on same content UPDATEs in place — UUID preserved, body_changed=False."""
    doc = ExtractedDoc(
        title="Resume", content="same body bytes", content_type="markdown",
        source_path="/tmp/resume.md", metadata={},
    )
    first = _ingest(test_db, fake_embedder, doc)
    second = _ingest(test_db, fake_embedder, doc, force=True)
    assert first.created is True
    # Post-fix: UPDATE-in-place — created=False (row exists), body_changed=False (same body).
    assert second.created is False
    assert second.body_changed is False
    assert second.document_id == first.document_id  # UUID preserved

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


# --- Wave Q2-SUMMARY-WIKI: `summary` is mirrored into frontmatter ----------
#
# Q1-D writes `documents.summary` via the post-ingest enrich hook (inside the
# same transaction as the INSERT). Q2 plumbs that value into the mirror
# writer so the on-disk `.md` carries `summary: …` in its YAML frontmatter,
# which the Quartz `SummaryLede` component reads at render time. These tests
# pin two contracts: (1) a fresh ingest with an enricher writes summary
# frontmatter end-to-end, and (2) a body update that re-enriches refreshes
# the mirror's summary line.


def _stub_enricher(text: str = "A canned two-sentence summary used by Q2 tests."):
    """Return a minimal enricher honoring the OllamaEnricher surface.

    NOT a monkey-patch — it's an explicit test double the ingest pipeline
    accepts via its public ``enricher=`` kwarg.
    """
    from dataclasses import dataclass

    from brain.enrichment import SummaryResult

    @dataclass
    class _Enricher:
        model: str = "llama3.1:8b"
        summary_text: str = text
        calls: int = 0

        def count_tokens(self, text: str) -> int:
            return max(1, len(text) // 4)

        def summarize(self, title: str, content: str):
            self.calls += 1
            return SummaryResult(summary=self.summary_text, model=self.model)

    return _Enricher()


def test_ingest_document_writes_summary_into_mirror_frontmatter(
    test_db, fake_embedder, tmp_path: Path
) -> None:
    """A fresh ingest with an enricher mirrors `summary:` to the on-disk file.

    The enrich post-ingest hook writes ``documents.summary`` INSIDE the
    ingest transaction. The mirror write at the bottom of ``ingest_document``
    runs after commit — by that point the DB already carries the summary,
    so :func:`brain.vault.export._build_frontmatter` picks it up and the
    resulting `.md` file's frontmatter carries `summary: …`.
    """
    vault = tmp_path / "vault"
    enricher = _stub_enricher("Two-sentence wiki TL;DR.")
    # Long-enough body that the enrich min-tokens gate (50) passes.
    long_body = "Body text. " * 50

    result = ingest_document(
        test_db,
        embedder=fake_embedder,
        doc=ExtractedDoc(
            title="Summary Mirror Note",
            content=long_body,
            content_type="note",
            source_path=None,
            metadata={},
        ),
        source_kind="manual",
        vault_root=vault,
        enricher=enricher,
    )

    assert result.document_id is not None
    assert enricher.calls == 1, "enrich hook should fire once on a fresh ingest"
    target = vault / "_ingested" / "manual" / "summary-mirror-note.md"
    assert target.is_file()
    fields, _body = parse_frontmatter(target.read_text(encoding="utf-8"))
    assert fields.get("summary") == "Two-sentence wiki TL;DR.", (
        "expected the enrich hook's summary to land in the mirror frontmatter"
    )


def test_ingest_document_without_enricher_omits_summary_from_mirror(
    test_db, fake_embedder, tmp_path: Path
) -> None:
    """No enricher → DB summary stays NULL → mirror has no `summary:` key.

    The lede component returns ``null`` for missing/non-string summaries,
    so leaving the frontmatter key absent (rather than ``summary: null``)
    keeps the rendered HTML clean.
    """
    vault = tmp_path / "vault"
    result = _ingest_manual_with_mirror(
        test_db,
        fake_embedder,
        vault_root=vault,
        title="No Enricher Note",
        content="hello brain",
    )
    assert result.document_id is not None
    target = vault / "_ingested" / "manual" / "no-enricher-note.md"
    assert target.is_file()
    fields, _body = parse_frontmatter(target.read_text(encoding="utf-8"))
    assert "summary" not in fields, (
        "no enricher should leave the mirror frontmatter free of a `summary:` key"
    )


def test_update_document_frontmatter_edit_preserves_summary_in_mirror(
    test_db, fake_embedder, tmp_path: Path
) -> None:
    """A frontmatter-only edit on an enriched doc keeps `summary:` on disk.

    ``_MIRROR_FRONTMATTER_FIELDS`` (extended in Q2 to include
    ``summary``) is the gating set for "did this change require a
    mirror rewrite?". When the user renames a doc whose
    ``documents.summary`` is already populated, the post-update mirror
    rewrite re-emits the existing summary from the DB column — no
    enricher call needed, no on-disk drift.
    """
    vault = tmp_path / "vault"
    enricher = _stub_enricher("Persistent canned summary.")
    long_body = "Initial body content. " * 50

    initial = ingest_document(
        test_db,
        embedder=fake_embedder,
        doc=ExtractedDoc(
            title="Original Title",
            content=long_body,
            content_type="note",
            source_path=None,
            metadata={},
        ),
        source_kind="manual",
        vault_root=vault,
        enricher=enricher,
    )
    assert initial.document_id is not None
    original_target = vault / "_ingested" / "manual" / "original-title.md"
    fields, _ = parse_frontmatter(original_target.read_text(encoding="utf-8"))
    assert fields.get("summary") == "Persistent canned summary."

    # Title-only edit — body unchanged, summary unchanged in the DB.
    # Mirror rewrite triggers via ``title`` ∈ _MIRROR_FRONTMATTER_FIELDS;
    # the rewritten file must still carry the original summary line so
    # the lede component keeps rendering on the next Quartz build.
    update_document(
        test_db,
        document_id=initial.document_id,
        new_title="Renamed Title",
        vault_root=vault,
    )
    fields, _ = parse_frontmatter(original_target.read_text(encoding="utf-8"))
    assert fields["title"] == "Renamed Title"
    assert fields.get("summary") == "Persistent canned summary.", (
        "the title-edit mirror rewrite must preserve the existing summary "
        "frontmatter (sourced from documents.summary)"
    )


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


# ---------------------------------------------------------------------------
# Force re-ingest UPDATE-in-place regression tests (plan 2026-05-12)
# ---------------------------------------------------------------------------

# --------------- Shared helpers for this section ----------------------------

@dataclass
class _FakeEnricher:
    """Minimal in-memory enricher for regression tests.

    Not a monkey-patch — accepted via the public ``enricher=`` kwarg on
    ingest_document. Counts calls so tests can verify skip rules fire.
    """

    model: str = "test-model"
    summary_text: str = "Canned summary for tests."
    calls: int = 0

    def count_tokens(self, text: str) -> int:
        return max(1, len(text) // 4)

    def summarize(self, title: str, content: str) -> SummaryResult:
        self.calls += 1
        return SummaryResult(summary=self.summary_text, model=self.model)


def _ingest_sourced(
    conn: psycopg.Connection,
    embedder: object,
    *,
    external_id: str,
    content: str = "krisp body content for testing purposes.",
    title: str = "Sourced doc",
    tags: list[str] | None = None,
    force: bool = False,
) -> IngestResult:
    """Helper: ingest a krisp stdin doc with a stable external_id."""
    return ingest_document(
        conn,
        embedder=embedder,  # type: ignore[arg-type]
        doc=ExtractedDoc(
            title=title,
            content=content,
            content_type="transcript",
            source_path=None,
            metadata={},
        ),
        source_kind="krisp",
        source_external_id=external_id,
        tags=tags or [],
        force=force,
    )


# Test #1
def test_force_reingest_sourced_preserves_document_id(test_db, fake_embedder):
    """Re-ingest with --force (same body) keeps the UUID and tags; body_changed=False."""
    r1 = _ingest_sourced(test_db, fake_embedder, external_id="meet-001", tags=["a", "b"])
    assert r1.created is True
    doc_id = r1.document_id

    r2 = _ingest_sourced(test_db, fake_embedder, external_id="meet-001", force=True)
    assert r2.created is False
    assert r2.body_changed is False
    assert r2.document_id == doc_id  # UUID preserved

    tags_row = test_db.execute(
        "SELECT tags FROM documents WHERE id=%s", (doc_id,)
    ).fetchone()
    assert tags_row is not None
    assert sorted(tags_row[0]) == ["a", "b"]  # curated tags preserved


# Test #2
def test_force_reingest_sourced_with_changed_body_updates_in_place(test_db, fake_embedder):
    """Same external_id, different content, --force → UUID stable, body_changed=True."""
    r1 = _ingest_sourced(
        test_db, fake_embedder, external_id="meet-002", content="version one body text."
    )
    assert r1.created is True
    doc_id = r1.document_id
    old_chunk_ids = {
        row[0] for row in test_db.execute(
            "SELECT id FROM chunks WHERE document_id=%s", (doc_id,)
        ).fetchall()
    }

    r2 = _ingest_sourced(
        test_db, fake_embedder,
        external_id="meet-002",
        content="version two body text — completely different.",
        force=True,
    )
    assert r2.created is False
    assert r2.body_changed is True
    assert r2.document_id == doc_id  # UUID stable

    content_row = test_db.execute(
        "SELECT content FROM documents WHERE id=%s", (doc_id,)
    ).fetchone()
    assert content_row is not None
    assert content_row[0] == "version two body text — completely different."

    new_chunk_ids = {
        row[0] for row in test_db.execute(
            "SELECT id FROM chunks WHERE document_id=%s", (doc_id,)
        ).fetchall()
    }
    assert new_chunk_ids  # chunks rebuilt
    assert new_chunk_ids.isdisjoint(old_chunk_ids)  # row UUIDs changed


# Test #3
def test_no_force_same_hash_sourced_short_circuits(test_db, fake_embedder):
    """Same external_id + same content + no --force → no-op, no chunks rebuilt."""
    r1 = _ingest_sourced(test_db, fake_embedder, external_id="meet-003")
    chunk_ids_before = {
        row[0] for row in test_db.execute(
            "SELECT id FROM chunks WHERE document_id=%s", (r1.document_id,)
        ).fetchall()
    }

    r2 = _ingest_sourced(test_db, fake_embedder, external_id="meet-003")
    assert r2.created is False
    assert r2.body_changed is False
    assert r2.document_id == r1.document_id

    chunk_ids_after = {
        row[0] for row in test_db.execute(
            "SELECT id FROM chunks WHERE document_id=%s", (r1.document_id,)
        ).fetchall()
    }
    assert chunk_ids_after == chunk_ids_before  # chunks untouched


# Test #4
def test_force_reingest_unions_tags_with_existing_sourced(test_db, fake_embedder):
    """Re-ingest with --force unions incoming tags with existing curated tags.
    Also verifies chunks.tags_text reflects merged_tags not just incoming tags.
    """
    r1 = _ingest_sourced(
        test_db, fake_embedder, external_id="meet-004", tags=["a", "b"]
    )
    doc_id = r1.document_id

    r2 = _ingest_sourced(
        test_db, fake_embedder, external_id="meet-004", tags=["c"], force=True
    )
    assert r2.created is False
    assert r2.document_id == doc_id

    tags_row = test_db.execute(
        "SELECT tags FROM documents WHERE id=%s", (doc_id,)
    ).fetchone()
    assert tags_row is not None
    merged = tags_row[0]
    # Insertion-order union: a, b from existing; c from incoming.
    # normalize_tags preserves first-seen insertion order.
    assert "a" in merged
    assert "b" in merged
    assert "c" in merged

    # chunks.tags_text must reflect merged_tags (not just incoming "c").
    chunk_tags = test_db.execute(
        "SELECT DISTINCT tags_text FROM chunks WHERE document_id=%s", (doc_id,)
    ).fetchall()
    assert chunk_tags, "expected at least one chunk"
    tags_text = chunk_tags[0][0]
    assert "a" in tags_text
    assert "b" in tags_text
    assert "c" in tags_text


# Test #5
def test_stdin_with_no_external_id_uses_content_hash_dedup(test_db, fake_embedder):
    """stdin ingest with no source_external_id still hits the content_hash fallback."""
    content = "stdin manual body for hash dedup test."
    r1 = ingest_document(
        test_db,
        embedder=fake_embedder,
        doc=ExtractedDoc(
            title="Manual stdin", content=content, content_type="note",
            source_path=None, metadata={},
        ),
        source_kind="manual",
    )
    assert r1.created is True

    r2 = ingest_document(
        test_db,
        embedder=fake_embedder,
        doc=ExtractedDoc(
            title="Manual stdin", content=content, content_type="note",
            source_path=None, metadata={},
        ),
        source_kind="manual",
    )
    assert r2.created is False
    assert r2.document_id == r1.document_id  # same-hash dedup


# Test #6
def test_force_reingest_file_path_preserves_document_id(test_db, fake_embedder):
    """--force on same file preserves UUID and existing tags."""
    path = "/tmp/file006.txt"
    r1 = ingest_document(
        test_db,
        embedder=fake_embedder,
        doc=ExtractedDoc(
            title="File 6", content="file six body.", content_type="txt",
            source_path=path, metadata={},
        ),
        tags=["keep-this"],
    )
    assert r1.created is True
    doc_id = r1.document_id

    r2 = ingest_document(
        test_db,
        embedder=fake_embedder,
        doc=ExtractedDoc(
            title="File 6", content="file six body.", content_type="txt",
            source_path=path, metadata={},
        ),
        tags=[],
        force=True,
    )
    assert r2.created is False
    assert r2.body_changed is False
    assert r2.document_id == doc_id  # UUID preserved

    tags_row = test_db.execute(
        "SELECT tags FROM documents WHERE id=%s", (doc_id,)
    ).fetchone()
    assert tags_row is not None
    assert "keep-this" in tags_row[0]  # curated tag preserved


# Test #7
def test_force_reingest_file_path_with_changed_body_updates_in_place(test_db, fake_embedder):
    """Same source_path, different body, --force → UUID stable, body_changed=True."""
    path = "/tmp/file007.txt"
    r1 = ingest_document(
        test_db,
        embedder=fake_embedder,
        doc=ExtractedDoc(
            title="File 7", content="version one file body.", content_type="txt",
            source_path=path, metadata={},
        ),
    )
    assert r1.created is True
    doc_id = r1.document_id

    r2 = ingest_document(
        test_db,
        embedder=fake_embedder,
        doc=ExtractedDoc(
            title="File 7", content="version two file body — updated.", content_type="txt",
            source_path=path, metadata={},
        ),
        force=True,
    )
    assert r2.created is False
    assert r2.body_changed is True
    assert r2.document_id == doc_id

    content_row = test_db.execute(
        "SELECT content FROM documents WHERE id=%s", (doc_id,)
    ).fetchone()
    assert content_row is not None
    assert content_row[0] == "version two file body — updated."


# Test #8
def test_force_reingest_unions_tags_with_existing_file(test_db, fake_embedder):
    """File re-ingest unions incoming tags with existing curated tags.
    Also verifies chunks.tags_text reflects merged_tags.
    """
    path = "/tmp/file008.txt"
    r1 = ingest_document(
        test_db,
        embedder=fake_embedder,
        doc=ExtractedDoc(
            title="File 8", content="file eight body.", content_type="txt",
            source_path=path, metadata={},
        ),
        tags=["a"],
    )
    doc_id = r1.document_id

    r2 = ingest_document(
        test_db,
        embedder=fake_embedder,
        doc=ExtractedDoc(
            title="File 8", content="file eight body.", content_type="txt",
            source_path=path, metadata={},
        ),
        tags=["b"],
        force=True,
    )
    assert r2.document_id == doc_id

    tags_row = test_db.execute(
        "SELECT tags FROM documents WHERE id=%s", (doc_id,)
    ).fetchone()
    assert tags_row is not None
    assert "a" in tags_row[0]
    assert "b" in tags_row[0]

    chunk_tags = test_db.execute(
        "SELECT DISTINCT tags_text FROM chunks WHERE document_id=%s", (doc_id,)
    ).fetchall()
    assert chunk_tags
    tags_text = chunk_tags[0][0]
    assert "a" in tags_text
    assert "b" in tags_text


# Test #9
def test_force_reingest_gmail_thread_unions_tags(test_db, fake_embedder):
    """Gmail-thread re-ingest also uses union semantics (was overwrite before)."""
    thread_id = "thread-009"
    r1 = ingest_document(
        test_db,
        embedder=fake_embedder,
        doc=ExtractedDoc(
            title="Thread 9", content="email thread body for testing.",
            content_type="email_thread", source_path=None,
            metadata={"thread_id": thread_id},
        ),
        source_kind="gmail",
        source_external_id="msg-9a",
        tags=["curated"],
    )
    assert r1.created is True
    doc_id = r1.document_id

    # Re-ingest with new body and NO tags — old behavior would wipe "curated".
    r2 = ingest_document(
        test_db,
        embedder=fake_embedder,
        doc=ExtractedDoc(
            title="Thread 9 updated", content="email thread body updated with new message.",
            content_type="email_thread", source_path=None,
            metadata={"thread_id": thread_id},
        ),
        source_kind="gmail",
        source_external_id="msg-9b",
        tags=[],
    )
    assert r2.created is False
    assert r2.document_id == doc_id

    tags_row = test_db.execute(
        "SELECT tags FROM documents WHERE id=%s", (doc_id,)
    ).fetchone()
    assert tags_row is not None
    assert "curated" in tags_row[0]  # preserved under union semantics


# Test #10
def test_force_reingest_preserves_links_derived_unresolved_rows(test_db, fake_embedder):
    """UPDATE-in-place preserves links/derived_links/unresolved_links referencing the doc.

    The old DELETE+INSERT would CASCADE-delete these rows. The new UPDATE-in-place
    keeps them. Fixture inserts rows directly via SQL (ingest_document does NOT
    materialize wiki links — that's a vault-sync responsibility).
    """
    # Setup: ingest doc A (target) and doc C (source of links)
    r_a = _ingest_sourced(
        test_db, fake_embedder, external_id="fk-test-a",
        content="target document body for FK test."
    )
    r_c = _ingest_sourced(
        test_db, fake_embedder, external_id="fk-test-c",
        content="source document body for FK test."
    )
    a_id = r_a.document_id
    c_id = r_c.document_id

    # Direct SQL: insert links, derived_links, unresolved_links.
    # Verified column names against migrations/003_vault_model.sql (links +
    # unresolved_links) and migrations/005_derived_links.sql (derived_links).
    test_db.execute(
        "INSERT INTO links (src_document_id, dst_document_id, link_text, link_kind, display_text) "
        "VALUES (%s, %s, 'A', 'wiki', NULL)",
        (c_id, a_id),
    )
    test_db.execute(
        "INSERT INTO derived_links (src_document_id, dst_document_id, rule, evidence, weight) "
        "VALUES (%s, %s, 'shared_thread', '{}'::jsonb, 1.0)",
        (c_id, a_id),
    )
    test_db.execute(
        "INSERT INTO unresolved_links (src_document_id, link_text, link_kind, display_text) "
        "VALUES (%s, 'NonExistent', 'wiki', NULL)",
        (c_id,),
    )

    # Action: re-ingest A with --force (same body). UUID must be preserved.
    r_a2 = _ingest_sourced(
        test_db, fake_embedder, external_id="fk-test-a",
        content="target document body for FK test.",
        force=True,
    )
    assert r_a2.document_id == a_id

    # Verify links and derived_links still reference A's original UUID.
    links_count = test_db.execute(
        "SELECT count(*) FROM links WHERE dst_document_id=%s", (a_id,)
    ).fetchone()[0]
    assert links_count == 1, "links row referencing A's UUID must survive UPDATE-in-place"

    derived_count = test_db.execute(
        "SELECT count(*) FROM derived_links WHERE dst_document_id=%s", (a_id,)
    ).fetchone()[0]
    assert derived_count == 1, "derived_links row must survive UPDATE-in-place"

    # Re-ingest C with --force. unresolved_links row (src=C) must survive.
    r_c2 = _ingest_sourced(
        test_db, fake_embedder, external_id="fk-test-c",
        content="source document body for FK test.",
        force=True,
    )
    assert r_c2.document_id == c_id

    unresolved_count = test_db.execute(
        "SELECT count(*) FROM unresolved_links WHERE src_document_id=%s", (c_id,)
    ).fetchone()[0]
    assert unresolved_count == 1, "unresolved_links row (src=C) must survive UPDATE-in-place"


# Test #11
def test_force_reingest_same_body_still_replaces_chunks(test_db, fake_embedder):
    """_update_doc_in_place always rebuilds chunks, even on same-body force re-ingest.

    chunk row UUIDs change; per-chunk content stays identical. body_changed=False
    (content_hash invariant) but chunks are mechanically rebuilt. These two
    assertions are NOT contradictory — see plan §body_changed contract.
    """
    r1 = _ingest_sourced(
        test_db, fake_embedder, external_id="meet-011",
        content="chunk test body paragraph one.\n\nparagraph two."
    )
    doc_id = r1.document_id
    chunks_before = test_db.execute(
        "SELECT id, content FROM chunks WHERE document_id=%s ORDER BY chunk_index",
        (doc_id,),
    ).fetchall()
    old_chunk_ids = {row[0] for row in chunks_before}
    old_contents = [row[1] for row in chunks_before]

    r2 = _ingest_sourced(
        test_db, fake_embedder, external_id="meet-011",
        content="chunk test body paragraph one.\n\nparagraph two.",
        force=True,
    )
    assert r2.body_changed is False  # same content_hash
    assert r2.document_id == doc_id

    chunks_after = test_db.execute(
        "SELECT id, content FROM chunks WHERE document_id=%s ORDER BY chunk_index",
        (doc_id,),
    ).fetchall()
    new_chunk_ids = {row[0] for row in chunks_after}
    new_contents = [row[1] for row in chunks_after]

    # Chunk row UUIDs changed (DELETE + INSERT).
    assert new_chunk_ids.isdisjoint(old_chunk_ids), (
        "chunk row UUIDs must be replaced by _update_doc_in_place even on same-body force"
    )
    # But per-chunk content is identical (same body, same chunker output).
    assert new_contents == old_contents

    # documents.content_hash is stable.
    hash_row = test_db.execute(
        "SELECT content_hash FROM documents WHERE id=%s", (doc_id,)
    ).fetchone()
    expected = hashlib.sha256(
        b"chunk test body paragraph one.\n\nparagraph two."
    ).hexdigest()
    assert hash_row is not None
    assert hash_row[0] == expected


# Test #12
def test_force_reingest_with_vault_root_regenerates_mirror_without_orphan(
    test_db, fake_embedder, tmp_path: Path
):
    """--force with same body regenerates mirror (tags may have changed); no orphan file."""
    vault = tmp_path / "vault"
    path = "/tmp/file012.txt"
    r1 = ingest_document(
        test_db,
        embedder=fake_embedder,
        doc=ExtractedDoc(
            title="Mirror Test", content="mirror test body content.",
            content_type="txt", source_path=path, metadata={},
        ),
        tags=["initial-tag"],
        vault_root=vault,
    )
    assert r1.created is True
    doc_id = r1.document_id

    mirror_dir = vault / "_ingested" / "manual"
    mirrors = list(mirror_dir.glob("*.md"))
    assert len(mirrors) == 1
    mirror_file = mirrors[0]

    # Re-ingest with --force, adding a new tag.
    r2 = ingest_document(
        test_db,
        embedder=fake_embedder,
        doc=ExtractedDoc(
            title="Mirror Test", content="mirror test body content.",
            content_type="txt", source_path=path, metadata={},
        ),
        tags=["new-tag"],
        force=True,
        vault_root=vault,
    )
    assert r2.created is False
    assert r2.document_id == doc_id

    # Mirror still at the original slug — no orphan file created.
    assert mirror_file.is_file(), f"mirror file must still exist: {mirror_file}"
    mirrors_after = list(mirror_dir.glob("*.md"))
    assert len(mirrors_after) == 1, "no orphan mirror files must be created on --force"

    # Frontmatter reflects union of tags.
    from brain.vault.frontmatter import parse_frontmatter
    fm, _ = parse_frontmatter(mirror_file.read_text(encoding="utf-8"))
    assert "initial-tag" in (fm.get("tags") or [])
    assert "new-tag" in (fm.get("tags") or [])


# Test #13
def test_force_reingest_skips_reenrichment_when_summary_matches(test_db, fake_embedder):
    """D11 idempotency: same body + same model → summary unchanged. Changed body → regenerates."""
    enricher = _FakeEnricher()
    long_content = "Enrichment test body content. " * 60  # well above min_tokens

    r1 = _ingest_sourced(
        test_db, fake_embedder, external_id="meet-013", content=long_content,
    )
    doc_id = r1.document_id
    # Manually set a summary (simulating a prior enrich run).
    test_db.execute(
        "UPDATE documents SET summary=%s, summary_model=%s, summary_at=NOW() WHERE id=%s",
        ("Prior summary text.", enricher.model, doc_id),
    )

    # Re-ingest with --force, same body → D11 skip (same hash + same model).
    _ingest_sourced(
        test_db, fake_embedder, external_id="meet-013",
        content=long_content, force=True,
    )
    # enricher was NOT passed — enrich hook won't fire regardless. Just verify summary preserved.
    summary_row = test_db.execute(
        "SELECT summary FROM documents WHERE id=%s", (doc_id,)
    ).fetchone()
    assert summary_row is not None
    assert summary_row[0] == "Prior summary text."

    # Now re-ingest with DIFFERENT content + enricher provided → summary regenerated.
    # Under the body_changed-kwarg approach, the helper signals the hook that
    # the body changed; the hook regenerates rather than skipping via D11.
    new_content = "Completely different body content. " * 60
    ingest_document(
        test_db,
        embedder=fake_embedder,
        doc=ExtractedDoc(
            title="Sourced doc", content=new_content,
            content_type="transcript", source_path=None, metadata={},
        ),
        source_kind="krisp",
        source_external_id="meet-013",
        tags=[],
        force=True,
        enricher=enricher,  # type: ignore[arg-type]
        enrich=True,
        enrich_min_tokens=10,
    )
    summary_row2 = test_db.execute(
        "SELECT summary FROM documents WHERE id=%s", (doc_id,)
    ).fetchone()
    assert summary_row2 is not None
    assert summary_row2[0] == enricher.summary_text  # regenerated by hook

    # And confirm same-body re-ingest WITH the enricher still D11-skips:
    # existing summary matches the regenerated content_hash + same model, so
    # the hook short-circuits and the summary stays.
    ingest_document(
        test_db,
        embedder=fake_embedder,
        doc=ExtractedDoc(
            title="Sourced doc", content=new_content,
            content_type="transcript", source_path=None, metadata={},
        ),
        source_kind="krisp",
        source_external_id="meet-013",
        tags=[],
        force=True,
        enricher=enricher,  # type: ignore[arg-type]
        enrich=True,
        enrich_min_tokens=10,
    )
    summary_row3 = test_db.execute(
        "SELECT summary FROM documents WHERE id=%s", (doc_id,)
    ).fetchone()
    assert summary_row3 is not None
    assert summary_row3[0] == enricher.summary_text  # unchanged via D11 skip


# Test #14
def test_sourced_branch_raises_on_ambiguous_source(test_db, fake_embedder):
    """Multiple documents sharing one source key → IngestAmbiguousSource raised, no mutation."""
    # Ingest two different docs against the SAME external_id to create the ambiguous state.
    # We do this by inserting a second document row directly, bypassing the dedup logic.
    r1 = _ingest_sourced(
        test_db, fake_embedder, external_id="dup-src-014",
        content="doc A body content."
    )
    a_id = r1.document_id

    # Get the source_id so we can point a second doc at the same source row.
    source_id_row = test_db.execute(
        "SELECT source_id FROM documents WHERE id=%s", (a_id,)
    ).fetchone()
    assert source_id_row is not None
    source_id = source_id_row[0]

    # Manually insert a second document sharing the same source_id.
    content_b = "doc B body — completely different."
    h_b = hashlib.sha256(content_b.encode()).hexdigest()
    test_db.execute(
        """
        INSERT INTO documents (source_id, title, content, content_hash, content_type, kind)
        VALUES (%s, %s, %s, %s, %s, 'ingested')
        """,
        (source_id, "Dup B", content_b, h_b, "transcript"),
    )

    # Now attempt to re-ingest via the sourced branch. Should raise IngestAmbiguousSource.
    doc_count_before = test_db.execute("SELECT count(*) FROM documents").fetchone()[0]
    with pytest.raises(IngestAmbiguousSource, match="Multiple documents share source"):
        _ingest_sourced(
            test_db, fake_embedder, external_id="dup-src-014", force=True
        )

    # No DB mutation occurred (the exception aborted the transaction).
    doc_count_after = test_db.execute("SELECT count(*) FROM documents").fetchone()[0]
    assert doc_count_after == doc_count_before


# Test #15
def test_sourced_branch_falls_through_to_content_hash_when_no_match(test_db, fake_embedder):
    """Sourced lookup returns 0 rows → falls through to content_hash fallback."""
    content = "unique fallback content for test 15."
    # First: ingest manually (no external_id) so the content_hash row exists.
    r1 = ingest_document(
        test_db,
        embedder=fake_embedder,
        doc=ExtractedDoc(
            title="Fallback", content=content, content_type="note",
            source_path=None, metadata={},
        ),
        source_kind="manual",
    )
    assert r1.created is True
    doc_id = r1.document_id

    # Now ingest via sourced branch with a NEW external_id.
    # Sourced lookup finds 0 rows (no source with this kind+external_id).
    # Falls through to content_hash fallback — finds the existing row.
    r2 = ingest_document(
        test_db,
        embedder=fake_embedder,
        doc=ExtractedDoc(
            title="Fallback", content=content, content_type="note",
            source_path=None, metadata={},
        ),
        source_kind="krisp",
        source_external_id="new-id-015",
    )
    assert r2.created is False
    assert r2.document_id == doc_id  # content_hash fallback found the existing row

    # With --force, the content_hash fallback does DELETE+INSERT (new UUID).
    r3 = ingest_document(
        test_db,
        embedder=fake_embedder,
        doc=ExtractedDoc(
            title="Fallback", content=content, content_type="note",
            source_path=None, metadata={},
        ),
        source_kind="krisp",
        source_external_id="new-id-015",
        force=True,
    )
    assert r3.created is True
    assert r3.document_id != doc_id  # DELETE+INSERT (no sourced row existed to UPDATE)


# Test #16
def test_no_force_changed_body_sourced_still_updates_in_place(test_db, fake_embedder):
    """Same external_id, different body, NO --force → still UPDATEs in place."""
    r1 = _ingest_sourced(
        test_db, fake_embedder, external_id="meet-016",
        content="version one body sixteen."
    )
    doc_id = r1.document_id

    r2 = _ingest_sourced(
        test_db, fake_embedder, external_id="meet-016",
        content="version two body sixteen — different hash.",
    )
    assert r2.created is False
    assert r2.body_changed is True
    assert r2.document_id == doc_id  # UUID stable

    content_row = test_db.execute(
        "SELECT content FROM documents WHERE id=%s", (doc_id,)
    ).fetchone()
    assert content_row is not None
    assert content_row[0] == "version two body sixteen — different hash."


# Test #17
def test_no_force_changed_body_file_still_updates_in_place(test_db, fake_embedder):
    """Same source_path, different body, NO --force → UPDATEs in place."""
    path = "/tmp/file017.txt"
    r1 = ingest_document(
        test_db,
        embedder=fake_embedder,
        doc=ExtractedDoc(
            title="File 17", content="version one file seventeen.",
            content_type="txt", source_path=path, metadata={},
        ),
    )
    assert r1.created is True
    doc_id = r1.document_id

    r2 = ingest_document(
        test_db,
        embedder=fake_embedder,
        doc=ExtractedDoc(
            title="File 17", content="version two file seventeen — updated.",
            content_type="txt", source_path=path, metadata={},
        ),
    )
    assert r2.created is False
    assert r2.body_changed is True
    assert r2.document_id == doc_id

    content_row = test_db.execute(
        "SELECT content FROM documents WHERE id=%s", (doc_id,)
    ).fetchone()
    assert content_row is not None
    assert content_row[0] == "version two file seventeen — updated."


# Test #19
def test_content_hash_fallback_does_not_match_file_or_vault_rows(test_db, fake_embedder):
    """Scoped content_hash fallback never matches file-based or vault-tier docs.

    Regression for Codex pass 12 finding #3: the old unscoped SELECT would match
    any row with the same content_hash, including file-based ingests and vault-tier
    rows, causing --force to DELETE the wrong row silently.

    The new query scopes to: WHERE content_hash=%s AND kind='ingested' AND source_path IS NULL
    """
    shared_body = "shared body content for cross-tier test nineteen."
    file_path = "/tmp/file019.txt"

    # 1. File-based ingest (source_path IS NOT NULL).
    r_file = ingest_document(
        test_db,
        embedder=fake_embedder,
        doc=ExtractedDoc(
            title="File 19", content=shared_body, content_type="txt",
            source_path=file_path, metadata={},
        ),
    )
    file_doc_id = r_file.document_id
    assert file_doc_id is not None

    # 2. Vault-tier row — inserted directly via SQL (no ingestion path).
    h_shared = hashlib.sha256(shared_body.encode()).hexdigest()
    vault_doc_id = str(uuid.uuid4())
    test_db.execute(
        """
        INSERT INTO documents (id, source_id, title, content, content_hash,
                               content_type, kind, source_path)
        VALUES (%s, NULL, 'Vault 19', %s, %s, 'note', 'vault', NULL)
        """,
        (vault_doc_id, shared_body, h_shared),
    )

    # 3. stdin ingest (source_path IS NULL, no external_id) of the SAME body with --force.
    r_stdin = ingest_document(
        test_db,
        embedder=fake_embedder,
        doc=ExtractedDoc(
            title="Stdin 19", content=shared_body, content_type="note",
            source_path=None, metadata={},
        ),
        source_kind="manual",
        force=True,
    )
    # A NEW stdin row is inserted — the scoped fallback query did NOT match the file or vault rows.
    assert r_stdin.created is True
    assert r_stdin.document_id != file_doc_id
    assert r_stdin.document_id != vault_doc_id

    # The file row at the original path still exists.
    file_row = test_db.execute(
        "SELECT id FROM documents WHERE source_path=%s", (file_path,)
    ).fetchone()
    assert file_row is not None
    assert str(file_row[0]) == file_doc_id

    # The vault-tier row still exists.
    vault_row = test_db.execute(
        "SELECT id FROM documents WHERE id=%s", (vault_doc_id,)
    ).fetchone()
    assert vault_row is not None
