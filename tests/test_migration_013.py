"""Real-DB tests for migration 013_graphrag_communities.sql (wave G3, §17c Q1).

Uses the ``test_db`` fixture (conftest's reset-and-migrate harness against the
Apache-AGE test instance on port 5434). Asserts the two relational community
tables exist and that the schema-level guards hold:

* both tables carry ``tenant_id`` (NOT NULL, default ``'default'``);
* ``tenant_id`` is part of every PK / UNIQUE and the btree lookup indexes;
* the ``level = 0`` CHECK (single-level only — §17c Q1 / §15);
* PK ``(tenant_id, community_key)`` / ``(tenant_id, community_key, entity_id)``;
* UNIQUE ``(tenant_id, level, members_hash)`` (tenant-scoped identity);
* tenant-safe composite FKs to BOTH graph_communities and graph_entities;
* ``ON DELETE CASCADE`` from communities + entities clears membership;
* non-negative count/weight CHECKs;
* ``summary_embedding`` ships as a NULLABLE ``vector(1024)`` with no HNSW;
* ``summary_tsv`` is GENERATED from ``summary`` and backed by a GIN index.

All rows are synthetic; no production data.
"""
from __future__ import annotations

import psycopg
import pytest

from brain.db import migrations_dir

_MIGRATION_013 = migrations_dir() / "013_graphrag_communities.sql"

_COMMUNITY_TABLES = ("graph_communities", "graph_community_members")


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #
def _insert_entity(
    conn: psycopg.Connection,
    *,
    entity_type: str = "person",
    name: str = "Person A",
    canonical_key: str = "person-a",
    tenant_id: str | None = None,
) -> str:
    """Insert a graph_entities row and return its UUID as text."""
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


def _insert_community(
    conn: psycopg.Connection,
    *,
    tenant_id: str | None = None,
    source_graph_hash: str = "gh",
    members_hash: str = "mh",
    summary: str | None = None,
) -> str:
    """Insert a graph_communities row and return its community_key as text.

    Omitting ``tenant_id`` lets the column default to ``'default'``.
    """
    cols = ["source_graph_hash", "members_hash"]
    vals: list[object] = [source_graph_hash, members_hash]
    if tenant_id is not None:
        cols.append("tenant_id")
        vals.append(tenant_id)
    if summary is not None:
        cols.append("summary")
        vals.append(summary)
    placeholders = ", ".join(["%s"] * len(vals))
    row = conn.execute(
        f"INSERT INTO graph_communities ({', '.join(cols)}) "
        f"VALUES ({placeholders}) RETURNING community_key::text",
        tuple(vals),
    ).fetchone()
    assert row is not None
    return str(row[0])


def _pk_columns(conn: psycopg.Connection, table: str) -> set[str]:
    rows = conn.execute(
        "SELECT a.attname FROM pg_index i "
        "JOIN pg_attribute a ON a.attrelid = i.indrelid AND a.attnum = ANY(i.indkey) "
        "WHERE i.indrelid = %s::regclass AND i.indisprimary",
        (table,),
    ).fetchall()
    return {str(r[0]) for r in rows}


def _unique_constraint_columns(conn: psycopg.Connection, conname: str) -> set[str]:
    rows = conn.execute(
        "SELECT a.attname FROM pg_constraint c "
        "JOIN pg_attribute a ON a.attrelid = c.conrelid AND a.attnum = ANY(c.conkey) "
        "WHERE c.conname = %s",
        (conname,),
    ).fetchall()
    return {str(r[0]) for r in rows}


def _index_columns(conn: psycopg.Connection, indexname: str) -> set[str]:
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
def test_migration_013_creates_two_tables(test_db: psycopg.Connection) -> None:
    rows = test_db.execute(
        "SELECT table_name FROM information_schema.tables "
        "WHERE table_schema = 'public' AND table_name = ANY(%s)",
        (list(_COMMUNITY_TABLES),),
    ).fetchall()
    assert {str(r[0]) for r in rows} == set(_COMMUNITY_TABLES)


