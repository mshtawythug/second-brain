"""Tests for :mod:`brain.capture_import` (RM-39): a shared text file becomes an inbox note.

The parser is pure; ``import_file`` runs against the 5434 test database and a
``tmp_path`` vault with the deterministic fake embedder. Every share is synthetic.
"""
from __future__ import annotations

import hashlib
import os
from pathlib import Path
from typing import Any

import psycopg
import pytest

from brain import vault as vault_module
from brain.capture_import import (
    CAPTURE_FOLDER,
    KEY_FIELD,
    MAX_FRONTMATTER_LINES,
    MAX_SHARE_BYTES,
    CaptureContext,
    Share,
    escalate,
    import_file,
    importer,
    parse_share,
    share_key,
)
from brain.config import Config
from brain.drop_folder import DropFolderError, MoveRaceError, RejectedFileError
from brain.errors import VaultNoteSyncError
from brain.vault.frontmatter import parse_frontmatter

URL = "https://example.com/articles/synthetic-widgets"
SHARE = f"Worth reading for the widget pricing idea\n\n{URL}\n"


# ---------------------------------------------------------------------------
# parse_share: line-based, no YAML, only ``sensitivity`` honoured
# ---------------------------------------------------------------------------


def test_a_share_with_no_frontmatter_is_all_body() -> None:
    assert parse_share(SHARE) == Share(body=SHARE.strip(), sensitivity=None)


def test_the_sensitivity_line_is_honoured_and_stripped_from_the_body() -> None:
    text = f"---\nsensitivity: confidential\n---\n{SHARE}"

    assert parse_share(text) == Share(body=SHARE.strip(), sensitivity="confidential")


@pytest.mark.parametrize(
    "line",
    [
        "sensitivity: normal",
        "sensitivity:normal",
        '  sensitivity : "Normal"  ',
        "sensitivity: 'normal'",
    ],
)
def test_a_normal_line_in_any_spelling_reads_normal(line: str) -> None:
    assert parse_share(f"---\n{line}\n---\nbody").sensitivity == "normal"


@pytest.mark.parametrize("value", ["secret", "", "confidential # yes", "[confidential]"])
def test_an_invalid_level_fails_closed_to_confidential(value: str) -> None:
    assert parse_share(f"---\nsensitivity: {value}\n---\nbody").sensitivity == "confidential"


def test_two_sensitivity_lines_take_the_stricter() -> None:
    text = "---\nsensitivity: confidential\nsensitivity: normal\n---\nbody"

    assert parse_share(text).sensitivity == "confidential"


def test_every_other_key_is_dropped() -> None:
    """``allow_secrets``, ``id``, ``kind``, ``tags``, a YAML anchor: none is honoured,
    none reaches the body, and no YAML parser ever sees them."""
    text = (
        "---\nallow_secrets: true\nid: 00000000-0000-0000-0000-000000000000\nkind: ingested\n"
        "tags: [forged]\nbomb: &a [*a, *a]\n!!python/object:os.system ['true']\n---\nbody\n"
    )

    assert parse_share(text) == Share(body="body", sensitivity=None)


def test_an_unclosed_block_is_all_body_and_confidential() -> None:
    text = f"---\nsensitivity: normal\n{SHARE}"

    assert parse_share(text) == Share(body=text.strip(), sensitivity="confidential")


def test_a_block_closing_past_the_line_limit_is_unclosed() -> None:
    filler = "note: x\n" * MAX_FRONTMATTER_LINES
    text = f"---\n{filler}---\nbody"

    assert parse_share(text) == Share(body=text.strip(), sensitivity="confidential")


def test_a_block_at_the_line_limit_still_closes() -> None:
    filler = "note: x\n" * (MAX_FRONTMATTER_LINES - 1)

    assert parse_share(f"---\n{filler}---\nbody") == Share(body="body", sensitivity=None)


def test_a_block_over_the_byte_limit_is_unclosed() -> None:
    text = f"---\nnote: {'x' * 5000}\n---\nbody"

    assert parse_share(text).sensitivity == "confidential"


def test_text_only_inside_a_closed_block_is_the_body_never_lost() -> None:
    """rev-111 3b: a share whose only text sits inside a closed ``---`` block is not
    empty (and deleted): the whole text is the body. Its ``sensitivity`` still counts."""
    text = "---\nsensitivity: confidential\nmy only thought\n---\n\n"

    assert parse_share(text) == Share(body=text.strip(), sensitivity="confidential")


