"""Vault sync re-chunks an edited ``code`` mirror with the code chunker (RM-42 F1, BL-35).

``vault/sync.py`` ``_embed_and_insert_chunks`` used to call ``chunk_text``
directly, so editing an ``_ingested/`` mirror of a ``content_type: code``
document replaced its definition-level chunks with prose chunks. RM-43 routes
that call through ``chunk_dispatch.chunk_for``, passing the row's
``content_type`` and metadata (whose ``path`` heads every chunk), and this test
was flipped from pinning the prose behaviour to pinning the code behaviour.
"""
from pathlib import Path
from typing import Any

import psycopg

from brain.ingest import ExtractedDoc, ingest_document
from brain.vault.export import export_vault
from brain.vault.frontmatter import dump_frontmatter, parse_frontmatter
from brain.vault.sync import sync_vault

SOURCE = "import os\n\n\ndef first():\n    return os.sep\n"


def _chunk_heads(conn: psycopg.Connection[Any], document_id: str) -> list[str]:
    rows = conn.execute(
        "SELECT content FROM chunks WHERE document_id=%s ORDER BY chunk_index",
        (document_id,),
    ).fetchall()
    return [str(r[0]).split("\n", 1)[0] for r in rows]


def test_vault_sync_rechunks_an_edited_code_mirror_with_the_code_chunker(
    test_db: psycopg.Connection[Any], fake_embedder: Any, tmp_path: Path
) -> None:
    result = ingest_document(
        test_db,
        embedder=fake_embedder,
        doc=ExtractedDoc(
            title="mod.py",
            content=SOURCE,
            content_type="code",
            source_path=None,
            metadata={"path": "pkg/mod.py"},
        ),
        source_kind="manual",
    )
    document_id = result.document_id
    assert document_id is not None
    assert _chunk_heads(test_db, document_id) == [
        "pkg/mod.py:1-1 <module>",
        "pkg/mod.py:4-5 first",
    ]
    vault = tmp_path / "vault"
    export_vault(test_db, vault_path=vault)
    sync_vault(test_db, embedder=fake_embedder, vault_path=vault)
    row = test_db.execute(
        "SELECT vault_path FROM documents WHERE id=%s", (document_id,)
    ).fetchone()
    assert row is not None and row[0]
    mirror = vault / str(row[0])
    frontmatter, body = parse_frontmatter(mirror.read_text())
    assert frontmatter.get("content_type") == "code"

    mirror.write_text(dump_frontmatter(frontmatter, body + "\n\ndef second():\n    return 2\n"))
    sync_vault(test_db, embedder=fake_embedder, vault_path=vault)

    # BL-35: definition-level chunks, each headed by the metadata path.
    assert _chunk_heads(test_db, document_id) == [
        "pkg/mod.py:1-1 <module>",
        "pkg/mod.py:4-5 first",
        "pkg/mod.py:8-9 second",
    ]


# --- RM-43: a code body materializes no wiki links ---------------------------------------

NOTE_TITLE = "Gadget Notes"
# The link is on a top-level line: an indented ``[[x]]`` is already skipped as an
# indented code block by ``vault.links``, so it would not exercise the ``code`` skip.
LINKING_CODE = f"# see [[{NOTE_TITLE}]]\ndef gadget():\n    return 1\n"
LINKING_PROSE = f"A synthetic note that points at [[{NOTE_TITLE}]].\n"


def _ingest(conn: psycopg.Connection[Any], embedder: Any, *, content: str,
            content_type: str, path: str) -> str:
    result = ingest_document(
        conn,
        embedder=embedder,
        doc=ExtractedDoc(
            title=path, content=content, content_type=content_type, source_path=None,
            metadata={"path": path},
        ),
        source_kind="manual",
        source_external_id=path,
    )
    assert result.document_id is not None
    return result.document_id


def _vault_with_note(conn: psycopg.Connection[Any], tmp_path: Path) -> tuple[Path, str]:
    """Export the mirrors, add a vault note titled :data:`NOTE_TITLE`, return its id."""
    vault = tmp_path / "vault"
    export_vault(conn, vault_path=vault)
    (vault / "gadget-notes.md").write_text(
        dump_frontmatter({"title": NOTE_TITLE}, "A synthetic target note.\n")
    )
    return vault, NOTE_TITLE


def _link_rows(conn: psycopg.Connection[Any], document_id: str) -> tuple[int, int]:
    resolved = conn.execute(
        "SELECT count(*) FROM links WHERE src_document_id = %s", (document_id,)
    ).fetchone()
    unresolved = conn.execute(
        "SELECT count(*) FROM unresolved_links WHERE src_document_id = %s", (document_id,)
    ).fetchone()
    assert resolved is not None and unresolved is not None
    return int(resolved[0]), int(unresolved[0])


def test_a_wiki_link_in_a_code_mirror_creates_no_link_even_to_an_existing_note(
    test_db: psycopg.Connection[Any], fake_embedder: Any, tmp_path: Path
) -> None:
    code_id = _ingest(test_db, fake_embedder, content=LINKING_CODE, content_type="code",
                      path="pkg/gadget.py")
    vault, _ = _vault_with_note(test_db, tmp_path)

    report = sync_vault(test_db, embedder=fake_embedder, vault_path=vault)
    dry = sync_vault(test_db, embedder=fake_embedder, vault_path=vault, dry_run=True)

    assert _link_rows(test_db, code_id) == (0, 0)
    assert (report.links_resolved, report.links_unresolved) == (0, 0)
    assert (dry.links_resolved, dry.links_unresolved) == (0, 0)


def test_a_prose_mirror_still_materializes_the_same_link(
    test_db: psycopg.Connection[Any], fake_embedder: Any, tmp_path: Path
) -> None:
    """The control: the skip is keyed on ``code``, not on mirrors in general."""
    prose_id = _ingest(test_db, fake_embedder, content=LINKING_PROSE, content_type="txt",
                       path="notes/pointer.txt")
    vault, _ = _vault_with_note(test_db, tmp_path)

    report = sync_vault(test_db, embedder=fake_embedder, vault_path=vault)
    dry = sync_vault(test_db, embedder=fake_embedder, vault_path=vault, dry_run=True)

    assert _link_rows(test_db, prose_id) == (1, 0)
    assert report.links_resolved == 1
    assert dry.links_resolved == 1


def test_a_resync_clears_link_rows_a_code_document_already_had(
    test_db: psycopg.Connection[Any], fake_embedder: Any, tmp_path: Path
) -> None:
    code_id = _ingest(test_db, fake_embedder, content=LINKING_CODE, content_type="code",
                      path="pkg/gadget.py")
    vault, _ = _vault_with_note(test_db, tmp_path)
    sync_vault(test_db, embedder=fake_embedder, vault_path=vault)
    note = test_db.execute("SELECT id FROM documents WHERE title = %s", (NOTE_TITLE,)).fetchone()
    assert note is not None
    test_db.execute(
        "INSERT INTO links (src_document_id, dst_document_id, link_text, link_kind) "
        "VALUES (%s, %s, %s, 'wiki')",
        (code_id, note[0], NOTE_TITLE),
    )
    test_db.execute(
        "INSERT INTO unresolved_links (src_document_id, link_text, link_kind) "
        "VALUES (%s, 'Nowhere', 'wiki')",
        (code_id,),
    )

    sync_vault(test_db, embedder=fake_embedder, vault_path=vault)

    assert _link_rows(test_db, code_id) == (0, 0)
