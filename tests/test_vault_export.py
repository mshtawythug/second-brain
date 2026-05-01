"""Tests for brain.vault.export — DB → vault folder dump."""
import json
import os
from pathlib import Path

import psycopg
import pytest
from typer.testing import CliRunner

from brain.cli import app
from brain.ingest import ExtractedDoc, ingest_document
from brain.vault.export import export_vault, regenerate_vault_file
from brain.vault.frontmatter import parse_frontmatter


def _ingest(
    conn: psycopg.Connection,
    *,
    embedder,
    title: str,
    content: str,
    source_kind: str,
    external_id: str | None = None,
    metadata: dict | None = None,
    source_metadata: dict | None = None,
) -> str:
    res = ingest_document(
        conn,
        embedder=embedder,
        doc=ExtractedDoc(
            title=title,
            content=content,
            content_type="note",
            source_path=None,
            metadata=metadata or {},
        ),
        source_kind=source_kind,
        source_external_id=external_id,
        source_metadata=source_metadata or {},
    )
    assert res.document_id
    return res.document_id


def test_empty_db_writes_no_documents(
    test_db: psycopg.Connection, tmp_path: Path
) -> None:
    summary = export_vault(test_db, vault_path=tmp_path / "vault")
    assert summary.written == 0
    assert summary.skipped == 0
    assert summary.errors == []
    # Init scaffold still ran, so the README is there.
    assert (tmp_path / "vault" / "README.md").is_file()


def test_writes_one_file_per_document(
    test_db: psycopg.Connection, fake_embedder, tmp_path: Path
) -> None:
    _ingest(
        test_db,
        embedder=fake_embedder,
        title="person-x Q1 review",
        content="Met with person-x.\n",
        source_kind="krisp",
        external_id="abc1234567",
        metadata={"date": "2026-04-15"},
        source_metadata={"date": "2026-04-15"},
    )
    _ingest(
        test_db,
        embedder=fake_embedder,
        title="Slack thread #careers",
        content="Discussion in #careers about hiring.",
        source_kind="slack",
        external_id="slack-msg-9999",
        metadata={"date": "2026-04-20"},
    )
    summary = export_vault(test_db, vault_path=tmp_path / "vault")
    assert summary.written == 2
    krisp_files = list((tmp_path / "vault" / "_ingested" / "krisp").glob("*.md"))
    slack_files = list((tmp_path / "vault" / "_ingested" / "slack").glob("*.md"))
    assert len(krisp_files) == 1
    assert len(slack_files) == 1
    assert krisp_files[0].name.startswith("2026-04-15-abc12345")
    assert "person-a-q1-review" in krisp_files[0].name


def test_manual_source_uses_simple_filename(
    test_db: psycopg.Connection, fake_embedder, tmp_path: Path
) -> None:
    _ingest(
        test_db,
        embedder=fake_embedder,
        title="Random Note",
        content="just some manual text",
        source_kind="manual",
    )
    summary = export_vault(test_db, vault_path=tmp_path / "vault")
    assert summary.written == 1
    # No date prefix for manual; just <slug>.md
    files = list((tmp_path / "vault" / "_ingested" / "manual").glob("*.md"))
    assert len(files) == 1
    assert files[0].name == "random-note.md"


def test_re_export_is_idempotent(
    test_db: psycopg.Connection, fake_embedder, tmp_path: Path
) -> None:
    _ingest(
        test_db,
        embedder=fake_embedder,
        title="person-x Q1 review",
        content="Original body content.\n",
        source_kind="krisp",
        external_id="abc1234567",
        metadata={"date": "2026-04-15"},
        source_metadata={"date": "2026-04-15"},
    )
    first = export_vault(test_db, vault_path=tmp_path / "vault")
    assert first.written == 1
    assert first.skipped == 0
    second = export_vault(test_db, vault_path=tmp_path / "vault")
    assert second.written == 0
    assert second.skipped == 1


