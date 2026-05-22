"""Effective ``tenant_id`` resolution for GraphRAG surfaces (spec §9/§10)."""
from __future__ import annotations

from ..config import Config
from ..errors import GraphTenantError

__all__ = ["resolve_tenant"]


def resolve_tenant(cfg: Config, override: str | None = None) -> str:
    """Return the effective, validated ``tenant_id`` for a graph operation.

    GraphRAG is multi-tenant (spec §9 D9): every relational row, AGE
    vertex/edge property, and generated query is scoped by ``tenant_id``.
    Centralizing the "override-else-default, validated non-empty" rule here is
    the single source of truth the G2 retrieval CLI / MCP / themes paths call,
    so they cannot each re-implement it (and drift).

    Precedence: a non-blank ``override`` (trimmed) wins; otherwise the config
    default ``cfg.graph_tenant_id`` (trimmed, ``BRAIN_GRAPH_TENANT``, itself
    defaulting to ``"default"`` for single-user local use). A ``None`` / blank /
    whitespace-only override is treated as "not supplied" and falls back to the
    default — matching the existing ``brain graphrag build/refresh`` ``--tenant``
    precedent in ``cli._graphrag_config`` — so a stray ``--tenant ""`` never
    silently scopes a query to an empty tenant.

    Raises:
        GraphTenantError: the resolved tenant id is empty. Every tenant-scoped
            row/vertex/edge/query needs a non-empty id (the schema's
            ``tenant_id TEXT NOT NULL`` invariant). With config validation this
            only fires on a programmatically-constructed Config carrying a blank
            ``graph_tenant_id``, but the guard keeps the invariant local and
            explicit.
    """
    if override is not None and override.strip():
        return override.strip()
    resolved = cfg.graph_tenant_id.strip()
    if not resolved:
        raise GraphTenantError(
            "effective tenant_id is empty: pass --tenant or set "
            "BRAIN_GRAPH_TENANT to a non-empty value"
        )
    return resolved
