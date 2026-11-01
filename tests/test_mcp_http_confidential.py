"""Confidential METADATA stays home over HTTP unless the server opts in (RM-46, sec-118 #2).

The confidential gate already refuses ``include_confidential`` over HTTP. sec-118
found what still left:

- (a) ``brain_link_proposal`` resolved a confidential destination, so its id and
  title came back. That was the same on stdio, so it is fixed for both: a
  sensitivity predicate unless the caller passes ``include_confidential``.
- (b) ``brain_show`` withholds a confidential body but returns its title, tags
  and source path. In HTTP mode (``McpState.hide_confidential``) a confidential
  document answers exactly as an unknown prefix does, for every tool that looks
  a document up by prefix. stdio is unchanged.
- (c) graph entity names extracted only from confidential documents. Round 5
  withholds the whole graph family in HTTP mode instead
  (``tests/test_mcp_http_graph_withheld.py``).

Every document, entity and title here is synthetic. Runs against the test DB on 5434.
"""
from __future__ import annotations

import dataclasses
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import psycopg
import pytest

from brain import mcp_server
from brain.config import Config
from brain.mcp_compat import MCPError
from brain.mcp_tools import related
from tests.conftest import TEST_DATABASE_URL, FakeEmbedder

pytestmark = pytest.mark.usefixtures("test_db")


def _doc(
    conn: psycopg.Connection[Any],
    *,
    doc_id: str,
    title: str,
    sensitivity: str = "normal",
    kind: str = "vault",
    vault_path: str | None = None,
) -> str:
    conn.execute(
        "INSERT INTO documents (id, kind, title, content, content_type, content_hash,"
        " vault_path, sensitivity) VALUES (%s, %s, %s, %s, 'note', %s, %s, %s)",
        (doc_id, kind, title, f"{title} body.", f"hash-{doc_id}", vault_path, sensitivity),
    )
    return doc_id


NORMAL_ID = "aaaaaaaa-1111-1111-1111-111111111111"
SECRET_ID = "bbbbbbbb-2222-2222-2222-222222222222"
SRC_ID = "cccccccc-3333-3333-3333-333333333333"


@pytest.fixture
def corpus(test_db: psycopg.Connection[Any], tmp_path: Path) -> Path:
    vault = tmp_path / "vault"
    vault.mkdir()
    _doc(test_db, doc_id=NORMAL_ID, title="Example Plan Alpha")
    _doc(test_db, doc_id=SECRET_ID, title="Example Reorg Bravo", sensitivity="confidential")
    _doc(test_db, doc_id=SRC_ID, title="Example Source Note", vault_path="source.md")
    (vault / "source.md").write_text(
        f"---\nid: {SRC_ID}\ntitle: \"Example Source Note\"\nkind: vault\n---\n\nBody.\n",
        encoding="utf-8",
    )
    return vault


def _state(
    monkeypatch: pytest.MonkeyPatch, vault: Path, *, hide: bool
) -> mcp_server._State:
    state = mcp_server._State(
        cfg=Config(database_url=TEST_DATABASE_URL, vault_path=vault),
        embedder=FakeEmbedder(),  # type: ignore[arg-type]
        hide_confidential=hide,
    )
    monkeypatch.setattr(mcp_server, "_state", state)
    return state


@pytest.fixture
def http_mode(monkeypatch: pytest.MonkeyPatch, corpus: Path) -> Iterator[None]:
    _state(monkeypatch, corpus, hide=True)
    yield


@pytest.fixture
def stdio_mode(monkeypatch: pytest.MonkeyPatch, corpus: Path) -> Iterator[None]:
    _state(monkeypatch, corpus, hide=False)
    yield


def _message(call: Any) -> str:
    with pytest.raises(MCPError) as excinfo:
        call()
    return str(excinfo.value.error.message)


# ------------------------------------------------------------------ (a) link proposal


@pytest.mark.usefixtures("stdio_mode")
@pytest.mark.parametrize("dst", ["Example Reorg Bravo", SECRET_ID[:8]])
def test_link_proposal_does_not_resolve_a_confidential_destination(dst: str) -> None:
    message = _message(
        lambda: mcp_server.brain_link_proposal(src_id_prefix=SRC_ID[:8], dst_id_or_title=dst)
    )

    assert message.startswith("no document found matching")
    assert SECRET_ID not in message


@pytest.mark.usefixtures("stdio_mode")
@pytest.mark.parametrize("dst", ["Example Plan Alpha", NORMAL_ID[:8]])
def test_link_proposal_still_resolves_a_normal_destination(dst: str) -> None:
    payload = mcp_server.brain_link_proposal(src_id_prefix=SRC_ID[:8], dst_id_or_title=dst)

    assert payload["dst_document_id"] == NORMAL_ID


@pytest.mark.usefixtures("stdio_mode")
def test_link_proposal_resolves_a_confidential_destination_when_asked() -> None:
    payload = mcp_server.brain_link_proposal(
        src_id_prefix=SRC_ID[:8], dst_id_or_title="Example Reorg Bravo", include_confidential=True
    )

    assert payload["dst_document_id"] == SECRET_ID


# --------------------------------------------------------- (b) lookups by id prefix


@pytest.mark.usefixtures("http_mode")
@pytest.mark.parametrize(
    "call",
    [
        lambda prefix: mcp_server.brain_show(id_prefix=prefix),
        lambda prefix: mcp_server.brain_backlinks(id_prefix=prefix),
        lambda prefix: mcp_server.brain_links(id_prefix=prefix),
        lambda prefix: related.brain_related(id=prefix),
    ],
    ids=["show", "backlinks", "links", "related"],
)
def test_http_mode_answers_a_confidential_document_as_unknown(call: Any) -> None:
    hidden = _message(lambda: call(SECRET_ID[:8]))
    unknown = _message(lambda: call("dddddddd"))

    assert hidden == f"document not found: {SECRET_ID[:8]}"
    assert unknown == "document not found: dddddddd"


@pytest.mark.usefixtures("http_mode")
def test_http_mode_still_shows_a_normal_document() -> None:
    payload = mcp_server.brain_show(id_prefix=NORMAL_ID[:8])

    assert payload["title"] == "Example Plan Alpha"
    assert payload["content"] == "Example Plan Alpha body."


@pytest.mark.usefixtures("stdio_mode")
def test_stdio_still_names_a_confidential_document_with_its_body_withheld() -> None:
    payload = mcp_server.brain_show(id_prefix=SECRET_ID[:8])

    assert payload["title"] == "Example Reorg Bravo"
    assert payload["content"] is None
    assert payload["sensitivity"] == "confidential"


def test_mcp_state_defaults_to_stdio_behaviour(corpus: Path) -> None:
    state = mcp_server._State(
        cfg=Config(database_url=TEST_DATABASE_URL),
        embedder=FakeEmbedder(),  # type: ignore[arg-type]
    )

    assert state.hide_confidential is False
    assert dataclasses.replace(state, hide_confidential=True).hide_confidential is True
