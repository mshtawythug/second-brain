"""Backfill of ``chunks.title_text`` / ``tags_text`` / ``search_extras`` (migration 009)."""
import hashlib
import json
import os
import uuid

from typer.testing import CliRunner

from brain.backfill import backfill_search
from brain.cli import app

_TEST_DATABASE_URL = os.environ.get(
    "TEST_DATABASE_URL",
    "postgresql://brain:brain@localhost:5433/second_brain_test",
)


def _seed_doc_with_chunks(
    conn,
    *,
    title: str,
    tags: list[str] | None = None,
    chunk_contents: list[str],
) -> tuple[str, list[str]]:
    """Insert a document + N chunks directly, bypassing the ingest pipeline.

    The backfill targets pre-existing rows whose chunks were inserted before
    migration 009, so chunks land with NULL title_text / tags_text /
    search_extras (the exact bug condition the backfill fixes). Tests build
    that condition by hand here — using ``ingest_document`` would actively
    populate those columns, hiding the bug.
    """
    body = "\n\n".join(chunk_contents) or "body"
    h = hashlib.sha256(body.encode()).hexdigest()
    row = conn.execute(
        """
        INSERT INTO documents (title, content, content_hash,
                               content_type, source_path, tags, metadata)
        VALUES (%s, %s, %s, 'note', NULL, %s, %s::jsonb)
        RETURNING id
        """,
        (title, body, h, tags or [], json.dumps({})),
    ).fetchone()
    assert row is not None
    doc_id = str(row[0])

    chunk_ids: list[str] = []
    for idx, content in enumerate(chunk_contents):
        chunk_id = str(uuid.uuid4())
        conn.execute(
            """
            INSERT INTO chunks (id, document_id, chunk_index, content, embedding,
                                title_text, tags_text, search_extras)
            VALUES (%s, %s, %s, %s, NULL, NULL, NULL, NULL)
            """,
            (chunk_id, doc_id, idx, content),
        )
        chunk_ids.append(chunk_id)
    return doc_id, chunk_ids


def test_backfill_stage_a_denormalizes_title_tags(test_db):
    """Stage A copies documents.title and documents.tags onto every chunk."""
    _, chunk_ids = _seed_doc_with_chunks(
        test_db,
        title="Career Notes",
        tags=["interview", "prep"],
        chunk_contents=["chunk one", "chunk two"],
    )

    report = backfill_search.run(test_db)

    assert report.stage_a_rows == 2
    assert report.total_chunks == 2

    rows = test_db.execute(
        "SELECT title_text, tags_text FROM chunks WHERE id = ANY(%s::uuid[]) "
        "ORDER BY chunk_index",
        (chunk_ids,),
    ).fetchall()
    for title_text, tags_text in rows:
        assert title_text == "Career Notes"
        # Tags array_to_string preserves insertion order with single-space sep.
        assert tags_text == "interview prep"


def test_backfill_stage_b_computes_search_extras(test_db):
    """Stage B writes extracted sub-tokens for every chunk."""
    _, chunk_ids = _seed_doc_with_chunks(
        test_db,
        title="Email contacts",
        chunk_contents=["Reach person-b@example-group.com about the call."],
    )

    report = backfill_search.run(test_db)

    assert report.stage_b_rows == 1
    extras = test_db.execute(
        "SELECT search_extras FROM chunks WHERE id = %s", (chunk_ids[0],)
    ).fetchone()
    assert extras is not None
    extras_str = extras[0]
    assert extras_str is not None
    # Extractor preserves first-seen order; both components must appear.
    assert "person-b" in extras_str.lower().split()
    assert "example-group" in extras_str.lower().split()


def test_backfill_is_no_op_on_second_run(test_db):
    """Second run reports 0 rows updated in BOTH stages — convergence."""
    _seed_doc_with_chunks(
        test_db,
        title="Doc",
        tags=["alpha"],
        chunk_contents=["Hello team@example.com hello"],
    )

    first = backfill_search.run(test_db)
    assert first.stage_a_rows == 1
    assert first.stage_b_rows == 1

    second = backfill_search.run(test_db)
    assert second.stage_a_rows == 0
    assert second.stage_b_rows == 0
    assert second.total_chunks == 1


