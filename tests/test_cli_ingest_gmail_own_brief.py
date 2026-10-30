"""``brain ingest-gmail`` end to end: the owner's own brief mail is skipped, nothing else (RM-16).

A separate file because ``tests/test_cli_ingest_gmail.py`` is past 800 lines. The ``gws`` runner
is a fake (the module-level ``_run`` is patched, as in that file), so nothing reaches Gmail. All
messages, addresses and bodies are synthetic.
"""
from __future__ import annotations

import base64
import json
import os
from pathlib import Path
from typing import Any

import psycopg
import pytest
from typer.testing import CliRunner

from brain.cli import app

TEST_DATABASE_URL = os.environ.get(
    "TEST_DATABASE_URL",
    "postgresql://brain:brain@localhost:5434/second_brain_test",
)
OWNER = "owner@example.invalid"


def _msg(mid: str, thread: str, *, sender: str, subject: str) -> dict[str, Any]:
    body = f"synthetic body {mid}"
    return {
        "id": mid,
        "threadId": thread,
        "labelIds": ["INBOX"],
        "payload": {
            "mimeType": "text/plain",
            "headers": [
                {"name": "From", "value": sender},
                {"name": "To", "value": OWNER},
                {"name": "Subject", "value": subject},
                {"name": "Date", "value": "Tue, 09 Jun 2026 07:00:00 +0000"},
            ],
            "body": {"data": base64.urlsafe_b64encode(body.encode()).decode(), "size": len(body)},
        },
    }


def _runner(messages: dict[str, dict[str, Any]]) -> Any:
    def run(cmd: list[str], *_args: object, **_kwargs: object) -> str:
        if cmd[4] == "list":
            stubs = [{"id": m, "threadId": r["threadId"]} for m, r in messages.items()]
            return json.dumps({"messages": stubs, "resultSizeEstimate": len(stubs)})
        return json.dumps(messages[json.loads(cmd[6])["id"]])

    return run


def _setup(monkeypatch: pytest.MonkeyPatch, fake_embedder: object, tmp_path: Path) -> None:
    monkeypatch.setenv("DATABASE_URL", TEST_DATABASE_URL)
    monkeypatch.setenv("BRAIN_VAULT_PATH", str(tmp_path))
    monkeypatch.setenv("BRAIN_USER_EMAIL", OWNER)
    monkeypatch.setattr("brain.cli._build_embedder", lambda cfg: fake_embedder)
    monkeypatch.setattr("brain.ingest.gmail._run", _runner(MESSAGES))


MESSAGES = {
    # the owner's delivered brief: skipped
    "m1": _msg("m1", "t-own-brief", sender=f"Owner <{OWNER}>", subject="[brain brief] 2026-06-09"),
    # a third party's mail with the same subject: ingested
    "m2": _msg("m2", "t-third-party", sender="someone@example.invalid",
               subject="[brain brief] 2026-06-09"),
    # the owner's other sent mail, address in another case: ingested
    "m3": _msg("m3", "t-own-other", sender=OWNER.upper(), subject="Quarterly plan"),
    # the brief, the owner's reply (it quotes the brief: skipped), and a stranger's reply (kept)
    "m4": _msg("m4", "t-mixed", sender=OWNER, subject="[brain brief] 2026-06-08"),
    "m5": _msg("m5", "t-mixed", sender=OWNER, subject="Re: [brain brief] 2026-06-08"),
    "m6": _msg("m6", "t-mixed", sender="someone@example.invalid",
               subject="RE: Re: [brain brief] 2026-06-08"),
}
#: Every scope flag reaches the same fetch path (list_messages, then each thread read in full).
SCOPES = [
    ["--query", "brief"],
    ["--label", "inbox"],
    ["--from", OWNER],
    ["--since", "2026/06/01"],
    ["--until", "2026/07/01"],
]


def _threads(conn: psycopg.Connection) -> dict[str, str]:
    rows = conn.execute(
        "SELECT s.external_id, d.content FROM documents d JOIN sources s ON s.id = d.source_id "
        "WHERE d.content_type = 'email_thread'"
    ).fetchall()
    return {r[0]: r[1] for r in rows}


@pytest.mark.parametrize("scope", SCOPES, ids=[s[0] for s in SCOPES])
def test_only_the_owners_own_brief_mail_is_skipped(
    monkeypatch: pytest.MonkeyPatch,
    test_db: psycopg.Connection,
    fake_embedder: object,
    tmp_path: Path,
    scope: list[str],
) -> None:
    _setup(monkeypatch, fake_embedder, tmp_path)

    result = CliRunner().invoke(app, ["ingest-gmail", *scope])

    assert result.exit_code == 0, result.output
    threads = _threads(test_db)
    assert sorted(threads) == ["t-mixed", "t-own-other", "t-third-party"]
    mixed = threads["t-mixed"]
    assert "synthetic body m6" in mixed
    assert "synthetic body m4" not in mixed and "synthetic body m5" not in mixed
    assert "0 failed" in result.output


def test_dry_run_marks_the_owners_own_brief_thread_as_skipped(
    monkeypatch: pytest.MonkeyPatch,
    test_db: psycopg.Connection,
    fake_embedder: object,
    tmp_path: Path,
) -> None:
    _setup(monkeypatch, fake_embedder, tmp_path)

    result = CliRunner().invoke(app, ["ingest-gmail", "--label", "inbox", "--dry-run"])

    assert result.exit_code == 0, result.output
    lines = {
        line.split("]")[0]: line for line in result.output.splitlines() if "thread_id=" in line
    }
    assert "skipped: your own delivered brief" in lines["  [thread_id=t-own-brief messages=1"]
    assert "Subject: Quarterly plan" in lines["  [thread_id=t-own-other messages=1"]
    # the mixed thread's first kept message is the stranger's reply
    assert "Subject: RE: Re: [brain brief] 2026-06-08" in lines["  [thread_id=t-mixed messages=3"]


def test_without_an_owner_address_nothing_is_skipped(
    monkeypatch: pytest.MonkeyPatch,
    test_db: psycopg.Connection,
    fake_embedder: object,
    tmp_path: Path,
) -> None:
    _setup(monkeypatch, fake_embedder, tmp_path)
    monkeypatch.delenv("BRAIN_USER_EMAIL")

    result = CliRunner().invoke(app, ["ingest-gmail", "--label", "inbox"])

    assert result.exit_code == 0, result.output
    assert sorted(_threads(test_db)) == ["t-mixed", "t-own-brief", "t-own-other", "t-third-party"]
