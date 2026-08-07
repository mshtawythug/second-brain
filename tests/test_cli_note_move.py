"""``brain note move`` — the CLI surface of F8.

A move is a rename to the *same* title in a different folder, so it reuses
the whole rename machinery. What is genuinely new, and what these tests
pin, is the command's own contract:

- ``--dry-run`` prints the plan and writes nothing;
- the move is confirmed by default (a move's blast radius — path-form
  ``[[…]]`` links across the entire vault — is not visible from the command
  line the way a rename's is), skippable with ``--yes``;
- ``documents.id`` survives, so **incoming backlinks survive** — the
  regression that motivated the whole feature;
- a collision is a hard failure with no ``--force`` and no overwrite;
- a traversal folder is a usage error, not a library traceback.

All fixture data is synthetic.
"""
from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import Any

import psycopg
import pytest
from typer.testing import CliRunner

from brain import vault as vault_module
from brain.cli import app
from brain.vault.frontmatter import dump_frontmatter
from brain.vault.sync import sync_vault

NOTE_TITLE = "Weekly Sync Platform"
NOTE_RELATIVE = "inbox/weekly-sync-platform.md"
MOVED_RELATIVE = "projects/atlas/weekly-sync-platform.md"


def _write(path: Path, fields: dict, body: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(dump_frontmatter(fields, body))


def _seed(
    test_db: psycopg.Connection,
    fake_embedder: Any,
    tmp_path: Path,
    *,
    extra: dict[str, tuple[str, str]] | None = None,
) -> tuple[Path, dict[str, str]]:
    """A vault with one movable note plus a referrer. Returns title→id."""
    vault = tmp_path / "vault"
    vault_module.init_vault(vault)
    _write(vault / NOTE_RELATIVE, {"title": NOTE_TITLE}, "primary body\n")
    _write(
        vault / "daily" / "2026-07-14.md",
        {"title": "Daily Entry"},
        f"see [[{NOTE_TITLE}]] for context\n",
    )
    for relative, (title, body) in (extra or {}).items():
        _write(vault / relative, {"title": title}, body)
    sync_vault(test_db, embedder=fake_embedder, vault_path=vault)
    rows = test_db.execute(
        "SELECT title, id::text FROM documents WHERE kind='vault'"
    ).fetchall()
    return vault, {str(t): str(i) for t, i in rows}


@pytest.fixture
def moveable(
    test_db: psycopg.Connection,
    tmp_path: Path,
    fake_embedder: Any,
    patch_embedder: Callable[[object], None],
    monkeypatch: pytest.MonkeyPatch,
) -> tuple[Path, str]:
    """Wire the CLI at the test vault + fake embedder. Returns (vault, id)."""
    patch_embedder(fake_embedder)
    vault, ids = _seed(test_db, fake_embedder, tmp_path)
    monkeypatch.setenv("BRAIN_VAULT_PATH", str(vault))
    return vault, ids[NOTE_TITLE]


# ---------------------------------------------------------------------------
# The happy path
# ---------------------------------------------------------------------------


def test_move_relocates_file_and_preserves_document_id(
    test_db: psycopg.Connection, moveable: tuple[Path, str]
) -> None:
    """Red-first before F8: ``No such command 'move'`` → ``SystemExit(2)``."""
    vault, doc_id = moveable

    result = CliRunner().invoke(
        app, ["note", "move", doc_id, "projects/atlas", "--yes"]
    )

    assert result.exit_code == 0, result.output
    assert (vault / MOVED_RELATIVE).is_file()
    assert not (vault / NOTE_RELATIVE).exists()
    assert f"moved {NOTE_RELATIVE} → {MOVED_RELATIVE}" in result.output
    row = test_db.execute(
        "SELECT title, vault_path FROM documents WHERE id=%s", (doc_id,)
    ).fetchone()
    assert row is not None, "the document id must be preserved, never re-minted"
    assert row == (NOTE_TITLE, MOVED_RELATIVE)


def test_incoming_links_survive_the_move(
    test_db: psycopg.Connection, moveable: tuple[Path, str]
) -> None:
    """The backlink-destruction regression.

    ``links`` rows reference ``documents.id``. A move that re-minted the id
    (or that let a racing watcher ``DELETE`` the row by its old
    ``vault_path``) would cascade every incoming edge away.
    """
    _vault, doc_id = moveable
    before = test_db.execute(
        "SELECT count(*) FROM links WHERE dst_document_id=%s", (doc_id,)
    ).fetchone()
    assert before is not None and before[0] > 0, (
        "pre-condition: the referrer must produce an incoming link row"
    )

    result = CliRunner().invoke(
        app, ["note", "move", doc_id, "projects/atlas", "--yes"]
    )
    assert result.exit_code == 0, result.output

    after = test_db.execute(
        "SELECT count(*) FROM links WHERE dst_document_id=%s", (doc_id,)
    ).fetchone()
    assert after is not None
    assert after[0] == before[0], "incoming backlinks must survive a move"


def test_move_creates_the_missing_destination_folder(
    moveable: tuple[Path, str],
) -> None:
    vault, doc_id = moveable
    assert not (vault / "archive").exists()

    result = CliRunner().invoke(
        app, ["note", "move", doc_id, "archive/2026/q3", "--yes"]
    )

    assert result.exit_code == 0, result.output
    assert (vault / "archive" / "2026" / "q3" / "weekly-sync-platform.md").is_file()


def test_move_to_the_vault_root(moveable: tuple[Path, str]) -> None:
    vault, doc_id = moveable

    result = CliRunner().invoke(app, ["note", "move", doc_id, ".", "--yes"])

    assert result.exit_code == 0, result.output
    assert (vault / "weekly-sync-platform.md").is_file()


def test_move_to_the_same_folder_is_a_reported_noop(
    test_db: psycopg.Connection, moveable: tuple[Path, str]
) -> None:
    vault, doc_id = moveable

    result = CliRunner().invoke(app, ["note", "move", doc_id, "inbox", "--yes"])

    assert result.exit_code == 0, result.output
    assert "already in inbox — nothing to do" in result.output
    assert (vault / NOTE_RELATIVE).is_file()
    row = test_db.execute(
        "SELECT vault_path FROM documents WHERE id=%s", (doc_id,)
    ).fetchone()
    assert row is not None and row[0] == NOTE_RELATIVE


# ---------------------------------------------------------------------------
# --dry-run
# ---------------------------------------------------------------------------


def test_dry_run_writes_nothing(
    test_db: psycopg.Connection, moveable: tuple[Path, str]
) -> None:
    vault, doc_id = moveable

    result = CliRunner().invoke(
        app, ["note", "move", doc_id, "projects/atlas", "--dry-run"]
    )

    assert result.exit_code == 0, result.output
    assert f"would move {NOTE_RELATIVE} → {MOVED_RELATIVE}" in result.output
    assert "(dry run — nothing written)" in result.output
    assert (vault / NOTE_RELATIVE).is_file()
    assert not (vault / "projects").exists()
    row = test_db.execute(
        "SELECT vault_path FROM documents WHERE id=%s", (doc_id,)
    ).fetchone()
    assert row is not None and row[0] == NOTE_RELATIVE


def test_dry_run_lists_every_file_that_would_be_rewritten(
    moveable: tuple[Path, str],
) -> None:
    _vault, doc_id = moveable

    result = CliRunner().invoke(
        app, ["note", "move", doc_id, "projects/atlas", "--dry-run"]
    )

    assert result.exit_code == 0, result.output
    assert "would rewrite 1 reference(s) in 1 file(s):" in result.output
    assert "daily/2026-07-14.md:" in result.output
    assert (
        f"[[inbox/weekly-sync-platform|{NOTE_TITLE}]] → [[{NOTE_TITLE}]]"
        in result.output
    )


def test_dry_run_does_not_prompt(moveable: tuple[Path, str]) -> None:
    """Nothing to confirm when nothing will be written."""
    _vault, doc_id = moveable

    result = CliRunner().invoke(
        app, ["note", "move", doc_id, "projects/atlas", "--dry-run"], input=""
    )

    assert result.exit_code == 0, result.output
    assert "[y/N]" not in result.output


# ---------------------------------------------------------------------------
# Confirmation
# ---------------------------------------------------------------------------


def test_confirmation_required_without_yes(
    test_db: psycopg.Connection, moveable: tuple[Path, str]
) -> None:
    vault, doc_id = moveable

    result = CliRunner().invoke(
        app, ["note", "move", doc_id, "projects/atlas"], input="n\n"
    )

    assert result.exit_code == 1
    assert (vault / NOTE_RELATIVE).is_file(), "declining must move nothing"
    assert not (vault / "projects").exists()
    row = test_db.execute(
        "SELECT vault_path FROM documents WHERE id=%s", (doc_id,)
    ).fetchone()
    assert row is not None and row[0] == NOTE_RELATIVE


def test_confirmation_prompt_states_the_blast_radius(
    moveable: tuple[Path, str],
) -> None:
    _vault, doc_id = moveable

    result = CliRunner().invoke(
        app, ["note", "move", doc_id, "projects/atlas"], input="n\n"
    )

    assert f"Move {NOTE_RELATIVE} → {MOVED_RELATIVE}" in result.output
    assert "rewriting 1 reference(s) in 1 file(s)" in result.output


def test_yes_skips_confirmation(moveable: tuple[Path, str]) -> None:
    vault, doc_id = moveable

    result = CliRunner().invoke(
        app, ["note", "move", doc_id, "projects/atlas", "--yes"], input=""
    )

    assert result.exit_code == 0, result.output
    assert (vault / MOVED_RELATIVE).is_file()


# ---------------------------------------------------------------------------
# Failure modes
# ---------------------------------------------------------------------------


def test_collision_exits_1_with_an_actionable_message(
    test_db: psycopg.Connection,
    tmp_path: Path,
    fake_embedder: Any,
    patch_embedder: Callable[[object], None],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """No ``--force``, no overwrite — and the occupant is untouched."""
    patch_embedder(fake_embedder)
    vault, ids = _seed(
        test_db,
        fake_embedder,
        tmp_path,
        extra={MOVED_RELATIVE: ("Weekly Sync Platform Copy", "other body\n")},
    )
    monkeypatch.setenv("BRAIN_VAULT_PATH", str(vault))

    result = CliRunner().invoke(
        app, ["note", "move", ids[NOTE_TITLE], "projects/atlas", "--yes"]
    )

    assert result.exit_code == 1
    combined = result.output + (result.stderr or "")
    assert "already exists" in combined
    assert MOVED_RELATIVE in combined
    assert "other body" in (vault / MOVED_RELATIVE).read_text()
    assert (vault / NOTE_RELATIVE).is_file(), "the source must stay put"


@pytest.mark.parametrize("folder", ["../../escape", "../sibling"])
def test_traversal_folder_exits_2(
    moveable: tuple[Path, str], folder: str
) -> None:
    """A usage error, not a library traceback."""
    vault, doc_id = moveable

    result = CliRunner().invoke(app, ["note", "move", doc_id, folder, "--yes"])

    assert result.exit_code == 2
    assert (vault / NOTE_RELATIVE).is_file()


def test_ingested_tier_doc_is_rejected(
    test_db: psycopg.Connection,
    tmp_path: Path,
    fake_embedder: Any,
    patch_embedder: Callable[[object], None],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Only authored vault notes have a file the user asked us to relocate."""
    patch_embedder(fake_embedder)
    vault, _ids = _seed(test_db, fake_embedder, tmp_path)
    monkeypatch.setenv("BRAIN_VAULT_PATH", str(vault))
    row = test_db.execute(
        "INSERT INTO documents (title, content, content_type, kind, content_hash) "
        "VALUES ('Imported Transcript', 'body', 'transcript', 'ingested', 'h-i1') "
        "RETURNING id::text"
    ).fetchone()
    assert row is not None

    result = CliRunner().invoke(
        app, ["note", "move", str(row[0]), "projects/atlas", "--yes"]
    )

    assert result.exit_code == 1
    combined = result.output + (result.stderr or "")
    assert "not 'vault'" in combined


# ---------------------------------------------------------------------------
# Link refactoring
# ---------------------------------------------------------------------------


def test_no_link_refactor_moves_file_but_leaves_references(
    moveable: tuple[Path, str],
) -> None:
    vault, doc_id = moveable
    referrer = vault / "daily" / "2026-07-14.md"
    before = referrer.read_text()

    result = CliRunner().invoke(
        app,
        ["note", "move", doc_id, "projects/atlas", "--yes", "--no-link-refactor"],
    )

    assert result.exit_code == 0, result.output
    assert (vault / MOVED_RELATIVE).is_file()
    assert referrer.read_text() == before


def test_derived_fence_survives_the_move(
    test_db: psycopg.Connection,
    tmp_path: Path,
    fake_embedder: Any,
    patch_embedder: Callable[[object], None],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A move is a file relocation, not a re-render — the fence is verbatim."""
    from brain.vault.derived_links.fence import (
        FENCE_END_MARKER,
        FENCE_START_MARKER,
    )

    patch_embedder(fake_embedder)
    vault = tmp_path / "vault"
    vault_module.init_vault(vault)
    fence_block = (
        f"{FENCE_START_MARKER}\n- co-occurrence note\n{FENCE_END_MARKER}\n"
    )
    _write(
        vault / NOTE_RELATIVE,
        {"title": NOTE_TITLE},
        f"primary body\n\n{fence_block}",
    )
    sync_vault(test_db, embedder=fake_embedder, vault_path=vault)
    row = test_db.execute(
        "SELECT id::text FROM documents WHERE title=%s", (NOTE_TITLE,)
    ).fetchone()
    assert row is not None
    monkeypatch.setenv("BRAIN_VAULT_PATH", str(vault))

    result = CliRunner().invoke(
        app, ["note", "move", str(row[0]), "projects/atlas", "--yes"]
    )

    assert result.exit_code == 0, result.output
    moved_text = (vault / MOVED_RELATIVE).read_text()
    assert fence_block in moved_text, "the derived-links fence must be byte-identical"
