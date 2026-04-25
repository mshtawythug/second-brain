"""Tests for `brain ingest-gmail` — Gmail ingester that shells out to the `gws` CLI.

The real ``gws`` surface is::

    gws gmail users messages list --params '{"userId":"me","maxResults":N,"q":"..."}' \
        --format json
    gws gmail users messages get  --params '{"userId":"me","id":"...","format":"full"}' \
        --format json

``list`` returns ``{"messages": [{"id": ..., "threadId": ...}, ...]}`` (stubs
with no subject/body). ``get`` returns a full Gmail Message resource whose
``payload.headers`` holds From/To/Subject/Date and whose body lives either on
``payload.body.data`` (single-part) or inside ``payload.parts[]`` (multipart),
base64url-encoded.
"""
import base64
import json
import os
from collections.abc import Callable
from typing import Any

import psycopg
import pytest
from typer.testing import CliRunner

from brain.cli import app
from brain.ingest import gmail as gmail_ingest

TEST_DATABASE_URL = os.environ.get(
    "TEST_DATABASE_URL",
    "postgresql://brain:brain@localhost:5433/second_brain_test",
)


def _b64url(text: str) -> str:
    """Encode ``text`` the way the Gmail API does — urlsafe base64, no padding."""
    return base64.urlsafe_b64encode(text.encode()).decode().rstrip("=")


def _msg(
    *,
    id: str,
    subject: str,
    from_addr: str,
    body: str,
    date: str = "2026-04-01",
    to: str = "ali@example.com",
) -> dict[str, Any]:
    """Build a realistic single-part Gmail Message resource for ``users.messages.get``."""
    return {
        "id": id,
        "threadId": f"t{id}",
        "labelIds": ["INBOX"],
        "payload": {
            "mimeType": "text/plain",
            "headers": [
                {"name": "From", "value": from_addr},
                {"name": "To", "value": to},
                {"name": "Subject", "value": subject},
                {"name": "Date", "value": date},
            ],
            "body": {
                "data": _b64url(body),
                "size": len(body),
            },
        },
    }


def _fake_runner(messages_by_id: dict[str, dict[str, Any]]) -> Callable[..., str]:
    """Simulate ``gws gmail users messages list/get``.

    ``messages_by_id`` maps message id → full Message resource. ``list`` responses
    are generated from its keys; ``get`` responses pull the resource by id.

    Accepts ``*args, **kwargs`` so it works both when passed as ``runner=`` and
    when used to replace the module-level ``_run`` (which has signature
    ``(cmd, runner=None)``).
    """

    def runner(cmd: list[str], *_args: object, **_kwargs: object) -> str:
        # Shape: ["gws", "gmail", "users", "messages", "list"|"get",
        #         "--params", "<json>", "--format", "json"]
        assert cmd[:4] == ["gws", "gmail", "users", "messages"], cmd
        op = cmd[4]
        if op == "list":
            return json.dumps(
                {
                    "messages": [
                        {"id": mid, "threadId": f"t{mid}"} for mid in messages_by_id
                    ],
                    "resultSizeEstimate": len(messages_by_id),
                }
            )
        if op == "get":
            params = json.loads(cmd[6])
            return json.dumps(messages_by_id[params["id"]])
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
    """`ingest-gmail --label ...` pulls each message and stores it as an email document."""
    _patch_embedder(monkeypatch, fake_embedder)
    msgs = {
        "m1": _msg(id="m1", subject="Hi", from_addr="n@x", body="Hello there"),
    }
    monkeypatch.setattr("brain.ingest.gmail._run", _fake_runner(msgs))
    result = CliRunner().invoke(app, ["ingest-gmail", "--label", "interviews"])
    assert result.exit_code == 0, result.output
    assert "ingested: Hi" in result.output
    with psycopg.connect(TEST_DATABASE_URL) as conn:
        row = conn.execute(
            "SELECT count(*), max(title) FROM documents WHERE content_type='email'"
        ).fetchone()
    assert row is not None
    assert row[0] == 1
    assert row[1] == "Hi"


