"""Tests for the concept aspect of graph reconcile/build (wave G2-c, GraphRAG).

The concept counterpart of ``test_graphrag_reconcile`` / ``test_graphrag_build``.
Concepts are extracted by an injected :class:`brain.graph_rag.extract.EntityExtractor`;
every test passes a deterministic ``FakeExtractor`` through the DI seam (NO live
Ollama, NO monkey-patching of production modules — CLAUDE.md rule 13). The one
CLI test that cannot reach the DI seam swaps the extractor *factory* with
``monkeypatch.setattr`` (a standard test double, explicitly allowed by rule 13).

Two layers, both against the AGE test instance (port 5434):

* **Live-AGE integration** — single-doc concept build, edit re-extract, delete +
  orphan-concept GC, idempotency (skip = no re-extraction via a call-counting
  fake), model-swap re-extract, batched ≡ incremental, tenant isolation,
  real-word-position co-occurrence (distinct from the person aspect's doc-level
  co-presence), person+concept coexistence (no MENTIONED_IN clobbering), and the
  ``concepts_enabled`` + missing-extractor guard.
* **Unit** — the pure ``concepts.py`` helpers (``concept_inputs_hash`` /
  ``concept_mention_source``).

All entity names are synthetic (Stripe / Phoenix / Acme / Datadog / Kafka); no
PII. The schema + AGE graph are reset per test by the ``test_db`` fixture.
"""
from __future__ import annotations

import hashlib
import json
import uuid
from collections.abc import Mapping, Sequence
from typing import Any

import psycopg
import pytest
from typer.testing import CliRunner

from brain.cli import app
from brain.db import DEFAULT_GRAPH_NAME
from brain.errors import GraphReconcileError
from brain.graph_rag.backends import AgeBackend
from brain.graph_rag.build import build_graph
from brain.graph_rag.concepts import (
    CONCEPTS_ASPECT,
    concept_inputs_hash,
    concept_mention_source,
)
from brain.graph_rag.extract import ExtractedEntity
from brain.graph_rag.reconcile import (
    ReconcileConfig,
    reconcile_document,
    remove_document,
)
from brain.vault.derived_links.directory import DirectoryStore

TEST_DATABASE_URL = "postgresql://brain:brain@localhost:5434/second_brain_test"

# Suppression-disabled ratio (cap = round(N * 1.0) = N) so the tiny test corpora
# always materialize edges. Mirrors ``_NO_SUPPRESS`` in test_graphrag_reconcile.
_NO_SUPPRESS = 1.0


# --------------------------------------------------------------------------- #
# Fake extractor (DI, not patching)
# --------------------------------------------------------------------------- #
class FakeExtractor:
    """Deterministic :class:`EntityExtractor` for the DI seam.

    Returns canned :class:`ExtractedEntity` lists keyed on a substring of the
    document text (so distinct docs can yield distinct concepts), and counts
    ``extract`` calls so a test can assert that a watermark skip performs NO
    extraction. ``version`` is mutable to simulate a model/algorithm swap.
    """

    def __init__(
        self,
        by_marker: dict[str, list[ExtractedEntity]] | None = None,
        *,
        default: list[ExtractedEntity] | None = None,
        version: str = "fake-model@concepts-v1",
    ) -> None:
        self._by_marker = by_marker or {}
        self._default = default or []
        self._version = version
        self.calls = 0

    @property
    def version(self) -> str:
        return self._version

    @version.setter
    def version(self, value: str) -> None:
        self._version = value

    def extract(self, text: str) -> list[ExtractedEntity]:
        self.calls += 1
        for marker, entities in self._by_marker.items():
            if marker in text:
                return list(entities)
        return list(self._default)


def _concept(
    canonical_key: str,
    entity_type: str = "topic",
    *,
    positions: tuple[int, ...] = (0,),
    display_name: str | None = None,
) -> ExtractedEntity:
    return ExtractedEntity(
        entity_type=entity_type,
        canonical_key=canonical_key,
        display_name=display_name or canonical_key.title(),
        positions=positions,
        mention_count=max(1, len(positions)),
    )


def _ccfg(
    tenant_id: str = "default",
    generic_df_ratio: float = _NO_SUPPRESS,
    **kwargs: Any,
) -> ReconcileConfig:
    """A concepts-enabled ReconcileConfig (no-suppression by default)."""
    return ReconcileConfig(
        tenant_id=tenant_id,
        generic_df_ratio=generic_df_ratio,
        concepts_enabled=True,
        **kwargs,
    )


