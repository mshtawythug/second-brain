"""Tests for ``brain.graph_rag.cross_type`` — cross-document type collapse (Bug A).

The catalog uniqueness key is ``(tenant_id, entity_type, canonical_key)``
(migration 012), so the SAME concept named under a different ``entity_type`` in
different documents lands as separate ``graph_entities`` rows
(``acmeplatform`` = org + project + tool). ``extract._dedupe_cross_type``
collapses this *within* one document; this module collapses it *across*
documents by generating merge rules (lower-precedence → highest-precedence per
``extract._TYPE_PRECEDENCE``, concept types only) and applying them through the
shipped, tested :func:`brain.graph_rag.aliases.merge_aliases` machinery — no
schema migration.

Two layers, both against the AGE test instance (port 5434, ``test_db``):

* **Live-AGE integration** — cross-doc collapse (entity/mention/contribution/
  doc_count accumulation), idempotency, the incremental ``GraphSyncer`` (sync.py)
  path, person isolation, and AGE-vertex consistency after the merge.
* **Unit** — rule generation precedence + validity over a directly-seeded
  catalog (relational only).

All entity names are synthetic (AcmePlatform / Sidekick / Northwind); no PII.
The schema + AGE graph are reset per test by the ``test_db`` fixture.
"""
from __future__ import annotations

import hashlib
import json
import uuid
from collections.abc import Mapping
from typing import Any

import psycopg

from brain.db import DEFAULT_GRAPH_NAME
from brain.graph_rag.aliases import _validate_alias_graph
from brain.graph_rag.backends import AgeBackend
from brain.graph_rag.cross_type import (
    _best_surface_form,
    collapse_cross_type_concepts,
    generate_cross_type_collapse_rules,
)
from brain.graph_rag.extract import ExtractedEntity
from brain.graph_rag.reconcile import ReconcileConfig, reconcile_document
from brain.graph_rag.schema import GraphEntity
from brain.graph_rag.sync import GraphSyncer

_NO_SUPPRESS = 1.0
_CONCEPT_TYPES = ("topic", "project", "org", "tool")


# --------------------------------------------------------------------------- #
# Fake extractor (DI seam — no live Ollama, no patching)
# --------------------------------------------------------------------------- #
class FakeExtractor:
    """Deterministic :class:`EntityExtractor` returning canned entities by marker."""

    def __init__(
        self,
        by_marker: dict[str, list[ExtractedEntity]],
        *,
        version: str = "fake-model@concepts-v6",
    ) -> None:
        self._by_marker = by_marker
        self._version = version

    @property
    def version(self) -> str:
        return self._version

    def extract(self, text: str) -> list[ExtractedEntity]:
        for marker, entities in self._by_marker.items():
            if marker in text:
                return list(entities)
        return []


def _concept(
    canonical_key: str,
    entity_type: str,
    *,
    positions: tuple[int, ...] = (0,),
    display_name: str | None = None,
) -> ExtractedEntity:
    return ExtractedEntity(
        entity_type=entity_type,
        canonical_key=canonical_key,
        display_name=display_name or canonical_key,
        positions=positions,
        mention_count=max(1, len(positions)),
    )


def _ccfg(tenant_id: str = "default", **kwargs: Any) -> ReconcileConfig:
    return ReconcileConfig(
        tenant_id=tenant_id,
        generic_df_ratio=_NO_SUPPRESS,
        concepts_enabled=True,
        **kwargs,
    )


# --------------------------------------------------------------------------- #
# Seeding helpers
# --------------------------------------------------------------------------- #
def _backend(conn: psycopg.Connection[Any]) -> AgeBackend:
    backend = AgeBackend()
    backend.bootstrap(conn)
    return backend


