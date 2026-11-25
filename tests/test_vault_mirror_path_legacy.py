"""Per-mechanism proof that F15 never moves, rewrites or duplicates a legacy mirror.

Each test seeds a ``tmp_path`` vault with an old-rule mirror (an absolute-path
external id, so the file sits under stray nested directories), drives ONE code
path that can write mirrors, and asserts all three of: the stored
``documents.vault_path`` is unchanged, the file's bytes are unchanged, and no
second copy of the document exists anywhere under ``_ingested/``.

The bulk CLI commands are covered by ``test_vault_mirror_path_bulk.py``; this file
covers the library paths underneath them and the ingest pipeline.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any

import psycopg
import pytest

from brain.ingest import ExtractedDoc, ingest_document
from brain.vault import init_vault
from brain.vault.derived_links.fence import rewrite_derived_fences
from brain.vault.export import export_vault, regenerate_vault_file
from brain.vault.frontmatter import parse_frontmatter
from brain.vault.links import parse_wiki_links
from brain.vault.paths import strip_md_extension
from brain.vault.resolver import resolve_link
from brain.vault.sync import sync_one_file, sync_vault
from tests.mirror_legacy_fixtures import (
    DATE,
    LegacyDoc,
    legacy_path,
    mirrors_of,
    seed_file_doc,
    seed_stdin_doc,
)

_SRC = "/srv/synthetic/repo/src/widget.py"
_TITLE = "sample-repo/src/widget.py"


@pytest.fixture
def vault(tmp_path: Path) -> Path:
    path = tmp_path / "vault"
    init_vault(path)
    return path


@pytest.fixture
def legacy(test_db: psycopg.Connection[Any], fake_embedder: Any, vault: Path) -> LegacyDoc:
    return seed_file_doc(
        test_db, fake_embedder, vault,
        source_kind="code", source_path=_SRC, title=_TITLE, content_type="code",
    )


def _stored(conn: psycopg.Connection[Any], doc_id: str) -> str | None:
    row = conn.execute("SELECT vault_path FROM documents WHERE id = %s", (doc_id,)).fetchone()
    assert row is not None
    return row[0]


def _assert_untouched(
    conn: psycopg.Connection[Any], vault: Path, doc: LegacyDoc, before: bytes
) -> None:
    assert _stored(conn, doc.doc_id) == doc.legacy_path
    assert (vault / doc.legacy_path).read_bytes() == before
    assert mirrors_of(vault, doc.doc_id) == [doc.legacy_path]


def _reingest(
    conn: psycopg.Connection[Any], embedder: Any, vault: Path, *, body: str, force: bool
) -> Any:
    return ingest_document(
        conn,
        embedder=embedder,
        doc=ExtractedDoc(
            title=_TITLE,
            content=body,
            content_type="code",
            source_path=_SRC,
            metadata={"date": DATE, "path": "src/widget.py"},
        ),
        source_kind="code",
        vault_root=vault,
        force=force,
    )


def test_export_vault_leaves_the_legacy_mirror_alone(
    test_db: psycopg.Connection[Any], vault: Path, legacy: LegacyDoc
) -> None:
    before = (vault / legacy.legacy_path).read_bytes()

    export_vault(test_db, vault_path=vault)
    export_vault(test_db, vault_path=vault, force=True)

    _assert_untouched(test_db, vault, legacy, before)


def test_regenerate_vault_file_writes_the_legacy_path_in_place(
    test_db: psycopg.Connection[Any], vault: Path, legacy: LegacyDoc
) -> None:
    before = (vault / legacy.legacy_path).read_bytes()

    written = regenerate_vault_file(test_db, legacy.doc_id, vault_path=vault, force=True)

    assert written == (vault / legacy.legacy_path).resolve()
    _assert_untouched(test_db, vault, legacy, before)


def test_vault_sync_and_the_watcher_upsert_keep_the_legacy_path(
    test_db: psycopg.Connection[Any], fake_embedder: Any, vault: Path, legacy: LegacyDoc
) -> None:
    before = (vault / legacy.legacy_path).read_bytes()

    sync_vault(test_db, embedder=fake_embedder, vault_path=vault, prune=True)
    sync_one_file(
        test_db, embedder=fake_embedder, vault_path=vault, file_path=vault / legacy.legacy_path
    )

    _assert_untouched(test_db, vault, legacy, before)


def test_the_derived_fence_rewrite_leaves_the_legacy_mirror_alone(
    test_db: psycopg.Connection[Any], vault: Path, legacy: LegacyDoc
) -> None:
    before = (vault / legacy.legacy_path).read_bytes()

    rewrite_derived_fences(test_db, {legacy.doc_id}, vault_path=vault)

    _assert_untouched(test_db, vault, legacy, before)


def test_reingest_without_force_is_a_skip(
    test_db: psycopg.Connection[Any], fake_embedder: Any, vault: Path, legacy: LegacyDoc
) -> None:
    body = test_db.execute(
        "SELECT content FROM documents WHERE id = %s", (legacy.doc_id,)
    ).fetchone()
    assert body is not None
    before = (vault / legacy.legacy_path).read_bytes()

    result = _reingest(test_db, fake_embedder, vault, body=str(body[0]), force=False)

    assert (result.created, result.body_changed) == (False, False)
    _assert_untouched(test_db, vault, legacy, before)


def test_reingest_with_force_rewrites_the_legacy_path_in_place(
    test_db: psycopg.Connection[Any], fake_embedder: Any, vault: Path, legacy: LegacyDoc
) -> None:
    body = test_db.execute(
        "SELECT content FROM documents WHERE id = %s", (legacy.doc_id,)
    ).fetchone()
    assert body is not None
    before = (vault / legacy.legacy_path).read_bytes()

    result = _reingest(test_db, fake_embedder, vault, body=str(body[0]), force=True)

    # A forced update re-stamps ``ingested_at`` (pre-existing, by design), which the
    # mirror carries as ``created``/``updated``. Everything else is byte-identical,
    # and the file is rewritten IN PLACE: same path, still the only copy.
    assert result.document_id == legacy.doc_id
    assert _stored(test_db, legacy.doc_id) == legacy.legacy_path
    assert mirrors_of(vault, legacy.doc_id) == [legacy.legacy_path]
    old_fields, old_body = parse_frontmatter(before.decode("utf-8"))
    new_fields, new_body = parse_frontmatter((vault / legacy.legacy_path).read_text())
    assert new_body == old_body
    restamped = {"created", "updated"}
    assert {k: v for k, v in new_fields.items() if k not in restamped} == {
        k: v for k, v in old_fields.items() if k not in restamped
    }


def test_reingest_with_a_changed_body_keeps_the_legacy_path(
    test_db: psycopg.Connection[Any], fake_embedder: Any, vault: Path, legacy: LegacyDoc
) -> None:
    new_body = "def widget_path():\n    return 'changed in a synthetic edit'\n" * 3

    result = _reingest(test_db, fake_embedder, vault, body=new_body, force=False)

    assert (result.document_id, result.body_changed) == (legacy.doc_id, True)
    assert _stored(test_db, legacy.doc_id) == legacy.legacy_path
    assert mirrors_of(vault, legacy.doc_id) == [legacy.legacy_path]
    assert "changed in a synthetic edit" in (vault / legacy.legacy_path).read_text()


# --- condition (e): a legacy file on disk whose row never recorded its path --------------


def test_an_unrecorded_legacy_file_is_reused_by_export_and_by_mirror_repair(
    test_db: psycopg.Connection[Any], fake_embedder: Any, vault: Path, legacy: LegacyDoc
) -> None:
    test_db.execute("UPDATE documents SET vault_path = NULL WHERE id = %s", (legacy.doc_id,))
    before = (vault / legacy.legacy_path).read_bytes()

    export_vault(test_db, vault_path=vault)
    assert mirrors_of(vault, legacy.doc_id) == [legacy.legacy_path]
    assert (vault / legacy.legacy_path).read_bytes() == before

    body = test_db.execute(
        "SELECT content FROM documents WHERE id = %s", (legacy.doc_id,)
    ).fetchone()
    assert body is not None
    result = _reingest(test_db, fake_embedder, vault, body=str(body[0]), force=False)

    assert result.mirror_repaired is True
    _assert_untouched(test_db, vault, legacy, before)


# --- the --force replace branch (stdin content-hash fallback) ---------------------------


def test_a_force_replace_overwrites_the_legacy_file_in_place_as_before(
    test_db: psycopg.Connection[Any], fake_embedder: Any, vault: Path
) -> None:
    body = "Synthetic replace body: the release checklist and the widget backlog.\n" * 3
    old = seed_stdin_doc(
        test_db, fake_embedder, vault,
        source_kind="krisp", external_id="/srv/synthetic/one", title="Replace call", body=body,
    )
    # Same content, a different id whose old 8-char segment is the same ("/srv/syn"
    # after the dash strip): the pipeline deletes the old row, inserts a new uuid.
    result = ingest_document(
        test_db,
        embedder=fake_embedder,
        doc=ExtractedDoc(
            title="Replace call", content=body, content_type="transcript",
            source_path=None, metadata={"date": DATE},
        ),
        source_kind="krisp",
        source_external_id="/srv/synthetic/two",
        vault_root=vault,
        force=True,
    )
    assert legacy_path("krisp", "/srv/synthetic/two", "Replace call") == old.legacy_path
    assert result.replaced_document_id == old.doc_id
    new_id = result.document_id
    assert new_id is not None and new_id != old.doc_id

    files = sorted(p.relative_to(vault).as_posix() for p in (vault / "_ingested").rglob("*.md"))
    assert [f for f in files if f.startswith("_ingested/krisp/")] == [old.legacy_path]
    assert mirrors_of(vault, new_id) == [old.legacy_path]
    assert _stored(test_db, new_id) == old.legacy_path


# --- wikilinks: old and new paths both resolve ------------------------------------------


def test_wikilinks_to_a_legacy_mirror_and_a_new_mirror_both_resolve(
    test_db: psycopg.Connection[Any], fake_embedder: Any, vault: Path, legacy: LegacyDoc
) -> None:
    before = (vault / legacy.legacy_path).read_bytes()
    fresh = ingest_document(
        test_db,
        embedder=fake_embedder,
        doc=ExtractedDoc(
            title="sample-repo/src/gadget.py",
            content="def gadget():\n    return 'a brand-new synthetic file'\n" * 3,
            content_type="code",
            source_path="/srv/synthetic/repo/src/gadget.py",
            metadata={"date": DATE, "path": "src/gadget.py"},
        ),
        source_kind="code",
        vault_root=vault,
    )
    assert fresh.document_id is not None
    fresh_path = _stored(test_db, fresh.document_id)
    assert fresh_path is not None and fresh_path.count("/") == 2

    body = (
        f"See [[{strip_md_extension(legacy.legacy_path)}]] "
        f"and [[{strip_md_extension(fresh_path)}]].\n"
    )
    resolved = [resolve_link(test_db, link) for link in parse_wiki_links(body)]
    targets = [t.document_id if t is not None else None for t in resolved]

    assert targets == [legacy.doc_id, fresh.document_id]
    _assert_untouched(test_db, vault, legacy, before)
