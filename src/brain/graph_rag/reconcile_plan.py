"""Read phase of :func:`~brain.graph_rag.reconcile.reconcile_document`: what to write.

Extracted from :mod:`brain.graph_rag.reconcile` (2026-10-22) so the read phase
can run in its OWN short transaction, separate from the write transaction, and
so that module stays under the 800-line ceiling. ``reconcile_document`` now
runs in three steps:

1. **Plan** (:func:`plan_reconcile`, inside a short read transaction): fetch
   the doc's inputs (:class:`~brain.graph_rag.relational.DocInputs`) and
   fingerprint them, resolve its persons, compare
   both aspects' ``graph_index_state`` watermarks, and, only when the concept
   aspect is stale, read the body pinned to that hash. The transaction commits
   before step 2, so every ``AccessShareLock`` it took on ``documents`` / ``graph_*`` is released.
2. **Extract**, with NO transaction open: the concept extractor's model call.
   A transaction open across that call would hold its relation locks for
   seconds to minutes and stall DDL behind it (migration 028's ``lock_timeout``
   is 3s).
3. **Write** (a new transaction): take the graph write lock
   (:mod:`brain.graph_rag.write_lock`), then :meth:`ReconcilePlan.is_current`
   re-reads the inputs and compares fingerprints. If the document was edited, or deleted, after
   step 1, the plan is stale and the reconcile writes nothing: the newer edit's
   own reconcile (it runs after every write), or the next build, indexes the
   current body. Without that check, a slow extraction of the OLD body could
   commit over a newer edit's graph (``tests/test_graphrag_deadlock.py``).

**Every input the plan read is re-checked (RM-27, residual 3).** Until RM-27
only a ``content_hash`` change invalidated a plan, so an edit that changed a
document's participants without touching its body could be overtaken: an older
reconcile's persons, read before the lock, committed after the newer edit's.
:attr:`ReconcilePlan.inputs_fingerprint` (:func:`compute_inputs_fingerprint`)
now covers ``content_hash``, ``content_type``, the source kind and the raw
participant keys, and the write compares it. The fingerprint is the plan's
FIRST read, before the person resolver: the plan transaction is READ COMMITTED,
so an edit can commit between two of its statements. Read first, the
fingerprint holds the older row and the write drops the plan. Read after the
resolver, it would hold the newer row, match at write time, and let the older
persons commit. The corpus-wide ``directory_entries`` is not a document input
and is not covered; a directory change is picked up by the next build through
the people watermark, as before.

A document that no longer exists raises :class:`DocumentNotFound`, which the
corpus build counts as ``skipped_deleted`` (RM-27, residual 2).

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
from .extract_policy import skip_watermark
from .person_resolver import ResolvedPerson
from .relational import DocInputs, fetch_doc_content_at, fetch_doc_inputs, index_state
from .weighting import suppress_ver

if TYPE_CHECKING:
    from .reconcile import PersonResolver, ReconcileConfig

__all__ = [
    "PEOPLE_ASPECT",
    "PEOPLE_ASPECT_VERSION",
    "DocumentNotFound",
    "ReconcilePlan",
    "compute_inputs_fingerprint",
    "plan_reconcile",
]


class DocumentNotFound(GraphReconcileError):
    """The document to reconcile does not exist (deleted, or never existed).

    A :class:`~brain.errors.GraphReconcileError`, so every existing handler
    still catches it. :func:`brain.graph_rag.build.build_graph` catches exactly
    this one and counts the document as ``skipped_deleted``: a document deleted
    between the build's id enumeration and its read phase needs no graph.
    """

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
    #: :func:`compute_inputs_fingerprint` over the inputs read at plan time;
    #: :meth:`is_current` re-checks it under the graph write lock.
    inputs_fingerprint: str
    sver: str
    persons: list[ResolvedPerson]
    person_inputs_hash: str
    person_stale: bool
    concept_stale: bool
    concept_extractor_ver: str
    concept_inputs_hash: str
    #: The body to extract from, read only when ``concept_stale`` and only if
    #: the document still has ``content_hash`` (else ``None``: superseded).
    content: str | None
    #: The content type skips concept extraction (``extract_policy``, RM-30): the
    #: two ``concept_*`` fields hold the skip pair, and ``content`` is ``""``.
    concept_skipped: bool = False

    @property
    def nothing_to_do(self) -> bool:
        """Write nothing: both watermarks match, or the plan is already superseded.

        Superseded: the body changed between the hash read and the body read.
        The newer edit's own reconcile indexes it.
        """
        return self.content is None or (not self.person_stale and not self.concept_stale)

    def is_current(self, conn: psycopg.Connection[Any]) -> bool:
        """True iff the document still exists with the inputs the plan read.

        Compares :attr:`inputs_fingerprint` with one recomputed from a fresh
        read: ``content_hash``, ``content_type``, the source kind and the raw
        participant keys. ``content`` was read only if the document still had
        that hash, so a match means the body the plan extracted is still the
        document's body (or a byte-identical revert of it), and the persons it
        resolved came from the participants the document still has. A change to
        metadata the plan never read (a label, a summary) is not a reason to
        drop the plan, and does not. Call it AFTER taking the graph write
        lock: from then on no other graph writer can index this document, so a
        plan that is current here stays current until this transaction commits.

        It is also the write transaction's first touch of the document, and it
        takes ``FOR KEY SHARE`` on its row
        (``relational.fetch_doc_inputs(..., key_share=True)``) before any graph
        row is written. A concurrent ``DELETE`` of the document
        therefore waits for this transaction instead of deadlocking with its
        cascade.
        """
        inputs = fetch_doc_inputs(conn, self.document_id, key_share=True)
        return inputs is not None and compute_inputs_fingerprint(inputs) == self.inputs_fingerprint


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
    :class:`DocumentNotFound` when the document does not exist.
    """
    # FIRST read, before the resolver (module docstring): the fingerprint must
    # describe a row no newer than the one the persons are resolved from.
    inputs = fetch_doc_inputs(conn, document_id)
    if inputs is None:
        raise DocumentNotFound(f"cannot reconcile document {document_id!r}: not found")
    content_hash, content_type = inputs.content_hash, inputs.content_type
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
    # RM-30: a type the policy skips is compared against, and later written
    # with, the policy's pair, so a skipped document counts as done.
    skip_pair = skip_watermark(content_type) if extractor is not None else None
    if skip_pair is not None:
        c_inputs_hash, extractor_ver = skip_pair
    elif extractor is not None:
        extractor_ver = extractor.version
        c_inputs_hash = concept_inputs_hash(
            cfg.cooccur_window,
            cfg.max_entities_per_doc,
            stopwords=cfg.graph_extract_stopwords,
        )
    if extractor is not None:
        concept_watermark = (content_hash, c_inputs_hash, extractor_ver, sver)
        concept_stale = force or (
            index_state(conn, tenant_id, document_id, CONCEPTS_ASPECT) != concept_watermark
        )

    return ReconcilePlan(
        document_id=document_id,
        content_hash=content_hash,
        content_type=content_type,
        inputs_fingerprint=compute_inputs_fingerprint(inputs),
        sver=sver,
        persons=persons,
        person_inputs_hash=person_inputs_hash,
        person_stale=person_stale,
        concept_stale=concept_stale,
        concept_extractor_ver=extractor_ver,
        concept_inputs_hash=c_inputs_hash,
        # Pinned to the hash read above (fetch_doc_content_at); ``None`` means the
        # doc changed in between, and the plan is superseded. A skipped type is
        # never extracted, so its body is not read.
        content=(
            fetch_doc_content_at(conn, document_id, content_hash)
            if concept_stale and skip_pair is None
            else ""
        ),
        concept_skipped=skip_pair is not None,
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


def compute_inputs_fingerprint(inputs: DocInputs) -> str:
    """SHA-256 over every document input a reconcile plan reads (RM-27).

    Covers ``content_hash``, ``content_type``, ``source_kind`` and the sorted
    raw participant keys (:func:`brain.people._doc_participant_keys`, the same
    extraction :func:`~brain.graph_rag.person_resolver.default_person_resolver`
    resolves from: Gmail ``from`` / ``to``, Krisp ``_participant_keys``). The
    rest of ``metadata`` is left out on purpose: the plan never reads it, so a
    change there cannot make a plan stale. Sorting makes participant order
    irrelevant. A document with no source row has no participants.
    """
    # Late import keeps :mod:`brain.graph_rag` import-cheap, mirroring
    # person_resolver's late import from the same module.
    from ..people import _doc_participant_keys

    keys = (
        _doc_participant_keys(source_kind=inputs.source_kind, metadata=inputs.metadata)
        if inputs.source_kind is not None
        else set()
    )
    payload = {
        "content_hash": inputs.content_hash,
        "content_type": inputs.content_type,
        "source_kind": inputs.source_kind,
        "participant_keys": sorted(keys),
    }
    serialized = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(serialized.encode("utf-8")).hexdigest()
