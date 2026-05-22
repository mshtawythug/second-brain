"""Derive-time edge weighting + generic-entity suppression (wave G1, GraphRAG).

Pure logic, no DB. Two derive-time concerns the spec assigns to this module
(spec §8 ``weighting.py — normalized lift + generic suppression``):

1. **Normalized lift** — the single normative edge-weight metric, in ``(0, 1]``
   (spec §4 D4: PMI rejected because it can go negative). The design names the
   metric but not its formula; this module makes the canonical, spec-faithful
   choice and documents the derivation:

       lift(A, B) = P(A, B) / (P(A) · P(B))                        # ∈ [0, ∞)

   Given fixed marginals, the co-document count cannot exceed the rarer
   entity's document count, so the maximum attainable lift is
   ``1 / max(P(A), P(B))``. Dividing lift by that maximum normalizes it into
   ``(0, 1]`` and — because the corpus size ``N`` cancels — collapses to a
   clean document-count ratio:

       normalized_lift(A, B) = co_df(A, B) / min(df(A), df(B))     # ∈ (0, 1]

   where ``df(X)`` is X's document frequency and ``co_df(A, B)`` the count of
   documents in which A and B co-occur. The value is ``1.0`` exactly when one
   entity always appears with the other (its document set is a subset of the
   other's), and strictly positive whenever they ever co-occur. This satisfies
   the migration-012 ``CHECK (weight > 0 AND weight <= 1)`` by construction.

2. **Generic-entity suppression** — entities that appear in almost every
   document carry no thematic signal, so edges touching them are *dropped* at
   derive time (spec §6b "exclude … generic"; §4 D4 "generic-suppressed at
   derive time"). An entity is **generic** when its document frequency exceeds
   the absolute cap ``round(GENERIC_DF × tenant_corpus_N)`` (spec §6b step 2;
   ``BRAIN_GRAPH_GENERIC_DF`` default ``0.30``, spec §10). The spec frames this
   as exclusion (drop), not down-weighting, so :func:`edge_weight` returns
   ``None`` for a suppressed edge — the row is simply not materialized.

**Versioning.** :data:`WEIGHTING_VERSION` is the derive-time algorithm version
that feeds ``graph_index_state.suppress_ver`` (migration 012; spec §7 step 1).
A change to the weighting/suppression *semantics* bumps the constant; a change
to the suppression *config* (the ``GENERIC_DF`` ratio) changes the suppression
outcome too, so :func:`suppress_ver` folds both into the watermark string,
forcing a re-derive when either moves.
"""
from __future__ import annotations

from ..errors import WeightingError

__all__ = [
    "DEFAULT_GENERIC_DF",
    "WEIGHTING_VERSION",
    "edge_weight",
    "generic_df_cap",
    "is_generic_entity",
    "is_suppressed_edge",
    "normalized_lift",
    "suppress_ver",
]

# Derive-time weighting/suppression algorithm version. Feeds
# ``graph_index_state.suppress_ver`` (spec §7). Bump when the lift formula or
# the suppression rule changes so reconcile re-derives affected documents.
WEIGHTING_VERSION = "nlift-v1"

# Default generic-entity document-frequency ratio: an entity appearing in more
# than this fraction of the tenant corpus is treated as generic and its edges
# are suppressed. Mirrors ``BRAIN_GRAPH_GENERIC_DF`` (spec §10, default 0.30).
DEFAULT_GENERIC_DF = 0.30


def normalized_lift(co_doc_count: int, src_doc_count: int, dst_doc_count: int) -> float:
    """Normalized lift in ``(0, 1]`` = ``co_doc_count / min(src_df, dst_df)``.

    See the module docstring for the derivation. The result is provably in
    ``(0, 1]`` once the guards below pass, so the migration-012 weight ``CHECK``
    cannot be violated.

    Args:
        co_doc_count: Documents in which the two entities co-occur (``>= 1``).
        src_doc_count: Document frequency of one endpoint (``>= 1``).
        dst_doc_count: Document frequency of the other endpoint (``>= 1``).

    Returns:
        The normalized-lift edge weight.

    Raises:
        WeightingError: if ``co_doc_count < 1`` (no edge), either marginal is
            ``< 1``, or ``co_doc_count`` exceeds ``min(src_df, dst_df)`` (a pair
            cannot co-occur in more documents than its rarer entity appears in).
    """
    if co_doc_count < 1:
        raise WeightingError(
            f"co_doc_count must be >= 1 for an edge to exist (got {co_doc_count})"
        )
    if src_doc_count < 1 or dst_doc_count < 1:
        raise WeightingError(
            "endpoint document frequencies must be >= 1 "
            f"(got src={src_doc_count}, dst={dst_doc_count})"
        )
    min_df = min(src_doc_count, dst_doc_count)
    if co_doc_count > min_df:
        raise WeightingError(
            f"co_doc_count ({co_doc_count}) cannot exceed the rarer endpoint's "
            f"document frequency ({min_df})"
        )
    return co_doc_count / min_df


