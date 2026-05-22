"""Real-DB tests for migration 012_graphrag.sql (tenantized, AGE pivot v5).

Uses the ``test_db`` fixture (conftest's reset-and-migrate harness against the
Apache-AGE test instance on port 5434). Asserts the five relational
source-of-truth tables exist and that the schema-level guards hold:

* every table carries ``tenant_id`` (NOT NULL, default ``'default'``);
* ``tenant_id`` is part of every PK / UNIQUE and the lookup indexes;
* the ``src_id < dst_id`` CHECK on both edge tables;
* the entity-type CHECK;
* the ``(tenant_id, entity_type, canonical_key)`` uniqueness (tenant-scoped);
* FK ``ON DELETE CASCADE`` from documents clears source-of-truth rows;
* ``graph_entities.embedding`` ships as a NULLABLE ``vector(1024)`` with no HNSW.

All rows are synthetic; no production data.
"""
from __future__ import annotations

from pathlib import Path

import psycopg
import pytest

_MIGRATION_012 = Path(__file__).parent.parent / "migrations" / "012_graphrag.sql"

_GRAPH_TABLES = (
    "graph_entities",
    "graph_entity_mentions",
    "graph_edge_contributions",
    "graph_relationships",
    "graph_index_state",
)


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #
def _seed_doc(conn: psycopg.Connection, content_hash: str = "g-hash") -> str:
    """Insert one bare document row (for the FK targets) and return its UUID."""
    row = conn.execute(
        "INSERT INTO documents (title, content, content_hash, content_type) "
        "VALUES (%s, %s, %s, %s) RETURNING id::text",
        ("seed", "seed body", content_hash, "note"),
    ).fetchone()
    assert row is not None
    return str(row[0])


def _insert_entity(
    conn: psycopg.Connection,
    *,
    entity_type: str = "person",
    name: str = "Person A",
    canonical_key: str = "person-a",
    tenant_id: str | None = None,
) -> str:
    """Insert a graph_entities row and return its UUID as text.

    Omitting ``tenant_id`` lets the column default to ``'default'``.
    """
    if tenant_id is None:
        row = conn.execute(
            "INSERT INTO graph_entities (entity_type, name, canonical_key) "
            "VALUES (%s, %s, %s) RETURNING id::text",
            (entity_type, name, canonical_key),
        ).fetchone()
    else:
        row = conn.execute(
            "INSERT INTO graph_entities (entity_type, name, canonical_key, tenant_id) "
            "VALUES (%s, %s, %s, %s) RETURNING id::text",
            (entity_type, name, canonical_key, tenant_id),
        ).fetchone()
    assert row is not None
    return str(row[0])


def _ordered_pair(conn: psycopg.Connection, a: str, b: str) -> tuple[str, str]:
    """Return ``(lo, hi)`` such that ``lo::uuid < hi::uuid`` (DB-authoritative)."""
    row = conn.execute(
        "SELECT least(%s::uuid, %s::uuid)::text, greatest(%s::uuid, %s::uuid)::text",
        (a, b, a, b),
    ).fetchone()
    assert row is not None
    return str(row[0]), str(row[1])


def _pk_columns(conn: psycopg.Connection, table: str) -> set[str]:
    """Return the set of primary-key column names for ``table``."""
    rows = conn.execute(
        "SELECT a.attname FROM pg_index i "
        "JOIN pg_attribute a ON a.attrelid = i.indrelid AND a.attnum = ANY(i.indkey) "
        "WHERE i.indrelid = %s::regclass AND i.indisprimary",
        (table,),
    ).fetchall()
    return {str(r[0]) for r in rows}


def _unique_constraint_columns(conn: psycopg.Connection, conname: str) -> set[str]:
    """Return the set of column names backing a named UNIQUE/PK constraint."""
    rows = conn.execute(
        "SELECT a.attname FROM pg_constraint c "
        "JOIN pg_attribute a ON a.attrelid = c.conrelid AND a.attnum = ANY(c.conkey) "
        "WHERE c.conname = %s",
        (conname,),
    ).fetchall()
    return {str(r[0]) for r in rows}