def test_migration_013_creates_indexes(test_db: psycopg.Connection) -> None:
    rows = test_db.execute(
        "SELECT indexname FROM pg_indexes "
        "WHERE schemaname = 'public' AND tablename = ANY(%s)",
        (list(_COMMUNITY_TABLES),),
    ).fetchall()
    names = {str(r[0]) for r in rows}
    for expected in (
        "idx_graph_communities_tenant_level",
        "idx_graph_communities_summary_tsv",
        "idx_gcm_entity",
    ):
        assert expected in names


def test_migration_013_btree_lookup_indexes_lead_with_tenant(
    test_db: psycopg.Connection,
) -> None:
    """The btree lookup indexes include ``tenant_id`` (spec §5)."""
    for index in ("idx_graph_communities_tenant_level", "idx_gcm_entity"):
        assert "tenant_id" in _index_columns(test_db, index), index


def test_migration_013_summary_tsv_has_gin_index(test_db: psycopg.Connection) -> None:
    """``summary_tsv`` is backed by a GIN index for the global FTS leg (§17c Q4)."""
    rows = test_db.execute(
        "SELECT indexdef FROM pg_indexes "
        "WHERE schemaname = 'public' AND indexname = 'idx_graph_communities_summary_tsv'"
    ).fetchall()
    assert len(rows) == 1
    indexdef = str(rows[0][0]).lower()
    assert "gin" in indexdef
    assert "summary_tsv" in indexdef


def test_migration_013_no_hnsw_index_on_communities(
    test_db: psycopg.Connection,
) -> None:
    """G3 defers the HNSW on ``summary_embedding`` to finalize-time (like 012)."""
    rows = test_db.execute(
        "SELECT indexdef FROM pg_indexes "
        "WHERE schemaname = 'public' AND tablename = 'graph_communities'"
    ).fetchall()
    assert all("hnsw" not in str(r[0]).lower() for r in rows)


def test_migration_013_summary_embedding_is_nullable_vector_1024(
    test_db: psycopg.Connection,
) -> None:
    """``summary_embedding`` ships as a NULLABLE ``vector(1024)`` (dim-reconcilable)."""
    row = test_db.execute(
        "SELECT format_type(atttypid, atttypmod), attnotnull FROM pg_attribute "
        "WHERE attrelid = 'graph_communities'::regclass AND attname = 'summary_embedding'"
    ).fetchone()
    assert row is not None
    assert str(row[0]) == "vector(1024)"
    assert row[1] is False  # nullable


# --------------------------------------------------------------------------- #
# tenant_id present everywhere
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("table", _COMMUNITY_TABLES)
def test_migration_013_table_has_tenant_id(
    test_db: psycopg.Connection, table: str
) -> None:
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
        ("graph_communities", {"tenant_id", "community_key"}),
        (
            "graph_community_members",
            {"tenant_id", "community_key", "entity_id"},
        ),
    ],
)
def test_migration_013_pk_includes_tenant_id(
    test_db: psycopg.Connection, table: str, expected_pk: set[str]
) -> None:
    assert _pk_columns(test_db, table) == expected_pk


def test_migration_013_uq_communities_columns(test_db: psycopg.Connection) -> None:
    """Per-community identity is tenant-scoped: (tenant_id, level, members_hash)."""
    assert _unique_constraint_columns(test_db, "uq_graph_communities") == {
        "tenant_id",
        "level",
        "members_hash",
    }


# --------------------------------------------------------------------------- #
# level CHECK (single-level only — §17c Q1 / §15)
# --------------------------------------------------------------------------- #
def test_level_defaults_to_zero(test_db: psycopg.Connection) -> None:
    key = _insert_community(test_db)
    row = test_db.execute(
        "SELECT level FROM graph_communities WHERE community_key = %s", (key,)
    ).fetchone()
    assert row is not None
    assert row[0] == 0


def test_level_check_rejects_nonzero(test_db: psycopg.Connection) -> None:
    with pytest.raises(psycopg.errors.CheckViolation):
        test_db.execute(
            "INSERT INTO graph_communities (level, source_graph_hash, members_hash) "
            "VALUES (%s, %s, %s)",
            (1, "gh", "mh"),
        )


