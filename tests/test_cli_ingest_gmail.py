"""Tests for `brain ingest-gmail` — Gmail ingester that shells out to the `gws` CLI."""
import json
import os
from collections.abc import Callable

import psycopg
import pytest
from typer.testing import CliRunner

from brain.cli import app
from brain.ingest import gmail as gmail_ingest

TEST_DATABASE_URL = os.environ.get(
    "TEST_DATABASE_URL",
    "postgresql://brain:brain@localhost:5433/second_brain_test",
)


def _fake_runner(messages: list[dict]) -> Callable[..., str]:
    """Return a callable that simulates `gws gmail list/read` responses.

    Accepts ``*args, **kwargs`` so it works both when passed as a ``runner=`` arg
    and when it replaces the module-level ``_run`` (which has signature
    ``(cmd, runner=None)``).
    """

    def runner(cmd: list[str], *_args: object, **_kwargs: object) -> str:
        # cmd shape: ["gws", "gmail", <verb>, ...]
        if cmd[2] == "list":
            stubs = [{"id": m["id"], "subject": m["subject"]} for m in messages]
            return json.dumps(stubs)
        if cmd[2] == "read":
            mid = cmd[cmd.index("--id") + 1]
            return json.dumps(next(m for m in messages if m["id"] == mid))
        raise AssertionError(f"unexpected gws call: {cmd}")

    return runner


def _patch_embedder(monkeypatch: pytest.MonkeyPatch, fake_embedder: object) -> None:
    monkeypatch.setenv("DATABASE_URL", TEST_DATABASE_URL)
    monkeypatch.setenv("VOYAGE_API_KEY", "fake")
    monkeypatch.setattr("brain.cli._build_embedder", lambda cfg: fake_embedder)


def test_ingest_gmail_requires_scope_flag(
    monkeypatch: pytest.MonkeyPatch,
    test_db: psycopg.Connection,
) -> None:
    """Bare `ingest-gmail` with no scope flags exits non-zero with a helpful message."""
    monkeypatch.setenv("DATABASE_URL", TEST_DATABASE_URL)
    monkeypatch.setenv("VOYAGE_API_KEY", "fake")
    result = CliRunner().invoke(app, ["ingest-gmail"])
    assert result.exit_code != 0
    combined = (result.output + (result.stderr if result.stderr_bytes else "")).lower()
    assert "scope" in combined or "required" in combined


def test_ingest_gmail_with_label(
    monkeypatch: pytest.MonkeyPatch,
    test_db: psycopg.Connection,
    fake_embedder: object,
) -> None:
    """`ingest-gmail --label ...` pulls each message and stores it as a document."""
    _patch_embedder(monkeypatch, fake_embedder)
    monkeypatch.setattr(
        "brain.ingest.gmail._run",
        _fake_runner(
            [
                {
                    "id": "m1", "subject": "Hi", "from": "n@x", "to": "a@x",
                    "date": "2026-04-01", "body": "Hello there",
                },
            ]
        ),
    )
    result = CliRunner().invoke(app, ["ingest-gmail", "--label", "interviews"])
    assert result.exit_code == 0, result.output
    with psycopg.connect(TEST_DATABASE_URL) as conn:
        row = conn.execute(
            "SELECT count(*) FROM documents WHERE content_type='email'"
        ).fetchone()
    assert row is not None
    assert row[0] == 1


def test_ingest_gmail_dry_run_skips_ingest(
    monkeypatch: pytest.MonkeyPatch,
    test_db: psycopg.Connection,
    fake_embedder: object,
) -> None:
    """`--dry-run` lists matches without writing to the DB."""
    _patch_embedder(monkeypatch, fake_embedder)
    monkeypatch.setattr(
        "brain.ingest.gmail._run",
        _fake_runner(
            [
                {
                    "id": "m1", "subject": "Hi", "from": "n@x", "to": "a@x",
                    "date": "2026-04-01", "body": "Hello there",
                },
                {
                    "id": "m2", "subject": "Bye", "from": "n@x", "to": "a@x",
                    "date": "2026-04-02", "body": "Goodbye",
                },
            ]
        ),
    )
    result = CliRunner().invoke(
        app, ["ingest-gmail", "--label", "interviews", "--dry-run"]
    )
    assert result.exit_code == 0, result.output
    assert "would ingest" in result.output
    assert "m1" in result.output
    with psycopg.connect(TEST_DATABASE_URL) as conn:
        row = conn.execute("SELECT count(*) FROM documents").fetchone()
    assert row is not None
    assert row[0] == 0


