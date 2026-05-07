"""CLI integration tests for ``brain vault sync``.

Runs through the Typer ``CliRunner`` so the smoke checks exercise argument
parsing + the full real-DB connection path. The patch_embedder fixture
swaps in a fake embedder so we don't depend on Ollama.
"""
import uuid
from pathlib import Path

import psycopg
from typer.testing import CliRunner

from brain.cli import app
from brain.vault.frontmatter import dump_frontmatter, parse_frontmatter


def _write(path: Path, fields: dict, body: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(dump_frontmatter(fields, body))


def test_cli_sync_creates_row(
    test_db: psycopg.Connection, fake_embedder, tmp_path: Path, patch_embedder
) -> None:
    patch_embedder(fake_embedder)
    vault = tmp_path / "vault"
    note_id = str(uuid.uuid4())
    _write(vault / "n.md", {"id": note_id, "title": "Hello"}, "world\n")
    runner = CliRunner()
    result = runner.invoke(app, ["vault", "sync", "--vault", str(vault)])
    assert result.exit_code == 0, result.stdout
    assert "created 1" in result.stdout
    row = test_db.execute(
        "SELECT title FROM documents WHERE id = %s", (note_id,)
    ).fetchone()
    assert row == ("Hello",)


def test_cli_sync_dry_run_writes_nothing(
    test_db: psycopg.Connection, fake_embedder, tmp_path: Path, patch_embedder
) -> None:
    patch_embedder(fake_embedder)
    vault = tmp_path / "vault"
    file_path = vault / "n.md"
    _write(file_path, {"title": "Pending"}, "x\n")
    pre_text = file_path.read_text()
    runner = CliRunner()
    result = runner.invoke(
        app, ["vault", "sync", "--vault", str(vault), "--dry-run"]
    )
    assert result.exit_code == 0, result.stdout
    assert "(dry-run)" in result.stdout
    assert "would assign ids to 1 file" in result.stdout
    # Singular "file", not "file(s)" — pluralization for the count == 1 case.
    assert "1 file(s)" not in result.stdout
    # Disk untouched.
    assert file_path.read_text() == pre_text
    # DB untouched.
    cnt = test_db.execute("SELECT count(*) FROM documents").fetchone()
    assert cnt is not None
    assert cnt[0] == 0


def test_cli_sync_prune_deletes(
    test_db: psycopg.Connection, fake_embedder, tmp_path: Path, patch_embedder
) -> None:
    patch_embedder(fake_embedder)
    vault = tmp_path / "vault"
    note_id = str(uuid.uuid4())
    file_path = vault / "n.md"
    _write(file_path, {"id": note_id, "title": "Soon Gone"}, "x\n")
    runner = CliRunner()
    runner.invoke(app, ["vault", "sync", "--vault", str(vault)])
    file_path.unlink()
    result = runner.invoke(
        app, ["vault", "sync", "--vault", str(vault), "--prune"]
    )
    assert result.exit_code == 0, result.stdout
    assert "deleted 1" in result.stdout
    cnt = test_db.execute("SELECT count(*) FROM documents").fetchone()
    assert cnt is not None
    assert cnt[0] == 0


def test_cli_sync_warn_default(
    test_db: psycopg.Connection, fake_embedder, tmp_path: Path, patch_embedder
) -> None:
    patch_embedder(fake_embedder)
    vault = tmp_path / "vault"
    note_id = str(uuid.uuid4())
    file_path = vault / "n.md"
    _write(file_path, {"id": note_id, "title": "X"}, "x\n")
    runner = CliRunner()
    runner.invoke(app, ["vault", "sync", "--vault", str(vault)])
    file_path.unlink()
    result = runner.invoke(app, ["vault", "sync", "--vault", str(vault)])
    assert result.exit_code == 0, result.stdout
    assert "warned 1" in result.stdout
    cnt = test_db.execute("SELECT count(*) FROM documents").fetchone()
    assert cnt is not None
    # Warn does NOT delete.
    assert cnt[0] == 1


def test_cli_sync_missing_vault_exits_2(
    test_db: psycopg.Connection,
    fake_embedder,
    tmp_path: Path,
    patch_embedder,
) -> None:
    patch_embedder(fake_embedder)
    vault = tmp_path / "does-not-exist"
    runner = CliRunner()
    result = runner.invoke(app, ["vault", "sync", "--vault", str(vault)])
    assert result.exit_code == 2
    assert "not a directory" in (result.stdout + (result.stderr or ""))


def test_cli_sync_uses_configured_default(
    test_db: psycopg.Connection,
    fake_embedder,
    tmp_path: Path,
    patch_embedder,
    monkeypatch,
) -> None:
    """Without ``--vault`` flag, falls back to ``BRAIN_VAULT_PATH``."""
    patch_embedder(fake_embedder)
    vault = tmp_path / "v"
    note_id = str(uuid.uuid4())
    _write(vault / "n.md", {"id": note_id, "title": "Default"}, "x\n")
    monkeypatch.setenv("BRAIN_VAULT_PATH", str(vault))
    runner = CliRunner()
    result = runner.invoke(app, ["vault", "sync"])
    assert result.exit_code == 0, result.stdout
    assert "created 1" in result.stdout


def test_cli_sync_reports_links(
    test_db: psycopg.Connection, fake_embedder, tmp_path: Path, patch_embedder
) -> None:
    """Both resolved and unresolved counters surface in the summary."""
    patch_embedder(fake_embedder)
    vault = tmp_path / "vault"
    a = str(uuid.uuid4())
    b = str(uuid.uuid4())
    _write(vault / "a.md", {"id": a, "title": "A"}, "x\n")
    _write(
        vault / "b.md",
        {"id": b, "title": "B"},
        "see [[A]] and [[Missing]]\n",
    )
    runner = CliRunner()
    result = runner.invoke(app, ["vault", "sync", "--vault", str(vault)])
    assert result.exit_code == 0, result.stdout
    assert "links_resolved 1" in result.stdout
    assert "links_unresolved 1" in result.stdout


def test_cli_sync_id_assignment_in_real_run(
    test_db: psycopg.Connection,
    fake_embedder,
    tmp_path: Path,
    patch_embedder,
) -> None:
    """The CLI summary tells the user how many ids were assigned."""
    patch_embedder(fake_embedder)
    vault = tmp_path / "vault"
    file_path = vault / "fresh.md"
    _write(file_path, {"title": "Fresh"}, "x\n")
    runner = CliRunner()
    result = runner.invoke(app, ["vault", "sync", "--vault", str(vault)])
    assert result.exit_code == 0, result.stdout
    assert "assigned ids to 1 file" in result.stdout
    assert "1 file(s)" not in result.stdout
    fields, _ = parse_frontmatter(file_path.read_text())
    assert "id" in fields


def test_cli_sync_id_assignment_plural(
    test_db: psycopg.Connection,
    fake_embedder,
    tmp_path: Path,
    patch_embedder,
) -> None:
    """With count > 1, the summary uses 'files', not 'file(s)' or 'file'."""
    patch_embedder(fake_embedder)
    vault = tmp_path / "vault"
    _write(vault / "a.md", {"title": "A"}, "x\n")
    _write(vault / "b.md", {"title": "B"}, "y\n")
    _write(vault / "c.md", {"title": "C"}, "z\n")
    runner = CliRunner()
    result = runner.invoke(app, ["vault", "sync", "--vault", str(vault)])
    assert result.exit_code == 0, result.stdout
    assert "assigned ids to 3 files" in result.stdout
    assert "3 file(s)" not in result.stdout


def test_cli_sync_prints_per_file_errors(
    test_db: psycopg.Connection,
    fake_embedder,
    tmp_path: Path,
    patch_embedder,
) -> None:
    patch_embedder(fake_embedder)
    vault = tmp_path / "vault"
    bad = vault / "broken.md"
    bad.parent.mkdir(parents=True, exist_ok=True)
    bad.write_text("---\nfoo: [unclosed\n---\nbody\n")
    runner = CliRunner()
    result = runner.invoke(app, ["vault", "sync", "--vault", str(vault)])
    assert result.exit_code == 0  # per-file errors don't fail the run
    assert "errors 1" in result.stdout
    # The file path appears in the per-file error line.
    combined = result.stdout + (result.stderr or "")
    assert str(bad) in combined


def test_cli_sync_summary_includes_fences_written(
    test_db: psycopg.Connection, fake_embedder, tmp_path: Path, patch_embedder
) -> None:
    """The sync summary line surfaces the ``fences_written`` counter.

    Regression for the 2026-05-08 fix: with the rewriter now
    idempotent, ``fences_written`` is a meaningful counter (only counts
    actual disk writes), so it belongs in the user-facing summary
    alongside ``links_rewritten``. Both the initial-sync and the
    steady-state branch print the same shape.
    """
    patch_embedder(fake_embedder)
    vault = tmp_path / "vault"
    note_id = str(uuid.uuid4())
    _write(vault / "n.md", {"id": note_id, "title": "Hello"}, "world\n")
    runner = CliRunner()
    result = runner.invoke(app, ["vault", "sync", "--vault", str(vault)])
    assert result.exit_code == 0, result.stdout
    assert "fences_written 0" in result.stdout

    # Run again — steady-state branch (no initial-sync prefix). Must
    # also include the counter.
    result2 = runner.invoke(app, ["vault", "sync", "--vault", str(vault)])
    assert result2.exit_code == 0, result2.stdout
    assert "fences_written 0" in result2.stdout
