"""Tests for the ``brain enrich`` CLI command (Wave Q1-D).

Covers both modes (``--backfill`` and ``--krisp-action-items``) as well
as the mutually-exclusive flag validation.
"""
from __future__ import annotations

import os
from dataclasses import dataclass

import psycopg
import pytest
from typer.testing import CliRunner

from brain.cli import app
from brain.enrichment import SummaryResult
from brain.errors import EnrichmentError, OllamaUnavailable
from brain.ingest import ExtractedDoc, ingest_document

TEST_DATABASE_URL = os.environ.get(
    "TEST_DATABASE_URL",
    "postgresql://brain:brain@localhost:5433/second_brain_test",
)


@dataclass
class _FakeEnricher:
    """Same in-memory enricher used by test_ingest_enrich_hook."""

    model: str = "llama3.1:8b"
    summary_text: str = "Canned test summary."
    raises: list[BaseException] | None = None
    calls: int = 0

    def count_tokens(self, text: str) -> int:
        return max(1, len(text) // 4)

    def summarize(self, title: str, content: str) -> SummaryResult:
        idx = self.calls
        self.calls += 1
        if self.raises is not None and idx < len(self.raises):
            exc = self.raises[idx]
            if exc is not None:
                raise exc
        return SummaryResult(summary=f"{self.summary_text} ({title})", model=self.model)


def _seed_docs(
    test_db: psycopg.Connection,
    fake_embedder: object,
    *,
    count: int,
    pre_summary_count: int = 0,
) -> list[str]:
    """Seed ``count`` manual docs; pre-populate ``summary`` on the first
    ``pre_summary_count`` of them so backfill skips those."""
    ids = []
    for i in range(count):
        # body must exceed 50 token min-tokens to trigger enrichment
        body = f"This is the body of doc {i}. " * 20
        result = ingest_document(
            test_db,
            embedder=fake_embedder,  # type: ignore[arg-type]
            doc=ExtractedDoc(
                title=f"Doc {i}",
                content=body,
                content_type="note",
                source_path=None,
                metadata={},
            ),
            source_kind="manual",
        )
        assert result.document_id is not None
        ids.append(result.document_id)
    # Pre-populate summaries on the first N rows with the SAME model the
    # FakeEnricher uses (``llama3.1:8b``) so the model-upgrade logic
    # treats them as up-to-date and the backfill skips them.
    for doc_id in ids[:pre_summary_count]:
        test_db.execute(
            "UPDATE documents SET summary='pre', summary_model='llama3.1:8b', "
            "summary_at=NOW() WHERE id=%s",
            (doc_id,),
        )
    return ids


def _patch_enricher(monkeypatch: pytest.MonkeyPatch, enricher: object) -> None:
    monkeypatch.setenv("DATABASE_URL", TEST_DATABASE_URL)
    monkeypatch.setattr("brain.cli._build_enricher", lambda cfg: enricher)


# ---------------------------------------------------------------------------
# --backfill
# ---------------------------------------------------------------------------


def test_brain_enrich_backfill_picks_up_null_rows(
    monkeypatch: pytest.MonkeyPatch,
    test_db: psycopg.Connection,
    fake_embedder: object,
) -> None:
    _seed_docs(test_db, fake_embedder, count=3, pre_summary_count=1)
    enricher = _FakeEnricher()
    _patch_enricher(monkeypatch, enricher)

    result = CliRunner().invoke(app, ["enrich", "--backfill"])
    assert result.exit_code == 0, result.output
    assert enricher.calls == 2  # only the two NULL rows

    row = test_db.execute(
        "SELECT count(*) FROM documents WHERE summary IS NULL"
    ).fetchone()
    assert row is not None
    assert row[0] == 0


def test_brain_enrich_backfill_limit(
    monkeypatch: pytest.MonkeyPatch,
    test_db: psycopg.Connection,
    fake_embedder: object,
) -> None:
    _seed_docs(test_db, fake_embedder, count=5)
    enricher = _FakeEnricher()
    _patch_enricher(monkeypatch, enricher)

    result = CliRunner().invoke(app, ["enrich", "--backfill", "--limit", "2"])
    assert result.exit_code == 0, result.output
    assert enricher.calls == 2

    row = test_db.execute(
        "SELECT count(*) FROM documents WHERE summary IS NOT NULL"
    ).fetchone()
    assert row is not None
    assert row[0] == 2


def test_brain_enrich_backfill_idempotent_when_all_enriched(
    monkeypatch: pytest.MonkeyPatch,
    test_db: psycopg.Connection,
    fake_embedder: object,
) -> None:
    _seed_docs(test_db, fake_embedder, count=2, pre_summary_count=2)
    enricher = _FakeEnricher()
    _patch_enricher(monkeypatch, enricher)
    result = CliRunner().invoke(app, ["enrich", "--backfill"])
    assert result.exit_code == 0, result.output
    assert "nothing to enrich" in result.output
    assert enricher.calls == 0


def test_brain_enrich_backfill_ollama_unavailable_first_row_exits_1(
    monkeypatch: pytest.MonkeyPatch,
    test_db: psycopg.Connection,
    fake_embedder: object,
) -> None:
    _seed_docs(test_db, fake_embedder, count=3)
    enricher = _FakeEnricher(raises=[OllamaUnavailable("refused")])
    _patch_enricher(monkeypatch, enricher)
    result = CliRunner().invoke(app, ["enrich", "--backfill"])
    assert result.exit_code == 1
    combined = result.output + (
        result.stderr if hasattr(result, "stderr") else ""
    )
    assert "Ollama unavailable" in combined


def test_brain_enrich_backfill_re_enriches_after_model_upgrade(
    monkeypatch: pytest.MonkeyPatch,
    test_db: psycopg.Connection,
    fake_embedder: object,
) -> None:
    """Codex finding 1 (HIGH): a row whose summary_model differs from the
    current BRAIN_ENRICH_MODEL must be re-enriched by ``--backfill``."""
    # Seed one doc already enriched by the OLD model.
    doc_ids = _seed_docs(test_db, fake_embedder, count=1)
    test_db.execute(
        "UPDATE documents SET summary='OLD summary', "
        "summary_model='llama3.1:8b', summary_at=NOW() WHERE id=%s",
        (doc_ids[0],),
    )

    # Switch to a NEW model and run backfill.
    enricher = _FakeEnricher(
        model="llama3.2:8b", summary_text="NEW summary"
    )
    _patch_enricher(monkeypatch, enricher)

    result = CliRunner().invoke(app, ["enrich", "--backfill"])
    assert result.exit_code == 0, result.output
    # The doc had a non-NULL summary but with the OLD model, so it must
    # be re-enriched.
    assert enricher.calls == 1
    row = test_db.execute(
        "SELECT summary, summary_model FROM documents WHERE id=%s",
        (doc_ids[0],),
    ).fetchone()
    assert row is not None
    assert row[0] == f"NEW summary ({_get_title(test_db, doc_ids[0])})"
    assert row[1] == "llama3.2:8b"


def _get_title(test_db: psycopg.Connection, doc_id: str) -> str:
    row = test_db.execute(
        "SELECT title FROM documents WHERE id=%s", (doc_id,)
    ).fetchone()
    assert row is not None
    return str(row[0])


def test_brain_enrich_backfill_skips_rows_with_current_model(
    monkeypatch: pytest.MonkeyPatch,
    test_db: psycopg.Connection,
    fake_embedder: object,
) -> None:
    """Idempotency — rows already enriched by the SAME model are skipped."""
    doc_ids = _seed_docs(test_db, fake_embedder, count=1)
    test_db.execute(
        "UPDATE documents SET summary='current', summary_model='llama3.1:8b', "
        "summary_at=NOW() WHERE id=%s",
        (doc_ids[0],),
    )
    enricher = _FakeEnricher(model="llama3.1:8b")
    _patch_enricher(monkeypatch, enricher)
    result = CliRunner().invoke(app, ["enrich", "--backfill"])
    assert result.exit_code == 0, result.output
    assert "nothing to enrich" in result.output
    assert enricher.calls == 0


def test_brain_enrich_backfill_partial_failure_continues(
    monkeypatch: pytest.MonkeyPatch,
    test_db: psycopg.Connection,
    fake_embedder: object,
) -> None:
    """Row 1 succeeds, row 2 fails with EnrichmentError, row 3 succeeds.
    Final exit code 0, one row stays NULL, two rows summarized."""
    _seed_docs(test_db, fake_embedder, count=3)
    enricher = _FakeEnricher(
        raises=[None, EnrichmentError("bad json"), None]
    )
    _patch_enricher(monkeypatch, enricher)
    result = CliRunner().invoke(app, ["enrich", "--backfill"])
    assert result.exit_code == 0, result.output
    assert enricher.calls == 3
    row = test_db.execute(
        "SELECT count(*) FROM documents WHERE summary IS NOT NULL"
    ).fetchone()
    assert row is not None
    assert row[0] == 2


# ---------------------------------------------------------------------------
# Mode validation
# ---------------------------------------------------------------------------


def test_brain_enrich_no_mode_flag_errors(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("DATABASE_URL", TEST_DATABASE_URL)
    result = CliRunner().invoke(app, ["enrich"])
    assert result.exit_code != 0
    combined = (
        result.output + result.stderr
        if hasattr(result, "stderr") else result.output
    )
    assert "expected --backfill" in combined


def test_brain_enrich_both_modes_errors(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("DATABASE_URL", TEST_DATABASE_URL)
    result = CliRunner().invoke(
        app, ["enrich", "--backfill", "--krisp-action-items"]
    )
    assert result.exit_code != 0


# ---------------------------------------------------------------------------
# --krisp-action-items — deferred-fetch pattern
# ---------------------------------------------------------------------------


def test_brain_enrich_krisp_action_items_names_real_mcp_tools(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The output must name the actual Krisp MCP tools per
    ``docs/specs/2026-04-24-second-brain-design.md`` and MUST NOT invent
    parameter syntax for any of them (Codex stop-time finding 2x).

    The CLI cannot know the live MCP parameter schema, so it points the
    agent at the tools + provides a plain-English window. The agent calls
    the tools with whatever signature its loaded MCP exposes.
    """
    import re

    monkeypatch.setenv("DATABASE_URL", TEST_DATABASE_URL)
    result = CliRunner().invoke(
        app, ["enrich", "--krisp-action-items", "--since", "7"]
    )
    assert result.exit_code == 0, result.output
    # Both real Krisp tools named explicitly so the agent picks the one
    # its MCP surface exposes.
    assert "mcp__claude_ai_Krisp__search_meetings" in result.output
    assert "mcp__claude_ai_Krisp__list_activities" in result.output
    assert "mcp__claude_ai_Krisp__get_multiple_documents" in result.output
    # Speculative tool name from the original plan must NOT appear.
    assert "list_action_items" not in result.output
    # No invented kwargs — the CLI never knows the real signature.
    assert "start_date=" not in result.output
    assert "end_date=" not in result.output
    assert "query=" not in result.output
    # Concrete ISO 8601 date for the lookback window, as a plain string the
    # agent can hand to whatever parameter the live tool exposes.
    iso_re = re.compile(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}")
    assert iso_re.search(result.output), (
        f"expected concrete ISO date for lookback window:\n{result.output}"
    )
    # The ingest-stdin recipe — which IS owned by this CLI — keeps its
    # exact flag shape so the agent can copy-paste.
    assert "brain ingest-stdin" in result.output
    assert "--source krisp" in result.output
    assert "--content-type krisp_action_items" in result.output
    assert "parent_meeting_external_id" in result.output
    assert "--action-items" in result.output  # external-id suffix convention


def test_brain_enrich_krisp_action_items_with_source_id_renders_hint(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``--source-id`` flows through as a plain-English scope hint — no
    invented MCP kwarg."""
    monkeypatch.setenv("DATABASE_URL", TEST_DATABASE_URL)
    result = CliRunner().invoke(
        app, ["enrich", "--krisp-action-items", "--source-id", "meeting-42"]
    )
    assert result.exit_code == 0, result.output
    assert "meeting-42" in result.output
    assert "mcp__claude_ai_Krisp__search_meetings" in result.output


def test_brain_enrich_krisp_action_items_no_filters(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """No filters → still emit the named-tools handoff, just without a
    date window or meeting-id hint."""
    monkeypatch.setenv("DATABASE_URL", TEST_DATABASE_URL)
    result = CliRunner().invoke(app, ["enrich", "--krisp-action-items"])
    assert result.exit_code == 0, result.output
    assert "mcp__claude_ai_Krisp__search_meetings" in result.output
    assert "mcp__claude_ai_Krisp__get_multiple_documents" in result.output
    # No lookback window text when --since omitted.
    assert " since " not in result.output
    # And no invented kwargs.
    assert "start_date=" not in result.output
