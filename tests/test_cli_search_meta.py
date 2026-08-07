"""`brain search` metadata surface: footer, facets panel, and --meta envelope.

This module is the CLI half of the backward-compatibility firewall. The bare
``--json`` list contract itself is asserted in
``tests/test_search_output_unchanged.py``; everything additive lives here.

All fixture data is synthetic.
"""
from __future__ import annotations

import io
import json
from typing import Any

import psycopg
import pytest
from rich.console import Console
from typer.testing import CliRunner

from brain.cli import app
from brain.facets import FacetBucket, SearchFacets
from brain.format_search import facets_renderable, search_meta_line
from brain.ingest import ExtractedDoc, ingest_document
from brain.search import SearchDiagnostics

# ---------------------------------------------------------------------------
# The pure formatter. Kept here rather than in a ninth module: these are the
# rendering branches of the same surface the CLI tests below exercise, and a
# terminal test cannot reach them all (a real corpus rarely has >8 tags, and
# the cached-embed suffix needs a warm LRU).
# ---------------------------------------------------------------------------


def test_meta_line_renders_every_phase() -> None:
    """The full footer, in the documented order."""
    # Arrange
    diag = SearchDiagnostics(
        fts_count=50, total_documents=544, embed_ms=5820.4,
        embed_cached=False, sql_ms=214.1, total_ms=6041.7,
    )

    # Act / Assert
    assert search_meta_line(diag, returned=3) == (
        "544 matched · 3 shown · embed 5820ms · sql 214ms · total 6042ms"
    )


def test_meta_line_marks_a_cached_embed() -> None:
    """A warm LRU hit is labelled, not silently reported as a 0 ms embed."""
    # Arrange
    diag = SearchDiagnostics(
        total_documents=544, embed_ms=0.2, embed_cached=True,
        sql_ms=198.0, total_ms=201.0,
    )

    # Act / Assert
    assert "embed 0ms (cached)" in search_meta_line(diag, returned=3)


def test_meta_line_includes_facets_segment_when_measured() -> None:
    """``--facets`` adds its own phase between sql and total."""
    # Arrange
    diag = SearchDiagnostics(
        total_documents=544, sql_ms=214.1, facets_ms=126.0, total_ms=6168.0,
    )

    # Act / Assert
    assert search_meta_line(diag, returned=3) == (
        "544 matched · 3 shown · sql 214ms · facets 126ms · total 6168ms"
    )


def test_meta_line_renders_unknown_total_as_a_question_mark() -> None:
    """A failed count must never be rendered as ``0 matched``."""
    # Arrange
    diag = SearchDiagnostics(total_documents=None, sql_ms=10.0, total_ms=12.0)

    # Act / Assert
    assert search_meta_line(diag, returned=3).startswith("? matched · 3 shown")


def test_facets_renderable_reports_the_truncated_remainder() -> None:
    """The ``(+N more)`` line is how the user learns tags were cut."""
    # Arrange
    facets = SearchFacets(
        source=(FacetBucket("krisp", 311),),
        content_type=(FacetBucket("transcript", 311),),
        tag=tuple(FacetBucket(f"tag-{i}", 10 - i) for i in range(8)),
        tag_truncated=153,
        total_documents=544,
    )

    # Act
    rendered = _render_to_text(facets_renderable(facets))

    # Assert — the short source/type columns padded; the tag column ran long.
    assert "(+153 more)" in rendered
    assert "krisp" in rendered and "transcript" in rendered
    assert "tag-7" in rendered


def test_facets_renderable_omits_the_remainder_when_nothing_was_cut() -> None:
    """No truncation, no ``(+N more)`` noise."""
    # Arrange
    facets = SearchFacets(
        source=(FacetBucket("manual", 3),),
        content_type=(FacetBucket("note", 3),),
        tag=(FacetBucket("planning", 3),),
        tag_truncated=0,
        total_documents=3,
    )

    # Act / Assert
    assert "more)" not in _render_to_text(facets_renderable(facets))


def _render_to_text(table: Any) -> str:
    """Render a Rich renderable to plain text for assertion."""
    console = Console(width=200, record=True, file=io.StringIO())
    console.print(table)
    return console.export_text()


def _seed(conn: psycopg.Connection[Any], embedder: Any, count: int = 3) -> None:
    """Ingest ``count`` synthetic documents all matching the word 'quarterly'.

    Bodies differ per document: ``documents.content_hash`` is UNIQUE, so
    identical bodies would dedup into one row.
    """
    for i in range(count):
        ingest_document(
            conn,
            embedder=embedder,
            doc=ExtractedDoc(
                title=f"Quarterly note {i}",
                content=f"The quarterly review covered budget and hiring {i}.",
                content_type="note",
                source_path=None,
                metadata={},
            ),
            source_kind="manual",
            source_external_id=f"manual:quarterly-{i}",
            tags=["planning"],
        )


