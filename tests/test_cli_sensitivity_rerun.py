"""The escalate-only ``--sensitivity`` rule for a stored document (sec-114 F11; RM-44 D7).

Shared by ``brain ingest-web`` and ``brain ingest-bookmarks``; each command's own
tests pin its output, these pin the helper against the 5434 database.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any

import psycopg
import pytest

from brain.cli_sensitivity_rerun import escalate_stored_sensitivity
from brain.ingest import ExtractedDoc, ingest_document
from brain.vault.sensitivity_propagate import PropagationFailure
from tests.conftest import FakeEmbedder

LABEL = "https://example.com/doc"


def _stored(conn: psycopg.Connection[Any], embedder: FakeEmbedder, level: str) -> str:
    doc = ExtractedDoc(
        title="Synthetic", content="A synthetic stored body.", content_type="note",
        source_path=None, metadata={},
    )
    result = ingest_document(
        conn, embedder=embedder, doc=doc, source_kind="manual",
        source_external_id="synthetic-1", enrich=False, sensitivity=level,
    )
    assert result.document_id is not None
    return result.document_id


def _level(conn: psycopg.Connection[Any], document_id: str) -> str:
    row = conn.execute("SELECT sensitivity FROM documents WHERE id = %s", (document_id,)).fetchone()
    assert row is not None
    return str(row[0])


class _Propagate:
    def __init__(self, failures: list[PropagationFailure] | None = None) -> None:
        self.calls: list[tuple[str, str]] = []
        self.failures = failures or []

    def __call__(
        self, conn: Any, document_id: str, *, level: str, vault_root: Path
    ) -> list[PropagationFailure]:
        self.calls.append((document_id, level))
        return self.failures


def _escalate(
    conn: psycopg.Connection[Any], document_id: str, level: str, explicit: bool,
    propagate: _Propagate, tmp_path: Path,
) -> bool:
    return escalate_stored_sensitivity(
        conn, document_id, level=level, explicit=explicit, vault_root=tmp_path,
        label=LABEL, propagate=propagate,
    )


def test_without_an_explicit_flag_nothing_happens(
    test_db: psycopg.Connection, fake_embedder: FakeEmbedder, tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    document_id = _stored(test_db, fake_embedder, "normal")
    propagate = _Propagate()

    assert _escalate(test_db, document_id, "confidential", False, propagate, tmp_path) is False

    assert _level(test_db, document_id) == "normal"
    assert propagate.calls == []
    assert capsys.readouterr().out == ""


def test_explicit_confidential_raises_and_propagates(
    test_db: psycopg.Connection, fake_embedder: FakeEmbedder, tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    document_id = _stored(test_db, fake_embedder, "normal")
    propagate = _Propagate()

    assert _escalate(test_db, document_id, "confidential", True, propagate, tmp_path) is True

    assert _level(test_db, document_id) == "confidential"
    assert propagate.calls == [(document_id, "confidential")]
    assert f"  marked confidential {LABEL}" in capsys.readouterr().out


def test_explicit_confidential_on_a_confidential_row_is_unchanged(
    test_db: psycopg.Connection, fake_embedder: FakeEmbedder, tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    document_id = _stored(test_db, fake_embedder, "confidential")
    propagate = _Propagate()

    assert _escalate(test_db, document_id, "confidential", True, propagate, tmp_path) is False

    assert propagate.calls == []
    assert capsys.readouterr().out == ""


def test_explicit_normal_never_lowers(
    test_db: psycopg.Connection, fake_embedder: FakeEmbedder, tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    document_id = _stored(test_db, fake_embedder, "confidential")
    propagate = _Propagate()

    assert _escalate(test_db, document_id, "normal", True, propagate, tmp_path) is False

    assert _level(test_db, document_id) == "confidential"
    assert propagate.calls == []
    assert (
        f"  kept confidential {LABEL} (lower it with: brain mark-normal {document_id[:8]})"
        in capsys.readouterr().out
    )


def test_explicit_normal_on_a_normal_row_says_nothing(
    test_db: psycopg.Connection, fake_embedder: FakeEmbedder, tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    document_id = _stored(test_db, fake_embedder, "normal")

    assert _escalate(test_db, document_id, "normal", True, _Propagate(), tmp_path) is False

    assert capsys.readouterr().out == ""


def test_explicit_normal_on_a_vanished_row_says_nothing(
    test_db: psycopg.Connection, tmp_path: Path, capsys: pytest.CaptureFixture[str],
) -> None:
    missing = "00000000-0000-0000-0000-000000000000"

    assert _escalate(test_db, missing, "normal", True, _Propagate(), tmp_path) is False

    assert capsys.readouterr().out == ""


def test_a_mirror_failure_is_reported_and_the_raise_still_counts(
    test_db: psycopg.Connection, fake_embedder: FakeEmbedder, tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    document_id = _stored(test_db, fake_embedder, "normal")
    failure = PropagationFailure(stage="mirror", message="synthetic mirror failure")

    raised = _escalate(test_db, document_id, "confidential", True, _Propagate([failure]), tmp_path)

    assert raised is True
    captured = capsys.readouterr()
    assert f"  mirror not updated {LABEL}: synthetic mirror failure" in captured.err
    assert f"  marked confidential {LABEL}" in captured.out
