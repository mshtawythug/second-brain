"""The staging-database swap and vault rename-aside (F3 §5.8, §6.4, §6.5, §7).

These exercise the real ``ALTER DATABASE ... RENAME`` against the private test
server. ``pg_restore`` itself is still faked — :class:`RestoringRunner` applies
the migrations that restoring an empty brain's dump would produce — so no real
dump file is ever replayed and production is never reachable.
"""
from __future__ import annotations

from collections.abc import Callable, Iterator
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import psycopg
import pytest
from psycopg import sql

from brain.backup import create_backup, read_manifest, restore_backup
from brain.config import Config
from brain.errors import BackupError, RestoreAborted
from tests.backup_fakes import (
    CONTAINER_VERSION,
    StubAndRestoreRunner,
    StubDumpRunner,
    drop_restore_artifacts,
    dsn_database,
    dsn_for_database,
    live_document_count,
    repo_root_guard,  # noqa: F401 — autouse fixture
    reset_restore_sandbox,
    seed_document_into,
)
from tests.backup_fakes import restore_sandbox_dsn as _restore_sandbox_dsn
from tests.conftest import TEST_DATABASE_URL as _SUITE_DATABASE_URL
from tests.conftest import FakeEmbedder
from tests.test_backup_manifest import EMBEDDER_NAME, EMBEDDING_DIM

# C22 (2026-08-07): these tests exercise a REAL restore swap — the live database
# is parked aside and a fresh one takes its name, then teardown reclaims it with
# `DROP DATABASE ... WITH (FORCE)`. Pointed at the suite's own database that
# terminates the session's own backends; twice it crash-restarted the Postgres
# instance and left the database unusable. So the whole module runs against a
# dedicated throwaway on the same server. Every derived name below
# (TEST_DB_NAME, PARKED_DB, MAINTENANCE_DSN) follows from it automatically.

TEST_DATABASE_URL = _restore_sandbox_dsn(_SUITE_DATABASE_URL)

NOW = datetime(2026, 7, 25, 18, 15, 0, tzinfo=UTC)
#: Derived, never hardcoded — a literal scratch-DB name passes only in the
#: sandbox that created it and fails in CI (`second_brain_test`).
TEST_DB_NAME = dsn_database(TEST_DATABASE_URL)
PARKED_DB = f"{TEST_DB_NAME}_replaced_20260725_181500"
#: Same server as the suite, `postgres` database — derived so it follows
#: TEST_DATABASE_URL's host and port instead of pinning this sandbox's.
MAINTENANCE_DSN = dsn_for_database(TEST_DATABASE_URL, "postgres")


@pytest.fixture(autouse=True)
def _restore_sandbox_state() -> Iterator[None]:
    """Give each test a clean sandbox, and reclaim what its swap parked aside.

    Setup: the sandbox is not the suite database, so nothing else empties it —
    and teardown deliberately renames the parked database (seeded corpus and
    all) back over the live name. Without the reset the module's own tests
    contaminate each other. Teardown: a successful restore RETAINS the replaced
    database; leaving it behind leaks one database per swap onto the shared
    server and poisons the next session's AGE catalog.
    """
    reset_restore_sandbox(TEST_DATABASE_URL)
    yield
    drop_restore_artifacts(TEST_DATABASE_URL)


@pytest.fixture
def seed_live_doc() -> Callable[..., str]:
    """``conftest.seed_doc``, but into the sandbox the restore will replace.

    Builds its embedder explicitly at ``EMBEDDING_DIM`` rather than taking the
    ``fake_embedder`` fixture: every other embedder in this module is an
    explicit ``FakeEmbedder(dim=EMBEDDING_DIM)``, and the fixture's default
    only happens to match. If that default ever moved, the seed would fail
    with a raw pgvector dimension error from a test that never mentions dims.
    """

    def _seed(*, title: str, content: str) -> str:
        return seed_document_into(
            TEST_DATABASE_URL,
            FakeEmbedder(dim=EMBEDDING_DIM),
            title=title,
            content=content,
        )

    return _seed


def _live_document_count() -> int:
    """Documents currently in the database under the live name."""
    return live_document_count(TEST_DATABASE_URL)


@pytest.fixture
def cfg(tmp_path: Path) -> Config:
    vault = tmp_path / "vault"
    vault.mkdir()
    (vault / "keep-me.md").write_text("Synthetic vault note.\n", encoding="utf-8")
    return Config(
        database_url=TEST_DATABASE_URL,
        embedder=EMBEDDER_NAME,
        vault_path=vault,
        backup_dir=tmp_path / "backups",
        brain_home=tmp_path / "brain-home",
    )


