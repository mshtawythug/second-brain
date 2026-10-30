"""``build_brief`` delivers the published brief (RM-16), and a delivery problem never breaks it.

Every test passes ``delivery_env`` as a plain dict and a fake channel (or a channel whose runner
is a fake), so nothing is sent and the process environment is not read. All data is synthetic.
"""
from __future__ import annotations

import logging
import os
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import date
from pathlib import Path
from typing import Any

import psycopg
import pytest

from brain import chat
from brain.brief_delivery import DeliveryAuthError, DeliveryError, make_channel
from brain.brief_publish import build_brief
from brain.config import Config
from brain.db import connect
from brain.errors import OllamaUnavailable
from brain.saved_search import MODE_LEXICAL, WatchFilters
from brain.saved_search_check import Matcher, add_watch
from brain.settings.brief_delivery import load_brief_delivery_settings
from tests.saved_search_fakes import WordEmbedder, ingest

TEST_DATABASE_URL = os.environ.get(
    "TEST_DATABASE_URL",
    "postgresql://brain:brain@localhost:5434/second_brain_test",
)
ON_DATE = date(2026, 6, 9)
OWNER = "owner@example.invalid"
EMAIL_ENV = {"BRAIN_BRIEF_DELIVERY": "email", "BRAIN_USER_EMAIL": OWNER}


def _cfg(vault: Path, *, user_email: str | None = OWNER) -> Config:
    return Config(
        database_url=TEST_DATABASE_URL,
        vault_path=vault,
        brief_since_hours=24,
        brief_todo_since_days=7,
        brief_capture_limit=20,
        brief_pin_limit=10,
        user_email=user_email,
    )


@pytest.fixture(autouse=True)
def _no_chat(monkeypatch: pytest.MonkeyPatch) -> None:
    def _boom(*_a: object, **_k: object) -> dict[str, object]:
        raise OllamaUnavailable("down")

    monkeypatch.setattr(chat, "chat_json", _boom)


class _FakeChannel:
    name = "fake"

    def __init__(self, error: Exception | None = None) -> None:
        self.sent: list[tuple[str, str]] = []
        self.error = error

    def send(self, *, subject: str, body: str) -> None:
        self.sent.append((subject, body))
        if self.error is not None:
            raise self.error


def _build(vault: Path, *, wiki: bool = True, cfg: Config | None = None, **kwargs: Any) -> Any:
    return build_brief(
        cfg or _cfg(vault),
        on_date=ON_DATE,
        since_hours=24,
        todo_since_days=7,
        enrich=False,
        wiki=wiki,
        **kwargs,
    )


def _lexical_matcher() -> Matcher:
    def _never() -> Any:
        raise AssertionError("a lexical check must not build an embedder")

    return Matcher(embedder=_never, vector_sim_floor=0.25, min_cosine=0.45)


def _watch_with_one_hit(conn: psycopg.Connection) -> None:
    add_watch(
        conn,
        name="zephyr",
        query="zephyr",
        filters=WatchFilters(),
        mode=MODE_LEXICAL,
        matcher=_lexical_matcher(),
    )
    ingest(conn, WordEmbedder(), title="Zephyr open", content="zephyr open plan")


def _unnotified(conn: psycopg.Connection) -> int:
    row = conn.execute(
        "SELECT count(*) FROM saved_search_hits WHERE notified_at IS NULL"
    ).fetchone()
    return int(row[0])


def _todo_doc(conn: psycopg.Connection, body: str) -> None:
    conn.execute(
        "INSERT INTO documents (title, content, content_hash, content_type) "
        "VALUES ('Action items', %s, %s, 'krisp_action_items')",
        (body, str(uuid.uuid4())),
    )


def test_build_brief_delivers_the_gated_payload_only(
    test_db: psycopg.Connection, tmp_path: Path
) -> None:
    words = WordEmbedder()
    _watch_with_one_hit(test_db)
    ingest(test_db, words, title="Zephyr sealed", content="zephyr sealed",
           sensitivity="confidential")
    ingest(test_db, words, title="Sealed capture", content="sealed body",
           sensitivity="confidential")
    _todo_doc(test_db, "- [ ] synthetic todo text\n")
    channel = _FakeChannel()

    data, written = _build(tmp_path, delivery_env=EMAIL_ENV, delivery_channel=channel)

    assert written is not None
    assert "Sealed capture" in {doc.title for doc in data.captures}  # the terminal has it
    assert len(channel.sent) == 1
    subject, body = channel.sent[0]
    assert subject == "[brain brief] 2026-06-09"
    assert "Zephyr open" in body
    assert "Sealed capture" not in body
    assert "Zephyr sealed" not in body
    assert "synthetic todo text" not in body
    assert "Open action items: 1" in body
    assert _unnotified(test_db) == 0


