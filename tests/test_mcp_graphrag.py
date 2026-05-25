"""MCP-parity tests for the GraphRAG tools (wave G2-i).

Exercises the four ``brain_graphrag_*`` MCP tools against the live Apache AGE
test instance (port 5434) over a tiny synthetic person graph built via
``build_graph`` + the real ``AgeBackend`` (each tool opens its own
``connect_age`` connection, so the autocommit ``test_db`` seed + AGE build are
visible). Asserts:

* ``brain_graphrag_search`` mode dispatch — explicit ``local``, ``auto`` →
  local, ``auto`` thematic-no-person → ``global`` (the G3-e flip; degradation
  signals dormant), ``themes`` mode, and explicit ``global`` EXECUTING the
  community path (no longer ``INVALID_PARAMS``);
* ``brain_graphrag_themes`` — required ``person`` (``INVALID_PARAMS`` when
  blank), the headline {bob, carol} theme, and the opt-in fake-enricher
  ``synthesize`` (no live Ollama);
* ``brain_graphrag_entity`` — the neighbourhood wrapper + required ``name``;
* ``brain_graphrag_build`` — ``backfill``, ``concepts`` (fake extractor, no
  Ollama), ``force``+``limit`` rejection, and the no-flag rejection;
* the returned JSON shape == :func:`brain.format.graph_context_json` + a
  ``session_id``;
* tenant scoping (no cross-tenant leak);
* ``PersonNotFound`` / ``PersonAmbiguous`` → ``INVALID_PARAMS``;
* NO ``"cypher"`` substring in any wire payload;
* PARITY: the MCP search payload equals the CLI ``--json`` payload (minus the
  per-call ``session_id``) for the same inputs — proving both call one core.

All people are synthetic (alice / bob / carol / dana lee / dana park); no PII.
The schema + AGE graph reset per test via the ``test_db`` fixture.
"""
from __future__ import annotations

import hashlib
import json as _json
import uuid
from collections.abc import Sequence
from typing import Any

import psycopg
import pytest
from mcp import McpError
from mcp.types import INTERNAL_ERROR, INVALID_PARAMS
from typer.testing import CliRunner

from brain import mcp_server
from brain.cli import app
from brain.config import Config
from brain.graph_rag.backends import AgeBackend
from brain.graph_rag.build import build_graph
from brain.graph_rag.reconcile import ReconcileConfig
from brain.queries import iter_all_document_ids
from brain.vault.derived_links.directory import DirectoryStore
from tests.conftest import FakeEmbedder

TEST_DATABASE_URL = "postgresql://brain:brain@localhost:5434/second_brain_test"

# ``graph_communities.summary_embedding`` ships as vector(1024) (migration 013).
# The community MCP tests install a 1024-dim FakeEmbedder so summarize writes at
# the migration dim (no resize) and the global query embeds at the same dim.
_SUMMARY_DIM = 1024

# Suppression-disabled ratio (cap = round(N * 1.0) = N) so the tiny corpora's
# edges/entities always materialize. Mirrors ``_NO_SUPPRESS`` in
# test_cli_graphrag_search / test_graphrag_build.
_NO_SUPPRESS = 1.0


# --------------------------------------------------------------------------- #
# Seeding helpers (mirror test_cli_graphrag_search / test_graphrag_retrieve)
# --------------------------------------------------------------------------- #
def _seed_directory(
    conn: psycopg.Connection[Any], pairs: Sequence[tuple[str, str]]
) -> None:
    store = DirectoryStore(conn)
    for name, email in pairs:
        store.upsert_pair(display_name=name, email=email, source="gmail")


def _seed_gmail_doc(
    conn: psycopg.Connection[Any],
    *,
    external_id: str,
    participants: Sequence[tuple[str, str]],
    content: str = "synthetic body",
) -> str:
    src_row = conn.execute(
        "INSERT INTO sources (kind, external_id, metadata) "
        "VALUES ('gmail', %s, '{}'::jsonb) RETURNING id",
        (external_id,),
    ).fetchone()
    assert src_row is not None
    from_hdr = f"{participants[0][0]} <{participants[0][1]}>"
    to_hdr = ", ".join(f"{n} <{e}>" for n, e in participants[1:])
    metadata = {"from": from_hdr, "to": to_hdr, "thread_id": external_id}
    salted = f"{content}\n<!-- {uuid.uuid4()} -->"
    content_hash = hashlib.sha256(salted.encode("utf-8")).hexdigest()
    doc_row = conn.execute(
        "INSERT INTO documents "
        "(source_id, title, content, content_hash, content_type, metadata) "
        "VALUES (%s, %s, %s, %s, 'email', %s::jsonb) RETURNING id::text",
        (src_row[0], external_id, salted, content_hash, _json.dumps(metadata)),
    ).fetchone()
    assert doc_row is not None
    return str(doc_row[0])


def _cfg(tenant: str = "default") -> ReconcileConfig:
    return ReconcileConfig(tenant_id=tenant, generic_df_ratio=_NO_SUPPRESS)


def _build(test_db: psycopg.Connection[Any], tenant: str = "default") -> AgeBackend:
    """Build the AGE graph from every seeded document into ``tenant``."""
    backend = AgeBackend()
    backend.bootstrap(test_db)
    all_ids = [doc_id for batch in iter_all_document_ids(test_db) for doc_id in batch]
    build_graph(test_db, all_ids, backend=backend, config=_cfg(tenant))
    return backend


