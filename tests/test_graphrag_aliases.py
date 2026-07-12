"""Tests for brain.graph_rag.aliases — alias rule loading + validation (C1).

C2 appends integration tests for ``apply_aliases`` exercised against the live
GraphRAG test database (port 5434, ``test_db`` fixture). The seeders are local
to this module — synthetic ``org``/``person`` entities only (no PII).
"""
from __future__ import annotations

import hashlib
import uuid
from pathlib import Path
from typing import Any

import psycopg
import pytest

from brain.errors import GraphReconcileError
from brain.graph_rag.aliases import AliasRule, apply_aliases, load_alias_rules

# ---------------------------------------------------------------------------
# Loader — happy path
# ---------------------------------------------------------------------------


def test_load_alias_rules_from_path(tmp_path: Path) -> None:
    """Arrange: valid YAML with two rules. Act: load. Assert: both parsed correctly."""
    # Arrange
    p = tmp_path / "a.yml"
    p.write_text(
        "rules:\n"
        "  - from: {type: org, key: acme}\n"
        "    to:   {type: org, key: acme corp}\n"
        "  - from: {type: topic, key: sam}\n"
        "    to:   {type: person, key: sam rivera}\n"
    )
    # Act
    rules = load_alias_rules(path=p)
    # Assert
    assert AliasRule("org", "acme", "org", "acme corp") in rules
    assert rules[1].to_type == "person"


def test_load_alias_rules_normalises_keys(tmp_path: Path) -> None:
    """Arrange: keys with mixed case and extra whitespace. Act: load. Assert: normalised."""
    # Arrange
    p = tmp_path / "a.yml"
    p.write_text(
        "rules:\n"
        "  - from: {type: ORG, key: '  Acme  Corp  '}\n"
        "    to:   {type: org, key: Northwind}\n"
    )
    # Act
    rules = load_alias_rules(path=p)
    # Assert
    assert rules[0].from_type == "org"
    assert rules[0].from_key == "acme corp"
    assert rules[0].to_key == "northwind"


def test_load_alias_rules_missing_file_returns_empty(tmp_path: Path) -> None:
    """Arrange: path does not exist. Act: load. Assert: empty list (feature opt-in)."""
    assert load_alias_rules(path=tmp_path / "nope.yml") == []


def test_load_alias_rules_none_path_returns_empty() -> None:
    """Arrange: path=None. Act: load. Assert: empty list."""
    assert load_alias_rules(path=None) == []


def test_load_alias_rules_empty_rules_section(tmp_path: Path) -> None:
    """Arrange: YAML with empty rules list. Act: load. Assert: empty list returned."""
    p = tmp_path / "a.yml"
    p.write_text("rules: []\n")
    assert load_alias_rules(path=p) == []


def test_load_alias_rules_missing_rules_key(tmp_path: Path) -> None:
    """Arrange: YAML with no 'rules' key. Act: load. Assert: empty list returned."""
    p = tmp_path / "a.yml"
    p.write_text("# just a comment\n")
    assert load_alias_rules(path=p) == []


# ---------------------------------------------------------------------------
# Validation — self-merge rejection
# ---------------------------------------------------------------------------


def test_load_alias_rules_rejects_self_merge(tmp_path: Path) -> None:
    """Arrange: rule merges an entity into itself. Act: load. Assert: GraphReconcileError."""
    # Arrange
    p = tmp_path / "a.yml"
    p.write_text("rules:\n  - from: {type: org, key: x}\n    to: {type: org, key: x}\n")
    # Act + Assert
    with pytest.raises(GraphReconcileError):
        load_alias_rules(path=p)


# ---------------------------------------------------------------------------
# Validation — invalid type rejection
# ---------------------------------------------------------------------------


def test_load_alias_rules_rejects_invalid_from_type(tmp_path: Path) -> None:
    """Arrange: from.type is not a valid entity type. Act: load. Assert: GraphReconcileError."""
    p = tmp_path / "a.yml"
    p.write_text("rules:\n  - from: {type: banana, key: x}\n    to: {type: org, key: y}\n")
    with pytest.raises(GraphReconcileError):
        load_alias_rules(path=p)