def test_ingest_gmail_dedups_on_message_id(
    monkeypatch: pytest.MonkeyPatch,
    test_db: psycopg.Connection,
    fake_embedder: object,
) -> None:
    """Re-running the same ingest reuses the source row and skips the document."""
    _patch_embedder(monkeypatch, fake_embedder)
    messages = [
        {
            "id": "m1", "subject": "Hi", "from": "n@x", "to": "a@x",
            "date": "2026-04-01", "body": "Hello there",
        },
    ]
    monkeypatch.setattr("brain.ingest.gmail._run", _fake_runner(messages))
    CliRunner().invoke(app, ["ingest-gmail", "--label", "interviews"])
    second = CliRunner().invoke(app, ["ingest-gmail", "--label", "interviews"])
    assert second.exit_code == 0, second.output
    assert "skipped" in second.output.lower()
    with psycopg.connect(TEST_DATABASE_URL) as conn:
        n = conn.execute("SELECT count(*) FROM documents").fetchone()
        s = conn.execute(
            "SELECT count(*) FROM sources WHERE kind='gmail'"
        ).fetchone()
    assert n is not None and n[0] == 1
    assert s is not None and s[0] == 1


def test_list_messages_builds_full_query() -> None:
    """All scope flags compose into a single `gws gmail list --query ...` string."""
    captured: dict[str, list[str]] = {}

    def runner(cmd: list[str]) -> str:
        captured["cmd"] = cmd
        return "[]"

    gmail_ingest.list_messages(
        query="project foo",
        label="interviews",
        from_addr="a@b",
        since="2026-01-01",
        until="2026-04-01",
        max_results=25,
        runner=runner,
    )
    cmd = captured["cmd"]
    assert cmd[:3] == ["gws", "gmail", "list"]
    q_index = cmd.index("--query") + 1
    q = cmd[q_index]
    assert "project foo" in q
    assert "label:interviews" in q
    assert "from:a@b" in q
    assert "after:2026-01-01" in q
    assert "before:2026-04-01" in q
    assert "--max" in cmd
    assert cmd[cmd.index("--max") + 1] == "25"
    assert cmd[-2:] == ["--format", "json"]


def test_list_messages_no_scope_sends_empty_query() -> None:
    """When no scope parts are supplied, the query string is empty (caller enforces scope)."""

    def runner(cmd: list[str]) -> str:
        q = cmd[cmd.index("--query") + 1]
        assert q == ""
        return "[]"

    assert gmail_ingest.list_messages(runner=runner) == []


def test_to_extracted_doc_handles_missing_subject() -> None:
    """Missing subject falls back to `(no subject)` and strips whitespace from the body."""
    doc = gmail_ingest.to_extracted_doc(
        {"id": "m1", "body": "  hello\n\n  ", "from": "x@y"}
    )
    assert doc.title == "(no subject)"
    assert doc.content == "hello"
    assert doc.content_type == "email"
    assert doc.metadata["from"] == "x@y"
    assert doc.metadata["message_id"] == "m1"
    assert doc.metadata["date"] is None


def test_read_message_calls_gws_read() -> None:
    """`read_message` issues a `gws gmail read --id ... --format json` call and parses stdout."""
    captured: dict[str, list[str]] = {}

    def runner(cmd: list[str]) -> str:
        captured["cmd"] = cmd
        return json.dumps({"id": "m42", "subject": "Hi", "body": "x"})

    msg = gmail_ingest.read_message("m42", runner=runner)
    assert msg["id"] == "m42"
    assert captured["cmd"] == [
        "gws", "gmail", "read", "--id", "m42", "--format", "json",
    ]
