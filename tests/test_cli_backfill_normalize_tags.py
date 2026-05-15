"""Integration tests for ``brain backfill normalize-tags``.

Real Postgres test DB + CliRunner. Each test seeds a doc with a
specifically-shaped tag list, runs the backfill subcommand, and verifies
both the DB column and (where applicable) the on-disk frontmatter
converge to the canonical form.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

import psycopg
import pytest
from typer.testing import CliRunner

from brain.cli import app
from brain.vault.frontmatter import dump_frontmatter, parse_frontmatter

TEST_DATABASE_URL = os.environ.get(
    "TEST_DATABASE_URL",
    "postgresql://brain:brain@localhost:5433/second_brain_test",
)


def _set_env(monkeypatch: pytest.MonkeyPatch, vault: Path) -> None:
    """Wire DATABASE_URL + BRAIN_VAULT_PATH for the CLI invocation."""
    monkeypatch.setenv("DATABASE_URL", TEST_DATABASE_URL)
    monkeypatch.setenv("BRAIN_VAULT_PATH", str(vault))


def _seed_doc_with_tags(
    conn: psycopg.Connection[Any],
    *,
    title: str,
    tags: list[str],
    vault_path_rel: str | None = None,
    kind: str = "vault",
) -> str:
    """Insert a documents row with raw (un-normalized) tags.

    We bypass :func:`apply_tags` (which would itself normalize) and write
    the raw input directly so the backfill has actual non-canonical state
    to repair.
    """
    row = conn.execute(
        """
        INSERT INTO documents
          (title, content, content_hash, content_type, tags, kind, vault_path)
        VALUES (%s, %s, %s, %s, %s, %s, %s)
        RETURNING id::text
        """,
        (
            title,
            f"body for {title}",
            f"hash-{title}-{','.join(tags)}",
            "note",
            tags,
            kind,
            vault_path_rel,
        ),
    ).fetchone()
    assert row is not None
    return str(row[0])


def _write_vault_file(
    vault: Path, vault_path_rel: str, fields: dict[str, Any], body: str
) -> Path:
    """Write a vault file at ``vault / vault_path_rel`` and return its absolute path."""
    target = vault / vault_path_rel
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(dump_frontmatter(fields, body), encoding="utf-8")
    return target


def _db_tags(conn: psycopg.Connection[Any], doc_id: str) -> list[str]:
    row = conn.execute(
        "SELECT tags FROM documents WHERE id = %s", (doc_id,)
    ).fetchone()
    assert row is not None
    return list(row[0] or [])


def _file_tags(path: Path) -> list[str]:
    fields, _ = parse_frontmatter(path.read_text(encoding="utf-8"))
    raw = fields.get("tags") or []
    assert isinstance(raw, list)
    return [str(t) for t in raw]


# ---------------------------------------------------------------------------
# (a) Already-canonical: zero changes.
# ---------------------------------------------------------------------------


def test_backfill_skips_already_canonical_doc(
    test_db: psycopg.Connection[Any],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    vault = tmp_path / "vault"
    vault.mkdir()
    file_path = _write_vault_file(
        vault, "n.md", {"id": "ignored", "title": "N", "tags": ["brandname"]}, "b\n"
    )
    doc_id = _seed_doc_with_tags(
        test_db, title="N", tags=["brandname"], vault_path_rel="n.md"
    )
    before_bytes = file_path.read_bytes()
    _set_env(monkeypatch, vault)

    result = CliRunner().invoke(app, ["backfill", "normalize-tags"])

    assert result.exit_code == 0, result.output
    assert "1 already-canonical skipped" in result.output
    assert "normalized 0 doc(s)" in result.output
    assert _db_tags(test_db, doc_id) == ["brandname"]
    assert file_path.read_bytes() == before_bytes


# ---------------------------------------------------------------------------
# (b) Mixed-case input: DB + file rewritten.
# ---------------------------------------------------------------------------


def test_backfill_rewrites_uppercase_in_db_and_file(
    test_db: psycopg.Connection[Any],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    vault = tmp_path / "vault"
    vault.mkdir()
    file_path = _write_vault_file(
        vault, "n.md", {"id": "ignored", "title": "N", "tags": ["BrandName"]}, "b\n"
    )
    doc_id = _seed_doc_with_tags(
        test_db, title="N", tags=["BrandName"], vault_path_rel="n.md"
    )
    _set_env(monkeypatch, vault)

    result = CliRunner().invoke(app, ["backfill", "normalize-tags"])

    assert result.exit_code == 0, result.output
    assert "normalized 1 doc(s)" in result.output
    assert "rewrote 1 file(s)" in result.output
    assert _db_tags(test_db, doc_id) == ["brandname"]
    assert _file_tags(file_path) == ["brandname"]


# ---------------------------------------------------------------------------
# (c) Casing duplicate collapses to a single canonical entry.
# ---------------------------------------------------------------------------


def test_backfill_collapses_case_duplicates(
    test_db: psycopg.Connection[Any],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    vault = tmp_path / "vault"
    vault.mkdir()
    file_path = _write_vault_file(
        vault,
        "n.md",
        {"id": "ignored", "title": "N", "tags": ["BrandName", "brandname"]},
        "b\n",
    )
    doc_id = _seed_doc_with_tags(
        test_db,
        title="N",
        tags=["BrandName", "brandname"],
        vault_path_rel="n.md",
    )
    _set_env(monkeypatch, vault)

    result = CliRunner().invoke(app, ["backfill", "normalize-tags"])

    assert result.exit_code == 0, result.output
    assert _db_tags(test_db, doc_id) == ["brandname"]
    assert _file_tags(file_path) == ["brandname"]


# ---------------------------------------------------------------------------
# (d) Dry-run: print plan, no writes.
# ---------------------------------------------------------------------------


def test_backfill_dry_run_makes_no_changes(
    test_db: psycopg.Connection[Any],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    vault = tmp_path / "vault"
    vault.mkdir()
    file_path = _write_vault_file(
        vault, "n.md", {"id": "ignored", "title": "N", "tags": ["BrandName"]}, "b\n"
    )
    doc_id = _seed_doc_with_tags(
        test_db, title="N", tags=["BrandName"], vault_path_rel="n.md"
    )
    before_bytes = file_path.read_bytes()
    _set_env(monkeypatch, vault)

    result = CliRunner().invoke(app, ["backfill", "normalize-tags", "--dry-run"])

    assert result.exit_code == 0, result.output
    assert "would normalize" in result.output
    assert "[BrandName]" in result.output or "['BrandName']" in result.output
    assert "['brandname']" in result.output
    # No DB or file write happened.
    assert _db_tags(test_db, doc_id) == ["BrandName"]
    assert file_path.read_bytes() == before_bytes


# ---------------------------------------------------------------------------
# (e) vault_path set but file missing → DB updated + warn.
# ---------------------------------------------------------------------------


def test_backfill_vault_path_set_but_file_missing_warns(
    test_db: psycopg.Connection[Any],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    vault = tmp_path / "vault"
    vault.mkdir()
    # No file on disk — but DB row carries a vault_path.
    doc_id = _seed_doc_with_tags(
        test_db,
        title="N",
        tags=["BrandName"],
        vault_path_rel="missing.md",
    )
    _set_env(monkeypatch, vault)

    result = CliRunner().invoke(app, ["backfill", "normalize-tags"])

    assert result.exit_code == 0, result.output
    # DB updated.
    assert _db_tags(test_db, doc_id) == ["brandname"]
    # Warn surfaced on stderr (CliRunner merges by default — assert by content).
    assert "1 file-missing skipped" in result.output
    assert "file missing on disk" in result.output


# ---------------------------------------------------------------------------
# (f) --mapping JSON collapses synonyms before lowercase normalization.
# ---------------------------------------------------------------------------


def test_backfill_mapping_collapses_synonyms(
    test_db: psycopg.Connection[Any],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    vault = tmp_path / "vault"
    vault.mkdir()
    file_path = _write_vault_file(
        vault,
        "n.md",
        {"id": "ignored", "title": "N", "tags": ["recruiters", "Recruiter"]},
        "b\n",
    )
    doc_id = _seed_doc_with_tags(
        test_db,
        title="N",
        tags=["recruiters", "Recruiter"],
        vault_path_rel="n.md",
    )
    mapping_path = tmp_path / "mapping.json"
    mapping_path.write_text(json.dumps({"recruiters": "recruiter"}), encoding="utf-8")
    _set_env(monkeypatch, vault)

    result = CliRunner().invoke(
        app, ["backfill", "normalize-tags", "--mapping", str(mapping_path)]
    )

    assert result.exit_code == 0, result.output
    assert _db_tags(test_db, doc_id) == ["recruiter"]
    assert _file_tags(file_path) == ["recruiter"]


# ---------------------------------------------------------------------------
# (g) Idempotent: a second run after convergence is a no-op.
# ---------------------------------------------------------------------------


def test_backfill_is_idempotent_on_second_run(
    test_db: psycopg.Connection[Any],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    vault = tmp_path / "vault"
    vault.mkdir()
    file_path = _write_vault_file(
        vault, "n.md", {"id": "ignored", "title": "N", "tags": ["BrandName"]}, "b\n"
    )
    _seed_doc_with_tags(
        test_db, title="N", tags=["BrandName"], vault_path_rel="n.md"
    )
    _set_env(monkeypatch, vault)
    runner = CliRunner()

    # First run normalizes.
    first = runner.invoke(app, ["backfill", "normalize-tags"])
    assert first.exit_code == 0, first.output
    assert "normalized 1 doc(s)" in first.output

    bytes_after_first = file_path.read_bytes()

    # Second run: nothing left to do.
    second = runner.invoke(app, ["backfill", "normalize-tags"])
    assert second.exit_code == 0, second.output
    assert "normalized 0 doc(s)" in second.output
    assert "rewrote 0 file(s)" in second.output
    assert "1 already-canonical skipped" in second.output
    assert file_path.read_bytes() == bytes_after_first


# ---------------------------------------------------------------------------
# Sanity: the command is registered and its --help text mentions the rule.
# ---------------------------------------------------------------------------


def test_backfill_normalize_tags_command_help_documents_canonical_rule() -> None:
    result = CliRunner().invoke(app, ["backfill", "normalize-tags", "--help"])
    assert result.exit_code == 0
    assert "Lowercase + dedupe" in result.output
    assert "--mapping" in result.output
    assert "--dry-run" in result.output