def test_load_alias_rules_rejects_invalid_to_type(tmp_path: Path) -> None:
    """Arrange: to.type is not a valid entity type. Act: load. Assert: GraphReconcileError."""
    p = tmp_path / "a.yml"
    p.write_text("rules:\n  - from: {type: org, key: x}\n    to: {type: banana, key: y}\n")
    with pytest.raises(GraphReconcileError):
        load_alias_rules(path=p)


# ---------------------------------------------------------------------------
# Validation — duplicate source rejection
# ---------------------------------------------------------------------------


def test_load_alias_rules_rejects_duplicate_source(tmp_path: Path) -> None:
    """Arrange: same (type, key) appears as source in two rules. Act: load. Assert: error."""
    # Arrange
    p = tmp_path / "a.yml"
    p.write_text(
        "rules:\n"
        "  - from: {type: org, key: a}\n    to: {type: org, key: b}\n"
        "  - from: {type: org, key: a}\n    to: {type: org, key: c}\n"
    )
    # Act + Assert
    with pytest.raises(GraphReconcileError):
        load_alias_rules(path=p)


# ---------------------------------------------------------------------------
# Validation — chain/cycle rejection
# ---------------------------------------------------------------------------


def test_load_alias_rules_rejects_chain(tmp_path: Path) -> None:
    """Arrange: A→B and B→C (chain). Act: load. Assert: GraphReconcileError."""
    # Arrange
    p = tmp_path / "a.yml"
    p.write_text(
        "rules:\n"
        "  - from: {type: org, key: a}\n    to: {type: org, key: b}\n"
        "  - from: {type: org, key: b}\n    to: {type: org, key: c}\n"
    )
    # Act + Assert
    with pytest.raises(GraphReconcileError):
        load_alias_rules(path=p)


def test_load_alias_rules_rejects_cycle(tmp_path: Path) -> None:
    """Arrange: A→B and B→A (cycle). Act: load. Assert: GraphReconcileError."""
    # Arrange
    p = tmp_path / "a.yml"
    p.write_text(
        "rules:\n"
        "  - from: {type: org, key: a}\n    to: {type: org, key: b}\n"
        "  - from: {type: org, key: b}\n    to: {type: org, key: a}\n"
    )
    # Act + Assert
    with pytest.raises(GraphReconcileError):
        load_alias_rules(path=p)


# ---------------------------------------------------------------------------
# Cross-type rules are valid (topic→person, etc.)
# ---------------------------------------------------------------------------


def test_load_alias_rules_cross_type_allowed(tmp_path: Path) -> None:
    """Arrange: rule merges a topic into a person (different types). Act: load. Assert: parsed."""
    p = tmp_path / "a.yml"
    p.write_text(
        "rules:\n"
        "  - from: {type: topic, key: sam}\n"
        "    to:   {type: person, key: sam rivera}\n"
    )
    rules = load_alias_rules(path=p)
    assert len(rules) == 1
    assert rules[0].from_type == "topic"
    assert rules[0].to_type == "person"


# ===========================================================================
# C2 — apply_aliases (real-DB integration; reuses the `test_db` fixture)
#
# F2 invariant: ``apply_aliases`` re-points mentions/contributions only and
# never DELETEs ``graph_entities`` rows. The orphan source is left
# zero-mention for ``refresh_aggregates``'s GC (called by C3) to delete +
# detach. All data here is synthetic.
# ===========================================================================