def test_json_meta_envelope_shape(
    test_db: psycopg.Connection[Any],
    fake_embedder: Any,
    patch_embedder: Any,
) -> None:
    """RED-FIRST: ``--json --meta`` emits the documented envelope."""
    # Arrange
    _seed(test_db, fake_embedder)
    patch_embedder(fake_embedder)

    # Act
    result = CliRunner().invoke(
        app, ["search", "quarterly", "--fts-only", "--json", "--meta"]
    )

    # Assert
    assert result.exit_code == 0, result.output
    payload = json.loads(result.stdout)
    assert set(payload) == {
        "query",
        "total_documents",
        "returned",
        "fts_count",
        "timing_ms",
        "embed_cached",
        "fts_only",
        "facets",
        "results",
    }
    assert payload["query"] == "quarterly"
    assert payload["total_documents"] == 3
    assert payload["returned"] == len(payload["results"])
    assert set(payload["timing_ms"]) == {"embed", "sql", "facets", "total"}


def test_envelope_results_deep_equal_the_bare_list(
    test_db: psycopg.Connection[Any],
    fake_embedder: Any,
    patch_embedder: Any,
) -> None:
    """Opting into ``--meta`` must not change a single result object."""
    # Arrange
    _seed(test_db, fake_embedder)
    patch_embedder(fake_embedder)
    runner = CliRunner()

    # Act
    bare = runner.invoke(app, ["search", "quarterly", "--fts-only", "--json"])
    wrapped = runner.invoke(
        app, ["search", "quarterly", "--fts-only", "--json", "--meta"]
    )

    # Assert — every field matches. ``score`` is compared with a tolerance
    # because the recency boost decays from ``now``, so two invocations
    # milliseconds apart legitimately differ in the last few digits.
    assert bare.exit_code == 0 and wrapped.exit_code == 0
    bare_rows = json.loads(bare.stdout)
    wrapped_rows = json.loads(wrapped.stdout)["results"]
    assert len(wrapped_rows) == len(bare_rows)
    for wrapped_row, bare_row in zip(wrapped_rows, bare_rows, strict=True):
        assert set(wrapped_row) == set(bare_row)
        assert wrapped_row["score"] == pytest.approx(bare_row["score"], rel=1e-6)
        for key in set(bare_row) - {"score"}:
            assert wrapped_row[key] == bare_row[key]


def test_footer_goes_to_stderr_not_stdout(
    test_db: psycopg.Connection[Any],
    fake_embedder: Any,
    patch_embedder: Any,
) -> None:
    """NON-NEGOTIABLE: the footer must never pollute stdout."""
    # Arrange
    _seed(test_db, fake_embedder)
    patch_embedder(fake_embedder)

    # Act
    result = CliRunner().invoke(app, ["search", "quarterly", "--fts-only"])

    # Assert
    assert result.exit_code == 0, result.output
    assert "matched" not in result.stdout
    assert "matched" in result.stderr


def test_no_meta_suppresses_the_footer(
    test_db: psycopg.Connection[Any],
    fake_embedder: Any,
    patch_embedder: Any,
) -> None:
    """``--no-meta`` turns off the footer and the count query behind it."""
    # Arrange
    _seed(test_db, fake_embedder)
    patch_embedder(fake_embedder)

    # Act
    result = CliRunner().invoke(
        app, ["search", "quarterly", "--fts-only", "--no-meta"]
    )

    # Assert
    assert result.exit_code == 0, result.output
    assert "matched" not in result.stderr
    assert "Quarterly note" in result.stdout


def test_zero_results_still_prints_footer(
    test_db: psycopg.Connection[Any],  # noqa: ARG001 — an empty corpus is the point
    fake_embedder: Any,
    patch_embedder: Any,
) -> None:
    """'Your query matched nothing, and here is how long that took.'"""
    # Arrange
    patch_embedder(fake_embedder)

    # Act
    result = CliRunner().invoke(app, ["search", "nonexistentterm", "--fts-only"])

    # Assert
    assert result.exit_code == 0, result.output
    assert "(no results)" in result.stdout
    assert "0 matched · 0 shown" in result.stderr


