"""Post-write / post-delete graph reconcile hook (wave G1-c, GraphRAG, spec §7/§10).

Bridges the document write/delete paths to the people-aspect reconcile in
:mod:`brain.graph_rag.reconcile`. One :class:`GraphSyncer` is built per CLI / MCP
/ watcher invocation from the resolved :class:`brain.config.Config` and threaded
into ``ingest_document`` / ``update_document`` / ``sync_vault`` and the explicit
delete sites, so a single immutable
:class:`brain.graph_rag.reconcile.ReconcileConfig` is shared across *every*
write and delete in that process. That shared object is the divergence-safety
guarantee G1-b flagged: a build and a later delete can never recompute the
tenant aggregate with a different ``generic_df_ratio`` (which would corrupt the
generic-suppression weights).

**Discipline (mirrors the Q1-D enrich hook).** Graph sync is best-effort and
MUST NOT block or crash an ingest / edit / delete:

* it runs only when graph sync is enabled (``BRAIN_GRAPH_ENABLED``) AND the
  database actually ships Apache AGE (``age_extension_available`` is a safe
  ``False`` on a stock pgvector DB -- e.g. the prod DB before the AGE cut-over),
* it provisions the graph labels/indexes lazily on first use (idempotent;
  guarded by the connection being in autocommit mode, since AGE catalog DDL
  needs it), and
* any failure is logged at WARNING with the document id and swallowed -- the
  relational write already committed and the graph is a recomputable mirror
  (``brain graphrag build`` rebuilds it). This is the *intended* degradation:
  distinct from a swallowed bug, a graph-sync hiccup must never surface as an
  ingest failure.

**Connection contract.** The caller's connection is reused (no per-document
connection churn). The hook is invoked at a point where the caller's write has
already committed and no transaction is open, so ``reconcile_document`` /
``remove_document`` open their own top-level transaction (real on an autocommit
connection, a true top-level one on an idle ``autocommit=False`` connection --
see the reconcile module's "Connection contract"). The connection must have AGE
loadable; :meth:`GraphSyncer._age_ready` runs ``LOAD 'age'`` itself, so callers
do not need :func:`brain.db.connect_age`.
"""
from __future__ import annotations

import logging
from functools import cache
from typing import Any

import psycopg

from ..config import Config
from ..db import age_extension_available, load_age
from .backends.age import AgeBackend
from .backends.base import GraphBackend
from .extract import EntityExtractor
from .reconcile import ReconcileConfig, reconcile_document, remove_document

_logger = logging.getLogger(__name__)

__all__ = ["GraphSyncer", "build_reconcile_config", "make_graph_syncer"]


@cache
def build_reconcile_config(cfg: Config) -> ReconcileConfig:
    """Resolve the ONE :class:`ReconcileConfig` for a Config (cached -> shared).

    ``functools.cache`` keyed on the frozen, hashable :class:`brain.config.Config`
    returns the *same* :class:`ReconcileConfig` instance for repeated calls in a
    process, so a build and a later delete provably reconcile the tenant
    aggregate with the identical ``generic_df_ratio`` / ``tenant_id`` (spec §7
    divergence-safety). ``owner_keys`` reuses ``cfg.owner_participants`` -- the
    corpus owner is stripped from the graph's person roster exactly as from the
    People Hub.
    """
    return ReconcileConfig(
        tenant_id=cfg.graph_tenant_id,
        cooccur_window=cfg.graph_cooccur_window,
        max_entities_per_doc=cfg.graph_max_entities,
        generic_df_ratio=cfg.graph_generic_df_ratio,
        owner_keys=cfg.owner_participants,
        sender_denylist=cfg.graph_sender_denylist,
        concepts_enabled=cfg.graph_concepts,
        graph_extract_stopwords=cfg.graph_extract_stopwords,
    )


