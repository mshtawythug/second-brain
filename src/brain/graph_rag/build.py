"""Corpus-wide people-aspect graph build driver (wave G1-d, GraphRAG, spec §9/§12).

The batch equivalent of the per-document reconcile hook (wave G1-c): walk a
stream of document ids and reconcile each one's people aspect into the graph.
Backs ``brain graphrag build --backfill`` — the one-shot way to populate (or
re-derive) the people graph for a corpus that was ingested before graph sync was
enabled, or to propagate a corpus-wide config change.

**Single source of truth for the per-doc logic.** :func:`build_graph` calls the
SAME :func:`brain.graph_rag.reconcile.reconcile_document` the incremental hook
uses, with the SAME immutable :class:`~brain.graph_rag.reconcile.ReconcileConfig`
— so a batched backfill produces a graph identical to having reconciled each
document one-by-one as it was ingested. No co-occurrence / weighting / AGE-sync
logic is reimplemented here; this module only iterates and tallies.

**Resumability (idempotent via the watermark).** ``reconcile_document`` skips a
document whose per-aspect ``graph_index_state`` watermark is unchanged, so a
build is safe to re-run and to resume after an interruption: re-running revisits
the documents in the same ascending-id order (see
:func:`brain.queries.iter_all_document_ids`) and the already-indexed prefix is
skipped cheaply (one ``SELECT`` per doc). This only holds when each document
commits independently, which it does when the caller passes an **autocommit**
connection — ``reconcile_document`` then opens its own top-level transaction per
document (see the reconcile module's "Connection contract"). The
``brain graphrag build`` CLI sets ``conn.autocommit = True`` before calling this.
"""
from __future__ import annotations

from collections.abc import Callable, Iterable
from dataclasses import dataclass
from typing import Any

import psycopg

from ..errors import GraphReconcileError
from .aggregates import (
    _gc_orphan_concepts,
    _gc_orphan_persons,
    _recompute_aggregates,
    refresh_aggregates,
)
from .backends.base import GraphBackend
from .extract import EntityExtractor
from .person_resolver import prebuilt_directory_resolver
from .reconcile import ReconcileConfig, ReconcileResult, reconcile_document
from .schema import GraphEntity

__all__ = ["BuildResult", "ProgressCallback", "build_graph"]


@dataclass(frozen=True)
class BuildResult:
    """Tally of a :func:`build_graph` run.

    ``processed`` is the number of documents reconcile was attempted on (bounded
    by ``limit``); ``reconciled`` is how many did graph work; ``skipped`` is how
    many short-circuited on an unchanged watermark (``reconciled + skipped ==
    processed``); ``orphans_removed`` is the total zero-mention entity vertices
    GC'd across the run; ``relationship_count`` is the number of tenant
    ``graph_relationships`` edges materialized by the post-loop refresh (0 when no
    document did work, so no refresh ran). Because the per-document reconcile
    defers the tenant-wide aggregate recompute + orphan GC + AGE ``CO_OCCURS``
    rebuild, ``orphans_removed`` and ``relationship_count`` are sourced from the
    single post-loop :func:`~brain.graph_rag.aggregates.refresh_aggregates`, not
    summed per document.
    """

    processed: int = 0
    reconciled: int = 0
    skipped: int = 0
    orphans_removed: int = 0
    relationship_count: int = 0


# Invoked after each document with ``(processed_count, document_id, result)`` so
# a CLI caller can throttle progress output without this module owning any I/O.
ProgressCallback = Callable[[int, str, ReconcileResult], None]