def _index_columns(conn: psycopg.Connection, indexname: str) -> set[str]:
    """Return the set of column names participating in ``indexname``."""
    rows = conn.execute(
        "SELECT a.attname FROM pg_index i "
        "JOIN pg_class c ON c.oid = i.indexrelid "
        "JOIN pg_attribute a ON a.attrelid = i.indrelid AND a.attnum = ANY(i.indkey) "
        "WHERE c.relname = %s",
        (indexname,),
    ).fetchall()
    return {str(r[0]) for r in rows}


# --------------------------------------------------------------------------- #
# Tables + indexes exist
# --------------------------------------------------------------------------- #
def test_migration_012_creates_five_tables(test_db: psycopg.Connection) -> None:
    rows = test_db.execute(
        "SELECT table_name FROM information_schema.tables "
        "WHERE table_schema = 'public' AND table_name = ANY(%s)",
        (list(_GRAPH_TABLES),),
    ).fetchall()
    present = {str(r[0]) for r in rows}
    assert present == set(_GRAPH_TABLES)


def test_migration_012_creates_indexes(test_db: psycopg.Connection) -> None:
    rows = test_db.execute(
        "SELECT indexname FROM pg_indexes "
        "WHERE schemaname = 'public' AND tablename = ANY(%s)",
        (list(_GRAPH_TABLES),),
    ).fetchall()
    names = {str(r[0]) for r in rows}
    for expected in (
        "idx_graph_entities_type",
        "idx_gem_document",
        "idx_gec_src",
        "idx_gec_dst",
        "idx_grel_src",
        "idx_grel_dst",
    ):
        assert expected in names


def test_migration_012_lookup_indexes_lead_with_tenant(
    test_db: psycopg.Connection,
) -> None:
    """Every per-tenant lookup index includes ``tenant_id`` (spec §5)."""
    for index in (
        "idx_graph_entities_type",
        "idx_gem_document",
        "idx_gec_src",
        "idx_gec_dst",
        "idx_grel_src",
        "idx_grel_dst",
    ):
        assert "tenant_id" in _index_columns(test_db, index), index


def test_migration_012_no_hnsw_index_on_graph_entities(
    test_db: psycopg.Connection,
) -> None:
    """G0 deliberately skips the HNSW index on ``graph_entities.embedding``."""
    rows = test_db.execute(
        "SELECT indexdef FROM pg_indexes "
        "WHERE schemaname = 'public' AND tablename = 'graph_entities'"
    ).fetchall()
    assert all("hnsw" not in str(r[0]).lower() for r in rows)


def test_migration_012_embedding_is_nullable_vector_1024(
    test_db: psycopg.Connection,
) -> None:
    """``graph_entities.embedding`` ships as a NULLABLE ``vector(1024)``.

    Keeps the G0b dim-reconciliation machinery working: ``ensure_embedding_column``
    reads the declared dim and resizes the column to the active embedder's dim.
    """
    row = test_db.execute(
        "SELECT format_type(atttypid, atttypmod), attnotnull FROM pg_attribute "
        "WHERE attrelid = 'graph_entities'::regclass AND attname = 'embedding'"
    ).fetchone()
    assert row is not None
    assert str(row[0]) == "vector(1024)"
    assert row[1] is False  # not NOT NULL → nullable


# --------------------------------------------------------------------------- #
# tenant_id present everywhere
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("table", _GRAPH_TABLES)
def test_migration_012_table_has_tenant_id(
    test_db: psycopg.Connection, table: str
) -> None:
    """Every graph table carries ``tenant_id`` NOT NULL DEFAULT 'default'."""
    row = test_db.execute(
        "SELECT is_nullable, column_default FROM information_schema.columns "
        "WHERE table_name = %s AND column_name = 'tenant_id'",
        (table,),
    ).fetchone()
    assert row is not None, f"{table} is missing a tenant_id column"
    assert str(row[0]) == "NO", f"{table}.tenant_id must be NOT NULL"
    assert "'default'" in str(row[1]), f"{table}.tenant_id must default to 'default'"