class GraphSyncer:
    """Best-effort people-aspect graph sync for one process invocation.

    Holds the single shared :class:`ReconcileConfig` + the graph backend, and
    exposes :meth:`reconcile` / :meth:`remove` -- both gated, both never-raise.
    Construct via :func:`make_graph_syncer` (production) or directly (tests).
    """

    def __init__(
        self,
        config: ReconcileConfig,
        *,
        enabled: bool,
        backend: GraphBackend | None = None,
        extractor: EntityExtractor | None = None,
    ) -> None:
        self._config = config
        self._enabled = enabled
        self._backend: GraphBackend = (
            backend if backend is not None else AgeBackend()
        )
        # Concept-aspect DI seam (wave G2-c). Threaded into reconcile only; when
        # ``config.concepts_enabled`` is True this MUST be set (``make_graph_syncer``
        # builds it from the config). ``None`` keeps the syncer person-only.
        self._extractor = extractor
        # Lazy one-time label/index provisioning per syncer (per invocation).
        self._bootstrapped = False

    @property
    def config(self) -> ReconcileConfig:
        """The single shared reconcile config (used by reconcile AND remove)."""
        return self._config

    @property
    def enabled(self) -> bool:
        """True iff ``BRAIN_GRAPH_ENABLED`` resolved truthy for this process."""
        return self._enabled

    def reconcile(self, conn: psycopg.Connection[Any], document_id: str) -> None:
        """Reconcile one document into the people graph after a write.

        Never raises -- a graph-sync failure is logged and swallowed so the
        (already-committed) relational write is never undone.
        """
        if not self._enabled:
            return
        try:
            if not self._age_ready(conn):
                return
            reconcile_document(
                conn,
                document_id,
                backend=self._backend,
                config=self._config,
                extractor=self._extractor,
            )
            # Bug A — cross-document concept type-collapse. Runs AFTER
            # reconcile_document has written + committed this document's
            # mentions/contributions (its own top-level transaction), so a
            # zero-mention source GC inside the collapse can never orphan an id
            # the pending mention-insert still references. Only meaningful when
            # concepts are active (the catalog is otherwise person-only, which
            # this never touches); idempotent + a cheap catalog scan no-op when
            # nothing is fragmented. Late import keeps the person-only path from
            # pulling the extractor transport at module load.
            if self._config.concepts_enabled and self._extractor is not None:
                from .cross_type import collapse_cross_type_concepts

                collapse_cross_type_concepts(
                    conn,
                    self._config.tenant_id,
                    self._backend,
                    config=self._config,
                )
        except Exception as exc:  # noqa: BLE001 -- best-effort: never block a write
            _logger.warning(
                "graph sync (reconcile) skipped for doc %s: %s; "
                "the graph is recomputable via `brain graphrag build`",
                document_id,
                exc,
            )

    def remove(self, conn: psycopg.Connection[Any], document_id: str) -> None:
        """Remove one document from the people graph after a delete.

        Never raises -- same best-effort discipline as :meth:`reconcile`.
        """
        if not self._enabled:
            return
        try:
            if not self._age_ready(conn):
                return
            remove_document(
                conn, document_id, backend=self._backend, config=self._config
            )
        except Exception as exc:  # noqa: BLE001 -- best-effort: never block a delete
            _logger.warning(
                "graph sync (remove) skipped for doc %s: %s; "
                "the graph is recomputable via `brain graphrag build`",
                document_id,
                exc,
            )

    def _age_ready(self, conn: psycopg.Connection[Any]) -> bool:
        """Gate + lazily provision: True iff AGE is usable on ``conn``.

        Returns ``False`` (caller skips) when the database does not ship AGE
        (stock pgvector) or the extension is not yet installed/loadable. When
        AGE is ready, lazily bootstraps the backend's labels + property indexes
        once per syncer -- idempotent, and only attempted on an autocommit
        connection (AGE catalog DDL needs it; the CLI / MCP write paths run
        autocommit). On a non-autocommit connection the bootstrap is skipped and
        reconcile relies on `brain init` having provisioned the graph; if it
        hasn't, the reconcile attempt fails and is caught by the caller.
        """
        if not age_extension_available(conn):
            return False
        if not load_age(conn):
            return False
        if not self._bootstrapped and conn.autocommit:
            self._backend.bootstrap(conn)
            self._bootstrapped = True
        return True


def make_graph_syncer(
    cfg: Config,
    *,
    backend: GraphBackend | None = None,
    extractor: EntityExtractor | None = None,
) -> GraphSyncer:
    """Build the per-invocation :class:`GraphSyncer` from a resolved Config.

    The single source of truth for wiring the graph sync hook. CLI / MCP /
    watcher entry points call this once and thread the returned syncer through
    every write/delete path so they all share one :class:`ReconcileConfig`.

    When ``BRAIN_GRAPH_CONCEPTS`` is on (``cfg.graph_concepts``) and no
    ``extractor`` is injected, the concept-aspect extractor is built from the
    config (:func:`brain.graph_rag.extract.make_extractor`, using
    ``BRAIN_GRAPH_EXTRACT_MODEL``); tests inject a fake instead. With concepts
    off the extractor stays ``None`` and the syncer is person-only.
    """
    syncer_extractor = extractor
    if syncer_extractor is None and cfg.graph_concepts:
        # Late import keeps this module import-cheap (extract pulls in the
        # enrichment transport) for the common concepts-disabled path.
        from .extract import make_extractor

        syncer_extractor = make_extractor(cfg)
    return GraphSyncer(
        build_reconcile_config(cfg),
        enabled=cfg.graph_enabled,
        backend=backend,
        extractor=syncer_extractor,
    )
