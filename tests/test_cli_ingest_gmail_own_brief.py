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


def test_only_the_owners_own_brief_mail_is_skipped(
    monkeypatch: pytest.MonkeyPatch,
    test_db: psycopg.Connection,
    fake_embedder: object,
    tmp_path: Path,
) -> None:
    monkeypatch.setenv("DATABASE_URL", TEST_DATABASE_URL)
    monkeypatch.setenv("BRAIN_VAULT_PATH", str(tmp_path))
    monkeypatch.setenv("BRAIN_USER_EMAIL", OWNER)
    monkeypatch.setattr("brain.cli._build_embedder", lambda cfg: fake_embedder)
    messages = {
        # the owner's delivered brief: skipped
        "m1": _msg("m1", "t-own-brief", sender=f"Owner <{OWNER}>",
                   subject="[brain brief] 2026-06-09"),
        # a third party's mail with the same subject: ingested
        "m2": _msg("m2", "t-third-party", sender="someone@example.invalid",
                   subject="[brain brief] 2026-06-09"),
        # the owner's other sent mail: ingested
        "m3": _msg("m3", "t-own-other", sender=OWNER.upper(), subject="Quarterly plan"),
        # a thread holding the brief and the owner's reply: the reply is ingested
        "m4": _msg("m4", "t-mixed", sender=OWNER, subject="[brain brief] 2026-06-08"),
        "m5": _msg("m5", "t-mixed", sender=OWNER, subject="Re: [brain brief] 2026-06-08"),
    }
    monkeypatch.setattr("brain.ingest.gmail._run", _runner(messages))

    result = CliRunner().invoke(app, ["ingest-gmail", "--since", "2026/06/01"])

    assert result.exit_code == 0, result.output
    rows = test_db.execute(
        "SELECT s.external_id, d.content FROM documents d JOIN sources s ON s.id = d.source_id "
        "WHERE d.content_type = 'email_thread' ORDER BY s.external_id"
    ).fetchall()
    assert [r[0] for r in rows] == ["t-mixed", "t-own-other", "t-third-party"]
    mixed = dict(rows)["t-mixed"]
    assert "synthetic body m5" in mixed and "synthetic body m4" not in mixed
    assert "failed" not in result.output.replace("0 failed", "")