# --------------------------------------------------------------------------- #
# Seeding helpers
# --------------------------------------------------------------------------- #
def _seed_manual_doc(
    conn: psycopg.Connection[Any], *, external_id: str, content: str = "note body"
) -> str:
    """Insert a manual note (no participants → resolves to zero persons)."""
    src_row = conn.execute(
        "INSERT INTO sources (kind, external_id, metadata) "
        "VALUES ('manual', %s, '{}'::jsonb) RETURNING id",
        (external_id,),
    ).fetchone()
    assert src_row is not None
    salted = f"{content}\n<!-- {uuid.uuid4()} -->"
    content_hash = hashlib.sha256(salted.encode("utf-8")).hexdigest()
    doc_row = conn.execute(
        """
        INSERT INTO documents
            (source_id, title, content, content_hash, content_type)
        VALUES (%s, %s, %s, %s, 'note')
        RETURNING id::text
        """,
        (src_row[0], external_id, salted, content_hash),
    ).fetchone()
    assert doc_row is not None
    return str(doc_row[0])


def _seed_gmail_doc(
    conn: psycopg.Connection[Any],
    *,
    external_id: str,
    participants: Sequence[tuple[str, str]],
    content: str = "thread body",
) -> str:
    """Insert a gmail doc carrying from/to participants AND body content."""
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
        """
        INSERT INTO documents
            (source_id, title, content, content_hash, content_type, metadata)
        VALUES (%s, %s, %s, %s, 'email', %s::jsonb)
        RETURNING id::text
        """,
        (src_row[0], external_id, salted, content_hash, json.dumps(metadata)),
    ).fetchone()
    assert doc_row is not None
    return str(doc_row[0])


def _set_doc_content(
    conn: psycopg.Connection[Any], document_id: str, content: str
) -> None:
    """Rewrite a doc's body + content_hash (an edit busts the watermark)."""
    salted = f"{content}\n<!-- {uuid.uuid4()} -->"
    content_hash = hashlib.sha256(salted.encode("utf-8")).hexdigest()
    conn.execute(
        "UPDATE documents SET content = %s, content_hash = %s WHERE id = %s",
        (salted, content_hash, document_id),
    )


def _seed_directory(
    conn: psycopg.Connection[Any], pairs: Sequence[tuple[str, str]]
) -> None:
    store = DirectoryStore(conn)
    for name, email in pairs:
        store.upsert_pair(display_name=name, email=email, source="gmail")


def _backend(conn: psycopg.Connection[Any]) -> AgeBackend:
    backend = AgeBackend()
    backend.bootstrap(conn)
    return backend


# --------------------------------------------------------------------------- #
# Relational assertions
# --------------------------------------------------------------------------- #
_CONCEPT_TYPES = ("topic", "project", "org", "tool")


def _concept_keys(conn: psycopg.Connection[Any], tenant: str) -> set[str]:
    rows = conn.execute(
        "SELECT canonical_key FROM graph_entities "
        "WHERE tenant_id = %s AND entity_type = ANY(%s)",
        (tenant, list(_CONCEPT_TYPES)),
    ).fetchall()
    return {str(r[0]) for r in rows}


def _person_keys(conn: psycopg.Connection[Any], tenant: str) -> set[str]:
    rows = conn.execute(
        "SELECT canonical_key FROM graph_entities "
        "WHERE tenant_id = %s AND entity_type = 'person'",
        (tenant,),
    ).fetchall()
    return {str(r[0]) for r in rows}


def _concept_entity_id(
    conn: psycopg.Connection[Any], tenant: str, canonical_key: str
) -> str:
    row = conn.execute(
        "SELECT id::text FROM graph_entities "
        "WHERE tenant_id = %s AND entity_type = ANY(%s) AND canonical_key = %s",
        (tenant, list(_CONCEPT_TYPES), canonical_key),
    ).fetchone()
    assert row is not None, f"no concept entity for {canonical_key!r}"
    return str(row[0])


def _mention_count(conn: psycopg.Connection[Any], tenant: str) -> int:
    row = conn.execute(
        "SELECT count(*) FROM graph_entity_mentions WHERE tenant_id = %s",
        (tenant,),
    ).fetchone()
    assert row is not None
    return int(row[0])


