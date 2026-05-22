"""Raw-text sliding-window entity co-occurrence (wave G1, GraphRAG).

Pure logic, no DB: given one document's ordered entity *occurrences* (each an
``(entity_id, position)`` pair over the raw-text token stream) this module
computes the per-document, per-entity-pair raw co-occurrence counts that feed
``graph_edge_contributions`` (migration 012; spec §4 D4, §5a).

**Spec interpretation (documented choice).** The design (spec §4 D4, §13) fixes
that edges come from a *window co-occurrence over raw text* with the default
window ``BRAIN_GRAPH_COOCCUR_WINDOW = 3`` (spec §10), but does not pin the exact
window predicate or the unit of ``position``. This module makes the most
spec-faithful, testable choice and documents it:

* ``position`` is a caller-supplied integer offset into the raw-text token
  stream (chunker-independent, per spec §4 D4). The pure function is unit-
  agnostic — the caller (G1 ``reconcile.py``) owns tokenization and decides what
  a position means; this module only compares positions.
* Two occurrences **co-occur** iff they belong to *distinct* entities and their
  positions differ by at most ``window`` (an inclusive maximum distance, i.e. a
  radius). ``window = 3`` ⇒ ``abs(pos_i - pos_j) <= 3``.
* Each unordered occurrence-pair is counted **at most once**, so overlapping
  sliding windows never double-count a pair (spec §13 "overlap regression").

Endpoints are canonicalized ``src_id < dst_id`` to match the
``graph_edge_contributions`` ``CHECK (src_id < dst_id)``. UUIDs are compared as
strings; for the canonical lowercase UUID text form this lexicographic order
matches PostgreSQL's ``uuid`` byte ordering (hyphens sit at fixed positions and
do not affect relative order), so the canonical pair always satisfies the DB
constraint.

The module is a single-responsibility derive-time helper (SOLID): occurrences
in, raw counts out. Generic-entity suppression and lift weighting are *not* done
here — they are separate derive-time concerns owned by
:mod:`brain.graph_rag.weighting`.
"""
from __future__ import annotations

from collections import Counter
from collections.abc import Mapping, Sequence
from dataclasses import dataclass

from ..errors import CooccurrenceError
from .schema import EdgeContribution

__all__ = [
    "DEFAULT_COOCCUR_WINDOW",
    "DEFAULT_MAX_ENTITIES_PER_DOC",
    "EntityOccurrence",
    "cooccurrence_counts",
    "to_contributions",
]

# Default sliding-window radius: two occurrences co-occur when their positions
# differ by at most this many units. Mirrors ``BRAIN_GRAPH_COOCCUR_WINDOW``
# (spec §10, default 3); reconcile.py passes the configured value.
DEFAULT_COOCCUR_WINDOW = 3

# Default cap on the number of DISTINCT entities considered per document
# (spec §8 "cooccur.py — ... + caps", §10 ``BRAIN_GRAPH_MAX_ENTITIES_PER_DOC``).
# Bounds the O(pairs) blow-up on entity-dense documents. When more distinct
# entities are present, the most-mentioned ones are kept (ties broken by
# ``entity_id`` ascending for determinism); pass ``None`` to disable.
DEFAULT_MAX_ENTITIES_PER_DOC = 40


@dataclass(frozen=True)
class EntityOccurrence:
    """One entity mention at a raw-text position (input to co-occurrence).

    ``entity_id`` is the durable entity UUID (``graph_entities.id``).
    ``position`` is a caller-defined integer offset into the raw-text token
    stream — only relative distances matter here, so the unit is the caller's
    choice (spec §4 D4). The same ``entity_id`` may appear at many positions
    (a repeated mention); same-entity pairs never produce an edge (no self
    loops — the DB ``CHECK (src_id < dst_id)`` forbids them).
    """

    entity_id: str
    position: int


def _canonical_pair(a: str, b: str) -> tuple[str, str]:
    """Return ``(src, dst)`` ordered ``src < dst`` (matches the DB CHECK).

    Caller guarantees ``a != b`` (same-entity pairs are skipped upstream).
    """
    return (a, b) if a < b else (b, a)


