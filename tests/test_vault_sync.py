"""Integration tests for brain.vault.sync.sync_vault.

Real test DB + fake embedder. Each test seeds a vault folder under tmp_path,
runs sync_vault, and asserts on either the SyncReport or the resulting DB
state.
"""
import uuid
from pathlib import Path

import psycopg
import pytest

from brain.vault.export import export_vault
from brain.vault.frontmatter import dump_frontmatter, parse_frontmatter
from brain.vault.sync import SyncReport, sync_one_file, sync_vault

# ---------------------------------------------------------------------------
# Helpers.
# ---------------------------------------------------------------------------


def _write(path: Path, frontmatter: dict, body: str) -> None:
    """Write a vault file with the given frontmatter + body."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(dump_frontmatter(frontmatter, body))


def _sync(
    conn: psycopg.Connection, fake_embedder, vault: Path, **kwargs
) -> SyncReport:
    """Tiny wrapper to keep call sites short."""
    return sync_vault(conn, embedder=fake_embedder, vault_path=vault, **kwargs)


def _doc_count(conn: psycopg.Connection) -> int:
    row = conn.execute("SELECT count(*) FROM documents").fetchone()
    assert row is not None
    return int(row[0])


def _link_count(conn: psycopg.Connection) -> int:
    row = conn.execute("SELECT count(*) FROM links").fetchone()
    assert row is not None
    return int(row[0])


def _unresolved_count(conn: psycopg.Connection) -> int:
    row = conn.execute("SELECT count(*) FROM unresolved_links").fetchone()
    assert row is not None
    return int(row[0])


def _derived_rows(conn: psycopg.Connection) -> list[tuple]:
    """All ``derived_links`` rows ordered for stable assertions.

    Mirrors the helper in ``tests/derived_links/test_pass_runner.py`` so the
    sync-level tests below can assert on the same shape produced by the
    linker pass runner.
    """
    return conn.execute(
        "SELECT src_document_id::text, dst_document_id::text, rule, weight "
        "FROM derived_links ORDER BY src_document_id, dst_document_id, rule"
    ).fetchall()


def _derived_count(conn: psycopg.Connection) -> int:
    row = conn.execute("SELECT count(*) FROM derived_links").fetchone()
    assert row is not None
    return int(row[0])


def _ingest_gmail(
    conn: psycopg.Connection,
    fake_embedder,
    *,
    external_id: str,
    title: str,
    body: str,
    from_header: str,
    to_header: str,
    thread_id: str,
    date: str,
) -> str:
    """Seed one Gmail doc by ingesting it through the real pipeline.

    Returns the new ``documents.id``. Used by the derived-links sync tests so
    each Gmail row has a real ``sources`` join and the metadata shape the
    linker pass reads (``from`` / ``to`` / ``thread_id`` / ``date``).
    """
    from brain.ingest import ExtractedDoc, ingest_document

    result = ingest_document(
        conn,
        embedder=fake_embedder,
        doc=ExtractedDoc(
            title=title,
            content=body,
            content_type="email",
            source_path=None,
            metadata={
                "from": from_header,
                "to": to_header,
                "thread_id": thread_id,
                "date": date,
            },
        ),
        source_kind="gmail",
        source_external_id=external_id,
    )
    assert result.document_id is not None
    return result.document_id


# ---------------------------------------------------------------------------
# Empty / single-file cases.
# ---------------------------------------------------------------------------


def test_empty_vault_yields_empty_report(
    test_db: psycopg.Connection, fake_embedder, tmp_path: Path
) -> None:
    vault = tmp_path / "vault"
    vault.mkdir()
    report = _sync(test_db, fake_embedder, vault)
    assert report == SyncReport()


def test_missing_vault_path_records_error(
    test_db: psycopg.Connection, fake_embedder, tmp_path: Path
) -> None:
    vault = tmp_path / "does-not-exist"
    report = _sync(test_db, fake_embedder, vault)
    assert report.errors
    assert "does not exist" in report.errors[0][1]


def test_one_new_vault_note_creates_row(
    test_db: psycopg.Connection, fake_embedder, tmp_path: Path
) -> None:
    vault = tmp_path / "vault"
    note_id = str(uuid.uuid4())
    _write(
        vault / "person-a.md",
        {"id": note_id, "title": "person-x conversation"},
        "Body of the note.\n",
    )
    report = _sync(test_db, fake_embedder, vault)
    assert report.created == 1
    assert report.updated == 0
    assert report.skipped == 0
    assert report.links_resolved == 0
    assert report.links_unresolved == 0
    row = test_db.execute(
        "SELECT title, kind, vault_path FROM documents WHERE id = %s",
        (note_id,),
    ).fetchone()
    assert row == ("person-x conversation", "vault", "person-a.md")


def test_id_assignment_writes_back_to_disk(
    test_db: psycopg.Connection, fake_embedder, tmp_path: Path
) -> None:
    """A note without an id gets one written into its frontmatter on first sync."""
    vault = tmp_path / "vault"
    file_path = vault / "fresh.md"
    _write(file_path, {"title": "Fresh note"}, "Hello.\n")
    report = _sync(test_db, fake_embedder, vault)
    assert report.created == 1
    assert report.id_assigned == 1
    fields, _ = parse_frontmatter(file_path.read_text())
    assert "id" in fields
    # Stored id matches the DB row.
    row = test_db.execute(
        "SELECT id::text FROM documents WHERE title = 'Fresh note'"
    ).fetchone()
    assert row is not None
    assert str(row[0]) == fields["id"]


# ---------------------------------------------------------------------------
# Idempotency / re-sync paths.
# ---------------------------------------------------------------------------


def test_resync_unchanged_file_is_skipped(
    test_db: psycopg.Connection, fake_embedder, tmp_path: Path
) -> None:
    vault = tmp_path / "vault"
    note_id = str(uuid.uuid4())
    _write(
        vault / "n.md",
        {"id": note_id, "title": "Stable"},
        "stable body\n",
    )
    _sync(test_db, fake_embedder, vault)
    report = _sync(test_db, fake_embedder, vault)
    assert report.skipped == 1
    assert report.updated == 0


def test_frontmatter_only_edit_does_not_re_embed(
    test_db: psycopg.Connection, counting_embedder, tmp_path: Path
) -> None:
    """Adding an alias (frontmatter-only) updates metadata but not chunks.

    The counting embedder lets us assert that no embed call happens — the
    body hash is unchanged, so the rechunk + re-embed branch must skip.
    """
    vault = tmp_path / "vault"
    note_id = str(uuid.uuid4())
    file_path = vault / "n.md"
    _write(file_path, {"id": note_id, "title": "X"}, "the body\n")
    sync_vault(test_db, embedder=counting_embedder, vault_path=vault)
    embed_calls_after_first = counting_embedder.embed_calls
    # Edit frontmatter only — same body.
    _write(
        file_path,
        {"id": note_id, "title": "X", "aliases": ["alt"]},
        "the body\n",
    )
    report = sync_vault(test_db, embedder=counting_embedder, vault_path=vault)
    assert report.updated == 1
    # No additional embed calls beyond the first sync.
    assert counting_embedder.embed_calls == embed_calls_after_first


def test_body_change_re_embeds_chunks(
    test_db: psycopg.Connection, fake_embedder, tmp_path: Path
) -> None:
    vault = tmp_path / "vault"
    note_id = str(uuid.uuid4())
    file_path = vault / "n.md"
    _write(file_path, {"id": note_id, "title": "X"}, "first body\n")
    _sync(test_db, fake_embedder, vault)
    pre_chunks = test_db.execute(
        "SELECT content FROM chunks WHERE document_id = %s ORDER BY chunk_index",
        (note_id,),
    ).fetchall()

    # Replace the body.
    _write(file_path, {"id": note_id, "title": "X"}, "completely new body text\n")
    report = _sync(test_db, fake_embedder, vault)
    assert report.updated == 1
    post_chunks = test_db.execute(
        "SELECT content FROM chunks WHERE document_id = %s ORDER BY chunk_index",
        (note_id,),
    ).fetchall()
    assert pre_chunks != post_chunks
    assert any("completely new body text" in (c[0] or "") for c in post_chunks)


# ---------------------------------------------------------------------------
# Wiki-link materialization.
# ---------------------------------------------------------------------------


def test_link_to_existing_doc_resolves(
    test_db: psycopg.Connection, fake_embedder, tmp_path: Path
) -> None:
    vault = tmp_path / "vault"
    a_id = str(uuid.uuid4())
    b_id = str(uuid.uuid4())
    _write(vault / "a.md", {"id": a_id, "title": "A"}, "body of a\n")
    _write(vault / "b.md", {"id": b_id, "title": "B"}, "see [[A]]\n")
    report = _sync(test_db, fake_embedder, vault)
    assert report.links_resolved == 1
    assert report.links_unresolved == 0
    rows = test_db.execute(
        "SELECT src_document_id::text, dst_document_id::text, link_text "
        "FROM links"
    ).fetchall()
    assert (b_id, a_id, "[[A]]") in rows


def test_dangling_link_lands_in_unresolved(
    test_db: psycopg.Connection, fake_embedder, tmp_path: Path
) -> None:
    vault = tmp_path / "vault"
    note_id = str(uuid.uuid4())
    _write(
        vault / "note.md",
        {"id": note_id, "title": "N"},
        "see [[Missing Target]]\n",
    )
    report = _sync(test_db, fake_embedder, vault)
    assert report.links_resolved == 0
    assert report.links_unresolved == 1
    rows = test_db.execute(
        "SELECT link_text FROM unresolved_links WHERE src_document_id = %s",
        (note_id,),
    ).fetchall()
    assert rows == [("[[Missing Target]]",)]


def test_unresolved_promotes_to_resolved_on_target_creation(
    test_db: psycopg.Connection, fake_embedder, tmp_path: Path
) -> None:
    """`[[Foo]]` in a note synced before Foo.md exists → unresolved.
    Then create Foo.md and re-sync → unresolved row is removed, links has it.
    """
    vault = tmp_path / "vault"
    src_id = str(uuid.uuid4())
    _write(
        vault / "src.md",
        {"id": src_id, "title": "Source"},
        "links to [[Foo]]\n",
    )
    first = _sync(test_db, fake_embedder, vault)
    assert first.links_unresolved == 1

    foo_id = str(uuid.uuid4())
    _write(vault / "foo.md", {"id": foo_id, "title": "Foo"}, "I am foo.\n")
    second = _sync(test_db, fake_embedder, vault)
    # In the second run we created foo.md (1) and the previously-unresolved
    # link from src now resolves; the retry pass moves it to ``links``.
    assert second.created == 1  # foo
    assert _unresolved_count(test_db) == 0
    assert _link_count(test_db) == 1


def test_link_dedup_with_repeated_link_text(
    test_db: psycopg.Connection, fake_embedder, tmp_path: Path
) -> None:
    """The unique constraint dedups identical [[X]] in the body."""
    vault = tmp_path / "vault"
    a_id = str(uuid.uuid4())
    b_id = str(uuid.uuid4())
    _write(vault / "a.md", {"id": a_id, "title": "A"}, "x\n")
    _write(
        vault / "b.md",
        {"id": b_id, "title": "B"},
        "see [[A]] then again [[A]] one more [[A]]\n",
    )
    report = _sync(test_db, fake_embedder, vault)
    # Three positional matches, one DB row (deduped on (src,dst,text,kind)).
    assert report.links_resolved == 3
    rows = test_db.execute(
        "SELECT count(*) FROM links WHERE src_document_id = %s",
        (b_id,),
    ).fetchone()
    assert rows is not None
    assert rows[0] == 1


def test_brain_id_prefix_link(
    test_db: psycopg.Connection, fake_embedder, tmp_path: Path
) -> None:
    vault = tmp_path / "vault"
    a_id = str(uuid.uuid4())
    b_id = str(uuid.uuid4())
    _write(vault / "a.md", {"id": a_id, "title": "A"}, "body a\n")
    _write(
        vault / "b.md",
        {"id": b_id, "title": "B"},
        f"link [[brain:{a_id[:8]}]]\n",
    )
    report = _sync(test_db, fake_embedder, vault)
    assert report.links_resolved == 1


def test_links_re_dropped_on_re_sync_of_modified_body(
    test_db: psycopg.Connection, fake_embedder, tmp_path: Path
) -> None:
    """Removing a [[link]] from a body removes it from the links table."""
    vault = tmp_path / "vault"
    a_id = str(uuid.uuid4())
    b_id = str(uuid.uuid4())
    _write(vault / "a.md", {"id": a_id, "title": "A"}, "x\n")
    file_b = vault / "b.md"
    _write(file_b, {"id": b_id, "title": "B"}, "see [[A]]\n")
    _sync(test_db, fake_embedder, vault)
    assert _link_count(test_db) == 1
    # Drop the link in a re-sync.
    _write(file_b, {"id": b_id, "title": "B"}, "no more links\n")
    _sync(test_db, fake_embedder, vault)
    assert _link_count(test_db) == 0


# ---------------------------------------------------------------------------
# Missing-on-disk: prune vs warn.
# ---------------------------------------------------------------------------


def test_missing_file_warns_by_default(
    test_db: psycopg.Connection, fake_embedder, tmp_path: Path
) -> None:
    vault = tmp_path / "vault"
    note_id = str(uuid.uuid4())
    file_path = vault / "n.md"
    _write(file_path, {"id": note_id, "title": "X"}, "x\n")
    _sync(test_db, fake_embedder, vault)
    file_path.unlink()
    report = _sync(test_db, fake_embedder, vault)
    assert report.warned == 1
    assert report.deleted == 0
    # DB row still present.
    assert _doc_count(test_db) == 1


def test_missing_file_pruned_with_flag(
    test_db: psycopg.Connection, fake_embedder, tmp_path: Path
) -> None:
    vault = tmp_path / "vault"
    note_id = str(uuid.uuid4())
    file_path = vault / "n.md"
    _write(file_path, {"id": note_id, "title": "X"}, "x\n")
    _sync(test_db, fake_embedder, vault)
    file_path.unlink()
    report = _sync(test_db, fake_embedder, vault, prune=True)
    assert report.deleted == 1
    assert _doc_count(test_db) == 0


def test_prune_does_not_touch_ingested_tier(
    test_db: psycopg.Connection, fake_embedder, tmp_path: Path
) -> None:
    """Ingested-tier docs without a matching vault file are not pruned —
    their source of truth is the DB, not the vault."""
    vault = tmp_path / "vault"
    vault.mkdir()
    test_db.execute(
        "INSERT INTO documents "
        "(title, content, content_hash, content_type, kind) "
        "VALUES ('Krisp call', 'body', 'h1', 'note', 'ingested')"
    )
    _sync(test_db, fake_embedder, vault, prune=True)
    assert _doc_count(test_db) == 1


# ---------------------------------------------------------------------------
# Dry-run.
# ---------------------------------------------------------------------------


def test_dry_run_does_not_write_to_db(
    test_db: psycopg.Connection, fake_embedder, tmp_path: Path
) -> None:
    vault = tmp_path / "vault"
    _write(vault / "n.md", {"title": "X"}, "body\n")
    pre = _doc_count(test_db)
    report = _sync(test_db, fake_embedder, vault, dry_run=True)
    assert pre == _doc_count(test_db)  # unchanged
    assert report.created == 1
    assert report.id_assigned == 1


def test_dry_run_does_not_modify_files(
    test_db: psycopg.Connection, fake_embedder, tmp_path: Path
) -> None:
    """No id back-write to disk on a dry-run."""
    vault = tmp_path / "vault"
    file_path = vault / "n.md"
    _write(file_path, {"title": "X"}, "body\n")
    pre_text = file_path.read_text()
    _sync(test_db, fake_embedder, vault, dry_run=True)
    assert file_path.read_text() == pre_text


def test_dry_run_with_prune_does_not_delete(
    test_db: psycopg.Connection, fake_embedder, tmp_path: Path
) -> None:
    vault = tmp_path / "vault"
    note_id = str(uuid.uuid4())
    file_path = vault / "n.md"
    _write(file_path, {"id": note_id, "title": "X"}, "x\n")
    _sync(test_db, fake_embedder, vault)
    file_path.unlink()
    report = _sync(test_db, fake_embedder, vault, prune=True, dry_run=True)
    assert report.deleted == 1  # planned
    assert _doc_count(test_db) == 1  # but row still present


# ---------------------------------------------------------------------------
# Round-trip parity with brain vault export.
# ---------------------------------------------------------------------------


def test_round_trip_export_then_sync_is_no_op(
    test_db: psycopg.Connection, counting_embedder, tmp_path: Path
) -> None:
    """A vault built by `brain vault export` round-trips through sync as zero re-embeds."""
    # Seed the DB by ingesting one doc.
    from brain.ingest import ExtractedDoc, ingest_document

    ingest_document(
        test_db,
        embedder=counting_embedder,
        doc=ExtractedDoc(
            title="Round Trip",
            content="Body content here.\n",
            content_type="note",
            source_path=None,
            metadata={},
        ),
        source_kind="manual",
    )
    embed_calls_after_ingest = counting_embedder.embed_calls
    vault = tmp_path / "vault"
    export_vault(test_db, vault_path=vault)
    # Pre-sync DB state.
    pre_hash = test_db.execute(
        "SELECT content_hash FROM documents WHERE title = 'Round Trip'"
    ).fetchone()
    assert pre_hash is not None

    report = sync_vault(test_db, embedder=counting_embedder, vault_path=vault)
    # Body byte-identical → no re-embed (the legacy hash check accepts it).
    # Critical contract: zero embed calls beyond the original ingest.
    assert counting_embedder.embed_calls == embed_calls_after_ingest
    # First run: the row's vault_path was NULL after ingest; sync writes it,
    # which counts as an "updated" — but with zero re-embeds.
    assert report.created == 0
    assert report.updated + report.skipped == 1
    # Second run: now that vault_path + content_hash match disk, it's a pure skip.
    second = sync_vault(test_db, embedder=counting_embedder, vault_path=vault)
    assert second.created == 0
    assert second.updated == 0
    assert second.skipped == 1
    assert counting_embedder.embed_calls == embed_calls_after_ingest


def test_round_trip_dry_run_after_first_sync_reports_zero_changes(
    test_db: psycopg.Connection, fake_embedder, tmp_path: Path
) -> None:
    """After the first sync settles vault_path, a dry-run reports zero changes.

    The very-first sync of an exported vault has to write ``vault_path`` onto
    rows that lacked it (ingest didn't set it) — that counts as an update.
    Once the row is settled, subsequent dry-runs see a pure no-op.
    """
    from brain.ingest import ExtractedDoc, ingest_document

    ingest_document(
        test_db,
        embedder=fake_embedder,
        doc=ExtractedDoc(
            title="Round",
            content="x\n",
            content_type="note",
            source_path=None,
            metadata={},
        ),
        source_kind="manual",
    )
    vault = tmp_path / "vault"
    export_vault(test_db, vault_path=vault)
    # Real sync first to settle vault_path + migrate hash.
    _sync(test_db, fake_embedder, vault)
    # Then dry-run — must be pure skip.
    report = _sync(test_db, fake_embedder, vault, dry_run=True)
    assert report.created == 0
    assert report.updated == 0
    assert report.skipped == 1


# ---------------------------------------------------------------------------
# Skip rules + error handling.
# ---------------------------------------------------------------------------


def test_files_in_templates_are_skipped(
    test_db: psycopg.Connection, fake_embedder, tmp_path: Path
) -> None:
    vault = tmp_path / "vault"
    _write(vault / "_templates" / "daily.md", {"title": "T"}, "{{date}}\n")
    report = _sync(test_db, fake_embedder, vault)
    assert report.created == 0
    assert _doc_count(test_db) == 0


def test_files_in_attachments_are_skipped(
    test_db: psycopg.Connection, fake_embedder, tmp_path: Path
) -> None:
    """``_attachments/`` is for binaries; any stray .md is ignored too."""
    vault = tmp_path / "vault"
    _write(vault / "_attachments" / "ignored.md", {"title": "I"}, "x\n")
    report = _sync(test_db, fake_embedder, vault)
    assert report.created == 0


def test_malformed_frontmatter_is_recorded_as_error(
    test_db: psycopg.Connection, fake_embedder, tmp_path: Path
) -> None:
    vault = tmp_path / "vault"
    bad = vault / "broken.md"
    bad.parent.mkdir(parents=True, exist_ok=True)
    bad.write_text("---\nfoo: [unclosed\n---\nbody\n")
    report = _sync(test_db, fake_embedder, vault)
    assert report.errors
    err_path, err_reason = report.errors[0]
    assert err_path == bad
    assert "frontmatter" in err_reason.lower()


def test_missing_title_is_an_error(
    test_db: psycopg.Connection, fake_embedder, tmp_path: Path
) -> None:
    vault = tmp_path / "vault"
    note_id = str(uuid.uuid4())
    _write(vault / "n.md", {"id": note_id}, "body\n")
    report = _sync(test_db, fake_embedder, vault)
    assert report.errors
    assert "title" in report.errors[0][1]


def test_aliases_propagate_into_metadata(
    test_db: psycopg.Connection, fake_embedder, tmp_path: Path
) -> None:
    """A note with `aliases:` in frontmatter syncs into ``documents.metadata``."""
    vault = tmp_path / "vault"
    note_id = str(uuid.uuid4())
    _write(
        vault / "n.md",
        {"id": note_id, "title": "X", "aliases": ["foo", "bar"]},
        "body\n",
    )
    _sync(test_db, fake_embedder, vault)
    row = test_db.execute(
        "SELECT metadata FROM documents WHERE id = %s", (note_id,)
    ).fetchone()
    assert row is not None
    meta = row[0]
    assert meta.get("aliases") == ["foo", "bar"]


def test_alias_resolution_works_after_sync(
    test_db: psycopg.Connection, fake_embedder, tmp_path: Path
) -> None:
    """A `[[alias]]` link resolves once both notes are synced."""
    vault = tmp_path / "vault"
    a_id = str(uuid.uuid4())
    b_id = str(uuid.uuid4())
    _write(
        vault / "a.md",
        {"id": a_id, "title": "Long Canonical Title", "aliases": ["short"]},
        "x\n",
    )
    _write(vault / "b.md", {"id": b_id, "title": "B"}, "see [[short]]\n")
    report = _sync(test_db, fake_embedder, vault)
    assert report.links_resolved == 1
    assert report.links_unresolved == 0


def test_existing_db_row_with_no_disk_file_does_not_count_as_skipped(
    test_db: psycopg.Connection, fake_embedder, tmp_path: Path
) -> None:
    """A row only known to the DB (no walked file) lands in warned, not skipped."""
    vault = tmp_path / "vault"
    vault.mkdir()
    test_db.execute(
        "INSERT INTO documents "
        "(title, content, content_hash, content_type, kind, vault_path) "
        "VALUES ('Orphan', 'body', 'h-orphan', 'note', 'vault', 'orphan.md')"
    )
    report = _sync(test_db, fake_embedder, vault)
    assert report.skipped == 0
    assert report.warned == 1


def test_unresolved_link_uniqueness(
    test_db: psycopg.Connection, fake_embedder, tmp_path: Path
) -> None:
    """Re-sync of a body with a duplicate dangling [[X]] doesn't multiply rows."""
    vault = tmp_path / "vault"
    note_id = str(uuid.uuid4())
    _write(
        vault / "n.md",
        {"id": note_id, "title": "N"},
        "[[Missing]] then [[Missing]] again\n",
    )
    _sync(test_db, fake_embedder, vault)
    # One unique unresolved row even though parser saw two refs.
    rows = test_db.execute(
        "SELECT count(*) FROM unresolved_links"
    ).fetchone()
    assert rows is not None
    assert rows[0] == 1


def test_retry_pass_resolves_cross_file_ordering(
    test_db: psycopg.Connection, fake_embedder, tmp_path: Path
) -> None:
    """Source file processed BEFORE target file in the same sync run.

    Files iterate in sorted order. A source file that names something later
    in the sort can't see the target during its own materialize_links pass,
    so it lands in unresolved_links. The end-of-run retry pass picks it up
    and promotes it.
    """
    vault = tmp_path / "vault"
    a_id = str(uuid.uuid4())
    z_id = str(uuid.uuid4())
    _write(vault / "a-src.md", {"id": a_id, "title": "Src"}, "see [[Target]]\n")
    _write(vault / "z-target.md", {"id": z_id, "title": "Target"}, "x\n")
    report = _sync(test_db, fake_embedder, vault)
    # Retry pass moved the row from unresolved to resolved.
    assert _unresolved_count(test_db) == 0
    assert _link_count(test_db) == 1
    # The report counters end consistent with the final state — the retry
    # pass adjusts them down/up rather than just leaving a stale total.
    assert report.links_unresolved == 0
    assert report.links_resolved == 1


def test_dry_run_existing_doc_unchanged_skips(
    test_db: psycopg.Connection, fake_embedder, tmp_path: Path
) -> None:
    """Dry-run on an existing doc whose body is unchanged reports skipped."""
    vault = tmp_path / "vault"
    note_id = str(uuid.uuid4())
    _write(vault / "n.md", {"id": note_id, "title": "Stable"}, "x\n")
    _sync(test_db, fake_embedder, vault)
    report = _sync(test_db, fake_embedder, vault, dry_run=True)
    assert report.skipped == 1
    assert report.updated == 0


def test_dry_run_existing_doc_with_body_change_reports_updated(
    test_db: psycopg.Connection, fake_embedder, tmp_path: Path
) -> None:
    vault = tmp_path / "vault"
    note_id = str(uuid.uuid4())
    file_path = vault / "n.md"
    _write(file_path, {"id": note_id, "title": "X"}, "first\n")
    _sync(test_db, fake_embedder, vault)
    _write(file_path, {"id": note_id, "title": "X"}, "second\n")
    report = _sync(test_db, fake_embedder, vault, dry_run=True)
    assert report.updated == 1
    # DB still holds the original body.
    row = test_db.execute(
        "SELECT content FROM documents WHERE id = %s", (note_id,)
    ).fetchone()
    assert row is not None
    assert row[0] == "first"


def test_dry_run_with_existing_target_resolves_link(
    test_db: psycopg.Connection, fake_embedder, tmp_path: Path
) -> None:
    """Dry-run on an existing target counts the link as resolved.

    Note: if BOTH source and target are new in the same dry-run, neither is
    inserted, so the link counts as unresolved (dry-run is forward-looking
    only on a per-file basis). With the target already in the DB, dry-run
    correctly reports the link as resolved without writing a links row.
    """
    vault = tmp_path / "vault"
    a = str(uuid.uuid4())
    b = str(uuid.uuid4())
    # Pre-seed A so dry-run can resolve [[A]] against it.
    _write(vault / "a.md", {"id": a, "title": "A"}, "x\n")
    _sync(test_db, fake_embedder, vault)
    _write(
        vault / "b.md",
        {"id": b, "title": "B"},
        "see [[A]] and [[Missing]]\n",
    )
    report = _sync(test_db, fake_embedder, vault, dry_run=True)
    assert report.links_resolved == 1
    assert report.links_unresolved == 1
    # No links rows added by dry-run.
    assert _link_count(test_db) == 0


def test_silent_hash_migration_after_export(
    test_db: psycopg.Connection, counting_embedder, tmp_path: Path
) -> None:
    """A doc whose stored content_hash is in the legacy form gets migrated to body_hash."""
    from brain.ingest import ExtractedDoc, ingest_document

    ingest_document(
        test_db,
        embedder=counting_embedder,
        doc=ExtractedDoc(
            title="Hashed",
            content="Body bytes\n",
            content_type="note",
            source_path=None,
            metadata={},
        ),
        source_kind="manual",
    )
    pre_hash_row = test_db.execute(
        "SELECT content_hash FROM documents WHERE title = 'Hashed'"
    ).fetchone()
    assert pre_hash_row is not None
    pre_hash = pre_hash_row[0]
    vault = tmp_path / "vault"
    export_vault(test_db, vault_path=vault)

    sync_vault(test_db, embedder=counting_embedder, vault_path=vault)
    post_hash_row = test_db.execute(
        "SELECT content_hash FROM documents WHERE title = 'Hashed'"
    ).fetchone()
    assert post_hash_row is not None
    post_hash = post_hash_row[0]
    # The hash got replaced with the new normalized form.
    assert pre_hash != post_hash


def test_silent_hash_migration_in_skipped_branch(
    test_db: psycopg.Connection, counting_embedder, fake_embedder, tmp_path: Path
) -> None:
    """The skipped + hash-migration branch fires when ONLY content_hash differs.

    Construct a row whose vault_path / title / tags / kind / content_type all
    match disk, but whose ``content_hash`` is in the legacy raw form. Sync
    should silently migrate the hash without re-embedding and report the
    file as skipped.
    """
    import hashlib

    vault = tmp_path / "vault"
    note_id = str(uuid.uuid4())
    body = "the body\n"
    legacy_hash = hashlib.sha256(body.encode()).hexdigest()
    test_db.execute(
        "INSERT INTO documents "
        "(id, title, content, content_hash, content_type, kind, vault_path, tags) "
        "VALUES (%s, 'X', %s, %s, 'note', 'vault', 'n.md', ARRAY[]::text[])",
        (note_id, body.strip(), legacy_hash),
    )
    _write(vault / "n.md", {"id": note_id, "title": "X"}, body)

    pre_embed_calls = counting_embedder.embed_calls
    report = sync_vault(test_db, embedder=counting_embedder, vault_path=vault)
    assert report.skipped == 1
    assert report.updated == 0
    # Zero re-embeds — body bytes byte-equivalent under legacy hash.
    assert counting_embedder.embed_calls == pre_embed_calls
    # Hash migrated.
    row = test_db.execute(
        "SELECT content_hash FROM documents WHERE id = %s", (note_id,)
    ).fetchone()
    assert row is not None
    assert row[0] != legacy_hash


def test_title_collision_logs_disambiguation_hint(
    test_db: psycopg.Connection,
    fake_embedder,
    tmp_path: Path,
    caplog,
) -> None:
    """Two docs with identical titles → unresolved + a warning naming both prefixes."""
    import logging

    vault = tmp_path / "vault"
    a_id = str(uuid.uuid4())
    b_id = str(uuid.uuid4())
    c_id = str(uuid.uuid4())
    _write(vault / "a.md", {"id": a_id, "title": "person-x"}, "x\n")
    _write(vault / "b.md", {"id": b_id, "title": "person-a"}, "y\n")
    _write(
        vault / "c.md",
        {"id": c_id, "title": "Source"},
        "see [[person-x]]\n",
    )
    with caplog.at_level(logging.WARNING, logger="brain.vault.sync"):
        _sync(test_db, fake_embedder, vault)
    # Link is unresolved because person-x is ambiguous.
    assert _unresolved_count(test_db) == 1
    # Warning surfaces both candidate prefixes + the [[brain:<prefix>]] hint.
    msgs = [r.getMessage() for r in caplog.records]
    assert any("matches 2 documents" in m for m in msgs)
    assert any("[[brain:" in m for m in msgs)


def test_empty_body_creates_doc_with_no_chunks(
    test_db: psycopg.Connection, fake_embedder, tmp_path: Path
) -> None:
    """An empty body still produces a documents row, but no chunks (matches ingest semantics)."""
    vault = tmp_path / "vault"
    note_id = str(uuid.uuid4())
    _write(vault / "empty.md", {"id": note_id, "title": "E"}, "")
    report = _sync(test_db, fake_embedder, vault)
    assert report.created == 1
    rows = test_db.execute(
        "SELECT count(*) FROM chunks WHERE document_id = %s", (note_id,)
    ).fetchone()
    assert rows is not None
    assert rows[0] == 0


def test_scalar_tags_string_is_promoted_to_list(
    test_db: psycopg.Connection, fake_embedder, tmp_path: Path
) -> None:
    """`tags: career` (scalar) round-trips into a single-element list."""
    vault = tmp_path / "vault"
    note_id = str(uuid.uuid4())
    _write(
        vault / "n.md",
        {"id": note_id, "title": "X", "tags": "career"},
        "x\n",
    )
    _sync(test_db, fake_embedder, vault)
    row = test_db.execute(
        "SELECT tags FROM documents WHERE id = %s", (note_id,)
    ).fetchone()
    assert row is not None
    assert list(row[0]) == ["career"]


def test_tags_with_invalid_type_falls_back_to_empty(
    test_db: psycopg.Connection, fake_embedder, tmp_path: Path
) -> None:
    """`tags: 123` (number — not a list, not a string) results in []."""
    vault = tmp_path / "vault"
    note_id = str(uuid.uuid4())
    _write(
        vault / "n.md",
        {"id": note_id, "title": "X", "tags": 123},
        "x\n",
    )
    _sync(test_db, fake_embedder, vault)
    row = test_db.execute(
        "SELECT tags FROM documents WHERE id = %s", (note_id,)
    ).fetchone()
    assert row is not None
    assert list(row[0] or []) == []


def test_extra_frontmatter_keys_land_in_metadata(
    test_db: psycopg.Connection, fake_embedder, tmp_path: Path
) -> None:
    """Non-reserved frontmatter keys (e.g. ``priority``) flow into metadata JSONB."""
    vault = tmp_path / "vault"
    note_id = str(uuid.uuid4())
    _write(
        vault / "n.md",
        {"id": note_id, "title": "X", "priority": "high", "stage": 2},
        "x\n",
    )
    _sync(test_db, fake_embedder, vault)
    row = test_db.execute(
        "SELECT metadata FROM documents WHERE id = %s", (note_id,)
    ).fetchone()
    assert row is not None
    meta = row[0]
    assert meta["priority"] == "high"
    assert meta["stage"] == 2


def test_retry_pass_skips_malformed_unresolved_link_text(
    test_db: psycopg.Connection, fake_embedder, tmp_path: Path
) -> None:
    """Defensive: a corrupt unresolved row with non-link text doesn't crash retry.

    The retry pass re-parses ``link_text``; an empty / unparseable value
    yields no ParsedLink and is silently left in place.
    """
    vault = tmp_path / "vault"
    src_id = str(uuid.uuid4())
    _write(vault / "src.md", {"id": src_id, "title": "Src"}, "x\n")
    # First sync to seed src.
    _sync(test_db, fake_embedder, vault)
    # Manually inject a corrupt unresolved row.
    test_db.execute(
        "INSERT INTO unresolved_links "
        "(src_document_id, link_text, link_kind) "
        "VALUES (%s, '', 'wiki')",
        (src_id,),
    )
    # Re-sync — retry pass over this src finds the corrupt row, skips it.
    report = _sync(test_db, fake_embedder, vault)
    # Row remains because materialize_links also drops it on re-process.
    # Just assert the run didn't crash.
    assert report.errors == []


def test_db_failure_mid_sync_leaves_file_unchanged(
    test_db: psycopg.Connection, fake_embedder, tmp_path: Path, mocker
) -> None:
    """If the DB upsert fails, the file's frontmatter is NOT id-stamped.

    The disk write is deferred to AFTER the DB transaction commits. A DB
    error during the upsert propagates out of sync (a corrupt or down DB
    is a hard failure, not a per-file warning); the file is left in its
    original state regardless.

    Pre-fix: the disk write happened first, so a DB crash would leave the
    file id-stamped on disk with no DB row to back it.
    """
    vault = tmp_path / "vault"
    file_path = vault / "fresh.md"
    _write(file_path, {"title": "No id yet"}, "body\n")
    pre_text = file_path.read_text()
    assert "id:" not in pre_text  # sanity: no id in frontmatter pre-sync

    # Patch the module-level helper to simulate a DB crash mid-transaction.
    # Patching at this seam (rather than on the connection object, whose
    # ``execute`` method is C-implemented and not setattr-able) is the
    # standard pytest-mock idiom for forcing a specific failure path.
    mocker.patch(
        "brain.vault.sync._insert_document",
        side_effect=psycopg.OperationalError("simulated DB crash"),
    )
    with pytest.raises(psycopg.OperationalError):
        sync_vault(test_db, embedder=fake_embedder, vault_path=vault)

    # File on disk is unchanged — no id stamped, no body rewrite.
    post_text = file_path.read_text()
    assert post_text == pre_text
    # No DB row was created (transaction rolled back).
    rows = test_db.execute(
        "SELECT count(*) FROM documents WHERE vault_path = 'fresh.md'"
    ).fetchone()
    assert rows is not None
    assert rows[0] == 0


def test_recovery_branch_finds_existing_row_by_vault_path(
    test_db: psycopg.Connection, fake_embedder, tmp_path: Path
) -> None:
    """A pre-existing vault row at the same path is reused when the file lacks an id.

    Simulates the recovery scenario: a previous sync committed the DB row
    but a disk-write OSError prevented stamping the id back. The next sync
    must NOT generate a fresh UUID and create a duplicate — it must reuse
    the existing row's id and rewrite the frontmatter.
    """
    vault = tmp_path / "vault"
    pre_existing_id = str(uuid.uuid4())
    test_db.execute(
        "INSERT INTO documents "
        "(id, title, content, content_hash, content_type, kind, vault_path) "
        "VALUES (%s, 'Recovered', 'body', 'h-recovered', 'note', 'vault', 'recovered.md')",
        (pre_existing_id,),
    )
    # Author the file without an id — the recovery branch should reuse pre_existing_id.
    file_path = vault / "recovered.md"
    _write(file_path, {"title": "Recovered"}, "body\n")
    report = _sync(test_db, fake_embedder, vault)
    assert report.errors == []
    # Only one row for this path; id matches the pre-existing one.
    rows = test_db.execute(
        "SELECT id::text FROM documents WHERE vault_path = 'recovered.md'"
    ).fetchall()
    assert len(rows) == 1
    assert str(rows[0][0]) == pre_existing_id
    # Frontmatter on disk was rewritten with the existing id.
    fields, _ = parse_frontmatter(file_path.read_text())
    assert fields["id"] == pre_existing_id


def test_disk_write_after_db_commit(
    test_db: psycopg.Connection, fake_embedder, tmp_path: Path
) -> None:
    """Happy path: file with no id → DB row created → frontmatter stamped.

    Pins the order: the disk write is performed AFTER ``conn.transaction()``
    commits. The test asserts both that the row exists AND that the file's
    new frontmatter contains the same id.
    """
    vault = tmp_path / "vault"
    file_path = vault / "ordered.md"
    _write(file_path, {"title": "Ordered"}, "body\n")
    _sync(test_db, fake_embedder, vault)
    fields, _ = parse_frontmatter(file_path.read_text())
    file_id = fields["id"]
    db_row = test_db.execute(
        "SELECT id::text FROM documents WHERE vault_path = 'ordered.md'"
    ).fetchone()
    assert db_row is not None
    assert str(db_row[0]) == file_id


def test_ingested_metadata_preserved_across_sync(
    test_db: psycopg.Connection, fake_embedder, tmp_path: Path
) -> None:
    """Re-syncing an _ingested/ file must NOT overwrite DB-owned metadata.

    Source-specific fields like ``date`` and ``duration_min`` are populated
    during ingest and never round-tripped through the export's frontmatter.
    Sync's tier-aware merge preserves them on every pass — for ingested-tier,
    the DB owns metadata.
    """
    from brain.ingest import ExtractedDoc, ingest_document

    ingest_document(
        test_db,
        embedder=fake_embedder,
        doc=ExtractedDoc(
            title="Krisp call",
            content="transcript body\n",
            content_type="transcript",
            source_path=None,
            metadata={"date": "2026-04-15", "duration_min": 42},
        ),
        source_kind="krisp",
        source_external_id="abc-123",
        source_metadata={"date": "2026-04-15"},
    )
    vault = tmp_path / "vault"
    export_vault(test_db, vault_path=vault)
    # First sync settles the row.
    _sync(test_db, fake_embedder, vault)
    pre = test_db.execute(
        "SELECT metadata FROM documents WHERE title = 'Krisp call'"
    ).fetchone()
    assert pre is not None
    pre_meta = pre[0]
    assert pre_meta.get("date") == "2026-04-15"
    assert pre_meta.get("duration_min") == 42

    # Now: re-sync — metadata should NOT be touched.
    _sync(test_db, fake_embedder, vault)
    post = test_db.execute(
        "SELECT metadata FROM documents WHERE title = 'Krisp call'"
    ).fetchone()
    assert post is not None
    post_meta = post[0]
    assert post_meta.get("date") == "2026-04-15"
    assert post_meta.get("duration_min") == 42


def test_ingested_aliases_added_via_file_are_merged(
    test_db: psycopg.Connection, fake_embedder, tmp_path: Path
) -> None:
    """User adds an alias to an _ingested/ file → merged in, DB metadata preserved.

    The exception to "DB owns ingested metadata": user-authored aliases on
    an ingested mirror are intentional (user wants `[[person-x]]` to resolve to
    a krisp call). They flow into ``metadata.aliases`` without disturbing
    other DB-owned fields.
    """
    from brain.ingest import ExtractedDoc, ingest_document

    ingest_document(
        test_db,
        embedder=fake_embedder,
        doc=ExtractedDoc(
            title="Krisp call",
            content="transcript\n",
            content_type="transcript",
            source_path=None,
            metadata={"date": "2026-04-15", "duration_min": 42},
        ),
        source_kind="krisp",
        source_external_id="abc-123",
    )
    vault = tmp_path / "vault"
    export_vault(test_db, vault_path=vault)
    # Find the exported file and add aliases to its frontmatter.
    target = next((vault / "_ingested" / "krisp").glob("*.md"))
    fields, body = parse_frontmatter(target.read_text())
    fields["aliases"] = ["person-a-call"]
    target.write_text(dump_frontmatter(fields, body))
    # First settle the row, then sync again with aliases added.
    _sync(test_db, fake_embedder, vault)
    _sync(test_db, fake_embedder, vault)

    row = test_db.execute(
        "SELECT metadata FROM documents WHERE title = 'Krisp call'"
    ).fetchone()
    assert row is not None
    meta = row[0]
    # Aliases merged in.
    assert meta.get("aliases") == ["person-a-call"]
    # Original DB-owned fields preserved.
    assert meta.get("date") == "2026-04-15"
    assert meta.get("duration_min") == 42


def test_vault_tier_metadata_overwrites_from_frontmatter(
    test_db: psycopg.Connection, fake_embedder, tmp_path: Path
) -> None:
    """Vault-tier metadata IS overwritten — file is authoritative.

    Counterpart of test_ingested_metadata_preserved_across_sync: for vault
    notes, removing a frontmatter field actually removes it from
    ``documents.metadata`` on the next sync.
    """
    vault = tmp_path / "vault"
    note_id = str(uuid.uuid4())
    file_path = vault / "n.md"
    _write(
        file_path,
        {"id": note_id, "title": "Vault Note", "priority": "high"},
        "x\n",
    )
    _sync(test_db, fake_embedder, vault)
    # Drop the priority field.
    _write(file_path, {"id": note_id, "title": "Vault Note"}, "x\n")
    _sync(test_db, fake_embedder, vault)
    row = test_db.execute(
        "SELECT metadata FROM documents WHERE id = %s", (note_id,)
    ).fetchone()
    assert row is not None
    assert "priority" not in (row[0] or {})


def test_ingested_authored_creates_source_row(
    test_db: psycopg.Connection, fake_embedder, tmp_path: Path
) -> None:
    """A fresh ``_ingested/krisp/foo.md`` (no prior DB row) creates a sources row.

    Without this, ``[[krisp:abc-1]]`` resolution — which JOINs on sources —
    would miss the row and the link would land in unresolved_links forever.
    """
    vault = tmp_path / "vault"
    auth_id = str(uuid.uuid4())
    src_id = str(uuid.uuid4())
    # Author an ingested-tier file directly (no `brain ingest` call ran).
    _write(
        vault / "_ingested" / "krisp" / "fresh.md",
        {
            "id": auth_id,
            "title": "Fresh Krisp",
            "kind": "ingested",
            "source": "krisp",
            "external_id": "abc-1",
        },
        "transcript content\n",
    )
    # And a vault-tier note that references it via [[krisp:abc-1]].
    _write(
        vault / "src.md",
        {"id": src_id, "title": "Src"},
        "see [[krisp:abc-1]]\n",
    )
    report = _sync(test_db, fake_embedder, vault)
    assert report.created == 2
    # The link resolved — a sources row was created and the JOIN found it.
    assert report.links_resolved == 1
    assert report.links_unresolved == 0
    # Verify DB state directly: the ingested doc has source_id pointing at a
    # sources row with kind='krisp', external_id='abc-1'.
    row = test_db.execute(
        "SELECT s.kind, s.external_id "
        "FROM documents d JOIN sources s ON s.id = d.source_id "
        "WHERE d.id = %s",
        (auth_id,),
    ).fetchone()
    assert row == ("krisp", "abc-1")


def test_ingested_authored_dedups_source_row_on_re_sync(
    test_db: psycopg.Connection, fake_embedder, tmp_path: Path
) -> None:
    """Re-syncing the same ingested-tier file doesn't duplicate the sources row."""
    vault = tmp_path / "vault"
    auth_id = str(uuid.uuid4())
    _write(
        vault / "_ingested" / "krisp" / "fresh.md",
        {
            "id": auth_id,
            "title": "Fresh",
            "kind": "ingested",
            "source": "krisp",
            "external_id": "abc-2",
        },
        "x\n",
    )
    _sync(test_db, fake_embedder, vault)
    _sync(test_db, fake_embedder, vault)
    cnt = test_db.execute(
        "SELECT count(*) FROM sources WHERE kind = 'krisp' AND external_id = 'abc-2'"
    ).fetchone()
    assert cnt is not None
    assert cnt[0] == 1


def test_duplicate_body_vault_notes_both_sync(
    test_db: psycopg.Connection, fake_embedder, tmp_path: Path
) -> None:
    """Two vault-tier notes with identical bodies both end up as separate rows.

    Migration 004 relaxes the ``UNIQUE(content_hash)`` constraint to apply
    only to ingested-tier — vault-tier notes legitimately share body bytes
    (two empty drafts, two ``"TBD"`` placeholders, two template-derived
    notes). Pre-migration, this scenario crashed sync with a
    ``UniqueViolation``.
    """
    vault = tmp_path / "vault"
    a_id = str(uuid.uuid4())
    b_id = str(uuid.uuid4())
    # Same body bytes in two different files.
    _write(vault / "a.md", {"id": a_id, "title": "Draft A"}, "TBD\n")
    _write(vault / "b.md", {"id": b_id, "title": "Draft B"}, "TBD\n")
    report = _sync(test_db, fake_embedder, vault)
    assert report.created == 2
    assert report.errors == []
    # Both rows present, both with vault_path populated.
    rows = test_db.execute(
        "SELECT id::text FROM documents WHERE kind = 'vault' ORDER BY id"
    ).fetchall()
    assert len(rows) == 2


def test_ingested_tier_still_dedups_on_content_hash(
    test_db: psycopg.Connection, fake_embedder
) -> None:
    """The partial unique index continues to enforce dedup for ingested-tier.

    Two ingested-tier rows with the same content_hash must collide — that's
    what stops re-ingesting the same Krisp call from producing two rows.
    """
    test_db.execute(
        "INSERT INTO documents "
        "(title, content, content_hash, content_type, kind) "
        "VALUES ('A', 'body', 'shared-hash', 'note', 'ingested')"
    )
    with pytest.raises(psycopg.errors.UniqueViolation):
        test_db.execute(
            "INSERT INTO documents "
            "(title, content, content_hash, content_type, kind) "
            "VALUES ('B', 'body', 'shared-hash', 'note', 'ingested')"
        )


def test_retry_pass_skips_invalid_link_kind(
    test_db: psycopg.Connection, fake_embedder, tmp_path: Path
) -> None:
    """Bypass via raw INSERT to a row with an invalid link_kind column.

    The CHECK constraint on link_kind would reject this normally, but we
    can simulate the path that defends against future schema drift by
    bypassing materialize_links and relying on the retry-pass code path's
    own validation.
    """
    # Easier to test: just verify _reparse_link_text directly.
    from brain.vault.sync import _reparse_link_text

    # Empty input → None.
    assert _reparse_link_text("", "wiki", None) is None
    # Invalid link_kind → None.
    assert _reparse_link_text("[[X]]", "bogus", None) is None
    # Valid → returns ParsedLink.
    parsed = _reparse_link_text("[[X]]", "wiki", None)
    assert parsed is not None
    assert parsed.target_value == "X"


# ---------------------------------------------------------------------------
# Metadata-derived linker pass — Task B.5 wires ``rebuild_derived_for`` into
# both ``sync_vault`` and ``sync_one_file`` so derived edges stay current
# every time the vault reconciles with the DB.
# ---------------------------------------------------------------------------


def test_sync_vault_rebuilds_derived_links(
    test_db: psycopg.Connection, fake_embedder, tmp_path: Path
) -> None:
    """Two Gmail docs in the same thread → one ``shared_thread`` derived edge.

    End-to-end: ingest two Gmail messages into the DB (linker not yet run),
    export the vault, then run ``sync_vault`` — the post-sync linker pass
    rebuilds ``derived_links`` for the touched docs and the report counter
    matches the inserted-row count.
    """
    a_id = _ingest_gmail(
        test_db,
        fake_embedder,
        external_id="syncv-a",
        title="Re: deal",
        body="hello there\n",
        from_header="person-x <person-a@example.com>",
        to_header="Ali <ali@example.com>",
        thread_id="t-syncv",
        date="Wed, 15 Apr 2026 12:00:00 -0700",
    )
    b_id = _ingest_gmail(
        test_db,
        fake_embedder,
        external_id="syncv-b",
        title="Re: deal",
        body="hi back\n",
        from_header="Ali <ali@example.com>",
        to_header="person-x <person-a@example.com>",
        thread_id="t-syncv",
        date="Wed, 15 Apr 2026 12:30:00 -0700",
    )
    # Sanity: ingest itself doesn't run the linker.
    assert _derived_count(test_db) == 0

    vault = tmp_path / "vault"
    export_vault(test_db, vault_path=vault)

    report = _sync(test_db, fake_embedder, vault)

    rows = _derived_rows(test_db)
    shared_thread_rows = [r for r in rows if r[2] == "shared_thread"]
    assert len(shared_thread_rows) == 1
    src, dst, rule, _weight = shared_thread_rows[0]
    assert {src, dst} == {a_id, b_id}
    assert src < dst  # canonical (LEAST, GREATEST) ordering
    assert rule == "shared_thread"
    # Both docs share participants → R2 (shared_participant) also fires
    # alongside R1, so the total inserted count covers every rule that
    # landed for the pair.
    assert report.derived_links == len(rows)
    assert report.derived_links >= 1


def test_sync_one_file_rebuilds_derived_links_for_that_file(
    test_db: psycopg.Connection, fake_embedder, tmp_path: Path
) -> None:
    """``sync_one_file`` only rebuilds edges touching the file it processed.

    Three Gmail docs in the same thread, exported to disk but never
    full-synced. ``sync_one_file`` on A's file alone must generate only
    A↔B and A↔C pairs — B↔C is absent because B and C aren't in the
    touched set. Mirrors ``test_rebuild_only_touches_doc_ids_in_set`` in
    ``test_pass_runner.py`` but goes through the sync hook end-to-end.
    """
    a_id = _ingest_gmail(
        test_db,
        fake_embedder,
        external_id="sync1-a",
        title="A msg",
        body="A body\n",
        from_header="person-x <person-a@example.com>",
        to_header="Ali <ali@example.com>",
        thread_id="t-sync1",
        date="Wed, 15 Apr 2026 12:00:00 -0700",
    )
    b_id = _ingest_gmail(
        test_db,
        fake_embedder,
        external_id="sync1-b",
        title="B msg",
        body="B body\n",
        from_header="Ali <ali@example.com>",
        to_header="person-x <person-a@example.com>",
        thread_id="t-sync1",
        date="Wed, 15 Apr 2026 13:00:00 -0700",
    )
    c_id = _ingest_gmail(
        test_db,
        fake_embedder,
        external_id="sync1-c",
        title="C msg",
        body="C body\n",
        from_header="person-x <person-a@example.com>",
        to_header="Ali <ali@example.com>",
        thread_id="t-sync1",
        date="Wed, 15 Apr 2026 14:00:00 -0700",
    )

    vault = tmp_path / "vault"
    export_vault(test_db, vault_path=vault)
    # Pre-condition: ingest doesn't run the linker, so the table starts empty.
    assert _derived_count(test_db) == 0

    # Locate A's exported file by frontmatter id (post-ingest ``vault_path``
    # is NULL — sync writes it; export only writes the file). Filename
    # construction is an internal export detail, so match on the stable
    # frontmatter id instead of the on-disk slug.
    gmail_dir = vault / "_ingested" / "gmail"
    a_path: Path | None = None
    for candidate in gmail_dir.glob("*.md"):
        fields, _ = parse_frontmatter(candidate.read_text())
        if fields.get("id") == a_id:
            a_path = candidate
            break
    assert a_path is not None, "could not find A's exported file"

    one_report = sync_one_file(
        test_db,
        embedder=fake_embedder,
        vault_path=vault,
        file_path=a_path,
    )

    rows = _derived_rows(test_db)
    # Every inserted edge involves A — the rebuild's touched set is {a_id}
    # so pair generation only emits canonical pairs that include A. B↔C is
    # absent because B and C weren't in the touched set.
    assert rows
    for src, dst, *_ in rows:
        assert a_id in {src, dst}
    bc_rows = [r for r in rows if {r[0], r[1]} == {b_id, c_id}]
    assert bc_rows == []
    # Report counter equals the rebuild's inserted-row count for this call.
    assert one_report.derived_links == len(rows)


def test_sync_vault_dry_run_skips_linker(
    test_db: psycopg.Connection, fake_embedder, tmp_path: Path
) -> None:
    """``dry_run=True`` must NOT write derived edges.

    The dry-run contract is "no DB writes"; gating the linker on
    ``not dry_run`` keeps it consistent. We pre-stage two Gmail docs that
    would otherwise trigger ``shared_thread`` edges, then verify
    ``sync_vault(..., dry_run=True)`` leaves ``derived_links`` empty and
    ``report.derived_links == 0``.
    """
    _ingest_gmail(
        test_db,
        fake_embedder,
        external_id="dry-a",
        title="A",
        body="A body\n",
        from_header="person-x <person-a@example.com>",
        to_header="Ali <ali@example.com>",
        thread_id="t-dry",
        date="Wed, 15 Apr 2026 12:00:00 -0700",
    )
    _ingest_gmail(
        test_db,
        fake_embedder,
        external_id="dry-b",
        title="B",
        body="B body\n",
        from_header="Ali <ali@example.com>",
        to_header="person-x <person-a@example.com>",
        thread_id="t-dry",
        date="Wed, 15 Apr 2026 13:00:00 -0700",
    )
    vault = tmp_path / "vault"
    export_vault(test_db, vault_path=vault)
    assert _derived_count(test_db) == 0

    report = _sync(test_db, fake_embedder, vault, dry_run=True)

    assert _derived_count(test_db) == 0
    assert report.derived_links == 0


def test_sync_vault_with_no_linkable_docs_succeeds(
    test_db: psycopg.Connection, fake_embedder, tmp_path: Path
) -> None:
    """Vault-tier-only sync runs the linker pass cleanly with zero edges.

    ``rebuild_derived_for`` receives the seen vault doc-ids but its SELECT
    filters to ``sources.kind in {gmail, krisp}`` so vault-tier ids are
    silently dropped from the candidate pool. The pass is a no-op and the
    counter stays at 0.
    """
    vault = tmp_path / "vault"
    a_id = str(uuid.uuid4())
    b_id = str(uuid.uuid4())
    _write(vault / "a.md", {"id": a_id, "title": "A"}, "vault A\n")
    _write(vault / "b.md", {"id": b_id, "title": "B"}, "vault B\n")

    report = _sync(test_db, fake_embedder, vault)

    assert report.errors == []
    assert report.created == 2
    assert report.derived_links == 0
    assert _derived_count(test_db) == 0


# ---------------------------------------------------------------------------
# Phase D — fence-awareness in body_hash, _normalized_body, _materialize_links.
# ---------------------------------------------------------------------------


def _fenced_body(base_body: str, *bullets: str) -> str:
    """Compose a body with a ``BRAIN_DERIVED`` fence appended after a blank line.

    Mirrors the shape :func:`brain.vault.derived_links.fence.replace_fence`
    produces in production: one blank line between body and START marker,
    one trailing newline after END. Tests use this to construct the same
    fenced bodies the linker would generate without depending on the
    renderer's DB query path.
    """
    from brain.vault.derived_links.fence import (
        FENCE_END_MARKER,
        FENCE_START_MARKER,
    )

    fence_lines = [FENCE_START_MARKER, "## Related (auto-generated, do not edit)"]
    fence_lines.extend(bullets)
    fence_lines.append(FENCE_END_MARKER)
    return f"{base_body.rstrip()}\n\n" + "\n".join(fence_lines) + "\n"


def test_body_hash_stable_across_fence_content(
    fake_embedder,  # noqa: ARG001 - signature parity with peers
) -> None:
    """A file with two different fence bodies must hash identically.

    This is the property that prevents the relink → re-embed cascade: the
    linker rewrites the fence on every relink; if ``body_hash`` saw the
    fence as authored content, every relink would mark every ingested file
    as "body changed" and re-embed the corpus. The fence is contractually
    not authored body and ``body_hash`` strips it before hashing.
    """
    base = "# Title\n\nBody paragraph.\n"
    file_a = dump_frontmatter(
        {"id": "x", "title": "T"},
        _fenced_body(base, "- [[a-stem|A]] *(shared_thread)*"),
    )
    file_b = dump_frontmatter(
        {"id": "x", "title": "T"},
        _fenced_body(
            base,
            "- [[a-stem|A]] *(shared_thread)*",
            "- [[b-stem|B]] *(shared_participant)*",
        ),
    )
    from brain.vault.frontmatter import body_hash
    assert body_hash(file_a) == body_hash(file_b)


def test_body_hash_same_with_and_without_fence(
    fake_embedder,  # noqa: ARG001 - signature parity with peers
) -> None:
    """A fenced file hashes the same as the same file with the fence removed.

    Inverse of the previous test: removing the fence entirely (the user
    deletes the section, or the renderer hasn't run yet) must not change
    the hash. Otherwise a fresh sync of a never-rendered file would re-embed
    after the linker first appends the fence — wasted work.
    """
    base = "# Title\n\nBody paragraph.\n"
    fenced = dump_frontmatter(
        {"id": "x", "title": "T"},
        _fenced_body(base, "- [[a-stem|A]] *(shared_thread)*"),
    )
    fenceless = dump_frontmatter({"id": "x", "title": "T"}, base)
    from brain.vault.frontmatter import body_hash
    assert body_hash(fenced) == body_hash(fenceless)


def test_legacy_body_hash_strips_fence(fake_embedder) -> None:  # noqa: ARG001
    """``_legacy_body_hash`` must also strip the fence.

    The legacy hash backstops the round-trip-no-op contract for files
    exported under Phase 1 semantics (``content_hash = sha256(doc.content)``,
    no normalization). If the linker appended a fence to such a file, the
    legacy form must still match — otherwise the silent-hash-migration
    branch in ``_sync_one`` would treat a fence-only change as a body
    change and re-embed.
    """
    import hashlib

    from brain.vault.sync import _legacy_body_hash

    base_body = "the unchanged authored body\n"
    fenced_body = _fenced_body(base_body, "- [[a|A]] *(shared_thread)*")
    # Legacy hash of the original body (no fence).
    expected = hashlib.sha256(base_body.encode("utf-8")).hexdigest()
    assert _legacy_body_hash(fenced_body) == expected


def test_fence_internal_wiki_links_not_in_links_table(
    test_db: psycopg.Connection, fake_embedder, tmp_path: Path
) -> None:
    """Wiki-links inside the auto-generated fence don't show up in ``links``.

    Phase D's whole point is that derived edges live in ``derived_links``,
    not ``links`` — and the fenced "Related" section's bullets are wiki-
    links to those same partners. If the wiki-link parser saw the fence,
    we'd double-count every derived edge in ``links`` AND ``derived_links``.
    """
    vault = tmp_path / "vault"
    target_id = str(uuid.uuid4())
    src_id = str(uuid.uuid4())

    # The target the fence's bullet points at — needs to exist so the link
    # would resolve if the parser saw it. (We're testing the parser doesn't
    # see it; "would resolve" makes the negative assertion meaningful.)
    _write(
        vault / "target.md",
        {"id": target_id, "title": "Target"},
        "target body\n",
    )
    # Source file with both an authored wiki-link AND a fence containing
    # a separate wiki-link. The authored link MUST land in ``links``;
    # the fence-internal one MUST NOT.
    _write(
        vault / "source.md",
        {"id": src_id, "title": "Source"},
        _fenced_body(
            "# Source\n\nAn [[Target]] reference in the body.\n",
            "- [[target|Target]] *(shared_thread)*",
            "- [[other-stem|Other Title]] *(shared_participant)*",
        ),
    )

    report = _sync(test_db, fake_embedder, vault)
    assert report.errors == []

    # Authored ``[[Target]]`` resolves and lands in ``links``. The fence's
    # ``[[target|Target]]`` is ALSO a wiki-link (different raw text — has a
    # display alias), so if the parser saw both we'd see two link rows for
    # this src_id. We check both the count and the link_text.
    rows = test_db.execute(
        "SELECT link_text, dst_document_id::text FROM links "
        "WHERE src_document_id = %s ORDER BY link_text",
        (src_id,),
    ).fetchall()
    assert len(rows) == 1, f"expected exactly 1 link row, got {rows!r}"
    raw_text, dst = rows[0]
    assert raw_text == "[[Target]]"  # authored, no alias
    assert dst == target_id

    # No unresolved-link row for the fence's ``[[other-stem|Other Title]]``
    # either — the fence is invisible to the link materializer entirely.
    unresolved = test_db.execute(
        "SELECT link_text FROM unresolved_links WHERE src_document_id = %s",
        (src_id,),
    ).fetchall()
    assert unresolved == []


def test_fence_only_change_does_not_re_embed(
    test_db: psycopg.Connection, fake_embedder, tmp_path: Path
) -> None:
    """Re-syncing after a fence rewrite is a clean ``skipped`` — no re-chunk.

    End-to-end check that the three fence strips (body_hash, normalized_body,
    legacy_body_hash) compose into the desired property: a relink that only
    edits the fence content of a file produces zero re-embeds on the next
    sync. The sync engine's ``skipped`` counter tracks the no-op path.
    """
    vault = tmp_path / "vault"
    doc_id = str(uuid.uuid4())
    base = "# Hello\n\nstable body content\n"

    # First sync — file has no fence yet.
    _write(vault / "note.md", {"id": doc_id, "title": "Hello"}, base)
    first = _sync(test_db, fake_embedder, vault)
    assert first.created == 1

    # Capture the chunks' ids so we can assert they weren't re-inserted.
    chunk_ids_before = sorted(
        r[0]
        for r in test_db.execute(
            "SELECT id::text FROM chunks WHERE document_id = %s",
            (doc_id,),
        ).fetchall()
    )

    # Now rewrite the file with a fence appended (simulating the linker's
    # output). Body content is unchanged; only the fence appears.
    fenced = _fenced_body(base, "- [[a-stem|A]] *(shared_thread)*")
    _write(vault / "note.md", {"id": doc_id, "title": "Hello"}, fenced)

    second = _sync(test_db, fake_embedder, vault)
    assert second.errors == []
    # The doc is "skipped" — body unchanged under the fence-aware hash.
    assert second.skipped == 1
    assert second.updated == 0

    # Same chunks (same ids) — re-embed would have DELETEd + re-INSERTed.
    chunk_ids_after = sorted(
        r[0]
        for r in test_db.execute(
            "SELECT id::text FROM chunks WHERE document_id = %s",
            (doc_id,),
        ).fetchall()
    )
    assert chunk_ids_after == chunk_ids_before


def test_documents_content_excludes_fence(
    test_db: psycopg.Connection, fake_embedder, tmp_path: Path
) -> None:
    """``documents.content`` for a fenced file stores the body WITHOUT the fence.

    Vector search ranks chunks by cosine similarity over the chunk content,
    which is sourced from ``documents.content``. If the fence were stored,
    a query like "Related auto-generated" would surface every ingested
    file that has any derived edges — pure noise. The fence strip in
    ``_normalized_body`` is what prevents that.
    """
    vault = tmp_path / "vault"
    doc_id = str(uuid.uuid4())
    base = "# Title\n\nThe authored body content goes here.\n"
    fenced = _fenced_body(
        base,
        "- [[partner-stem|Partner]] *(shared_thread)*",
    )
    _write(vault / "note.md", {"id": doc_id, "title": "Title"}, fenced)

    report = _sync(test_db, fake_embedder, vault)
    assert report.errors == []

    row = test_db.execute(
        "SELECT content FROM documents WHERE id = %s", (doc_id,)
    ).fetchone()
    assert row is not None
    stored = str(row[0])
    assert "BRAIN_DERIVED_START" not in stored
    assert "BRAIN_DERIVED_END" not in stored
    assert "auto-generated" not in stored
    assert "partner-stem" not in stored
    # The authored body did make it through unchanged.
    assert "The authored body content goes here." in stored


def test_no_regression_for_fenceless_files(
    test_db: psycopg.Connection, fake_embedder, tmp_path: Path
) -> None:
    """Files without a fence behave exactly as before — strip_fence is a no-op.

    Backstop for the regression risk in the plan: every existing test in
    this module sees fence-less files. This explicit test pins the
    behavior so a future refactor of ``strip_fence`` (e.g. accidentally
    rstripping fence-less bodies) is caught immediately.
    """
    vault = tmp_path / "vault"
    doc_id = str(uuid.uuid4())
    body = "# Plain Note\n\nNo fence here, never has been.\n"
    _write(vault / "plain.md", {"id": doc_id, "title": "Plain Note"}, body)

    first = _sync(test_db, fake_embedder, vault)
    assert first.errors == []
    assert first.created == 1

    # Re-sync with no changes is a clean skip.
    second = _sync(test_db, fake_embedder, vault)
    assert second.errors == []
    assert second.skipped == 1
    assert second.updated == 0
    assert second.created == 0

    # Body is stored verbatim under normalization (LF, stripped trailing
    # newline) — no fence-related rewrites snuck in.
    row = test_db.execute(
        "SELECT content FROM documents WHERE id = %s", (doc_id,)
    ).fetchone()
    assert row is not None
    assert str(row[0]) == body.strip()


# ---------------------------------------------------------------------------
# Phase 3 — sync normalizes tags from the file → DB hop AND writes the
# canonical form back to disk so future syncs are no-ops.
# ---------------------------------------------------------------------------


def test_sync_normalizes_tags_from_frontmatter_into_db_and_disk(
    test_db: psycopg.Connection, fake_embedder, tmp_path: Path
) -> None:
    """File tags are canonicalized in BOTH the DB column and the file frontmatter.

    Setup: a vault file with a mix of casing + a duplicate that only
    differs by case. Exercise: ``sync_vault``. Verify: DB ``tags`` column
    holds the canonical lowercase deduped list, AND the file's
    frontmatter is rewritten to the same canonical list (so the next
    sync has nothing to do).
    """
    vault = tmp_path / "vault"
    note_id = str(uuid.uuid4())
    _write(
        vault / "n.md",
        {"id": note_id, "title": "X", "tags": ["Foo", "BAR", "foo"]},
        "x\n",
    )

    _sync(test_db, fake_embedder, vault)

    row = test_db.execute(
        "SELECT tags FROM documents WHERE id = %s", (note_id,)
    ).fetchone()
    assert row is not None
    assert list(row[0]) == ["foo", "bar"]

    fields, _ = parse_frontmatter((vault / "n.md").read_text(encoding="utf-8"))
    assert fields["tags"] == ["foo", "bar"]


def test_sync_canonical_tags_is_a_no_op_for_disk(
    test_db: psycopg.Connection, fake_embedder, tmp_path: Path
) -> None:
    """A file already in canonical form syncs without touching disk.

    Regression guard: the tag-normalization write-back must NOT trigger on
    files that are already canonical (would churn mtime + git history on
    every sync).
    """
    vault = tmp_path / "vault"
    note_id = str(uuid.uuid4())
    file_path = vault / "n.md"
    _write(
        file_path,
        {"id": note_id, "title": "X", "tags": ["foo", "bar"]},
        "x\n",
    )
    before_bytes = file_path.read_bytes()

    _sync(test_db, fake_embedder, vault)

    assert file_path.read_bytes() == before_bytes
    row = test_db.execute(
        "SELECT tags FROM documents WHERE id = %s", (note_id,)
    ).fetchone()
    assert row is not None
    assert list(row[0]) == ["foo", "bar"]