def test_the_email_goes_only_to_the_owner(test_db: psycopg.Connection, tmp_path: Path) -> None:
    calls: list[list[str]] = []
    settings = load_brief_delivery_settings(EMAIL_ENV)
    channel = make_channel(settings, runner=lambda argv: calls.append(argv) or "")

    _build(tmp_path, delivery_env=EMAIL_ENV, delivery_channel=channel)

    assert len(calls) == 1
    argv = calls[0]
    assert argv[:3] == ["gws", "gmail", "+send"]
    assert [arg for arg in argv if arg.startswith("--to")] == [f"--to={OWNER}"]
    assert "--subject=[brain brief] 2026-06-09" in argv


def test_build_brief_with_delivery_off_calls_no_channel(
    test_db: psycopg.Connection, tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.DEBUG, logger="brain.brief_delivery")
    _watch_with_one_hit(test_db)
    channel = _FakeChannel()

    _, written = _build(tmp_path, delivery_env={}, delivery_channel=channel)

    assert written is not None
    assert channel.sent == []
    assert _unnotified(test_db) == 0
    assert [r for r in caplog.records if r.name.startswith("brain.brief_delivery")] == []


def test_no_delivery_without_wiki(test_db: psycopg.Connection, tmp_path: Path) -> None:
    channel = _FakeChannel()

    _, written = _build(tmp_path, wiki=False, delivery_env=EMAIL_ENV, delivery_channel=channel)

    assert written is None
    assert channel.sent == []


def test_build_brief_survives_a_failing_channel(
    test_db: psycopg.Connection, tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.WARNING)
    _watch_with_one_hit(test_db)

    _, written = _build(
        tmp_path,
        delivery_env=EMAIL_ENV,
        delivery_channel=_FakeChannel(DeliveryError("x", log_detail="exit 2")),
    )

    assert written is not None and "Zephyr open" in written.read_text()
    assert len(caplog.records) == 1
    assert "DeliveryError[exit 2]" in caplog.records[0].getMessage()
    # D3: the hit was never sent, so it stays pending and goes out with the next brief.
    assert _unnotified(test_db) == 1
    retry = _FakeChannel()
    _build(tmp_path, delivery_env=EMAIL_ENV, delivery_channel=retry)
    assert "Zephyr open" in retry.sent[0][1]
    assert _unnotified(test_db) == 0


def test_an_auth_failure_from_build_brief_names_gws_reauth(
    test_db: psycopg.Connection, tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.WARNING)

    _, written = _build(
        tmp_path, delivery_env=EMAIL_ENV, delivery_channel=_FakeChannel(DeliveryAuthError())
    )

    assert written is not None
    assert len(caplog.records) == 1
    assert "bin/gws-reauth" in caplog.records[0].getMessage()


@pytest.mark.parametrize(
    "env",
    [
        {"BRAIN_BRIEF_DELIVERY": "slack",
         "BRAIN_BRIEF_SLACK_WEBHOOK_URL": "https://hooks.example.invalid/x"},
        {"BRAIN_BRIEF_DELIVERY": "email"},
        {"BRAIN_BRIEF_DELIVERY": "pigeon"},
    ],
)
def test_a_delivery_config_error_degrades_to_one_warning(
    test_db: psycopg.Connection,
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
    env: dict[str, str],
) -> None:
    caplog.set_level(logging.WARNING)
    _watch_with_one_hit(test_db)
    channel = _FakeChannel()

    _, written = _build(tmp_path, delivery_env=env, delivery_channel=channel)

    assert written is not None
    assert channel.sent == []
    assert len(caplog.records) == 1
    message = caplog.records[0].getMessage()
    assert "ConfigError" in message and "brain doctor" in message
    assert "hooks.example.invalid" not in caplog.text
    assert _unnotified(test_db) == 1


def test_a_recipient_other_than_the_configured_owner_is_refused(
    test_db: psycopg.Connection, tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.WARNING)
    channel = _FakeChannel()

    _build(
        tmp_path,
        cfg=_cfg(tmp_path, user_email="someone-else@example.invalid"),
        delivery_env=EMAIL_ENV,
        delivery_channel=channel,
    )

    assert channel.sent == []
    assert len(caplog.records) == 1
    assert "BRAIN_USER_EMAIL" in caplog.records[0].getMessage()
    assert OWNER not in caplog.text