def build_graph(
    conn: psycopg.Connection[Any],
    document_ids: Iterable[str],
    *,
    backend: GraphBackend,
    config: ReconcileConfig,
    limit: int | None = None,
    progress: ProgressCallback | None = None,
    extractor: EntityExtractor | None = None,
    force: bool = False,
) -> BuildResult:
    """Reconcile each document's people aspect into the graph (batch backfill).

    Iterates ``document_ids`` (already ordered by the caller — ascending id for
    a resumable build), calling :func:`reconcile_document` on each with the
    shared ``config`` + ``backend``. Stops after ``limit`` documents when set
    (caps the corpus for testing / partial runs). Returns a :class:`BuildResult`
    tally; ``progress`` (when given) is called once per processed document.

    **Deferred whole-tenant refresh (perf).** Every per-document reconcile runs
    with ``defer_tenant_refresh=True``, so the tenant-wide derived layers — the
    ``graph_relationships`` recompute, the orphan GC, and the AGE ``CO_OCCURS``
    rematerialization — are skipped per document and recomputed ONCE after the
    loop via :func:`~brain.graph_rag.aggregates.refresh_aggregates` (only when at
    least one document did work). The AGE ``CO_OCCURS`` rebuild is a whole-tenant
    operation whose cost scales with the tenant's relationship count R, so running
    it per document made a corpus build O(docs × R); hoisting it to a single final
    pass makes the build O(docs × doc_entities + R) while producing the identical
    end state (the derived layers are a deterministic full recompute from the
    per-document relational source-of-truth, which IS still written per document).
    Applies to both the plain backfill and the ``force`` rebuild below.

    **Directory index hoist (perf, Fix B).** The People-Hub directory index
    (``directory_entries``) drives person resolution but is corpus-wide and does
    NOT change during a build, so this driver builds it ONCE before the loop and
    injects it through reconcile's ``person_resolver`` DI seam (via
    :func:`brain.graph_rag.person_resolver.prebuilt_directory_resolver`) — instead
    of every per-document reconcile rebuilding the ~1.2k-row ``SELECT`` + dict.
    The incremental ingest hook (``sync.py``) is unaffected: it uses the default
    resolver, which builds its own single-document index.

    The connection SHOULD be autocommit so each document commits on its own,
    making the build resumable after an interruption (see the module docstring).
    A non-autocommit connection still works but the whole run becomes one
    transaction — an interruption then rolls everything back.

    ``extractor`` is the concept-aspect DI seam (wave G2-c): when
    ``config.concepts_enabled`` is True it MUST be provided (production passes
    :func:`brain.graph_rag.extract.make_extractor`; tests pass a fake), and each
    document's concept aspect is (re)indexed alongside its person aspect under its
    own watermark. When concepts are disabled (the default) it is ignored and the
    build is person-only — fully backward compatible. The force pre-pass GCs
    orphans of BOTH aspects + restores ALL of the tenant's entity vertices (any
    type), so a force rebuild faithfully restores any existing concept graph from
    the relational source-of-truth even without re-extraction.

    ``force=True`` is the authoritative **clean-then-rebuild** recovery path
    (``brain graphrag build --force``) that produces a clean state in BOTH the
    relational source AND the AGE mirror. It:

    1. cleans the RELATIONAL source for the tenant — recompute
       ``graph_relationships`` from the current contributions (drops aggregate
       rows stranded by deleted docs) and GC orphan entities (zero remaining
       mentions); reuses :func:`~brain.graph_rag.aggregates._recompute_aggregates`
       + :func:`~brain.graph_rag.aggregates._gc_orphan_persons`. This is what
       makes the zero-doc / all-docs-deleted case authoritative (no per-doc
       reconcile runs there to clean up);
    2. clears the tenant's ENTIRE existing AGE mirror
       (:meth:`~brain.graph_rag.backends.base.GraphBackend.clear_tenant`), so no
       stale survivors remain — a ``Document`` vertex for a doc removed from the
       relational source, an AGE-only ``Entity`` with no catalog row, orphan
       edges;
    3. restores every (now non-orphan) tenant ``Entity`` vertex from the clean
       relational catalog (the pre-pass that lets the per-doc full-tenant
       ``CO_OCCURS`` rematerialization find its endpoints); then
    4. threads :func:`reconcile_document`'s force flag through every document,
       bypassing the per-aspect watermark so each doc is fully re-reconciled and
       its watermark rewritten.

    Net result: BOTH stores are clean — no orphan entities and
    ``graph_relationships`` (relational + the AGE ``CO_OCCURS`` mirror) reflect
    only current contributions — and the AGE mirror is rebuilt fresh from the
    relational source-of-truth (entities + ``MENTIONED_IN`` + ``Document``
    vertices + ``CO_OCCURS``). A zero-document corpus ends empty in both stores.
    No document short-circuits, so ``reconciled == processed`` and
    ``skipped == 0``.

    ``force`` is incompatible with ``limit`` (a clear-then-partial-rebuild would
    permanently lose the un-rebuilt remainder) — passing both raises
    :class:`brain.errors.GraphReconcileError` (the CLI rejects it earlier as a
    ``BadParameter``). A zero-document corpus + ``force`` clears the tenant to an
    empty graph (correct).
    """
    if force and limit is not None:
        # A clear-then-partial-rebuild is incoherent: the clear wipes the whole
        # tenant mirror, but a limited loop would only rebuild a prefix, leaving
        # the rest permanently missing. Reject the combination (the CLI surfaces
        # this as a BadParameter before reaching here).
        raise GraphReconcileError(
            "build_graph(force=True) rebuilds the full corpus and cannot be "
            "combined with limit (a clear-then-partial-rebuild is incoherent)"
        )

    if force:
        # Authoritative rebuild = a clean state in BOTH stores (spec §7), in this
        # order: clean the relational source, then clear + rebuild the AGE mirror
        # FROM that clean source.
        #
        # The pre-pass (steps 1-3) runs inside ONE outer transaction so it is
        # atomic AS A UNIT: a failure during the restore (step 3), AFTER the AGE
        # mirror was cleared (step 2), rolls EVERYTHING back — relational + AGE
        # are left exactly as they were before the --force call. A recovery
        # command must never leave the graph in a worse (cleared / half-restored)
        # state than it started. ``clear_tenant`` and ``upsert_entities`` open
        # their own ``conn.transaction()``, which nest here as SAVEPOINTs, and
        # each AGE primitive activates its own ``ag_catalog`` search_path for the
        # duration of its statements; the relational steps run on the default
        # search_path. The per-doc reconcile LOOP below stays incremental and
        # resumable (each document commits in its own transaction) — only the
        # clear/restore must be atomic together.
        with conn.transaction():
            # 1. RELATIONAL clean: recompute graph_relationships from the CURRENT
            #    contributions (drops aggregate rows stranded by deleted docs) and
            #    GC orphan entities (zero remaining mentions). Reuses the same
            #    aggregate helpers as reconcile/refresh — no duplicated logic.
            #    This is what makes the zero-doc / all-docs-deleted case
            #    authoritative: there is no per-doc reconcile then to GC/recompute,
            #    so the stale relational rows would otherwise survive (and be
            #    mirrored into AGE by the pre-pass below).
            _recompute_aggregates(conn, config.tenant_id, config.generic_df_ratio)
            # GC orphans of BOTH aspects (concept GC is a no-op when no concept
            # entities exist, so this is safe for the person-only default too).
            _gc_orphan_persons(conn, config.tenant_id)
            _gc_orphan_concepts(conn, config.tenant_id)
            # 2. Clear the tenant's entire AGE mirror so nothing stale survives — a
            #    Document vertex for a doc removed from the relational source, an
            #    AGE-only Entity with no catalog row, orphan edges.
            backend.clear_tenant(conn, config.tenant_id)
            # 3. Restore Entity vertices from the now-clean relational catalog. The
            #    GC in step 1 ran first, so orphan entities never reach AGE; this
            #    pre-pass lets each per-doc CO_OCCURS refresh bind its endpoints (a
            #    per-doc reconcile rematerializes the WHOLE tenant's CO_OCCURS,
            #    which requires every contribution-referenced Entity vertex to
            #    exist).
            _restore_tenant_entity_vertices(conn, config.tenant_id, backend=backend)

    # Perf Fix B (2026-05-24): build the People-Hub directory index ONCE for the
    # whole batch and inject it through reconcile's person_resolver DI seam,
    # instead of every per-document reconcile rebuilding it. The directory
    # (``directory_entries``, ~1.2k rows) is corpus-wide and does NOT change
    # during a build, so rebuilding the SELECT + dict per document is pure waste
    # (~30-80 ms/doc). The incremental ingest hook (``sync.py``) keeps using the
    # default resolver, which builds its own single-document index, so the
    # one-doc path is unchanged. Late import keeps this module import-cheap
    # (``build_people`` pulls in the wiki package), mirroring person_resolver's
    # own late import of the same helper.
    from ..wiki.build_people import _build_directory_index

    directory = _build_directory_index(conn, sender_denylist=config.sender_denylist)
    resolver = prebuilt_directory_resolver(directory)

    processed = 0
    reconciled = 0
    skipped = 0
    orphans_removed = 0
    for document_id in document_ids:
        if limit is not None and processed >= limit:
            break
        result = reconcile_document(
            conn,
            document_id,
            backend=backend,
            config=config,
            person_resolver=resolver,
            extractor=extractor,
            force=force,
            defer_tenant_refresh=True,
        )
        processed += 1
        if result.skipped:
            skipped += 1
        else:
            reconciled += 1
        orphans_removed += result.orphans_removed
        if progress is not None:
            progress(processed, document_id, result)

    # Each per-document reconcile above ran with ``defer_tenant_refresh=True``, so
    # the tenant-wide derived layers — ``graph_relationships`` (normalized lift +
    # generic suppression), the orphan GC of both aspects, the AGE ``CO_OCCURS``
    # rematerialization, and the orphan-vertex DETACH DELETE — were skipped per
    # document and are recomputed ONCE here from the now-complete relational
    # source-of-truth. This is the SAME whole-tenant operation reconcile ran per
    # document before (``refresh_aggregates`` reuses the identical aggregate + GC
    # helpers), hoisted out of the loop: a corpus build drops from O(docs ×
    # tenant_R) per-doc CO_OCCURS rebuilds to O(docs × doc_entities + tenant_R)
    # (one final rebuild), and the end state is identical (a deterministic full
    # recompute from the source-of-truth).
    #
    # Gate on ``reconciled > 0``: the refresh runs only when at least one document
    # changed the relational source (so the derived layers are stale). When every
    # document short-circuited on its watermark (an idempotent re-run, or a plain
    # ``--backfill`` over an unchanged corpus) the derived layers are already
    # consistent from the build that did the work, so skipping the refresh keeps
    # the re-run a true no-op AND avoids a spurious ``refresh_cooccur_edges`` that
    # would raise against a deliberately-dropped AGE mirror (the recovery path for
    # that is ``--force``, which always reconciles).
    #
    # CRASH-WINDOW RECOVERY: if a deferred build is interrupted AFTER the last
    # document's watermark is written but BEFORE this final refresh runs, a
    # same-command rerun all-skips (``reconciled == 0``) and will NOT repair the
    # derived layer (``graph_relationships`` / AGE ``CO_OCCURS``) — run
    # ``brain graphrag refresh`` (or ``brain graphrag build --force``) to repair.
    # The relational source-of-truth stays correct throughout.
    relationship_count = 0
    if reconciled > 0:
        refresh = refresh_aggregates(conn, backend=backend, config=config)
        relationship_count = refresh.relationship_count
        orphans_removed += refresh.orphans_removed
    return BuildResult(
        processed=processed,
        reconciled=reconciled,
        skipped=skipped,
        orphans_removed=orphans_removed,
        relationship_count=relationship_count,
    )


