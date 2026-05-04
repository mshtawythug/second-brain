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
from pathlib import Path
from typing import Any

import psycopg
import pytest
from pytest_mock import MockerFixture
from typer.testing import CliRunner

from brain.cli import app
from brain.ingest import ExtractedDoc, ingest_document
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
    monkeypatch.setattr("brain.cli._build_embedder", lambda cfg: fake_embedder)


def test_ingest_gmail_requires_scope_flag(
    monkeypatch: pytest.MonkeyPatch,
    test_db: psycopg.Connection,
) -> None:
    """Bare `ingest-gmail` with no scope flags exits non-zero with a helpful message."""
    monkeypatch.setenv("DATABASE_URL", TEST_DATABASE_URL)
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


# ---------------------------------------------------------------------------
# Directory hook (Task B.2): Gmail ingest must populate ``directory_entries``
# in the same transaction as the document insert. The hook lives inside
# ``ingest_document`` gated on ``source_kind == "gmail"`` so every Gmail
# ingest path (CLI, MCP, future automation) gets it for free.
# ---------------------------------------------------------------------------


def _gmail_doc(
    *,
    body: str = "Hello world",
    title: str = "Hi",
    from_addr: str | None = "Ali Sarkis <redacted@example.com>",
    to: str | None = "person-x last-a <person-a@example.com>",
    message_id: str = "m1",
) -> ExtractedDoc:
    """Build an ``ExtractedDoc`` shaped like ``gmail.to_extracted_doc`` output."""
    return ExtractedDoc(
        title=title,
        content=body,
        content_type="email",
        source_path=None,
        metadata={
            "from": from_addr,
            "to": to,
            "date": "2026-04-01",
            "message_id": message_id,
            "thread_id": f"t{message_id}",
            "label_ids": ["INBOX"],
        },
    )


def test_gmail_ingest_upserts_directory_entries(
    test_db: psycopg.Connection,
    fake_embedder: object,
) -> None:
    """A successful Gmail ingest writes one ``directory_entries`` row per address.

    Display names are normalized via ``normalize_participant`` (lowercased,
    whitespace collapsed). Sources are tagged ``gmail`` and counts start at 1.
    """
    result = ingest_document(
        test_db,
        embedder=fake_embedder,
        doc=_gmail_doc(),
        source_kind="gmail",
        source_external_id="m1",
    )
    assert result.created is True

    rows = test_db.execute(
        "SELECT display_name, email, source, occurrence_count "
        "FROM directory_entries ORDER BY email"
    ).fetchall()
    assert rows == [
        ("person-a last-a", "person-a@example.com", "gmail", 1),
        ("ali sarkis", "redacted@example.com", "gmail", 1),
    ]


def test_re_ingesting_gmail_increments_occurrence(
    test_db: psycopg.Connection,
    fake_embedder: object,
) -> None:
    """Two emails from the same correspondent bump ``occurrence_count`` to 2.

    Different bodies + different message ids dodge the content-hash dedup
    so both ingests insert a document and trigger the directory hook.
    """
    ingest_document(
        test_db,
        embedder=fake_embedder,
        doc=_gmail_doc(body="First email", message_id="m1"),
        source_kind="gmail",
        source_external_id="m1",
    )
    ingest_document(
        test_db,
        embedder=fake_embedder,
        doc=_gmail_doc(body="Second email", message_id="m2"),
        source_kind="gmail",
        source_external_id="m2",
    )

    rows = test_db.execute(
        "SELECT email, occurrence_count FROM directory_entries ORDER BY email"
    ).fetchall()
    assert rows == [
        ("person-a@example.com", 2),
        ("redacted@example.com", 2),
    ]


