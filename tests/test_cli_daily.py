"""Integration tests for ``brain daily``."""
from __future__ import annotations

import stat
from collections.abc import Callable
from datetime import date
from pathlib import Path

import psycopg
import pytest
from typer.testing import CliRunner

from brain import vault as vault_module
from brain.cli import app
from brain.vault.frontmatter import parse_frontmatter


def _init_vault(p: Path) -> None:
    vault_module.init_vault(p)


def _make_fake_editor(tmp_path: Path, *, body: str = "#!/bin/sh\nexit 0\n") -> Path:
    s = tmp_path / "ed.sh"
    s.write_text(body)
    s.chmod(s.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    return s


def test_daily_creates_today(
    test_db: psycopg.Connection,
    tmp_path: Path,
    fake_embedder,
    patch_embedder: Callable[[object], None],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    patch_embedder(fake_embedder)
    vault = tmp_path / "vault"
    _init_vault(vault)
    monkeypatch.setenv("BRAIN_VAULT_PATH", str(vault))
    today = date.today()
    target = vault / "daily" / f"{today.year:04d}" / f"{today.isoformat()}.md"
    result = CliRunner().invoke(app, ["daily", "--no-edit"])
    assert result.exit_code == 0, result.output
    assert target.is_file()
    assert "created" in result.output


def test_daily_with_explicit_date(
    test_db: psycopg.Connection,
    tmp_path: Path,
    fake_embedder,
    patch_embedder: Callable[[object], None],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    patch_embedder(fake_embedder)
    vault = tmp_path / "vault"
    _init_vault(vault)
    monkeypatch.setenv("BRAIN_VAULT_PATH", str(vault))
    result = CliRunner().invoke(
        app, ["daily", "--date", "2025-12-25", "--no-edit"]
    )
    assert result.exit_code == 0, result.output
    target = vault / "daily" / "2025" / "2025-12-25.md"
    assert target.is_file()
    fields, _ = parse_frontmatter(target.read_text())
    assert fields["title"] == "2025-12-25"


def test_daily_idempotent(
    test_db: psycopg.Connection,
    tmp_path: Path,
    fake_embedder,
    patch_embedder: Callable[[object], None],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Re-running daily for the same date opens the existing file."""
    patch_embedder(fake_embedder)
    vault = tmp_path / "vault"
    _init_vault(vault)
    monkeypatch.setenv("BRAIN_VAULT_PATH", str(vault))
    runner = CliRunner()
    runner.invoke(app, ["daily", "--date", "2026-04-29", "--no-edit"])
    target = vault / "daily" / "2026" / "2026-04-29.md"
    before = target.read_text()
    result = runner.invoke(app, ["daily", "--date", "2026-04-29", "--no-edit"])
    assert result.exit_code == 0, result.output
    assert "opened" in result.output
    assert "(existing)" in result.output
    # File contents unchanged.
    assert target.read_text() == before


def test_daily_invalid_date(
    test_db: psycopg.Connection,
    tmp_path: Path,
    fake_embedder,
    patch_embedder: Callable[[object], None],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    patch_embedder(fake_embedder)
    vault = tmp_path / "vault"
    _init_vault(vault)
    monkeypatch.setenv("BRAIN_VAULT_PATH", str(vault))
    result = CliRunner().invoke(
        app, ["daily", "--date", "2026/04/29", "--no-edit"]
    )
    assert result.exit_code != 0
    combined = result.stdout + (result.stderr or "")
    assert "YYYY-MM-DD" in combined or "isoformat" in combined.lower() or "date" in combined.lower()


def test_daily_year_folder_on_demand(
    test_db: psycopg.Connection,
    tmp_path: Path,
    fake_embedder,
    patch_embedder: Callable[[object], None],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    patch_embedder(fake_embedder)
    vault = tmp_path / "vault"
    _init_vault(vault)
    monkeypatch.setenv("BRAIN_VAULT_PATH", str(vault))
    # Year folder doesn't exist yet — daily should create it.
    assert not (vault / "daily" / "2030").exists()
    result = CliRunner().invoke(
        app, ["daily", "--date", "2030-01-01", "--no-edit"]
    )
    assert result.exit_code == 0, result.output
    assert (vault / "daily" / "2030" / "2030-01-01.md").is_file()


def test_daily_db_row_uses_date_as_title(
    test_db: psycopg.Connection,
    tmp_path: Path,
    fake_embedder,
    patch_embedder: Callable[[object], None],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    patch_embedder(fake_embedder)
    vault = tmp_path / "vault"
    _init_vault(vault)
    monkeypatch.setenv("BRAIN_VAULT_PATH", str(vault))
    CliRunner().invoke(app, ["daily", "--date", "2026-04-29", "--no-edit"])
    row = test_db.execute(
        "SELECT title, kind FROM documents WHERE kind='vault'"
    ).fetchone()
    assert row == ("2026-04-29", "vault")


def test_daily_invokes_editor_on_existing(
    test_db: psycopg.Connection,
    tmp_path: Path,
    fake_embedder,
    patch_embedder: Callable[[object], None],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Running daily a second time opens the existing file in $EDITOR."""
    patch_embedder(fake_embedder)
    vault = tmp_path / "vault"
    _init_vault(vault)
    monkeypatch.setenv("BRAIN_VAULT_PATH", str(vault))
    runner = CliRunner()
    runner.invoke(app, ["daily", "--date", "2026-04-29", "--no-edit"])
    editor = _make_fake_editor(
        tmp_path,
        body="#!/bin/sh\nprintf 'APPENDED\\n' >> \"$1\"\nexit 0\n",
    )
    monkeypatch.setenv("EDITOR", str(editor))
    monkeypatch.delenv("VISUAL", raising=False)
    result = runner.invoke(app, ["daily", "--date", "2026-04-29"])
    assert result.exit_code == 0, result.output
    target = vault / "daily" / "2026" / "2026-04-29.md"
    assert "APPENDED" in target.read_text()
    row = test_db.execute(
        "SELECT content FROM documents WHERE title='2026-04-29'"
    ).fetchone()
    assert row is not None and "APPENDED" in row[0]


def test_daily_vault_flag_override(
    test_db: psycopg.Connection,
    tmp_path: Path,
    fake_embedder,
    patch_embedder: Callable[[object], None],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    patch_embedder(fake_embedder)
    vault_a = tmp_path / "a"
    vault_b = tmp_path / "b"
    _init_vault(vault_a)
    _init_vault(vault_b)
    monkeypatch.setenv("BRAIN_VAULT_PATH", str(vault_a))
    result = CliRunner().invoke(
        app,
        ["daily", "--date", "2026-04-29", "--no-edit", "--vault", str(vault_b)],
    )
    assert result.exit_code == 0, result.output
    assert (vault_b / "daily" / "2026" / "2026-04-29.md").is_file()
    assert not (vault_a / "daily" / "2026" / "2026-04-29.md").exists()