def _make_doc(conn: psycopg.Connection[Any]) -> str:
    """Insert a minimal ``documents`` row (no source) and return its id::text.

    Used so the ``graph_entity_mentions``/``graph_edge_contributions`` FKs to
    ``documents(id)`` have a real target — content is salted with a uuid so
    the global ``content_hash`` UNIQUE never collides across tests.
    """
    salted = f"alias-test\n<!-- {uuid.uuid4()} -->"
    content_hash = hashlib.sha256(salted.encode("utf-8")).hexdigest()
    row = conn.execute(
        """
        INSERT INTO documents
            (title, content, content_hash, content_type)
        VALUES (%s, %s, %s, 'note')
        RETURNING id::text
        """,
        (f"alias-test-{content_hash[:8]}", salted, content_hash),
    ).fetchone()
    assert row is not None
    return str(row[0])


def _seed_entity(
    conn: psycopg.Connection[Any],
    entity_type: str,
    canonical_key: str,
    *,
    tenant_id: str = "default",
    doc_count: int = 0,
) -> str:
    """Insert a ``graph_entities`` row and return its id::text.

    ``doc_count`` is an authoritative-on-write field for tests only — production
    code derives it via ``refresh_aggregates``; here it just lets us assert the
    field is left untouched by ``apply_aliases``.
    """
    row = conn.execute(
        """
        INSERT INTO graph_entities
            (tenant_id, entity_type, name, canonical_key, doc_count)
        VALUES (%s, %s, %s, %s, %s)
        RETURNING id::text
        """,
        (tenant_id, entity_type, canonical_key.title(), canonical_key, doc_count),
    ).fetchone()
    assert row is not None
    return str(row[0])


def _seed_mention(
    conn: psycopg.Connection[Any],
    entity_type: str,
    canonical_key: str,
    *,
    document_id: str,
    tenant_id: str = "default",
    mention_count: int = 1,
    source: str = "people",
) -> None:
    """Insert a ``graph_entity_mentions`` row keyed on a seeded entity."""
    eid = _entity_id(conn, entity_type, canonical_key, tenant_id=tenant_id)
    assert eid is not None, f"no entity for {entity_type}:{canonical_key}"
    conn.execute(
        """
        INSERT INTO graph_entity_mentions
            (tenant_id, entity_id, document_id, mention_count, source)
        VALUES (%s, %s, %s, %s, %s)
        """,
        (tenant_id, eid, document_id, mention_count, source),
    )


def _seed_contribution(
    conn: psycopg.Connection[Any],
    a_type: str,
    a_key: str,
    b_type: str,
    b_key: str,
    *,
    document_id: str,
    tenant_id: str = "default",
    cooccur_count: int = 1,
) -> None:
    """Insert a canonical (src_id < dst_id) row into ``graph_edge_contributions``."""
    a = _entity_id(conn, a_type, a_key, tenant_id=tenant_id)
    b = _entity_id(conn, b_type, b_key, tenant_id=tenant_id)
    assert a is not None and b is not None
    src, dst = (a, b) if a < b else (b, a)
    conn.execute(
        """
        INSERT INTO graph_edge_contributions
            (tenant_id, document_id, src_id, dst_id, cooccur_count)
        VALUES (%s, %s, %s, %s, %s)
        """,
        (tenant_id, document_id, src, dst, cooccur_count),
    )


def _entity_id(
    conn: psycopg.Connection[Any],
    entity_type: str,
    canonical_key: str,
    *,
    tenant_id: str = "default",
) -> str | None:
    row = conn.execute(
        "SELECT id::text FROM graph_entities "
        "WHERE tenant_id = %s AND entity_type = %s AND canonical_key = %s",
        (tenant_id, entity_type, canonical_key),
    ).fetchone()
    return None if row is None else str(row[0])


def _mention_count(
    conn: psycopg.Connection[Any],
    entity_type: str,
    canonical_key: str,
    *,
    tenant_id: str = "default",
) -> int:
    """Total mention-rows owned by the named entity (zero if entity absent)."""
    eid = _entity_id(conn, entity_type, canonical_key, tenant_id=tenant_id)
    if eid is None:
        return 0
    row = conn.execute(
        "SELECT count(*) FROM graph_entity_mentions "
        "WHERE tenant_id = %s AND entity_id = %s",
        (tenant_id, eid),
    ).fetchone()
    assert row is not None
    return int(row[0])


