"""Ingest boundary regressions that need the test database (0.4.0 QA F1, F2, F7).

F1: an embedding failure is one line, not a Rich traceback. ``ingest-dir`` stops on a
transient failure (the embedder is down) and continues past a permanent one. F7: tags
are normalized on the INSERT path, from every caller. The failing embedders are test
doubles injected at the ``brain.cli._build_embedder`` patch point; real Ollama is never
called.
"""
from __future__ import annotations

import os
from pathlib import Path

import psycopg
import pytest
from typer.testing import CliRunner

from brain import mcp_server
from brain.cli import app
from brain.config import Config
from brain.embeddings import OllamaEmbedError
from tests.conftest import FakeEmbedder
from tests.test_cli_ingest_gmail import _fake_runner, _msg

TEST_DATABASE_URL = os.environ.get(
    "TEST_DATABASE_URL",
    "postgresql://brain:brain@localhost:5434/second_brain_test",
)
RAW_TAGS = ["qa-a", "Markdown_Test", "Interview Prep", "MARKDOWN-test"]
CANONICAL_TAGS = ["qa-a", "markdown-test", "interview-prep"]


class _FailingEmbedder(FakeEmbedder):
    """Embeds normally for the first ``ok_calls`` calls, then raises like Ollama does."""

    def __init__(self, *, ok_calls: int = 0, transient: bool = True) -> None:
        super().__init__()
        self.ok_calls = ok_calls
        self.transient = transient
        self.calls = 0

    def embed(self, texts: list[str], input_type: str = "document") -> list[list[float]]:
        self.calls += 1
        if self.calls > self.ok_calls:
            raise OllamaEmbedError("Ollama request failed: timed out", transient=self.transient)
        return super().embed(texts, input_type)


def _use(monkeypatch: pytest.MonkeyPatch, tmp_path: Path, embedder: object) -> None:
    monkeypatch.setenv("DATABASE_URL", TEST_DATABASE_URL)
    monkeypatch.setenv("BRAIN_VAULT_PATH", str(tmp_path / "vault"))
    monkeypatch.setenv("BRAIN_GRAPH_ENABLED", "false")
    monkeypatch.setattr("brain.cli._build_embedder", lambda cfg: embedder)
    monkeypatch.setattr("brain.cli._build_enricher", lambda cfg: None)


def _doc_count(conn: psycopg.Connection) -> int:
    row = conn.execute("SELECT count(*) FROM documents").fetchone()
    assert row is not None
    return int(row[0])


def _corpus(tmp_path: Path, n: int) -> Path:
    corpus = tmp_path / "corpus"
    corpus.mkdir()
    for i in range(1, n + 1):
        (corpus / f"note{i}.txt").write_text(f"synthetic note number {i}\n", encoding="utf-8")
    return corpus


# ---------------------------------------------------------------------------
# F1
# ---------------------------------------------------------------------------


def test_ingest_embed_timeout_prints_one_line(
    monkeypatch: pytest.MonkeyPatch, test_db: psycopg.Connection, tmp_path: Path
) -> None:
    _use(monkeypatch, tmp_path, _FailingEmbedder())
    note = _corpus(tmp_path, 1) / "note1.txt"
    result = CliRunner().invoke(app, ["ingest", str(note), "--no-enrich"])
    assert result.exit_code == 1, result.output
    assert "embedding failed for note1.txt (OllamaEmbedError)" in result.output
    assert "nothing was stored" in result.output
    assert "Traceback" not in result.output
    assert "timed out" not in result.output  # class only, never the message
    assert _doc_count(test_db) == 0


def test_ingest_stdin_embed_timeout_prints_one_line(
    monkeypatch: pytest.MonkeyPatch, test_db: psycopg.Connection, tmp_path: Path
) -> None:
    _use(monkeypatch, tmp_path, _FailingEmbedder())
    result = CliRunner().invoke(
        app,
        ["ingest-stdin", "--source", "slack", "--external-id", "qa-1", "--title", "QA thread",
         "--no-enrich"],
        input="hello synthetic\n",
    )
    assert result.exit_code == 1, result.output
    assert "embedding failed for QA thread (OllamaEmbedError)" in result.output
    assert "Traceback" not in result.output
    assert _doc_count(test_db) == 0


def test_ingest_dir_stops_on_transient_embed_failure(
    monkeypatch: pytest.MonkeyPatch, test_db: psycopg.Connection, tmp_path: Path
) -> None:
    corpus = _corpus(tmp_path, 3)
    failing = _FailingEmbedder(ok_calls=1, transient=True)
    _use(monkeypatch, tmp_path, failing)
    result = CliRunner().invoke(app, ["ingest-dir", str(corpus), "--no-enrich"])
    assert result.exit_code == 1, result.output
    assert "Traceback" not in result.output
    assert result.output.count("  ingested: ") == 1
    assert "stopped at file 2 of 3" in result.output
    assert failing.calls == 2  # the third file was never attempted
    assert _doc_count(test_db) == 1

    # A re-run with a working embedder resumes: the stored file is skipped.
    _use(monkeypatch, tmp_path, FakeEmbedder())
    rerun = CliRunner().invoke(app, ["ingest-dir", str(corpus), "--no-enrich"])
    assert rerun.exit_code == 0, rerun.output
    assert rerun.output.count("  skipped: ") == 1
    assert rerun.output.count("  ingested: ") == 2
    assert _doc_count(test_db) == 3