@pytest.mark.parametrize(
    ("table", "expected_pk"),
    [
        # graph_entities is keyed on the COMPOSITE (tenant_id, id) so child
        # tables can carry tenant-safe composite FKs.
        ("graph_entities", {"tenant_id", "id"}),
        ("graph_entity_mentions", {"tenant_id", "entity_id", "document_id"}),
        (
            "graph_edge_contributions",
            {"tenant_id", "document_id", "src_id", "dst_id"},
        ),
        ("graph_relationships", {"tenant_id", "src_id", "dst_id", "rel_type"}),
        ("graph_index_state", {"tenant_id", "document_id", "aspect"}),
    ],
)
def test_migration_012_pk_includes_tenant_id(
    test_db: psycopg.Connection, table: str, expected_pk: set[str]
) -> None:
    """The PK of every graph table includes ``tenant_id`` (spec §5)."""
    assert _pk_columns(test_db, table) == expected_pk


def test_migration_012_uq_graph_entities_includes_tenant_id(
    test_db: psycopg.Connection,
) -> None:
    """``graph_entities`` dedup uniqueness is scoped to the tenant (spec §5)."""
    assert _unique_constraint_columns(test_db, "uq_graph_entities") == {
        "tenant_id",
        "entity_type",
        "canonical_key",
    }


# --------------------------------------------------------------------------- #
# entity_type CHECK + tenant-scoped uniqueness
# --------------------------------------------------------------------------- #
def test_entity_type_check_rejects_invalid_type(test_db: psycopg.Connection) -> None:
    with pytest.raises(psycopg.errors.CheckViolation):
        test_db.execute(
            "INSERT INTO graph_entities (entity_type, name, canonical_key) "
            "VALUES (%s, %s, %s)",
            ("alien", "Bad", "bad-key"),
        )


def test_entity_type_check_accepts_each_allowed_type(
    test_db: psycopg.Connection,
) -> None:
    for etype in ("person", "org", "project", "topic", "tool"):
        _insert_entity(test_db, entity_type=etype, name=etype, canonical_key=etype)
    count = test_db.execute("SELECT count(*) FROM graph_entities").fetchone()
    assert count is not None
    assert count[0] == 5


def test_uq_graph_entities_rejects_duplicate(test_db: psycopg.Connection) -> None:
    _insert_entity(test_db, entity_type="topic", name="Topic", canonical_key="dup")
    with pytest.raises(psycopg.errors.UniqueViolation):
        _insert_entity(
            test_db, entity_type="topic", name="Topic Two", canonical_key="dup"
        )


def test_uq_graph_entities_allows_same_key_different_type(
    test_db: psycopg.Connection,
) -> None:
    """Uniqueness is scoped to ``(tenant_id, entity_type, canonical_key)``."""
    _insert_entity(test_db, entity_type="topic", name="Acme", canonical_key="acme")
    _insert_entity(test_db, entity_type="org", name="Acme", canonical_key="acme")
    count = test_db.execute("SELECT count(*) FROM graph_entities").fetchone()
    assert count is not None
    assert count[0] == 2


def test_uq_graph_entities_allows_same_key_different_tenant(
    test_db: psycopg.Connection,
) -> None:
    """Tenant isolation: the same dedup key may exist independently per tenant."""
    _insert_entity(
        test_db,
        entity_type="topic",
        name="Acme",
        canonical_key="acme",
        tenant_id="default",
    )
    _insert_entity(
        test_db,
        entity_type="topic",
        name="Acme",
        canonical_key="acme",
        tenant_id="acme",
    )
    count = test_db.execute("SELECT count(*) FROM graph_entities").fetchone()
    assert count is not None
    assert count[0] == 2