def _concept_mention_count(conn: psycopg.Connection[Any], tenant: str) -> int:
    row = conn.execute(
        "SELECT count(*) FROM graph_entity_mentions m "
        "JOIN graph_entities e ON e.tenant_id = m.tenant_id AND e.id = m.entity_id "
        "WHERE m.tenant_id = %s AND e.entity_type = ANY(%s)",
        (tenant, list(_CONCEPT_TYPES)),
    ).fetchone()
    assert row is not None
    return int(row[0])


def _contribution_count(conn: psycopg.Connection[Any], tenant: str) -> int:
    row = conn.execute(
        "SELECT count(*) FROM graph_edge_contributions WHERE tenant_id = %s",
        (tenant,),
    ).fetchone()
    assert row is not None
    return int(row[0])


def _watermark_count(
    conn: psycopg.Connection[Any], tenant: str, aspect: str
) -> int:
    row = conn.execute(
        "SELECT count(*) FROM graph_index_state "
        "WHERE tenant_id = %s AND aspect = %s",
        (tenant, aspect),
    ).fetchone()
    assert row is not None
    return int(row[0])


def _concept_mention_source_for(
    conn: psycopg.Connection[Any], tenant: str, canonical_key: str
) -> str:
    eid = _concept_entity_id(conn, tenant, canonical_key)
    row = conn.execute(
        "SELECT source FROM graph_entity_mentions "
        "WHERE tenant_id = %s AND entity_id = %s",
        (tenant, eid),
    ).fetchone()
    assert row is not None
    return str(row[0])


# --------------------------------------------------------------------------- #
# AGE assertions (independent raw Cypher, not via the backend under test)
# --------------------------------------------------------------------------- #
def _cypher_scalar(
    conn: psycopg.Connection[Any], query: str, params: Mapping[str, Any]
) -> list[tuple[Any, ...]]:
    conn.execute('SET search_path = ag_catalog, "$user", public')
    try:
        rows = conn.execute(
            f"SELECT * FROM ag_catalog.cypher('{DEFAULT_GRAPH_NAME}', "
            f"$$ {query} $$, %s::ag_catalog.agtype) AS (v ag_catalog.agtype)",
            (json.dumps(params),),
        ).fetchall()
    finally:
        conn.execute("RESET search_path")
    return rows


def _age_entity_count(conn: psycopg.Connection[Any], tenant: str) -> int:
    rows = _cypher_scalar(
        conn, "MATCH (e:Entity {tenant_id: $t}) RETURN count(e)", {"t": tenant}
    )
    return int(str(rows[0][0]))


def _age_concept_count(conn: psycopg.Connection[Any], tenant: str) -> int:
    """Count AGE Entity vertices whose entity_type is a concept type."""
    total = 0
    for entity_type in _CONCEPT_TYPES:
        rows = _cypher_scalar(
            conn,
            "MATCH (e:Entity {tenant_id: $t}) WHERE e.entity_type = $et "
            "RETURN count(e)",
            {"t": tenant, "et": entity_type},
        )
        total += int(str(rows[0][0]))
    return total


def _age_cooccur_count(conn: psycopg.Connection[Any], tenant: str) -> int:
    rows = _cypher_scalar(
        conn,
        "MATCH ()-[r:CO_OCCURS {tenant_id: $t}]->() RETURN count(r)",
        {"t": tenant},
    )
    return int(str(rows[0][0]))


def _age_mentioned_in_count(conn: psycopg.Connection[Any], tenant: str) -> int:
    rows = _cypher_scalar(
        conn,
        "MATCH ()-[r:MENTIONED_IN {tenant_id: $t}]->() RETURN count(r)",
        {"t": tenant},
    )
    return int(str(rows[0][0]))


def _age_document_count(conn: psycopg.Connection[Any], tenant: str) -> int:
    rows = _cypher_scalar(
        conn, "MATCH (d:Document {tenant_id: $t}) RETURN count(d)", {"t": tenant}
    )
    return int(str(rows[0][0]))


