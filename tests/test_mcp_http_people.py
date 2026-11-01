"""People and timeline names stay home over HTTP (RM-46, sec-118b N1 and #2-i).

- N1: the ``person`` argument resolved against EVERY directory entry, including
  people only confidential documents name, and a substring probe ("ab") listed
  their display names as candidates; a unique match returned their email keys.
  In HTTP hide mode, a person with no non-confidential document is unknown,
  candidate lists hold public people only, and ``person_keys`` is dropped from
  every tool result (``brain.mcp_http_tools``).
- #2-i: ``brain_timeline`` matched entities by SUBSTRING and returned up to 50
  names. In hide mode only entities a non-confidential document mentions match.

stdio is unchanged. Every name and address is synthetic (``example.com``).
"""
from __future__ import annotations

from pathlib import Path
from typing import Any

import psycopg
import pytest

from brain import mcp_server
from brain.config import Config
from brain.errors import PersonAmbiguous, PersonNotFound
from brain.graph_rag import retrieve
from brain.mcp_compat import MCPError
from brain.queries import resolve_person_to_keys
from brain.vault.derived_links.directory import DirectoryStore
from tests.conftest import TEST_DATABASE_URL, FakeEmbedder
from tests.entity_filter_seed import seed_entity, seed_mention
from tests.test_build_people import _seed_doc

pytestmark = pytest.mark.usefixtures("test_db")


def _person(
    conn: psycopg.Connection[Any], name: str, email: str, *, sensitivity: str
) -> str:
    DirectoryStore(conn).upsert_pair(display_name=name, email=email, source="people_yml")
    return _seed_doc(
        conn,
        source_kind="krisp",
        external_id=f"krisp-{email}",
        title=f"Call with {name}",
        metadata={"_participant_keys": [email], "date": "2026-04-29"},
        sensitivity=sensitivity,
    )


@pytest.fixture
def people(test_db: psycopg.Connection[Any]) -> dict[str, str]:
    return {
        "alice": _person(test_db, "person-alice-public", "alice@example.com", sensitivity="normal"),
        "alba": _person(test_db, "person-alba-public", "alba@example.com", sensitivity="normal"),
        "alder": _person(
            test_db, "person-alder-secret", "alder@example.com", sensitivity="confidential"
        ),
        "abner": _person(
            test_db, "person-abner-secret", "abner@example.com", sensitivity="confidential"
        ),
        "abby": _person(
            test_db, "person-abby-secret", "abby@example.com", sensitivity="confidential"
        ),
    }


# ----------------------------------------------------------------- the resolver


@pytest.mark.usefixtures("people")
def test_a_person_only_confidential_documents_name_is_unknown_in_public_mode(
    test_db: psycopg.Connection[Any],
) -> None:
    with pytest.raises(PersonNotFound):
        resolve_person_to_keys(test_db, "person-abner-secret", public_only=True)
    with pytest.raises(PersonNotFound):
        resolve_person_to_keys(test_db, "abner@example.com", public_only=True)

    stdio = resolve_person_to_keys(test_db, "person-abner-secret")
    assert "abner@example.com" in stdio.keys


@pytest.mark.usefixtures("people")
def test_a_two_letter_probe_names_no_confidential_only_person(
    test_db: psycopg.Connection[Any],
) -> None:
    with pytest.raises(PersonNotFound):
        resolve_person_to_keys(test_db, "ab", public_only=True)

    with pytest.raises(PersonAmbiguous) as stdio:
        resolve_person_to_keys(test_db, "ab")
    assert len(stdio.value.candidates) == 2


@pytest.mark.usefixtures("people")
def test_an_ambiguous_public_probe_lists_public_people_only(
    test_db: psycopg.Connection[Any],
) -> None:
    with pytest.raises(PersonAmbiguous) as excinfo:
        resolve_person_to_keys(test_db, "person-al", public_only=True)

    names = " ".join(excinfo.value.candidates).lower()
    assert "alice" in names and "alba" in names
    assert "alder" not in names and "alder" not in str(excinfo.value).lower()


@pytest.mark.usefixtures("people")
def test_a_public_person_still_resolves_in_public_mode(test_db: psycopg.Connection[Any]) -> None:
    match = resolve_person_to_keys(test_db, "person-alice-public", public_only=True)

    assert "alice@example.com" in match.keys


# ------------------------------------------------------------ through the tools


def _state(monkeypatch: pytest.MonkeyPatch, tmp_path: Path, *, hide: bool) -> None:
    state = mcp_server._State(
        cfg=Config(database_url=TEST_DATABASE_URL, vault_path=tmp_path),
        embedder=FakeEmbedder(),  # type: ignore[arg-type]
        hide_confidential=hide,
    )
    monkeypatch.setattr(mcp_server, "_state", state)


@pytest.mark.usefixtures("people")
@pytest.mark.parametrize(
    "call",
    [
        lambda person: mcp_server.brain_search(query="call", person=person, fts_only=True),
        lambda person: mcp_server.brain_recall(query="call", person=person),
    ],
    ids=["search", "recall"],
)
def test_http_mode_search_and_recall_treat_a_confidential_only_person_as_unknown(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, call: Any
) -> None:
    _state(monkeypatch, tmp_path, hide=True)

    with pytest.raises(MCPError) as probe:
        call("ab")
    public = call("person-alice-public")

    message = str(probe.value).lower()
    assert "abner" not in message and "abby" not in message
    assert public is not None


@pytest.mark.usefixtures("people")
def test_graph_themes_resolve_people_publicly_when_switched(
    test_db: psycopg.Connection[Any],
) -> None:
    cfg = Config(database_url=TEST_DATABASE_URL)
    with pytest.raises(PersonNotFound):
        retrieve.graph_rag_search(
            test_db, cfg, "themes", backend=object(), mode="themes",  # type: ignore[arg-type]
            person="person-abner-secret", public_entities_only=True,
        )


# --------------------------------------------------------------------- timeline


@pytest.fixture
def timeline_entities(test_db: psycopg.Connection[Any], people: dict[str, str]) -> None:
    public = seed_entity(test_db, name="Zephyrpublic", canonical_key="zephyrpublic")
    secret = seed_entity(test_db, name="Zephyrcode", canonical_key="zephyrcode")
    seed_mention(test_db, public, people["alice"])
    seed_mention(test_db, secret, people["abner"])


@pytest.mark.usefixtures("timeline_entities")
@pytest.mark.parametrize(
    ("hide", "expected"), [(True, ["Zephyrpublic"]), (False, ["Zephyrcode", "Zephyrpublic"])]
)
def test_timeline_names_only_public_entities_in_http_mode(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, hide: bool, expected: list[str]
) -> None:
    _state(monkeypatch, tmp_path, hide=hide)

    payload = mcp_server.brain_timeline(query="zephyr")

    assert sorted(payload["entity_names"]) == expected


@pytest.mark.usefixtures("timeline_entities")
def test_timeline_person_is_resolved_publicly_in_http_mode(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _state(monkeypatch, tmp_path, hide=True)

    with pytest.raises(MCPError) as excinfo:
        mcp_server.brain_timeline(query="zephyr", person="person-abner-secret")

    assert "matched no one" in str(excinfo.value)
