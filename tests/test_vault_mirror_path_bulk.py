"""Every bulk vault command leaves legacy mirror files exactly where they are (F15).

F15 changed how a NEW mirror is named. The owner's vault already holds mirrors at
old-rule nested paths (``_ingested/code/2026-01-15-/srv/syn-…``), and a live
watcher syncs it. Each test here seeds a ``tmp_path`` vault with such mirrors,
whose rows store those paths. It runs ONE bulk command through the CLI, and
compares a full snapshot (every file's relative path + sha256, and every
directory) before and after. The only allowed difference is the explicit set of
writes the command exists to make; nothing else may be added, removed or changed.
"""
from __future__ import annotations

import uuid
from pathlib import Path
from typing import Any

import psycopg
import pytest
from pytest_mock import MockerFixture
from typer.testing import CliRunner, Result

from brain.cli import app
from brain.vault.frontmatter import dump_frontmatter
from tests.mirror_legacy_fixtures import (
    LegacyDoc,
    directories,
    seed_legacy_vault,
    snapshot,
    stored_paths,
)

_ORPHAN = "_ingested/manual/synthetic-orphan.md"


@pytest.fixture
def vault(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    path = tmp_path / "vault"
    monkeypatch.setenv("BRAIN_VAULT_PATH", str(path))
    return path


@pytest.fixture
def seeded(
    test_db: psycopg.Connection[Any],
    fake_embedder: Any,
    patch_embedder: Any,
    vault: Path,
    mocker: MockerFixture,
) -> list[LegacyDoc]:
    patch_embedder(fake_embedder)
    # relink-derived refreshes Calendar/Contacts through ``gws``; with no binary on
    # PATH the refresh degrades to a warning and nothing real is ever called.
    mocker.patch("brain.gws.shutil.which", return_value=None)
    docs = seed_legacy_vault(test_db, fake_embedder, vault)
    # Sanity: the seed really is the nested, legacy shape this suite is about.
    assert all(d.legacy_path.count("/") > 2 for d in docs)
    return docs


def _invoke(*argv: str) -> Result:
    result = CliRunner().invoke(app, list(argv))
    assert result.exit_code == 0, result.output
    return result


def _assert_only(
    before: dict[str, str],
    after: dict[str, str],
    *,
    changed: frozenset[str] = frozenset(),
    added: frozenset[str] = frozenset(),
    removed: frozenset[str] = frozenset(),
) -> None:
    assert set(after) - set(before) == added
    assert set(before) - set(after) == removed
    really_changed = {p for p in set(before) & set(after) if before[p] != after[p]}
    assert really_changed == changed


def _run_and_compare(
    conn: psycopg.Connection[Any],
    vault: Path,
    argv: tuple[str, ...],
    *,
    added_dirs: frozenset[str] = frozenset(),
    **expected: Any,
) -> None:
    files, dirs, rows = snapshot(vault), directories(vault), stored_paths(conn)

    _invoke(*argv)

    _assert_only(files, snapshot(vault), **expected)
    assert directories(vault) == dirs | added_dirs
    assert stored_paths(conn) == rows


def test_vault_export_changes_nothing(
    test_db: psycopg.Connection[Any], vault: Path, seeded: list[LegacyDoc]
) -> None:
    _run_and_compare(test_db, vault, ("vault", "export", "--to", str(vault)))


def test_vault_export_force_changes_nothing(
    test_db: psycopg.Connection[Any], vault: Path, seeded: list[LegacyDoc]
) -> None:
    _run_and_compare(test_db, vault, ("vault", "export", "--to", str(vault), "--force"))


@pytest.mark.parametrize("extra", [(), ("--prune",)])
def test_vault_sync_changes_nothing(
    test_db: psycopg.Connection[Any],
    vault: Path,
    seeded: list[LegacyDoc],
    extra: tuple[str, ...],
) -> None:
    _run_and_compare(test_db, vault, ("vault", "sync", "--vault", str(vault), *extra))


def test_vault_relink_derived_changes_nothing_but_the_people_hub(
    test_db: psycopg.Connection[Any], vault: Path, seeded: list[LegacyDoc]
) -> None:
    # Its one expected write is the People Hub index (and its folder); no mirror
    # file may move.
    _run_and_compare(
        test_db,
        vault,
        ("vault", "relink-derived"),
        added=frozenset({"people/index.md"}),
        added_dirs=frozenset({"people"}),
    )


@pytest.mark.parametrize("extra", [(), ("--include-stale",)])
def test_vault_prune_orphans_removes_only_the_true_orphan(
    test_db: psycopg.Connection[Any],
    vault: Path,
    seeded: list[LegacyDoc],
    extra: tuple[str, ...],
) -> None:
    orphan = vault / _ORPHAN
    orphan.write_text(
        dump_frontmatter({"id": str(uuid.uuid4()), "title": "orphan"}, "gone\n"),
        encoding="utf-8",
    )

    _run_and_compare(
        test_db,
        vault,
        ("vault", "prune-orphans", "--apply", "--vault", str(vault), *extra),
        removed=frozenset({_ORPHAN}),
    )


def test_vault_sync_summaries_rewrites_only_the_summarized_mirror(
    test_db: psycopg.Connection[Any], vault: Path, seeded: list[LegacyDoc]
) -> None:
    code = seeded[0]
    test_db.execute(
        "UPDATE documents SET summary = %s WHERE id = %s",
        ("A synthetic two-sentence summary. It names nothing real.", code.doc_id),
    )

    _run_and_compare(
        test_db,
        vault,
        ("vault", "sync-summaries", "--vault", str(vault)),
        changed=frozenset({code.legacy_path}),
    )