# --------------------------------------------------------------------------- #
# 1. Single-doc concept build (live-AGE integration)
# --------------------------------------------------------------------------- #
def test_concept_single_doc_builds_entities_mentions_edges(
    test_db: psycopg.Connection[Any],
) -> None:
    backend = _backend(test_db)
    doc = _seed_manual_doc(test_db, external_id="n1", content="stripe and phoenix")
    extractor = FakeExtractor(
        default=[
            _concept("stripe", "tool", positions=(0,)),
            _concept("phoenix", "project", positions=(1,)),
        ]
    )

    result = reconcile_document(
        test_db, doc, backend=backend, config=_ccfg(), extractor=extractor
    )

    assert result.skipped is False
    # People aspect ran (0 persons) alongside concepts.
    assert result.person_count == 0
    assert result.concept_count == 2
    assert result.concept_mention_count == 2
    assert result.concept_contribution_count == 1
    assert result.relationship_count == 1
    assert extractor.calls == 1

    # Relational source-of-truth.
    assert _concept_keys(test_db, "default") == {"stripe", "phoenix"}
    assert _concept_mention_count(test_db, "default") == 2
    assert _contribution_count(test_db, "default") == 1
    # Provenance: source = extractor:<model>@<ver>.
    assert _concept_mention_source_for(test_db, "default", "stripe") == (
        f"extractor:{extractor.version}"
    )

    # AGE mirror: concept Entity vertices + CO_OCCURS edge appear.
    assert _age_concept_count(test_db, "default") == 2
    assert _age_entity_count(test_db, "default") == 2
    assert _age_cooccur_count(test_db, "default") == 1
    assert _age_document_count(test_db, "default") == 1

    # Concept watermark written; people watermark also written (always-on aspect).
    assert _watermark_count(test_db, "default", CONCEPTS_ASPECT) == 1
    assert _watermark_count(test_db, "default", "people") == 1


# --------------------------------------------------------------------------- #
# 2. Co-occurrence uses REAL word positions (distinct from doc-level people)
# --------------------------------------------------------------------------- #
def test_concept_cooccurrence_uses_real_word_positions(
    test_db: psycopg.Connection[Any],
) -> None:
    """Concepts pair only within the window; people would pair them all."""
    backend = _backend(test_db)
    doc = _seed_manual_doc(test_db, external_id="n1", content="a b ... c")
    # window default 3: a@0 & b@1 co-occur; c@50 is too far from both.
    extractor = FakeExtractor(
        default=[
            _concept("acme", "org", positions=(0,)),
            _concept("beacon", "topic", positions=(1,)),
            _concept("comet", "topic", positions=(50,)),
        ]
    )

    result = reconcile_document(
        test_db, doc, backend=backend, config=_ccfg(), extractor=extractor
    )

    assert result.concept_count == 3
    # Only the within-window pair (acme-beacon) — NOT the 3 pairs a doc-level
    # co-presence model (the person aspect) would have produced.
    assert result.concept_contribution_count == 1
    assert _contribution_count(test_db, "default") == 1
    assert _age_cooccur_count(test_db, "default") == 1


# --------------------------------------------------------------------------- #
# 3. Idempotency — skip = NO re-extraction (pre-extraction watermark check)
# --------------------------------------------------------------------------- #
def test_concept_idempotent_skip_does_not_re_extract(
    test_db: psycopg.Connection[Any],
) -> None:
    backend = _backend(test_db)
    doc = _seed_manual_doc(test_db, external_id="n1", content="stripe phoenix")
    extractor = FakeExtractor(
        default=[
            _concept("stripe", "tool", positions=(0,)),
            _concept("phoenix", "project", positions=(1,)),
        ]
    )

    first = reconcile_document(
        test_db, doc, backend=backend, config=_ccfg(), extractor=extractor
    )
    assert first.skipped is False
    assert extractor.calls == 1

    second = reconcile_document(
        test_db, doc, backend=backend, config=_ccfg(), extractor=extractor
    )
    assert second.skipped is True
    # The unchanged content_hash + extractor version short-circuited BEFORE the
    # LLM call — no second extraction.
    assert extractor.calls == 1
    assert _concept_keys(test_db, "default") == {"stripe", "phoenix"}
    assert _age_cooccur_count(test_db, "default") == 1