@pytest.fixture
def runner() -> StubAndRestoreRunner:
    return StubAndRestoreRunner(
        base_dsn=TEST_DATABASE_URL, responses={"--version": CONTAINER_VERSION}
    )


@pytest.fixture
def archive(test_db: psycopg.Connection, cfg: Config) -> Path:
    """An archive of the empty test database (counts 0/0, real migration head)."""
    return create_backup(
        cfg,
        runner=StubDumpRunner(responses={"--version": CONTAINER_VERSION}),
        clock=lambda: NOW,
        embedder=FakeEmbedder(dim=EMBEDDING_DIM),
    ).archive_path


def _databases(pattern: str) -> list[str]:
    with psycopg.connect(MAINTENANCE_DSN) as conn:
        rows = conn.execute(
            "SELECT datname FROM pg_database WHERE datname LIKE %s ORDER BY 1",
            (pattern,),
        ).fetchall()
    return [str(row[0]) for row in rows]


def _restore(
    archive: Path, cfg: Config, runner: StubAndRestoreRunner, **kwargs: Any
) -> Any:
    return restore_backup(
        archive,
        cfg,
        runner=runner,
        clock=lambda: NOW,
        embedder=FakeEmbedder(dim=EMBEDDING_DIM),
        **kwargs,
    )


def test_successful_swap_retains_previous_database(
    test_db: psycopg.Connection,
    archive: Path,
    cfg: Config,
    runner: StubAndRestoreRunner,
    seed_live_doc: Callable[..., str],
) -> None:
    """The database that was replaced must still exist afterwards (§7)."""
    seed_live_doc(
        title="Larkspur review", content="Synthetic body before the restore."
    )
    assert _live_document_count() == 1

    report = _restore(archive, cfg, runner)

    assert report.db_restored is True
    assert report.replaced_database == PARKED_DB
    assert PARKED_DB in _databases(f"{TEST_DB_NAME}_replaced_%")
    # The live name now holds the restored (empty) corpus.
    assert _live_document_count() == 0
    # No staging database is left behind.
    assert _databases(f"{TEST_DB_NAME}_restore_%") == []


def test_previous_database_still_holds_the_old_rows(
    test_db: psycopg.Connection,
    archive: Path,
    cfg: Config,
    runner: StubAndRestoreRunner,
    seed_live_doc: Callable[..., str],
) -> None:
    """The retained database is a real undo, not an empty shell."""
    seed_live_doc(
        title="Larkspur review", content="Synthetic body before the restore."
    )

    report = _restore(archive, cfg, runner)

    parked_dsn = dsn_for_database(TEST_DATABASE_URL, str(report.replaced_database))
    with psycopg.connect(parked_dsn) as conn:
        row = conn.execute("SELECT count(*) FROM documents").fetchone()
    assert row is not None and row[0] == 1


def test_verification_failure_drops_staging_and_leaves_live_untouched(
    test_db: psycopg.Connection,
    archive: Path,
    cfg: Config,
    seed_live_doc: Callable[..., str],
) -> None:
    """A restore that did not populate staging aborts before any swap (§6.4)."""
    seed_live_doc(
        title="Larkspur review", content="Synthetic body before the restore."
    )
    # A runner whose pg_restore is inert: staging ends up with no schema, so
    # verification must refuse rather than swap an empty database in.
    inert = StubAndRestoreRunner(
        base_dsn=TEST_DATABASE_URL, responses={"--version": CONTAINER_VERSION}
    )
    inert.migrate_enabled = False

    with pytest.raises(BackupError, match="Nothing was changed"):
        _restore(archive, cfg, inert)

    # The live database still holds the pre-restore corpus, unswapped.
    assert _live_document_count() == 1
    assert _databases(f"{TEST_DB_NAME}_restore_%") == []
    assert _databases(f"{TEST_DB_NAME}_replaced_%") == []