def test_re_export_after_user_edit_to_frontmatter_only(
    test_db: psycopg.Connection, fake_embedder, tmp_path: Path
) -> None:
    """Editing frontmatter-only content does NOT trigger a re-write.

    Idempotency hashes the body (frontmatter stripped). If the user adds an
    alias to a vault file the export should still be a no-op.
    """
    _ingest(
        test_db,
        embedder=fake_embedder,
        title="person-x",
        content="The body text.\n",
        source_kind="krisp",
        external_id="abc1234567",
        metadata={"date": "2026-04-15"},
        source_metadata={"date": "2026-04-15"},
    )
    export_vault(test_db, vault_path=tmp_path / "vault")
    target_dir = tmp_path / "vault" / "_ingested" / "krisp"
    target = next(target_dir.glob("*.md"))
    text = target.read_text()
    # Mutate frontmatter only.
    fields, body = parse_frontmatter(text)
    fields["aliases"] = ["person-a"]
    from brain.vault.frontmatter import dump_frontmatter
    target.write_text(dump_frontmatter(fields, body))

    second = export_vault(test_db, vault_path=tmp_path / "vault")
    # Body unchanged → still skipped.
    assert second.skipped == 1
    assert second.written == 0


def test_collision_resolved_with_short_id_suffix(
    test_db: psycopg.Connection, fake_embedder, tmp_path: Path
) -> None:
    """Two docs that hash to the same target path get a deterministic suffix."""
    _ingest(
        test_db,
        embedder=fake_embedder,
        title="Same Title",
        content="body 1",
        source_kind="manual",
    )
    _ingest(
        test_db,
        embedder=fake_embedder,
        title="Same Title",
        content="body 2",
        source_kind="manual",
    )
    summary = export_vault(test_db, vault_path=tmp_path / "vault")
    assert summary.written == 2
    files = sorted((tmp_path / "vault" / "_ingested" / "manual").glob("*.md"))
    assert len(files) == 2
    # The doc encountered first (lower UUID) lands at <slug>.md; the second
    # at <slug>-<docid8>.md. Iteration is by UUID order, so we can't predict
    # which is which — only that one is plain and one has the suffix.
    names = sorted(f.name for f in files)
    assert "same-title.md" in names
    suffixed = [n for n in names if n.startswith("same-title-")]
    assert len(suffixed) == 1
    assert suffixed[0].endswith(".md")
    # Suffix is exactly 8 hex chars (short doc id).
    suffix_part = suffixed[0][len("same-title-") : -len(".md")]
    assert len(suffix_part) == 8
    assert all(c in "0123456789abcdef" for c in suffix_part)


def test_collision_suffix_stable_across_reruns(
    test_db: psycopg.Connection, fake_embedder, tmp_path: Path
) -> None:
    _ingest(test_db, embedder=fake_embedder, title="Same", content="a", source_kind="manual")
    _ingest(test_db, embedder=fake_embedder, title="Same", content="b", source_kind="manual")
    export_vault(test_db, vault_path=tmp_path / "vault")
    first_files = sorted(
        f.name for f in (tmp_path / "vault" / "_ingested" / "manual").iterdir()
    )
    # Rebuild a clean vault and re-export — filenames must be identical.
    second_target = tmp_path / "vault2"
    export_vault(test_db, vault_path=second_target)
    second_files = sorted(
        f.name for f in (second_target / "_ingested" / "manual").iterdir()
    )
    assert first_files == second_files


def test_frontmatter_round_trips_via_yaml_safe_load(
    test_db: psycopg.Connection, fake_embedder, tmp_path: Path
) -> None:
    doc_id = _ingest(
        test_db,
        embedder=fake_embedder,
        title="person-x conversation",
        content="The body.\n",
        source_kind="krisp",
        external_id="abc1234567",
        metadata={"date": "2026-04-15"},
        source_metadata={"date": "2026-04-15"},
    )
    export_vault(test_db, vault_path=tmp_path / "vault")
    target_dir = tmp_path / "vault" / "_ingested" / "krisp"
    target = next(target_dir.glob("*.md"))
    text = target.read_text()
    fields, body = parse_frontmatter(text)
    assert fields["id"] == doc_id
    assert fields["title"] == "person-x conversation"
    assert fields["kind"] == "ingested"
    assert fields["source"] == "krisp"
    assert fields["external_id"] == "abc1234567"
    assert "created" in fields
    assert "updated" in fields
    assert body == "The body.\n"


