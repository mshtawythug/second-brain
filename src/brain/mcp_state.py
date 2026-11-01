"""The MCP server's shared state, and the one way to build it (stdio and HTTP).

Moved out of :func:`brain.mcp_server.main` by RM-46 so ``brain mcp serve``
builds exactly the state the stdio server builds, instead of a second copy of
startup that could drift. A leaf: it never imports :mod:`brain.mcp_server`
(which re-exports :class:`McpState` as ``_State`` for the tests that build it by
hand) and keeps the networkx-pulling graph chain behind a lazy import.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from .config import Config
from .db import PersistentConnection
from .embeddings import make_embedder
from .enrichment import OllamaEnricher, make_enricher
from .ingest import Embedder

if TYPE_CHECKING:
    from .graph_rag.sync import GraphSyncer


@dataclass
class McpState:
    """Shared server state, built once at startup by :func:`build_state`.

    Tests build this directly and assign it to ``brain.mcp_server._state`` (via
    ``monkeypatch.setattr``) instead of calling ``main()``.

    Wave Q2-SUMMARY-WIKI (2026-05-11): ``enricher`` lets the ``brain_edit``
    tool refresh ``documents.summary`` on body-changing edits. ``None`` is
    allowed so tests can opt out of the LLM round-trip; production always
    populates it via :func:`make_enricher`.

    DB-08 (perf/waves-0-2 T6): ``db_conn`` holds the persistent connection
    reused across all tool calls. ``None`` makes ``mcp_server._mcp_conn`` open a
    connection per call: what tests that build state by hand get, and what the
    HTTP transport asks for (RM-46.D5), because its calls run concurrently on
    worker threads and one psycopg connection must not be shared by them.
    """

    cfg: Config
    embedder: Embedder
    enricher: OllamaEnricher | None = None
    # Wave G1-c: people-aspect graph syncer wired through ``brain_ingest`` /
    # ``brain_edit`` so MCP-driven writes keep the graph in lock-step. ``None``
    # (tests that build state directly) skips graph sync. TYPE_CHECKING-only so
    # the networkx-pulling graph_rag chain stays off MCP-server startup.
    graph_syncer: GraphSyncer | None = None
    db_conn: PersistentConnection | None = None
    # RM-46, sec-118 #2: set by ``brain mcp serve`` unless --include-confidential.
    # A confidential document then answers every by-prefix lookup exactly as an
    # unknown prefix does, and a graph entity is named only if a non-confidential
    # document mentions it. stdio leaves it False, so its behaviour is unchanged.
    hide_confidential: bool = False
    # RM-46, sec-118c LOW-2: ``--call-timeout``, set by ``brain mcp serve``. A call
    # past it gets its 504 but keeps its work-limit slot until the tool returns,
    # so ``brain_ask`` stops at its next step past it, and over HTTP the loop and
    # depth arguments are refused above their configured values. stdio: None.
    call_timeout_s: float | None = None


def build_state(
    cfg: Config,
    *,
    persistent_db: bool = True,
    hide_confidential: bool = False,
    call_timeout_s: float | None = None,
) -> McpState:
    """Construct the embedder, enricher, graph syncer and connection for ``cfg``.

    Nothing here touches the network or the database: the enricher does not
    probe Ollama, the graph syncer self-gates on ``BRAIN_GRAPH_ENABLED`` and
    AGE, and the persistent connection opens lazily on first use.
    ``persistent_db=False`` leaves ``db_conn`` unset (see :class:`McpState`).
    """
    embedder = make_embedder(cfg)
    # Every external service has explicit timeouts; the enricher reads
    # ``Config.enrich_timeout_seconds``.
    enricher = make_enricher(cfg)
    # Imported lazily so the networkx graph_rag chain stays off the hot path.
    from .graph_rag.sync import make_graph_syncer

    graph_syncer = make_graph_syncer(cfg)
    db_conn = PersistentConnection(cfg.database_url) if persistent_db else None
    return McpState(
        cfg=cfg,
        embedder=embedder,
        enricher=enricher,
        graph_syncer=graph_syncer,
        db_conn=db_conn,
        hide_confidential=hide_confidential,
        call_timeout_s=call_timeout_s,
    )