def _restore_tenant_entity_vertices(
    conn: psycopg.Connection[Any], tenant_id: str, *, backend: GraphBackend
) -> int:
    """Re-MERGE every relational ``graph_entities`` row for a tenant into AGE.

    The force-rebuild pre-pass (spec §7 authoritative rebuild). A per-document
    :func:`reconcile_document` recreates the doc's own entity vertices but then
    rematerializes the WHOLE tenant's ``CO_OCCURS`` edges, which requires *every*
    contribution-referenced entity to already have an AGE vertex
    (:meth:`~brain.graph_rag.backends.base.GraphBackend.refresh_cooccur_edges`
    raises if an endpoint vertex is missing). After a dropped or corrupted AGE
    mirror that invariant is broken — the relational catalog still lists every
    entity but no AGE vertex exists — so the first forced doc's cooccur refresh
    would fail to bind an endpoint that belongs to a not-yet-processed doc.
    Restoring all of the tenant's Entity vertices up front (an idempotent MERGE)
    re-establishes the invariant, so each subsequent forced per-doc reconcile
    rebuilds mentions / MENTIONED_IN / Document vertices / CO_OCCURS exactly as
    the incremental path would. Returns the number of entity vertices restored.
    """
    rows = conn.execute(
        "SELECT id::text, entity_type, name, canonical_key "
        "FROM graph_entities WHERE tenant_id = %s",
        (tenant_id,),
    ).fetchall()
    entities = [
        GraphEntity(
            id=str(row[0]),
            entity_type=str(row[1]),
            name=str(row[2]),
            canonical_key=str(row[3]),
            tenant_id=tenant_id,
        )
        for row in rows
    ]
    if entities:
        backend.upsert_entities(conn, tenant_id, entities)
    return len(entities)