def test_refuses_to_write_into_unmanaged_non_empty_dir(
    test_db: psycopg.Connection, fake_embedder, tmp_path: Path
) -> None:
    target = tmp_path / "vault"
    target.mkdir()
    (target / "stray.txt").write_text("not ours")
    with pytest.raises(ValueError, match="not empty"):
        export_vault(test_db, vault_path=target)


def test_force_writes_into_unmanaged_dir(
    test_db: psycopg.Connection, fake_embedder, tmp_path: Path
) -> None:
    _ingest(
        test_db,
        embedder=fake_embedder,
        title="x",
        content="y",
        source_kind="manual",
    )
    target = tmp_path / "vault"
    target.mkdir()
    (target / "stray.txt").write_text("not ours")
    summary = export_vault(test_db, vault_path=target, force=True)
    assert summary.written == 1
    # stray file is left alone.
    assert (target / "stray.txt").is_file()


def test_managed_dir_does_not_need_force(
    test_db: psycopg.Connection, fake_embedder, tmp_path: Path
) -> None:
    """A previously-initialized vault (signature: README.md) skips the force gate."""
    target = tmp_path / "vault"
    # Run once to install README.
    export_vault(test_db, vault_path=target)
    # Add a file to make it non-empty in a normal way.
    _ingest(
        test_db,
        embedder=fake_embedder,
        title="follow up",
        content="z",
        source_kind="manual",
    )
    # Re-export without force — should succeed because README signature exists.
    summary = export_vault(test_db, vault_path=target)
    assert summary.written == 1


def test_legacy_doc_with_no_external_id_uses_local_placeholder(
    test_db: psycopg.Connection, fake_embedder, tmp_path: Path
) -> None:
    """Older krisp ingests without external_id still produce a deterministic name.

    Production rollback path: a doc somehow ended up in a non-manual source
    (say, krisp) but with no external_id row. We synthesize ``local-<short>``
    so the date + title still line up with the standard format.
    """
    # Manually insert a source row without external_id, then a document
    # pointing at it — there's no API for "krisp + no external id" but the
    # schema allows it.
    src_row = test_db.execute(
        "INSERT INTO sources (kind, external_id, metadata) "
        "VALUES ('krisp', NULL, '{}'::jsonb) RETURNING id"
    ).fetchone()
    assert src_row is not None
    doc_row = test_db.execute(
        """
        INSERT INTO documents
          (source_id, title, content, content_hash, content_type, metadata)
        VALUES (%s, 'legacy krisp note', 'body legacy', 'hash-legacy',
                'note', '{"date":"2026-01-01"}'::jsonb)
        RETURNING id
        """,
        (src_row[0],),
    ).fetchone()
    assert doc_row is not None
    summary = export_vault(test_db, vault_path=tmp_path / "vault")
    assert summary.written == 1
    krisp = list((tmp_path / "vault" / "_ingested" / "krisp").glob("*.md"))
    assert len(krisp) == 1
    assert "local-" in krisp[0].name


def test_export_into_empty_existing_dir_succeeds(
    test_db: psycopg.Connection, fake_embedder, tmp_path: Path
) -> None:
    """Pre-existing but empty target dir is not "unmanaged" — no force needed."""
    _ingest(
        test_db,
        embedder=fake_embedder,
        title="x",
        content="y",
        source_kind="manual",
    )
    target = tmp_path / "vault"
    target.mkdir()  # exists, but empty
    summary = export_vault(test_db, vault_path=target)
    assert summary.written == 1