def _contribution_count_for(
    conn: psycopg.Connection[Any],
    entity_type: str,
    canonical_key: str,
    *,
    tenant_id: str = "default",
) -> int:
    """Number of contribution rows where the named entity appears on either end."""
    eid = _entity_id(conn, entity_type, canonical_key, tenant_id=tenant_id)
    if eid is None:
        return 0
    row = conn.execute(
        "SELECT count(*) FROM graph_edge_contributions "
        "WHERE tenant_id = %s AND (src_id = %s OR dst_id = %s)",
        (tenant_id, eid, eid),
    ).fetchone()
    assert row is not None
    return int(row[0])


def _cooccur_total(
    conn: psycopg.Connection[Any],
    entity_type_a: str,
    key_a: str,
    entity_type_b: str,
    key_b: str,
    *,
    document_id: str,
    tenant_id: str = "default",
) -> int:
    """Summed ``cooccur_count`` on the canonical edge between two entities in a doc."""
    a = _entity_id(conn, entity_type_a, key_a, tenant_id=tenant_id)
    b = _entity_id(conn, entity_type_b, key_b, tenant_id=tenant_id)
    if a is None or b is None:
        return 0
    src, dst = (a, b) if a < b else (b, a)
    row = conn.execute(
        "SELECT cooccur_count FROM graph_edge_contributions "
        "WHERE tenant_id = %s AND document_id = %s AND src_id = %s AND dst_id = %s",
        (tenant_id, document_id, src, dst),
    ).fetchone()
    return 0 if row is None else int(row[0])


# ---------------------------------------------------------------------------
# Re-point + orphan invariant (F2)
# ---------------------------------------------------------------------------


def test_apply_aliases_repoints_and_orphans_source(
    test_db: psycopg.Connection[Any],
) -> None:
    """Arrange: source 'acme' has two mentions; target 'acme corp' has none.

    Act: apply the rule. Assert: F2 — source row still exists but is
    zero-mention; the two mentions live under the target; the result's
    ``sources_orphaned`` / ``mentions_repointed`` counters match.
    """
    # Arrange
    _seed_entity(test_db, "org", "acme", doc_count=2)
    _seed_entity(test_db, "org", "acme corp", doc_count=3)
    d1, d2 = _make_doc(test_db), _make_doc(test_db)
    _seed_mention(test_db, "org", "acme", document_id=d1)
    _seed_mention(test_db, "org", "acme", document_id=d2)

    # Act
    res = apply_aliases(
        test_db,
        "default",
        [AliasRule("org", "acme", "org", "acme corp")],
    )

    # Assert — counters
    assert res.rules_total == 1
    assert res.rules_applied == 1
    assert res.sources_orphaned == 1
    assert res.mentions_repointed == 2
    assert res.dry_run is False

    # Assert — F2: source row preserved, but mentions moved off it.
    assert _entity_id(test_db, "org", "acme") is not None
    assert _mention_count(test_db, "org", "acme") == 0
    assert _mention_count(test_db, "org", "acme corp") == 2


# ---------------------------------------------------------------------------
# Dry-run rolls back persistence
# ---------------------------------------------------------------------------


def test_apply_aliases_dry_run_persists_nothing(
    test_db: psycopg.Connection[Any],
) -> None:
    """Arrange: same shape as the previous test, but call with ``dry_run=True``.

    Act: apply. Assert: counters report the would-be move, but the database
    state is unchanged — source mention count stays at 1.
    """
    # Arrange
    _seed_entity(test_db, "org", "acme", doc_count=1)
    _seed_entity(test_db, "org", "acme corp", doc_count=1)
    d1 = _make_doc(test_db)
    _seed_mention(test_db, "org", "acme", document_id=d1)

    # Act
    res = apply_aliases(
        test_db,
        "default",
        [AliasRule("org", "acme", "org", "acme corp")],
        dry_run=True,
    )

    # Assert
    assert res.dry_run is True
    assert res.sources_orphaned == 1
    assert res.rules_applied == 1
    assert res.mentions_repointed == 1
    # DB untouched.
    assert _mention_count(test_db, "org", "acme") == 1
    assert _mention_count(test_db, "org", "acme corp") == 0


