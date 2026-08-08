"""MCP CRUD parity: ``brain_rm`` / ``brain_note_rename`` / ``brain_note_move`` (F8).

The MCP surface was read-heavy — it could create notes but never delete,
rename or relocate one, so an agent that wanted to reorganize the vault had
to ask the user to run the CLI.

Closing that gap puts destructive verbs in reach of a model, so every one of
them is **default-refuse**: ``confirm`` is ``False`` unless explicitly set,
and a refused call must perform no work while naming exactly what it
declined to touch. The refusal tests below are the load-bearing ones — they
are what stops an agent deleting a note by accident.

Mirrors ``tests/test_mcp_vault_tools.py``: a fresh ``_State`` per test
pointing at the test DB, the fake embedder and a ``tmp_path`` vault, calling
the tool functions directly.

All fixture data is synthetic.
"""
from __future__ import annotations

import inspect
import os
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import psycopg
import pytest

from brain import mcp_server
from brain import vault as vault_module
from brain.config import Config
from brain.mcp_compat import MCPError
from brain.vault.frontmatter import dump_frontmatter
from brain.vault.sync import sync_vault

TEST_DATABASE_URL = os.environ.get(
    "TEST_DATABASE_URL",
    "postgresql://brain:brain@localhost:5434/second_brain_test",
)

NOTE_TITLE = "Weekly Sync Platform"
NOTE_RELATIVE = "inbox/weekly-sync-platform.md"


@pytest.fixture
def vault_dir(tmp_path: Path) -> Path:
    vault = tmp_path / "vault"
    vault_module.init_vault(vault)
    return vault


@pytest.fixture
def mcp_state(
    monkeypatch: pytest.MonkeyPatch,
    test_db: psycopg.Connection,  # noqa: ARG001 — keeps the schema fresh
    fake_embedder: object,
    vault_dir: Path,
) -> Iterator[mcp_server._State]:
    state = mcp_server._State(
        cfg=Config(database_url=TEST_DATABASE_URL, vault_path=vault_dir),
        embedder=fake_embedder,  # type: ignore[arg-type]
    )
    monkeypatch.setattr(mcp_server, "_state", state)
    yield state