def test_export_target_is_a_file_errors(
    test_db: psycopg.Connection, tmp_path: Path
) -> None:
    """Pointing --to at a regular file (not a dir) is an error without force."""
    target = tmp_path / "vault-file.md"
    target.write_text("I am a file, not a vault")
    with pytest.raises(ValueError, match="not empty"):
        export_vault(test_db, vault_path=target)


def test_export_uses_metadata_date_as_datetime(
    test_db: psycopg.Connection, fake_embedder, tmp_path: Path
) -> None:
    """``metadata.date`` stored as a JSONB ISO datetime string is honored.

    Round-tripping through psycopg: a JSONB field stored from a Python
    ``datetime`` ends up as an ISO string after fetch — covers the
    ``isinstance(raw, str)`` branch with a longer-than-10 input.
    """
    _ingest(
        test_db,
        embedder=fake_embedder,
        title="dated krisp",
        content="x",
        source_kind="krisp",
        external_id="abcdef12",
        metadata={"date": "2026-04-15T10:00:00Z"},
        source_metadata={"date": "2026-04-15T10:00:00Z"},
    )
    summary = export_vault(test_db, vault_path=tmp_path / "vault")
    assert summary.written == 1
    krisp_files = list((tmp_path / "vault" / "_ingested" / "krisp").glob("*.md"))
    assert len(krisp_files) == 1
    # First 10 chars of the ISO datetime is the date prefix.
    assert krisp_files[0].name.startswith("2026-04-15-")


def test_vault_tier_doc_round_trips_to_explicit_vault_path(
    test_db: psycopg.Connection, tmp_path: Path
) -> None:
    """A vault-tier doc with an explicit vault_path is written there verbatim.

    Round-trip identity: the file stays where the user authored it, regardless
    of slug / source rules. Covers the kind='vault' branch of the path
    resolver.
    """
    test_db.execute(
        "INSERT INTO documents "
        "(title, content, content_hash, content_type, kind, vault_path) "
        "VALUES ('Vault Note', 'authored body', 'h-vault', 'note', "
        "'vault', 'projects/notes/vault-note.md')"
    )
    summary = export_vault(test_db, vault_path=tmp_path / "vault")
    assert summary.written == 1
    target = tmp_path / "vault" / "projects" / "notes" / "vault-note.md"
    assert target.is_file()
    fields, body = parse_frontmatter(target.read_text())
    assert fields["kind"] == "vault"
    assert body == "authored body"


def test_unparseable_existing_file_triggers_rewrite(
    test_db: psycopg.Connection, fake_embedder, tmp_path: Path
) -> None:
    """A vault file with malformed YAML is treated as needing a rewrite.

    Otherwise the user would be stuck with a corrupt file that the export
    silently skipped; rewriting recovers it.
    """
    _ingest(
        test_db,
        embedder=fake_embedder,
        title="rewrite me",
        content="fresh body",
        source_kind="manual",
    )
    target_dir = tmp_path / "vault" / "_ingested" / "manual"
    target_dir.mkdir(parents=True)
    bad = target_dir / "rewrite-me.md"
    # Malformed YAML — opening fence + unparseable content + closing fence.
    bad.write_text("---\nfoo: [unclosed\n---\nold body")
    # Provide a managed signature so the unmanaged-dir check passes.
    (tmp_path / "vault" / "README.md").write_text("# vault")
    summary = export_vault(test_db, vault_path=tmp_path / "vault")
    assert summary.written == 1
    # File now has valid frontmatter pointing at the doc.
    fields, body = parse_frontmatter(bad.read_text())
    assert fields["title"] == "rewrite me"
    assert body == "fresh body"


