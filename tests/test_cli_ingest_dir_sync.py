"""``brain ingest-dir <dir> --sync [--apply] [--allow-empty]`` (RM-37).

The CLI half of the prune pass: option misuse is a usage error, every refusal exits 1 before
anything is written, a dry run (the default) lists and deletes nothing, and ``--apply`` deletes
one line per document. The leaf's own rules are in ``tests/test_import_sync.py``.

Trees are ``tmp_path`` only; the database is the 5434 test database. All content is synthetic.
"""
from __future__ import annotations

import os
from pathlib import Path
from typing import Any

import psycopg
import pytest
from typer.testing import CliRunner

from brain.cli import app

TEST_DATABASE_URL = os.environ.get(
    "TEST_DATABASE_URL",
    "postgresql://brain:brain@localhost:5434/second_brain_test",
)
NOTE_BODY = "# Synthetic note\n\nA paragraph about an imaginary planning offsite.\n"


@pytest.fixture
def env(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> tuple[Path, Path]:
    """``(import_root, vault)``: the fake embedder, no enricher, graph off, a tmp vault."""
    from tests.conftest import FakeEmbedder

    monkeypatch.setattr("brain.cli._build_embedder", lambda cfg: FakeEmbedder())
    monkeypatch.setattr("brain.cli._build_enricher", lambda cfg: None)
    monkeypatch.setenv("BRAIN_GRAPH_ENABLED", "false")
    monkeypatch.setenv("DATABASE_URL", TEST_DATABASE_URL)
    vault = tmp_path / "vault"
    vault.mkdir()
    monkeypatch.setenv("BRAIN_VAULT_PATH", str(vault))
    root = tmp_path / "notes"
    root.mkdir()
    return root, vault


def _write(root: Path, name: str) -> Path:
    path = root / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(f"{NOTE_BODY}\n{name}\n", encoding="utf-8")
    return path


def _invoke(*args: str) -> Any:
    return CliRunner().invoke(app, ["ingest-dir", *args])


def _import(root: Path, *names: str) -> dict[str, str]:
    """Write and ``ingest-dir`` the files; return ``{name: document_id}``."""
    for name in names:
        _write(root, name)
    result = _invoke(str(root))
    assert result.exit_code == 0, result.output
    return {name: _doc_id(root / name) for name in names}


def _doc_id(path: Path) -> str:
    with psycopg.connect(TEST_DATABASE_URL) as conn:
        row = conn.execute(
            "SELECT id::text FROM documents WHERE source_path=%s", (str(path.resolve()),)
        ).fetchone()
    assert row is not None
    return str(row[0])


def _exists(document_id: str) -> bool:
    with psycopg.connect(TEST_DATABASE_URL) as conn:
        return (
            conn.execute("SELECT 1 FROM documents WHERE id=%s", (document_id,)).fetchone()
            is not None
        )


def _doc_count() -> int:
    with psycopg.connect(TEST_DATABASE_URL) as conn:
        row = conn.execute("SELECT count(*) FROM documents").fetchone()
    assert row is not None
    return int(row[0])


# ---------------------------------------------------------------------------
# Option misuse — exit 2
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "flags",
    [["--apply"], ["--allow-empty"], ["--sync", "--apply", "--dry-run"]],
)
def test_option_misuse_is_a_usage_error(
    test_db: psycopg.Connection[Any], env: tuple[Path, Path], flags: list[str]
) -> None:
    root, _ = env
    _write(root, "a.md")

    result = _invoke(str(root), *flags)

    assert result.exit_code == 2, result.output
    assert _doc_count() == 0


# ---------------------------------------------------------------------------
# Refusals — exit 1, nothing written
# ---------------------------------------------------------------------------


def test_refusal_missing_directory(
    test_db: psycopg.Connection[Any], env: tuple[Path, Path]
) -> None:
    root, _ = env
    ids = _import(root, "a.md")

    result = _invoke(str(root / "unmounted"), "--sync", "--apply")

    assert result.exit_code != 0
    assert all(_exists(i) for i in ids.values())


def test_refusal_directory_inside_the_vault(
    test_db: psycopg.Connection[Any], env: tuple[Path, Path]
) -> None:
    _, vault = env
    inner = vault / "imported"
    _write(inner, "new.md")

    result = _invoke(str(inner), "--sync")

    assert result.exit_code == 1, result.output
    assert "brain vault" in result.output
    assert _doc_count() == 0  # refused before the walk ingested anything


def test_refusal_empty_tree_while_rows_exist(
    test_db: psycopg.Connection[Any], env: tuple[Path, Path]
) -> None:
    root, _ = env
    ids = _import(root, "a.md", "b.md")
    for name in ids:
        (root / name).unlink()

    result = _invoke(str(root), "--sync", "--apply")

    assert result.exit_code == 1, result.output
    assert "--allow-empty" in result.output
    assert all(_exists(i) for i in ids.values())


def test_refusal_empty_tree_also_refuses_the_dry_run(
    test_db: psycopg.Connection[Any], env: tuple[Path, Path]
) -> None:
    root, _ = env
    ids = _import(root, "a.md")
    (root / "a.md").unlink()

    result = _invoke(str(root), "--sync")

    assert result.exit_code == 1, result.output
    assert _exists(ids["a.md"])