def test_rename_failure_after_first_rename_reports_recovery_sql(
    monkeypatch: pytest.MonkeyPatch,
    test_db: psycopg.Connection,
    archive: Path,
    cfg: Config,
    runner: StubAndRestoreRunner,
) -> None:
    """The non-transactional window between the two renames is handled (§6.5)."""
    from brain.backup import restore as restore_module

    real_swap = restore_module._swap_databases

    def _fail_on_second(
        conn: Any, live_db: str, staging_db: str, parked_db: str
    ) -> None:
        # Do the first rename for real, then fail the second the way a
        # concurrent reconnect or a vanished staging DB would.
        real_swap(conn, live_db, "definitely_not_a_database", parked_db)

    monkeypatch.setattr(restore_module, "_swap_databases", _fail_on_second)

    try:
        with pytest.raises(RestoreAborted) as excinfo:
            _restore(archive, cfg, runner)
    finally:
        # Follow the printed recovery SQL to put the live database back.
        parked = _databases(f"{TEST_DB_NAME}_replaced_%")
        if parked:
            with psycopg.connect(MAINTENANCE_DSN) as conn:
                conn.autocommit = True
                conn.execute(
                    sql.SQL("ALTER DATABASE {} RENAME TO {}").format(
                        sql.Identifier(parked[0]), sql.Identifier(TEST_DB_NAME)
                    )
                )

    assert f"RENAME TO {TEST_DB_NAME}" in excinfo.value.recovery_sql
    assert excinfo.value.recovery_sql.startswith("ALTER DATABASE ")
    assert "intact" in str(excinfo.value)


def test_post_restore_runs_analyze(
    test_db: psycopg.Connection,
    archive: Path,
    cfg: Config,
    runner: StubAndRestoreRunner,
) -> None:
    """`brain doctor`'s 'chunks stats WARN — never analyzed' must not fire (§5.8).

    This is the regression test that wires the existing doctor warning to its
    fix: without the post-restore ANALYZE, the first thing a user sees after a
    successful restore is a doctor warning.
    """
    _restore(archive, cfg, runner)

    with psycopg.connect(TEST_DATABASE_URL) as conn:
        row = conn.execute(
            "SELECT last_analyze IS NOT NULL OR last_autoanalyze IS NOT NULL "
            "FROM pg_stat_user_tables WHERE relname = 'chunks'"
        ).fetchone()
    assert row is not None and row[0] is True


def test_vault_moved_aside_not_deleted(
    test_db: psycopg.Connection,
    archive: Path,
    cfg: Config,
    runner: StubAndRestoreRunner,
) -> None:
    """The user's notes are renamed aside, never rmtree'd (§7)."""
    report = _restore(archive, cfg, runner)

    assert report.vault_restored is True
    assert report.replaced_vault_path is not None
    assert (
        report.replaced_vault_path / "keep-me.md"
    ).read_text(encoding="utf-8") == "Synthetic vault note.\n"
    # The restored vault is back in place at the original path.
    assert (cfg.vault_path / "keep-me.md").exists()


def test_db_only_leaves_the_vault_alone(
    test_db: psycopg.Connection,
    archive: Path,
    cfg: Config,
    runner: StubAndRestoreRunner,
) -> None:
    marker = cfg.vault_path / "untouched.md"
    marker.write_text("still here\n", encoding="utf-8")

    report = _restore(archive, cfg, runner, vault_leg=False)

    assert report.vault_restored is False
    assert report.replaced_vault_path is None
    assert marker.read_text(encoding="utf-8") == "still here\n"


def test_pre_restore_backup_is_taken_before_anything_changes(
    test_db: psycopg.Connection,
    archive: Path,
    cfg: Config,
    runner: StubAndRestoreRunner,
    seed_live_doc: Callable[..., str],
) -> None:
    """A restore without a safety net is the scenario the wipe rule prevents."""
    seed_live_doc(
        title="Larkspur review", content="Synthetic body before the restore."
    )

    report = _restore(archive, cfg, runner)

    assert report.pre_restore_backup is not None
    assert report.pre_restore_backup.exists()
    assert "pre-restore" in report.pre_restore_backup.name
    # A safety net that captured an EMPTY database is not a safety net. The
    # existence assertions above pass either way, so pin what it actually
    # holds: the corpus that was about to be replaced.
    assert read_manifest(report.pre_restore_backup).counts["documents"] == 1


def test_follow_up_names_the_graph_rebuild(
    test_db: psycopg.Connection,
    archive: Path,
    cfg: Config,
    runner: StubAndRestoreRunner,
) -> None:
    """AGE data is excluded by design, so the rebuild must be surfaced (§5.2)."""
    report = _restore(archive, cfg, runner)

    assert "brain graphrag build --force" in report.follow_up
