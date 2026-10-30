"""Every Gmail query ``ingest/gmail.py`` builds excludes the delivered brief (RM-16, AC6).

``brain brief --wiki`` can email the owner a ``[brain brief] YYYY-MM-DD`` message; without the
exclusion, ``brain ingest-gmail --since …`` would ingest the brain's own digest back into it.
The runner is a fake; nothing reaches ``gws``.
"""
from __future__ import annotations

import itertools
import json

import pytest

from brain.brief_delivery import SUBJECT_PREFIX
from brain.ingest.gmail import BRIEF_EXCLUSION, _build_query, list_messages

_SCOPES = {
    "query": "project zephyr",
    "label": "interviews",
    "since": "2026-01-01",
    "until": "2026-02-01",
    "from_addr": "sender@example.invalid",
}


def test_the_exclusion_names_the_delivered_subject() -> None:
    expected = f'-subject:"{SUBJECT_PREFIX}"'
    assert expected == BRIEF_EXCLUSION


@pytest.mark.parametrize(
    "present",
    [combo for n in range(len(_SCOPES) + 1) for combo in itertools.combinations(_SCOPES, n)],
)
def test_every_gmail_query_excludes_the_brief_subject(present: tuple[str, ...]) -> None:
    kwargs = {name: (_SCOPES[name] if name in present else None) for name in _SCOPES}

    q = _build_query(**kwargs)

    assert q.endswith(BRIEF_EXCLUSION)
    assert q.count(BRIEF_EXCLUSION) == 1


def test_list_messages_always_sends_the_exclusion() -> None:
    calls: list[list[str]] = []

    def runner(argv: list[str]) -> str:
        calls.append(argv)
        return "{}"

    list_messages(runner=runner)

    params = json.loads(calls[0][calls[0].index("--params") + 1])
    assert params["q"] == BRIEF_EXCLUSION