def test_concept_model_swap_re_extracts(
    test_db: psycopg.Connection[Any],
) -> None:
    """Bumping the extractor version busts the concept watermark → re-extract."""
    backend = _backend(test_db)
    doc = _seed_manual_doc(test_db, external_id="n1", content="stripe phoenix")
    extractor = FakeExtractor(
        default=[_concept("stripe", "tool"), _concept("phoenix", "project")]
    )
    reconcile_document(
        test_db, doc, backend=backend, config=_ccfg(), extractor=extractor
    )
    assert extractor.calls == 1

    # Simulate a model swap: same content, new extractor version.
    extractor.version = "other-model@concepts-v1"
    again = reconcile_document(
        test_db, doc, backend=backend, config=_ccfg(), extractor=extractor
    )
    assert again.skipped is False
    assert extractor.calls == 2
    # Mention provenance refreshed to the new model fingerprint.
    assert _concept_mention_source_for(test_db, "default", "stripe") == (
        "extractor:other-model@concepts-v1"
    )


# --------------------------------------------------------------------------- #
# 4. Edit — content change re-extracts + rewrites
# --------------------------------------------------------------------------- #
def test_concept_edit_re_extracts_and_rewrites(
    test_db: psycopg.Connection[Any],
) -> None:
    backend = _backend(test_db)
    doc = _seed_manual_doc(test_db, external_id="n1", content="MARKER_A body")
    extractor = FakeExtractor(
        by_marker={
            "MARKER_A": [_concept("stripe", "tool"), _concept("phoenix", "project")],
            "MARKER_B": [_concept("datadog", "tool")],
        }
    )
    reconcile_document(
        test_db, doc, backend=backend, config=_ccfg(), extractor=extractor
    )
    assert _concept_keys(test_db, "default") == {"stripe", "phoenix"}

    # Edit the content → new content_hash → re-extract with the new marker.
    _set_doc_content(test_db, doc, "MARKER_B body")
    result = reconcile_document(
        test_db, doc, backend=backend, config=_ccfg(), extractor=extractor
    )

    assert result.skipped is False
    assert result.concept_count == 1
    # Old concepts dropped + GC'd; only the new one survives.
    assert _concept_keys(test_db, "default") == {"datadog"}
    assert _concept_mention_count(test_db, "default") == 1
    # datadog alone → no co-occurrence edge.
    assert _contribution_count(test_db, "default") == 0
    assert _age_concept_count(test_db, "default") == 1
    assert _age_cooccur_count(test_db, "default") == 0


# --------------------------------------------------------------------------- #
# 5. Delete — remove_document cleans concepts + GCs orphan concepts
# --------------------------------------------------------------------------- #
def test_remove_document_cleans_concepts(
    test_db: psycopg.Connection[Any],
) -> None:
    backend = _backend(test_db)
    doc = _seed_manual_doc(test_db, external_id="n1", content="stripe phoenix")
    extractor = FakeExtractor(
        default=[_concept("stripe", "tool"), _concept("phoenix", "project")]
    )
    reconcile_document(
        test_db, doc, backend=backend, config=_ccfg(), extractor=extractor
    )
    assert _concept_keys(test_db, "default") == {"stripe", "phoenix"}

    result = remove_document(test_db, doc, backend=backend, config=_ccfg())

    # Both concepts orphaned + GC'd.
    assert result.orphans_removed == 2
    assert _concept_keys(test_db, "default") == set()
    assert _concept_mention_count(test_db, "default") == 0
    assert _contribution_count(test_db, "default") == 0
    assert _age_concept_count(test_db, "default") == 0
    assert _age_document_count(test_db, "default") == 0
    assert _age_cooccur_count(test_db, "default") == 0
    # Concept watermark cleared too.
    assert _watermark_count(test_db, "default", CONCEPTS_ASPECT) == 0