# --------------------------------------------------------------------------- #
# src_id < dst_id CHECK — contributions
# --------------------------------------------------------------------------- #
def test_contribution_canonical_check_accepts_ordered_pair(
    test_db: psycopg.Connection,
) -> None:
    doc = _seed_doc(test_db)
    a = _insert_entity(test_db, canonical_key="a")
    b = _insert_entity(test_db, canonical_key="b")
    lo, hi = _ordered_pair(test_db, a, b)
    test_db.execute(
        "INSERT INTO graph_edge_contributions (document_id, src_id, dst_id) "
        "VALUES (%s, %s, %s)",
        (doc, lo, hi),
    )
    count = test_db.execute(
        "SELECT count(*) FROM graph_edge_contributions"
    ).fetchone()
    assert count is not None
    assert count[0] == 1


def test_contribution_canonical_check_rejects_reversed_pair(
    test_db: psycopg.Connection,
) -> None:
    doc = _seed_doc(test_db)
    a = _insert_entity(test_db, canonical_key="a")
    b = _insert_entity(test_db, canonical_key="b")
    lo, hi = _ordered_pair(test_db, a, b)
    with pytest.raises(psycopg.errors.CheckViolation):
        test_db.execute(
            "INSERT INTO graph_edge_contributions (document_id, src_id, dst_id) "
            "VALUES (%s, %s, %s)",
            (doc, hi, lo),  # reversed → src_id > dst_id
        )


def test_contribution_canonical_check_rejects_equal_pair(
    test_db: psycopg.Connection,
) -> None:
    doc = _seed_doc(test_db)
    a = _insert_entity(test_db, canonical_key="a")
    with pytest.raises(psycopg.errors.CheckViolation):
        test_db.execute(
            "INSERT INTO graph_edge_contributions (document_id, src_id, dst_id) "
            "VALUES (%s, %s, %s)",
            (doc, a, a),  # equal → not src_id < dst_id
        )


# --------------------------------------------------------------------------- #
# src_id < dst_id CHECK — relationships
# --------------------------------------------------------------------------- #
def test_relationship_canonical_check_accepts_ordered_pair(
    test_db: psycopg.Connection,
) -> None:
    a = _insert_entity(test_db, canonical_key="a")
    b = _insert_entity(test_db, canonical_key="b")
    lo, hi = _ordered_pair(test_db, a, b)
    test_db.execute(
        "INSERT INTO graph_relationships (src_id, dst_id, weight) VALUES (%s, %s, %s)",
        (lo, hi, 0.5),
    )
    count = test_db.execute("SELECT count(*) FROM graph_relationships").fetchone()
    assert count is not None
    assert count[0] == 1


def test_relationship_canonical_check_rejects_reversed_pair(
    test_db: psycopg.Connection,
) -> None:
    a = _insert_entity(test_db, canonical_key="a")
    b = _insert_entity(test_db, canonical_key="b")
    lo, hi = _ordered_pair(test_db, a, b)
    with pytest.raises(psycopg.errors.CheckViolation):
        test_db.execute(
            "INSERT INTO graph_relationships (src_id, dst_id, weight) "
            "VALUES (%s, %s, %s)",
            (hi, lo, 0.5),  # reversed → canonical CHECK fails (weight is valid)
        )


# --------------------------------------------------------------------------- #
# Tenant-safe composite FKs (Codex S1): a child row can only reference an
# entity in its OWN tenant.
# --------------------------------------------------------------------------- #
def test_mention_cross_tenant_fk_is_rejected(test_db: psycopg.Connection) -> None:
    """A mention in tenant 'A' cannot reference an entity owned by tenant 'B'."""
    doc = _seed_doc(test_db)
    ent_b = _insert_entity(test_db, canonical_key="b-ent", tenant_id="tenant-b")
    with pytest.raises(psycopg.errors.ForeignKeyViolation):
        test_db.execute(
            "INSERT INTO graph_entity_mentions "
            "(tenant_id, entity_id, document_id, source) VALUES (%s, %s, %s, %s)",
            ("tenant-a", ent_b, doc, "people"),  # (tenant-a, ent_b) has no entity row
        )