# --------------------------------------------------------------------------- #
# summary_tsv GENERATED column
# --------------------------------------------------------------------------- #
def test_summary_tsv_generated_from_summary(test_db: psycopg.Connection) -> None:
    key = _insert_community(test_db, summary="alpha bravo charlie")
    row = test_db.execute(
        "SELECT summary_tsv @@ plainto_tsquery('english', 'bravo') "
        "FROM graph_communities WHERE community_key = %s",
        (key,),
    ).fetchone()
    assert row is not None
    assert row[0] is True


def test_summary_tsv_empty_when_summary_null(test_db: psycopg.Connection) -> None:
    key = _insert_community(test_db, summary=None)
    row = test_db.execute(
        "SELECT summary_tsv FROM graph_communities WHERE community_key = %s", (key,)
    ).fetchone()
    assert row is not None
    assert str(row[0]) == ""  # to_tsvector('english','') -> empty tsvector


def test_summary_tsv_is_not_writable(test_db: psycopg.Connection) -> None:
    """A GENERATED column rejects a direct write (it derives from ``summary``)."""
    with pytest.raises(psycopg.errors.GeneratedAlways):
        test_db.execute(
            "INSERT INTO graph_communities "
            "(source_graph_hash, members_hash, summary_tsv) VALUES (%s, %s, %s)",
            ("gh", "mh", "alpha"),
        )


# --------------------------------------------------------------------------- #
# UNIQUE (tenant_id, level, members_hash)
# --------------------------------------------------------------------------- #
def test_uq_communities_rejects_duplicate(test_db: psycopg.Connection) -> None:
    _insert_community(test_db, members_hash="dup")
    with pytest.raises(psycopg.errors.UniqueViolation):
        _insert_community(test_db, members_hash="dup")


def test_uq_communities_allows_same_hash_different_tenant(
    test_db: psycopg.Connection,
) -> None:
    _insert_community(test_db, members_hash="dup", tenant_id="default")
    _insert_community(test_db, members_hash="dup", tenant_id="acme")
    count = test_db.execute("SELECT count(*) FROM graph_communities").fetchone()
    assert count is not None
    assert count[0] == 2


# --------------------------------------------------------------------------- #
# Tenant-safe composite FKs: a member can only reference a community / entity
# in its OWN tenant.
# --------------------------------------------------------------------------- #
def test_member_same_tenant_fk_is_accepted(test_db: psycopg.Connection) -> None:
    ent = _insert_entity(test_db, canonical_key="a", tenant_id="tenant-b")
    key = _insert_community(test_db, tenant_id="tenant-b")
    test_db.execute(
        "INSERT INTO graph_community_members (tenant_id, community_key, entity_id) "
        "VALUES (%s, %s, %s)",
        ("tenant-b", key, ent),
    )
    count = test_db.execute(
        "SELECT count(*) FROM graph_community_members"
    ).fetchone()
    assert count is not None
    assert count[0] == 1


def test_member_cross_tenant_community_fk_is_rejected(
    test_db: psycopg.Connection,
) -> None:
    """A member in tenant-a cannot reference a community owned by tenant-b."""
    ent = _insert_entity(test_db, canonical_key="a", tenant_id="tenant-a")
    key_b = _insert_community(test_db, tenant_id="tenant-b")
    with pytest.raises(psycopg.errors.ForeignKeyViolation):
        test_db.execute(
            "INSERT INTO graph_community_members (tenant_id, community_key, entity_id) "
            "VALUES (%s, %s, %s)",
            ("tenant-a", key_b, ent),  # (tenant-a, key_b) has no community row
        )


def test_member_cross_tenant_entity_fk_is_rejected(
    test_db: psycopg.Connection,
) -> None:
    """A member cannot reference an entity owned by a different tenant."""
    ent_b = _insert_entity(test_db, canonical_key="a", tenant_id="tenant-b")
    key_a = _insert_community(test_db, tenant_id="tenant-a")
    with pytest.raises(psycopg.errors.ForeignKeyViolation):
        test_db.execute(
            "INSERT INTO graph_community_members (tenant_id, community_key, entity_id) "
            "VALUES (%s, %s, %s)",
            ("tenant-a", key_a, ent_b),  # (tenant-a, ent_b) has no entity row
        )


