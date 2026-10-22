"""Read phase of :func:`~brain.graph_rag.reconcile.reconcile_document`: what to write.

Extracted from :mod:`brain.graph_rag.reconcile` (2026-10-22) so the read phase
can run in its OWN short transaction, separate from the write transaction, and
so that module stays under the 800-line ceiling. ``reconcile_document`` now
runs in three steps:

1. **Plan** (:func:`plan_reconcile`, inside a short read transaction): fetch
   the doc's ``content_hash`` / ``content_type`` (and, when the concept aspect
   is active, its body in the same SELECT), resolve its persons, and compare
   both aspects' ``graph_index_state`` watermarks. The transaction commits before step 2, so every
   ``AccessShareLock`` it took on ``documents`` / ``graph_*`` is released.
2. **Extract**, with NO transaction open: the concept extractor's model call.
   A transaction open across that call would hold its relation locks for
   seconds to minutes and stall DDL behind it (migration 028's ``lock_timeout``
   is 3s).
3. **Write** (a new transaction): take the graph write lock
   (:mod:`brain.graph_rag.write_lock`), then :meth:`ReconcilePlan.is_current`
   re-reads the ``content_hash``. If the document was edited, or deleted, after
   step 1, the plan is stale and the reconcile writes nothing: the newer edit's
   own reconcile (it runs after every write), or the next build, indexes the
   current body. Without that check, a slow extraction of the OLD body could
   commit over a newer edit's graph (``tests/test_graphrag_deadlock.py``).

Only a ``content_hash`` change invalidates a plan. A metadata-only edit that
changes the resolved persons without touching the body is not detected here;
the plan records that residual (``docs/plans/2026-10-22-graph-entities-deadlock.md``).

When the caller is already inside a transaction, both blocks nest as
SAVEPOINTs and the caller's transaction keeps its locks across step 2. That is
the caller's choice; every production caller syncs on an autocommit connection.
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

import psycopg

from ..errors import GraphReconcileError
from .concepts import CONCEPTS_ASPECT, concept_inputs_hash
from .extract import EntityExtractor
from .person_resolver import ResolvedPerson
from .relational import fetch_doc_meta, fetch_doc_snapshot, index_state
from .weighting import suppress_ver

if TYPE_CHECKING:
    from .reconcile import PersonResolver, ReconcileConfig

__all__ = ["PEOPLE_ASPECT", "PEOPLE_ASPECT_VERSION", "ReconcilePlan", "plan_reconcile"]

# The migration-012 ``graph_index_state.aspect`` value the person aspect owns.
# People and concepts (G2) re-index independently under their own watermark rows.
PEOPLE_ASPECT = "people"

# Versions the people-aspect derivation. Stored in
# ``graph_index_state.extractor_ver`` (spec §7 step 1) — bumping it forces every
# document's people aspect to re-reconcile (e.g. if the participant-derivation
# logic changes). There is no LLM extractor for the people aspect (people are
# derived from participants), so this is the derivation-logic version.
#
# people-v2 (2026-05-23, Phase 1 data-quality remediation): the shared
# person-name normalizer (:mod:`brain.person_name`) now drives canonical
# keys — mailing-list "via X" decoration stripped, ``Last, First`` flipped,
# separators collapsed (so ``Jane.Doe`` / ``Jane Doe`` merge), email-as-name
# humanized from the local part, automated / org senders dropped, and owner
# first-name / local-part variants excluded. A re-`build --backfill` re-extracts
# every document's people aspect cleanly under the new keys.
PEOPLE_ASPECT_VERSION = "people-v2"


@dataclass(frozen=True)
class ReconcilePlan:
    """Everything the write phase needs, read before the graph write lock."""

    document_id: str
    content_hash: str
    content_type: str
    sver: str
    persons: list[ResolvedPerson]
    person_inputs_hash: str
    person_stale: bool
    concept_stale: bool
    concept_extractor_ver: str
    concept_inputs_hash: str
    #: The body to extract from, read in the SAME select as ``content_hash``;
    #: empty unless ``concept_stale``.
    content: str

    @property
    def nothing_to_do(self) -> bool:
        """Both aspects' watermarks match: the document is already indexed."""
        return not self.person_stale and not self.concept_stale

    def is_current(self, conn: psycopg.Connection[Any]) -> bool:
        """True iff the document still exists with the ``content_hash`` the plan read.

        That hash came from the same row version as ``content``, so a match
        means the body the plan extracted is still the document's body (or a
        byte-identical revert of it). Call it AFTER taking the graph write
        lock: from then on no other graph writer can index this document, so a
        plan that is current here stays current until this transaction commits.
        """
        meta = fetch_doc_meta(conn, self.document_id)
        return meta is not None and meta[0] == self.content_hash