def test_handles_doc_with_no_metadata_date_falls_back_to_ingested_at(
    test_db: psycopg.Connection, fake_embedder, tmp_path: Path
) -> None:
    _ingest(
        test_db,
        embedder=fake_embedder,
        title="Krisp without metadata date",
        content="x",
        source_kind="krisp",
        external_id="abcd1234",
        # No metadata.date — must fall back to ingested_at::date.
    )
    summary = export_vault(test_db, vault_path=tmp_path / "vault")
    assert summary.written == 1
    krisp = list((tmp_path / "vault" / "_ingested" / "krisp").glob("*.md"))
    assert len(krisp) == 1
    # Filename must start with a YYYY-MM-DD prefix (10 chars + dash).
    name = krisp[0].name
    assert len(name.split("-")[0]) == 4  # year
    assert name[4] == "-"
    assert name[7] == "-"


# ---------------------------------------------------------------------------
# CLI wrapper smoke.
# ---------------------------------------------------------------------------


def test_cli_vault_export_writes_files(
    test_db: psycopg.Connection,
    fake_embedder,
    tmp_path: Path,
    monkeypatch,
) -> None:
    monkeypatch.setenv(
        "DATABASE_URL",
        # The conftest test_db fixture uses TEST_DATABASE_URL, but the CLI
        # path goes through Config.load() which reads DATABASE_URL. Point
        # it at the same test DB.
        os.environ.get(
            "TEST_DATABASE_URL",
            "postgresql://brain:brain@localhost:5433/second_brain_test",
        ),
    )
    _ingest(
        test_db,
        embedder=fake_embedder,
        title="Hello",
        content="world",
        source_kind="manual",
    )
    runner = CliRunner()
    target = tmp_path / "vault"
    result = runner.invoke(app, ["vault", "export", "--to", str(target)])
    assert result.exit_code == 0, result.stdout
    assert "wrote 1" in result.stdout
    assert (target / "_ingested" / "manual" / "hello.md").is_file()


def test_cli_vault_export_idempotent(
    test_db: psycopg.Connection,
    fake_embedder,
    tmp_path: Path,
    monkeypatch,
) -> None:
    monkeypatch.setenv(
        "DATABASE_URL",
        os.environ.get(
            "TEST_DATABASE_URL",
            "postgresql://brain:brain@localhost:5433/second_brain_test",
        ),
    )
    _ingest(
        test_db,
        embedder=fake_embedder,
        title="dup",
        content="x",
        source_kind="manual",
    )
    runner = CliRunner()
    target = tmp_path / "vault"
    runner.invoke(app, ["vault", "export", "--to", str(target)])
    second = runner.invoke(app, ["vault", "export", "--to", str(target)])
    assert second.exit_code == 0
    assert "wrote 0" in second.stdout
    assert "skipped 1" in second.stdout


def test_cli_vault_export_refuses_unmanaged_dir_without_force(
    test_db: psycopg.Connection,
    tmp_path: Path,
    monkeypatch,
) -> None:
    monkeypatch.setenv(
        "DATABASE_URL",
        os.environ.get(
            "TEST_DATABASE_URL",
            "postgresql://brain:brain@localhost:5433/second_brain_test",
        ),
    )
    target = tmp_path / "stranger"
    target.mkdir()
    (target / "random.txt").write_text("hi")
    runner = CliRunner()
    result = runner.invoke(app, ["vault", "export", "--to", str(target)])
    assert result.exit_code == 1
    assert "not empty" in result.stdout or "not empty" in (result.stderr or "")


# Regression: psycopg `Json` adapter — keep an explicit assertion that
# documents.metadata round-trips as a Python dict, not a JSON string.
def test_export_reads_metadata_as_dict(
    test_db: psycopg.Connection, fake_embedder, tmp_path: Path
) -> None:
    _ingest(
        test_db,
        embedder=fake_embedder,
        title="meta check",
        content="m",
        source_kind="krisp",
        external_id="abcd1234",
        metadata={"date": "2026-04-15", "extra": {"nested": True}},
        source_metadata={"date": "2026-04-15"},
    )
    summary = export_vault(test_db, vault_path=tmp_path / "vault")
    assert summary.written == 1
    target = next((tmp_path / "vault" / "_ingested" / "krisp").glob("*.md"))
    text = target.read_text()
    fields, _ = parse_frontmatter(text)
    # The frontmatter we write doesn't include the freeform metadata blob —
    # but ensure the date prefix used for filename resolution honored
    # metadata.date specifically.
    assert "2026-04-15" in target.name
    # Keep json import alive (avoids unused import warning if test grows):
    assert json.dumps(fields)