def test_allow_empty_overrides_the_empty_tree_refusal(
    test_db: psycopg.Connection[Any], env: tuple[Path, Path]
) -> None:
    root, _ = env
    ids = _import(root, "a.md", "b.md")
    for name in ids:
        (root / name).unlink()

    result = _invoke(str(root), "--sync", "--apply", "--allow-empty")

    assert result.exit_code == 0, result.output
    assert not any(_exists(i) for i in ids.values())


def _mass_tree(root: Path, *, total: int, gone: int) -> dict[str, str]:
    ids = _import(root, *(f"n{i:02d}.md" for i in range(total)))
    for name in sorted(ids)[:gone]:
        (root / name).unlink()
    return ids


def test_mass_delete_guard_refuses_apply(
    test_db: psycopg.Connection[Any], env: tuple[Path, Path]
) -> None:
    root, _ = env
    ids = _mass_tree(root, total=7, gone=6)

    result = _invoke(str(root), "--sync", "--apply")

    assert result.exit_code == 1, result.output
    assert "6 of 7" in result.output and "--allow-empty" in result.output
    assert all(_exists(i) for i in ids.values())


def test_mass_delete_guard_allows_exactly_half(
    test_db: psycopg.Connection[Any], env: tuple[Path, Path]
) -> None:
    root, _ = env
    ids = _mass_tree(root, total=12, gone=6)

    result = _invoke(str(root), "--sync", "--apply")

    assert result.exit_code == 0, result.output
    assert sum(_exists(i) for i in ids.values()) == 6


def test_mass_delete_guard_dry_run_reports_and_warns(
    test_db: psycopg.Connection[Any], env: tuple[Path, Path]
) -> None:
    root, _ = env
    ids = _mass_tree(root, total=7, gone=6)

    result = _invoke(str(root), "--sync")

    assert result.exit_code == 0, result.output
    assert "6 of 7 documents under" in result.output
    assert "--apply would refuse" in result.output
    assert all(_exists(i) for i in ids.values())


def test_mass_delete_guard_is_overridden_by_allow_empty(
    test_db: psycopg.Connection[Any], env: tuple[Path, Path]
) -> None:
    root, _ = env
    ids = _mass_tree(root, total=7, gone=6)

    result = _invoke(str(root), "--sync", "--apply", "--allow-empty")

    assert result.exit_code == 0, result.output
    assert sum(_exists(i) for i in ids.values()) == 1


# ---------------------------------------------------------------------------
# Acceptance — dry run lists, --apply removes; vault-tier rows never touched
# ---------------------------------------------------------------------------


def test_dry_run_lists_the_deleted_file_and_deletes_nothing(
    test_db: psycopg.Connection[Any], env: tuple[Path, Path]
) -> None:
    root, _ = env
    ids = _import(root, "keep.md", "sub/gone.md")
    (root / "sub" / "gone.md").unlink()

    result = _invoke(str(root), "--sync")

    assert result.exit_code == 0, result.output
    assert f"would delete: {ids['sub/gone.md'][:8]}  sub/gone.md" in result.output
    assert "1 of 2 documents under" in result.output
    assert "--apply" in result.output
    assert _exists(ids["sub/gone.md"]) and _exists(ids["keep.md"])


def test_apply_removes_the_deleted_file_and_keeps_the_rest(
    test_db: psycopg.Connection[Any], env: tuple[Path, Path]
) -> None:
    root, vault = env
    ids = _import(root, "keep.md", "sub/gone.md")
    mirrors_before = sorted(p.name for p in vault.rglob("*.md"))
    (root / "sub" / "gone.md").unlink()

    result = _invoke(str(root), "--sync", "--apply")

    assert result.exit_code == 0, result.output
    assert f"deleted: {ids['sub/gone.md'][:8]}  sub/gone.md" in result.output
    assert "deleted 1 of 2 documents under" in result.output
    assert not _exists(ids["sub/gone.md"])
    assert _exists(ids["keep.md"])
    assert len(sorted(p.name for p in vault.rglob("*.md"))) == len(mirrors_before) - 1


def test_apply_after_a_move_keeps_the_new_row_and_drops_the_old(
    test_db: psycopg.Connection[Any], env: tuple[Path, Path]
) -> None:
    root, _ = env
    ids = _import(root, "old-name.md")
    (root / "old-name.md").rename(root / "new-name.md")

    result = _invoke(str(root), "--sync", "--apply")

    assert result.exit_code == 0, result.output
    assert not _exists(ids["old-name.md"])
    assert _exists(_doc_id(root / "new-name.md"))


def test_dry_run_flag_lists_the_walk_and_the_prune(
    test_db: psycopg.Connection[Any], env: tuple[Path, Path]
) -> None:
    root, _ = env
    ids = _import(root, "keep.md", "gone.md")
    (root / "gone.md").unlink()
    _write(root, "fresh.md")
    before = _doc_count()

    result = _invoke(str(root), "--sync", "--dry-run")

    assert result.exit_code == 0, result.output
    assert "would ingest:" in result.output and "fresh.md" in result.output
    assert f"would delete: {ids['gone.md'][:8]}  gone.md" in result.output
    assert _doc_count() == before