def test_ingest_gmail_dry_run_skips_ingest(
    monkeypatch: pytest.MonkeyPatch,
    test_db: psycopg.Connection,
    fake_embedder: object,
) -> None:
    """`--dry-run` lists match ids without writing to the DB."""
    _patch_embedder(monkeypatch, fake_embedder)
    msgs = {
        "m1": _msg(id="m1", subject="Hi", from_addr="n@x", body="Hello there"),
        "m2": _msg(id="m2", subject="Bye", from_addr="n@x", body="Goodbye"),
    }
    monkeypatch.setattr("brain.ingest.gmail._run", _fake_runner(msgs))
    result = CliRunner().invoke(
        app, ["ingest-gmail", "--label", "interviews", "--dry-run"]
    )
    assert result.exit_code == 0, result.output
    assert "would ingest" in result.output
    assert "m1" in result.output
    assert "m2" in result.output
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
    msgs = {
        "m1": _msg(id="m1", subject="Hi", from_addr="n@x", body="Hello there"),
    }
    monkeypatch.setattr("brain.ingest.gmail._run", _fake_runner(msgs))
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


def test_ingest_gmail_continues_on_per_message_error(
    monkeypatch: pytest.MonkeyPatch,
    test_db: psycopg.Connection,
    fake_embedder: object,
) -> None:
    """A `GmailError` on one message must not abort the whole batch."""
    _patch_embedder(monkeypatch, fake_embedder)

    good = {
        "good1": _msg(id="good1", subject="Good 1", from_addr="x@y", body="body of good1"),
        "good2": _msg(id="good2", subject="Good 2", from_addr="x@y", body="body of good2"),
    }

    def flaky_runner(cmd: list[str], *_args: object, **_kwargs: object) -> str:
        assert cmd[:4] == ["gws", "gmail", "users", "messages"], cmd
        op = cmd[4]
        if op == "list":
            return json.dumps(
                {
                    "messages": [
                        {"id": "good1", "threadId": "tgood1"},
                        {"id": "bad", "threadId": "tbad"},
                        {"id": "good2", "threadId": "tgood2"},
                    ],
                    "resultSizeEstimate": 3,
                }
            )
        if op == "get":
            params = json.loads(cmd[6])
            mid = params["id"]
            if mid == "bad":
                raise gmail_ingest.GmailError(f"gws failed for {mid}")
            return json.dumps(good[mid])
        raise AssertionError(f"unexpected gws call: {cmd}")

    monkeypatch.setattr("brain.ingest.gmail._run", flaky_runner)
    result = CliRunner().invoke(app, ["ingest-gmail", "--label", "test"])
    assert result.exit_code == 0, result.output
    assert "ingested" in result.output
    assert "failed" in result.output.lower()
    assert "bad" in result.output
    with psycopg.connect(TEST_DATABASE_URL) as conn:
        row = conn.execute(
            "SELECT count(*) FROM documents WHERE content_type='email'"
        ).fetchone()
    assert row is not None
    assert row[0] == 2


def test_list_messages_builds_full_query() -> None:
    """All scope flags compose into a single Gmail ``q`` string sent via ``--params``."""
    captured: dict[str, list[str]] = {}

    def runner(cmd: list[str]) -> str:
        captured["cmd"] = cmd
        return json.dumps({"messages": [], "resultSizeEstimate": 0})

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
    assert cmd[:5] == ["gws", "gmail", "users", "messages", "list"]
    assert cmd[5] == "--params"
    params = json.loads(cmd[6])
    assert params["userId"] == "me"
    assert params["maxResults"] == 25
    q = params["q"]
    assert "project foo" in q
    assert "label:interviews" in q
    assert "from:a@b" in q
    assert "after:2026-01-01" in q
    assert "before:2026-04-01" in q
    assert cmd[-2:] == ["--format", "json"]


def test_list_messages_no_scope_omits_query_param() -> None:
    """When no scope parts are supplied, the ``q`` key is omitted from ``--params``."""

    def runner(cmd: list[str]) -> str:
        params = json.loads(cmd[6])
        assert "q" not in params
        assert params["userId"] == "me"
        return json.dumps({"resultSizeEstimate": 0})

    assert gmail_ingest.list_messages(runner=runner) == []


def test_list_messages_handles_zero_results() -> None:
    """Gmail omits the ``messages`` key entirely when there are zero matches."""

    def runner(cmd: list[str]) -> str:
        return json.dumps({"resultSizeEstimate": 0})

    assert gmail_ingest.list_messages(query="x", runner=runner) == []