def test_mention_same_tenant_fk_is_accepted(test_db: psycopg.Connection) -> None:
    """The composite FK still allows a same-tenant reference (sanity)."""
    doc = _seed_doc(test_db)
    ent = _insert_entity(test_db, canonical_key="b-ent", tenant_id="tenant-b")
    test_db.execute(
        "INSERT INTO graph_entity_mentions "
        "(tenant_id, entity_id, document_id, source) VALUES (%s, %s, %s, %s)",
        ("tenant-b", ent, doc, "people"),
    )
    count = test_db.execute(
        "SELECT count(*) FROM graph_entity_mentions"
    ).fetchone()
    assert count is not None
    assert count[0] == 1


def test_contribution_cross_tenant_fk_is_rejected(
    test_db: psycopg.Connection,
) -> None:
    """A contribution endpoint cannot reference an entity in a different tenant."""
    doc = _seed_doc(test_db)
    a = _insert_entity(test_db, canonical_key="a", tenant_id="tenant-b")
    b = _insert_entity(test_db, canonical_key="b", tenant_id="tenant-b")
    lo, hi = _ordered_pair(test_db, a, b)
    with pytest.raises(psycopg.errors.ForeignKeyViolation):
        test_db.execute(
            "INSERT INTO graph_edge_contributions "
            "(tenant_id, document_id, src_id, dst_id) VALUES (%s, %s, %s, %s)",
            ("tenant-a", doc, lo, hi),  # endpoints live in tenant-b
        )


def test_relationship_cross_tenant_fk_is_rejected(
    test_db: psycopg.Connection,
) -> None:
    """A relationship endpoint cannot reference an entity in a different tenant."""
    a = _insert_entity(test_db, canonical_key="a", tenant_id="tenant-b")
    b = _insert_entity(test_db, canonical_key="b", tenant_id="tenant-b")
    lo, hi = _ordered_pair(test_db, a, b)
    with pytest.raises(psycopg.errors.ForeignKeyViolation):
        test_db.execute(
            "INSERT INTO graph_relationships (tenant_id, src_id, dst_id, weight) "
            "VALUES (%s, %s, %s, %s)",
            ("tenant-a", lo, hi, 0.5),  # endpoints live in tenant-b
        )


# --------------------------------------------------------------------------- #
# aspect CHECK (Codex S2)
# --------------------------------------------------------------------------- #
def test_index_state_aspect_check_rejects_unknown(
    test_db: psycopg.Connection,
) -> None:
    """``aspect`` is constrained to the two known aspects (spec §7)."""
    doc = _seed_doc(test_db)
    with pytest.raises(psycopg.errors.CheckViolation):
        test_db.execute(
            "INSERT INTO graph_index_state "
            "(document_id, aspect, content_hash, inputs_hash, extractor_ver) "
            "VALUES (%s, %s, %s, %s, %s)",
            (doc, "bogus", "ch", "ih", "people@1"),
        )


def test_index_state_aspect_check_accepts_known(
    test_db: psycopg.Connection,
) -> None:
    doc = _seed_doc(test_db)
    for aspect in ("people", "concepts"):
        test_db.execute(
            "INSERT INTO graph_index_state "
            "(document_id, aspect, content_hash, inputs_hash, extractor_ver) "
            "VALUES (%s, %s, %s, %s, %s)",
            (doc, aspect, "ch", "ih", "people@1"),
        )
    count = test_db.execute(
        "SELECT count(*) FROM graph_index_state WHERE document_id = %s", (doc,)
    ).fetchone()
    assert count is not None
    assert count[0] == 2


# --------------------------------------------------------------------------- #
# weight + count sanity CHECKs (Codex S3)
# --------------------------------------------------------------------------- #
def test_relationship_weight_is_not_null_without_default(
    test_db: psycopg.Connection,
) -> None:
    """``weight`` has no default — omitting it is a NOT NULL violation."""
    a = _insert_entity(test_db, canonical_key="a")
    b = _insert_entity(test_db, canonical_key="b")
    lo, hi = _ordered_pair(test_db, a, b)
    with pytest.raises(psycopg.errors.NotNullViolation):
        test_db.execute(
            "INSERT INTO graph_relationships (src_id, dst_id) VALUES (%s, %s)",
            (lo, hi),
        )


