"""``brain ingest-gmail`` skips the owner's own delivered briefs, in Python (RM-16, AC6).

The Gmail query is left alone: what Gmail makes of a negated group cannot be checked offline, and
a misread would silently drop all of the owner's sent mail. So the skip is a predicate on the
fetched message's ``From`` and ``Subject``: the owner's address AND the brief's subject prefix
(after any ``Re:`` / ``Fwd:`` / ``Fw:``), both. Third-party mail and the owner's other mail are
ingested. Messages are synthetic; the runner is a fake, so nothing reaches ``gws``.
"""
from __future__ import annotations

import json
import logging

import pytest

from brain import brief_delivery
from brain.brief_mail import SUBJECT_PREFIX, is_own_brief_mail, is_own_brief_message
from brain.ingest import gmail
from brain.ingest.gmail import _build_query
from brain.ingest.gmail_brief_skip import read_thread_messages

OWNER = "owner@example.invalid"
BRIEF_SUBJECT = f"{SUBJECT_PREFIX} 2026-06-09"


def _msg(message_id: str, *, sender: str, subject: str) -> dict[str, object]:
    return {
        "id": message_id,
        "threadId": "t1",
        "payload": {"headers": [{"name": "From", "value": sender},
                                {"name": "Subject", "value": subject}]},
    }


@pytest.mark.parametrize(
    "sender",
    [
        OWNER,
        "OWNER@Example.Invalid",
        f"Owner Name <{OWNER}>",
        f'"Owner, Name" <{OWNER.upper()}>',
    ],
)
def test_the_owners_own_brief_mail_is_recognised(sender: str) -> None:
    assert is_own_brief_mail(sender, BRIEF_SUBJECT, OWNER)


@pytest.mark.parametrize(
    "subject",
    [
        f"Re: {BRIEF_SUBJECT}",
        f"Fwd: {BRIEF_SUBJECT}",
        f"FW: {BRIEF_SUBJECT}",
        f"re:  RE: Fwd:{BRIEF_SUBJECT}",
        f"  {BRIEF_SUBJECT}",
    ],
)
def test_the_owners_replies_and_forwards_of_a_brief_are_recognised(subject: str) -> None:
    # Ruling: they quote the brief's body, so they are skipped too.
    assert is_own_brief_mail(OWNER, subject, OWNER)


@pytest.mark.parametrize(
    ("sender", "subject"),
    [
        ("someone@example.invalid", BRIEF_SUBJECT),  # a third party's mail with that subject
        (OWNER, "Quarterly plan"),  # the owner's other sent mail
        (OWNER, f"Notes on {BRIEF_SUBJECT}"),  # the prefix is not at the start
        (OWNER, f"Reply: {BRIEF_SUBJECT}"),  # not a reply marker
        ("someone@example.invalid", f"Re: {BRIEF_SUBJECT}"),  # a stranger's reply
        ("Someone <someone@example.invalid>", f"{BRIEF_SUBJECT} {OWNER}"),
        ("", BRIEF_SUBJECT),
        (OWNER, ""),
    ],
)
def test_anything_else_is_not_the_owners_brief(sender: str, subject: str) -> None:
    assert not is_own_brief_mail(sender, subject, OWNER)


def test_without_an_owner_address_nothing_is_skipped() -> None:
    assert not is_own_brief_mail(OWNER, BRIEF_SUBJECT, None)
    assert not is_own_brief_mail(OWNER, BRIEF_SUBJECT, "")


def test_the_prefix_is_the_one_delivery_sends() -> None:
    subject, _ = brief_delivery.render_delivery(
        brief_delivery.BriefData(
            date=__import__("datetime").date(2026, 6, 9),
            captures=[], open_todos=[], pinned=[], suggestions=[],
        ),
        include_todos=False,
    )

    assert brief_delivery.SUBJECT_PREFIX is SUBJECT_PREFIX
    assert subject == BRIEF_SUBJECT
    assert is_own_brief_mail(OWNER, subject, OWNER)


def test_read_thread_messages_drops_only_the_owners_brief_and_logs_a_count(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.DEBUG)
    messages = {
        "m1": _msg("m1", sender=OWNER, subject=BRIEF_SUBJECT),
        "m2": _msg("m2", sender="someone@example.invalid", subject=BRIEF_SUBJECT),
        "m3": _msg("m3", sender=f"Owner <{OWNER}>", subject="Quarterly plan"),
        "m4": _msg("m4", sender=OWNER, subject=f"Re: {BRIEF_SUBJECT}"),
    }

    def runner(argv: list[str], _runner: object = None) -> str:
        params = json.loads(argv[argv.index("--params") + 1])
        return json.dumps(messages[params["id"]])

    monkeypatch.setattr(gmail, "_run", runner)

    kept = read_thread_messages([{"id": m} for m in messages], owner=OWNER)

    assert [m["id"] for m in kept] == ["m2", "m3"]
    assert "skipped 2" in caplog.text
    assert OWNER not in caplog.text and "brain brief" not in caplog.text


def test_a_message_without_headers_is_ingested() -> None:
    assert not is_own_brief_message({"id": "m9"}, OWNER)
    assert not is_own_brief_message({"payload": {"headers": None}}, OWNER)


def test_the_gmail_query_is_unchanged() -> None:
    # The skip is not in the query: no scope flag means no q at all, as before RM-16.
    assert _build_query(query=None, label=None, since=None, until=None, from_addr=None) == ""
    assert "brain brief" not in _build_query(
        query="x", label="l", since="2026/01/01", until="2026/02/01", from_addr=OWNER
    )
    assert not hasattr(gmail, "BRIEF_EXCLUSION")