def _seed_triangle(test_db: psycopg.Connection[Any]) -> None:
    """alice-bob, alice-carol, bob-carol → complete person triangle."""
    _seed_directory(
        test_db,
        [("alice", "alice@x.com"), ("bob", "bob@x.com"), ("carol", "carol@x.com")],
    )
    _seed_gmail_doc(
        test_db, external_id="m1",
        participants=[("alice", "alice@x.com"), ("bob", "bob@x.com")],
    )
    _seed_gmail_doc(
        test_db, external_id="m2",
        participants=[("alice", "alice@x.com"), ("carol", "carol@x.com")],
    )
    _seed_gmail_doc(
        test_db, external_id="m3",
        participants=[("bob", "bob@x.com"), ("carol", "carol@x.com")],
    )


def _seed_dana_cluster(test_db: psycopg.Connection[Any]) -> None:
    """Dana + bob + carol all in one doc → bob/carol co-occur within Dana's scope."""
    _seed_directory(
        test_db,
        [
            ("dana lee", "dana@x.com"),
            ("bob", "bob@x.com"),
            ("carol", "carol@x.com"),
        ],
    )
    _seed_gmail_doc(
        test_db,
        external_id="d1",
        participants=[
            ("dana lee", "dana@x.com"),
            ("bob", "bob@x.com"),
            ("carol", "carol@x.com"),
        ],
    )


# --------------------------------------------------------------------------- #
# Test doubles + state install
# --------------------------------------------------------------------------- #
class _FakeEnricher:
    """Returns a canned theme summary; records calls. No Ollama."""

    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    def summarize_group(
        self,
        *,
        person: str | None,
        entity_names: list[str],
        doc_titles: list[str],
    ) -> str | None:
        self.calls.append(
            {"person": person, "entity_names": list(entity_names),
             "doc_titles": list(doc_titles)}
        )
        return "SYNTHETIC THEME SUMMARY"


class _FakeExtractor:
    """Concept extractor that returns nothing — exercises the concepts wiring
    (config.concepts_enabled + extractor injected) with no live Ollama."""

    def __init__(self) -> None:
        self.calls = 0

    @property
    def version(self) -> str:
        return "fake-extractor@1"

    def extract(self, text: str) -> list[Any]:  # noqa: ARG002 — protocol shape
        self.calls += 1
        return []


def _make_state(
    fake_embedder: object, *, enricher: object | None = None
) -> mcp_server._State:
    """Build a server state pointing at the test DB + a no-suppression cfg.

    ``graph_generic_df_ratio=1.0`` disables generic suppression so the tiny
    corpora's themes always materialize (the CLI test sets the equivalent
    ``BRAIN_GRAPH_GENERIC_DF=1.0`` env)."""
    return mcp_server._State(
        cfg=Config(database_url=TEST_DATABASE_URL, graph_generic_df_ratio=1.0),
        embedder=fake_embedder,  # type: ignore[arg-type]
        enricher=enricher,  # type: ignore[arg-type]
    )


@pytest.fixture
def graph_state(
    monkeypatch: pytest.MonkeyPatch,
    test_db: psycopg.Connection,  # noqa: ARG001 — fixture keeps schema + graph fresh
    fake_embedder: object,
) -> mcp_server._State:
    """Install a default graphrag MCP state (no enricher)."""
    state = _make_state(fake_embedder)
    monkeypatch.setattr(mcp_server, "_state", state)
    return state


_GRAPH_CONTEXT_KEYS = (
    "session_id", "mode", "query", "tenant_id", "person", "requested_mode",
    "degraded_from", "degradation_reason", "themes", "communities", "entities",
    "docs", "explanation",
)


# --------------------------------------------------------------------------- #
# Community seeding + state install (wave G3-f) — direct relational inserts
# (mirror test_cli_graphrag_search / test_graphrag_global). Two dense triangles
# + a weak bridge → two communities (min_size=3).
# --------------------------------------------------------------------------- #
class _FakeCommunityEnricher:
    """Fake community summarizer: distinct per-community summary, no Ollama."""

    model = "fake-model:1b"

    def __init__(self) -> None:
        self.calls = 0

    def summarize_group(
        self,
        *,
        person: str | None,
        entity_names: list[str],
        doc_titles: list[str],
    ) -> str | None:
        self.calls += 1
        return "Community covering " + ", ".join(entity_names)


def _community_state(
    monkeypatch: pytest.MonkeyPatch, enricher: object | None
) -> mcp_server._State:
    """Install a graphrag state with a 1024-dim embedder + the given enricher."""
    state = mcp_server._State(
        cfg=Config(database_url=TEST_DATABASE_URL, graph_generic_df_ratio=1.0),
        embedder=FakeEmbedder(dim=_SUMMARY_DIM),  # type: ignore[arg-type]
        enricher=enricher,  # type: ignore[arg-type]
    )
    monkeypatch.setattr(mcp_server, "_state", state)
    return state


def _insert_entity(
    conn: psycopg.Connection[Any], tenant: str, name: str, canonical_key: str
) -> str:
    row = conn.execute(
        "INSERT INTO graph_entities (tenant_id, entity_type, name, canonical_key) "
        "VALUES (%s, 'person', %s, %s) RETURNING id::text",
        (tenant, name, canonical_key),
    ).fetchone()
    assert row is not None
    return str(row[0])


