"""A REAL ``pg_dump`` → ``pg_restore`` round trip on the test server (RM-47).

Every other restore suite fakes ``pg_restore`` (``tests/backup_fakes.py``), and
that is exactly how a restore of any Apache-AGE brain could fail for months
unnoticed: the dump excludes ``ag_catalog`` but keeps the ``age`` EXTENSION
entry, which ``pg_restore --exit-on-error`` replays as ``CREATE EXTENSION IF NOT
EXISTS age WITH SCHEMA ag_catalog`` and fails on a fresh staging database.

This module runs the real tools, through production's own container discovery
(``resolve_pg_tool`` picks the container that publishes the test DSN's port), so
it reaches the 5434 test container and nothing else:

* the **source** is a dedicated throwaway database, created and dropped here by
  exact name;
* the **target** is the shared restore sandbox (``restore_sandbox_dsn``), never
  the suite database, and teardown puts its original back with
  ``drop_restore_artifacts`` exactly as ``test_restore_swap.py`` does.

Skipped when ``docker`` is not on ``PATH``, except under GitHub Actions, where a
missing ``docker`` is an error: CI runs the same AGE test container on port
5434, and must run this test rather than skip it.
"""
from __future__ import annotations

import os
import shutil
from collections.abc import Iterator
from pathlib import Path

import psycopg
import pytest
from psycopg import sql

from brain.backup import RestoreReport, SubprocessRunner, create_backup, restore_backup
from brain.config import Config
from brain.db import bootstrap_age, connect, ensure_embedding_column, run_migrations
from tests.backup_fakes import (
    ProdDsnLeak,
    SuiteDatabaseNameTooLong,
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
from tests.conftest import FakeEmbedder, _looks_like_prod_db
from tests.test_backup_manifest import EMBEDDER_NAME, EMBEDDING_DIM

if shutil.which("docker") is None:
    # In CI a skip here would turn the only real-tool regression test into a green
    # no-op, so CI must provide the tools: fail loudly there instead of skipping.
    if os.environ.get("GITHUB_ACTIONS") == "true":
        raise RuntimeError("CI must run the real pg_dump/pg_restore round trip: no docker")
    pytest.skip("docker is not on PATH: no real pg_dump/pg_restore", allow_module_level=True)

try:
    SANDBOX_DSN = _restore_sandbox_dsn(_SUITE_DATABASE_URL)
except SuiteDatabaseNameTooLong as exc:
    pytest.skip(str(exc), allow_module_level=True)

SUITE_DB = dsn_database(_SUITE_DATABASE_URL)
SOURCE_AGE_DB = f"{SUITE_DB}_rtage"
SOURCE_PLAIN_DB = f"{SUITE_DB}_rtplain"
ADMIN_DSN = dsn_for_database(_SUITE_DATABASE_URL, "postgres")


def _refuse_prod(dsn: str) -> None:
    params = psycopg.conninfo.conninfo_to_dict(dsn)
    port = params.get("port")
    if _looks_like_prod_db(
        str(params.get("host") or "") or None,
        int(str(port)) if port else None,
        str(params.get("dbname") or "") or None,
    ):
        raise ProdDsnLeak(f"refusing a real pg tool run against production: {dsn!r}")


def _drop_exact(name: str) -> None:
    assert name in {SOURCE_AGE_DB, SOURCE_PLAIN_DB}, name
    with psycopg.connect(ADMIN_DSN, autocommit=True) as admin:
        admin.execute(
            sql.SQL("DROP DATABASE IF EXISTS {} WITH (FORCE)").format(sql.Identifier(name))
        )


def _make_source(name: str, *, with_age: bool) -> str:
    """A migrated throwaway brain holding one synthetic document."""
    dsn = dsn_for_database(_SUITE_DATABASE_URL, name)
    _refuse_prod(dsn)
    _drop_exact(name)
    with psycopg.connect(ADMIN_DSN, autocommit=True) as admin:
        admin.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(name)))
    embedder = FakeEmbedder(dim=EMBEDDING_DIM)
    with connect(dsn) as conn:
        conn.autocommit = True
        run_migrations(conn)
        ensure_embedding_column(conn, embedder)
        if with_age:
            bootstrap_age(conn)
    seed_document_into(
        dsn, embedder, title="Larkspur review", content="Synthetic body for a real round trip."
    )
    return dsn


def _has_age(dsn: str) -> bool:
    with psycopg.connect(dsn) as conn:
        row = conn.execute("SELECT 1 FROM pg_extension WHERE extname = 'age'").fetchone()
    return row is not None


def _cfg(dsn: str, root: Path) -> Config:
    vault = root / "vault"
    vault.mkdir(parents=True)
    return Config(
        database_url=dsn,
        embedder=EMBEDDER_NAME,
        vault_path=vault,
        backup_dir=root / "backups",
        brain_home=root / "home",
    )


@pytest.fixture(autouse=True)
def _sandbox_and_sources() -> Iterator[None]:
    _refuse_prod(SANDBOX_DSN)
    reset_restore_sandbox(SANDBOX_DSN)
    yield
    drop_restore_artifacts(SANDBOX_DSN)
    _drop_exact(SOURCE_AGE_DB)
    _drop_exact(SOURCE_PLAIN_DB)


def _round_trip(source_dsn: str, tmp_path: Path) -> RestoreReport:
    embedder = FakeEmbedder(dim=EMBEDDING_DIM)
    archive = create_backup(
        _cfg(source_dsn, tmp_path / "source"), runner=SubprocessRunner(), embedder=embedder
    ).archive_path
    return restore_backup(
        archive,
        _cfg(SANDBOX_DSN, tmp_path / "target"),
        runner=SubprocessRunner(),
        embedder=embedder,
    )


def test_a_real_dump_of_an_age_brain_restores(tmp_path: Path) -> None:
    """Regression: ``schema "ag_catalog" does not exist`` aborted every AGE restore."""
    source = _make_source(SOURCE_AGE_DB, with_age=True)
    assert _has_age(source)  # precondition: the dump carries the age EXTENSION entry

    report = _round_trip(source, tmp_path)

    assert report.db_restored is True
    assert live_document_count(SANDBOX_DSN) == 1


def test_a_real_dump_without_age_still_restores(tmp_path: Path) -> None:
    """The pair: an archive with no ``age`` entry is unaffected by the staging prep."""
    source = _make_source(SOURCE_PLAIN_DB, with_age=False)
    assert not _has_age(source)  # precondition: no age entry in this dump

    report = _round_trip(source, tmp_path)

    assert report.db_restored is True
    assert live_document_count(SANDBOX_DSN) == 1
