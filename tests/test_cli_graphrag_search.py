"""CLI tests for the GraphRAG retrieval surfaces (wave G2-h).

Covers ``brain graphrag search`` / ``themes`` / ``entity`` against the live AGE
test instance (port 5434), built from a tiny synthetic person graph via
``build_graph`` + the real ``AgeBackend`` (the CLI opens its own ``connect_age``
connection, so the autocommit ``test_db`` seed is visible). Asserts:

* mode dispatch (explicit local / auto / themes / global): explicit ``global``
  EXECUTES the community path, and an ``auto`` thematic query with no resolvable
  person now ROUTES to ``global`` (the G3-e flip — no degradation note);
* the ``themes`` command's required ``--person`` (exit 2 when missing);
* the ``entity`` neighbourhood wrapper;
* the ``--json`` wire shape, ``--tenant`` scoping, and the opt-in ``--synthesize``
  group-summary (fake enricher — no live Ollama);
* error → exit-code mapping: ``PersonNotFound`` / ``PersonAmbiguous`` → exit 1,
  AGE-absent → exit 1.

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
from typer.testing import CliRunner

from brain.cli import app
from brain.graph_rag.backends import AgeBackend
from brain.graph_rag.build import build_graph
from brain.graph_rag.reconcile import ReconcileConfig
from brain.queries import iter_all_document_ids
from brain.vault.derived_links.directory import DirectoryStore
from tests.conftest import FakeEmbedder

TEST_DATABASE_URL = "postgresql://brain:brain@localhost:5434/second_brain_test"

# ``graph_communities.summary_embedding`` ships as vector(1024) (migration 013).
# The community CLI tests monkeypatch ``_build_embedder`` to a 1024-dim
# FakeEmbedder so summarize_communities writes embeddings at the migration dim
# (no resize) and the global query embeds at the same dim.
_SUMMARY_DIM = 1024

# Suppression-disabled ratio (cap = round(N * 1.0) = N) so the tiny corpora's
# edges always materialize. Mirrors ``_NO_SUPPRESS`` in test_graphrag_build.
_NO_SUPPRESS = 1.0


# --------------------------------------------------------------------------- #
# Seeding helpers (mirror test_graphrag_build / test_graphrag_retrieve)
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


def _env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DATABASE_URL", TEST_DATABASE_URL)
    monkeypatch.setenv("BRAIN_GRAPH_GENERIC_DF", "1.0")


# --------------------------------------------------------------------------- #
# Community seeding (wave G3-f) — direct relational inserts (mirror
# test_graphrag_global). Two dense triangles + a weak bridge → two communities
# (min_size=3). Documents/mentions/chunks back the summary + global doc results.
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


class _NullCommunityEnricher:
    """Fake summarizer that always returns None (simulates an unreachable Ollama)."""

    model = "fake-model:1b"

    def summarize_group(
        self,
        *,
        person: str | None,
        entity_names: list[str],
        doc_titles: list[str],
    ) -> str | None:
        return None


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


def _add_chunk(
    conn: psycopg.Connection[Any], document_id: str, content: str
) -> None:
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


def _patch_community_factories(
    monkeypatch: pytest.MonkeyPatch, enricher: object
) -> None:
    """Swap the CLI's enricher/embedder factories for fakes (no Ollama)."""
    monkeypatch.setattr("brain.cli._build_enricher", lambda cfg: enricher)
    monkeypatch.setattr(
        "brain.cli._build_embedder", lambda cfg: FakeEmbedder(dim=_SUMMARY_DIM)
    )