# ---------------------------------------------------------------------------
# Missing source = idempotent no-op
# ---------------------------------------------------------------------------


def test_apply_aliases_missing_source_is_idempotent_noop(
    test_db: psycopg.Connection[Any],
) -> None:
    """Arrange: source 'gone' was never seeded; target 'acme corp' exists.

    Act: apply. Assert: ``rules_applied == 0`` and no counters tick.
    """
    _seed_entity(test_db, "org", "acme corp")

    res = apply_aliases(
        test_db,
        "default",
        [AliasRule("org", "gone", "org", "acme corp")],
    )

    assert res.rules_total == 1
    assert res.rules_applied == 0
    assert res.sources_orphaned == 0
    assert res.mentions_repointed == 0
    assert res.contributions_repointed == 0


# ---------------------------------------------------------------------------
# Contribution collapse + self-edge drop
# ---------------------------------------------------------------------------


def test_apply_aliases_collapses_contributions_and_drops_self_edge(
    test_db: psycopg.Connection[Any],
) -> None:
    """Variant ``acme`` and target ``acme corp`` co-occur with topic ``x`` in d1,
    AND with each other in d1. After merging:

    * the (acme, acme corp) edge MUST NOT become a self-loop on
      (acme corp, acme corp) — it's dropped.
    * the (acme, x) and (acme corp, x) edges MUST collapse to a single
      (acme corp, x) row with summed ``cooccur_count`` (= 1 + 1 = 2).
    """
    # Arrange
    _seed_entity(test_db, "org", "acme")
    _seed_entity(test_db, "org", "acme corp")
    _seed_entity(test_db, "topic", "x")
    d1 = _make_doc(test_db)
    _seed_contribution(
        test_db, "org", "acme", "org", "acme corp", document_id=d1, cooccur_count=1
    )
    _seed_contribution(
        test_db, "org", "acme", "topic", "x", document_id=d1, cooccur_count=1
    )
    _seed_contribution(
        test_db, "org", "acme corp", "topic", "x", document_id=d1, cooccur_count=1
    )

    # Act
    res = apply_aliases(
        test_db,
        "default",
        [AliasRule("org", "acme", "org", "acme corp")],
    )

    # Assert — counters: 2 source-touching contributions were considered
    # ((acme, acme corp) and (acme, x)). One becomes a self-edge (dropped),
    # one collapses with the pre-existing (acme corp, x).
    assert res.rules_applied == 1
    assert res.sources_orphaned == 1
    assert res.contributions_repointed == 2

    # Source is fully detached from contributions.
    assert _contribution_count_for(test_db, "org", "acme") == 0

    # No self-edge on the target.
    target_id = _entity_id(test_db, "org", "acme corp")
    assert target_id is not None
    self_row = test_db.execute(
        "SELECT count(*) FROM graph_edge_contributions "
        "WHERE tenant_id = %s AND src_id = %s AND dst_id = %s",
        ("default", target_id, target_id),
    ).fetchone()
    assert self_row is not None and int(self_row[0]) == 0

    # (acme corp, x) edge survives with summed cooccur_count.
    assert (
        _cooccur_total(test_db, "org", "acme corp", "topic", "x", document_id=d1) == 2
    )

    # F2: source row still present (caller GCs it via refresh_aggregates).
    assert _entity_id(test_db, "org", "acme") is not None


# ---------------------------------------------------------------------------
# Task 2.8 — presence-flag aspects must not double-count on merge
# ---------------------------------------------------------------------------