# --------------------------------------------------------------------------- #
# 6. Batched build ≡ incremental reconcile (concepts)
# --------------------------------------------------------------------------- #
def test_concept_batched_build_equals_incremental(
    test_db: psycopg.Connection[Any],
) -> None:
    backend = _backend(test_db)
    doc1 = _seed_manual_doc(test_db, external_id="n1", content="MARK1 body")
    doc2 = _seed_manual_doc(test_db, external_id="n2", content="MARK2 body")
    # Two docs sharing one concept (stripe) so an aggregate edge forms across docs.
    canned = {
        "MARK1": [_concept("stripe", "tool"), _concept("phoenix", "project")],
        "MARK2": [_concept("stripe", "tool"), _concept("datadog", "tool")],
    }

    # Batched backfill into tenant "batch".
    build_graph(
        test_db,
        [doc1, doc2],
        backend=backend,
        config=_ccfg("batch"),
        extractor=FakeExtractor(by_marker=canned),
    )
    # Incremental per-doc into tenant "incr".
    for doc in (doc1, doc2):
        reconcile_document(
            test_db,
            doc,
            backend=backend,
            config=_ccfg("incr"),
            extractor=FakeExtractor(by_marker=canned),
        )

    assert _concept_keys(test_db, "batch") == _concept_keys(test_db, "incr")
    assert _concept_keys(test_db, "batch") == {"stripe", "phoenix", "datadog"}
    assert _concept_mention_count(test_db, "batch") == _concept_mention_count(
        test_db, "incr"
    )
    assert _contribution_count(test_db, "batch") == _contribution_count(
        test_db, "incr"
    )
    assert _age_concept_count(test_db, "batch") == _age_concept_count(
        test_db, "incr"
    )
    assert _age_cooccur_count(test_db, "batch") == _age_cooccur_count(
        test_db, "incr"
    )


# --------------------------------------------------------------------------- #
# 7. Tenant isolation
# --------------------------------------------------------------------------- #
def test_concept_tenant_isolation(test_db: psycopg.Connection[Any]) -> None:
    backend = _backend(test_db)
    doc = _seed_manual_doc(test_db, external_id="n1", content="stripe phoenix")
    extractor = FakeExtractor(
        default=[_concept("stripe", "tool"), _concept("phoenix", "project")]
    )

    reconcile_document(
        test_db, doc, backend=backend, config=_ccfg("tenant-a"), extractor=extractor
    )

    assert _concept_keys(test_db, "tenant-a") == {"stripe", "phoenix"}
    assert _concept_keys(test_db, "tenant-b") == set()
    assert _age_concept_count(test_db, "tenant-a") == 2
    assert _age_concept_count(test_db, "tenant-b") == 0

    # Removing the doc from tenant-b (where it was never indexed) is a no-op for
    # tenant-a.
    remove_document(test_db, doc, backend=backend, config=_ccfg("tenant-b"))
    assert _concept_keys(test_db, "tenant-a") == {"stripe", "phoenix"}
    assert _age_concept_count(test_db, "tenant-a") == 2


# --------------------------------------------------------------------------- #
# 8. Person + concept coexistence (no MENTIONED_IN clobbering)
# --------------------------------------------------------------------------- #
def test_person_and_concept_aspects_coexist(
    test_db: psycopg.Connection[Any],
) -> None:
    """A doc with BOTH persons and concepts holds both in one combined graph."""
    backend = _backend(test_db)
    _seed_directory(test_db, [("alice", "alice@x.com"), ("bob", "bob@x.com")])
    doc = _seed_gmail_doc(
        test_db,
        external_id="m1",
        participants=[("alice", "alice@x.com"), ("bob", "bob@x.com")],
        content="stripe phoenix",
    )
    extractor = FakeExtractor(
        default=[_concept("stripe", "tool"), _concept("phoenix", "project")]
    )

    result = reconcile_document(
        test_db, doc, backend=backend, config=_ccfg(), extractor=extractor
    )

    assert result.person_count == 2
    assert result.concept_count == 2
    # 4 entities total (2 persons + 2 concepts); 4 mentions; 2 contributions
    # (alice-bob person pair + stripe-phoenix concept pair — never crossed).
    assert _person_keys(test_db, "default") == {"alice", "bob"}
    assert _concept_keys(test_db, "default") == {"stripe", "phoenix"}
    assert _mention_count(test_db, "default") == 4
    assert _contribution_count(test_db, "default") == 2
    # AGE: combined MENTIONED_IN = 4 (one Document vertex, 4 entity→doc edges);
    # 4 entity vertices; 2 CO_OCCURS edges.
    assert _age_entity_count(test_db, "default") == 4
    assert _age_concept_count(test_db, "default") == 2
    assert _age_mentioned_in_count(test_db, "default") == 4
    assert _age_cooccur_count(test_db, "default") == 2
    assert _age_document_count(test_db, "default") == 1