def plan_reconcile(
    conn: psycopg.Connection[Any],
    document_id: str,
    cfg: ReconcileConfig,
    *,
    person_resolver: PersonResolver,
    extractor: EntityExtractor | None,
    force: bool,
) -> ReconcilePlan:
    """Read the document and both aspects' watermarks; decide what is stale.

    ``extractor`` is ``None`` when the concept aspect is inactive. ``force``
    marks every active aspect stale (spec §7 authoritative rebuild). Raises
    :class:`~brain.errors.GraphReconcileError` when the document does not exist.
    """
    # When the concept aspect is active, read the hash and the body in ONE select
    # (fetch_doc_snapshot): two reads could pair one edit's hash with another's body.
    if extractor is not None:
        snapshot = fetch_doc_snapshot(conn, document_id)
    else:
        meta = fetch_doc_meta(conn, document_id)
        snapshot = None if meta is None else (meta[0], meta[1], "")
    if snapshot is None:
        raise GraphReconcileError(f"cannot reconcile document {document_id!r}: not found")
    content_hash, content_type, body = snapshot
    tenant_id = cfg.tenant_id
    sver = suppress_ver(cfg.generic_df_ratio)

    # --- People aspect: resolve + watermark (always active). ---
    persons = person_resolver(
        conn,
        document_id,
        owner_keys=cfg.owner_keys,
        sender_denylist=cfg.sender_denylist,
    )
    person_inputs_hash = _inputs_hash(persons, cfg.cooccur_window, cfg.max_entities_per_doc)
    person_watermark = (content_hash, person_inputs_hash, PEOPLE_ASPECT_VERSION, sver)
    person_stale = force or (
        index_state(conn, tenant_id, document_id, PEOPLE_ASPECT) != person_watermark
    )

    # --- Concept aspect: PRE-extraction watermark check (no LLM on skip). ---
    concept_stale = False
    extractor_ver = ""
    c_inputs_hash = ""
    if extractor is not None:
        extractor_ver = extractor.version
        c_inputs_hash = concept_inputs_hash(
            cfg.cooccur_window,
            cfg.max_entities_per_doc,
            stopwords=cfg.graph_extract_stopwords,
        )
        concept_watermark = (content_hash, c_inputs_hash, extractor_ver, sver)
        concept_stale = force or (
            index_state(conn, tenant_id, document_id, CONCEPTS_ASPECT) != concept_watermark
        )

    return ReconcilePlan(
        document_id=document_id,
        content_hash=content_hash,
        content_type=content_type,
        sver=sver,
        persons=persons,
        person_inputs_hash=person_inputs_hash,
        person_stale=person_stale,
        concept_stale=concept_stale,
        concept_extractor_ver=extractor_ver,
        concept_inputs_hash=c_inputs_hash,
        content=body if concept_stale else "",
    )


def _inputs_hash(
    persons: list[ResolvedPerson], window: int, max_entities: int | None
) -> str:
    """Stable fingerprint of the people-aspect inputs for the watermark.

    Captures everything that determines this document's people-aspect output:
    the resolved person *tuples* — ``(canonical_key, display_name)`` — plus the
    co-occurrence config. Hashing the display name (not just the canonical key)
    means a resolver that changes a person's presentation name (without changing
    their canonical identity) still flips the hash and re-indexes, so the AGE
    vertex / catalog ``name`` cannot go stale. A change in participants,
    directory resolution, owner filtering, or config flips the hash and forces a
    re-reconcile (spec §7 step 1). The suppression/weighting config is tracked
    separately via ``suppress_ver``.
    """
    payload = {
        "persons": sorted(
            [person.canonical_key, person.display_name] for person in persons
        ),
        "window": window,
        "max_entities": max_entities,
    }
    serialized = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(serialized.encode("utf-8")).hexdigest()