def test_backfill_recomputes_when_search_extras_diverged(test_db):
    """Per revision #5: stale ``search_extras`` is restored to the canonical value."""
    _, chunk_ids = _seed_doc_with_chunks(
        test_db,
        title="Doc",
        chunk_contents=["Reach person-b@example.com today."],
    )

    # First run — populates from scratch.
    first = backfill_search.run(test_db)
    assert first.stage_b_rows == 1
    canonical = test_db.execute(
        "SELECT search_extras FROM chunks WHERE id = %s", (chunk_ids[0],)
    ).fetchone()[0]
    assert canonical is not None and canonical != ""

    # Manually corrupt search_extras to a stale string.
    test_db.execute(
        "UPDATE chunks SET search_extras = %s WHERE id = %s",
        ("totally-stale-value", chunk_ids[0]),
    )

    # Re-run — Stage A is a no-op (title/tags still match), Stage B detects
    # the diff and overwrites with the canonical value.
    second = backfill_search.run(test_db)
    assert second.stage_a_rows == 0
    assert second.stage_b_rows == 1
    restored = test_db.execute(
        "SELECT search_extras FROM chunks WHERE id = %s", (chunk_ids[0],)
    ).fetchone()[0]
    assert restored == canonical


def test_backfill_handles_empty_corpus(test_db):
    """Fresh DB with 0 chunks — backfill is safe and reports zeros."""
    report = backfill_search.run(test_db)
    assert report.stage_a_rows == 0
    assert report.stage_b_rows == 0
    assert report.total_chunks == 0


def test_backfill_handles_chunk_without_url_or_email(test_db):
    """Chunks whose content has no extractable sub-tokens get search_extras=""

    The "compute and compare" loop has to write something the second-run
    convergence path expects — verify the empty-result path doesn't keep
    looping forever.
    """
    _, chunk_ids = _seed_doc_with_chunks(
        test_db,
        title="Plain",
        chunk_contents=["just a sentence with no urls or emails"],
    )

    first = backfill_search.run(test_db)
    # No URL/email/host — extracted sub-tokens are "". Storing NULL → "" is
    # treated as a no-op (the row's effective tsv would not change), so
    # stage_b_rows stays 0.
    assert first.stage_b_rows == 0

    extras = test_db.execute(
        "SELECT search_extras FROM chunks WHERE id = %s", (chunk_ids[0],)
    ).fetchone()[0]
    # We did NOT write to it — stays NULL. Coalesce inside the generated tsv
    # column handles the NULL safely.
    assert extras is None


def test_backfill_paginates_across_thousand_chunk_boundary(test_db):
    """More chunks than the page size — pagination loop covers every row.

    Indirectly verifies the keyset cursor advances correctly.
    """
    # Seed 1100 chunks across a few documents — bigger than the 1000-page
    # boundary. Each chunk gets a unique sub-token so we can verify by
    # rowcount.
    contents = [f"https://example{i}.io/page" for i in range(1100)]
    _, chunk_ids = _seed_doc_with_chunks(
        test_db,
        title="Big",
        chunk_contents=contents,
    )

    report = backfill_search.run(test_db)
    assert report.total_chunks == 1100
    assert report.stage_b_rows == 1100

    # Spot-check a chunk in the second page (after the keyset boundary).
    extras = test_db.execute(
        "SELECT search_extras FROM chunks WHERE id = %s", (chunk_ids[1050],)
    ).fetchone()[0]
    assert extras is not None
    assert "example1050" in extras


def test_backfill_search_cmd_outputs_report(monkeypatch, test_db):
    """CLI command exits 0 and prints the per-stage rowcounts."""
    _seed_doc_with_chunks(
        test_db,
        title="Doc",
        tags=["one"],
        chunk_contents=["See https://example.com/groups for details."],
    )

    monkeypatch.setenv("DATABASE_URL", _TEST_DATABASE_URL)
    result = CliRunner().invoke(app, ["backfill", "search"])

    assert result.exit_code == 0, result.output
    assert "Stage A (title/tags denorm): 1 rows updated" in result.output
    assert "Stage B (search_extras):     1 rows updated" in result.output
    assert "Total chunks:                1" in result.output


def test_backfill_search_cmd_no_op_after_convergence(monkeypatch, test_db):
    """Second CLI invocation reports 0/0 — backfill has converged."""
    _seed_doc_with_chunks(
        test_db,
        title="Doc",
        chunk_contents=["Email me@example.com"],
    )

    # First run via the underlying function so we can assert the second
    # CLI invocation sees a converged corpus.
    backfill_search.run(test_db)

    monkeypatch.setenv("DATABASE_URL", _TEST_DATABASE_URL)
    result = CliRunner().invoke(app, ["backfill", "search"])

    assert result.exit_code == 0, result.output
    assert "Stage A (title/tags denorm): 0 rows updated" in result.output
    assert "Stage B (search_extras):     0 rows updated" in result.output
    assert "Total chunks:                1" in result.output