def test_person_only_reconcile_does_not_clobber_existing_concepts(
    test_db: psycopg.Connection[Any],
) -> None:
    """A later person-only reconcile (concepts fresh) preserves concept edges."""
    backend = _backend(test_db)
    _seed_directory(test_db, [("alice", "alice@x.com"), ("bob", "bob@x.com")])
    doc = _seed_gmail_doc(
        test_db,
        external_id="m1",
        participants=[("alice", "alice@x.com")],
        content="stripe phoenix",
    )
    extractor = FakeExtractor(
        default=[_concept("stripe", "tool"), _concept("phoenix", "project")]
    )
    # First pass: both aspects (1 person, 2 concepts).
    reconcile_document(
        test_db, doc, backend=backend, config=_ccfg(), extractor=extractor
    )
    assert extractor.calls == 1
    assert _age_concept_count(test_db, "default") == 2
    assert _age_mentioned_in_count(test_db, "default") == 3  # 1 person + 2 concept

    # Edit ONLY the participants (add bob) — content unchanged, so the concept
    # watermark stays fresh: person aspect re-runs, concept aspect skips.
    test_db.execute(
        "UPDATE documents SET metadata = metadata || %s::jsonb WHERE id = %s",
        (
            json.dumps({"from": "alice <alice@x.com>", "to": "bob <bob@x.com>"}),
            doc,
        ),
    )
    result = reconcile_document(
        test_db, doc, backend=backend, config=_ccfg(), extractor=extractor
    )

    assert result.skipped is False
    # No re-extraction (concept watermark unchanged); concepts preserved.
    assert extractor.calls == 1
    assert result.person_count == 2
    assert _person_keys(test_db, "default") == {"alice", "bob"}
    assert _concept_keys(test_db, "default") == {"stripe", "phoenix"}
    # Combined MENTIONED_IN rebuilt intact: 2 persons + 2 concepts = 4 (the
    # concept edges were NOT clobbered by the person-only rewrite).
    assert _age_mentioned_in_count(test_db, "default") == 4
    assert _age_concept_count(test_db, "default") == 2


# --------------------------------------------------------------------------- #
# 9. Default-OFF + guard
# --------------------------------------------------------------------------- #
def test_concepts_disabled_ignores_extractor(
    test_db: psycopg.Connection[Any],
) -> None:
    """With concepts_enabled=False, an injected extractor is never used."""
    backend = _backend(test_db)
    doc = _seed_manual_doc(test_db, external_id="n1", content="stripe phoenix")
    extractor = FakeExtractor(default=[_concept("stripe", "tool")])

    result = reconcile_document(
        test_db,
        doc,
        backend=backend,
        config=ReconcileConfig(generic_df_ratio=_NO_SUPPRESS),  # concepts off
        extractor=extractor,
    )

    assert result.concept_count == 0
    assert extractor.calls == 0
    assert _concept_keys(test_db, "default") == set()
    assert _watermark_count(test_db, "default", CONCEPTS_ASPECT) == 0
    # People aspect still wrote its watermark.
    assert _watermark_count(test_db, "default", "people") == 1


def test_concepts_enabled_without_extractor_raises(
    test_db: psycopg.Connection[Any],
) -> None:
    backend = _backend(test_db)
    doc = _seed_manual_doc(test_db, external_id="n1")
    with pytest.raises(GraphReconcileError, match="no EntityExtractor"):
        reconcile_document(
            test_db, doc, backend=backend, config=_ccfg(), extractor=None
        )


def test_concept_extraction_empty_still_watermarks(
    test_db: psycopg.Connection[Any],
) -> None:
    """An empty extraction (e.g. Ollama down) writes the watermark, 0 concepts."""
    backend = _backend(test_db)
    doc = _seed_manual_doc(test_db, external_id="n1", content="body")
    extractor = FakeExtractor(default=[])  # models a no-result / Ollama-down run

    result = reconcile_document(
        test_db, doc, backend=backend, config=_ccfg(), extractor=extractor
    )

    assert result.concept_count == 0
    assert _concept_keys(test_db, "default") == set()
    # Watermark IS written (idempotent for a genuinely concept-less doc); recovery
    # for a transient outage is `brain graphrag build --concepts --force`.
    assert _watermark_count(test_db, "default", CONCEPTS_ASPECT) == 1
    # A second run skips (no re-extraction).
    again = reconcile_document(
        test_db, doc, backend=backend, config=_ccfg(), extractor=extractor
    )
    assert again.skipped is True
    assert extractor.calls == 1


