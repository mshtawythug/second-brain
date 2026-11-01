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
- (c) graph entity names extracted only from confidential documents. In HTTP mode
  an entity is named only if at least one non-confidential document mentions it.
  stdio is unchanged.

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
from brain.graph_rag.public_entities import public_entity_ids, withhold_confidential_entities
from brain.graph_rag.relational import list_entities
from brain.graph_rag.schema import (
    CommunityGroup,
    GraphContext,
    GraphEntity,
    GraphExplanation,
    ThemeGroup,
)
from brain.mcp_compat import MCPError
from brain.mcp_tools import related
from tests.conftest import TEST_DATABASE_URL, FakeEmbedder
from tests.entity_filter_seed import seed_entity, seed_mention

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


# ------------------------------------------------------------------ (c) entities


@pytest.fixture
def entities(test_db: psycopg.Connection[Any], corpus: Path) -> dict[str, str]:
    only_secret = seed_entity(test_db, name="Codename Zephyr", canonical_key="codename zephyr")
    mixed = seed_entity(test_db, name="Project Harbor", canonical_key="project harbor")
    only_normal = seed_entity(test_db, name="Project Lantern", canonical_key="project lantern")
    seed_mention(test_db, only_secret, SECRET_ID)
    seed_mention(test_db, mixed, SECRET_ID)
    seed_mention(test_db, mixed, NORMAL_ID)
    seed_mention(test_db, only_normal, NORMAL_ID)
    test_db.execute("UPDATE graph_entities SET doc_count = 2 WHERE id = %s", (mixed,))
    return {"only_secret": only_secret, "mixed": mixed, "only_normal": only_normal}


def test_public_entity_ids_keep_entities_with_a_normal_mention(
    test_db: psycopg.Connection[Any], entities: dict[str, str]
) -> None:
    public = public_entity_ids(test_db, "default", list(entities.values()))

    assert public == {entities["mixed"], entities["only_normal"]}
    assert public_entity_ids(test_db, "default", []) == set()


def test_list_entities_public_only_drops_confidential_only_names(
    test_db: psycopg.Connection[Any], entities: dict[str, str]
) -> None:
    rows = list_entities(test_db, "default", public_only=True)
    public = {row.name: row.doc_count for row in rows}
    everything = {row.name for row in list_entities(test_db, "default")}

    assert public == {"Project Harbor": 2, "Project Lantern": 0}
    assert everything == {"Codename Zephyr", "Project Harbor", "Project Lantern"}


def _entity(entity_id: str, name: str) -> GraphEntity:
    return GraphEntity(id=entity_id, entity_type="project", name=name, canonical_key=name.lower())


def test_graph_context_entities_are_filtered_everywhere(
    test_db: psycopg.Connection[Any], entities: dict[str, str]
) -> None:
    secret = _entity(entities["only_secret"], "Codename Zephyr")
    mixed = _entity(entities["mixed"], "Project Harbor")
    ctx = GraphContext(
        session_id="s",
        mode="local",
        query="q",
        entities=[secret, mixed],
        themes=[ThemeGroup(group_id=1, entities=[secret, mixed])],
        communities=[CommunityGroup(community_key="c", entities=[mixed, secret])],
        explanation=GraphExplanation(mode="local", seed_entity_ids=[secret.id, mixed.id]),
    )

    filtered = withhold_confidential_entities(test_db, ctx)

    assert [e.name for e in filtered.entities] == ["Project Harbor"]
    assert [e.name for e in filtered.themes[0].entities] == ["Project Harbor"]
    assert [e.name for e in filtered.communities[0].entities] == ["Project Harbor"]
    assert filtered.explanation is not None
    assert filtered.explanation.seed_entity_ids == [mixed.id]
    assert [e.name for e in ctx.entities] == ["Codename Zephyr", "Project Harbor"]  # not mutated


def test_a_graph_context_with_nothing_to_withhold_is_unchanged(
    test_db: psycopg.Connection[Any], entities: dict[str, str]
) -> None:
    harbor = _entity(entities["mixed"], "Project Harbor")
    ctx = GraphContext(session_id="s", mode="local", query="q", entities=[harbor])

    assert withhold_confidential_entities(test_db, ctx) == ctx


@pytest.mark.parametrize(("hide", "expected"), [
    (True, {"Project Harbor", "Project Lantern"}),
    (False, {"Codename Zephyr", "Project Harbor", "Project Lantern"}),
])
def test_brain_graphrag_entities_follows_the_mode(
    monkeypatch: pytest.MonkeyPatch,
    corpus: Path,
    entities: dict[str, str],
    hide: bool,
    expected: set[str],
) -> None:
    _state(monkeypatch, corpus, hide=hide)

    payload = mcp_server.brain_graphrag_entities()

    assert {row["name"] for row in payload["entities"]} == expected


def test_graph_retrieval_in_http_mode_names_no_confidential_only_entity(
    monkeypatch: pytest.MonkeyPatch, corpus: Path, entities: dict[str, str]
) -> None:
    import brain.graph_rag as graph_rag_pkg

    secret = _entity(entities["only_secret"], "Codename Zephyr")
    mixed = _entity(entities["mixed"], "Project Harbor")

    def fake_search(*_: Any, **__: Any) -> GraphContext:
        return GraphContext(session_id="s", mode="local", query="q", entities=[secret, mixed])

    monkeypatch.setattr(graph_rag_pkg, "graph_rag_search", fake_search)
    names = {}
    for hide in (True, False):
        state = _state(monkeypatch, corpus, hide=hide)
        ctx = mcp_server._graphrag_search_or_mcp_error(
            state.cfg, "q", mode="local", tenant=None, person=None, depth=None, limit=None,
            synthesize=False, enricher=None,
        )
        names[hide] = [e.name for e in ctx.entities]

    assert names[True] == ["Project Harbor"]
    assert names[False] == ["Codename Zephyr", "Project Harbor"]


def test_mcp_state_defaults_to_stdio_behaviour(corpus: Path) -> None:
    state = mcp_server._State(
        cfg=Config(database_url=TEST_DATABASE_URL),
        embedder=FakeEmbedder(),  # type: ignore[arg-type]
    )

    assert state.hide_confidential is False
    assert dataclasses.replace(state, hide_confidential=True).hide_confidential is True


def test_public_mention_sql_refuses_a_non_identifier_alias() -> None:
    from brain.graph_rag.public_entities import public_mention_sql

    with pytest.raises(ValueError, match="plain identifier"):
        public_mention_sql("e; DROP TABLE documents")
    assert "e.tenant_id" in public_mention_sql("e")


@pytest.mark.parametrize(("hide", "expected"), [
    (True, {"Project Harbor", "Project Lantern"}),
    (False, {"Codename Zephyr", "Project Harbor", "Project Lantern"}),
])
def test_brain_graphrag_stats_top_names_follow_the_mode(
    monkeypatch: pytest.MonkeyPatch,
    corpus: Path,
    entities: dict[str, str],
    hide: bool,
    expected: set[str],
) -> None:
    """rev-118 #10: stats' top-10 are NAMES; only its counts are corpus-wide."""
    _state(monkeypatch, corpus, hide=hide)

    payload = mcp_server.brain_graphrag_stats()

    assert {row["name"] for row in payload["top_entities"]} == expected
    assert payload["total_entities"] == 3  # counts are documented as corpus-wide