@pytest.mark.parametrize("text", ["---\nsensitivity: confidential\n---\n", "---\n\n  \n---\n"])
def test_a_block_with_nothing_but_sensitivity_or_blanks_is_still_empty(text: str) -> None:
    assert parse_share(text).body == ""


def test_crlf_and_a_bom_are_read_like_plain_newlines() -> None:
    text = "﻿---\r\nsensitivity: confidential\r\n---\r\nline one\r\nline two\r\n"

    assert parse_share(text) == Share(body="line one\nline two", sensitivity="confidential")


def test_a_rule_later_in_the_text_is_not_frontmatter() -> None:
    text = "intro\n---\nsensitivity: confidential\n---\nrest"

    assert parse_share(text) == Share(body=text, sensitivity=None)


@pytest.mark.parametrize(
    ("folder", "share", "expected"),
    [
        ("normal", None, "normal"),
        ("normal", "normal", "normal"),
        ("normal", "confidential", "confidential"),
        ("confidential", None, "confidential"),
        ("confidential", "normal", "confidential"),
    ],
)
def test_sensitivity_only_ever_escalates(folder: str, share: str | None, expected: str) -> None:
    assert escalate(folder, share) == expected


def test_the_key_binds_the_name_and_the_bytes() -> None:
    assert share_key("a", b"x") == hashlib.sha256(b"a\x00x").hexdigest()
    assert share_key("a", b"x") != share_key("b", b"x")
    assert share_key("a", b"x") != share_key("a", b"y")


# ---------------------------------------------------------------------------
# import_file against the database
# ---------------------------------------------------------------------------


