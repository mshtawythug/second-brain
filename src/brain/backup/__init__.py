"""Durable, verifiable snapshots of the brain — `brain backup` / `brain restore` (F3).

An archive is a single ``brain-backup-<ts>[-label].tar.gz`` holding a
custom-format Postgres dump, an uncompressed ``vault.tar``, and a
self-describing ``manifest.json``, next to a ``.sha256`` sidecar. Restore
refuses every incompatible combination *before* touching anything, demands a
typed phrase no flag can bypass, takes its own pre-restore backup, and swaps
via a staging database so the previous one still exists afterwards.
"""
from .archive import extract_archive, read_manifest, sha256_file, verify_sidecar
from .create import BackupResult, create_backup, validate_label
from .discovery import BackupSummary, latest_backup, list_backups
from .manifest import MANIFEST_SCHEMA, BackupManifest, FileEntry, collect_manifest
from .pgtool import (
    CommandRunner,
    PgToolPlan,
    SubprocessRunner,
    password_env_file,
    resolve_pg_tool,
    server_version,
)
from .restore import (
    RESTORE_PHRASE,
    Preflight,
    PreflightIssue,
    PreparedRestore,
    RestoreReport,
    preflight,
    prepare_restore,
    restore_backup,
)

__all__ = [
    "MANIFEST_SCHEMA",
    "RESTORE_PHRASE",
    "BackupManifest",
    "BackupResult",
    "BackupSummary",
    "CommandRunner",
    "FileEntry",
    "PgToolPlan",
    "Preflight",
    "PreflightIssue",
    "PreparedRestore",
    "RestoreReport",
    "SubprocessRunner",
    "collect_manifest",
    "create_backup",
    "extract_archive",
    "latest_backup",
    "list_backups",
    "password_env_file",
    "preflight",
    "prepare_restore",
    "read_manifest",
    "resolve_pg_tool",
    "restore_backup",
    "server_version",
    "sha256_file",
    "validate_label",
    "verify_sidecar",
]