def test_footer_omits_embed_segment_under_fts_only(
    test_db: psycopg.Connection[Any],
    fake_embedder: Any,
    patch_embedder: Any,
) -> None:
    """No embed ran, so no ``embed 0ms`` — that would imply a free embed."""
    # Arrange
    _seed(test_db, fake_embedder)
    patch_embedder(fake_embedder)

    # Act
    result = CliRunner().invoke(app, ["search", "quarterly", "--fts-only"])

    # Assert
    assert result.exit_code == 0, result.output
    assert "embed" not in result.stderr
    assert "sql" in result.stderr
    assert "total" in result.stderr


def test_footer_reports_embed_when_the_vector_leg_runs(
    test_db: psycopg.Connection[Any],
    fake_embedder: Any,
    patch_embedder: Any,
) -> None:
    """With the vector leg on, the embed phase is reported."""
    # Arrange
    _seed(test_db, fake_embedder)
    patch_embedder(fake_embedder)

    # Act
    result = CliRunner().invoke(app, ["search", "quarterly"])

    # Assert
    assert result.exit_code == 0, result.output
    assert "embed" in result.stderr


def test_facets_panel_goes_to_stderr(
    test_db: psycopg.Connection[Any],
    fake_embedder: Any,
    patch_embedder: Any,
) -> None:
    """The facet panel is metadata: stderr, never stdout."""
    # Arrange
    _seed(test_db, fake_embedder)
    patch_embedder(fake_embedder)

    # Act
    result = CliRunner().invoke(
        app, ["search", "quarterly", "--fts-only", "--facets"]
    )

    # Assert
    assert result.exit_code == 0, result.output
    assert "Content type" in result.stderr
    assert "planning" in result.stderr
    assert "Content type" not in result.stdout
    assert "facets" in result.stderr  # the footer gained its facets segment


def test_facets_implies_meta_under_json(
    test_db: psycopg.Connection[Any],
    fake_embedder: Any,
    patch_embedder: Any,
) -> None:
    """``--facets --json`` emits the envelope — facets have nowhere else to go."""
    # Arrange
    _seed(test_db, fake_embedder)
    patch_embedder(fake_embedder)

    # Act
    result = CliRunner().invoke(
        app, ["search", "quarterly", "--fts-only", "--json", "--facets"]
    )

    # Assert
    assert result.exit_code == 0, result.output
    payload = json.loads(result.stdout)
    assert isinstance(payload, dict)
    assert payload["facets"] is not None
    assert set(payload["facets"]) == {
        "source",
        "content_type",
        "tag",
        "tag_truncated",
    }
    assert {"value": "manual", "count": 3} in payload["facets"]["source"]


def test_facets_with_zero_matches_prints_the_empty_notice(
    test_db: psycopg.Connection[Any],  # noqa: ARG001 — an empty corpus is the point
    fake_embedder: Any,
    patch_embedder: Any,
) -> None:
    """No match set means no panel — an explicit notice, not an empty table."""
    # Arrange
    patch_embedder(fake_embedder)

    # Act
    result = CliRunner().invoke(
        app, ["search", "nonexistentterm", "--fts-only", "--facets"]
    )

    # Assert
    assert result.exit_code == 0, result.output
    assert "no facets (0 documents matched)" in result.stderr


def test_count_query_failure_degrades_gracefully(
    test_db: psycopg.Connection[Any],
    fake_embedder: Any,
    patch_embedder: Any,
    mocker: Any,
) -> None:
    """NON-NEGOTIABLE: a failed count costs the number, never the results."""
    # Arrange
    _seed(test_db, fake_embedder)
    patch_embedder(fake_embedder)
    mocker.patch(
        "brain.search._count_matching_documents",
        side_effect=psycopg.OperationalError("count backend is down"),
    )

    # Act
    result = CliRunner().invoke(app, ["search", "quarterly", "--fts-only"])

    # Assert
    assert result.exit_code == 0, result.output
    assert "Quarterly note" in result.stdout  # the table still rendered
    assert "? matched" in result.stderr
    assert "match count unavailable" in result.stderr


def test_duration_ms_is_persisted_for_the_cli_surface(
    test_db: psycopg.Connection[Any],
    fake_embedder: Any,
    patch_embedder: Any,
) -> None:
    """Migration 024's column is populated on new rows (F7 reads it later)."""
    # Arrange
    _seed(test_db, fake_embedder)
    patch_embedder(fake_embedder)

    # Act
    result = CliRunner().invoke(app, ["search", "quarterly", "--fts-only"])

    # Assert
    assert result.exit_code == 0, result.output
    row = test_db.execute(
        "SELECT duration_ms, source FROM search_queries ORDER BY at DESC LIMIT 1"
    ).fetchone()
    assert row is not None
    assert row[1] == "cli"
    assert isinstance(row[0], int) and row[0] >= 0