@pytest.fixture
def vault(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    root = tmp_path / "vault"
    vault_module.init_vault(root)
    monkeypatch.setenv("BRAIN_VAULT_PATH", str(root))
    return root


def _ctx(embedder: Any, sensitivity: str = "normal", folder: Path | None = None) -> CaptureContext:
    cfg = Config.load()
    where = folder if folder is not None else cfg.vault_path.parent / "share-folder"
    return CaptureContext(cfg=cfg, embedder=embedder, sensitivity=sensitivity, folder=where)


def _share(tmp_path: Path, text: str | bytes, name: str = "share-1a2b3c4d.md") -> Path:
    path = tmp_path / name
    if isinstance(text, str):
        path.write_text(text, encoding="utf-8")
    else:
        path.write_bytes(text)
    return path


def _notes(conn: psycopg.Connection[Any]) -> list[dict[str, Any]]:
    rows = conn.execute(
        "SELECT id::text, title, kind, tags, sensitivity, vault_path, metadata, content "
        "FROM documents ORDER BY ingested_at"
    ).fetchall()
    keys = ("id", "title", "kind", "tags", "sensitivity", "vault_path", "metadata", "content")
    return [dict(zip(keys, row, strict=True)) for row in rows]


def _note_files(vault: Path) -> list[Path]:
    """The notes under ``capture/`` (``init_vault`` puts a README there)."""
    return sorted(p for p in (vault / CAPTURE_FOLDER).glob("*.md") if p.name != "README.md")


def _import(conn: psycopg.Connection[Any], path: Path, ctx: CaptureContext) -> str:
    return import_file(conn, path, title="share", timeout=30.0, ctx=ctx)


def test_a_share_becomes_an_inbox_note_with_the_comment_and_the_url(
    test_db: psycopg.Connection[Any], tmp_path: Path, vault: Path, fake_embedder: Any
) -> None:
    status = _import(test_db, _share(tmp_path, SHARE), _ctx(fake_embedder))

    assert status == "ingested"
    (note,) = _notes(test_db)
    assert note["kind"] == "vault"
    assert note["tags"] == ["inbox"]
    assert note["sensitivity"] == "normal"
    assert note["vault_path"].startswith(f"{CAPTURE_FOLDER}/")
    assert note["title"].endswith("-capture-worth-reading-for-the-widget-pricing")
    assert "widget pricing idea" in note["content"] and URL in note["content"]
    raw = SHARE.encode()
    assert note["metadata"][KEY_FIELD] == share_key("share", raw)
    fields, body = parse_frontmatter((vault / note["vault_path"]).read_text(encoding="utf-8"))
    assert "sensitivity" not in fields
    assert body.strip() == SHARE.strip()


def test_a_confidential_share_becomes_a_confidential_note(
    test_db: psycopg.Connection[Any], tmp_path: Path, vault: Path, fake_embedder: Any
) -> None:
    path = _share(tmp_path, f"---\nsensitivity: confidential\n---\n{SHARE}")

    _import(test_db, path, _ctx(fake_embedder))

    (note,) = _notes(test_db)
    assert note["sensitivity"] == "confidential"
    fields, _ = parse_frontmatter((vault / note["vault_path"]).read_text(encoding="utf-8"))
    assert fields["sensitivity"] == "confidential"


def test_a_confidential_folder_makes_a_confidential_note(
    test_db: psycopg.Connection[Any], tmp_path: Path, vault: Path, fake_embedder: Any
) -> None:
    path = _share(tmp_path, f"---\nsensitivity: normal\n---\n{SHARE}")

    _import(test_db, path, _ctx(fake_embedder, sensitivity="confidential"))

    assert _notes(test_db)[0]["sensitivity"] == "confidential"


def test_the_same_share_imported_twice_is_one_note(
    test_db: psycopg.Connection[Any], tmp_path: Path, vault: Path, fake_embedder: Any
) -> None:
    ctx = _ctx(fake_embedder)
    path = _share(tmp_path, SHARE)

    first = _import(test_db, path, ctx)
    second = _import(test_db, path, ctx)

    assert (first, second) == ("ingested", "unchanged")  # the runner reports it ``duplicate``
    assert len(_notes(test_db)) == 1
    assert len(_note_files(vault)) == 1


def test_two_files_with_one_name_and_different_bytes_are_two_notes(
    test_db: psycopg.Connection[Any], tmp_path: Path, vault: Path, fake_embedder: Any
) -> None:
    ctx = _ctx(fake_embedder)

    _import(test_db, _share(tmp_path, "first share", name="a.md"), ctx)
    _import(test_db, _share(tmp_path, "second share", name="b.md"), ctx)

    assert len(_notes(test_db)) == 2


def test_an_ingested_document_carrying_the_key_does_not_count(
    test_db: psycopg.Connection[Any], tmp_path: Path, vault: Path, fake_embedder: Any
) -> None:
    """Only a vault-tier note proves the share was captured."""
    path = _share(tmp_path, SHARE)
    key = share_key("share", SHARE.encode())
    test_db.execute(
        "INSERT INTO documents (title, content, content_hash, content_type, kind, metadata) "
        "VALUES ('x', 'x', 'h-rm39', 'note', 'ingested', jsonb_build_object(%s::text, %s::text))",
        (KEY_FIELD, key),
    )

    assert _import(test_db, path, _ctx(fake_embedder)) == "ingested"


@pytest.mark.parametrize("text", ["", "   \n\n", "---\nsensitivity: confidential\n---\n  \n"])
def test_an_empty_share_is_empty_and_writes_nothing(
    test_db: psycopg.Connection[Any], tmp_path: Path, vault: Path, fake_embedder: Any, text: str
) -> None:
    assert _import(test_db, _share(tmp_path, text), _ctx(fake_embedder)) == "empty"
    assert _notes(test_db) == []
    assert _note_files(vault) == []


def test_a_nul_byte_is_dropped(
    test_db: psycopg.Connection[Any], tmp_path: Path, vault: Path, fake_embedder: Any
) -> None:
    _import(test_db, _share(tmp_path, b"before\x00after"), _ctx(fake_embedder))

    assert _notes(test_db)[0]["content"].strip() == "beforeafter"


def test_a_share_over_the_size_cap_is_rejected_and_writes_nothing(
    test_db: psycopg.Connection[Any], tmp_path: Path, vault: Path, fake_embedder: Any
) -> None:
    path = _share(tmp_path, b"x" * (MAX_SHARE_BYTES + 1))

    with pytest.raises(RejectedFileError):
        _import(test_db, path, _ctx(fake_embedder))
    assert _notes(test_db) == []


def test_a_share_at_the_size_cap_is_imported(
    test_db: psycopg.Connection[Any], tmp_path: Path, vault: Path, fake_embedder: Any
) -> None:
    path = _share(tmp_path, b"word " * (MAX_SHARE_BYTES // 5))

    assert _import(test_db, path, _ctx(fake_embedder)) == "ingested"


def test_a_share_that_is_not_utf8_is_rejected(
    test_db: psycopg.Connection[Any], tmp_path: Path, vault: Path, fake_embedder: Any
) -> None:
    with pytest.raises(RejectedFileError) as err:
        _import(test_db, _share(tmp_path, b"\xff\xfe binary"), _ctx(fake_embedder))

    assert isinstance(err.value.__cause__, UnicodeDecodeError)


def test_a_symlink_at_the_path_is_refused(
    test_db: psycopg.Connection[Any], tmp_path: Path, vault: Path, fake_embedder: Any
) -> None:
    target = _share(tmp_path, SHARE, name="target.md")
    link = tmp_path / "link.md"
    link.symlink_to(target)

    with pytest.raises(OSError):
        _import(test_db, link, _ctx(fake_embedder))
    assert _notes(test_db) == []


def test_a_fifo_at_the_path_is_refused(
    test_db: psycopg.Connection[Any], tmp_path: Path, vault: Path, fake_embedder: Any
) -> None:
    fifo = tmp_path / "pipe.md"
    os.mkfifo(fifo)

    with pytest.raises(MoveRaceError):
        _import(test_db, fifo, _ctx(fake_embedder))


def test_a_vault_without_a_template_is_transient_and_writes_nothing(
    test_db: psycopg.Connection[Any], tmp_path: Path, fake_embedder: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    bare = tmp_path / "bare-vault"
    bare.mkdir()
    monkeypatch.setenv("BRAIN_VAULT_PATH", str(bare))

    with pytest.raises(VaultNoteSyncError):
        _import(test_db, _share(tmp_path, SHARE), _ctx(fake_embedder))
    assert list(bare.iterdir()) == []


def test_a_non_autocommit_connection_is_refused_before_writing(
    tmp_path: Path, vault: Path, fake_embedder: Any, test_db: psycopg.Connection[Any]
) -> None:
    with psycopg.connect(os.environ["DATABASE_URL"]) as conn, pytest.raises(VaultNoteSyncError):
        _import(conn, _share(tmp_path, SHARE), _ctx(fake_embedder))
    assert _note_files(vault) == []


class _BrokenEmbedder:
    """An embedder whose service is down: the note is written, then indexing fails."""

    def __init__(self, inner: Any) -> None:
        self.dim = inner.dim
        self.count_tokens = inner.count_tokens

    def embed(self, texts: list[str], *, input_type: str) -> list[list[float]]:
        raise ConnectionError("embedder down")


def test_an_index_failure_after_the_write_is_written_not_retried(
    test_db: psycopg.Connection[Any], tmp_path: Path, vault: Path, fake_embedder: Any,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """The content is safe in the vault once the file is written, so the share is a
    success (``written``) and never moved back to author a second note."""
    status = _import(test_db, _share(tmp_path, SHARE), _ctx(_BrokenEmbedder(fake_embedder)))

    assert status == "written"
    (note_file,) = _note_files(vault)
    assert URL in note_file.read_text(encoding="utf-8")
    assert "ConnectionError" in caplog.text
    assert "widget" not in caplog.text  # class only, never content


def test_a_share_hidden_in_its_frontmatter_becomes_a_note(
    test_db: psycopg.Connection[Any], tmp_path: Path, vault: Path, fake_embedder: Any
) -> None:
    path = _share(tmp_path, f"---\n{URL}\n---\n")

    assert _import(test_db, path, _ctx(fake_embedder)) == "ingested"
    assert URL in _notes(test_db)[0]["content"]


@pytest.mark.parametrize("where", ["vault", "inside", "contains"])
def test_a_capture_folder_overlapping_the_vault_is_refused(
    tmp_path: Path, vault: Path, fake_embedder: Any, where: str
) -> None:
    """rev-111 M1: a capture folder that is the vault, lies inside it, or holds it would
    consume the vault's own notes (re-created, the originals deleted), every 10 minutes."""
    folder = {"vault": vault, "inside": vault / CAPTURE_FOLDER, "contains": vault.parent}[where]

    with pytest.raises(DropFolderError, match="vault"):
        importer(_ctx(fake_embedder, folder=folder))


def test_a_differently_spelled_path_to_the_vault_is_refused(
    tmp_path: Path, vault: Path, fake_embedder: Any
) -> None:
    alias = tmp_path / "vault-alias"
    alias.symlink_to(vault)

    with pytest.raises(DropFolderError):
        importer(_ctx(fake_embedder, folder=alias / CAPTURE_FOLDER))


def test_a_sibling_whose_name_starts_like_the_vault_is_allowed(
    tmp_path: Path, vault: Path, fake_embedder: Any
) -> None:
    sibling = tmp_path / "vault-shares"
    sibling.mkdir()

    importer(_ctx(fake_embedder, folder=sibling))


def test_the_importer_calls_import_file(
    test_db: psycopg.Connection[Any], tmp_path: Path, vault: Path, fake_embedder: Any
) -> None:
    ingest = importer(_ctx(fake_embedder))

    assert ingest(test_db, _share(tmp_path, SHARE), title="share", timeout=1.0) == "ingested"
