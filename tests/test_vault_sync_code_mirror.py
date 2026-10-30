"""Pins TODAY's behaviour: vault sync re-chunks an edited ``code`` mirror as prose (RM-42 F1).

``vault/sync.py`` ``_embed_and_insert_chunks`` still calls ``chunk_text``
directly, so when an ``_ingested/`` mirror of a ``content_type: code`` document
is edited, sync replaces its definition-level chunks with prose chunks. RM-42
left that alone on purpose: ``sync.py`` is over the size ceiling and nothing
produces ``code`` until RM-43. RM-43 must route that call through
``chunk_dispatch.chunk_for`` (or keep code documents out of mirror re-chunking),
and then flip the final assertion here. See
``docs/plans/2026-10-30-rm-42-code-chunker.md``, "Carry-forward to RM-43".
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


def test_vault_sync_rechunks_an_edited_code_mirror_with_the_prose_chunker(
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

    # Today: prose chunks, no ``path:start-end symbol`` header. RM-43 flips this.
    heads = _chunk_heads(test_db, document_id)
    assert heads
    assert not any(head.startswith("pkg/mod.py:") for head in heads)
    assert heads[0] == "import os"
