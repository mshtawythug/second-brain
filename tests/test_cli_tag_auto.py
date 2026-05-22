"""Tests for ``brain tag --auto`` (Wave Q1-D 3.2)."""
from __future__ import annotations

import os
from dataclasses import dataclass

import psycopg
import pytest
from typer.testing import CliRunner

from brain.cli import app
from brain.enrichment import TagProposal
from brain.errors import OllamaUnavailable
from brain.ingest import ExtractedDoc, ingest_document

TEST_DATABASE_URL = os.environ.get(
    "TEST_DATABASE_URL",
    "postgresql://brain:brain@localhost:5434/second_brain_test",
)


@dataclass
class _FakeEnricher:
    """Stand-in for :class:`brain.enrichment.OllamaEnricher` — only
    ``propose_tags`` is exercised; ``count_tokens`` / ``summarize`` are
    untouched by the auto-tag path."""

    proposal: TagProposal | None = None
    raise_exc: BaseException | None = None
    calls: int = 0

    def propose_tags(
        self,
        *,
        title: str,
        summary: str,
        existing_vocab: list[str],
        current_tags: list[str],
        max_new: int = 1,
    ) -> TagProposal:
        self.calls += 1
        if self.raise_exc is not None:
            raise self.raise_exc
        assert self.proposal is not None
        return self.proposal


def _seed(
    test_db: psycopg.Connection,
    fake_embedder: object,
    *,
    summary: str | None,
    tags: list[str] | None = None,
) -> str:
    result = ingest_document(
        test_db,
        embedder=fake_embedder,  # type: ignore[arg-type]
        doc=ExtractedDoc(
            title="A document",
            content="Body content for the auto-tag test. " * 10,
            content_type="note",
            source_path=None,
            metadata={},
        ),
        source_kind="manual",
        tags=tags or [],
    )
    assert result.document_id is not None
    if summary is not None:
        test_db.execute(
            "UPDATE documents SET summary=%s, summary_model='llama3.1:8b', "
            "summary_at=NOW() WHERE id=%s",
            (summary, result.document_id),
        )
    return result.document_id


def _patch(
    monkeypatch: pytest.MonkeyPatch, enricher: object
) -> None:
    monkeypatch.setenv("DATABASE_URL", TEST_DATABASE_URL)
    monkeypatch.setattr("brain.cli._build_enricher", lambda cfg: enricher)


def _tags_of(conn: psycopg.Connection, doc_id: str) -> list[str]:
    row = conn.execute(
        "SELECT tags FROM documents WHERE id=%s", (doc_id,)
    ).fetchone()
    assert row is not None
    return list(row[0] or [])


# ---------------------------------------------------------------------------
# Happy paths
# ---------------------------------------------------------------------------


def test_brain_tag_auto_accept_all_applies_every_proposed(
    monkeypatch: pytest.MonkeyPatch,
    test_db: psycopg.Connection,
    fake_embedder: object,
) -> None:
    doc_id = _seed(test_db, fake_embedder, summary="A short summary.")
    enricher = _FakeEnricher(
        proposal=TagProposal(existing=["interview-prep"], new=["person-a"])
    )
    _patch(monkeypatch, enricher)

    result = CliRunner().invoke(
        app, ["tag", doc_id, "--auto", "--accept-all"]
    )
    assert result.exit_code == 0, result.output
    assert enricher.calls == 1
    tags = _tags_of(test_db, doc_id)
    assert "interview-prep" in tags
    assert "person-a" in tags


def test_brain_tag_auto_interactive_accepts_with_a_input(
    monkeypatch: pytest.MonkeyPatch,
    test_db: psycopg.Connection,
    fake_embedder: object,
) -> None:
    doc_id = _seed(test_db, fake_embedder, summary="A short summary.")
    enricher = _FakeEnricher(
        proposal=TagProposal(existing=["alpha"], new=["beta"])
    )
    _patch(monkeypatch, enricher)
    result = CliRunner().invoke(app, ["tag", doc_id, "--auto"], input="a\n")
    assert result.exit_code == 0, result.output
    assert "alpha" in _tags_of(test_db, doc_id)
    assert "beta" in _tags_of(test_db, doc_id)


def test_brain_tag_auto_interactive_rejects_with_r_input(
    monkeypatch: pytest.MonkeyPatch,
    test_db: psycopg.Connection,
    fake_embedder: object,
) -> None:
    doc_id = _seed(test_db, fake_embedder, summary="A short summary.")
    enricher = _FakeEnricher(
        proposal=TagProposal(existing=["alpha"], new=["beta"])
    )
    _patch(monkeypatch, enricher)
    result = CliRunner().invoke(app, ["tag", doc_id, "--auto"], input="r\n")
    assert result.exit_code == 0, result.output
    assert _tags_of(test_db, doc_id) == []


