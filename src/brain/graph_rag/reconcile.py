"""Incremental graph reconcile — people + concept aspects (G1-b/G2-c, spec §7).

Centralizes the "make the graph match this document" operation. The **person
aspect** (G1-b) always runs; the **concept aspect** (G2-c) runs additionally
when ``config.concepts_enabled`` is set and an
:class:`~brain.graph_rag.extract.EntityExtractor` is injected (its relational
helpers live in :mod:`brain.graph_rag.concepts`; the shared per-document
read/write primitives in :mod:`brain.graph_rag.relational`). Each aspect carries
its OWN per-aspect ``graph_index_state`` watermark and skips independently. This
module keeps the three layers in lock-step for one document:

1. the **relational source-of-truth** (migration 012: ``graph_entities`` /
   ``graph_entity_mentions`` / ``graph_edge_contributions``),
2. the **derived aggregate mirror** (``graph_relationships`` — normalized lift,
   recomputed from contributions), and
3. the **Apache AGE graph** (``Entity`` / ``Document`` vertices, ``MENTIONED_IN``
   / ``CO_OCCURS`` edges), synced through the G0-4 :class:`GraphBackend`
   primitives — reconcile *orchestrates* them and never emits raw Cypher itself.

Two entry points, both tenant-aware and idempotent:

* :func:`reconcile_document` — (re)index one document's people aspect. Skips when
  the per-aspect ``graph_index_state`` watermark (``content_hash`` +
  ``inputs_hash`` + ``extractor_ver`` + ``suppress_ver``; spec §7 step 1) is
  unchanged, mirroring the enrich idempotency pattern.
* :func:`remove_document` — drop one document from the people graph (all delete
  paths). Explicitly deletes the doc's source rows (rather than relying on the
  ``ON DELETE CASCADE`` from a possibly-not-yet-deleted ``documents`` row), so it
  is correct whether called before or after the row itself goes away.

**Person source-of-truth (spec §3 reuse map).** Person entities are derived for
free from the existing people pipeline — ``directory_entries`` +
``documents.metadata`` participant keys — via the same internal helpers
:mod:`brain.wiki.build_people` uses (``_build_directory_index`` /
``_doc_participant_keys`` / ``_resolve_key_to_person`` / ``humanize_display_name``),
so a doc's graph people roster can never drift from its People-Hub roster. Each
resolved person becomes a ``graph_entities`` row keyed on
``(tenant_id, entity_type='person', canonical_key)`` where ``canonical_key`` is
the normalized lowercase display name (the People-Hub canonical identity) and
``name`` is its humanized form.

**Doc-level person co-occurrence model (spec §6b; documented choice).** A person
derived from participants has no raw-text position, so the spec's "window
co-occurrence over raw text" (spec §4 D4) is interpreted at the *document* level
for the person aspect: every participant of a document is modelled as occurring
at the same notional position (0), so under :mod:`brain.graph_rag.cooccur`'s
``|pos_i - pos_j| <= window`` predicate (any ``window >= 1``) every *distinct*
pair of the doc's persons co-occurs **exactly once** — the complete graph over
the doc's persons with raw count 1. This is co-presence in a document, not text
proximity, and it is chunker-independent (spec §4 D4). The same
:func:`~brain.graph_rag.cooccur.cooccurrence_counts` /
:func:`~brain.graph_rag.cooccur.to_contributions` from G1-a produce the rows.

**Aggregate refresh = full tenant recompute (spec §7 step 4, §15).** Because the
aggregates derive purely from the per-document source-of-truth, every reconcile
that does work recomputes the tenant's ``graph_relationships`` (normalized lift +
generic suppression, G1-a :mod:`~brain.graph_rag.weighting`) from *all*
contributions, then rematerializes the AGE ``CO_OCCURS`` edges via
:meth:`~brain.graph_rag.backends.base.GraphBackend.refresh_cooccur_edges`. Full
refresh is correct and cascade-safe; affected-only incremental refresh is
explicitly out of scope for v1 (spec §15) and batching/perf is a later wave.

**Resolved spec ambiguities (flagged for review).**

* *Tenant corpus N for generic suppression* (spec §6b "round(GENERIC_DF ×
  tenant_corpus_N)"): defined here as the count of **distinct documents that have
  any mention in the tenant** (person + concept union; ``COUNT(DISTINCT
  document_id)`` over ``graph_entity_mentions`` regardless of ``source``) — the
  graph's document universe for the tenant. Tenant-scoped and well-defined;
  ``documents`` is not tenantized in G0.
* *GC scope*: now-zero-mention vertices of EVERY active aspect are garbage-
  collected — :func:`reconcile_document` GCs the person and (when concepts are
  enabled) concept aspects it ran, and :func:`remove_document` GCs both
  unconditionally (a removed document loses all of its graph presence).

**Config (single object, no divergence).** Both entry points take ONE frozen
:class:`ReconcileConfig` (tenant + co-occurrence window + per-doc cap + generic
ratio + owner keys). Bundling them guarantees a build and a later delete can
never recompute the full-tenant aggregate with a *different* ``generic_df_ratio``
(which would corrupt the suppression/weights). G1-c/G1-d resolve it once from
``BRAIN_GRAPH_*`` (spec §10) and thread the same object through every write/delete
path.

**Connection contract (atomicity).** Each entry point opens ``with
conn.transaction()`` as its FIRST database action and performs *all* reads and
writes inside it. That guarantees a true top-level transaction even on a
caller's ``autocommit=False`` connection (a pre-transaction read would otherwise
open an implicit transaction and demote the block to a SAVEPOINT), so the
relational rewrite and the AGE sync commit or roll back **together** — a failure
mid-sync never leaves a half-written graph or a dangling transaction. When the
caller is already inside a transaction the block nests as a SAVEPOINT and the
caller still owns the outer transaction (G1-c wiring relies on this).
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from typing import Any, Protocol

import psycopg

from ..errors import GraphReconcileError
from .aggregates import (
    RefreshResult,
    _gc_orphan_concepts,
    _gc_orphan_persons,
    _recompute_aggregates,
    refresh_aggregates,
)
from .backends.base import GraphBackend
from .concepts import (
    CONCEPT_ENTITY_TYPES,
    CONCEPTS_ASPECT,
    build_concept_rows,
    concept_inputs_hash,
    concept_mention_source,
    upsert_concept_entities,
)
from .cooccur import (
    DEFAULT_COOCCUR_WINDOW,
    DEFAULT_MAX_ENTITIES_PER_DOC,
    EntityOccurrence,
    cooccurrence_counts,
    to_contributions,
)
from .extract import EntityExtractor
from .person_resolver import ResolvedPerson, default_person_resolver
from .relational import (
    delete_doc_relational,
    fetch_doc_content,
    fetch_doc_meta,
    index_state,
    read_doc_mentions,
    rewrite_doc_relational,
    upsert_index_state,
)
from .schema import EdgeContribution, EntityMention, GraphEntity
from .weighting import DEFAULT_GENERIC_DF, suppress_ver

__all__ = [
    "CONCEPTS_ASPECT",
    "PEOPLE_ASPECT",
    "PEOPLE_ASPECT_VERSION",
    "PEOPLE_MENTION_SOURCE",
    "PersonResolver",
    "ReconcileConfig",
    "ReconcileResult",
    "RefreshResult",
    "ResolvedPerson",
    "default_person_resolver",
    "reconcile_document",
    "refresh_aggregates",
    "remove_document",
]

# The ``entity_type``s the person aspect owns — the scope of its aspect-scoped
# relational rewrite + GC. Concepts own :data:`CONCEPT_ENTITY_TYPES`.
_PERSON_ENTITY_TYPES: tuple[str, ...] = ("person",)

# The migration-012 ``graph_index_state.aspect`` value this module owns. People
# and concepts (G2) re-index independently under their own watermark rows.
PEOPLE_ASPECT = "people"

# Versions the people-aspect derivation. Stored in
# ``graph_index_state.extractor_ver`` (spec §7 step 1) — bumping it forces every
# document's people aspect to re-reconcile (e.g. if the participant-derivation
# logic changes). There is no LLM extractor for the people aspect (people are
# derived from participants), so this is the derivation-logic version.
#
# people-v2 (2026-05-23, Phase 1 data-quality remediation): the shared
# person-name normalizer (:mod:`brain.wiki._person_name`) now drives canonical
# keys — mailing-list "via X" decoration stripped, ``Last, First`` flipped,
# separators collapsed (so ``Jane.Doe`` / ``Jane Doe`` merge), email-as-name
# humanized from the local part, automated / org senders dropped, and owner
# first-name / local-part variants excluded. A re-`build --backfill` re-extracts
# every document's people aspect cleanly under the new keys.
PEOPLE_ASPECT_VERSION = "people-v2"

# ``graph_entity_mentions.source`` provenance for person mentions (spec §5a:
# ``'people'`` for the people pipeline vs ``'extractor:<model>@<ver>'`` for the
# concept extractor).
PEOPLE_MENTION_SOURCE = "people"


@dataclass(frozen=True)
class ReconcileConfig:
    """Resolved per-call config shared by reconcile + remove (no divergence).

    Bundling tenant + co-occurrence window + per-doc cap + generic ratio + owner
    keys into ONE frozen object means :func:`reconcile_document` and
    :func:`remove_document` cannot be handed a *different* ``generic_df_ratio``
    (or tenant) — a divergence would recompute the full-tenant aggregate with
    mismatched suppression and corrupt the weights. G1-c/G1-d build this once
    from ``BRAIN_GRAPH_*`` (spec §10) and thread the same object through every
    write/delete path. ``owner_keys`` is consumed only by the person resolver
    (``reconcile_document``); ``remove_document`` ignores it.
    """

    tenant_id: str = "default"
    cooccur_window: int = DEFAULT_COOCCUR_WINDOW
    max_entities_per_doc: int | None = DEFAULT_MAX_ENTITIES_PER_DOC
    generic_df_ratio: float = DEFAULT_GENERIC_DF
    owner_keys: frozenset[str] = frozenset()
    # Extra automated-sender denylist entries (``BRAIN_GRAPH_SENDER_DENYLIST``)
    # threaded to the person resolver's :func:`brain.wiki._person_name
    # .is_automated_sender` filter, on top of the always-on generic heuristic
    # (no-reply / notifications / mailer / …). Consumed only by the resolver
    # (``reconcile_document``); ``remove_document`` ignores it.
    sender_denylist: frozenset[str] = frozenset()
    # Wave G2-c: gate the concept aspect (``BRAIN_GRAPH_CONCEPTS``). When True AND
    # an :class:`~brain.graph_rag.extract.EntityExtractor` is injected,
    # :func:`reconcile_document` also extracts + indexes the document's concept
    # entities (its own ``aspect='concepts'`` watermark) alongside the always-on
    # person aspect. Default False keeps the person-only behavior unchanged.
    concepts_enabled: bool = False
    # Phase B (2026-05-25): operator-curated extraction stopwords folded into the
    # concept watermark (``concept_inputs_hash``) so a stopword-set change forces
    # re-extraction beyond the one-time ``EXTRACTOR_VERSION`` bump. Default empty
    # (real terms are employer-specific, rule 15). Threaded from
    # ``Config.graph_extract_stopwords`` by the CLI/sync build path.
    graph_extract_stopwords: frozenset[str] = frozenset()


@dataclass(frozen=True)
class ReconcileResult:
    """Outcome of a :func:`reconcile_document` / :func:`remove_document` call.

    ``skipped`` is ``True`` only when :func:`reconcile_document` short-circuited
    on an unchanged ``graph_index_state`` watermark for **every** active aspect
    (no writes performed). ``person_count`` / ``mention_count`` /
    ``contribution_count`` describe the **people** aspect; the ``concept_*``
    fields describe the **concepts** aspect (wave G2-c; all zero when concepts
    are disabled). ``orphans_removed`` and ``relationship_count`` reflect the
    tenant-wide aggregate refresh across both aspects (the relationship mirror is
    shared). For ``remove_document`` the per-document counts are zero.
    """

    document_id: str
    tenant_id: str
    aspect: str = PEOPLE_ASPECT
    skipped: bool = False
    person_count: int = 0
    mention_count: int = 0
    contribution_count: int = 0
    relationship_count: int = 0
    orphans_removed: int = 0
    concept_count: int = 0
    concept_mention_count: int = 0
    concept_contribution_count: int = 0


class PersonResolver(Protocol):
    """Resolve one document to its person set (dependency-inversion seam).

    The default implementation (:func:`default_person_resolver`) reuses the
    People-Hub infrastructure; tests inject fakes to exercise the orchestration
    in isolation.
    """

    def __call__(
        self,
        conn: psycopg.Connection[Any],
        document_id: str,
        *,
        owner_keys: frozenset[str],
        sender_denylist: frozenset[str] = frozenset(),
    ) -> list[ResolvedPerson]:
        ...


# The default :class:`PersonResolver` implementation
# (:func:`default_person_resolver`) and its :class:`ResolvedPerson` value object
# live in :mod:`brain.graph_rag.person_resolver` (extracted to keep this module
# under the 800-line cap). They are imported above and re-exported via
# ``__all__`` so existing
# ``from brain.graph_rag.reconcile import ResolvedPerson, default_person_resolver``
# imports keep working.


# --------------------------------------------------------------------------- #
# Public entry points
# --------------------------------------------------------------------------- #
def reconcile_document(
    conn: psycopg.Connection[Any],
    document_id: str,
    *,
    backend: GraphBackend,
    config: ReconcileConfig | None = None,
    person_resolver: PersonResolver = default_person_resolver,
    extractor: EntityExtractor | None = None,
    force: bool = False,
    defer_tenant_refresh: bool = False,
) -> ReconcileResult:
    """(Re)index one document's people (+ optionally concept) aspect (spec §7).

    All work runs inside ``with conn.transaction()`` (opened as the first DB
    action — see the module "Connection contract"), so the relational rewrites
    and the AGE sync are one atomic unit. The **people** aspect always runs; the
    **concepts** aspect (wave G2-c) runs additionally when
    ``config.concepts_enabled`` is True AND an ``extractor`` is injected (DI
    seam, like ``person_resolver``). Each aspect has its OWN per-aspect
    ``graph_index_state`` watermark and skips independently (spec §7 step 1):

    * **People** — ``content_hash`` + ``inputs_hash`` over the *resolved persons*
      + co-occurrence config, ``extractor_ver`` = :data:`PEOPLE_ASPECT_VERSION`.
    * **Concepts** — ``content_hash`` + ``inputs_hash`` over only the
      co-occurrence config (the extracted entities are captured by
      ``content_hash`` + ``extractor_ver``, so this skip-check runs BEFORE the
      LLM call: an unchanged concept watermark short-circuits extraction),
      ``extractor_ver`` = ``extractor.version`` (``"<model>@concepts-v5"``).

    Both share ``suppress_ver`` (the derive-time weighting/suppression version).
    For each STALE aspect the pipeline upserts its ``graph_entities`` rows and
    aspect-scopes its mentions + co-occurrence-contribution rewrite (people at
    notional position 0 → complete graph; concepts at real raw-text word
    positions → windowed proximity). Then ONCE: recompute the tenant's shared
    aggregate ``graph_relationships`` (normalized lift + generic suppression), GC
    each ran-aspect's now-orphaned catalog rows, and sync AGE via the G0-4
    primitives — rebuilding the doc's COMBINED ``MENTIONED_IN`` (read back from
    the relational source-of-truth so a fresh aspect's edges are never clobbered),
    rematerializing ``CO_OCCURS``, and DETACH-DELETE-ing orphan vertices. Finally
    each ran aspect's watermark is upserted. When EVERY active aspect's watermark
    already matches (and not ``force``), nothing is written and ``skipped`` is
    True (concepts never extracts on a skip).

    ``config`` defaults to :class:`ReconcileConfig` (single-tenant, person-only);
    G1-c/G1-d/G2-c pass a resolved one. Idempotent: a second call with the same
    content + persons + concept inputs + config skips; re-running after a
    watermark change yields an identical graph (deterministic full recompute).

    ``force=True`` BYPASSES the per-aspect watermark skip and re-reconciles every
    active aspect unconditionally — the authoritative recovery path for a dropped
    or corrupted AGE mirror (spec §7). ``brain graphrag build --force`` threads
    this through every document.

    ``defer_tenant_refresh=True`` (bulk build only) HOISTS the three tenant-wide
    derived-layer steps — the ``graph_relationships`` recompute, the orphan GC,
    and the AGE ``CO_OCCURS`` rematerialization (+ orphan-vertex DETACH DELETE) —
    out of the per-document path so a corpus build pays them ONCE (after the loop,
    via :func:`brain.graph_rag.build.build_graph` →
    :func:`~brain.graph_rag.aggregates.refresh_aggregates`) instead of once per
    document. The per-document relational rewrite and the doc's own AGE
    entity/``MENTIONED_IN`` sync + watermark still run, so the relational
    source-of-truth stays current and the build stays resumable; the deferred
    end state is identical (the derived layers are a deterministic full recompute
    from that source-of-truth). On a deferred call ``relationship_count`` and
    ``orphans_removed`` are 0 — the post-loop refresh reports the tenant totals.
    Leave it ``False`` (the default) for the incremental ingest hook, which must
    keep every single-document write fully consistent immediately.

    Crash-window recovery: if a deferred bulk build is interrupted AFTER a
    document's watermark is written but BEFORE ``build_graph``'s final
    ``refresh_aggregates`` runs, a same-command rerun all-skips
    (``reconciled == 0``) and will NOT repair the derived layer
    (``graph_relationships`` / AGE ``CO_OCCURS``) — run ``brain graphrag refresh``
    (or ``brain graphrag build --force``) to repair. The relational
    source-of-truth stays correct throughout.

    Raises:
        GraphReconcileError: the document does not exist;
            ``config.max_entities_per_doc`` is set below 1; or
            ``config.concepts_enabled`` is True but no ``extractor`` was injected.
    """
    cfg = config if config is not None else ReconcileConfig()
    if cfg.max_entities_per_doc is not None and cfg.max_entities_per_doc < 1:
        raise GraphReconcileError(
            "config.max_entities_per_doc must be a positive integer or None "
            f"(got {cfg.max_entities_per_doc})"
        )
    concepts_active = cfg.concepts_enabled and extractor is not None
    if cfg.concepts_enabled and extractor is None:
        raise GraphReconcileError(
            "config.concepts_enabled is True but no EntityExtractor was injected "
            "(reconcile cannot index the concept aspect without one)"
        )
    tenant_id = cfg.tenant_id

    # Open the transaction FIRST so no implicit transaction is pre-opened by a
    # read (which would demote this block to a SAVEPOINT on an autocommit=False
    # connection); see the module "Connection contract". The return commits on
    # the way out of the ``with`` block.
    with conn.transaction():
        doc_meta = fetch_doc_meta(conn, document_id)
        if doc_meta is None:
            raise GraphReconcileError(
                f"cannot reconcile document {document_id!r}: not found"
            )
        content_hash, content_type = doc_meta
        sver = suppress_ver(cfg.generic_df_ratio)

        # --- People aspect: resolve + watermark (always active). ---
        persons = person_resolver(
            conn,
            document_id,
            owner_keys=cfg.owner_keys,
            sender_denylist=cfg.sender_denylist,
        )
        person_inputs_hash = _inputs_hash(
            persons, cfg.cooccur_window, cfg.max_entities_per_doc
        )
        person_watermark = (
            content_hash,
            person_inputs_hash,
            PEOPLE_ASPECT_VERSION,
            sver,
        )
        person_stale = force or (
            index_state(conn, tenant_id, document_id, PEOPLE_ASPECT)
            != person_watermark
        )

        # --- Concept aspect: PRE-extraction watermark check (no LLM on skip). ---
        concept_stale = False
        concept_extractor_ver = ""
        c_inputs_hash = ""
        if concepts_active:
            assert extractor is not None  # narrowed by concepts_active
            concept_extractor_ver = extractor.version
            c_inputs_hash = concept_inputs_hash(
                cfg.cooccur_window,
                cfg.max_entities_per_doc,
                stopwords=cfg.graph_extract_stopwords,
            )
            concept_watermark = (
                content_hash,
                c_inputs_hash,
                concept_extractor_ver,
                sver,
            )
            concept_stale = force or (
                index_state(conn, tenant_id, document_id, CONCEPTS_ASPECT)
                != concept_watermark
            )

        if not person_stale and not concept_stale:
            return ReconcileResult(
                document_id=document_id,
                tenant_id=tenant_id,
                skipped=True,
                person_count=len(persons),
            )

        # --- People relational rewrite (only when its watermark is stale). ---
        person_entities: list[GraphEntity] = []
        person_mentions: list[EntityMention] = []
        person_contributions: list[EdgeContribution] = []
        if person_stale:
            capped = _apply_person_cap(persons, cfg.max_entities_per_doc)
            person_entities = _upsert_person_entities(conn, tenant_id, capped)
            person_mentions = [
                EntityMention(
                    entity_id=entity.id,
                    document_id=document_id,
                    source=PEOPLE_MENTION_SOURCE,
                    tenant_id=tenant_id,
                    mention_count=1,
                )
                for entity in person_entities
            ]
            # Doc-level co-occurrence: every participant occupies position 0, so
            # the window predicate makes the complete graph over the doc's persons
            # with count 1 (already capped above, so disable cooccur's own cap).
            occurrences = [
                EntityOccurrence(entity_id=entity.id, position=0)
                for entity in person_entities
            ]
            counts = cooccurrence_counts(
                occurrences, window=cfg.cooccur_window, max_entities=None
            )
            person_contributions = to_contributions(
                counts, document_id=document_id, tenant_id=tenant_id
            )
            rewrite_doc_relational(
                conn,
                tenant_id,
                document_id,
                person_mentions,
                person_contributions,
                entity_types=_PERSON_ENTITY_TYPES,
            )

        # --- Concept relational rewrite (only when its watermark is stale). ---
        concept_entities: list[GraphEntity] = []
        concept_mentions: list[EntityMention] = []
        concept_contributions: list[EdgeContribution] = []
        if concept_stale:
            assert extractor is not None  # narrowed by concepts_active
            content = fetch_doc_content(conn, document_id)
            # OllamaExtractor.extract is never-raise: an Ollama outage yields []
            # (logged WARN). The watermark is still written, marking the doc
            # concept-indexed-empty; `brain graphrag build --concepts --force`
            # re-extracts it once Ollama is back (spec §7 / §17b decision 7).
            extracted = extractor.extract(content)
            concept_entities = upsert_concept_entities(conn, tenant_id, extracted)
            concept_mentions, concept_contributions = build_concept_rows(
                extracted,
                concept_entities,
                document_id=document_id,
                tenant_id=tenant_id,
                window=cfg.cooccur_window,
                source=concept_mention_source(concept_extractor_ver),
            )
            rewrite_doc_relational(
                conn,
                tenant_id,
                document_id,
                concept_mentions,
                concept_contributions,
                entity_types=CONCEPT_ENTITY_TYPES,
            )

        # --- Shared aggregate recompute + per-aspect orphan GC. ---
        # When ``defer_tenant_refresh`` is set (bulk build), the tenant-wide
        # aggregate recompute, the orphan GC, and (below) the AGE CO_OCCURS
        # rematerialization are HOISTED out of the per-document loop and run ONCE
        # after it — :func:`brain.graph_rag.build.build_graph` calls
        # :func:`~brain.graph_rag.aggregates.refresh_aggregates` when any document
        # did work. This turns a corpus build from O(docs × tenant_R) per-document
        # CO_OCCURS rebuilds into O(docs × doc_entities + tenant_R) — a single
        # final rebuild. The per-document relational rewrite and the doc's own AGE
        # entity/MENTIONED_IN sync STILL run (so the relational source-of-truth and
        # the doc's mention edges stay current); only the tenant-wide DERIVED layers
        # are deferred, and because those derive purely from the per-document
        # source-of-truth, the deferred end state is identical to the per-document
        # path (a deterministic full recompute). ``relationship_count`` /
        # ``orphans_removed`` are then 0 on this result — the final refresh reports
        # the tenant-wide totals.
        relationship_count = 0
        orphan_ids: list[str] = []
        if not defer_tenant_refresh:
            relationship_count = _recompute_aggregates(
                conn, tenant_id, cfg.generic_df_ratio
            )
            if person_stale:
                orphan_ids += _gc_orphan_persons(conn, tenant_id)
            if concept_stale:
                orphan_ids += _gc_orphan_concepts(conn, tenant_id)

        # --- AGE sync. Re-read the doc's COMBINED current mentions from the
        # relational source-of-truth so a fresh aspect's MENTIONED_IN edges are
        # rebuilt intact (upsert_mention_edges deletes+recreates ALL of a doc's
        # MENTIONED_IN, so it must always carry the complete person+concept set).
        # Only the ran aspects' entity properties are refreshed (the other
        # aspect's vertices are MERGEd defensively by upsert_mention_edges).
        entities_ran = [*person_entities, *concept_entities]
        combined_mentions = read_doc_mentions(conn, tenant_id, document_id)
        _sync_age_reconcile(
            backend,
            conn,
            tenant_id,
            document_id,
            entities=entities_ran,
            mentions=combined_mentions,
            content_type=content_type,
            orphan_ids=orphan_ids,
            skip_cooccur=defer_tenant_refresh,
        )

        # --- Per-aspect watermarks (only for the aspects that ran). ---
        if person_stale:
            upsert_index_state(
                conn,
                tenant_id,
                document_id,
                aspect=PEOPLE_ASPECT,
                content_hash=content_hash,
                inputs_hash=person_inputs_hash,
                extractor_ver=PEOPLE_ASPECT_VERSION,
                sver=sver,
            )
        if concept_stale:
            upsert_index_state(
                conn,
                tenant_id,
                document_id,
                aspect=CONCEPTS_ASPECT,
                content_hash=content_hash,
                inputs_hash=c_inputs_hash,
                extractor_ver=concept_extractor_ver,
                sver=sver,
            )

        return ReconcileResult(
            document_id=document_id,
            tenant_id=tenant_id,
            skipped=False,
            person_count=len(person_entities) if person_stale else len(persons),
            mention_count=len(person_mentions),
            contribution_count=len(person_contributions),
            relationship_count=relationship_count,
            orphans_removed=len(orphan_ids),
            concept_count=len(concept_entities),
            concept_mention_count=len(concept_mentions),
            concept_contribution_count=len(concept_contributions),
        )


def remove_document(
    conn: psycopg.Connection[Any],
    document_id: str,
    *,
    backend: GraphBackend,
    config: ReconcileConfig | None = None,
) -> ReconcileResult:
    """Remove one document from the graph — BOTH aspects (all delete paths; §7).

    Aspect-agnostic: a document being removed loses ALL of its graph presence
    (person AND concept), regardless of ``config.concepts_enabled`` — so no
    ``extractor`` is needed. Takes the SAME :class:`ReconcileConfig` as
    :func:`reconcile_document` so the full-tenant aggregate is recomputed with the
    identical ``generic_df_ratio`` used to build it (a mismatch would corrupt the
    surviving weights). Runs entirely inside ``with conn.transaction()`` (see the
    module "Connection contract").

    Explicitly deletes the document's relational source rows (all mentions, all
    contributions, BOTH aspects' watermarks) — robust whether the ``documents``
    row still exists or was already deleted (its ``ON DELETE CASCADE`` would have
    cleared these). Then recomputes the tenant aggregate, GCs now-orphaned person
    AND concept entities, and syncs AGE (DETACH DELETE the ``Document`` vertex +
    its ``MENTIONED_IN`` edges, rematerialize ``CO_OCCURS``, DETACH DELETE orphan
    vertices). Idempotent: a second call is a stable no-op that converges to the
    same graph.
    """
    cfg = config if config is not None else ReconcileConfig()
    tenant_id = cfg.tenant_id
    with conn.transaction():
        delete_doc_relational(conn, tenant_id, document_id)
        relationship_count = _recompute_aggregates(
            conn, tenant_id, cfg.generic_df_ratio
        )
        orphan_ids = [
            *_gc_orphan_persons(conn, tenant_id),
            *_gc_orphan_concepts(conn, tenant_id),
        ]

        backend.detach_delete_documents(conn, tenant_id, [document_id])
        backend.refresh_cooccur_edges(conn, tenant_id)
        if orphan_ids:
            backend.detach_delete_entities(conn, tenant_id, orphan_ids)

        return ReconcileResult(
            document_id=document_id,
            tenant_id=tenant_id,
            skipped=False,
            relationship_count=relationship_count,
            orphans_removed=len(orphan_ids),
        )


# --------------------------------------------------------------------------- #
# AGE sync (orchestration over the G0-4 primitives — no raw Cypher here)
# --------------------------------------------------------------------------- #
def _sync_age_reconcile(
    backend: GraphBackend,
    conn: psycopg.Connection[Any],
    tenant_id: str,
    document_id: str,
    *,
    entities: list[GraphEntity],
    mentions: list[EntityMention],
    content_type: str,
    orphan_ids: list[str],
    skip_cooccur: bool = False,
) -> None:
    """Mirror the just-rewritten relational state for one doc into AGE.

    ``entities`` are the ran aspects' upserted entities (person and/or concept,
    for property freshness); ``mentions`` is the doc's COMBINED current mention
    set read back from the relational source-of-truth (both aspects), so the
    single :meth:`GraphBackend.upsert_mention_edges` call — which deletes and
    recreates ALL of a doc's ``MENTIONED_IN`` edges — always carries the complete
    set and never clobbers a fresh aspect's edges. When the doc has at least one
    mention: MERGE the ran-aspect vertices, then recreate the doc's
    ``MENTIONED_IN`` (which defensively MERGEs every mentioned vertex, so a fresh
    aspect's vertices are ensured even when not re-upserted), tagging the
    ``Document`` vertex with ``content_type``. When an edit drops the doc to zero
    mentions: DETACH DELETE its ``Document`` vertex so entity-less docs hold no
    graph presence. Then rematerialize the tenant's ``CO_OCCURS`` edges from the
    recomputed mirror and DETACH DELETE any orphaned vertices.

    ``skip_cooccur`` (bulk build) defers the two TENANT-WIDE steps — the
    ``CO_OCCURS`` rematerialization and the orphan-vertex DETACH DELETE — to a
    single post-loop :func:`~brain.graph_rag.aggregates.refresh_aggregates`. The
    per-document Document / Entity / ``MENTIONED_IN`` sync above ALWAYS runs, so
    the doc's own mention edges are written incrementally; only the whole-tenant
    derived edges are hoisted out of the loop (``orphan_ids`` is empty in this
    mode, the GC having been deferred too).
    """
    if mentions:
        backend.upsert_entities(conn, tenant_id, entities)
        backend.upsert_mention_edges(
            conn,
            tenant_id,
            document_id,
            mentions,
            document_props={"content_type": content_type},
        )
    else:
        backend.detach_delete_documents(conn, tenant_id, [document_id])
    if not skip_cooccur:
        backend.refresh_cooccur_edges(conn, tenant_id)
        if orphan_ids:
            backend.detach_delete_entities(conn, tenant_id, orphan_ids)


# --------------------------------------------------------------------------- #
# Person-aspect relational helpers (parameterized SQL only). The aspect-agnostic
# per-document read/write + watermark helpers live in
# :mod:`brain.graph_rag.relational` (shared by both aspects).
# --------------------------------------------------------------------------- #
def _upsert_person_entities(
    conn: psycopg.Connection[Any],
    tenant_id: str,
    persons: list[ResolvedPerson],
) -> list[GraphEntity]:
    """Upsert person ``graph_entities`` rows, returning them with their ids.

    Keyed on ``(tenant_id, entity_type='person', canonical_key)`` so re-running
    reuses the existing row (and refreshes its humanized ``name``). The returned
    :class:`GraphEntity` objects carry the durable catalog ``id`` (= the AGE
    ``entity_uuid``).
    """
    entities: list[GraphEntity] = []
    for person in persons:
        row = conn.execute(
            """
            INSERT INTO graph_entities (tenant_id, entity_type, name, canonical_key)
            VALUES (%s, 'person', %s, %s)
            ON CONFLICT (tenant_id, entity_type, canonical_key) DO UPDATE SET
                name = EXCLUDED.name,
                updated_at = NOW()
            RETURNING id::text
            """,
            (tenant_id, person.display_name, person.canonical_key),
        ).fetchone()
        # RETURNING on an INSERT ... ON CONFLICT DO UPDATE always yields one row.
        assert row is not None
        entities.append(
            GraphEntity(
                id=str(row[0]),
                entity_type="person",
                name=person.display_name,
                canonical_key=person.canonical_key,
                tenant_id=tenant_id,
            )
        )
    return entities


# --------------------------------------------------------------------------- #
# Pure helpers
# --------------------------------------------------------------------------- #
def _apply_person_cap(
    persons: list[ResolvedPerson], max_entities: int | None
) -> list[ResolvedPerson]:
    """Cap the per-document person set deterministically (spec §10).

    Bounds ``MAX_ENTITIES_PER_DOC`` so an entity-dense document cannot blow up
    the O(pairs) co-occurrence. Persons are kept by ``canonical_key`` ascending
    (all carry equal per-doc weight), so both the mentions and the contributions
    derive from the same capped set. ``None`` disables the cap.
    """
    if max_entities is None or len(persons) <= max_entities:
        return list(persons)
    return sorted(persons, key=lambda person: person.canonical_key)[:max_entities]


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