@pytest.mark.parametrize("bad_weight", [0.0, -0.1, 1.0001, 2.0])
def test_relationship_weight_range_rejects_out_of_bounds(
    test_db: psycopg.Connection, bad_weight: float
) -> None:
    """``weight`` must be normalized lift in (0, 1] (spec §4 D4)."""
    a = _insert_entity(test_db, canonical_key="a")
    b = _insert_entity(test_db, canonical_key="b")
    lo, hi = _ordered_pair(test_db, a, b)
    with pytest.raises(psycopg.errors.CheckViolation):
        test_db.execute(
            "INSERT INTO graph_relationships (src_id, dst_id, weight) "
            "VALUES (%s, %s, %s)",
            (lo, hi, bad_weight),
        )


@pytest.mark.parametrize("good_weight", [0.0001, 0.5, 1.0])
def test_relationship_weight_range_accepts_in_bounds(
    test_db: psycopg.Connection, good_weight: float
) -> None:
    a = _insert_entity(test_db, canonical_key="a")
    b = _insert_entity(test_db, canonical_key="b")
    lo, hi = _ordered_pair(test_db, a, b)
    test_db.execute(
        "INSERT INTO graph_relationships (src_id, dst_id, weight) VALUES (%s, %s, %s)",
        (lo, hi, good_weight),
    )
    count = test_db.execute("SELECT count(*) FROM graph_relationships").fetchone()
    assert count is not None
    assert count[0] == 1


def test_graph_entities_doc_count_non_negative(test_db: psycopg.Connection) -> None:
    with pytest.raises(psycopg.errors.CheckViolation):
        test_db.execute(
            "INSERT INTO graph_entities (entity_type, name, canonical_key, doc_count) "
            "VALUES (%s, %s, %s, %s)",
            ("topic", "Neg", "neg", -1),
        )


def test_mention_count_non_negative(test_db: psycopg.Connection) -> None:
    doc = _seed_doc(test_db)
    ent = _insert_entity(test_db, canonical_key="a")
    with pytest.raises(psycopg.errors.CheckViolation):
        test_db.execute(
            "INSERT INTO graph_entity_mentions "
            "(entity_id, document_id, source, mention_count) VALUES (%s, %s, %s, %s)",
            (ent, doc, "people", -1),
        )


def test_contribution_cooccur_count_non_negative(
    test_db: psycopg.Connection,
) -> None:
    doc = _seed_doc(test_db)
    a = _insert_entity(test_db, canonical_key="a")
    b = _insert_entity(test_db, canonical_key="b")
    lo, hi = _ordered_pair(test_db, a, b)
    with pytest.raises(psycopg.errors.CheckViolation):
        test_db.execute(
            "INSERT INTO graph_edge_contributions "
            "(document_id, src_id, dst_id, cooccur_count) VALUES (%s, %s, %s, %s)",
            (doc, lo, hi, -1),
        )


@pytest.mark.parametrize(
    "stmt",
    [
        "INSERT INTO graph_relationships (src_id, dst_id, weight, co_count) "
        "VALUES (%s, %s, %s, %s)",
        "INSERT INTO graph_relationships (src_id, dst_id, weight, doc_count) "
        "VALUES (%s, %s, %s, %s)",
    ],
)
def test_relationship_counts_non_negative(
    test_db: psycopg.Connection, stmt: str
) -> None:
    a = _insert_entity(test_db, canonical_key="a")
    b = _insert_entity(test_db, canonical_key="b")
    lo, hi = _ordered_pair(test_db, a, b)
    with pytest.raises(psycopg.errors.CheckViolation):
        test_db.execute(stmt, (lo, hi, 0.5, -1))