def generic_df_cap(corpus_doc_count: int, generic_df_ratio: float = DEFAULT_GENERIC_DF) -> int:
    """Absolute generic-entity document-frequency cap (spec §6b step 2).

    ``round(corpus_doc_count × generic_df_ratio)``. An entity whose document
    frequency exceeds this cap is generic (see :func:`is_generic_entity`).
    ``round`` uses Python's banker's rounding (round-half-to-even), matching the
    spec's plain "round".

    Args:
        corpus_doc_count: Number of documents in the tenant corpus (``>= 0``).
        generic_df_ratio: Generic fraction in ``(0, 1]``
            (default :data:`DEFAULT_GENERIC_DF`).

    Returns:
        The absolute document-frequency cap.

    Raises:
        WeightingError: if ``corpus_doc_count < 0`` or ``generic_df_ratio`` is
            outside ``(0, 1]``.
    """
    if corpus_doc_count < 0:
        raise WeightingError(
            f"corpus_doc_count must be >= 0 (got {corpus_doc_count})"
        )
    if not 0.0 < generic_df_ratio <= 1.0:
        raise WeightingError(
            f"generic_df_ratio must be in (0, 1] (got {generic_df_ratio})"
        )
    return round(corpus_doc_count * generic_df_ratio)


def is_generic_entity(entity_doc_count: int, cap: int) -> bool:
    """True when an entity's document frequency exceeds the generic ``cap``.

    Strictly greater-than: an entity sitting exactly at the cap is kept (spec
    §6b — only entities that co-occur with *almost everything* are excluded).
    """
    return entity_doc_count > cap


def is_suppressed_edge(src_doc_count: int, dst_doc_count: int, cap: int) -> bool:
    """True when *either* endpoint is generic, so the edge is suppressed."""
    return is_generic_entity(src_doc_count, cap) or is_generic_entity(dst_doc_count, cap)


def edge_weight(
    co_doc_count: int,
    src_doc_count: int,
    dst_doc_count: int,
    *,
    cap: int,
) -> float | None:
    """Derive-time edge weight, or ``None`` when the edge is suppressed.

    Returns ``None`` when either endpoint is generic (document frequency above
    ``cap``); otherwise the :func:`normalized_lift` weight. The ``None`` signal
    means "do not materialize this edge" (spec §6b excludes generic entities).

    Args:
        co_doc_count: Documents in which the two entities co-occur.
        src_doc_count: Document frequency of one endpoint.
        dst_doc_count: Document frequency of the other endpoint.
        cap: Absolute generic cap (see :func:`generic_df_cap`).

    Raises:
        WeightingError: propagated from :func:`normalized_lift` for a
            non-suppressed edge with impossible counts.
    """
    if is_suppressed_edge(src_doc_count, dst_doc_count, cap):
        return None
    return normalized_lift(co_doc_count, src_doc_count, dst_doc_count)


def suppress_ver(generic_df_ratio: float = DEFAULT_GENERIC_DF) -> str:
    """Composite derive-time version for ``graph_index_state.suppress_ver``.

    Folds the algorithm version (:data:`WEIGHTING_VERSION`) together with the
    config that changes the suppression outcome (the ``GENERIC_DF`` ratio), so
    a ratio change forces reconcile to re-derive (spec §7 watermark).

    The ratio is rendered with ``repr`` (``!r``), i.e. Python's shortest
    round-trippable float string, for two reasons:

    * **Collision-free** — ``repr`` is injective over floats (two *distinct*
      floats always render to distinct strings), so two different
      ``GENERIC_DF`` ratios can never fold to the same ``suppress_ver`` and
      silently skip a re-derive. The old ``:g`` format rounded to 6 significant
      figures, so e.g. ``0.1234561`` and ``0.1234562`` both rendered
      ``0.123456`` — a watermark collision that would skip a legitimate
      suppression change.
    * **Stable across spellings** — equal floats render identically
      (``0.30`` and ``0.3`` are the same float ⇒ both ``0.3``), so an
      equivalent re-spelling of the config does not spuriously force a
      re-derive.
    """
    return f"{WEIGHTING_VERSION}:gdf={generic_df_ratio!r}"
