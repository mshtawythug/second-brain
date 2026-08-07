"""BackupManifest serialization + live collection (F3 §5.5, §5.7)."""
from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import urlparse

import psycopg
import pytest

from brain.backup.manifest import (
    MANIFEST_SCHEMA,
    BackupManifest,
    FileEntry,
    collect_manifest,
)
from brain.backup.pgtool import PgToolPlan
from brain.config import Config
from brain.db import migrations_dir
from brain.errors import BackupError
from brain.ingest import ExtractedDoc, ingest_document
from tests.backup_fakes import repo_root_guard  # noqa: F401
from tests.conftest import TEST_DATABASE_URL, FakeEmbedder

#: Derived from TEST_DATABASE_URL, never hardcoded: a literal scratch-DB name
#: passes only in the sandbox that created it and fails in CI, which uses
#: `second_brain_test`.
EXPECTED_DB_NAME = urlparse(TEST_DATABASE_URL).path.lstrip("/")

NOW = datetime(2026, 7, 25, 14, 12, 3, 482119, tzinfo=UTC)

#: The test schema's `chunks.embedding` is vector(4096) (migration 002), so the
#: fake embedder is sized to match and the config names the 4096-dim backend.
EMBEDDING_DIM = 4096
EMBEDDER_NAME = "qwen3"

PLAN = PgToolPlan(
    tool="pg_dump",
    source="container",
    version="16.14",
    major=16,
    container="second-brain-postgres",
    argv_prefix=("docker", "exec", "second-brain-postgres", "pg_dump"),
)


def _manifest(**overrides: object) -> BackupManifest:
    """A fully-populated manifest — every value synthetic."""
    base: dict[str, object] = {
        "schema": MANIFEST_SCHEMA,
        "created_at": NOW,
        "label": "pre-upgrade",
        "brain_version": "0.2.1",
        "postgres_version": "16.14",
        "postgres_version_num": 160014,
        "pg_dump_version": "16.14",
        "pg_dump_source": "container",
        "container_name": "second-brain-postgres",
        "database_name": EXPECTED_DB_NAME,
        "dump_format": "custom",
        "dump_excluded_schemas": ("ag_catalog", "brain_graph"),
        "migration_head": "023_search_queries_fts_count.sql",
        "migration_count": 23,
        "embedder": EMBEDDER_NAME,
        "embedding_dim": EMBEDDING_DIM,
        "embedding_column_type": f"vector({EMBEDDING_DIM})",
        "embedding_not_null": True,
        "embedding_has_index": True,
        "counts": {"documents": 3, "chunks": 7, "sources": 1},
        "graph_entities": 12,
        "vault_included": True,
        "vault_path": "/tmp/synthetic-vault",
        "vault_file_count": 4,
        "files": {
            "db/second_brain.dump": FileEntry(bytes=2048, sha256="a" * 64),
            "vault.tar": FileEntry(bytes=512, sha256="c" * 64),
        },
    }
    base.update(overrides)
    return BackupManifest(**base)  # type: ignore[arg-type]


def test_roundtrip_preserves_every_field() -> None:
    manifest = _manifest()

    assert BackupManifest.from_dict(manifest.to_dict()) == manifest


def test_to_dict_is_json_serializable_with_documented_key_types() -> None:
    payload = json.loads(json.dumps(_manifest().to_dict()))

    assert payload["schema"] == 1
    assert payload["created_at"] == NOW.isoformat()
    assert payload["dump_excluded_schemas"] == ["ag_catalog", "brain_graph"]
    assert payload["counts"] == {"documents": 3, "chunks": 7, "sources": 1}
    assert payload["files"]["vault.tar"] == {"bytes": 512, "sha256": "c" * 64}


def test_rejects_unknown_schema_version() -> None:
    payload = _manifest().to_dict()
    payload["schema"] = 2

    with pytest.raises(BackupError, match="schema"):
        BackupManifest.from_dict(payload)


@pytest.mark.parametrize(
    "key",
    [
        "created_at",
        "brain_version",
        "postgres_version_num",
        "pg_dump_source",
        "database_name",
        "migration_head",
        "embedder",
        "embedding_dim",
        "counts",
        "vault_included",
        "files",
    ],
)
def test_rejects_missing_required_key(key: str) -> None:
    payload = _manifest().to_dict()
    del payload[key]

    with pytest.raises(BackupError, match=key):
        BackupManifest.from_dict(payload)