def test_to_extracted_doc_handles_missing_subject() -> None:
    """Missing Subject header falls back to `(no subject)` and strips whitespace."""
    payload = {
        "mimeType": "text/plain",
        "headers": [{"name": "From", "value": "x@y"}],
        "body": {"data": _b64url("  hello\n\n  "), "size": 11},
    }
    doc = gmail_ingest.to_extracted_doc(
        {"id": "m1", "threadId": "t1", "payload": payload}
    )
    assert doc.title == "(no subject)"
    assert doc.content == "hello"
    assert doc.content_type == "email"
    assert doc.metadata["from"] == "x@y"
    assert doc.metadata["message_id"] == "m1"
    assert doc.metadata["thread_id"] == "t1"
    assert doc.metadata["date"] is None


def test_to_extracted_doc_handles_multipart_body() -> None:
    """A multipart message with text/plain in parts[] is decoded correctly."""
    msg = {
        "id": "m1",
        "threadId": "t1",
        "payload": {
            "mimeType": "multipart/alternative",
            "headers": [{"name": "Subject", "value": "Multi"}],
            "body": {"size": 0},
            "parts": [
                {
                    "mimeType": "text/plain",
                    "headers": [],
                    "body": {"data": _b64url("plain text body"), "size": 15},
                },
                {
                    "mimeType": "text/html",
                    "headers": [],
                    "body": {
                        "data": _b64url("<p>html body</p>"),
                        "size": 16,
                    },
                },
            ],
        },
    }
    doc = gmail_ingest.to_extracted_doc(msg)
    assert doc.title == "Multi"
    assert doc.content == "plain text body"


def test_to_extracted_doc_falls_back_to_html() -> None:
    """When only text/html is present, the HTML is stripped and used as the body."""
    html = (
        "<html><head><style>.x{color:red}</style></head>"
        "<body><p>Hello <b>world</b></p><script>alert(1)</script></body></html>"
    )
    msg = {
        "id": "m1",
        "threadId": "t1",
        "payload": {
            "mimeType": "multipart/alternative",
            "headers": [{"name": "Subject", "value": "HTML only"}],
            "body": {"size": 0},
            "parts": [
                {
                    "mimeType": "text/html",
                    "headers": [],
                    "body": {"data": _b64url(html), "size": len(html)},
                },
            ],
        },
    }
    doc = gmail_ingest.to_extracted_doc(msg)
    assert doc.title == "HTML only"
    # Style/script content dropped, tags stripped, whitespace collapsed.
    assert "Hello" in doc.content
    assert "world" in doc.content
    assert "color:red" not in doc.content
    assert "alert" not in doc.content
    assert "<" not in doc.content


def test_to_extracted_doc_uses_root_body_when_no_parts() -> None:
    """If payload has no text/plain or text/html parts, fall back to payload.body.data."""
    msg = {
        "id": "m1",
        "threadId": "t1",
        "payload": {
            # Non-text mime on root with no parts — forces the root-body fallback.
            "mimeType": "application/octet-stream",
            "headers": [{"name": "Subject", "value": "Root only"}],
            "body": {"data": _b64url("fallback content"), "size": 16},
        },
    }
    doc = gmail_ingest.to_extracted_doc(msg)
    assert doc.title == "Root only"
    assert doc.content == "fallback content"


def test_decode_body_data_handles_empty_string() -> None:
    """`_decode_body_data` short-circuits on empty input rather than raising."""
    assert gmail_ingest._decode_body_data("") == ""


def test_read_message_calls_gws_users_messages_get() -> None:
    """`read_message` issues `gws gmail users messages get --params ... --format json`."""
    captured: dict[str, list[str]] = {}

    def runner(cmd: list[str]) -> str:
        captured["cmd"] = cmd
        return json.dumps({"id": "m42", "threadId": "t42", "payload": {}})

    msg = gmail_ingest.read_message("m42", runner=runner)
    assert msg["id"] == "m42"
    cmd = captured["cmd"]
    assert cmd[:5] == ["gws", "gmail", "users", "messages", "get"]
    assert cmd[5] == "--params"
    params = json.loads(cmd[6])
    assert params == {"userId": "me", "id": "m42", "format": "full"}
    assert cmd[-2:] == ["--format", "json"]