def _apply_entity_cap(
    occurrences: Sequence[EntityOccurrence], max_entities: int
) -> list[EntityOccurrence]:
    """Keep occurrences of the top ``max_entities`` most-mentioned entities.

    Entities are ranked by total occurrence count (descending), ties broken by
    ``entity_id`` ascending so the selection is deterministic. Occurrences of
    dropped entities are removed; the relative order of the survivors is
    preserved. Returns the input list unchanged when it already references at
    most ``max_entities`` distinct entities.
    """
    freq = Counter(occ.entity_id for occ in occurrences)
    if len(freq) <= max_entities:
        return list(occurrences)
    ranked = sorted(freq.items(), key=lambda kv: (-kv[1], kv[0]))
    kept = {entity_id for entity_id, _count in ranked[:max_entities]}
    return [occ for occ in occurrences if occ.entity_id in kept]


def cooccurrence_counts(
    occurrences: Sequence[EntityOccurrence],
    *,
    window: int = DEFAULT_COOCCUR_WINDOW,
    max_entities: int | None = DEFAULT_MAX_ENTITIES_PER_DOC,
) -> dict[tuple[str, str], int]:
    """Compute raw within-window co-occurrence counts for one document.

    Two occurrences co-occur when they belong to distinct entities and their
    positions differ by at most ``window``. Each unordered occurrence-pair is
    counted once (overlapping windows do not double-count). The result maps each
    canonical ``(src_id, dst_id)`` pair (``src_id < dst_id``) to its raw count.

    Args:
        occurrences: One document's entity occurrences (any order). Multiple
            occurrences of the same ``entity_id`` are allowed and only ever
            pair with *other* entities.
        window: Inclusive maximum positional distance for a co-occurrence
            (default :data:`DEFAULT_COOCCUR_WINDOW`). Must be ``>= 1``.
        max_entities: Cap on distinct entities considered (default
            :data:`DEFAULT_MAX_ENTITIES_PER_DOC`); ``None`` disables the cap.
            Must be ``>= 1`` when set.

    Returns:
        A fresh ``dict`` from canonical entity pair to raw co-occurrence count;
        empty when no pair falls within the window.

    Raises:
        CooccurrenceError: if ``window < 1`` or ``max_entities`` is set ``< 1``.
    """
    if window < 1:
        raise CooccurrenceError(
            f"co-occurrence window must be a positive integer (got {window})"
        )
    if max_entities is not None and max_entities < 1:
        raise CooccurrenceError(
            f"max_entities must be a positive integer or None (got {max_entities})"
        )

    considered = (
        occurrences if max_entities is None else _apply_entity_cap(occurrences, max_entities)
    )

    # Sort by position so a forward sweep can stop early once the gap to a later
    # occurrence exceeds the window (the remaining occurrences are even farther).
    ordered = sorted(considered, key=lambda occ: occ.position)
    counts: dict[tuple[str, str], int] = {}
    total = len(ordered)
    for i in range(total):
        left = ordered[i]
        for j in range(i + 1, total):
            right = ordered[j]
            if right.position - left.position > window:
                break
            if left.entity_id == right.entity_id:
                continue  # no self loops
            pair = _canonical_pair(left.entity_id, right.entity_id)
            counts[pair] = counts.get(pair, 0) + 1
    return counts


def to_contributions(
    counts: Mapping[tuple[str, str], int],
    *,
    document_id: str,
    tenant_id: str = "default",
) -> list[EdgeContribution]:
    """Wrap raw pair counts as :class:`EdgeContribution` rows for one document.

    The thin adapter between the unit-agnostic core (:func:`cooccurrence_counts`)
    and the ``graph_edge_contributions`` source-of-truth shape (spec §5a). Rows
    are emitted in canonical ``(src_id, dst_id)`` order for deterministic
    persistence; ``tenant_id`` defaults to the single-user ``"default"`` tenant
    (spec §4 D9).
    """
    return [
        EdgeContribution(
            document_id=document_id,
            src_id=src_id,
            dst_id=dst_id,
            tenant_id=tenant_id,
            cooccur_count=count,
        )
        for (src_id, dst_id), count in sorted(counts.items())
    ]
