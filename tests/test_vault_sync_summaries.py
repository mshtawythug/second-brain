"""Tests for ``brain vault sync-summaries`` and its module API.

Wave Q2-SUMMARY-WIKI Item 3. The module backfills ``summary:``
frontmatter into existing vault mirror files for documents the Q1-D
enricher has already touched but whose on-disk frontmatter pre-dates
the Q2 writer's awareness of the ``summary`` key.

The integration tests below ingest documents with a stub enricher,
strip the resulting `summary:` line from the mirror frontmatter on
disk (simulating a pre-Q2 mirror), and then assert that
:func:`sync_summaries` reconciles the file. Each test also exercises
one outcome branch of the report counters.
"""
from __future__ import annotations

import os
from pathlib import Path

import psycopg
import pytest
from typer.testing import CliRunner

from brain.cli import app
from brain.ingest import ExtractedDoc, ingest_document
from brain.vault.frontmatter import dump_frontmatter, parse_frontmatter
from brain.vault.sync_summaries import sync_summaries


def _stub_enricher(text: str = "Q2 stub summary."):
    """Return a minimal OllamaEnricher test double (see Q1-D hook tests)."""
    from dataclasses import dataclass

    from brain.enrichment import SummaryResult

    @dataclass
    class _Enricher:
        model: str = "llama3.1:8b"
        summary_text: str = text

        def count_tokens(self, text: str) -> int:
            return max(1, len(text) // 4)

        def summarize(self, title: str, content: str):
            return SummaryResult(summary=self.summary_text, model=self.model)

    return _Enricher()


def _ingest_with_summary(
    conn: psycopg.Connection,
    embedder,
    vault: Path,
    *,
    title: str,
    summary_text: str = "Canned summary.",
    body_seed: str | None = None,
) -> str:
    """Helper — ingest a doc with the enrich hook firing, return doc_id.

    ``body_seed`` is mixed into the body so multiple test docs in the
    same test get distinct ``content_hash`` values (stdin ingest dedups
    on hash; without a seed, ``_ingest_with_summary`` would no-op the
    second call). Defaults to ``title`` so the per-test docs naturally
    diverge.
    """
    seed = body_seed if body_seed is not None else title
    body = f"Long body for the enrich hook — {seed}. " * 50
    result = ingest_document(
        conn,
        embedder=embedder,
        doc=ExtractedDoc(
            title=title,
            content=body,
            content_type="note",
            source_path=None,
            metadata={},
        ),
        source_kind="manual",
        vault_root=vault,
        enricher=_stub_enricher(summary_text),
    )
    assert result.document_id is not None
    return result.document_id


def _strip_summary_from_mirror(target: Path) -> None:
    """Remove the ``summary:`` line from a mirror's frontmatter in place.

    Used to simulate the pre-Q2 corpus: docs ingested + enriched
    BEFORE the export writer learned the ``summary`` key. The on-disk
    file carries every other frontmatter field as it was written; only
    the ``summary:`` line is dropped.
    """
    fields, body = parse_frontmatter(target.read_text(encoding="utf-8"))
    fields.pop("summary", None)
    target.write_text(dump_frontmatter(fields, body), encoding="utf-8")


def test_sync_summaries_inserts_missing_summary_into_frontmatter(
    test_db: psycopg.Connection, fake_embedder, tmp_path: Path
) -> None:
    """A pre-Q2 mirror (no ``summary:`` line) gets one inserted from the DB."""
    vault = tmp_path / "vault"
    _ingest_with_summary(
        test_db,
        fake_embedder,
        vault,
        title="Backfill Me",
        summary_text="Two-sentence canned summary for backfill.",
    )
    target = vault / "_ingested" / "manual" / "backfill-me.md"
    _strip_summary_from_mirror(target)
    fields_before, _ = parse_frontmatter(target.read_text(encoding="utf-8"))
    assert "summary" not in fields_before

    report = sync_summaries(test_db, vault_root=vault)

    assert report.inspected == 1
    assert report.updated == 1
    assert report.unchanged == 0
    assert report.missing_file == 0
    assert report.errored == 0
    fields_after, _ = parse_frontmatter(target.read_text(encoding="utf-8"))
    assert fields_after["summary"] == "Two-sentence canned summary for backfill."


def test_sync_summaries_is_idempotent_when_already_in_sync(
    test_db: psycopg.Connection, fake_embedder, tmp_path: Path
) -> None:
    """A second run after a successful backfill reports `unchanged`."""
    vault = tmp_path / "vault"
    _ingest_with_summary(test_db, fake_embedder, vault, title="Idempotent")
    target = vault / "_ingested" / "manual" / "idempotent.md"
    # Post-Q2 ingest already writes summary into the mirror, so the
    # first call must report `unchanged`. (Belt + suspenders: we also
    # confirm a second pass keeps the same counter.)
    first = sync_summaries(test_db, vault_root=vault)
    assert first.unchanged == 1
    assert first.updated == 0
    mtime_before = target.stat().st_mtime_ns

    second = sync_summaries(test_db, vault_root=vault)
    assert second.inspected == 1
    assert second.unchanged == 1
    assert second.updated == 0
    assert target.stat().st_mtime_ns == mtime_before, (
        "an unchanged backfill must not rewrite the file"
    )


def test_sync_summaries_dry_run_reports_without_writing(
    test_db: psycopg.Connection, fake_embedder, tmp_path: Path
) -> None:
    """``dry_run=True`` reports what would change but writes nothing."""
    vault = tmp_path / "vault"
    _ingest_with_summary(
        test_db,
        fake_embedder,
        vault,
        title="Dry Run Doc",
        summary_text="Dry-run preview summary.",
    )
    target = vault / "_ingested" / "manual" / "dry-run-doc.md"
    _strip_summary_from_mirror(target)
    mtime_before = target.stat().st_mtime_ns

    report = sync_summaries(test_db, vault_root=vault, dry_run=True)

    assert report.inspected == 1
    assert report.updated == 1, "dry-run still counts what WOULD be updated"
    assert target.stat().st_mtime_ns == mtime_before
    fields_after, _ = parse_frontmatter(target.read_text(encoding="utf-8"))
    assert "summary" not in fields_after, (
        "dry-run must NOT mutate the on-disk frontmatter"
    )


def test_sync_summaries_reports_missing_file(
    test_db: psycopg.Connection, fake_embedder, tmp_path: Path
) -> None:
    """A DB row whose ``vault_path`` no longer exists on disk is reported."""
    vault = tmp_path / "vault"
    _ingest_with_summary(test_db, fake_embedder, vault, title="Removed Mirror")
    target = vault / "_ingested" / "manual" / "removed-mirror.md"
    assert target.is_file()
    target.unlink()  # simulate manual delete

    report = sync_summaries(test_db, vault_root=vault)

    assert report.inspected == 1
    assert report.missing_file == 1
    assert report.updated == 0
    assert report.unchanged == 0


def test_sync_summaries_skips_docs_with_null_summary(
    test_db: psycopg.Connection, fake_embedder, tmp_path: Path
) -> None:
    """Docs with NULL ``documents.summary`` are not even inspected.

    The SELECT filter is ``summary IS NOT NULL AND vault_path IS NOT NULL``,
    so a doc the enricher never touched does not appear in the report.
    """
    vault = tmp_path / "vault"
    # No enricher — summary stays NULL.
    ingest_document(
        test_db,
        embedder=fake_embedder,
        doc=ExtractedDoc(
            title="No Summary",
            content="brief",
            content_type="note",
            source_path=None,
            metadata={},
        ),
        source_kind="manual",
        vault_root=vault,
    )
    report = sync_summaries(test_db, vault_root=vault)
    assert report.inspected == 0, (
        "docs without a summary must be excluded by the SELECT"
    )


def test_sync_summaries_limit_caps_total_inspected(
    test_db: psycopg.Connection, fake_embedder, tmp_path: Path
) -> None:
    """``limit`` caps the rows inspected across batches."""
    vault = tmp_path / "vault"
    for i in range(3):
        _ingest_with_summary(test_db, fake_embedder, vault, title=f"Doc {i}")

    report = sync_summaries(test_db, vault_root=vault, limit=2)
    assert report.inspected == 2


def test_sync_summaries_reports_parse_errors(
    test_db: psycopg.Connection, fake_embedder, tmp_path: Path
) -> None:
    """A corrupt frontmatter block is reported as ``errored`` without halting.

    Replaces the file's content with a malformed YAML frontmatter block
    so :func:`parse_frontmatter` raises :class:`yaml.YAMLError`. The loop
    must continue and the report's ``errored`` counter must reflect the
    failure.
    """
    vault = tmp_path / "vault"
    _ingest_with_summary(test_db, fake_embedder, vault, title="Corrupt")
    target = vault / "_ingested" / "manual" / "corrupt.md"
    # Closed frontmatter fence with malformed YAML inside — triggers
    # ``yaml.YAMLError`` from :func:`parse_frontmatter`. Without the
    # closing fence the parser would silently treat the whole file as
    # body (no error), which doesn't exercise the error branch.
    target.write_text(
        "---\nnot valid yaml: [unterminated\n---\nbody\n", encoding="utf-8"
    )

    report = sync_summaries(test_db, vault_root=vault)
    assert report.errored == 1
    assert report.updated == 0
    assert any("corrupt" in err.lower() for err in report.errors)


def test_sync_summaries_field_order_places_summary_after_content_type(
    test_db: psycopg.Connection, fake_embedder, tmp_path: Path
) -> None:
    """After backfill, ``summary:`` sits right after ``content_type:``.

    Pins the canonical field order against future field-ordering
    regressions. The post-Q2 fresh-write path puts summary after
    content_type; the backfill path must produce the same ordering so
    a corpus-wide re-export doesn't oscillate.
    """
    vault = tmp_path / "vault"
    _ingest_with_summary(test_db, fake_embedder, vault, title="Order Doc")
    target = vault / "_ingested" / "manual" / "order-doc.md"
    _strip_summary_from_mirror(target)

    sync_summaries(test_db, vault_root=vault)

    text = target.read_text(encoding="utf-8")
    content_type_idx = text.find("content_type:")
    summary_idx = text.find("summary:")
    assert content_type_idx > -1, "expected content_type in test fixture"
    assert summary_idx > -1, "expected summary inserted by sync"
    assert content_type_idx < summary_idx, (
        f"summary must follow content_type; got "
        f"content_type@{content_type_idx} summary@{summary_idx}"
    )


# ---------------------------------------------------------------------------
# CLI wrapper smoke.
# ---------------------------------------------------------------------------


def test_cli_vault_sync_summaries_dry_run(
    test_db: psycopg.Connection,
    fake_embedder,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """End-to-end CliRunner exercise of ``brain vault sync-summaries --dry-run``."""
    monkeypatch.setenv(
        "DATABASE_URL",
        os.environ.get(
            "TEST_DATABASE_URL",
            "postgresql://brain:brain@localhost:5434/second_brain_test",
        ),
    )
    vault = tmp_path / "vault"
    _ingest_with_summary(test_db, fake_embedder, vault, title="CLI Doc")
    target = vault / "_ingested" / "manual" / "cli-doc.md"
    _strip_summary_from_mirror(target)
    mtime_before = target.stat().st_mtime_ns

    runner = CliRunner()
    result = runner.invoke(
        app, ["vault", "sync-summaries", "--vault", str(vault), "--dry-run"]
    )
    assert result.exit_code == 0, result.stdout
    assert "inspected 1" in result.stdout
    assert "would update 1" in result.stdout
    # Dry-run guarantees no on-disk mutation.
    assert target.stat().st_mtime_ns == mtime_before


def test_cli_vault_sync_summaries_applies(
    test_db: psycopg.Connection,
    fake_embedder,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """End-to-end CliRunner exercise of ``brain vault sync-summaries`` (real apply)."""
    monkeypatch.setenv(
        "DATABASE_URL",
        os.environ.get(
            "TEST_DATABASE_URL",
            "postgresql://brain:brain@localhost:5434/second_brain_test",
        ),
    )
    vault = tmp_path / "vault"
    _ingest_with_summary(
        test_db,
        fake_embedder,
        vault,
        title="CLI Apply",
        summary_text="Applied summary line.",
    )
    target = vault / "_ingested" / "manual" / "cli-apply.md"
    _strip_summary_from_mirror(target)

    runner = CliRunner()
    result = runner.invoke(
        app, ["vault", "sync-summaries", "--vault", str(vault)]
    )
    assert result.exit_code == 0, result.stdout
    assert "inspected 1" in result.stdout
    assert "updated 1" in result.stdout
    fields, _body = parse_frontmatter(target.read_text(encoding="utf-8"))
    assert fields["summary"] == "Applied summary line."