def _insert_document(conn: psycopg.Connection[Any], title: str) -> str:
    row = conn.execute(
        "INSERT INTO documents (title, content, content_hash, content_type) "
        "VALUES (%s, %s, %s, 'note') RETURNING id::text",
        (title, f"{title} discussion body.", uuid.uuid4().hex),
    ).fetchone()
    assert row is not None
    return str(row[0])


def _add_mention(
    conn: psycopg.Connection[Any], tenant: str, entity_id: str, document_id: str
) -> None:
    conn.execute(
        "INSERT INTO graph_entity_mentions "
        "(tenant_id, entity_id, document_id, source) VALUES (%s, %s, %s, 'people')",
        (tenant, entity_id, document_id),
    )


def _add_chunk(conn: psycopg.Connection[Any], document_id: str, content: str) -> None:
    vec = FakeEmbedder().embed([content], input_type="document")[0]
    conn.execute(
        "INSERT INTO chunks (document_id, chunk_index, content, embedding) "
        "VALUES (%s, 0, %s, %s)",
        (document_id, content, vec),
    )


def _seed_communities_corpus(
    test_db: psycopg.Connection[Any], tenant: str = "default"
) -> None:
    """Two triangles + weak bridge + docs/mentions/chunks → two communities."""
    def _rel(a: str, b: str, weight: float) -> None:
        src, dst = sorted((a, b))
        test_db.execute(
            "INSERT INTO graph_relationships "
            "(tenant_id, src_id, dst_id, rel_type, weight, co_count, doc_count) "
            "VALUES (%s, %s, %s, 'co_occurs', %s, 1, 1)",
            (tenant, src, dst, weight),
        )

    c1 = [_insert_entity(test_db, tenant, f"P-{i}", f"p-{tenant}-{i}") for i in range(3)]
    c2 = [_insert_entity(test_db, tenant, f"Q-{i}", f"q-{tenant}-{i}") for i in range(3)]
    for a, b in [(0, 1), (0, 2), (1, 2)]:
        _rel(c1[a], c1[b], 0.8)
        _rel(c2[a], c2[b], 0.8)
    _rel(c1[2], c2[0], 0.05)  # weak bridge
    d1 = _insert_document(test_db, "Cluster One Doc")
    d2 = _insert_document(test_db, "Cluster Two Doc")
    for entity in c1:
        _add_mention(test_db, tenant, entity, d1)
    for entity in c2:
        _add_mention(test_db, tenant, entity, d2)
    _add_chunk(test_db, d1, "Cluster one discussion body.")
    _add_chunk(test_db, d2, "Cluster two discussion body.")


# --------------------------------------------------------------------------- #
# 1. brain_graphrag_search — local + auto
# --------------------------------------------------------------------------- #
def test_search_explicit_local(
    test_db: psycopg.Connection, graph_state: mcp_server._State  # noqa: ARG001
) -> None:
    _seed_triangle(test_db)
    _build(test_db)
    payload = mcp_server.brain_graphrag_search(query="bob", mode="local")
    assert payload["mode"] == "local"
    keys = {e["canonical_key"] for e in payload["entities"]}
    assert {"bob", "alice", "carol"} <= keys


def test_search_auto_routes_local(
    test_db: psycopg.Connection, graph_state: mcp_server._State  # noqa: ARG001
) -> None:
    """A non-thematic query under the default mode='auto' dispatches to local."""
    _seed_triangle(test_db)
    _build(test_db)
    payload = mcp_server.brain_graphrag_search(query="bob")
    assert payload["mode"] == "local"
    assert payload["requested_mode"] is None
    assert payload["degraded_from"] is None


