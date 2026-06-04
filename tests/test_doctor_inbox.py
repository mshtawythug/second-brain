"""Tests for the ``brain doctor`` inbox-size WARN + ``count_documents_with_tag``.

Plan 09 Phase 3 observability. Covers the queries helper directly, the
``_check_inbox_size`` doctor sub-check in isolation (real test DB + capsys), and
the end-to-end ``brain doctor`` line via ``CliRunner`` (Ollama mocked through the
shared helpers in ``test_cli_doctor``). All documents are synthetic.
"""
from __future__ import annotations

import os
from pathlib import Path

import psycopg
import pytest
from typer.testing import CliRunner

from brain.cli import _check_inbox_size, app
from brain.config import Config
from brain.ingest import ExtractedDoc, ingest_document
from brain.queries import count_documents_with_tag
from tests.test_cli_doctor import _ok_ollama_transport, _patch_httpx_client

TEST_DATABASE_URL = os.environ.get(
    "TEST_DATABASE_URL",
    "postgresql://brain:brain@localhost:5434/second_brain_test",
)


def _seed_tagged(
    conn: psycopg.Connection,
    embedder: object,
    *,
    n: int,
    tags: list[str],
) -> None:
    """Ingest ``n`` synthetic documents each carrying ``tags`` (no vault mirror)."""
    # Fold the tag set into the body so distinct ``tags`` never collide on
    # ``content_hash`` (which would make the second call a dedup no-op).
    marker = "-".join(tags) or "untagged"
    for i in range(n):
        ingest_document(
            conn,
            embedder=embedder,  # type: ignore[arg-type]
            doc=ExtractedDoc(
                title=f"synthetic {marker} item {i}",
                content=f"synthetic {marker} body number {i} about topic-{i}",
                content_type="note",
                source_path=None,
                metadata={},
            ),
            source_kind="manual",
            tags=list(tags),
        )


# ---------------------------------------------------------------------------
# count_documents_with_tag
# ---------------------------------------------------------------------------


def test_count_documents_with_tag_counts_matching_rows(
    test_db: psycopg.Connection,
    fake_embedder: object,
) -> None:
    """Counts only rows whose ``tags`` array contains the queried tag."""
    _seed_tagged(test_db, fake_embedder, n=2, tags=["inbox"])
    _seed_tagged(test_db, fake_embedder, n=1, tags=["reference"])

    assert count_documents_with_tag(test_db, "inbox") == 2
    assert count_documents_with_tag(test_db, "reference") == 1
    assert count_documents_with_tag(test_db, "does-not-exist") == 0


# ---------------------------------------------------------------------------
# _check_inbox_size (doctor sub-check, in isolation)
# ---------------------------------------------------------------------------


def test_check_inbox_size_warns_above_threshold(
    test_db: psycopg.Connection,
    fake_embedder: object,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Inbox count strictly above the threshold → yellow WARN with the count."""
    _seed_tagged(test_db, fake_embedder, n=3, tags=["inbox"])
    cfg = Config(database_url=TEST_DATABASE_URL, capture_inbox_warn_threshold=2)

    _check_inbox_size(test_db, cfg)

    out = capsys.readouterr().out
    assert "inbox" in out
    assert "WARN" in out
    assert "3 items" in out
    assert "brain capture review" in out


def test_check_inbox_size_ok_at_threshold(
    test_db: psycopg.Connection,
    fake_embedder: object,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Count equal to the threshold is NOT a warning (strict greater-than)."""
    _seed_tagged(test_db, fake_embedder, n=2, tags=["inbox"])
    cfg = Config(database_url=TEST_DATABASE_URL, capture_inbox_warn_threshold=2)

    _check_inbox_size(test_db, cfg)

    out = capsys.readouterr().out
    assert "inbox           OK (2 items)" in out
    assert "WARN" not in out


def test_check_inbox_size_ok_when_empty(
    test_db: psycopg.Connection,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """An empty inbox prints the zero-count OK line."""
    cfg = Config(database_url=TEST_DATABASE_URL, capture_inbox_warn_threshold=5)

    _check_inbox_size(test_db, cfg)

    out = capsys.readouterr().out
    assert "inbox           OK (0 items)" in out


# ---------------------------------------------------------------------------
# brain doctor — end-to-end line via CliRunner
# ---------------------------------------------------------------------------


def test_doctor_reports_inbox_warn(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    test_db: psycopg.Connection,
    fake_embedder: object,
) -> None:
    """A large inbox surfaces the WARN line in ``brain doctor`` (exit 0)."""
    _seed_tagged(test_db, fake_embedder, n=3, tags=["inbox"])
    monkeypatch.setenv("DATABASE_URL", TEST_DATABASE_URL)
    monkeypatch.setenv("BRAIN_VAULT_PATH", str(tmp_path / "vault"))
    monkeypatch.setenv("BRAIN_CAPTURE_INBOX_WARN_THRESHOLD", "1")
    with _patch_httpx_client(_ok_ollama_transport()):
        result = CliRunner().invoke(app, ["doctor"])
    assert result.exit_code == 0, result.output
    assert "inbox" in result.output
    assert "WARN" in result.output
    assert "3 items" in result.output
    assert "brain capture review" in result.output


def test_doctor_reports_inbox_ok_when_empty(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    test_db: psycopg.Connection,  # noqa: ARG001 — fresh empty schema
) -> None:
    """An empty inbox prints the OK line in ``brain doctor`` (exit 0)."""
    monkeypatch.setenv("DATABASE_URL", TEST_DATABASE_URL)
    monkeypatch.setenv("BRAIN_VAULT_PATH", str(tmp_path / "vault"))
    monkeypatch.setenv("BRAIN_CAPTURE_INBOX_WARN_THRESHOLD", "5")
    with _patch_httpx_client(_ok_ollama_transport()):
        result = CliRunner().invoke(app, ["doctor"])
    assert result.exit_code == 0, result.output
    assert "inbox           OK (0 items)" in result.output