# --------------------------------------------------------------------------- #
# Cascade
# --------------------------------------------------------------------------- #
def test_members_cascade_on_community_delete(test_db: psycopg.Connection) -> None:
    ent = _insert_entity(test_db, canonical_key="a")
    key = _insert_community(test_db)
    test_db.execute(
        "INSERT INTO graph_community_members (community_key, entity_id) "
        "VALUES (%s, %s)",
        (key, ent),
    )
    test_db.execute("DELETE FROM graph_communities WHERE community_key = %s", (key,))
    count = test_db.execute(
        "SELECT count(*) FROM graph_community_members WHERE community_key = %s", (key,)
    ).fetchone()
    assert count is not None
    assert count[0] == 0


def test_members_cascade_on_entity_delete(test_db: psycopg.Connection) -> None:
    ent = _insert_entity(test_db, canonical_key="a")
    key = _insert_community(test_db)
    test_db.execute(
        "INSERT INTO graph_community_members (community_key, entity_id) "
        "VALUES (%s, %s)",
        (key, ent),
    )
    test_db.execute("DELETE FROM graph_entities WHERE id = %s", (ent,))
    count = test_db.execute(
        "SELECT count(*) FROM graph_community_members WHERE entity_id = %s", (ent,)
    ).fetchone()
    assert count is not None
    assert count[0] == 0


# --------------------------------------------------------------------------- #
# Non-negative count / weight CHECKs
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    ("column", "value"),
    [("member_count", -1), ("edge_count", -1), ("total_weight", -0.1)],
)
def test_community_counts_non_negative(
    test_db: psycopg.Connection, column: str, value: float
) -> None:
    with pytest.raises(psycopg.errors.CheckViolation):
        test_db.execute(
            f"INSERT INTO graph_communities "
            f"(source_graph_hash, members_hash, {column}) VALUES (%s, %s, %s)",
            ("gh", "mh", value),
        )


@pytest.mark.parametrize(
    ("column", "value"), [("member_rank", -1), ("member_weight", -0.1)]
)
def test_member_counts_non_negative(
    test_db: psycopg.Connection, column: str, value: float
) -> None:
    ent = _insert_entity(test_db, canonical_key="a")
    key = _insert_community(test_db)
    with pytest.raises(psycopg.errors.CheckViolation):
        test_db.execute(
            f"INSERT INTO graph_community_members "
            f"(community_key, entity_id, {column}) VALUES (%s, %s, %s)",
            (key, ent, value),
        )


# --------------------------------------------------------------------------- #
# NOT NULL fingerprints (no default)
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("column", ["source_graph_hash", "members_hash"])
def test_community_fingerprints_are_not_null(
    test_db: psycopg.Connection, column: str
) -> None:
    """``source_graph_hash`` + ``members_hash`` have no default → NOT NULL."""
    other = "members_hash" if column == "source_graph_hash" else "source_graph_hash"
    with pytest.raises(psycopg.errors.NotNullViolation):
        test_db.execute(
            f"INSERT INTO graph_communities ({other}) VALUES (%s)", ("x",)
        )


# --------------------------------------------------------------------------- #
# Idempotency
# --------------------------------------------------------------------------- #
def test_migration_013_is_idempotent(test_db: psycopg.Connection) -> None:
    """Re-running the SQL on a fresh DB is safe (IF NOT EXISTS guards)."""
    sql = _MIGRATION_013.read_text()
    test_db.execute(sql)  # second apply (first ran via the fixture)
    test_db.execute(sql)  # third apply — still safe
    rows = test_db.execute(
        "SELECT table_name FROM information_schema.tables "
        "WHERE table_schema = 'public' AND table_name = ANY(%s)",
        (list(_COMMUNITY_TABLES),),
    ).fetchall()
    assert {str(r[0]) for r in rows} == set(_COMMUNITY_TABLES)