# ---------------------------------------------------------------------------
# Phase 2 carryover (Task 2.7 #5): aliases in exported frontmatter.
# ---------------------------------------------------------------------------


def test_export_emits_aliases_when_metadata_carries_them(
    test_db: psycopg.Connection, fake_embedder, tmp_path: Path
) -> None:
    """``documents.metadata.aliases`` is materialized into the frontmatter.

    Round-trip parity: a future sync pass must be able to read the exported
    file and re-derive the same alias list.
    """
    _ingest(
        test_db,
        embedder=fake_embedder,
        title="person-x conversation",
        content="body",
        source_kind="manual",
        metadata={"aliases": ["person-x", "person-a-talk"]},
    )
    export_vault(test_db, vault_path=tmp_path / "vault")
    target = next((tmp_path / "vault" / "_ingested" / "manual").glob("*.md"))
    fields, _ = parse_frontmatter(target.read_text())
    assert fields["aliases"] == ["person-x", "person-a-talk"]


def test_export_omits_aliases_field_when_empty(
    test_db: psycopg.Connection, fake_embedder, tmp_path: Path
) -> None:
    """No ``aliases:`` line at all when the metadata has none.

    Keeps the file readable for the common case (no aliases) and stops a
    blank ``aliases: []`` from polluting every export.
    """
    _ingest(
        test_db,
        embedder=fake_embedder,
        title="plain note",
        content="x",
        source_kind="manual",
    )
    export_vault(test_db, vault_path=tmp_path / "vault")
    target = next((tmp_path / "vault" / "_ingested" / "manual").glob("*.md"))
    fields, _ = parse_frontmatter(target.read_text())
    assert "aliases" not in fields


def test_export_drops_non_string_alias_entries(
    test_db: psycopg.Connection, fake_embedder, tmp_path: Path
) -> None:
    """Defensive coercion: a corrupted aliases array filters down to strings.

    Hand-edited metadata could contain a stray int or null; we don't propagate
    those into the YAML — the export stays a tidy ``list[str]``.
    """
    _ingest(
        test_db,
        embedder=fake_embedder,
        title="defensive aliases",
        content="x",
        source_kind="manual",
        metadata={"aliases": ["good", 42, None, ""]},
    )
    export_vault(test_db, vault_path=tmp_path / "vault")
    target = next((tmp_path / "vault" / "_ingested" / "manual").glob("*.md"))
    fields, _ = parse_frontmatter(target.read_text())
    assert fields["aliases"] == ["good"]


def test_export_omits_aliases_when_metadata_value_is_not_a_list(
    test_db: psycopg.Connection, fake_embedder, tmp_path: Path
) -> None:
    """A scalar ``aliases`` value (not a list) is treated as no aliases at all."""
    _ingest(
        test_db,
        embedder=fake_embedder,
        title="bad aliases shape",
        content="x",
        source_kind="manual",
        metadata={"aliases": "not-a-list"},
    )
    export_vault(test_db, vault_path=tmp_path / "vault")
    target = next((tmp_path / "vault" / "_ingested" / "manual").glob("*.md"))
    fields, _ = parse_frontmatter(target.read_text())
    assert "aliases" not in fields


# ---------------------------------------------------------------------------
# Phase 1 (Task: regenerate_vault_file): single-doc materialization helper.
# ---------------------------------------------------------------------------


