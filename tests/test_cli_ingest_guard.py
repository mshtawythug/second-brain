"""CLI-surface tests for the ingest secret guard (F4).

Two contracts are under test here, and the second matters as much as the first:

1. The guard is reachable and correct from every ingest command.
2. **stdout is byte-identical to its pre-F4 form.** Every guard message goes to
   stderr, so a script (or ``tests/test_cli_ingest.py``) parsing stdout is
   unaffected whether or not a document trips the guard.
"""
from __future__ import annotations

from pathlib import Path

import psycopg
import pytest
from typer.testing import CliRunner

from brain.cli import app
from tests.conftest import TEST_DATABASE_URL
from tests.secret_fixtures import CLEAN_PROSE, SYNTHETIC_AWS_KEY

_INGEST_COMMANDS = ["ingest", "ingest-dir", "ingest-stdin", "ingest-gmail"]


def _sandbox(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Point the CLI at the test DB + a throwaway vault, with no LLM hooks."""
    from tests.conftest import FakeEmbedder

    monkeypatch.setenv("DATABASE_URL", TEST_DATABASE_URL)
    monkeypatch.setenv("BRAIN_VAULT_PATH", str(tmp_path / "vault"))
    monkeypatch.setenv("BRAIN_GRAPH_ENABLED", "false")
    monkeypatch.setattr("brain.cli._build_embedder", lambda cfg: FakeEmbedder())
    monkeypatch.setattr("brain.cli._build_enricher", lambda cfg: None)


def _write_note(directory: Path, name: str, body: str) -> Path:
    path = directory / name
    path.write_text(body)
    return path


def _document_count(conn: psycopg.Connection) -> int:
    row = conn.execute("SELECT count(*) FROM documents").fetchone()
    assert row is not None
    return int(row[0])


# ---------------------------------------------------------------------------
# Backward compatibility — the assertion that protects every stdout consumer
# ---------------------------------------------------------------------------


def test_clean_ingest_stdout_is_byte_identical_to_the_legacy_format(
    monkeypatch: pytest.MonkeyPatch,
    test_db: psycopg.Connection,
    tmp_path: Path,
) -> None:
    # --- setup
    _sandbox(monkeypatch, tmp_path)
    note = _write_note(tmp_path, "clean.md", CLEAN_PROSE)

    # --- exercise
    result = CliRunner().invoke(app, ["ingest", str(note)])

    # --- verify
    assert result.exit_code == 0, result.output
    row = test_db.execute("SELECT id::text FROM documents").fetchone()
    assert row is not None
    assert result.stdout == f"ingested: clean.md → {row[0]}\n"


def test_stdout_stays_clean_even_when_the_guard_fires(
    monkeypatch: pytest.MonkeyPatch,
    test_db: psycopg.Connection,
    tmp_path: Path,
) -> None:
    """The whole point of routing guard output to stderr."""
    # --- setup
    _sandbox(monkeypatch, tmp_path)
    monkeypatch.setenv("BRAIN_SECRET_GUARD", "warn")
    note = _write_note(tmp_path, "runbook.md", f"key: {SYNTHETIC_AWS_KEY}\n")

    # --- exercise
    result = CliRunner().invoke(app, ["ingest", str(note)])

    # --- verify
    assert result.exit_code == 0, result.stderr
    row = test_db.execute("SELECT id::text FROM documents").fetchone()
    assert row is not None
    assert result.stdout == f"ingested: runbook.md → {row[0]}\n"
    assert "secret guard" in result.stderr
    assert "aws_access_key_id" in result.stderr
    assert SYNTHETIC_AWS_KEY not in result.stderr


# ---------------------------------------------------------------------------
# Modes
# ---------------------------------------------------------------------------


def test_reject_exits_one_with_the_refusal_prefix_on_stderr(
    monkeypatch: pytest.MonkeyPatch,
    test_db: psycopg.Connection,
    tmp_path: Path,
) -> None:
    # --- setup
    _sandbox(monkeypatch, tmp_path)
    monkeypatch.setenv("BRAIN_SECRET_GUARD", "reject")
    note = _write_note(tmp_path, "runbook.md", f"key: {SYNTHETIC_AWS_KEY}\n")

    # --- exercise
    result = CliRunner().invoke(app, ["ingest", str(note)])

    # --- verify
    assert result.exit_code == 1
    assert "✗  secret guard:" in result.stderr
    assert SYNTHETIC_AWS_KEY not in result.stderr
    assert _document_count(test_db) == 0


def test_allow_secrets_flag_exits_zero_and_still_reports(
    monkeypatch: pytest.MonkeyPatch,
    test_db: psycopg.Connection,
    tmp_path: Path,
) -> None:
    # --- setup
    _sandbox(monkeypatch, tmp_path)
    monkeypatch.setenv("BRAIN_SECRET_GUARD", "reject")
    note = _write_note(tmp_path, "runbook.md", f"key: {SYNTHETIC_AWS_KEY}\n")

    # --- exercise
    result = CliRunner().invoke(app, ["ingest", str(note), "--allow-secrets"])

    # --- verify
    assert result.exit_code == 0, result.stderr
    assert _document_count(test_db) == 1
    assert "guard bypassed" in result.stderr


def test_off_mode_prints_nothing(
    monkeypatch: pytest.MonkeyPatch,
    test_db: psycopg.Connection,
    tmp_path: Path,
) -> None:
    # --- setup
    _sandbox(monkeypatch, tmp_path)
    monkeypatch.setenv("BRAIN_SECRET_GUARD", "off")
    note = _write_note(tmp_path, "runbook.md", f"key: {SYNTHETIC_AWS_KEY}\n")

    # --- exercise
    result = CliRunner().invoke(app, ["ingest", str(note)])

    # --- verify
    assert result.exit_code == 0, result.stderr
    assert "secret guard" not in result.stderr


# ---------------------------------------------------------------------------
# ingest-dir — a refusal must not abort the walk
# ---------------------------------------------------------------------------


def test_ingest_dir_refuses_one_file_and_keeps_going(
    monkeypatch: pytest.MonkeyPatch,
    test_db: psycopg.Connection,
    tmp_path: Path,
) -> None:
    """Under ``reject``, file 400 of 900 must not kill the run.

    That failure mode is precisely why ``warn`` is the default; when a user
    does opt into ``reject``, the walk still degrades per-file rather than
    aborting.
    """
    # --- setup
    _sandbox(monkeypatch, tmp_path)
    monkeypatch.setenv("BRAIN_SECRET_GUARD", "reject")
    corpus = tmp_path / "corpus"
    corpus.mkdir()
    _write_note(corpus, "clean.md", CLEAN_PROSE)
    _write_note(corpus, "leaky.md", f"key: {SYNTHETIC_AWS_KEY}\n")

    # --- exercise
    result = CliRunner().invoke(app, ["ingest-dir", str(corpus)])

    # --- verify
    assert result.exit_code == 0, result.stderr
    assert "refused: leaky.md" in result.stderr
    assert _document_count(test_db) == 1


# ---------------------------------------------------------------------------
# ingest-stdin
# ---------------------------------------------------------------------------


def test_ingest_stdin_reject_exits_one_and_writes_nothing(
    monkeypatch: pytest.MonkeyPatch,
    test_db: psycopg.Connection,
    tmp_path: Path,
) -> None:
    # --- setup
    _sandbox(monkeypatch, tmp_path)
    monkeypatch.setenv("BRAIN_SECRET_GUARD", "reject")

    # --- exercise
    result = CliRunner().invoke(
        app,
        [
            "ingest-stdin",
            "--source", "slack",
            "--external-id", "C0000001-1700000000.000100",
            "--title", "#infra — rotating the CI token",
        ],
        input=f"someone pasted {SYNTHETIC_AWS_KEY} in the channel\n",
    )

    # --- verify
    assert result.exit_code == 1
    assert "✗  secret guard:" in result.stderr
    assert _document_count(test_db) == 0


def test_ingest_stdin_warn_stores_and_reports(
    monkeypatch: pytest.MonkeyPatch,
    test_db: psycopg.Connection,
    tmp_path: Path,
) -> None:
    # --- setup
    _sandbox(monkeypatch, tmp_path)
    monkeypatch.setenv("BRAIN_SECRET_GUARD", "warn")

    # --- exercise
    result = CliRunner().invoke(
        app,
        [
            "ingest-stdin",
            "--source", "slack",
            "--external-id", "C0000001-1700000000.000200",
            "--title", "#infra thread",
        ],
        input=f"someone pasted {SYNTHETIC_AWS_KEY} in the channel\n",
    )

    # --- verify
    assert result.exit_code == 0, result.stderr
    assert _document_count(test_db) == 1
    assert "secret guard" in result.stderr
    assert result.stdout.startswith("ingested: #infra thread → ")


# ---------------------------------------------------------------------------
# Flag surface
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("command", _INGEST_COMMANDS, ids=str)
def test_allow_secrets_is_offered_on_every_ingest_command(command: str) -> None:
    """Including ``ingest-gmail``: a bulk pull is where a false positive hurts most."""
    # --- exercise
    result = CliRunner().invoke(app, [command, "--help"])

    # --- verify
    assert result.exit_code == 0
    assert "--allow-secrets" in result.stdout