def _mention_count_for_doc(
    conn: psycopg.Connection[Any],
    entity_type: str,
    canonical_key: str,
    document_id: str,
    *,
    tenant_id: str = "default",
) -> int | None:
    """Return the ``mention_count`` on the (entity, doc) mention row, or None."""
    eid = _entity_id(conn, entity_type, canonical_key, tenant_id=tenant_id)
    if eid is None:
        return None
    row = conn.execute(
        "SELECT mention_count FROM graph_entity_mentions "
        "WHERE tenant_id = %s AND entity_id = %s AND document_id = %s",
        (tenant_id, eid, document_id),
    ).fetchone()
    return None if row is None else int(row[0])


def test_apply_aliases_person_mentions_stay_presence(
    test_db: psycopg.Connection[Any],
) -> None:
    """Person mentions are presence flags — merging two people that both mention
    the same doc must keep ``mention_count`` at 1, not sum to 2 (Task 2.8).

    Regression: the on-conflict clause used to always SUM ``mention_count``, which
    inflated the person presence flag (``source = 'people'``, always 1) to 2.
    """
    # Arrange: two PERSON entities that both mention doc D (presence 1 each).
    _seed_entity(test_db, "person", "jane doe")
    _seed_entity(test_db, "person", "jane d")  # variant to merge into 'jane doe'
    d = _make_doc(test_db)
    _seed_mention(test_db, "person", "jane doe", document_id=d, mention_count=1)
    _seed_mention(test_db, "person", "jane d", document_id=d, mention_count=1)

    # Act: merge the variant into the canonical person.
    apply_aliases(
        test_db,
        "default",
        [AliasRule("person", "jane d", "person", "jane doe")],
    )

    # Assert: presence preserved (1), NOT summed to 2.
    assert _mention_count_for_doc(test_db, "person", "jane doe", d) == 1


def test_apply_aliases_concept_mentions_still_sum(
    test_db: psycopg.Connection[Any],
) -> None:
    """Concept mentions carry real counts (``source = 'extractor:...'``) and must
    still SUM on merge — the aspect-aware clause only clamps people presence.
    """
    # Arrange: two TOPIC (concept) entities that both mention doc D with real
    # counts under a concept extractor source.
    concept_source = "extractor:test-model@concepts-v5"
    _seed_entity(test_db, "topic", "widget a")
    _seed_entity(test_db, "topic", "widget b")
    d = _make_doc(test_db)
    _seed_mention(
        test_db, "topic", "widget a", document_id=d, mention_count=2, source=concept_source
    )
    _seed_mention(
        test_db, "topic", "widget b", document_id=d, mention_count=3, source=concept_source
    )

    # Act
    apply_aliases(
        test_db,
        "default",
        [AliasRule("topic", "widget a", "topic", "widget b")],
    )

    # Assert: concept counts still sum (2 + 3 = 5).
    assert _mention_count_for_doc(test_db, "topic", "widget b", d) == 5


def test_apply_aliases_person_cooccur_stays_presence(
    test_db: psycopg.Connection[Any],
) -> None:
    """Person co-occurrence edges are presence flags too — merging two people who
    both co-occur with a third in the same doc must keep ``cooccur_count`` at 1,
    not sum to 2 (Task 2.8, contribution side).
    """
    # Arrange: 'jane doe' and its variant both co-occur with 'carol' in doc D
    # (person-person presence, cooccur 1 each).
    _seed_entity(test_db, "person", "jane doe")
    _seed_entity(test_db, "person", "jane d")
    _seed_entity(test_db, "person", "carol")
    d = _make_doc(test_db)
    _seed_contribution(
        test_db, "person", "jane doe", "person", "carol", document_id=d, cooccur_count=1
    )
    _seed_contribution(
        test_db, "person", "jane d", "person", "carol", document_id=d, cooccur_count=1
    )

    # Act
    apply_aliases(
        test_db,
        "default",
        [AliasRule("person", "jane d", "person", "jane doe")],
    )

    # Assert: the (jane doe, carol) presence edge stays at 1, not summed to 2.
    assert (
        _cooccur_total(test_db, "person", "jane doe", "person", "carol", document_id=d)
        == 1
    )