def test_ingest_dir_continues_past_permanent_embed_failure(
    monkeypatch: pytest.MonkeyPatch, test_db: psycopg.Connection, tmp_path: Path
) -> None:
    corpus = _corpus(tmp_path, 3)
    failing = _FailingEmbedder(transient=False)
    _use(monkeypatch, tmp_path, failing)
    result = CliRunner().invoke(app, ["ingest-dir", str(corpus), "--no-enrich"])
    assert result.exit_code == 0, result.output
    assert "Traceback" not in result.output
    assert result.output.count("  failed: ") == 3
    assert "(OllamaEmbedError)" in result.output
    assert failing.calls == 3
    assert "stopped" not in result.output
    assert _doc_count(test_db) == 0


def test_ingest_dir_sync_skips_prune_when_stopped(
    monkeypatch: pytest.MonkeyPatch, test_db: psycopg.Connection, tmp_path: Path
) -> None:
    corpus = _corpus(tmp_path, 2)
    _use(monkeypatch, tmp_path, FakeEmbedder())
    first = CliRunner().invoke(app, ["ingest-dir", str(corpus), "--no-enrich"])
    assert first.exit_code == 0, first.output
    (corpus / "note1.txt").unlink()  # its document is now stale: a prune candidate
    (corpus / "note2.txt").write_text("synthetic note two, edited\n", encoding="utf-8")

    _use(monkeypatch, tmp_path, _FailingEmbedder(transient=True))
    result = CliRunner().invoke(
        app, ["ingest-dir", str(corpus), "--no-enrich", "--sync", "--apply", "--allow-empty"]
    )
    assert result.exit_code == 1, result.output
    assert "stopped at file 1 of 1" in result.output
    assert _doc_count(test_db) == 2  # nothing pruned by the aborted run


# ---------------------------------------------------------------------------
# F2: errors inside the body are not relabelled as "cannot reach".
# ---------------------------------------------------------------------------


def test_connect_or_exit_lets_body_errors_through(test_db: psycopg.Connection) -> None:
    from brain.cli_ingest_boundary import connect_or_exit

    with (
        pytest.raises(psycopg.errors.UndefinedTable),
        connect_or_exit(TEST_DATABASE_URL) as conn,
    ):
        conn.execute("SELECT 1 FROM no_such_table_c1")


# ---------------------------------------------------------------------------
# F7
# ---------------------------------------------------------------------------


def _tag_args() -> list[str]:
    return [arg for tag in RAW_TAGS for arg in ("--tag", tag)]


def _stored_tags(conn: psycopg.Connection) -> list[list[str]]:
    return [list(r[0]) for r in conn.execute("SELECT tags FROM documents").fetchall()]


@pytest.mark.parametrize("command", ["ingest", "ingest-dir", "ingest-stdin"])
def test_cli_ingest_tags_are_normalized(
    monkeypatch: pytest.MonkeyPatch,
    test_db: psycopg.Connection,
    tmp_path: Path,
    command: str,
) -> None:
    _use(monkeypatch, tmp_path, FakeEmbedder())
    corpus = _corpus(tmp_path, 1)
    args, stdin = {
        "ingest": (["ingest", str(corpus / "note1.txt")], None),
        "ingest-dir": (["ingest-dir", str(corpus)], None),
        "ingest-stdin": (
            ["ingest-stdin", "--source", "slack", "--external-id", "qa-t", "--title", "QA"],
            "hello synthetic\n",
        ),
    }[command]
    result = CliRunner().invoke(app, [*args, "--no-enrich", *_tag_args()], input=stdin)
    assert result.exit_code == 0, result.output
    assert _stored_tags(test_db) == [CANONICAL_TAGS]
    row = test_db.execute("SELECT DISTINCT tags_text FROM chunks").fetchall()
    assert [r[0] for r in row] == [" ".join(CANONICAL_TAGS)]


def test_ingest_gmail_tags_are_normalized(
    monkeypatch: pytest.MonkeyPatch, test_db: psycopg.Connection, tmp_path: Path
) -> None:
    _use(monkeypatch, tmp_path, FakeEmbedder())
    msgs = {"m1": _msg(id="m1", subject="Hi", from_addr="n@example.com", body="Hello there")}
    monkeypatch.setattr("brain.ingest.gmail._run", _fake_runner(msgs))
    result = CliRunner().invoke(
        app, ["ingest-gmail", "--label", "synthetic", "--no-enrich", *_tag_args()]
    )
    assert result.exit_code == 0, result.output
    assert _stored_tags(test_db) == [CANONICAL_TAGS]


def test_mcp_ingest_stdin_tags_are_normalized(
    monkeypatch: pytest.MonkeyPatch, test_db: psycopg.Connection, tmp_path: Path
) -> None:
    state = mcp_server._State(
        cfg=Config(database_url=TEST_DATABASE_URL, vault_path=tmp_path),
        embedder=FakeEmbedder(),  # type: ignore[arg-type]
    )
    monkeypatch.setattr(mcp_server, "_state", state)
    payload = mcp_server.brain_ingest_stdin(
        content="synthetic snippet body",
        source="slack",
        external_id="qa-mcp-1",
        title="QA snippet",
        tags=["Team_Notes", "team notes"],
    )
    assert payload["created"] is True
    assert _stored_tags(test_db) == [["team-notes", "source-mcp"]]