def test_gmail_ingest_handles_bare_email(
    test_db: psycopg.Connection,
    fake_embedder: object,
) -> None:
    """A bare-email From header (no display name) lands as ``display_name=''``."""
    ingest_document(
        test_db,
        embedder=fake_embedder,
        doc=_gmail_doc(from_addr="bob@example.com", to=None),
        source_kind="gmail",
        source_external_id="m1",
    )

    rows = test_db.execute(
        "SELECT display_name, email, source FROM directory_entries"
    ).fetchall()
    assert rows == [("", "bob@example.com", "gmail")]


def test_gmail_ingest_with_no_from_or_to(
    test_db: psycopg.Connection,
    fake_embedder: object,
) -> None:
    """Empty ``from``/``to`` strings produce no upserts; ingest still succeeds."""
    result = ingest_document(
        test_db,
        embedder=fake_embedder,
        doc=ExtractedDoc(
            title="Empty headers",
            content="body content",
            content_type="email",
            source_path=None,
            metadata={"from": "", "to": ""},
        ),
        source_kind="gmail",
        source_external_id="m1",
    )
    assert result.created is True

    count = test_db.execute("SELECT count(*) FROM directory_entries").fetchone()
    assert count is not None
    assert count[0] == 0
    # Document itself was still written.
    doc_count = test_db.execute("SELECT count(*) FROM documents").fetchone()
    assert doc_count is not None
    assert doc_count[0] == 1


def test_directory_upsert_skipped_for_non_gmail_source(
    test_db: psycopg.Connection,
    fake_embedder: object,
) -> None:
    """Other sources (manual, krisp, slack) MUST NOT touch ``directory_entries``.

    The hook is Gmail-specific in B.2; Krisp gets its own treatment in B.3.
    """
    ingest_document(
        test_db,
        embedder=fake_embedder,
        doc=ExtractedDoc(
            title="A note",
            content="manual body",
            content_type="note",
            source_path=None,
            metadata={"from": "Ali <redacted@example.com>", "to": "x@y.com"},
        ),
        source_kind="manual",
    )

    count = test_db.execute("SELECT count(*) FROM directory_entries").fetchone()
    assert count is not None
    assert count[0] == 0


def test_directory_upsert_runs_in_same_transaction(
    test_db: psycopg.Connection,
    fake_embedder: object,
    mocker: MockerFixture,
) -> None:
    """If the directory upsert raises, the document insert rolls back too.

    Forces the second ``upsert_pair`` call to raise ``RuntimeError``. The
    outer ``with conn.transaction():`` block in ``ingest_document`` must
    propagate the failure and roll back the partially-inserted document +
    chunks + the first directory row.
    """
    real_upsert = mocker.patch(
        "brain.ingest.DirectoryStore.upsert_pair",
        autospec=True,
    )

    call_state = {"n": 0}

    def flaky(self: Any, **kwargs: Any) -> None:
        call_state["n"] += 1
        if call_state["n"] == 2:
            raise RuntimeError("simulated directory failure")
        # First call: emulate a real upsert by writing the row directly.
        # We bypass DirectoryStore.upsert_pair to avoid recursion through
        # the patched mock.
        self._conn.execute(
            "INSERT INTO directory_entries (display_name, email, source) "
            "VALUES (%s, %s, %s)",
            (
                (kwargs["display_name"] or "").lower(),
                kwargs["email"].lower(),
                kwargs["source"],
            ),
        )

    real_upsert.side_effect = flaky

    with pytest.raises(RuntimeError, match="simulated directory failure"):
        ingest_document(
            test_db,
            embedder=fake_embedder,
            doc=_gmail_doc(),  # has both From and To → 2 upsert calls
            source_kind="gmail",
            source_external_id="m1",
        )

    # The transaction rolled back: no documents, no chunks, no directory rows.
    docs = test_db.execute("SELECT count(*) FROM documents").fetchone()
    chunks = test_db.execute("SELECT count(*) FROM chunks").fetchone()
    dir_rows = test_db.execute("SELECT count(*) FROM directory_entries").fetchone()
    assert docs is not None and docs[0] == 0
    assert chunks is not None and chunks[0] == 0
    assert dir_rows is not None and dir_rows[0] == 0


