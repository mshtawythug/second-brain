"""Tests that ``brain init`` auto-runs the search backfill when migration 009 is fresh."""
import hashlib
import json
import os
import uuid
from pathlib import Path

import psycopg
import pytest
from typer.testing import CliRunner

from brain import config as config_module
from brain.cli import app

TEST_DATABASE_URL = os.environ.get(
    "TEST_DATABASE_URL",
    "postgresql://brain:brain@localhost:5433/second_brain_test",
)


@pytest.fixture()
def isolated_dotenv(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Block .env file sources so delenv tests aren't undone by T1.0 setdefault."""
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(config_module, "_project_dotenv", lambda: tmp_path / "project.env")
    monkeypatch.setattr(
        config_module, "_brain_home_dotenv", lambda: tmp_path / "brain_home.env"
    )
    monkeypatch.delenv("BRAIN_HOME", raising=False)


def _wipe_db() -> None:
    with psycopg.connect(TEST_DATABASE_URL, autocommit=True) as conn:
        conn.execute("DROP SCHEMA public CASCADE; CREATE SCHEMA public;")


def _seed_legacy_chunk_with_email(conn) -> tuple[str, str]:
    """Insert a doc + one chunk in the post-009 schema with NULL search columns.

    Mirrors the state we'd see after migrating from a 008 snapshot to 009 on
    a populated DB: title_text/tags_text/search_extras are NULL until the
    backfill writes them.
    """
    title = "Outreach"
    tags = ["intro"]
    content = "Email person-b@example.com about the next session."
    h = hashlib.sha256(content.encode()).hexdigest()
    doc_row = conn.execute(
        """
        INSERT INTO documents (title, content, content_hash,
                               content_type, source_path, tags, metadata)
        VALUES (%s, %s, %s, 'note', NULL, %s, %s::jsonb)
        RETURNING id
        """,
        (title, content, h, tags, json.dumps({})),
    ).fetchone()
    assert doc_row is not None
    doc_id = str(doc_row[0])

    chunk_id = str(uuid.uuid4())
    conn.execute(
        """
        INSERT INTO chunks (id, document_id, chunk_index, content, embedding,
                            title_text, tags_text, search_extras)
        VALUES (%s, %s, 0, %s, NULL, NULL, NULL, NULL)
        """,
        (chunk_id, doc_id, content),
    )
    return doc_id, chunk_id


def test_init_runs_backfill_when_migration_009_is_freshly_applied(
    monkeypatch: pytest.MonkeyPatch,
    isolated_dotenv: None,
) -> None:
    """A DB at the 008 schema state — re-running init applies 009 + backfill."""
    monkeypatch.setenv("DATABASE_URL", TEST_DATABASE_URL)
    monkeypatch.delenv("BRAIN_EMBEDDER", raising=False)
    _wipe_db()

    # Bootstrap migrations once so we can observe the auto-run on a second
    # run-of-init that re-applies just 009.
    initial = CliRunner().invoke(app, ["init"])
    assert initial.exit_code == 0, initial.output

    with psycopg.connect(TEST_DATABASE_URL, autocommit=True) as conn:
        _seed_legacy_chunk_with_email(conn)
        # Verify pre-state — none of the new columns are populated.
        row = conn.execute(
            "SELECT title_text, tags_text, search_extras FROM chunks LIMIT 1"
        ).fetchone()
        assert row == (None, None, None)
        # Roll back migration 009 from schema_migrations so the next init
        # re-applies it. The schema itself (post-009 columns) is fine — the
        # migration's ADD COLUMN IF NOT EXISTS guards make re-application a
        # no-op for the column adds; only the DROP/CREATE of the tsv +
        # index actually re-runs.
        conn.execute(
            "DELETE FROM schema_migrations WHERE name = %s",
            ("009_chunks_weighted_tsv.sql",),
        )

    result = CliRunner().invoke(app, ["init"])

    assert result.exit_code == 0, result.output
    assert "applied 009_chunks_weighted_tsv.sql" in result.output
    # Backfill report line is printed only when 009 was newly applied.
    assert "backfill search Stage A:" in result.output
    assert "Stage A: 1 row(s)" in result.output
    assert "Stage B: 1 row(s)" in result.output
    assert "total chunks: 1" in result.output

    with psycopg.connect(TEST_DATABASE_URL, autocommit=True) as conn:
        row = conn.execute(
            "SELECT title_text, tags_text, search_extras FROM chunks LIMIT 1"
        ).fetchone()
        assert row is not None
        title_text, tags_text, search_extras = row
        assert title_text == "Outreach"
        assert tags_text == "intro"
        assert search_extras is not None
        assert "person-b" in search_extras.lower().split()
        assert "example-group" in search_extras.lower().split()


def test_init_skips_backfill_when_no_new_migrations(
    monkeypatch: pytest.MonkeyPatch,
    isolated_dotenv: None,
) -> None:
    """Subsequent ``brain init`` runs don't re-execute the backfill."""
    monkeypatch.setenv("DATABASE_URL", TEST_DATABASE_URL)
    monkeypatch.delenv("BRAIN_EMBEDDER", raising=False)
    _wipe_db()

    # First init applies migrations 001-009 fresh. Migration 009 is in the
    # newly-applied list, so the backfill DOES run on this pass — but on a
    # zero-chunk DB it is a no-op (Stage A 0 rows, Stage B 0 rows).
    first = CliRunner().invoke(app, ["init"])
    assert first.exit_code == 0, first.output
    assert "applied 009_chunks_weighted_tsv.sql" in first.output
    assert "backfill search Stage A:" in first.output

    # Second init — no migrations are newly applied, so the backfill must
    # NOT run again. The "backfill search" line must be absent from the
    # output.
    second = CliRunner().invoke(app, ["init"])
    assert second.exit_code == 0, second.output
    assert "no migrations to apply" in second.output
    assert "backfill search" not in second.output


def test_init_auto_backfill_handles_empty_db(
    monkeypatch: pytest.MonkeyPatch,
    isolated_dotenv: None,
) -> None:
    """Fresh DB — backfill runs on the first init but reports zeros."""
    monkeypatch.setenv("DATABASE_URL", TEST_DATABASE_URL)
    monkeypatch.delenv("BRAIN_EMBEDDER", raising=False)
    _wipe_db()

    result = CliRunner().invoke(app, ["init"])

    assert result.exit_code == 0, result.output
    assert "applied 009_chunks_weighted_tsv.sql" in result.output
    assert "Stage A: 0 row(s)" in result.output
    assert "Stage B: 0 row(s)" in result.output
    assert "total chunks: 0" in result.output