# --------------------------------------------------------------------------- #
# graph_index_state PK (tenant_id, document_id, aspect)
# --------------------------------------------------------------------------- #
def _insert_index_state(conn: psycopg.Connection, doc: str, aspect: str) -> None:
    conn.execute(
        "INSERT INTO graph_index_state "
        "(document_id, aspect, content_hash, inputs_hash, extractor_ver) "
        "VALUES (%s, %s, %s, %s, %s)",
        (doc, aspect, "ch", "ih", "people@1"),
    )


def test_index_state_rejects_duplicate_doc_aspect(
    test_db: psycopg.Connection,
) -> None:
    doc = _seed_doc(test_db)
    _insert_index_state(test_db, doc, "people")
    with pytest.raises(psycopg.errors.UniqueViolation):
        _insert_index_state(test_db, doc, "people")


def test_index_state_allows_same_doc_distinct_aspects(
    test_db: psycopg.Connection,
) -> None:
    """People and concepts re-index independently → both aspects coexist."""
    doc = _seed_doc(test_db)
    _insert_index_state(test_db, doc, "people")
    _insert_index_state(test_db, doc, "concepts")
    count = test_db.execute(
        "SELECT count(*) FROM graph_index_state WHERE document_id = %s", (doc,)
    ).fetchone()
    assert count is not None
    assert count[0] == 2


def test_index_state_has_no_bare_extractor_column(
    test_db: psycopg.Connection,
) -> None:
    """Spec §5 lists only ``extractor_ver`` — the bare ``extractor`` is dropped."""
    cols = test_db.execute(
        "SELECT column_name FROM information_schema.columns "
        "WHERE table_name = 'graph_index_state'"
    ).fetchall()
    names = {str(r[0]) for r in cols}
    assert "extractor_ver" in names
    assert "extractor" not in names


# --------------------------------------------------------------------------- #
# Cascade + idempotency
# --------------------------------------------------------------------------- #
def test_mentions_cascade_on_document_delete(test_db: psycopg.Connection) -> None:
    """``ON DELETE CASCADE`` to documents clears a doc's source-of-truth rows."""
    doc = _seed_doc(test_db)
    ent = _insert_entity(test_db, canonical_key="a")
    test_db.execute(
        "INSERT INTO graph_entity_mentions (entity_id, document_id, source) "
        "VALUES (%s, %s, %s)",
        (ent, doc, "people"),
    )
    test_db.execute("DELETE FROM documents WHERE id = %s", (doc,))
    count = test_db.execute(
        "SELECT count(*) FROM graph_entity_mentions WHERE document_id = %s", (doc,)
    ).fetchone()
    assert count is not None
    assert count[0] == 0


def test_mentions_cascade_on_entity_delete(test_db: psycopg.Connection) -> None:
    """``ON DELETE CASCADE`` to graph_entities clears a deleted entity's mentions."""
    doc = _seed_doc(test_db)
    ent = _insert_entity(test_db, canonical_key="a")
    test_db.execute(
        "INSERT INTO graph_entity_mentions (entity_id, document_id, source) "
        "VALUES (%s, %s, %s)",
        (ent, doc, "people"),
    )
    test_db.execute("DELETE FROM graph_entities WHERE id = %s", (ent,))
    count = test_db.execute(
        "SELECT count(*) FROM graph_entity_mentions WHERE entity_id = %s", (ent,)
    ).fetchone()
    assert count is not None
    assert count[0] == 0


def test_migration_012_is_idempotent(test_db: psycopg.Connection) -> None:
    """Re-running the SQL on a fresh DB is safe (IF NOT EXISTS guards)."""
    sql = _MIGRATION_012.read_text()
    test_db.execute(sql)  # second apply (first ran via the fixture)
    test_db.execute(sql)  # third apply — still safe
    rows = test_db.execute(
        "SELECT table_name FROM information_schema.tables "
        "WHERE table_schema = 'public' AND table_name = ANY(%s)",
        (list(_GRAPH_TABLES),),
    ).fetchall()
    assert {str(r[0]) for r in rows} == set(_GRAPH_TABLES)