def test_ingest_gmail_creates_vault_mirror(
    monkeypatch: pytest.MonkeyPatch,
    test_db: psycopg.Connection,
    fake_embedder: object,
    tmp_path: Path,
) -> None:
    """`brain ingest-gmail` writes a mirror under ``<vault>/_ingested/gmail/``.

    Setup: stub the gws CLI so a single message is returned; sandbox
    ``BRAIN_VAULT_PATH`` to ``tmp_path``.
    Exercise: invoke ``ingest-gmail --label`` to pull the stubbed message.
    Verify: a Markdown file lands under ``_ingested/gmail/`` with the
    message body inside.
    """
    _patch_embedder(monkeypatch, fake_embedder)
    monkeypatch.setenv("BRAIN_VAULT_PATH", str(tmp_path))
    msgs = {
        "m1": _msg(
            id="m1",
            subject="Mirror this email",
            from_addr="n@x",
            body="Mirror body content",
        ),
    }
    monkeypatch.setattr("brain.ingest.gmail._run", _fake_runner(msgs))

    result = CliRunner().invoke(app, ["ingest-gmail", "--label", "interviews"])

    assert result.exit_code == 0, result.output
    mirror_dir = tmp_path / "_ingested" / "gmail"
    assert mirror_dir.is_dir(), f"missing mirror dir: {mirror_dir}"
    mirrors = list(mirror_dir.glob("*.md"))
    assert len(mirrors) == 1, f"expected one mirror file, got {mirrors}"
    assert "Mirror body content" in mirrors[0].read_text(encoding="utf-8")


def test_cli_ingest_gmail_populates_directory(
    monkeypatch: pytest.MonkeyPatch,
    test_db: psycopg.Connection,
    fake_embedder: object,
) -> None:
    """End-to-end: ``brain ingest-gmail`` writes ``directory_entries`` rows."""
    _patch_embedder(monkeypatch, fake_embedder)
    msgs = {
        "m1": _msg(
            id="m1",
            subject="Hi",
            from_addr="Ali Sarkis <redacted@example.com>",
            to="person-x last-a <person-a@example.com>",
            body="Hello there",
        ),
    }
    monkeypatch.setattr("brain.ingest.gmail._run", _fake_runner(msgs))
    result = CliRunner().invoke(app, ["ingest-gmail", "--label", "interviews"])
    assert result.exit_code == 0, result.output

    with psycopg.connect(TEST_DATABASE_URL) as conn:
        rows = conn.execute(
            "SELECT display_name, email, source FROM directory_entries "
            "ORDER BY email"
        ).fetchall()
    assert rows == [
        ("person-a last-a", "person-a@example.com", "gmail"),
        ("ali sarkis", "redacted@example.com", "gmail"),
    ]


# ---------------------------------------------------------------------------
# P1.3 — RFC header capture in ``to_extracted_doc``. Each branch mirrors a
# distinct ``_PROMOTED_COLUMNS`` feeder in ``brain.ingest`` so the typed-column
# promotion landed in P1.1 actually gets fed real data.
# ---------------------------------------------------------------------------


def _msg_with_headers(headers: dict[str, str], *, body: str = "Hi") -> dict[str, Any]:
    """Build a Gmail Message resource with arbitrary ``payload.headers``."""
    return {
        "id": "m1",
        "threadId": "t1",
        "payload": {
            "mimeType": "text/plain",
            "headers": [{"name": k, "value": v} for k, v in headers.items()],
            "body": {"data": _b64url(body), "size": len(body)},
        },
    }


def test_to_extracted_doc_captures_rfc_message_id() -> None:
    """``Message-ID`` header → ``metadata.rfc_message_id`` (verbatim, brackets kept)."""
    msg = _msg_with_headers(
        {"Subject": "Hi", "Message-ID": "<abc@example.com>", "From": "x@y"}
    )
    doc = gmail_ingest.to_extracted_doc(msg)
    assert doc.metadata["rfc_message_id"] == "<abc@example.com>"