def test_search_auto_routes_global(
    test_db: psycopg.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Thematic query + no resolvable person now ROUTES to global (G3-e flip).

    No longer the G2 global→local degradation: ``mode='global'`` executes the
    community path directly, with all degradation signals dormant (``None``).
    """
    _community_state(monkeypatch, None)
    _seed_triangle(test_db)
    _build(test_db)
    payload = mcp_server.brain_graphrag_search(query="recurring themes")
    assert payload["mode"] == "global"
    assert payload["requested_mode"] is None
    assert payload["degraded_from"] is None
    assert payload["degradation_reason"] is None
    # No communities built → empty-but-valid global context.
    assert payload["communities"] == []


def test_search_global_executes(
    test_db: psycopg.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An explicit mode='global' now EXECUTES (no longer INVALID_PARAMS)."""
    _community_state(monkeypatch, _FakeCommunityEnricher())
    _seed_communities_corpus(test_db)
    mcp_server.brain_graphrag_communities_build()

    payload = mcp_server.brain_graphrag_search(query="Cluster", mode="global")
    assert payload["mode"] == "global"
    # Communities built + summarized + embedded → the vector leg ranks them.
    assert payload["communities"], "expected at least one ranked community"
    first = payload["communities"][0]
    for key in ("community_key", "level", "member_count", "score", "summary"):
        assert key in first, key
    assert "cypher" not in _json.dumps(payload).lower()


def test_search_fuse_executes(
    test_db: psycopg.Connection, graph_state: mcp_server._State  # noqa: ARG001
) -> None:
    """An explicit mode='fuse' RRF-merges the graph + hybrid doc legs (G4-c).

    Parity with the CLI ``--mode fuse`` test: the fused docs land in the standard
    ``docs`` array and per-doc leg provenance rides in
    ``explanation.matched_filters.fuse_doc_provenance``.
    """
    _seed_triangle(test_db)
    _build(test_db)
    # A chunk so the hybrid (FTS+vector) leg returns a doc overlapping the graph
    # leg → a real two-leg fusion.
    row = test_db.execute(
        "SELECT id::text FROM documents WHERE title = 'm1'"
    ).fetchone()
    assert row is not None
    _add_chunk(test_db, row[0], "bob roadmap planning")

    payload = mcp_server.brain_graphrag_search(query="bob", mode="fuse")
    assert payload["mode"] == "fuse"
    assert payload["docs"], "expected fused docs"
    prov = payload["explanation"]["matched_filters"]["fuse_doc_provenance"]
    returned_ids = {d["id"] for d in payload["docs"]}
    assert set(prov) == returned_ids
    assert any(
        e["graph_rank"] is not None and e["hybrid_rank"] is not None
        for e in prov.values()
    )
    assert payload["explanation"]["matched_filters"]["hybrid_vector_arm_used"] is True
    assert "cypher" not in _json.dumps(payload).lower()


def test_search_unknown_mode_invalid_params(
    test_db: psycopg.Connection, graph_state: mcp_server._State  # noqa: ARG001
) -> None:
    """An unrecognized mode surfaces as INVALID_PARAMS (router ValueError)."""
    _seed_triangle(test_db)
    _build(test_db)
    with pytest.raises(McpError) as exc_info:
        mcp_server.brain_graphrag_search(query="bob", mode="sideways")
    assert exc_info.value.error.code == INVALID_PARAMS
    assert "unknown graph retrieval mode" in exc_info.value.error.message.lower()


def test_search_fuse_non_default_tenant_invalid_params(
    test_db: psycopg.Connection, graph_state: mcp_server._State  # noqa: ARG001
) -> None:
    """mode='fuse' with a non-default tenant → P1-1 gate ValueError → INVALID_PARAMS.

    Regression for G4-review P1-1: fuse's hybrid leg is corpus-wide
    (documents/chunks are not tenantized), so a non-default fuse would leak
    cross-tenant documents. The gate's ValueError maps to INVALID_PARAMS.
    """
    _build(test_db)  # bootstrap AGE so the gate (not AGE-absent) is what fires
    with pytest.raises(McpError) as exc_info:
        mcp_server.brain_graphrag_search(query="bob", mode="fuse", tenant="custom")
    assert exc_info.value.error.code == INVALID_PARAMS
    assert "only available for tenant 'default'" in exc_info.value.error.message.lower()


# --------------------------------------------------------------------------- #
# 2. brain_graphrag_search — themes mode + JSON shape
# --------------------------------------------------------------------------- #
def test_search_themes_mode(
    test_db: psycopg.Connection, graph_state: mcp_server._State  # noqa: ARG001
) -> None:
    _seed_dana_cluster(test_db)
    _build(test_db)
    payload = mcp_server.brain_graphrag_search(
        query="ignored", mode="themes", person="dana lee"
    )
    assert payload["mode"] == "themes"
    assert payload["person"] == "Dana Lee"
    keysets = {
        frozenset(e["canonical_key"] for e in t["entities"])
        for t in payload["themes"]
    }
    assert frozenset({"bob", "carol"}) in keysets
    # Dana (the seed) never appears as a theme entity.
    assert all(
        "dana lee" not in {e["canonical_key"] for e in t["entities"]}
        for t in payload["themes"]
    )


def test_search_themes_missing_person_invalid_params(
    test_db: psycopg.Connection, graph_state: mcp_server._State  # noqa: ARG001
) -> None:
    """mode='themes' with no person → router ValueError → INVALID_PARAMS."""
    _seed_triangle(test_db)
    _build(test_db)
    with pytest.raises(McpError) as exc_info:
        mcp_server.brain_graphrag_search(query="bob", mode="themes")
    assert exc_info.value.error.code == INVALID_PARAMS


def test_search_json_shape_has_all_context_keys(
    test_db: psycopg.Connection, graph_state: mcp_server._State  # noqa: ARG001
) -> None:
    """The payload carries the full graph_context_json shape + a session_id."""
    _seed_triangle(test_db)
    _build(test_db)
    payload = mcp_server.brain_graphrag_search(query="bob", mode="local")
    for key in _GRAPH_CONTEXT_KEYS:
        assert key in payload, key
    assert isinstance(payload["session_id"], str) and payload["session_id"]
    sample = payload["entities"][0]
    for field in ("id", "entity_type", "name", "canonical_key", "doc_count"):
        assert field in sample, field
    assert payload["explanation"]["mode"] == "local"


def test_search_session_id_is_fresh_per_call(
    test_db: psycopg.Connection, graph_state: mcp_server._State  # noqa: ARG001
) -> None:
    """Each call mints a fresh session_id (like brain_search)."""
    _seed_triangle(test_db)
    _build(test_db)
    a = mcp_server.brain_graphrag_search(query="bob", mode="local")
    b = mcp_server.brain_graphrag_search(query="bob", mode="local")
    assert a["session_id"] != b["session_id"]


# --------------------------------------------------------------------------- #
# 3. brain_graphrag_themes
# --------------------------------------------------------------------------- #
def test_themes_tool(
    test_db: psycopg.Connection, graph_state: mcp_server._State  # noqa: ARG001
) -> None:
    _seed_dana_cluster(test_db)
    _build(test_db)
    payload = mcp_server.brain_graphrag_themes(person="dana lee")
    assert payload["mode"] == "themes"
    assert payload["person"] == "Dana Lee"
    keysets = {
        frozenset(e["canonical_key"] for e in t["entities"])
        for t in payload["themes"]
    }
    assert frozenset({"bob", "carol"}) in keysets


@pytest.mark.parametrize("person", ["", "   "])
def test_themes_tool_requires_person(
    graph_state: mcp_server._State, person: str  # noqa: ARG001
) -> None:
    """Blank / whitespace-only person → INVALID_PARAMS (before any DB work)."""
    with pytest.raises(McpError) as exc_info:
        mcp_server.brain_graphrag_themes(person=person)
    assert exc_info.value.error.code == INVALID_PARAMS
    assert "person is required" in exc_info.value.error.message


def test_themes_synthesize_uses_injected_enricher(
    monkeypatch: pytest.MonkeyPatch,
    test_db: psycopg.Connection,
    fake_embedder: object,
) -> None:
    """themes synthesize=True attaches the (faked) enricher's group summary."""
    _seed_dana_cluster(test_db)
    _build(test_db)
    fake = _FakeEnricher()
    monkeypatch.setattr(mcp_server, "_state", _make_state(fake_embedder, enricher=fake))

    payload = mcp_server.brain_graphrag_themes(person="dana lee", synthesize=True)
    assert payload["themes"], "expected at least one theme group"
    assert all(t["summary"] == "SYNTHETIC THEME SUMMARY" for t in payload["themes"])
    assert fake.calls  # the enricher was actually invoked


def test_themes_synthesize_no_enricher_degrades(
    test_db: psycopg.Connection, graph_state: mcp_server._State  # noqa: ARG001
) -> None:
    """synthesize=True with no enricher in state degrades to summary=None."""
    _seed_dana_cluster(test_db)
    _build(test_db)
    # ``graph_state`` has enricher=None; synthesize must never raise.
    payload = mcp_server.brain_graphrag_themes(person="dana lee", synthesize=True)
    assert payload["themes"]
    assert all(t["summary"] is None for t in payload["themes"])


# --------------------------------------------------------------------------- #
# 4. brain_graphrag_entity
# --------------------------------------------------------------------------- #
def test_entity_tool(
    test_db: psycopg.Connection, graph_state: mcp_server._State  # noqa: ARG001
) -> None:
    _seed_triangle(test_db)
    _build(test_db)
    payload = mcp_server.brain_graphrag_entity(name="bob")
    assert payload["mode"] == "local"
    assert payload["query"] == "bob"
    keys = {e["canonical_key"] for e in payload["entities"]}
    assert {"bob", "alice", "carol"} <= keys
    assert payload["explanation"]["depth"] >= 1


@pytest.mark.parametrize("name", ["", "   "])
def test_entity_tool_requires_name(
    graph_state: mcp_server._State, name: str  # noqa: ARG001
) -> None:
    with pytest.raises(McpError) as exc_info:
        mcp_server.brain_graphrag_entity(name=name)
    assert exc_info.value.error.code == INVALID_PARAMS
    assert "name is required" in exc_info.value.error.message


# --------------------------------------------------------------------------- #
# 5. brain_graphrag_build
# --------------------------------------------------------------------------- #
def test_build_backfill(
    test_db: psycopg.Connection, graph_state: mcp_server._State  # noqa: ARG001
) -> None:
    """build(backfill=True) reconciles every seeded doc into the graph."""
    _seed_triangle(test_db)  # 3 docs, graph NOT pre-built
    result = mcp_server.brain_graphrag_build(backfill=True)
    assert result["processed"] == 3
    assert result["reconciled"] == 3
    assert result["skipped"] == 0
    assert result["tenant_id"] == "default"
    assert result["concepts"] is False
    # The graph is now queryable via the retrieval tools.
    payload = mcp_server.brain_graphrag_search(query="bob", mode="local")
    assert {"bob", "alice", "carol"} <= {
        e["canonical_key"] for e in payload["entities"]
    }


def test_build_idempotent_second_run_skips(
    test_db: psycopg.Connection, graph_state: mcp_server._State  # noqa: ARG001
) -> None:
    """A second build skips already-indexed docs via the watermark."""
    _seed_triangle(test_db)
    mcp_server.brain_graphrag_build(backfill=True)
    again = mcp_server.brain_graphrag_build(backfill=True)
    assert again["processed"] == 3
    assert again["skipped"] == 3
    assert again["reconciled"] == 0


def test_build_concepts_wires_extractor(
    monkeypatch: pytest.MonkeyPatch,
    test_db: psycopg.Connection,
    graph_state: mcp_server._State,  # noqa: ARG001
) -> None:
    """build(concepts=True) injects the extractor (faked — no live Ollama)."""
    _seed_triangle(test_db)
    fake = _FakeExtractor()
    monkeypatch.setattr(
        "brain.graph_rag.extract.make_extractor", lambda cfg: fake  # noqa: ARG005
    )
    result = mcp_server.brain_graphrag_build(backfill=True, concepts=True)
    assert result["concepts"] is True
    assert result["processed"] == 3
    assert fake.calls == 3  # extractor invoked once per document


def test_build_force_with_limit_rejected(
    graph_state: mcp_server._State,  # noqa: ARG001
) -> None:
    with pytest.raises(McpError) as exc_info:
        mcp_server.brain_graphrag_build(force=True, limit=2)
    assert exc_info.value.error.code == INVALID_PARAMS
    assert "cannot be combined with limit" in exc_info.value.error.message


def test_build_no_flags_rejected(
    graph_state: mcp_server._State,  # noqa: ARG001
) -> None:
    """Calling with neither backfill nor force is rejected (MCP twin of the
    CLI's 'pass --backfill' hint)."""
    with pytest.raises(McpError) as exc_info:
        mcp_server.brain_graphrag_build()
    assert exc_info.value.error.code == INVALID_PARAMS
    assert "backfill" in exc_info.value.error.message


def test_build_limit_caps_documents(
    test_db: psycopg.Connection, graph_state: mcp_server._State  # noqa: ARG001
) -> None:
    _seed_triangle(test_db)  # 3 docs
    result = mcp_server.brain_graphrag_build(backfill=True, limit=2)
    assert result["processed"] == 2


# --------------------------------------------------------------------------- #
# 6. tenant scoping (no cross-tenant leak)
# --------------------------------------------------------------------------- #
def test_search_tenant_scoping(
    test_db: psycopg.Connection, graph_state: mcp_server._State  # noqa: ARG001
) -> None:
    """Building into a custom tenant; querying default returns no entities."""
    _seed_triangle(test_db)
    _build(test_db, tenant="custom")

    custom = mcp_server.brain_graphrag_search(
        query="bob", mode="local", tenant="custom"
    )
    assert custom["tenant_id"] == "custom"
    assert {e["canonical_key"] for e in custom["entities"]} >= {"bob"}

    default = mcp_server.brain_graphrag_search(query="bob", mode="local")
    assert default["tenant_id"] == "default"
    assert default["entities"] == []


# --------------------------------------------------------------------------- #
# 7. Person error mapping → INVALID_PARAMS
# --------------------------------------------------------------------------- #
def test_themes_person_not_found_invalid_params(
    test_db: psycopg.Connection, graph_state: mcp_server._State  # noqa: ARG001
) -> None:
    _seed_triangle(test_db)
    _build(test_db)
    with pytest.raises(McpError) as exc_info:
        mcp_server.brain_graphrag_themes(person="nobody-unknown-xyz")
    assert exc_info.value.error.code == INVALID_PARAMS
    assert "no one" in exc_info.value.error.message.lower()


def test_themes_person_ambiguous_invalid_params(
    test_db: psycopg.Connection, graph_state: mcp_server._State  # noqa: ARG001
) -> None:
    _seed_directory(
        test_db,
        [("dana lee", "dana.lee@x.com"), ("dana park", "dana.park@x.com")],
    )
    with pytest.raises(McpError) as exc_info:
        mcp_server.brain_graphrag_themes(person="dana")
    assert exc_info.value.error.code == INVALID_PARAMS
    assert "more specific" in exc_info.value.error.message.lower()


# --------------------------------------------------------------------------- #
# 8. AGE absent → INTERNAL_ERROR
# --------------------------------------------------------------------------- #
def test_search_age_absent_internal_error(
    monkeypatch: pytest.MonkeyPatch,
    test_db: psycopg.Connection,
    graph_state: mcp_server._State,  # noqa: ARG001
) -> None:
    """AGE-absent image → INTERNAL_ERROR (unrecoverable subsystem)."""
    _seed_triangle(test_db)
    _build(test_db)
    monkeypatch.setattr(mcp_server, "age_extension_available", lambda conn: False)
    with pytest.raises(McpError) as exc_info:
        mcp_server.brain_graphrag_search(query="bob", mode="local")
    assert exc_info.value.error.code == INTERNAL_ERROR
    assert "Apache AGE is not available" in exc_info.value.error.message


# --------------------------------------------------------------------------- #
# 9. No raw Cypher in any wire payload
# --------------------------------------------------------------------------- #
def test_no_cypher_in_payloads(
    test_db: psycopg.Connection, graph_state: mcp_server._State  # noqa: ARG001
) -> None:
    _seed_dana_cluster(test_db)
    _build(test_db)
    search = mcp_server.brain_graphrag_search(query="bob", mode="local")
    themes = mcp_server.brain_graphrag_search(
        query="x", mode="themes", person="dana lee"
    )
    entity = mcp_server.brain_graphrag_entity(name="bob")
    for payload in (search, themes, entity):
        assert "cypher" not in _json.dumps(payload).lower()


# --------------------------------------------------------------------------- #
# 10. PARITY — MCP payload == CLI --json payload (minus session_id)
# --------------------------------------------------------------------------- #
def test_parity_mcp_search_equals_cli_json(
    test_db: psycopg.Connection,
    monkeypatch: pytest.MonkeyPatch,
    fake_embedder: object,
) -> None:
    """The MCP search payload equals the CLI --json payload for the same inputs.

    Both surfaces call the same ``graph_rag_search`` core, so identical inputs
    must yield an identical ``GraphContext`` (only the per-call ``session_id``
    differs). This is the parity guarantee."""
    _seed_triangle(test_db)
    _build(test_db)

    # MCP side — installed state with the no-suppression cfg.
    monkeypatch.setattr(mcp_server, "_state", _make_state(fake_embedder))
    mcp_payload = mcp_server.brain_graphrag_search(query="bob", mode="local")

    # CLI side — same DB + the equivalent BRAIN_GRAPH_GENERIC_DF=1.0 env.
    monkeypatch.setenv("DATABASE_URL", TEST_DATABASE_URL)
    monkeypatch.setenv("BRAIN_GRAPH_GENERIC_DF", "1.0")
    res = CliRunner().invoke(
        app, ["graphrag", "search", "bob", "--mode", "local", "--json"]
    )
    assert res.exit_code == 0, res.output
    cli_payload = _json.loads(res.stdout)

    # session_id is freshly minted per call on both sides — drop before compare.
    mcp_payload.pop("session_id")
    cli_payload.pop("session_id")
    assert mcp_payload == cli_payload


# --------------------------------------------------------------------------- #
# 11. brain_graphrag_communities_build / brain_graphrag_communities (wave G3-f)
# --------------------------------------------------------------------------- #
def test_communities_build_tool(
    test_db: psycopg.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    """build tool detects, persists, and summarizes the tenant's communities."""
    fake = _FakeCommunityEnricher()
    _community_state(monkeypatch, fake)
    _seed_communities_corpus(test_db)
    result = mcp_server.brain_graphrag_communities_build()
    assert result["tenant_id"] == "default"
    assert result["build"]["communities_total"] == 2
    assert result["build"]["created"] == 2
    assert result["build"]["skipped"] is False
    assert result["summary"]["summarized"] == 2
    assert result["summary"]["embedded"] == 2
    assert fake.calls == 2


def test_communities_build_tool_skips_unchanged(
    test_db: psycopg.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A second build hits the dirty gate; force=True rebuilds anyway."""
    _community_state(monkeypatch, _FakeCommunityEnricher())
    _seed_communities_corpus(test_db)
    mcp_server.brain_graphrag_communities_build()

    skipped = mcp_server.brain_graphrag_communities_build()
    assert skipped["build"]["skipped"] is True

    forced = mcp_server.brain_graphrag_communities_build(force=True)
    assert forced["build"]["skipped"] is False
    assert forced["build"]["reused"] == 2


def test_communities_build_tool_succeeds_without_enricher(
    test_db: psycopg.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A None enricher (no Ollama) leaves summaries NULL; the build still works."""
    _community_state(monkeypatch, None)
    _seed_communities_corpus(test_db)
    result = mcp_server.brain_graphrag_communities_build()
    assert result["build"]["communities_total"] == 2
    # enricher=None → the whole summary/embedding pass is a logged no-op.
    assert result["summary"]["skipped"] is True


def test_communities_list_tool(
    test_db: psycopg.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    """list tool returns the stored community rows for the tenant."""
    _community_state(monkeypatch, _FakeCommunityEnricher())
    _seed_communities_corpus(test_db)
    mcp_server.brain_graphrag_communities_build()

    payload = mcp_server.brain_graphrag_communities()
    assert payload["tenant_id"] == "default"
    assert payload["count"] == 2
    sample = payload["communities"][0]
    for key in ("community_key", "member_count", "edge_count", "summary"):
        assert key in sample, key
    assert sample["member_count"] == 3


def test_communities_tools_tenant_scoped(
    test_db: psycopg.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    """build + list are tenant-scoped — a built tenant ≠ the default."""
    _community_state(monkeypatch, _FakeCommunityEnricher())
    _seed_communities_corpus(test_db, tenant="custom")
    mcp_server.brain_graphrag_communities_build(tenant="custom")

    custom = mcp_server.brain_graphrag_communities(tenant="custom")
    assert custom["tenant_id"] == "custom"
    assert custom["count"] == 2

    default = mcp_server.brain_graphrag_communities()
    assert default["tenant_id"] == "default"
    assert default["count"] == 0


def test_communities_tools_no_cypher_in_payloads(
    test_db: psycopg.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Neither communities tool leaks raw Cypher onto the wire."""
    _community_state(monkeypatch, _FakeCommunityEnricher())
    _seed_communities_corpus(test_db)
    build = mcp_server.brain_graphrag_communities_build()
    listed = mcp_server.brain_graphrag_communities()
    for payload in (build, listed):
        assert "cypher" not in _json.dumps(payload).lower()


def test_communities_build_age_absent_internal_error(
    test_db: psycopg.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    """AGE-absent image → INTERNAL_ERROR (matches the other graphrag tools)."""
    _community_state(monkeypatch, _FakeCommunityEnricher())
    _seed_communities_corpus(test_db)
    monkeypatch.setattr(mcp_server, "age_extension_available", lambda conn: False)
    with pytest.raises(McpError) as exc_info:
        mcp_server.brain_graphrag_communities_build()
    assert exc_info.value.error.code == INTERNAL_ERROR
    assert "Apache AGE is not available" in exc_info.value.error.message


# --------------------------------------------------------------------------- #
# 12. brain_graphrag_refresh (B3 parity — `brain graphrag refresh`)
# --------------------------------------------------------------------------- #
def test_refresh_recomputes_aggregates(
    test_db: psycopg.Connection, graph_state: mcp_server._State  # noqa: ARG001
) -> None:
    """refresh recomputes the tenant's aggregate edges from the SoT.

    The triangle's three co-occurrence pairs (m1 alice-bob, m2 alice-carol,
    m3 bob-carol) yield exactly three relationship edges; nothing is orphaned.
    """
    _seed_triangle(test_db)
    _build(test_db)
    result = mcp_server.brain_graphrag_refresh()
    assert result["tenant_id"] == "default"
    assert result["relationship_count"] == 3
    assert result["orphans_removed"] == 0
    # The graph is still queryable after the recompute.
    payload = mcp_server.brain_graphrag_search(query="bob", mode="local")
    assert {"bob", "alice", "carol"} <= {
        e["canonical_key"] for e in payload["entities"]
    }


def test_refresh_idempotent_second_run(
    test_db: psycopg.Connection, graph_state: mcp_server._State  # noqa: ARG001
) -> None:
    """A second refresh converges to the identical edge count (idempotent)."""
    _seed_triangle(test_db)
    _build(test_db)
    first = mcp_server.brain_graphrag_refresh()
    second = mcp_server.brain_graphrag_refresh()
    assert second["relationship_count"] == first["relationship_count"] == 3
    assert second["orphans_removed"] == 0


def test_refresh_tenant_scoping(
    test_db: psycopg.Connection, graph_state: mcp_server._State  # noqa: ARG001
) -> None:
    """refresh is tenant-scoped — a built tenant recomputes; default stays empty."""
    _seed_triangle(test_db)
    _build(test_db, tenant="custom")

    custom = mcp_server.brain_graphrag_refresh(tenant="custom")
    assert custom["tenant_id"] == "custom"
    assert custom["relationship_count"] == 3

    # The default tenant has no contributions → a clean zero recompute.
    default = mcp_server.brain_graphrag_refresh()
    assert default["tenant_id"] == "default"
    assert default["relationship_count"] == 0
    assert default["orphans_removed"] == 0


def test_refresh_no_cypher_in_payload(
    test_db: psycopg.Connection, graph_state: mcp_server._State  # noqa: ARG001
) -> None:
    _seed_triangle(test_db)
    _build(test_db)
    result = mcp_server.brain_graphrag_refresh()
    assert "cypher" not in _json.dumps(result).lower()


def test_refresh_age_absent_internal_error(
    test_db: psycopg.Connection,
    monkeypatch: pytest.MonkeyPatch,
    graph_state: mcp_server._State,  # noqa: ARG001
) -> None:
    """AGE-absent image → INTERNAL_ERROR (matches the other graphrag tools)."""
    _seed_triangle(test_db)
    _build(test_db)
    monkeypatch.setattr(mcp_server, "age_extension_available", lambda conn: False)
    with pytest.raises(McpError) as exc_info:
        mcp_server.brain_graphrag_refresh()
    assert exc_info.value.error.code == INTERNAL_ERROR
    assert "Apache AGE is not available" in exc_info.value.error.message


# --------------------------------------------------------------------------- #
# 13. brain_graphrag_communities_refresh (B3 parity — `communities refresh`)
# --------------------------------------------------------------------------- #
def test_communities_refresh_forces_rebuild(
    test_db: psycopg.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    """refresh bypasses the dirty gate that a plain build would hit.

    Build once, then a plain build SKIPS (graph unchanged), but refresh forces a
    full re-detect (``skipped`` False, the two communities reused).
    """
    _community_state(monkeypatch, _FakeCommunityEnricher())
    _seed_communities_corpus(test_db)
    mcp_server.brain_graphrag_communities_build()

    skipped = mcp_server.brain_graphrag_communities_build()
    assert skipped["build"]["skipped"] is True

    forced = mcp_server.brain_graphrag_communities_refresh()
    assert forced["tenant_id"] == "default"
    assert forced["build"]["skipped"] is False
    assert forced["build"]["communities_total"] == 2
    assert forced["build"]["reused"] == 2


def test_communities_refresh_tenant_scoped(
    test_db: psycopg.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    """refresh is tenant-scoped (matches the build tool)."""
    _community_state(monkeypatch, _FakeCommunityEnricher())
    _seed_communities_corpus(test_db, tenant="custom")
    mcp_server.brain_graphrag_communities_refresh(tenant="custom")

    custom = mcp_server.brain_graphrag_communities(tenant="custom")
    assert custom["tenant_id"] == "custom"
    assert custom["count"] == 2

    default = mcp_server.brain_graphrag_communities()
    assert default["count"] == 0


def test_communities_refresh_no_cypher_in_payload(
    test_db: psycopg.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    _community_state(monkeypatch, _FakeCommunityEnricher())
    _seed_communities_corpus(test_db)
    result = mcp_server.brain_graphrag_communities_refresh()
    assert "cypher" not in _json.dumps(result).lower()


def test_communities_refresh_age_absent_internal_error(
    test_db: psycopg.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    """AGE-absent image → INTERNAL_ERROR (matches the other graphrag tools)."""
    _community_state(monkeypatch, _FakeCommunityEnricher())
    _seed_communities_corpus(test_db)
    monkeypatch.setattr(mcp_server, "age_extension_available", lambda conn: False)
    with pytest.raises(McpError) as exc_info:
        mcp_server.brain_graphrag_communities_refresh()
    assert exc_info.value.error.code == INTERNAL_ERROR
    assert "Apache AGE is not available" in exc_info.value.error.message