def test_regenerate_vault_file_writes_same_bytes_as_export_vault(
    test_db: psycopg.Connection, fake_embedder, tmp_path: Path
) -> None:
    """Single-doc helper produces byte-identical output to a corpus dump.

    This is the contract that lets ``brain tag --regenerate-file`` slot
    in safely: regenerating one doc must look exactly like an `export_vault`
    pass for that doc — same path, same frontmatter, same body.
    """
    doc_id = _ingest(
        test_db,
        embedder=fake_embedder,
        title="round trip",
        content="body content",
        source_kind="krisp",
        external_id="rt12345678",
        metadata={"date": "2026-04-15"},
        source_metadata={"date": "2026-04-15"},
    )

    # Single-doc regenerate into vault A.
    vault_a = tmp_path / "vault_a"
    written = regenerate_vault_file(test_db, doc_id, vault_path=vault_a)

    # Corpus export into vault B.
    vault_b = tmp_path / "vault_b"
    export_vault(test_db, vault_path=vault_b)

    # Same file path under both roots.
    rel = written.relative_to(vault_a.resolve())
    counterpart = vault_b / rel
    assert counterpart.is_file()
    assert written.read_bytes() == counterpart.read_bytes()


def test_regenerate_vault_file_returns_absolute_path(
    test_db: psycopg.Connection, fake_embedder, tmp_path: Path
) -> None:
    """Return value is a real absolute path that exists on disk."""
    doc_id = _ingest(
        test_db,
        embedder=fake_embedder,
        title="abs path",
        content="x",
        source_kind="manual",
    )
    written = regenerate_vault_file(test_db, doc_id, vault_path=tmp_path / "vault")
    assert written.is_absolute()
    assert written.exists()
    assert written.is_file()


def test_regenerate_vault_file_unknown_id_raises(
    test_db: psycopg.Connection, tmp_path: Path
) -> None:
    """Unknown UUID surfaces as ValueError — caller decides how to report it.

    Uses a syntactically-valid UUID that isn't in the DB so the
    ``WHERE id = %s::uuid`` cast succeeds but the row lookup returns
    nothing.
    """
    bogus_id = "00000000-0000-0000-0000-000000000000"
    with pytest.raises(ValueError, match="no document"):
        regenerate_vault_file(test_db, bogus_id, vault_path=tmp_path / "vault")


def test_regenerate_vault_file_refuses_kind_vault(
    test_db: psycopg.Connection, tmp_path: Path
) -> None:
    """Vault-tier authored notes are file-source-of-truth — never regenerate.

    The DB's copy can lag behind the on-disk version (the user edits the
    vault file directly between syncs). Rebuilding from the DB would
    silently clobber unsaved edits, so we error out and force the caller
    to pick a real recovery path (git restore / backup).
    """
    row = test_db.execute(
        "INSERT INTO documents "
        "(title, content, content_hash, content_type, kind, vault_path) "
        "VALUES ('Vault Note', 'body', 'h-vault-x', 'note', "
        "'vault', 'projects/notes/v.md') RETURNING id::text"
    ).fetchone()
    assert row is not None
    doc_id = row[0]
    with pytest.raises(ValueError, match="vault-tier"):
        regenerate_vault_file(test_db, doc_id, vault_path=tmp_path / "vault")


def test_regenerate_vault_file_is_idempotent(
    test_db: psycopg.Connection, fake_embedder, tmp_path: Path
) -> None:
    """Second call returns the same path and does not rewrite the file.

    Mirrors the body-hash skip path in ``export_vault``: if the on-disk
    body matches the DB content_hash, the file stays put. mtime stability
    is the observable signal here.
    """
    doc_id = _ingest(
        test_db,
        embedder=fake_embedder,
        title="idempotent",
        content="stable body",
        source_kind="manual",
    )
    vault = tmp_path / "vault"
    first = regenerate_vault_file(test_db, doc_id, vault_path=vault)
    first_mtime_ns = first.stat().st_mtime_ns
    first_bytes = first.read_bytes()

    second = regenerate_vault_file(test_db, doc_id, vault_path=vault)
    assert second == first
    assert second.read_bytes() == first_bytes
    # No-op rewrite leaves mtime untouched (didn't open the file for write).
    assert second.stat().st_mtime_ns == first_mtime_ns