def test_rejects_non_mapping_payload() -> None:
    with pytest.raises(BackupError, match="object"):
        BackupManifest.from_dict(["not", "a", "mapping"])  # type: ignore[arg-type]


def test_rejects_malformed_file_entry() -> None:
    payload = _manifest().to_dict()
    payload["files"]["vault.tar"] = {"bytes": 512}  # type: ignore[index]

    with pytest.raises(BackupError, match="sha256"):
        BackupManifest.from_dict(payload)


def test_null_container_name_and_vault_path_roundtrip() -> None:
    """`--no-vault` plus a host-side dump: four fields go null and must survive."""
    manifest = _manifest(
        pg_dump_source="host",
        container_name=None,
        vault_included=False,
        vault_path=None,
        vault_file_count=None,
        graph_entities=None,
        files={"db/second_brain.dump": FileEntry(bytes=2048, sha256="a" * 64)},
    )

    restored = BackupManifest.from_dict(manifest.to_dict())

    assert restored == manifest
    assert restored.container_name is None
    assert restored.graph_entities is None
    assert "vault.tar" not in restored.files


def test_manifest_is_immutable() -> None:
    manifest = _manifest()

    with pytest.raises(AttributeError):
        manifest.label = "changed"  # type: ignore[misc]


def test_with_files_returns_a_new_manifest() -> None:
    """create() collects the manifest before the dump exists, then adds checksums."""
    manifest = _manifest(files={})
    entries = {"db/second_brain.dump": FileEntry(bytes=99, sha256="b" * 64)}

    updated = manifest.with_files(entries)

    assert manifest.files == {}
    assert updated.files == entries
    assert updated is not manifest


def test_collect_manifest_reads_live_db(test_db: psycopg.Connection) -> None:
    embedder = FakeEmbedder(dim=EMBEDDING_DIM)
    for index in range(3):
        ingest_document(
            test_db,
            embedder=embedder,
            doc=ExtractedDoc(
                title=f"Larkspur quarterly review {index}",
                # Bodies must differ: `documents.content_hash` is UNIQUE, so
                # three identical bodies would dedup down to a single document.
                content=f"Synthetic body {index} for the backup manifest test.",
                content_type="note",
                source_path=None,
                metadata={},
            ),
            source_kind="manual",
            tags=[],
        )
    cfg = Config(database_url=TEST_DATABASE_URL, embedder=EMBEDDER_NAME)
    # Read the expected head from disk so this survives migration 024/025.
    migration_files = sorted(migrations_dir().glob("*.sql"))

    manifest = collect_manifest(
        test_db,
        cfg,
        embedder,
        label="unit",
        pg_dump_plan=PLAN,
        vault_included=False,
        vault_file_count=None,
        now=NOW,
    )

    assert manifest.schema == MANIFEST_SCHEMA
    assert manifest.counts["documents"] == 3
    assert manifest.counts["chunks"] > 0
    assert manifest.migration_head == migration_files[-1].name
    assert manifest.migration_count == len(migration_files)
    assert manifest.embedding_dim == embedder.dim
    assert manifest.embedder == EMBEDDER_NAME
    assert manifest.database_name == EXPECTED_DB_NAME
    assert manifest.pg_dump_version == "16.14"
    assert manifest.pg_dump_source == "container"
    assert manifest.container_name == "second-brain-postgres"
    assert manifest.dump_excluded_schemas == ("ag_catalog", "brain_graph")
    assert manifest.dump_format == "custom"
    assert manifest.created_at == NOW
    assert manifest.label == "unit"
    assert manifest.vault_included is False
    assert manifest.vault_path is None
    assert manifest.files == {}
    assert manifest.postgres_version_num >= 160000
    assert manifest.embedding_column_type.startswith("vector")


def test_collect_manifest_records_vault_when_included(
    test_db: psycopg.Connection,
) -> None:
    cfg = Config(database_url=TEST_DATABASE_URL, embedder=EMBEDDER_NAME)

    manifest = collect_manifest(
        test_db,
        cfg,
        FakeEmbedder(dim=EMBEDDING_DIM),
        label="",
        pg_dump_plan=PLAN,
        vault_included=True,
        vault_file_count=4,
        now=NOW,
    )

    assert manifest.vault_included is True
    assert manifest.vault_file_count == 4
    assert manifest.vault_path == str(cfg.vault_path)


