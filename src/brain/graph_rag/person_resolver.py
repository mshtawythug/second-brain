"""Default person resolver for the graph people aspect (spec §3 reuse map).

Extracted from :mod:`brain.graph_rag.reconcile` (Phase 1, 2026-05-23) to keep
that module under the 800-line cap. Holds the :class:`ResolvedPerson` value
object and :func:`default_person_resolver` — the production
:class:`~brain.graph_rag.reconcile.PersonResolver` implementation that derives a
document's person set from the existing People-Hub pipeline so the graph's
person roster for a document can never drift from its People-Hub roster.

Both are re-exported from :mod:`brain.graph_rag.reconcile` for backwards
compatibility, so existing imports
(``from brain.graph_rag.reconcile import ResolvedPerson, default_person_resolver``)
keep working.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import psycopg

__all__ = ["ResolvedPerson", "default_person_resolver"]


@dataclass(frozen=True)
class ResolvedPerson:
    """One person resolved for a document — the input to entity upsert.

    ``canonical_key`` is the normalized lowercase display name (the People-Hub
    canonical identity, unique per ``(tenant_id, entity_type, canonical_key)``);
    ``display_name`` is its humanized presentation form (stored as
    ``graph_entities.name``).
    """

    canonical_key: str
    display_name: str


def default_person_resolver(
    conn: psycopg.Connection[Any],
    document_id: str,
    *,
    owner_keys: frozenset[str] = frozenset(),
    sender_denylist: frozenset[str] = frozenset(),
) -> list[ResolvedPerson]:
    """Derive a document's person set from the existing People-Hub pipeline.

    Reuses :mod:`brain.wiki.build_people`'s directory index + per-doc participant
    extraction + key resolution (and the shared
    :mod:`brain.wiki._person_name` normalizer) so the graph's person roster for
    a document is identical to its People-Hub roster (spec §3 reuse map). Owner
    keys are expanded (first-name-only + email-local-part variants) then
    stripped both before resolution (raw participant key) and after (resolved
    canonical key), matching ``aggregate_people``'s owner filter. Automated /
    org senders are dropped by the directory index (``sender_denylist`` adds
    ``BRAIN_GRAPH_SENDER_DENYLIST`` extras to the always-on generic heuristic).
    Returns the deduplicated persons sorted by ``canonical_key`` for
    determinism.

    Returns an empty list when the document does not exist or has no resolvable
    participants (e.g. a manual note, or a Gmail header with no directory match).
    """
    # Late import keeps :mod:`brain.graph_rag` import-cheap and avoids a cycle
    # with the wiki package, mirroring ``queries.resolve_person_to_keys``.
    from ..wiki._person_name import expand_owner_keys
    from ..wiki.build_people import (
        _build_directory_index,
        _doc_participant_keys,
        _resolve_key_to_person,
        humanize_display_name,
    )

    row = conn.execute(
        "SELECT s.kind, d.metadata FROM documents d "
        "JOIN sources s ON s.id = d.source_id WHERE d.id = %s",
        (document_id,),
    ).fetchone()
    if row is None:
        return []
    source_kind, raw_metadata = row
    metadata: dict[str, Any] = dict(raw_metadata) if raw_metadata else {}

    expanded_owner = expand_owner_keys(owner_keys)
    directory = _build_directory_index(conn, sender_denylist=sender_denylist)
    raw_keys = _doc_participant_keys(source_kind=source_kind, metadata=metadata)

    resolved: dict[str, str] = {}
    for key in raw_keys:
        if key.strip().lower() in expanded_owner:
            continue
        canonical = _resolve_key_to_person(key, directory=directory)
        if canonical is None or canonical in expanded_owner:
            continue
        # Record-level owner filter (mirrors ``aggregate_people``): drop a person
        # whose primary email is an owner key even when only the email — not the
        # display name — was listed, since the display-name participant key would
        # otherwise resolve the owner back in.
        primary_email = directory.primary_email_by_key.get(canonical)
        if primary_email is not None and primary_email.lower() in expanded_owner:
            continue
        resolved[canonical] = humanize_display_name(canonical)

    return [
        ResolvedPerson(canonical_key=key, display_name=name)
        for key, name in sorted(resolved.items())
    ]