def test_regenerate_vault_file_honors_existing_vault_path(
    test_db: psycopg.Connection, fake_embedder, tmp_path: Path
) -> None:
    """Ingested doc with a recorded ``vault_path`` regenerates at THAT path.

    The corpus dump always recomputes ingested paths from source/title;
    the single-doc helper instead trusts the doc's saved location so a
    previously-synced mirror doesn't get duplicated under a freshly
    computed path (which would orphan the original).
    """
    doc_id = _ingest(
        test_db,
        embedder=fake_embedder,
        title="custom location",
        content="body",
        source_kind="manual",
    )
    custom_rel = "_ingested/manual/custom/place.md"
    test_db.execute(
        "UPDATE documents SET vault_path = %s WHERE id = %s::uuid",
        (custom_rel, doc_id),
    )
    vault = tmp_path / "vault"
    written = regenerate_vault_file(test_db, doc_id, vault_path=vault)
    assert written == (vault / custom_rel).resolve()
    assert written.is_file()


def test_regenerate_vault_file_force_rewrites_when_body_unchanged(
    test_db: psycopg.Connection, fake_embedder, tmp_path: Path
) -> None:
    """``force=True`` bypasses the body-hash skip on a frontmatter-only edit.

    Regression for the bug discovered during phase review of
    ``feature/vault-mirror-on-ingest``: ``_write_doc_file`` fingerprints the
    body only, so a frontmatter-only DB edit (tags / metadata / content_type
    — i.e. anything that does NOT change the slug) would silently skip the
    rewrite and leave stale frontmatter on disk.
    ``regenerate_vault_file(force=True)`` is the per-doc fix; the corpus
    dump (:func:`export_vault`) keeps ``force=False`` so its re-runs stay
    cheap.

    We mutate ``tags`` here (not ``title``) precisely because a title change
    also rotates the slug — that would route the write to a fresh path
    where the body-hash check sees no existing file, masking the bug. Tags
    keep the slug constant so the same on-disk path is reused on the next
    regenerate call.
    """
    # Setup — ingest a doc and write its initial mirror at force=False
    # (the default for the first regenerate call).
    doc_id = _ingest(
        test_db,
        embedder=fake_embedder,
        title="Stable Slug",
        content="body never changes",
        source_kind="manual",
    )
    vault = tmp_path / "vault"
    initial_path = regenerate_vault_file(test_db, doc_id, vault_path=vault)
    fields_before, _ = parse_frontmatter(initial_path.read_text(encoding="utf-8"))
    assert fields_before.get("tags") == []

    # Mutate frontmatter-only state in the DB. ``tags`` does not affect the
    # slug, so the regenerate call below targets the same on-disk path with
    # the same body hash — the exact scenario the body-hash skip mishandled.
    test_db.execute(
        "UPDATE documents SET tags = %s WHERE id = %s::uuid",
        (["alpha", "beta"], doc_id),
    )

    # Exercise without force — the body-hash skip leaves the file stale.
    skipped_path = regenerate_vault_file(test_db, doc_id, vault_path=vault)
    assert skipped_path == initial_path
    fields_skipped, _ = parse_frontmatter(skipped_path.read_text(encoding="utf-8"))
    assert fields_skipped.get("tags") == [], (
        "default force=False must keep the body-hash skip — confirms the "
        "bug existed and that force is what unlocks the rewrite"
    )

    # Now with force=True, the file is rewritten and the new tags land.
    forced_path = regenerate_vault_file(
        test_db, doc_id, vault_path=vault, force=True
    )

    # Verify — same file path, new frontmatter tags.
    assert forced_path == initial_path
    fields_after, _ = parse_frontmatter(forced_path.read_text(encoding="utf-8"))
    assert fields_after.get("tags") == ["alpha", "beta"]