def test_collect_manifest_survives_a_manifest_json_roundtrip(
    test_db: psycopg.Connection, tmp_path: Path
) -> None:
    """What create() writes to manifest.json is what restore() reads back."""
    cfg = Config(database_url=TEST_DATABASE_URL, embedder=EMBEDDER_NAME)
    manifest = collect_manifest(
        test_db,
        cfg,
        FakeEmbedder(dim=EMBEDDING_DIM),
        label="",
        pg_dump_plan=PLAN,
        vault_included=False,
        vault_file_count=None,
        now=NOW,
    )
    target = tmp_path / "manifest.json"
    target.write_text(json.dumps(manifest.to_dict()), encoding="utf-8")

    reloaded = BackupManifest.from_dict(json.loads(target.read_text(encoding="utf-8")))

    assert reloaded == manifest


@pytest.mark.fresh_schema
def test_graph_entities_null_when_table_absent(test_db: psycopg.Connection) -> None:
    """A pre-GraphRAG database yields null rather than raising."""
    test_db.execute("DROP TABLE IF EXISTS graph_entity_mentions CASCADE")
    test_db.execute("DROP TABLE IF EXISTS graph_edge_contributions CASCADE")
    test_db.execute("DROP TABLE IF EXISTS graph_community_members CASCADE")
    test_db.execute("DROP TABLE IF EXISTS graph_relationships CASCADE")
    test_db.execute("DROP TABLE IF EXISTS graph_entities CASCADE")
    cfg = Config(database_url=TEST_DATABASE_URL, embedder=EMBEDDER_NAME)

    manifest = collect_manifest(
        test_db,
        cfg,
        FakeEmbedder(dim=EMBEDDING_DIM),
        label="",
        pg_dump_plan=PLAN,
        vault_included=False,
        vault_file_count=None,
        now=NOW,
    )

    assert manifest.graph_entities is None


def test_collect_manifest_counts_graph_entities_when_present(
    test_db: psycopg.Connection,
) -> None:
    cfg = Config(database_url=TEST_DATABASE_URL, embedder=EMBEDDER_NAME)

    manifest = collect_manifest(
        test_db,
        cfg,
        FakeEmbedder(dim=EMBEDDING_DIM),
        label="",
        pg_dump_plan=PLAN,
        vault_included=False,
        vault_file_count=None,
        now=NOW,
    )

    assert manifest.graph_entities == 0


def test_brain_version_falls_back_when_not_installed() -> None:
    """A source checkout that was never pip-installed must not crash a backup."""
    from importlib.metadata import PackageNotFoundError
    from unittest.mock import patch

    from brain.backup.manifest import brain_version

    with patch(
        "brain.backup.manifest._dist_version", side_effect=PackageNotFoundError
    ):
        assert "unknown" in brain_version()


def test_file_entry_rejects_a_non_mapping() -> None:
    with pytest.raises(BackupError, match="not an object"):
        FileEntry.from_dict(["nope"], name="vault.tar")


def test_rejects_unparseable_created_at() -> None:
    payload = _manifest().to_dict()
    payload["created_at"] = "not-a-timestamp"

    with pytest.raises(BackupError, match="created_at"):
        BackupManifest.from_dict(payload)


def test_rejects_non_object_files() -> None:
    payload = _manifest().to_dict()
    payload["files"] = ["db/second_brain.dump"]

    with pytest.raises(BackupError, match="'files' must be an object"):
        BackupManifest.from_dict(payload)


def test_rejects_non_object_counts() -> None:
    payload = _manifest().to_dict()
    payload["counts"] = [3, 7, 1]

    with pytest.raises(BackupError, match="'counts' must be an object"):
        BackupManifest.from_dict(payload)


def test_server_major_is_derived_from_version_num() -> None:
    assert _manifest(postgres_version_num=160014).server_major == 16
    assert _manifest(postgres_version_num=170004).server_major == 17


@pytest.mark.fresh_schema
def test_collect_manifest_refuses_an_unmigrated_database(
    test_db: psycopg.Connection,
) -> None:
    """Backing up a database `brain init` never touched would be a trap."""
    test_db.execute("DELETE FROM schema_migrations")
    cfg = Config(database_url=TEST_DATABASE_URL, embedder=EMBEDDER_NAME)

    with pytest.raises(BackupError, match="brain init"):
        collect_manifest(
            test_db,
            cfg,
            FakeEmbedder(dim=EMBEDDING_DIM),
            label="",
            pg_dump_plan=PLAN,
            vault_included=False,
            vault_file_count=None,
            now=NOW,
        )
