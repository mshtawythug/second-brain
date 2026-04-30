"""Backfill of legacy markdown documents missing a ``source_id``."""
import hashlib
import json
import os

from typer.testing import CliRunner

from brain.backfill import backfill_source_rows
from brain.cli import app

_TEST_DATABASE_URL = os.environ.get(
    "TEST_DATABASE_URL",
    "postgresql://brain:brain@localhost:5433/second_brain_test",
)


def _seed_doc(
    conn,
    *,
    title: str,
    content: str,
    content_type: str = "markdown",
    source_path: str | None,
    source_id: str | None = None,
) -> str:
    """Insert one ``documents`` row directly, bypassing the ingest pipeline.

    The backfill targets pre-existing rows whose ingest predates the
    manual-source default; using the live ingest pipeline here would
    actively prevent us from constructing the bug condition. So tests
    set up the rows by hand with explicit ``source_id`` (NULL or not).
    """
    h = hashlib.sha256(content.encode()).hexdigest()
    row = conn.execute(
        """
        INSERT INTO documents (source_id, title, content, content_hash,
                               content_type, source_path, tags, metadata)
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
        RETURNING id
        """,
        (
            source_id,
            title,
            content,
            h,
            content_type,
            source_path,
            [],
            json.dumps({}),
        ),
    ).fetchone()
    assert row is not None
    return str(row[0])


def test_backfill_creates_manual_source_for_each_legacy_doc(test_db):
    a = _seed_doc(
        test_db, title="A", content="alpha", source_path="/legacy/a.md"
    )
    b = _seed_doc(
        test_db, title="B", content="bravo", source_path="/legacy/b.md"
    )

    report = backfill_source_rows(test_db)

    assert report.dry_run is False
    assert report.candidates == 2
    assert report.documents_updated == 2
    assert report.sources_created == 2

    rows = test_db.execute(
        "SELECT d.id, s.kind, s.external_id "
        "FROM documents d JOIN sources s ON s.id = d.source_id "
        "WHERE d.id = ANY(%s::uuid[]) ORDER BY d.id",
        ([a, b],),
    ).fetchall()
    assert len(rows) == 2
    for _, kind, external_id in rows:
        assert kind == "manual"
        assert external_id in {"/legacy/a.md", "/legacy/b.md"}


def test_backfill_dedups_source_rows_by_external_id(test_db):
    """Two legacy docs sharing a ``source_path`` collapse onto one source row.

    Defensive — the live ingest pipeline already dedups by ``source_path``,
    but a hand-curated DB or restored snapshot can carry duplicates.
    """
    shared = "/legacy/duplicate.md"
    a = _seed_doc(
        test_db, title="A", content="alpha", source_path=shared
    )
    b = _seed_doc(
        test_db, title="B", content="bravo", source_path=shared
    )

    report = backfill_source_rows(test_db)

    assert report.documents_updated == 2
    assert report.sources_created == 1

    src_ids = test_db.execute(
        "SELECT source_id FROM documents WHERE id = ANY(%s::uuid[])",
        ([a, b],),
    ).fetchall()
    assert {str(r[0]) for r in src_ids} == {str(src_ids[0][0])}


def test_backfill_is_idempotent(test_db):
    """A second run is a no-op — the WHERE filter excludes already-fixed rows."""
    _seed_doc(
        test_db, title="A", content="alpha", source_path="/legacy/a.md"
    )

    first = backfill_source_rows(test_db)
    assert first.documents_updated == 1

    second = backfill_source_rows(test_db)
    assert second.candidates == 0
    assert second.documents_updated == 0
    assert second.sources_created == 0