def test_brain_tag_auto_some_mode_accepts_subset(
    monkeypatch: pytest.MonkeyPatch,
    test_db: psycopg.Connection,
    fake_embedder: object,
) -> None:
    doc_id = _seed(test_db, fake_embedder, summary="A short summary.")
    enricher = _FakeEnricher(
        proposal=TagProposal(existing=["alpha", "gamma"], new=["beta"])
    )
    _patch(monkeypatch, enricher)
    # Order of prompt: existing then new. Accept "alpha", reject "gamma" + "beta".
    result = CliRunner().invoke(
        app, ["tag", doc_id, "--auto"], input="s\ny\nn\nn\n"
    )
    assert result.exit_code == 0, result.output
    tags = _tags_of(test_db, doc_id)
    assert "alpha" in tags
    assert "gamma" not in tags
    assert "beta" not in tags


# ---------------------------------------------------------------------------
# Error paths
# ---------------------------------------------------------------------------


def test_brain_tag_auto_without_summary_errors(
    monkeypatch: pytest.MonkeyPatch,
    test_db: psycopg.Connection,
    fake_embedder: object,
) -> None:
    doc_id = _seed(test_db, fake_embedder, summary=None)
    enricher = _FakeEnricher(
        proposal=TagProposal(existing=["x"], new=[])
    )
    _patch(monkeypatch, enricher)
    result = CliRunner().invoke(
        app, ["tag", doc_id, "--auto", "--accept-all"]
    )
    assert result.exit_code == 1
    combined = result.output + (
        result.stderr if hasattr(result, "stderr") else ""
    )
    assert "summary" in combined.lower()
    # Enricher must not be called.
    assert enricher.calls == 0


def test_brain_tag_auto_plus_mods_errors(
    monkeypatch: pytest.MonkeyPatch,
    test_db: psycopg.Connection,
    fake_embedder: object,
) -> None:
    doc_id = _seed(test_db, fake_embedder, summary="ok")
    enricher = _FakeEnricher(
        proposal=TagProposal(existing=[], new=[])
    )
    _patch(monkeypatch, enricher)
    result = CliRunner().invoke(
        app, ["tag", doc_id, "+foo", "--auto", "--accept-all"]
    )
    assert result.exit_code != 0


def test_brain_tag_accept_all_without_auto_errors(
    monkeypatch: pytest.MonkeyPatch,
    test_db: psycopg.Connection,
    fake_embedder: object,
) -> None:
    doc_id = _seed(test_db, fake_embedder, summary="ok")
    monkeypatch.setenv("DATABASE_URL", TEST_DATABASE_URL)
    result = CliRunner().invoke(
        app, ["tag", doc_id, "+x", "--accept-all"]
    )
    assert result.exit_code != 0


def test_brain_tag_existing_positional_path_still_works(
    monkeypatch: pytest.MonkeyPatch,
    test_db: psycopg.Connection,
    fake_embedder: object,
) -> None:
    """Regression — legacy ``brain tag <id> +foo -bar`` stays byte-identical."""
    monkeypatch.setenv("DATABASE_URL", TEST_DATABASE_URL)
    doc_id = _seed(test_db, fake_embedder, summary=None, tags=["old"])
    result = CliRunner().invoke(app, ["tag", doc_id, "+new", "-old"])
    assert result.exit_code == 0, result.output
    tags = _tags_of(test_db, doc_id)
    assert "new" in tags
    assert "old" not in tags


def test_brain_tag_auto_ollama_unavailable_exits_1(
    monkeypatch: pytest.MonkeyPatch,
    test_db: psycopg.Connection,
    fake_embedder: object,
) -> None:
    doc_id = _seed(test_db, fake_embedder, summary="ok")
    enricher = _FakeEnricher(raise_exc=OllamaUnavailable("refused"))
    _patch(monkeypatch, enricher)
    result = CliRunner().invoke(
        app, ["tag", doc_id, "--auto", "--accept-all"]
    )
    assert result.exit_code == 1
    assert _tags_of(test_db, doc_id) == []


def test_brain_tag_auto_empty_proposal_is_noop(
    monkeypatch: pytest.MonkeyPatch,
    test_db: psycopg.Connection,
    fake_embedder: object,
) -> None:
    doc_id = _seed(test_db, fake_embedder, summary="ok")
    enricher = _FakeEnricher(proposal=TagProposal(existing=[], new=[]))
    _patch(monkeypatch, enricher)
    result = CliRunner().invoke(
        app, ["tag", doc_id, "--auto", "--accept-all"]
    )
    assert result.exit_code == 0, result.output
    assert "no tags proposed" in result.output
    assert _tags_of(test_db, doc_id) == []
