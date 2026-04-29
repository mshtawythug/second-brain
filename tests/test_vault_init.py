"""Tests for brain.vault.init_vault — directory + template scaffold."""
from pathlib import Path

from typer.testing import CliRunner

from brain.cli import app
from brain.vault import (
    VAULT_SUBDIRS,
    VAULT_TEMPLATE_FILES,
    init_vault,
)
from brain.vault.templates import (
    DAILY_TEMPLATE,
    INGESTED_README,
    NOTE_TEMPLATE,
    VAULT_README,
)


def test_creates_full_directory_tree(tmp_path: Path) -> None:
    vault = tmp_path / "vault"
    summary = init_vault(vault)
    assert summary.vault_path == vault
    for sub in VAULT_SUBDIRS:
        assert (vault / sub).is_dir(), f"missing dir: {sub}"
    # Every dir was new on first run.
    assert set(summary.created_dirs) == set(VAULT_SUBDIRS)
    assert summary.existing_dirs == []


def test_writes_all_default_templates(tmp_path: Path) -> None:
    vault = tmp_path / "vault"
    summary = init_vault(vault)
    expected_paths = [rel for rel, _ in VAULT_TEMPLATE_FILES]
    for rel in expected_paths:
        assert (vault / rel).is_file(), f"missing file: {rel}"
    assert set(summary.written_files) == set(expected_paths)
    assert summary.preserved_files == []


def test_template_contents_match_expected(tmp_path: Path) -> None:
    vault = tmp_path / "vault"
    init_vault(vault)
    assert (vault / "_templates/daily.md").read_text() == DAILY_TEMPLATE
    assert (vault / "_templates/note.md").read_text() == NOTE_TEMPLATE
    assert (vault / "_ingested/README.md").read_text() == INGESTED_README
    assert (vault / "README.md").read_text() == VAULT_README


def test_re_init_is_idempotent(tmp_path: Path) -> None:
    vault = tmp_path / "vault"
    init_vault(vault)
    second = init_vault(vault)
    # Second pass should report everything as already present.
    assert second.created_dirs == []
    assert set(second.existing_dirs) == set(VAULT_SUBDIRS)
    assert second.written_files == []
    assert set(second.preserved_files) == {rel for rel, _ in VAULT_TEMPLATE_FILES}


def test_re_init_preserves_user_edits_to_templates(tmp_path: Path) -> None:
    vault = tmp_path / "vault"
    init_vault(vault)
    custom_daily = "# my custom daily template\n"
    (vault / "_templates/daily.md").write_text(custom_daily)
    init_vault(vault)
    # User edits survive the re-run.
    assert (vault / "_templates/daily.md").read_text() == custom_daily


def test_re_init_after_partial_removal_recreates_only_missing(tmp_path: Path) -> None:
    vault = tmp_path / "vault"
    init_vault(vault)
    # Wipe one dir + one template.
    (vault / "_templates/daily.md").unlink()
    (vault / "_attachments").rmdir()
    summary = init_vault(vault)
    assert "_attachments" in summary.created_dirs
    assert "_templates/daily.md" in summary.written_files
    # Untouched files stay preserved.
    assert "README.md" in summary.preserved_files


def test_init_creates_parent_dirs_when_missing(tmp_path: Path) -> None:
    deep = tmp_path / "nested" / "deeper" / "vault"
    init_vault(deep)
    assert deep.is_dir()


# ---------------------------------------------------------------------------
# CLI smoke — `brain vault init` end-to-end.
# ---------------------------------------------------------------------------


def test_cli_vault_init_uses_explicit_path(
    tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.setenv("DATABASE_URL", "postgresql://x:y@h:5432/d")
    runner = CliRunner()
    target = tmp_path / "vault-explicit"
    result = runner.invoke(app, ["vault", "init", "--path", str(target)])
    assert result.exit_code == 0, result.stdout
    assert (target / "README.md").is_file()
    assert "wrote files" in result.stdout
    assert "vault path" in result.stdout


def test_cli_vault_init_uses_configured_default(
    tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.setenv("DATABASE_URL", "postgresql://x:y@h:5432/d")
    target = tmp_path / "vault-default"
    monkeypatch.setenv("BRAIN_VAULT_PATH", str(target))
    runner = CliRunner()
    result = runner.invoke(app, ["vault", "init"])
    assert result.exit_code == 0, result.stdout
    assert (target / "README.md").is_file()
    assert (target / "_ingested/krisp").is_dir()


def test_cli_vault_init_idempotent_via_cli(
    tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.setenv("DATABASE_URL", "postgresql://x:y@h:5432/d")
    target = tmp_path / "vault"
    runner = CliRunner()
    runner.invoke(app, ["vault", "init", "--path", str(target)])
    result = runner.invoke(app, ["vault", "init", "--path", str(target)])
    assert result.exit_code == 0
    # Second pass: everything's already there, no fresh writes.
    assert "left untouched" in result.stdout
    assert "wrote files" not in result.stdout