def test_to_extracted_doc_captures_in_reply_to() -> None:
    """``In-Reply-To`` header → ``metadata.in_reply_to`` (verbatim)."""
    msg = _msg_with_headers(
        {"Subject": "Re: Hi", "In-Reply-To": "<parent@example.com>", "From": "x@y"}
    )
    doc = gmail_ingest.to_extracted_doc(msg)
    assert doc.metadata["in_reply_to"] == "<parent@example.com>"


def test_to_extracted_doc_parses_date_to_iso_utc() -> None:
    """RFC 2822 ``Date:`` → ISO-8601 UTC string in ``metadata.sent_at``.

    The original raw ``date`` field is preserved for backwards compat — code
    paths that haven't migrated to ``sent_at`` keep working.
    """
    msg = _msg_with_headers(
        {
            "Subject": "Hi",
            "Date": "Tue, 28 Apr 2026 16:38:01 -0400",
            "From": "x@y",
        }
    )
    doc = gmail_ingest.to_extracted_doc(msg)
    assert doc.metadata["sent_at"] == "2026-04-28T20:38:01+00:00"
    # Raw value is preserved.
    assert doc.metadata["date"] == "Tue, 28 Apr 2026 16:38:01 -0400"


def test_to_extracted_doc_unparseable_date_does_not_crash() -> None:
    """A garbage ``Date:`` header drops ``sent_at`` but keeps the raw value."""
    msg = _msg_with_headers(
        {"Subject": "Hi", "Date": "garbage", "From": "x@y"}
    )
    doc = gmail_ingest.to_extracted_doc(msg)
    assert "sent_at" not in doc.metadata
    assert doc.metadata["date"] == "garbage"


def test_to_extracted_doc_missing_message_id_omits_field() -> None:
    """When the ``Message-ID`` header is absent, the metadata key is omitted entirely.

    Downstream column-promotion treats missing keys as "leave the column as
    NULL", which is the desired behavior — we never want to write empty strings.
    """
    msg = _msg_with_headers({"Subject": "Hi", "From": "x@y"})
    doc = gmail_ingest.to_extracted_doc(msg)
    assert "rfc_message_id" not in doc.metadata
    assert "in_reply_to" not in doc.metadata
    # ``date`` raw key still exists (set to None) since we always emit it.
    assert doc.metadata["date"] is None


def test_to_extracted_doc_strips_boilerplate_from_body() -> None:
    """Body extraction routes through ``strip_boilerplate`` before doc construction."""
    body = "Real content here.\n\nSent from my iPhone"
    msg = _msg_with_headers({"Subject": "Hi", "From": "x@y"}, body=body)
    doc = gmail_ingest.to_extracted_doc(msg)
    assert "Sent from my iPhone" not in doc.content
    assert "Real content here." in doc.content


def test_to_extracted_doc_naive_date_treated_as_utc() -> None:
    """A ``Date:`` value with no timezone offset is treated as UTC.

    ``parsedate_to_datetime`` returns a naive datetime when the input has no
    timezone offset (and isn't a known TZ abbreviation); the parser explicitly
    coerces such values to UTC rather than raising or skipping.
    """
    msg = _msg_with_headers(
        {"Subject": "Hi", "Date": "Tue, 28 Apr 2026 16:38:01 -0000", "From": "x@y"}
    )
    doc = gmail_ingest.to_extracted_doc(msg)
    # ``-0000`` is RFC 2822's "no meaningful TZ" sentinel; some parsers return
    # it as naive. Either way our code emits a UTC-anchored ISO string.
    assert doc.metadata["sent_at"].endswith("+00:00")
    assert "2026-04-28T16:38:01" in doc.metadata["sent_at"]