# --------------------------------------------------------------------------- #
# 10. CLI surfaces — --concepts + default-OFF
# --------------------------------------------------------------------------- #
def test_cli_build_concepts_flag(
    test_db: psycopg.Connection[Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    """`brain graphrag build --backfill --concepts` runs the concept aspect.

    The CLI builds its extractor via ``make_extractor`` (no DI seam at the CLI
    boundary), so we swap the FACTORY with a fake via ``monkeypatch.setattr`` — a
    standard test double (CLAUDE.md rule 13), not production monkey-patching.
    """
    _seed_manual_doc(test_db, external_id="n1", content="stripe phoenix")
    _seed_manual_doc(test_db, external_id="n2", content="stripe datadog")

    fake = FakeExtractor(
        by_marker={
            "stripe phoenix": [
                _concept("stripe", "tool"),
                _concept("phoenix", "project"),
            ],
            "stripe datadog": [
                _concept("stripe", "tool"),
                _concept("datadog", "tool"),
            ],
        }
    )
    monkeypatch.setattr(
        "brain.graph_rag.extract.make_extractor", lambda cfg: fake
    )
    monkeypatch.setenv("DATABASE_URL", TEST_DATABASE_URL)
    monkeypatch.setenv("BRAIN_GRAPH_GENERIC_DF", "1.0")

    res = CliRunner().invoke(app, ["graphrag", "build", "--backfill", "--concepts"])
    assert res.exit_code == 0, res.output
    assert "people + concepts aspect" in res.output
    assert "graphrag build: 2 processed" in res.output
    assert _concept_keys(test_db, "default") == {"stripe", "phoenix", "datadog"}
    assert _age_concept_count(test_db, "default") == 3
    assert _watermark_count(test_db, "default", CONCEPTS_ASPECT) == 2


def test_cli_build_default_off_skips_concepts(
    test_db: psycopg.Connection[Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    """Without --concepts (and env off), the build is person-only."""
    _seed_manual_doc(test_db, external_id="n1", content="stripe phoenix")
    monkeypatch.setenv("DATABASE_URL", TEST_DATABASE_URL)
    monkeypatch.setenv("BRAIN_GRAPH_GENERIC_DF", "1.0")
    # The concept env gate stays off via the session-autouse
    # _force_graph_flags_default fixture; a delenv here would instead let the
    # local .env (which the concept backfill sets BRAIN_GRAPH_CONCEPTS=true in)
    # re-inject the flag and silently flip this test.

    res = CliRunner().invoke(app, ["graphrag", "build", "--backfill"])
    assert res.exit_code == 0, res.output
    assert "people aspect" in res.output
    assert "concepts" not in res.output
    assert _concept_keys(test_db, "default") == set()
    assert _watermark_count(test_db, "default", CONCEPTS_ASPECT) == 0


def test_cli_build_env_gate_includes_concepts(
    test_db: psycopg.Connection[Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    """BRAIN_GRAPH_CONCEPTS=true includes concepts even without the flag."""
    _seed_manual_doc(test_db, external_id="n1", content="stripe phoenix")
    fake = FakeExtractor(
        default=[_concept("stripe", "tool"), _concept("phoenix", "project")]
    )
    monkeypatch.setattr(
        "brain.graph_rag.extract.make_extractor", lambda cfg: fake
    )
    monkeypatch.setenv("DATABASE_URL", TEST_DATABASE_URL)
    monkeypatch.setenv("BRAIN_GRAPH_GENERIC_DF", "1.0")
    monkeypatch.setenv("BRAIN_GRAPH_CONCEPTS", "true")

    res = CliRunner().invoke(app, ["graphrag", "build", "--backfill"])
    assert res.exit_code == 0, res.output
    assert "people + concepts aspect" in res.output
    assert _concept_keys(test_db, "default") == {"stripe", "phoenix"}


# --------------------------------------------------------------------------- #
# 11. Unit — pure concepts.py helpers
# --------------------------------------------------------------------------- #
def test_concept_mention_source_format() -> None:
    assert concept_mention_source("llama3.1:8b@concepts-v1") == (
        "extractor:llama3.1:8b@concepts-v1"
    )


def test_concept_inputs_hash_is_config_only_and_stable() -> None:
    # Stable for identical config; differs when window or cap changes.
    base = concept_inputs_hash(3, 40)
    assert base == concept_inputs_hash(3, 40)
    assert base != concept_inputs_hash(4, 40)
    assert base != concept_inputs_hash(3, 41)
    assert base != concept_inputs_hash(3, None)
