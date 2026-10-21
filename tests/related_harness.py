"""Corpus-wide driver for the related-docs ranking, for tests only.

Not a ``test_*`` module, so pytest does not collect it. ``iter_hybrid_neighbors``
was ``brain.related._iter_hybrid_neighbors``: the corpus-wide entry point that
fed the Quartz ``static/related/<slug>.json`` emitter. The emitter was deleted
with the wiki at R3 (wiki-to-ui phase 9), which left the driver with no
production caller. It moved here rather than staying as dead production code.

It is the same composition of production helpers it always was:
``_eligible_source_docs`` (also used by ``brain connect``),
``_corpus_common_lexemes`` and ``_neighbors_for_source`` (both behind
``compute_related``). So the ranking and gating tests that drive it still
exercise the shipped code paths, and only the loop that joins them lives here.
"""
from __future__ import annotations

from typing import Any

import psycopg

from brain.related import (
    _corpus_common_lexemes,
    _eligible_source_docs,
    _neighbors_for_source,
)


def iter_hybrid_neighbors(
    conn: psycopg.Connection[Any],
    *,
    k: int,
    vector_sim_floor: float,
    exclude_confidential: bool = True,
) -> list[tuple[Any, ...]]:
    """Rank every eligible source document's top-``k`` neighbours.

    Returns ``(source_vault_path, related_vault_path, related_title,
    related_source, related_score, related_snippet)`` rows. The confidential
    gate defaults to fail-closed (``True`` = exclude), matching
    :func:`brain.related.compute_related`, and is applied to BOTH the source
    set and the candidate legs.
    """
    eligible = _eligible_source_docs(conn, exclude_confidential=exclude_confidential)
    corpus_common = _corpus_common_lexemes(conn)
    rows: list[tuple[Any, ...]] = []
    for source in eligible:
        neighbors = _neighbors_for_source(
            conn,
            source=source,
            k=k,
            vector_sim_floor=vector_sim_floor,
            corpus_common=corpus_common,
            exclude_confidential=exclude_confidential,
        )
        rows.extend(
            (
                source.vault_path,
                neighbor.vault_path,
                neighbor.title,
                neighbor.source,
                neighbor.score,
                neighbor.snippet,
            )
            for neighbor in neighbors
        )
    return rows
