"""The self-describing `manifest.json` that makes a restore verifiable."""
from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, replace
from datetime import datetime
from importlib.metadata import PackageNotFoundError
from importlib.metadata import version as _dist_version
from typing import Any

import psycopg

from ..config import Config
from ..db import _table_exists
from ..errors import BackupError
from ..ingest import Embedder
from ..queries import embedding_column_state, summary_counts
from .pgtool import PgToolPlan, server_version

#: Manifest format version. Restore refuses anything else outright rather than
#: guessing at an unknown layout — a wrong guess here restores wrong data.
MANIFEST_SCHEMA = 1

#: `pg_dump -Fc`. Recorded so a future plain-SQL variant stays distinguishable.
DUMP_FORMAT = "custom"

#: Apache AGE keeps graphs outside `public`, in schemas `pg_dump` cannot
#: round-trip: the `ag_catalog` registry rows are not dumped, so restored label
#: tables would form a graph AGE cannot open. The graph's relational source of
#: truth lives in `public` and IS dumped; the mirror is rebuilt afterwards with
#: `brain graphrag build --force`. See F3 §5.2.
EXCLUDED_SCHEMAS = ("ag_catalog", "brain_graph")

#: Distribution name on PyPI — the import package is `brain`. Duplicated from
#: `cli._DIST_NAME` rather than imported: extracted command modules must never
#: import `brain.cli` (it imports them), and this is a one-line constant.
_DIST_NAME = "secondbrain-py"

_REQUIRED_KEYS = (
    "schema",
    "created_at",
    "label",
    "brain_version",
    "postgres_version",
    "postgres_version_num",
    "pg_dump_version",
    "pg_dump_source",
    "container_name",
    "database_name",
    "dump_format",
    "dump_excluded_schemas",
    "migration_head",
    "migration_count",
    "embedder",
    "embedding_dim",
    "embedding_column_type",
    "embedding_not_null",
    "embedding_has_index",
    "counts",
    "graph_entities",
    "vault_included",
    "vault_path",
    "vault_file_count",
    "files",
)


def brain_version() -> str:
    """Installed distribution version, or a sentinel for a source checkout."""
    try:
        return _dist_version(_DIST_NAME)
    except PackageNotFoundError:
        return "unknown (not installed as a distribution)"


@dataclass(frozen=True)
class FileEntry:
    """One checksummed member inside the archive."""

    bytes: int
    sha256: str

    def to_dict(self) -> dict[str, Any]:
        return {"bytes": self.bytes, "sha256": self.sha256}

    @classmethod
    def from_dict(cls, payload: Any, *, name: str) -> FileEntry:
        if not isinstance(payload, Mapping):
            raise BackupError(
                f"manifest files entry {name!r} is not an object: {payload!r}"
            )
        for key in ("bytes", "sha256"):
            if key not in payload:
                raise BackupError(f"manifest files entry {name!r} is missing {key!r}")
        return cls(bytes=int(payload["bytes"]), sha256=str(payload["sha256"]))