def test_backfill_skips_non_markdown_and_stdin_rows(test_db):
    """Non-markdown content_types and stdin (NULL ``source_path``) rows are
    intentionally skipped — only markdown file ingests get the manual default."""
    pdf_id = _seed_doc(
        test_db,
        title="PDF",
        content="pdf body",
        content_type="pdf",
        source_path="/legacy/x.pdf",
    )
    stdin_id = _seed_doc(
        test_db,
        title="Krisp",
        content="stdin body",
        content_type="transcript",
        source_path=None,
    )
    keeper_id = _seed_doc(
        test_db,
        title="MD",
        content="md body",
        source_path="/legacy/x.md",
    )

    report = backfill_source_rows(test_db)
    assert report.candidates == 1
    assert report.documents_updated == 1

    rows = test_db.execute(
        "SELECT id, source_id FROM documents WHERE id = ANY(%s::uuid[])",
        ([pdf_id, stdin_id, keeper_id],),
    ).fetchall()
    by_id = {str(r[0]): r[1] for r in rows}
    assert by_id[pdf_id] is None
    assert by_id[stdin_id] is None
    assert by_id[keeper_id] is not None


def test_backfill_dry_run_writes_nothing(test_db):
    """``commit=False`` reports candidate counts but writes nothing."""
    doc_id = _seed_doc(
        test_db, title="A", content="alpha", source_path="/legacy/a.md"
    )

    report = backfill_source_rows(test_db, commit=False)

    assert report.dry_run is True
    assert report.candidates == 1
    assert report.documents_updated == 0
    assert report.sources_created == 0

    src = test_db.execute(
        "SELECT source_id FROM documents WHERE id = %s", (doc_id,)
    ).fetchone()
    assert src[0] is None
    sources_count = test_db.execute(
        "SELECT count(*) FROM sources WHERE kind = 'manual'"
    ).fetchone()[0]
    assert sources_count == 0


def test_backfill_reuses_existing_manual_source_row(test_db):
    """If a ``(manual, source_path)`` source row already exists (e.g. a
    teammate ingested the same file post-fix), the backfill reuses it
    instead of inserting a duplicate."""
    pre = test_db.execute(
        "INSERT INTO sources (kind, external_id, metadata) "
        "VALUES ('manual', '/legacy/a.md', %s::jsonb) RETURNING id",
        (json.dumps({}),),
    ).fetchone()
    assert pre is not None
    existing_source_id = str(pre[0])

    doc_id = _seed_doc(
        test_db, title="A", content="alpha", source_path="/legacy/a.md"
    )

    report = backfill_source_rows(test_db)
    assert report.documents_updated == 1
    assert report.sources_created == 0

    new_source_id = test_db.execute(
        "SELECT source_id FROM documents WHERE id = %s", (doc_id,)
    ).fetchone()[0]
    assert str(new_source_id) == existing_source_id


def test_backfill_report_with_no_candidates(test_db):
    """Empty result set returns zero counts — used as the CLI's "nothing to
    do" signal."""
    report = backfill_source_rows(test_db)
    assert report.candidates == 0
    assert report.documents_updated == 0
    assert report.sources_created == 0
    assert report.dry_run is False


def test_cli_backfill_source_rows(test_db, monkeypatch):
    """End-to-end smoke test for ``brain backfill source-rows``.

    The CLI command opens its own DB connection from the env var, so we
    set ``DATABASE_URL`` and let it run against the test schema set up by
    the ``test_db`` fixture."""
    _seed_doc(
        test_db, title="A", content="alpha", source_path="/legacy/a.md"
    )
    _seed_doc(
        test_db, title="B", content="bravo", source_path="/legacy/b.md"
    )

    monkeypatch.setenv("DATABASE_URL", _TEST_DATABASE_URL)
    runner = CliRunner()

    result = runner.invoke(app, ["backfill", "source-rows", "--dry-run"])
    assert result.exit_code == 0, result.output
    assert "would backfill 2" in result.output

    result = runner.invoke(app, ["backfill", "source-rows"])
    assert result.exit_code == 0, result.output
    assert "backfilled 2 document(s)" in result.output
    assert "created 2 new manual source row(s)" in result.output
    assert "brain vault export" in result.output

    result = runner.invoke(app, ["backfill", "source-rows"])
    assert result.exit_code == 0, result.output
    assert "nothing to backfill" in result.output


def test_cli_backfill_help_text_mentions_export(monkeypatch):
    """The success path tells the user to re-export the vault — surface that
    same hint in ``--help`` so a user reading docs first sees it."""
    runner = CliRunner()
    result = runner.invoke(app, ["backfill", "source-rows", "--help"])
    assert result.exit_code == 0
    assert "re-export the vault" in result.output
