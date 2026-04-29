"""Pure rule functions for the metadata-aware linker — R1, R2, R3."""
import datetime
from dataclasses import dataclass
from typing import Any, Literal

# Confidence weights for the three derived-link rules. Stored on every
# `derived_links.weight` insert so formatters can tier-style edges
# (spec §Q3). Constrained by migration 005's `CHECK (weight >= 0 AND weight <= 1)`.
WEIGHT_SHARED_THREAD = 1.0       # R1 — Gmail thread match (highest confidence)
WEIGHT_SAME_DAY_PARTICIPANT = 0.7  # R3 — participant + ±1-day match
WEIGHT_SHARED_PARTICIPANT = 0.4    # R2 — participant only (weakest)


@dataclass(frozen=True)
class DocSnapshot:
    """A read-only projection of a document used by rule evaluation.

    Built once per linker pass from `documents` + `metadata->'_participant_keys'`.
    Rules consume snapshots; they never query the DB themselves. ``source_kind``
    matches ``sources.kind`` in the DB schema, or ``None`` for vault-tier docs
    without a source.
    """

    document_id: str
    source_kind: Literal["gmail", "krisp", "manual"] | None
    metadata: dict[str, Any]
    participant_keys: frozenset[str]
    date: datetime.date | None


@dataclass(frozen=True)
class Evidence:
    """Outcome of a successful rule evaluation for a document pair.

    The pass-runner stores `(rule, weight, payload)` into `derived_links`
    columns + the `evidence` JSONB column. Payload shape varies per rule —
    e.g. {"thread_id": "..."} for R1, {"participant": "...", "date": "..."}
    for R3.
    """

    rule: str  # 'shared_thread' | 'shared_participant' | 'same_day_participant'
    weight: float  # 1.0 / 0.4 / 0.7
    payload: dict[str, Any]


def rule_shared_thread(a: DocSnapshot, b: DocSnapshot) -> Evidence | None:
    """R1 — Gmail↔Gmail edge when both share `metadata.thread_id`.

    Returns None for non-Gmail pairs, missing thread_ids, or thread_id
    mismatch. Weight ``WEIGHT_SHARED_THREAD``.
    """
    raise NotImplementedError("Implemented in Task A.4")


def rule_shared_participant(a: DocSnapshot, b: DocSnapshot) -> Evidence | None:
    """R2 — edge when both docs' participant_keys intersect.

    Applies across Krisp↔Gmail, Krisp↔Krisp, Gmail↔Gmail. Returns None for
    self-pairs, empty intersections. Weight ``WEIGHT_SHARED_PARTICIPANT``.
    The pass-runner is responsible for suppressing R2 when R3 fires for
    the same pair.
    """
    raise NotImplementedError("Implemented in Task A.4")


def rule_same_day_participant(a: DocSnapshot, b: DocSnapshot) -> Evidence | None:
    """R3 — Krisp↔Gmail edge when participants intersect AND dates within ±1 day.

    Strictly stronger than R2 for Krisp↔Gmail pairs. Returns None for
    non-Krisp/Gmail pairs, missing dates, mismatched participant sets, or
    dates more than 1 day apart. Timezone-naive comparison. Weight
    ``WEIGHT_SAME_DAY_PARTICIPANT``.
    """
    raise NotImplementedError("Implemented in Task A.4")
