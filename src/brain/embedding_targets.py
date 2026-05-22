"""Allowlist + identifier-safety helpers for pgvector embedding columns.

The dim-reconciliation paths (:func:`brain.db.ensure_embedding_column` /
:func:`brain.queries.finalize_embedding_index`) are generalized over
``(table, column)`` so the GraphRAG tables can reuse them. To keep that
generalization safe, every table/column pair is validated against the
hard-coded allowlist here and the identifiers are only ever passed to SQL
through :class:`psycopg.sql.Identifier` — never string-formatted into a
statement. A non-allowlisted pair raises before any SQL is built.
"""
from .errors import BrainError

# Hard-coded allowlist of (table, column) pairs that carry a pgvector embedding
# column subject to dim reconciliation. ``graph_communities.summary_embedding``
# is forward-compat (G3) and is NOT exercised yet — it is listed so the helper
# accepts it without a code change once the communities migration lands.
EMBEDDING_TARGET_ALLOWLIST: frozenset[tuple[str, str]] = frozenset(
    {
        ("chunks", "embedding"),
        ("graph_entities", "embedding"),
        ("graph_communities", "summary_embedding"),
    }
)


def validate_embedding_target(table: str, column: str) -> None:
    """Reject any ``(table, column)`` pair not on the embedding allowlist.

    Guards every generalized dim-reconciliation entry point so a caller can
    never smuggle an arbitrary identifier into DDL — defense in depth alongside
    the :class:`psycopg.sql.Identifier` quoting used to build the statements.

    Raises:
        BrainError: ``(table, column)`` is not allowlisted.
    """
    if (table, column) not in EMBEDDING_TARGET_ALLOWLIST:
        allowed = ", ".join(
            f"{tbl}.{col}" for tbl, col in sorted(EMBEDDING_TARGET_ALLOWLIST)
        )
        raise BrainError(
            f"refusing to reconcile embedding column {table}.{column}: "
            f"not on the allowlist ({allowed})"
        )


def embedding_index_name(table: str, column: str) -> str:
    """Return the conventional HNSW index name for ``<table>.<column>``.

    Matches the legacy ``chunks_embedding_idx`` name so the existing chunks
    index is found unchanged after the generalization (both the drop in
    :func:`brain.db.ensure_embedding_column` and the create in
    :func:`brain.queries.finalize_embedding_index` derive the name here).
    """
    return f"{table}_{column}_idx"