def test_vault_tier_row_is_never_touched(
    test_db: psycopg.Connection[Any], env: tuple[Path, Path]
) -> None:
    root, _ = env
    ids = _import(root, "keep.md", "owned.md")
    with psycopg.connect(TEST_DATABASE_URL, autocommit=True) as conn:
        conn.execute("UPDATE documents SET kind='vault' WHERE id=%s", (ids["owned.md"],))
    (root / "owned.md").unlink()

    result = _invoke(str(root), "--sync", "--apply", "--allow-empty")

    assert result.exit_code == 0, result.output
    assert ids["owned.md"][:8] not in result.output
    assert _exists(ids["owned.md"])


def test_sync_with_nothing_to_remove_says_so(
    test_db: psycopg.Connection[Any], env: tuple[Path, Path]
) -> None:
    root, _ = env
    _import(root, "keep.md")

    result = _invoke(str(root), "--sync", "--apply")

    assert result.exit_code == 0, result.output
    assert "deleted 0 of 1 documents under" in result.output


def test_unverifiable_and_vault_nested_counts_are_reported(
    test_db: psycopg.Connection[Any],
    env: tuple[Path, Path],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root, _ = env
    locked = root / "locked"
    ids = _import(root, "keep.md", "locked/private.md")
    nested_vault = root / "nested-vault"
    nested_vault.mkdir()
    _write(nested_vault, "inside.md")
    assert _invoke(str(nested_vault)).exit_code == 0
    (nested_vault / "inside.md").unlink()
    monkeypatch.setenv("BRAIN_VAULT_PATH", str(nested_vault))
    locked.chmod(0)
    try:
        if os.access(locked, os.X_OK):
            pytest.skip("running with privileges that ignore directory permissions")
        result = _invoke(str(root), "--sync")
    finally:
        locked.chmod(0o755)

    assert result.exit_code == 0, result.output
    assert "could not check 1 document" in result.output
    assert "skipped 1 document" in result.output
    assert _exists(ids["locked/private.md"])


def test_apply_reports_a_failed_delete_and_exits_1(
    test_db: psycopg.Connection[Any], env: tuple[Path, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    root, _ = env
    ids = _import(root, "keep.md", "gone.md")
    (root / "gone.md").unlink()

    def failing_delete(conn: Any, **kwargs: Any) -> Any:
        raise psycopg.OperationalError("synthetic detail")

    monkeypatch.setattr("brain.ingest.import_sync.delete_document", failing_delete)

    result = _invoke(str(root), "--sync", "--apply")

    assert result.exit_code == 1, result.output
    assert f"failed: {ids['gone.md'][:8]}  gone.md — OperationalError" in result.output
    assert "synthetic detail" not in result.output
    assert _exists(ids["gone.md"])


def test_apply_reports_kept_and_missing_rows(
    test_db: psycopg.Connection[Any], env: tuple[Path, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    root, _ = env
    ids = _import(root, "keep.md", "back.md", "raced.md")
    (root / "back.md").unlink()
    (root / "raced.md").unlink()
    from brain.ingest import import_sync

    real_apply = import_sync.apply_prune

    def apply_after_races(conn: Any, plan: Any, **kwargs: Any) -> Any:
        _write(root, "back.md")
        conn.execute("DELETE FROM documents WHERE id=%s", (ids["raced.md"],))
        return real_apply(conn, plan, **kwargs)

    monkeypatch.setattr("brain.cli_import_sync.apply_prune", apply_after_races)

    result = _invoke(str(root), "--sync", "--apply")

    assert result.exit_code == 0, result.output
    assert f"kept (back on disk): {ids['back.md'][:8]}  back.md" in result.output
    assert f"already gone: {ids['raced.md'][:8]}  raced.md" in result.output
    assert "deleted 0 of 3 documents under" in result.output


def test_apply_rechecks_the_mass_delete_guard_after_the_walk(
    test_db: psycopg.Connection[Any], env: tuple[Path, Path]
) -> None:
    # Files that vanish while the walk runs pass the preflight and must still be refused.
    import typer

    from brain.cli_import_sync import DirSync
    from brain.ingest.dispatch import supported_extensions

    root, vault = env
    ids = _import(root, *(f"n{i:02d}.md" for i in range(7)))
    run = DirSync.start_or_exit(
        root,
        vault_root=vault,
        extensions=supported_extensions(directory_walk=True),
        supported_files=7,
        apply=True,
        allow_empty=False,
    )
    with psycopg.connect(TEST_DATABASE_URL, autocommit=True) as conn:
        run.preflight_or_exit(conn)
        for name in sorted(ids)[:6]:
            (root / name).unlink()

        with pytest.raises(typer.Exit) as exited:
            run.prune(conn, graph_syncer=None)

    assert exited.value.exit_code == 1
    assert all(_exists(i) for i in ids.values())