def _seed_manual_doc(
    conn: psycopg.Connection[Any], *, external_id: str, content: str
) -> str:
    """Insert a manual note (no participants → zero persons); return its id."""
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
        INSERT INTO documents (source_id, title, content, content_hash, content_type)
        VALUES (%s, %s, %s, %s, 'note')
        RETURNING id::text
        """,
        (src_row[0], external_id, salted, content_hash),
    ).fetchone()
    assert doc_row is not None
    return str(doc_row[0])


def _seed_entity(
    conn: psycopg.Connection[Any],
    entity_type: str,
    canonical_key: str,
    *,
    tenant_id: str = "default",
) -> str:
    """Insert a ``graph_entities`` row directly; return id::text."""
    row = conn.execute(
        """
        INSERT INTO graph_entities (tenant_id, entity_type, name, canonical_key)
        VALUES (%s, %s, %s, %s)
        RETURNING id::text
        """,
        (tenant_id, entity_type, canonical_key.title(), canonical_key),
    ).fetchone()
    assert row is not None
    return str(row[0])


def _seed_mention(
    conn: psycopg.Connection[Any],
    entity_id: str,
    document_id: str,
    *,
    tenant_id: str = "default",
    source: str = "extractor:fake@concepts-v6",
) -> None:
    conn.execute(
        """
        INSERT INTO graph_entity_mentions
            (tenant_id, entity_id, document_id, mention_count, source)
        VALUES (%s, %s, %s, 1, %s)
        """,
        (tenant_id, entity_id, document_id, source),
    )


# --------------------------------------------------------------------------- #
# Relational assertions
# --------------------------------------------------------------------------- #
def _rows_for_key(
    conn: psycopg.Connection[Any], canonical_key: str, *, tenant: str = "default"
) -> list[tuple[str, str]]:
    """Return ``(entity_type, id)`` for every catalog row with ``canonical_key``."""
    rows = conn.execute(
        "SELECT entity_type, id::text FROM graph_entities "
        "WHERE tenant_id = %s AND canonical_key = %s",
        (tenant, canonical_key),
    ).fetchall()
    return [(str(r[0]), str(r[1])) for r in rows]


def _concept_rows_for_key(
    conn: psycopg.Connection[Any], canonical_key: str, *, tenant: str = "default"
) -> list[tuple[str, str]]:
    rows = conn.execute(
        "SELECT entity_type, id::text FROM graph_entities "
        "WHERE tenant_id = %s AND canonical_key = %s AND entity_type = ANY(%s)",
        (tenant, canonical_key, list(_CONCEPT_TYPES)),
    ).fetchall()
    return [(str(r[0]), str(r[1])) for r in rows]


def _mention_count(
    conn: psycopg.Connection[Any], entity_id: str, *, tenant: str = "default"
) -> int:
    row = conn.execute(
        "SELECT count(*) FROM graph_entity_mentions "
        "WHERE tenant_id = %s AND entity_id = %s",
        (tenant, entity_id),
    ).fetchone()
    assert row is not None
    return int(row[0])


def _doc_count(
    conn: psycopg.Connection[Any], entity_id: str, *, tenant: str = "default"
) -> int:
    row = conn.execute(
        "SELECT doc_count FROM graph_entities WHERE tenant_id = %s AND id = %s",
        (tenant, entity_id),
    ).fetchone()
    assert row is not None
    return int(row[0])


def _contribution_count_touching(
    conn: psycopg.Connection[Any], entity_id: str, *, tenant: str = "default"
) -> int:
    row = conn.execute(
        "SELECT count(*) FROM graph_edge_contributions "
        "WHERE tenant_id = %s AND (src_id = %s OR dst_id = %s)",
        (tenant, entity_id, entity_id),
    ).fetchone()
    assert row is not None
    return int(row[0])


# --------------------------------------------------------------------------- #
# AGE assertions (independent raw Cypher)
# --------------------------------------------------------------------------- #
def _cypher(
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


def _age_entity_uuids(conn: psycopg.Connection[Any], tenant: str) -> set[str]:
    rows = _cypher(
        conn, "MATCH (e:Entity {tenant_id: $t}) RETURN e.entity_uuid", {"t": tenant}
    )
    return {str(r[0]).strip('"') for r in rows}


def _age_entity_name(
    conn: psycopg.Connection[Any], tenant: str, entity_uuid: str
) -> str | None:
    rows = _cypher(
        conn,
        "MATCH (e:Entity {entity_uuid: $u, tenant_id: $t}) RETURN e.name",
        {"u": entity_uuid, "t": tenant},
    )
    return None if not rows else str(rows[0][0]).strip('"')


def _relational_name(
    conn: psycopg.Connection[Any], entity_id: str, *, tenant: str = "default"
) -> str:
    row = conn.execute(
        "SELECT name FROM graph_entities WHERE tenant_id = %s AND id = %s",
        (tenant, entity_id),
    ).fetchone()
    assert row is not None
    return str(row[0])


# --------------------------------------------------------------------------- #
# 1. Cross-doc collapse — accumulation of entity / mention / contribution / dc
# --------------------------------------------------------------------------- #
def test_cross_doc_collapse_merges_to_precedence_winner(
    test_db: psycopg.Connection[Any],
) -> None:
    """Two docs name one concept under different types → ONE row at the winner.

    Doc A: ``acmeplatform`` as PROJECT + ``sidekick`` as tool.
    Doc B: ``acmeplatform`` as ORG     + ``sidekick`` as tool.
    org outranks project, so after collapse ``acmeplatform`` is a single ORG row
    carrying both docs' mentions/contributions; the project row is gone.
    """
    # Arrange
    backend = _backend(test_db)
    doc_a = _seed_manual_doc(test_db, external_id="cdc-a", content="alpha body")
    doc_b = _seed_manual_doc(test_db, external_id="cdc-b", content="beta body")
    extractor = FakeExtractor(
        {
            "alpha": [
                _concept("acmeplatform", "project", positions=(0,)),
                _concept("sidekick", "tool", positions=(1,)),
            ],
            "beta": [
                _concept("acmeplatform", "org", positions=(0,)),
                _concept("sidekick", "tool", positions=(1,)),
            ],
        }
    )
    reconcile_document(test_db, doc_a, backend=backend, config=_ccfg(), extractor=extractor)
    reconcile_document(test_db, doc_b, backend=backend, config=_ccfg(), extractor=extractor)

    # Pre-collapse: project + org rows BOTH exist for the same key (the bug).
    pre = dict(_concept_rows_for_key(test_db, "acmeplatform"))
    assert set(pre) == {"project", "org"}
    project_id = pre["project"]

    # Act
    result = collapse_cross_type_concepts(
        test_db, "default", backend, config=_ccfg()
    )

    # Assert — exactly one row, at the precedence winner (org).
    after = _concept_rows_for_key(test_db, "acmeplatform")
    assert len(after) == 1
    win_type, win_id = after[0]
    assert win_type == "org"
    assert result.rules_total == 1
    assert result.rules_applied == 1

    # Mentions + doc_count accumulate onto the winner (both docs).
    assert _mention_count(test_db, win_id) == 2
    assert _doc_count(test_db, win_id) == 2
    # Contributions (acmeplatform, sidekick) from BOTH docs now touch the winner.
    assert _contribution_count_touching(test_db, win_id) == 2

    # AGE: the project vertex is detached, the org vertex remains.
    uuids = _age_entity_uuids(test_db, "default")
    assert project_id not in uuids
    assert win_id in uuids


def test_collapse_preserves_winner_display_name(
    test_db: psycopg.Connection[Any],
) -> None:
    """The merged target KEEPS its extractor surface name (camelCase intact).

    Regression: ``merge_aliases``'s find-or-create rewrites the target's ``name``
    to ``humanize_person_name(canonical_key)`` → ``"Acmeplatform"``. The collapse
    must restore the winner's real display name (``"AcmePlatform"``) both
    relationally and on the AGE vertex.
    """
    # Arrange — both docs spell the concept "AcmePlatform" (key 'acmeplatform').
    backend = _backend(test_db)
    doc_a = _seed_manual_doc(test_db, external_id="name-a", content="alpha body")
    doc_b = _seed_manual_doc(test_db, external_id="name-b", content="beta body")
    extractor = FakeExtractor(
        {
            "alpha": [_concept("acmeplatform", "project", display_name="AcmePlatform")],
            "beta": [_concept("acmeplatform", "org", display_name="AcmePlatform")],
        }
    )
    reconcile_document(test_db, doc_a, backend=backend, config=_ccfg(), extractor=extractor)
    reconcile_document(test_db, doc_b, backend=backend, config=_ccfg(), extractor=extractor)

    # Act
    collapse_cross_type_concepts(test_db, "default", backend, config=_ccfg())

    # Assert — single org winner, name preserved (NOT the humanized "Acmeplatform").
    after = _concept_rows_for_key(test_db, "acmeplatform")
    assert len(after) == 1
    win_type, win_id = after[0]
    assert win_type == "org"
    assert _relational_name(test_db, win_id) == "AcmePlatform"
    assert _age_entity_name(test_db, "default", win_id) == "AcmePlatform"


def test_best_surface_form_heuristic() -> None:
    """F1 best-name heuristic is a deterministic total order over the variants."""
    # Empty input → empty string (caller guards).
    assert _best_surface_form([]) == ""
    # Rule 1: a mixed-case form beats an ALL-CAPS "shout" even with more docs +
    # uppercase ("Neon" over "NEON", "DACs" over "DACS").
    assert _best_surface_form([("Neon", 1), ("NEON", 9)]) == "Neon"
    assert _best_surface_form([("DACs", 1), ("DACS", 5)]) == "DACs"
    # …but a lone all-caps acronym (no mixed-case sibling) is kept.
    assert _best_surface_form([("NFPA", 4)]) == "NFPA"
    # Rule 2a: a branded form beats all-lowercase even with FEWER docs.
    assert _best_surface_form([("acmeplatform", 9), ("AcmePlatform", 1)]) == "AcmePlatform"
    # Rule 2b: MORE uppercase letters wins — a better-cased branded form beats a
    # worse one (the AI::Client vs Ai::Client regression the boolean rule caused).
    assert _best_surface_form([("Ai::Client", 5), ("AI::Client", 1)]) == "AI::Client"
    # Rule 3: equal casing → higher doc_count wins EVEN OVER a longer name
    # (isolates doc_count > length: the shorter form has more docs and must win).
    assert _best_surface_form([("AcmeOne", 9), ("AcmeLonger", 1)]) == "AcmeOne"
    # Rule 4: equal casing + doc_count → the longer name wins.
    assert _best_surface_form([("Acme", 2), ("Acmee", 2)]) == "Acmee"
    # Rule 5: equal casing + doc_count + length → lexicographically smallest.
    assert _best_surface_form([("AcmeB", 2), ("AcmeA", 2)]) == "AcmeA"
    # All-lowercase: casing + uppercase tie (0), so doc_count decides.
    assert _best_surface_form([("acme", 1), ("acmecorp", 3)]) == "acmecorp"


def test_collapse_picks_best_surface_form_over_lowercase_winner(
    test_db: psycopg.Connection[Any],
) -> None:
    """F1: the survivor takes the BEST variant name, not the winning TYPE's name.

    The ORG variant is all-lowercase ``"acmeplatform"`` and wins type precedence;
    the PROJECT variant is the branded ``"AcmePlatform"``. The merged ORG survivor
    must be named ``"AcmePlatform"`` (the branded form) — relationally AND on AGE —
    even though the lowercase org row won precedence.
    """
    # Arrange — same key 'acmeplatform', different surface forms per type.
    backend = _backend(test_db)
    doc_a = _seed_manual_doc(test_db, external_id="best-a", content="alpha body")
    doc_b = _seed_manual_doc(test_db, external_id="best-b", content="beta body")
    extractor = FakeExtractor(
        {
            "alpha": [_concept("acmeplatform", "org", display_name="acmeplatform")],
            "beta": [_concept("acmeplatform", "project", display_name="AcmePlatform")],
        }
    )
    reconcile_document(test_db, doc_a, backend=backend, config=_ccfg(), extractor=extractor)
    reconcile_document(test_db, doc_b, backend=backend, config=_ccfg(), extractor=extractor)

    # Act
    collapse_cross_type_concepts(test_db, "default", backend, config=_ccfg())

    # Assert — single org survivor, BEST (branded) name on both surfaces.
    after = _concept_rows_for_key(test_db, "acmeplatform")
    assert len(after) == 1
    win_type, win_id = after[0]
    assert win_type == "org"
    assert _relational_name(test_db, win_id) == "AcmePlatform"
    assert _age_entity_name(test_db, "default", win_id) == "AcmePlatform"


# --------------------------------------------------------------------------- #
# 2. Idempotency — second collapse is a no-op; generated rules are valid
# --------------------------------------------------------------------------- #
def test_collapse_is_idempotent_and_rules_validate(
    test_db: psycopg.Connection[Any],
) -> None:
    """A second collapse finds no >1-type key (empty rules → no-op); the
    generated rules pass the chain/cycle/dup-source validation."""
    # Arrange — three types for one key across three docs.
    backend = _backend(test_db)
    triples = (
        ("topic", "tword", "id-top"),
        ("tool", "uword", "id-tool"),
        ("project", "pword", "id-proj"),
    )
    for etype, marker, ext_id in triples:
        doc = _seed_manual_doc(test_db, external_id=ext_id, content=f"{marker} body")
        extractor = FakeExtractor({marker: [_concept("acmeplatform", etype)]})
        reconcile_document(
            test_db, doc, backend=backend, config=_ccfg(), extractor=extractor
        )

    # Generated rules are well-formed (no chain/cycle/dup-source).
    rules = generate_cross_type_collapse_rules(test_db, "default")
    assert len(rules) == 2  # project + topic + tool → 2 sources collapse into 1 winner
    _validate_alias_graph(rules)  # must not raise
    # All rules target the SAME winning type (project outranks tool + topic).
    assert {r.to_type for r in rules} == {"project"}
    assert {r.from_type for r in rules} == {"tool", "topic"}

    # Act — first collapse merges, second is a no-op.
    first = collapse_cross_type_concepts(test_db, "default", backend, config=_ccfg())
    second = collapse_cross_type_concepts(test_db, "default", backend, config=_ccfg())

    # Assert
    assert first.rules_total == 2
    assert first.rules_applied == 2
    assert second.rules_total == 0  # nothing left to collapse
    assert generate_cross_type_collapse_rules(test_db, "default") == []
    assert len(_concept_rows_for_key(test_db, "acmeplatform")) == 1


# --------------------------------------------------------------------------- #
# 3. Incremental sync — a doc synced AFTER an entity exists under another type
# --------------------------------------------------------------------------- #
def test_incremental_sync_collapses_after_second_doc(
    test_db: psycopg.Connection[Any],
) -> None:
    """The ``GraphSyncer`` (sync.py) path collapses cross-type fragments — the
    case easy to miss vs the build path."""
    # Arrange
    test_db.autocommit = True
    backend = _backend(test_db)
    extractor = FakeExtractor(
        {
            "alpha": [_concept("acmeplatform", "project")],
            "beta": [_concept("acmeplatform", "org")],
        }
    )
    syncer = GraphSyncer(_ccfg(), enabled=True, backend=backend, extractor=extractor)
    doc_a = _seed_manual_doc(test_db, external_id="sync-a", content="alpha body")
    doc_b = _seed_manual_doc(test_db, external_id="sync-b", content="beta body")

    # Act — sync doc A (project), then doc B (org). The second sync's collapse
    # hook merges the now-fragmented pair.
    syncer.reconcile(test_db, doc_a)
    assert set(dict(_concept_rows_for_key(test_db, "acmeplatform"))) == {"project"}
    syncer.reconcile(test_db, doc_b)

    # Assert — single org row after the incremental collapse.
    after = _concept_rows_for_key(test_db, "acmeplatform")
    assert len(after) == 1
    assert after[0][0] == "org"
    assert _mention_count(test_db, after[0][1]) == 2


class _RefreshCountingBackend(AgeBackend):
    """``AgeBackend`` that counts ``refresh_cooccur_edges`` calls.

    Lets a test assert that a clean single-doc sync's collapse hook pays NO extra
    whole-tenant refresh (the cost the ``rules_applied > 0`` gate avoids).
    """

    def __init__(self) -> None:
        super().__init__()
        self.refresh_calls = 0

    def refresh_cooccur_edges(
        self, conn: psycopg.Connection[Any], tenant_id: str
    ) -> int:
        self.refresh_calls += 1
        return super().refresh_cooccur_edges(conn, tenant_id)


def test_clean_sync_collapse_is_noop_no_extra_refresh(
    test_db: psycopg.Connection[Any],
) -> None:
    """A single-doc sync with no cross-type duplicates pays no collapse refresh.

    The collapse generates an empty rule set, so ``merge_aliases`` short-circuits
    before opening a transaction (no whole-tenant ``refresh_cooccur_edges``) — the
    common clean-sync path stays cheap.
    """
    # Arrange — one doc naming each concept under a SINGLE distinct type.
    test_db.autocommit = True
    backend = _RefreshCountingBackend()
    backend.bootstrap(test_db)
    extractor = FakeExtractor(
        {
            "alpha": [
                _concept("acmeplatform", "org", positions=(0,)),
                _concept("sidekick", "tool", positions=(1,)),
            ]
        }
    )
    syncer = GraphSyncer(_ccfg(), enabled=True, backend=backend, extractor=extractor)
    doc = _seed_manual_doc(test_db, external_id="clean-sync", content="alpha body")

    # Act — sync the doc (reconcile_document refreshes once; the collapse hook
    # must add ZERO further refreshes because nothing is fragmented).
    syncer.reconcile(test_db, doc)
    calls_after_sync = backend.refresh_calls

    # Assert — no cross-type fragment, so the collapse is a pure no-op.
    assert generate_cross_type_collapse_rules(test_db, "default") == []
    res = collapse_cross_type_concepts(test_db, "default", backend, config=_ccfg())
    assert res.rules_total == 0
    assert backend.refresh_calls == calls_after_sync  # collapse paid no refresh


# --------------------------------------------------------------------------- #
# 4. Person isolation — a person sharing a key with a concept is NOT merged
# --------------------------------------------------------------------------- #
def test_person_sharing_key_is_not_merged(
    test_db: psycopg.Connection[Any],
) -> None:
    """A ``person`` row sharing a canonical_key with concept rows stays intact —
    only the concept types collapse among themselves."""
    # Arrange — person + project + org all keyed 'acmeplatform' (synthetic).
    backend = _backend(test_db)
    doc = _seed_manual_doc(test_db, external_id="iso", content="iso body")
    person_id = _seed_entity(test_db, "person", "acmeplatform")
    project_id = _seed_entity(test_db, "project", "acmeplatform")
    org_id = _seed_entity(test_db, "org", "acmeplatform")
    _seed_mention(test_db, person_id, doc, source="people")
    _seed_mention(test_db, project_id, doc)
    _seed_mention(test_db, org_id, doc)
    # Provision AGE vertices so the merge's CO_OCCURS rebuild can bind endpoints.
    backend.upsert_entities(
        test_db,
        "default",
        [
            GraphEntity(id=person_id, entity_type="person", name="Acmeplatform",
                        canonical_key="acmeplatform", tenant_id="default"),
            GraphEntity(id=project_id, entity_type="project", name="Acmeplatform",
                        canonical_key="acmeplatform", tenant_id="default"),
            GraphEntity(id=org_id, entity_type="org", name="Acmeplatform",
                        canonical_key="acmeplatform", tenant_id="default"),
        ],
    )

    # Act
    result = collapse_cross_type_concepts(test_db, "default", backend, config=_ccfg())

    # Assert — only the two CONCEPT rows were in scope (project → org); the
    # person row was never a source or target.
    assert result.rules_total == 1
    rows = dict(_rows_for_key(test_db, "acmeplatform"))
    assert set(rows) == {"person", "org"}  # project collapsed, person untouched
    assert rows["person"] == person_id
    assert _mention_count(test_db, person_id) == 1  # person mention intact
    # The person vertex still exists in AGE.
    assert person_id in _age_entity_uuids(test_db, "default")


# --------------------------------------------------------------------------- #
# 5. Unit — rule generation over a directly-seeded catalog (relational only)
# --------------------------------------------------------------------------- #
def test_generate_rules_precedence_and_no_single_type(
    test_db: psycopg.Connection[Any],
) -> None:
    """org wins over project/tool/topic; a key under a single type yields no rule;
    distinct keys are independent."""
    # Arrange — 'acmeplatform' under all four types; 'northwind' under one type.
    for etype in _CONCEPT_TYPES:
        _seed_entity(test_db, etype, "acmeplatform")
    _seed_entity(test_db, "tool", "northwind")  # single type → no rule

    # Act
    rules = generate_cross_type_collapse_rules(test_db, "default")

    # Assert — three sources (project, tool, topic) all → org; nothing for northwind.
    assert all(r.to_type == "org" and r.to_key == "acmeplatform" for r in rules)
    assert {r.from_type for r in rules} == {"project", "tool", "topic"}
    assert all(r.from_key == "acmeplatform" for r in rules)
    assert not any(r.from_key == "northwind" for r in rules)
    _validate_alias_graph(rules)  # well-formed