# --- review round 1 (rev-82 / aud-82 / sec-82) ------------------------------------------------


@pytest.mark.parametrize(
    "env",
    [
        {"BRAIN_BRIEF_DELIVERY": "none",
         "BRAIN_BRIEF_SLACK_WEBHOOK_URL": "http://hooks.example.invalid/x"},
        {"BRAIN_BRIEF_DELIVERY_INCLUDE_TODOS": "maybe"},
    ],
)
def test_delivery_off_is_silent_whatever_the_other_knobs_say(
    test_db: psycopg.Connection,
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
    env: dict[str, str],
) -> None:
    # rev-82 H1: with the channel off, a malformed delivery knob must not warn or hold hits.
    caplog.set_level(logging.WARNING)
    _watch_with_one_hit(test_db)
    channel = _FakeChannel()

    _, written = _build(tmp_path, delivery_env=env, delivery_channel=channel)

    assert written is not None
    assert channel.sent == []
    assert caplog.records == []
    assert _unnotified(test_db) == 0


@pytest.mark.parametrize(
    "url",
    [
        "https://hooks.slack.com/services/T0/B0/x\x7fy",
        "https://hooks.slack.com/services/T0/B0/x?a=\x01",
    ],
)
def test_a_control_character_in_the_webhook_degrades_to_one_warning(
    test_db: psycopg.Connection, tmp_path: Path, caplog: pytest.LogCaptureFixture, url: str
) -> None:
    # rev-82 H2 / aud-82 1: httpx.InvalidURL is not an httpx.HTTPError; it must not escape.
    caplog.set_level(logging.WARNING)
    _watch_with_one_hit(test_db)

    _, written = _build(
        tmp_path,
        delivery_env={"BRAIN_BRIEF_DELIVERY": "slack", "BRAIN_BRIEF_SLACK_WEBHOOK_URL": url},
    )

    assert written is not None
    assert len(caplog.records) == 1
    assert "ConfigError" in caplog.records[0].getMessage()
    assert "T0" not in caplog.text
    assert _unnotified(test_db) == 1


def test_drafts_are_never_delivered(test_db: psycopg.Connection, tmp_path: Path) -> None:
    # sec-82 1: a draft capture, pin or watch hit stays local; the terminal is unchanged.
    words = WordEmbedder()
    add_watch(
        test_db, name="zephyr", query="zephyr", filters=WatchFilters(), mode=MODE_LEXICAL,
        matcher=_lexical_matcher(),
    )
    hit = ingest(test_db, words, title="Zephyr draft hit", content="zephyr draft plan")
    ingest(test_db, words, title="Zephyr open hit", content="zephyr open plan")
    draft = ingest(test_db, words, title="Draft capture", content="draft body")
    _build(tmp_path, wiki=False)  # records both hits, marks nothing
    test_db.execute(
        "UPDATE documents SET draft = TRUE WHERE id = ANY(%s::uuid[])", ([hit, draft],)
    )
    test_db.execute(
        "INSERT INTO interactions (document_id, action, source) VALUES (%s, 'pinned', 'cli')",
        (draft,),
    )
    channel = _FakeChannel()

    data, written = _build(tmp_path, delivery_env=EMAIL_ENV, delivery_channel=channel)

    (_, body), = channel.sent
    assert "Zephyr open hit" in body
    assert "Zephyr draft hit" not in body
    assert "Draft capture" not in body
    terminal = {doc.title for doc in data.captures} | {h.title for h in data.watch_hits}
    assert {"Draft capture", "Zephyr draft hit"} <= terminal
    assert written is not None and "Zephyr draft hit" in written.read_text()


def test_a_failed_draft_lookup_skips_delivery(
    test_db: psycopg.Connection, tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    # D13 fails closed: without the lookup a draft could be sent.
    caplog.set_level(logging.WARNING)
    _watch_with_one_hit(test_db)
    opened: list[str] = []

    @contextmanager
    def flaky(url: str) -> Iterator[psycopg.Connection[Any]]:
        opened.append(url)
        if len(opened) == 2:  # the brief's own connection, then the draft lookup's
            raise psycopg.OperationalError("synthetic outage")
        with connect(url) as conn:
            yield conn

    channel = _FakeChannel()

    _, written = _build(tmp_path, delivery_env=EMAIL_ENV, delivery_channel=channel, connect=flaky)

    assert written is not None
    assert channel.sent == []
    assert len(caplog.records) == 1
    assert "OperationalError" in caplog.records[0].getMessage()
    assert "synthetic outage" not in caplog.text
    assert _unnotified(test_db) == 1