def _write(path: Path, fields: dict, body: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(dump_frontmatter(fields, body))


@pytest.fixture
def seeded_note(
    test_db: psycopg.Connection,
    fake_embedder: Any,
    vault_dir: Path,
    mcp_state: mcp_server._State,  # noqa: ARG001 — ordering: state before use
) -> str:
    """One vault-tier note plus a referrer. Returns the note's document id."""
    _write(vault_dir / NOTE_RELATIVE, {"title": NOTE_TITLE}, "primary body\n")
    _write(
        vault_dir / "daily" / "2026-07-14.md",
        {"title": "Daily Entry"},
        f"see [[{NOTE_TITLE}]] for context\n",
    )
    sync_vault(test_db, embedder=fake_embedder, vault_path=vault_dir)
    row = test_db.execute(
        "SELECT id::text FROM documents WHERE title=%s", (NOTE_TITLE,)
    ).fetchone()
    assert row is not None
    return str(row[0])


# ---------------------------------------------------------------------------
# The tools exist at all (red-first before F8)
# ---------------------------------------------------------------------------


def test_delete_and_move_tools_exist() -> None:
    for name in ("brain_rm", "brain_note_rename", "brain_note_move"):
        assert hasattr(mcp_server, name), f"MCP tool {name} is missing"


def test_every_destructive_tool_defaults_confirm_to_false() -> None:
    """The default-refuse contract, asserted structurally.

    A future contributor who adds ``confirm: bool = True`` — or drops the
    parameter — breaks the safety property this whole module exists for,
    and it would otherwise only surface as a deleted note.
    """
    for name in ("brain_rm", "brain_note_rename", "brain_note_move"):
        signature = inspect.signature(getattr(mcp_server, name))
        assert "confirm" in signature.parameters, f"{name} has no confirm gate"
        assert signature.parameters["confirm"].default is False, (
            f"{name}'s confirm parameter must default to False"
        )


# ---------------------------------------------------------------------------
# brain_rm
# ---------------------------------------------------------------------------


def test_rm_refuses_without_confirm_and_deletes_nothing(
    test_db: psycopg.Connection, vault_dir: Path, seeded_note: str
) -> None:
    with pytest.raises(MCPError) as excinfo:
        mcp_server.brain_rm(id=seeded_note)

    message = str(excinfo.value)
    assert "confirm=true" in message
    assert "Nothing was changed" in message
    assert NOTE_TITLE in message, "the refusal must name what it would delete"
    assert NOTE_RELATIVE in message, "and where that document lives on disk"
    assert (vault_dir / NOTE_RELATIVE).is_file(), "the file must survive"
    assert (
        test_db.execute(
            "SELECT 1 FROM documents WHERE id=%s", (seeded_note,)
        ).fetchone()
        is not None
    ), "the row must survive a refused delete"


def test_rm_with_confirm_deletes_row_and_mirror(
    test_db: psycopg.Connection, vault_dir: Path, seeded_note: str
) -> None:
    result = mcp_server.brain_rm(id=seeded_note, confirm=True)

    assert result["document_id"] == seeded_note
    assert result["title"] == NOTE_TITLE
    assert result["vault_path"] == NOTE_RELATIVE
    assert result["mirror_action"] == "unlinked"
    assert not (vault_dir / NOTE_RELATIVE).exists()
    assert (
        test_db.execute(
            "SELECT 1 FROM documents WHERE id=%s", (seeded_note,)
        ).fetchone()
        is None
    )


def test_rm_rejects_an_unknown_id_prefix(mcp_state: mcp_server._State) -> None:
    with pytest.raises(MCPError):
        mcp_server.brain_rm(id="deadbeefdeadbeef", confirm=True)


# ---------------------------------------------------------------------------
# brain_note_rename
# ---------------------------------------------------------------------------


def test_rename_refuses_without_confirm_and_writes_nothing(
    vault_dir: Path, seeded_note: str
) -> None:
    with pytest.raises(MCPError) as excinfo:
        mcp_server.brain_note_rename(id=seeded_note, new_title="Platform Sync")

    message = str(excinfo.value)
    assert "confirm=true" in message
    assert "inbox/platform-sync.md" in message, "name the destination path"
    assert "reference(s)" in message, "and the reference blast radius"
    assert (vault_dir / NOTE_RELATIVE).is_file()
    assert not (vault_dir / "inbox" / "platform-sync.md").exists()


def test_rename_with_confirm_moves_the_file_and_keeps_the_id(
    test_db: psycopg.Connection, vault_dir: Path, seeded_note: str
) -> None:
    result = mcp_server.brain_note_rename(
        id=seeded_note, new_title="Platform Sync", confirm=True
    )

    assert result["document_id"] == seeded_note
    assert result["old_title"] == NOTE_TITLE
    assert result["new_title"] == "Platform Sync"
    assert result["vault_path"] == "inbox/platform-sync.md"
    assert (vault_dir / "inbox" / "platform-sync.md").is_file()
    assert not (vault_dir / NOTE_RELATIVE).exists()
    row = test_db.execute(
        "SELECT title, vault_path FROM documents WHERE id=%s", (seeded_note,)
    ).fetchone()
    assert row == ("Platform Sync", "inbox/platform-sync.md")


def test_rename_rejects_a_non_vault_document(
    test_db: psycopg.Connection, mcp_state: mcp_server._State
) -> None:
    """Ingested-tier docs have no authored file to rename."""
    row = test_db.execute(
        "INSERT INTO documents (title, content, content_type, kind, content_hash) "
        "VALUES ('Imported Transcript', 'body', 'transcript', 'ingested', 'h1') "
        "RETURNING id::text"
    ).fetchone()
    assert row is not None

    with pytest.raises(MCPError, match="not 'vault'"):
        mcp_server.brain_note_rename(
            id=str(row[0]), new_title="Renamed", confirm=True
        )


# ---------------------------------------------------------------------------
# brain_note_move
# ---------------------------------------------------------------------------


def test_move_refuses_without_confirm_and_writes_nothing(
    test_db: psycopg.Connection, vault_dir: Path, seeded_note: str
) -> None:
    with pytest.raises(MCPError) as excinfo:
        mcp_server.brain_note_move(id=seeded_note, new_folder="projects/atlas")

    message = str(excinfo.value)
    assert "confirm=true" in message
    assert "projects/atlas/weekly-sync-platform.md" in message
    assert (vault_dir / NOTE_RELATIVE).is_file()
    assert not (vault_dir / "projects").exists(), (
        "a refused move must not even create the destination folder"
    )
    row = test_db.execute(
        "SELECT vault_path FROM documents WHERE id=%s", (seeded_note,)
    ).fetchone()
    assert row is not None and row[0] == NOTE_RELATIVE


def test_move_with_confirm_relocates_and_preserves_the_id(
    test_db: psycopg.Connection, vault_dir: Path, seeded_note: str
) -> None:
    result = mcp_server.brain_note_move(
        id=seeded_note, new_folder="projects/atlas", confirm=True
    )

    assert result["moved"] is True
    assert result["document_id"] == seeded_note
    assert result["vault_path"] == "projects/atlas/weekly-sync-platform.md"
    assert result["new_title"] == NOTE_TITLE, "a move must not change the title"
    assert (vault_dir / "projects" / "atlas" / "weekly-sync-platform.md").is_file()
    assert not (vault_dir / NOTE_RELATIVE).exists()
    row = test_db.execute(
        "SELECT vault_path FROM documents WHERE id=%s", (seeded_note,)
    ).fetchone()
    assert row is not None
    assert row[0] == "projects/atlas/weekly-sync-platform.md"


def test_move_to_the_same_folder_is_a_reported_noop(
    vault_dir: Path, seeded_note: str
) -> None:
    """No confirm needed and nothing written — an agent can retry safely."""
    result = mcp_server.brain_note_move(id=seeded_note, new_folder="inbox")

    assert result["moved"] is False
    assert result["vault_path"] == NOTE_RELATIVE
    assert "already in inbox" in result["detail"]
    assert (vault_dir / NOTE_RELATIVE).is_file()


def test_move_rejects_a_traversal_folder(
    vault_dir: Path, seeded_note: str
) -> None:
    with pytest.raises(MCPError, match="must stay within the vault"):
        mcp_server.brain_note_move(
            id=seeded_note, new_folder="../../escape", confirm=True
        )

    assert (vault_dir / NOTE_RELATIVE).is_file()


def test_move_refuses_to_overwrite_an_existing_note(
    test_db: psycopg.Connection,
    fake_embedder: Any,
    vault_dir: Path,
    seeded_note: str,
) -> None:
    """There is no ``--force`` equivalent: a collision is a hard failure."""
    occupied = vault_dir / "projects" / "atlas" / "weekly-sync-platform.md"
    _write(occupied, {"title": "Weekly Sync Platform Copy"}, "other body\n")
    sync_vault(test_db, embedder=fake_embedder, vault_path=vault_dir)

    with pytest.raises(MCPError, match="already exists"):
        mcp_server.brain_note_move(
            id=seeded_note, new_folder="projects/atlas", confirm=True
        )

    assert "other body" in occupied.read_text(), "the occupant must be untouched"


def test_move_with_link_refactor_disabled_leaves_references_alone(
    vault_dir: Path, seeded_note: str
) -> None:
    referrer = vault_dir / "daily" / "2026-07-14.md"
    before = referrer.read_text()

    result = mcp_server.brain_note_move(
        id=seeded_note,
        new_folder="projects/atlas",
        confirm=True,
        link_refactor=False,
    )

    assert result["moved"] is True
    assert result["references_rewritten"] == 0
    assert referrer.read_text() == before