@dataclass(frozen=True)
class BackupManifest:
    """Everything needed to validate a later restore. Serialized as manifest.json."""

    schema: int
    created_at: datetime
    label: str
    brain_version: str
    postgres_version: str
    postgres_version_num: int
    pg_dump_version: str
    pg_dump_source: str
    container_name: str | None
    database_name: str
    dump_format: str
    dump_excluded_schemas: tuple[str, ...]
    migration_head: str
    migration_count: int
    embedder: str
    embedding_dim: int
    embedding_column_type: str
    embedding_not_null: bool
    embedding_has_index: bool
    counts: Mapping[str, int]
    graph_entities: int | None
    vault_included: bool
    vault_path: str | None
    vault_file_count: int | None
    files: Mapping[str, FileEntry]

    @property
    def server_major(self) -> int:
        """Major version of the server this archive was dumped from."""
        return self.postgres_version_num // 10000

    def with_files(self, files: Mapping[str, FileEntry]) -> BackupManifest:
        """Return a copy carrying ``files``.

        The manifest is collected before the dump exists, so member checksums
        are attached once the members have actually been written.
        """
        return replace(self, files=dict(files))

    def to_dict(self) -> dict[str, Any]:
        """JSON-ready mapping. ``created_at`` becomes an ISO-8601 UTC string."""
        return {
            "schema": self.schema,
            "created_at": self.created_at.isoformat(),
            "label": self.label,
            "brain_version": self.brain_version,
            "postgres_version": self.postgres_version,
            "postgres_version_num": self.postgres_version_num,
            "pg_dump_version": self.pg_dump_version,
            "pg_dump_source": self.pg_dump_source,
            "container_name": self.container_name,
            "database_name": self.database_name,
            "dump_format": self.dump_format,
            "dump_excluded_schemas": list(self.dump_excluded_schemas),
            "migration_head": self.migration_head,
            "migration_count": self.migration_count,
            "embedder": self.embedder,
            "embedding_dim": self.embedding_dim,
            "embedding_column_type": self.embedding_column_type,
            "embedding_not_null": self.embedding_not_null,
            "embedding_has_index": self.embedding_has_index,
            "counts": dict(self.counts),
            "graph_entities": self.graph_entities,
            "vault_included": self.vault_included,
            "vault_path": self.vault_path,
            "vault_file_count": self.vault_file_count,
            "files": {name: entry.to_dict() for name, entry in self.files.items()},
        }

    @classmethod
    def from_dict(cls, payload: Any) -> BackupManifest:
        """Parse a manifest, rejecting anything unexpected.

        Strict by design: a manifest is read from a file the user may have
        copied between machines, so a missing or malformed key must surface as
        an actionable error *before* a restore starts, never as a ``KeyError``
        halfway through one.
        """
        if not isinstance(payload, Mapping):
            raise BackupError(
                "manifest.json must contain a JSON object, got "
                f"{type(payload).__name__}"
            )
        missing = [key for key in _REQUIRED_KEYS if key not in payload]
        if missing:
            raise BackupError(
                "manifest.json is missing required key(s): " + ", ".join(missing)
            )
        schema = payload["schema"]
        if schema != MANIFEST_SCHEMA:
            raise BackupError(
                f"unsupported manifest schema {schema!r}; this brain understands "
                f"schema {MANIFEST_SCHEMA}. The archive was probably written by a "
                "newer brain — upgrade with: pipx upgrade secondbrain-py"
            )
        try:
            created_at = datetime.fromisoformat(str(payload["created_at"]))
        except ValueError as exc:
            raise BackupError(
                "manifest.json has an unparseable created_at: "
                f"{payload['created_at']!r}"
            ) from exc
        files_payload = payload["files"]
        if not isinstance(files_payload, Mapping):
            raise BackupError("manifest.json 'files' must be an object")
        counts_payload = payload["counts"]
        if not isinstance(counts_payload, Mapping):
            raise BackupError("manifest.json 'counts' must be an object")
        container_name = payload["container_name"]
        vault_path = payload["vault_path"]
        vault_file_count = payload["vault_file_count"]
        graph_entities = payload["graph_entities"]
        return cls(
            schema=int(schema),
            created_at=created_at,
            label=str(payload["label"]),
            brain_version=str(payload["brain_version"]),
            postgres_version=str(payload["postgres_version"]),
            postgres_version_num=int(payload["postgres_version_num"]),
            pg_dump_version=str(payload["pg_dump_version"]),
            pg_dump_source=str(payload["pg_dump_source"]),
            container_name=None if container_name is None else str(container_name),
            database_name=str(payload["database_name"]),
            dump_format=str(payload["dump_format"]),
            dump_excluded_schemas=tuple(
                str(item) for item in payload["dump_excluded_schemas"]
            ),
            migration_head=str(payload["migration_head"]),
            migration_count=int(payload["migration_count"]),
            embedder=str(payload["embedder"]),
            embedding_dim=int(payload["embedding_dim"]),
            embedding_column_type=str(payload["embedding_column_type"]),
            embedding_not_null=bool(payload["embedding_not_null"]),
            embedding_has_index=bool(payload["embedding_has_index"]),
            counts={str(k): int(v) for k, v in counts_payload.items()},
            graph_entities=None if graph_entities is None else int(graph_entities),
            vault_included=bool(payload["vault_included"]),
            vault_path=None if vault_path is None else str(vault_path),
            vault_file_count=(
                None if vault_file_count is None else int(vault_file_count)
            ),
            files={
                str(name): FileEntry.from_dict(entry, name=str(name))
                for name, entry in files_payload.items()
            },
        )


def _graph_entity_count(conn: psycopg.Connection[Any]) -> int | None:
    """Count graph entities, or ``None`` on a pre-GraphRAG database."""
    if not _table_exists(conn, "graph_entities"):
        return None
    row = conn.execute("SELECT count(*) FROM graph_entities").fetchone()
    return int(row[0]) if row is not None else None


def _migration_state(conn: psycopg.Connection[Any]) -> tuple[str, int]:
    """Return ``(head, applied_count)`` from ``schema_migrations``."""
    row = conn.execute("SELECT max(name), count(*) FROM schema_migrations").fetchone()
    if row is None or row[0] is None:
        raise BackupError(
            "this database has no applied migrations — run `brain init` before "
            "backing it up"
        )
    return str(row[0]), int(row[1])


def _database_name(database_url: str) -> str:
    """Database name from a DSN, via psycopg's parser — never split by hand."""
    parsed = psycopg.conninfo.conninfo_to_dict(database_url)
    dbname = parsed.get("dbname")
    if not dbname:
        raise BackupError(
            f"could not determine the database name from the configured DSN "
            f"(host={parsed.get('host')!r})"
        )
    return str(dbname)


def collect_manifest(
    conn: psycopg.Connection[Any],
    cfg: Config,
    embedder: Embedder,
    *,
    label: str,
    pg_dump_plan: PgToolPlan,
    vault_included: bool,
    vault_file_count: int | None,
    now: datetime,
) -> BackupManifest:
    """Snapshot everything a later restore must validate against.

    ``files`` is left empty here and filled in by
    :func:`brain.backup.create.create_backup` once the members exist and can be
    checksummed — at this point the dump has not been taken yet.
    """
    counts = summary_counts(conn)
    column = embedding_column_state(conn)
    head, applied_count = _migration_state(conn)
    postgres_version, postgres_version_num = server_version(conn)
    return BackupManifest(
        schema=MANIFEST_SCHEMA,
        created_at=now,
        label=label,
        brain_version=brain_version(),
        postgres_version=postgres_version,
        postgres_version_num=postgres_version_num,
        pg_dump_version=pg_dump_plan.version,
        pg_dump_source=pg_dump_plan.source,
        container_name=pg_dump_plan.container,
        database_name=_database_name(cfg.database_url),
        dump_format=DUMP_FORMAT,
        dump_excluded_schemas=EXCLUDED_SCHEMAS,
        migration_head=head,
        migration_count=applied_count,
        embedder=cfg.embedder,
        embedding_dim=embedder.dim,
        embedding_column_type=column.column_type,
        embedding_not_null=column.not_null,
        embedding_has_index=column.has_index,
        counts={
            "documents": counts.documents,
            "chunks": counts.chunks,
            "sources": counts.sources,
        },
        graph_entities=_graph_entity_count(conn),
        vault_included=vault_included,
        vault_path=str(cfg.vault_path) if vault_included else None,
        vault_file_count=vault_file_count,
        files={},
    )
