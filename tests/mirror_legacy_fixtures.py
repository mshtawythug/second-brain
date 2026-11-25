"""Seed a tmp vault with OLD-rule ("legacy") mirror files, for the F15 suites.

Before F15 a mirror's filename carried the first eight characters of the external
id, unsanitized, so an absolute path or ``owner/repo#N`` id became nested
directories: ``_ingested/code/2026-01-15-/srv/syn-<slug>.md``. Real vaults hold
files at exactly those paths, with ``documents.vault_path`` storing them. These
helpers rebuild that state in ``tmp_path`` so the suites can prove that nothing
ever moves, rewrites or deletes such a file.

The legacy path is computed here from the OLD formula written out by hand, not by
calling production code, so a change to the rule cannot silently change what the
fixture means. Every id, path and body is synthetic.
"""
from __future__ import annotations

import hashlib
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import psycopg

from brain.ingest import ExtractedDoc, ingest_document
from brain.vault import init_vault
from brain.vault.export import regenerate_vault_file
from brain.vault.frontmatter import read_frontmatter_id
from brain.vault.slug import slugify

DATE = "2026-01-15"


@dataclass(frozen=True)
class LegacyDoc:
    """One seeded document and the old-rule path its mirror lives at."""

    doc_id: str
    legacy_path: str
    source_kind: str
    external_id: str


def legacy_path(source_kind: str, external_id: str, title: str, date: str = DATE) -> str:
    """The pre-F15 path, by the old formula: no sanitizing of the 8-char segment."""
    segment = external_id.replace("-", "")[:8]
    return f"_ingested/{source_kind}/{date}-{segment}-{slugify(title)}.md"


def _body(label: str) -> str:
    return (
        f"Synthetic {label} body. The quarterly widget review covered the release "
        "checklist, the documentation backlog and two follow-up owners. Nothing "
        "here refers to a real person, project or company.\n"
    )


def seed_file_doc(
    conn: psycopg.Connection[Any],
    embedder: Any,
    vault: Path,
    *,
    source_kind: str,
    source_path: str,
    title: str,
    content_type: str,
) -> LegacyDoc:
    """Ingest a file-backed doc DB-only, then pin and write its legacy mirror."""
    result = ingest_document(
        conn,
        embedder=embedder,
        doc=ExtractedDoc(
            title=title,
            content=_body(title),
            content_type=content_type,
            source_path=source_path,
            metadata={"date": DATE, "path": "src/widget.py"},
        ),
        source_kind=source_kind,
    )
    assert result.document_id is not None
    return _pin_legacy(conn, vault, result.document_id, source_kind, source_path, title)


def seed_stdin_doc(
    conn: psycopg.Connection[Any],
    embedder: Any,
    vault: Path,
    *,
    source_kind: str,
    external_id: str,
    title: str,
    body: str | None = None,
) -> LegacyDoc:
    """Ingest a sourced stdin doc DB-only, then pin and write its legacy mirror."""
    result = ingest_document(
        conn,
        embedder=embedder,
        doc=ExtractedDoc(
            title=title,
            content=body if body is not None else _body(title),
            content_type="transcript",
            source_path=None,
            metadata={"date": DATE},
        ),
        source_kind=source_kind,
        source_external_id=external_id,
    )
    assert result.document_id is not None
    return _pin_legacy(conn, vault, result.document_id, source_kind, external_id, title)


def _pin_legacy(
    conn: psycopg.Connection[Any],
    vault: Path,
    doc_id: str,
    source_kind: str,
    external_id: str,
    title: str,
) -> LegacyDoc:
    path = legacy_path(source_kind, external_id, title)
    conn.execute("UPDATE documents SET vault_path = %s WHERE id = %s", (path, doc_id))
    regenerate_vault_file(conn, doc_id, vault_path=vault, force=True)
    assert (vault / path).is_file()
    return LegacyDoc(doc_id, path, source_kind, external_id)


def seed_legacy_vault(
    conn: psycopg.Connection[Any], embedder: Any, vault: Path
) -> list[LegacyDoc]:
    """A scaffolded vault holding four legacy nested-path mirrors with stored paths."""
    init_vault(vault)
    return [
        seed_file_doc(
            conn, embedder, vault,
            source_kind="code",
            source_path="/srv/synthetic/repo/src/widget.py",
            title="sample-repo/src/widget.py",
            content_type="code",
        ),
        seed_file_doc(
            conn, embedder, vault,
            source_kind="screenshot",
            source_path="/srv/synthetic/drop/imported/shot-0a1b2c3d.png",
            title="Screenshot dashboard",
            content_type="screenshot",
        ),
        seed_stdin_doc(
            conn, embedder, vault,
            source_kind="github", external_id="acme/widgets#7", title="Widget PR seven",
        ),
        seed_stdin_doc(
            conn, embedder, vault,
            source_kind="krisp", external_id="/srv/synthetic/krisp/call-1",
            title="Synthetic sync call",
        ),
    ]


def snapshot(vault: Path) -> dict[str, str]:
    """``{relative path: sha256}`` for every file (and symlink) under ``vault``."""
    out: dict[str, str] = {}
    for path in sorted(vault.rglob("*")):
        if path.is_file() or path.is_symlink():
            out[path.relative_to(vault).as_posix()] = hashlib.sha256(
                path.read_bytes()
            ).hexdigest()
    return out


def directories(vault: Path) -> set[str]:
    """Every directory under ``vault``, relative — catches a stray dir added or removed."""
    return {p.relative_to(vault).as_posix() for p in vault.rglob("*") if p.is_dir()}


def mirrors_of(vault: Path, doc_id: str) -> list[str]:
    """Every ``_ingested/`` file whose frontmatter id is ``doc_id``."""
    return sorted(
        p.relative_to(vault).as_posix()
        for p in (vault / "_ingested").rglob("*.md")
        if read_frontmatter_id(p) == doc_id
    )


def stored_paths(conn: psycopg.Connection[Any]) -> dict[str, str | None]:
    rows = conn.execute("SELECT id::text, vault_path FROM documents").fetchall()
    return {str(r[0]): r[1] for r in rows}