# --------------------------------------------------------------------------- #
# 1. search — local (explicit + auto)
# --------------------------------------------------------------------------- #
def test_cli_search_explicit_local(
    test_db: psycopg.Connection[Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    _seed_triangle(test_db)
    _build(test_db)
    _env(monkeypatch)
    res = CliRunner().invoke(
        app, ["graphrag", "search", "bob", "--mode", "local"], env={"COLUMNS": "200"}
    )
    assert res.exit_code == 0, res.output
    assert "mode=local" in res.output
    # bob (seed) + its neighbours alice/carol render in the entities table.
    out = res.output.lower()
    assert "bob" in out
    assert "alice" in out
    assert "carol" in out


def test_cli_search_auto_routes_local(
    test_db: psycopg.Connection[Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    """A non-thematic query under the default --mode auto dispatches to local."""
    _seed_triangle(test_db)
    _build(test_db)
    _env(monkeypatch)
    res = CliRunner().invoke(app, ["graphrag", "search", "bob"])
    assert res.exit_code == 0, res.output
    assert "mode=local" in res.output


def test_cli_search_auto_routes_global(
    test_db: psycopg.Connection[Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    """Thematic query + no resolvable person now ROUTES to global (G3-e flip).

    No longer the G2 global→local degradation: ``mode='global'`` executes the
    community path directly, with all degradation signals dormant (``None``).
    """
    _seed_triangle(test_db)
    _build(test_db)
    _env(monkeypatch)
    monkeypatch.setattr(
        "brain.cli._build_embedder", lambda cfg: FakeEmbedder(dim=_SUMMARY_DIM)
    )
    res = CliRunner().invoke(
        app, ["graphrag", "search", "recurring themes", "--json"]
    )
    assert res.exit_code == 0, res.output
    payload = _json.loads(res.stdout)
    assert payload["mode"] == "global"
    # The G3-e flip: no degradation signalling — global executes directly.
    assert payload["requested_mode"] is None
    assert payload["degraded_from"] is None
    assert payload["degradation_reason"] is None
    # No communities built → empty-but-valid global context.
    assert payload["communities"] == []


def test_cli_search_explicit_global_executes(
    test_db: psycopg.Connection[Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    """An explicit --mode global now EXECUTES (no longer rejected exit 2)."""
    _seed_communities_corpus(test_db)
    _env(monkeypatch)
    _patch_community_factories(monkeypatch, _FakeCommunityEnricher())
    build = CliRunner().invoke(app, ["graphrag", "communities", "build", "--json"])
    assert build.exit_code == 0, build.output

    res = CliRunner().invoke(
        app,
        ["graphrag", "search", "Cluster", "--mode", "global", "--json"],
        env={"COLUMNS": "200"},
    )
    assert res.exit_code == 0, res.output
    payload = _json.loads(res.stdout)
    assert payload["mode"] == "global"
    # Communities were built + summarized + embedded → the vector leg ranks them.
    assert payload["communities"], "expected at least one ranked community"
    first = payload["communities"][0]
    for key in ("community_key", "level", "member_count", "score", "summary"):
        assert key in first, key
    assert "cypher" not in res.stdout.lower()


def test_cli_search_fuse_executes(
    test_db: psycopg.Connection[Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    """An explicit --mode fuse RRF-merges the graph + hybrid doc legs (G4-c).

    Parity with the MCP ``brain_graphrag_search(mode='fuse')`` test: the fused
    docs land in the standard ``docs`` array and per-doc leg provenance rides in
    ``explanation.matched_filters.fuse_doc_provenance``.
    """
    _env(monkeypatch)
    monkeypatch.setattr("brain.cli._build_embedder", lambda cfg: FakeEmbedder())
    _seed_triangle(test_db)
    _build(test_db)
    # A chunk so the hybrid (FTS+vector) leg returns a doc that overlaps the
    # graph leg → a real two-leg fusion.
    row = test_db.execute(
        "SELECT id::text FROM documents WHERE title = 'm1'"
    ).fetchone()
    assert row is not None
    _add_chunk(test_db, row[0], "bob roadmap planning")

    res = CliRunner().invoke(
        app,
        ["graphrag", "search", "bob", "--mode", "fuse", "--json"],
        env={"COLUMNS": "200"},
    )
    assert res.exit_code == 0, res.stdout
    payload = _json.loads(res.stdout)
    assert payload["mode"] == "fuse"
    assert payload["docs"], "expected fused docs"
    prov = payload["explanation"]["matched_filters"]["fuse_doc_provenance"]
    returned_ids = {d["id"] for d in payload["docs"]}
    assert set(prov) == returned_ids
    # The graph doc with a matching chunk is in BOTH legs; every returned doc came
    # from at least one leg.
    assert any(
        e["graph_rank"] is not None and e["hybrid_rank"] is not None
        for e in prov.values()
    )
    for entry in prov.values():
        assert entry["graph_rank"] is not None or entry["hybrid_rank"] is not None
    assert payload["explanation"]["matched_filters"]["hybrid_vector_arm_used"] is True
    assert "cypher" not in res.stdout.lower()


# --------------------------------------------------------------------------- #
# 2. themes (HEADLINE) — command + via search --mode themes
# --------------------------------------------------------------------------- #
def test_cli_themes_command(
    test_db: psycopg.Connection[Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    _seed_dana_cluster(test_db)
    _build(test_db)
    _env(monkeypatch)
    # COLUMNS=200 keeps the Themes table's Entities column from wrapping/truncating.
    res = CliRunner().invoke(
        app, ["graphrag", "themes", "--person", "dana lee"], env={"COLUMNS": "200"}
    )
    assert res.exit_code == 0, res.output
    assert "mode=themes" in res.output
    assert "person=Dana Lee" in res.output
    # The {bob, carol} theme group surfaces (co-mentioned within Dana's scope).
    out = res.output.lower()
    assert "bob" in out
    assert "carol" in out


def test_cli_themes_command_requires_person() -> None:
    """`brain graphrag themes` with no --person is a usage error (exit 2)."""
    res = CliRunner().invoke(app, ["graphrag", "themes"], env={"COLUMNS": "200"})
    assert res.exit_code == 2
    assert "person" in res.output.lower()


def test_cli_search_themes_mode_json(
    test_db: psycopg.Connection[Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    """`search --mode themes --person X --json` returns the themes wire shape."""
    _seed_dana_cluster(test_db)
    _build(test_db)
    _env(monkeypatch)
    res = CliRunner().invoke(
        app,
        ["graphrag", "search", "ignored", "--mode", "themes",
         "--person", "dana lee", "--json"],
    )
    assert res.exit_code == 0, res.output
    payload = _json.loads(res.stdout)
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


# --------------------------------------------------------------------------- #
# 3. entity neighbourhood
# --------------------------------------------------------------------------- #
def test_cli_entity_command(
    test_db: psycopg.Connection[Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    _seed_triangle(test_db)
    _build(test_db)
    _env(monkeypatch)
    res = CliRunner().invoke(
        app, ["graphrag", "entity", "bob"], env={"COLUMNS": "200"}
    )
    assert res.exit_code == 0, res.output
    assert "mode=local" in res.output
    out = res.output.lower()
    assert "bob" in out
    # Neighbours reachable from bob's CO_OCCURS edges.
    assert "alice" in out
    assert "carol" in out


def test_cli_entity_json_shape(
    test_db: psycopg.Connection[Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    """The --json payload carries the full GraphContext wire shape."""
    _seed_triangle(test_db)
    _build(test_db)
    _env(monkeypatch)
    res = CliRunner().invoke(app, ["graphrag", "entity", "bob", "--json"])
    assert res.exit_code == 0, res.output
    payload = _json.loads(res.stdout)
    for key in (
        "session_id", "mode", "query", "tenant_id", "person", "requested_mode",
        "degraded_from", "degradation_reason", "themes", "entities", "docs",
        "explanation",
    ):
        assert key in payload, key
    assert payload["mode"] == "local"
    assert payload["query"] == "bob"
    assert payload["tenant_id"] == "default"
    # The seed entity is present with the read-side fields.
    keys = {e["canonical_key"] for e in payload["entities"]}
    assert "bob" in keys
    sample = payload["entities"][0]
    for field in ("id", "entity_type", "name", "canonical_key", "doc_count"):
        assert field in sample, field
    # Explanation carries the traversal diagnostics, no raw Cypher.
    assert payload["explanation"]["mode"] == "local"
    assert payload["explanation"]["depth"] >= 1
    assert "cypher" not in res.stdout.lower()


# --------------------------------------------------------------------------- #
# 4. --tenant scoping
# --------------------------------------------------------------------------- #
def test_cli_search_tenant_scoping(
    test_db: psycopg.Connection[Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    """Building into a custom tenant; querying default returns no entities."""
    _seed_triangle(test_db)
    _build(test_db, tenant="custom")
    _env(monkeypatch)

    # Query the custom tenant → bob's neighbourhood resolves.
    res_custom = CliRunner().invoke(
        app, ["graphrag", "search", "bob", "--mode", "local",
              "--tenant", "custom", "--json"]
    )
    assert res_custom.exit_code == 0, res_custom.output
    custom = _json.loads(res_custom.stdout)
    assert custom["tenant_id"] == "custom"
    assert {e["canonical_key"] for e in custom["entities"]} >= {"bob"}

    # Query the default tenant → nothing was built there → no seed entity.
    res_default = CliRunner().invoke(
        app, ["graphrag", "search", "bob", "--mode", "local", "--json"]
    )
    assert res_default.exit_code == 0, res_default.output
    default = _json.loads(res_default.stdout)
    assert default["tenant_id"] == "default"
    assert default["entities"] == []


# --------------------------------------------------------------------------- #
# 5. --synthesize (fake enricher — never live Ollama)
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


def test_cli_themes_synthesize_uses_injected_enricher(
    test_db: psycopg.Connection[Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    """`themes --synthesize` attaches the (faked) enricher's group summary."""
    _seed_dana_cluster(test_db)
    _build(test_db)
    _env(monkeypatch)
    fake = _FakeEnricher()
    monkeypatch.setattr("brain.cli._build_enricher", lambda cfg: fake)

    res = CliRunner().invoke(
        app,
        ["graphrag", "themes", "--person", "dana lee", "--synthesize", "--json"],
    )
    assert res.exit_code == 0, res.output
    payload = _json.loads(res.stdout)
    assert payload["themes"], "expected at least one theme group"
    assert all(
        t["summary"] == "SYNTHETIC THEME SUMMARY" for t in payload["themes"]
    )
    assert fake.calls  # the enricher was actually invoked


# --------------------------------------------------------------------------- #
# 6. Error → exit-code mapping
# --------------------------------------------------------------------------- #
def test_cli_themes_person_not_found_exit1(
    test_db: psycopg.Connection[Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    _seed_triangle(test_db)
    _build(test_db)
    _env(monkeypatch)
    res = CliRunner().invoke(
        app, ["graphrag", "themes", "--person", "nobody-unknown-xyz"]
    )
    assert res.exit_code == 1, res.output
    assert "nobody-unknown-xyz" in res.output
    assert "no one" in res.output.lower()


def test_cli_themes_person_ambiguous_exit1(
    test_db: psycopg.Connection[Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    _seed_directory(
        test_db,
        [("dana lee", "dana.lee@x.com"), ("dana park", "dana.park@x.com")],
    )
    _env(monkeypatch)
    res = CliRunner().invoke(app, ["graphrag", "themes", "--person", "dana"])
    assert res.exit_code == 1, res.output
    assert "dana" in res.output.lower()
    assert "more specific" in res.output.lower()


def test_cli_search_exits_when_age_absent(
    test_db: psycopg.Connection[Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    """AGE-absent image → clean exit 1, no traceback (matches build/refresh)."""
    _seed_triangle(test_db)
    _build(test_db)
    _env(monkeypatch)
    monkeypatch.setattr("brain.cli.age_extension_available", lambda conn: False)
    res = CliRunner().invoke(app, ["graphrag", "search", "bob", "--mode", "local"])
    assert res.exit_code == 1
    assert "Apache AGE is not available" in res.output


def test_cli_search_unknown_mode_exit2(
    test_db: psycopg.Connection[Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    """An unrecognized --mode value surfaces as a BadParameter (exit 2)."""
    _seed_triangle(test_db)
    _build(test_db)
    _env(monkeypatch)
    res = CliRunner().invoke(
        app,
        ["graphrag", "search", "bob", "--mode", "sideways"],
        env={"COLUMNS": "200"},
    )
    assert res.exit_code == 2, res.output
    normalized = " ".join(res.output.replace("│", " ").split())
    assert "unknown graph retrieval mode" in normalized


def test_cli_search_fuse_non_default_tenant_exit2(
    test_db: psycopg.Connection[Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    """`--mode fuse --tenant <non-default>` is a usage error (exit 2) — P1-1 gate.

    Regression for G4-review P1-1: fuse's hybrid leg is corpus-wide
    (documents/chunks are not tenantized), so a non-default fuse would leak
    cross-tenant documents. The gate's ValueError maps to BadParameter (exit 2).
    """
    _build(test_db)  # bootstrap AGE so the gate (not AGE-absent) is what fires
    _env(monkeypatch)
    res = CliRunner().invoke(
        app,
        ["graphrag", "search", "bob", "--mode", "fuse", "--tenant", "custom"],
        env={"COLUMNS": "200"},
    )
    assert res.exit_code == 2, res.output
    normalized = " ".join(res.output.replace("│", " ").split())
    assert "only available for tenant 'default'" in normalized


# --------------------------------------------------------------------------- #
# 7. communities admin (build / refresh / list) — wave G3-f
# --------------------------------------------------------------------------- #
def test_cli_communities_build_creates_and_summarizes(
    test_db: psycopg.Connection[Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    """`communities build --json` detects, persists, and summarizes communities."""
    _seed_communities_corpus(test_db)
    _env(monkeypatch)
    fake = _FakeCommunityEnricher()
    _patch_community_factories(monkeypatch, fake)
    res = CliRunner().invoke(app, ["graphrag", "communities", "build", "--json"])
    assert res.exit_code == 0, res.output
    payload = _json.loads(res.stdout)
    assert payload["tenant_id"] == "default"
    # Two dense triangles → two communities, both freshly created + summarized.
    assert payload["build"]["communities_total"] == 2
    assert payload["build"]["created"] == 2
    assert payload["build"]["skipped"] is False
    assert payload["summary"]["summarized"] == 2
    assert payload["summary"]["embedded"] == 2
    assert fake.calls == 2


def test_cli_communities_build_skips_unchanged_graph(
    test_db: psycopg.Connection[Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    """A second `communities build` on an unchanged graph hits the dirty gate."""
    _seed_communities_corpus(test_db)
    _env(monkeypatch)
    _patch_community_factories(monkeypatch, _FakeCommunityEnricher())
    first = CliRunner().invoke(app, ["graphrag", "communities", "build", "--json"])
    assert first.exit_code == 0, first.output

    second = CliRunner().invoke(app, ["graphrag", "communities", "build", "--json"])
    assert second.exit_code == 0, second.output
    payload = _json.loads(second.stdout)
    assert payload["build"]["skipped"] is True
    assert payload["build"]["communities_total"] == 2


def test_cli_communities_refresh_forces_rebuild(
    test_db: psycopg.Connection[Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    """`communities refresh` rebuilds even when the dirty gate would skip."""
    _seed_communities_corpus(test_db)
    _env(monkeypatch)
    _patch_community_factories(monkeypatch, _FakeCommunityEnricher())
    build = CliRunner().invoke(app, ["graphrag", "communities", "build", "--json"])
    assert build.exit_code == 0, build.output

    refresh = CliRunner().invoke(
        app, ["graphrag", "communities", "refresh", "--json"]
    )
    assert refresh.exit_code == 0, refresh.output
    payload = _json.loads(refresh.stdout)
    # Forced: NOT skipped even though the graph is unchanged (dirty=False).
    assert payload["build"]["skipped"] is False
    assert payload["build"]["communities_total"] == 2
    # Reused keys (Jaccard identity preserved across the forced rebuild).
    assert payload["build"]["reused"] == 2


def test_cli_communities_build_succeeds_without_ollama(
    test_db: psycopg.Connection[Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    """An unreachable Ollama (enricher → None) leaves summaries NULL; build OK."""
    _seed_communities_corpus(test_db)
    _env(monkeypatch)
    _patch_community_factories(monkeypatch, _NullCommunityEnricher())
    res = CliRunner().invoke(app, ["graphrag", "communities", "build", "--json"])
    assert res.exit_code == 0, res.output
    payload = _json.loads(res.stdout)
    # Communities still materialize; summaries all fail (NULL), nothing embedded.
    assert payload["build"]["communities_total"] == 2
    assert payload["summary"]["summarized"] == 0
    assert payload["summary"]["summary_failures"] == 2
    assert payload["summary"]["embedded"] == 0


def test_cli_communities_list_shows_built_communities(
    test_db: psycopg.Connection[Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    """`communities list --json` returns the stored community rows."""
    _seed_communities_corpus(test_db)
    _env(monkeypatch)
    _patch_community_factories(monkeypatch, _FakeCommunityEnricher())
    CliRunner().invoke(app, ["graphrag", "communities", "build"])

    res = CliRunner().invoke(app, ["graphrag", "communities", "list", "--json"])
    assert res.exit_code == 0, res.output
    payload = _json.loads(res.stdout)
    assert payload["tenant_id"] == "default"
    assert payload["count"] == 2
    assert len(payload["communities"]) == 2
    sample = payload["communities"][0]
    for key in ("community_key", "member_count", "edge_count", "summary"):
        assert key in sample, key
    assert sample["member_count"] == 3
    assert "cypher" not in res.stdout.lower()


def test_cli_communities_list_human_render(
    test_db: psycopg.Connection[Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    """The bare `communities` group defaults to the (human) list table."""
    _seed_communities_corpus(test_db)
    _env(monkeypatch)
    _patch_community_factories(monkeypatch, _FakeCommunityEnricher())
    CliRunner().invoke(app, ["graphrag", "communities", "build"])

    res = CliRunner().invoke(
        app, ["graphrag", "communities"], env={"COLUMNS": "200"}
    )
    assert res.exit_code == 0, res.output
    assert "Communities" in res.output
    assert "Community covering" in res.output


def test_cli_communities_list_tenant_scoped(
    test_db: psycopg.Connection[Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    """`communities list` is tenant-scoped — a built tenant ≠ the default."""
    _seed_communities_corpus(test_db, tenant="custom")
    _env(monkeypatch)
    _patch_community_factories(monkeypatch, _FakeCommunityEnricher())
    CliRunner().invoke(
        app, ["graphrag", "communities", "build", "--tenant", "custom"]
    )

    custom = _json.loads(
        CliRunner()
        .invoke(
            app, ["graphrag", "communities", "list", "--tenant", "custom", "--json"]
        )
        .stdout
    )
    assert custom["tenant_id"] == "custom"
    assert custom["count"] == 2

    default = _json.loads(
        CliRunner().invoke(app, ["graphrag", "communities", "list", "--json"]).stdout
    )
    assert default["tenant_id"] == "default"
    assert default["count"] == 0


def test_cli_communities_build_exits_when_age_absent(
    test_db: psycopg.Connection[Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    """AGE-absent image → clean exit 1 (matches the other graphrag commands)."""
    _seed_communities_corpus(test_db)
    _env(monkeypatch)
    _patch_community_factories(monkeypatch, _FakeCommunityEnricher())
    monkeypatch.setattr("brain.cli.age_extension_available", lambda conn: False)
    res = CliRunner().invoke(app, ["graphrag", "communities", "build"])
    assert res.exit_code == 1
    assert "Apache AGE is not available" in res.output
