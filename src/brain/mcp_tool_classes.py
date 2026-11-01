"""Which MCP tools are safe to serve read-only over HTTP (RM-46).

``brain mcp serve`` is read-only unless ``--allow-writes``: every registered tool
NOT named in :data:`READ_ONLY_TOOLS` is removed from the server before it
listens, so ``tools/list`` does not advertise it and ``tools/call`` answers
"Unknown tool". The list is an ALLOWLIST and fails closed: a tool added later
is withheld until someone classifies it here.

"Read" means the tool does not create, change or delete anything the owner
sees as knowledge: documents, tags, vault files, graph state, watches, the
review queue, link decisions. Telemetry rows (``search_queries``, the
``interactions`` an answer's citations log) are allowed, as R1 Q4 decided:
withholding ``brain_search`` would make the transport pointless.

Each entry was judged against its tool's docstring and, for the six that were
not obvious, its code (plan ``docs/plans/2026-10-31-rm-46-mcp-http.md``);
``tests/test_mcp_http_readonly.py`` pins the judgement calls to the docstring
phrases they rest on. stdio never calls :func:`apply_read_only`.
"""
from __future__ import annotations

from collections.abc import Iterable
from typing import Any, Protocol

READ_ONLY_TOOLS: frozenset[str] = frozenset(
    {
        # Retrieval: they log a ``search_queries`` / ``interactions`` row only.
        "brain_search",
        "brain_recall",
        "brain_ask",
        "brain_explain",  # brain_search's diagnostics, per result
        "brain_show",  # logs an ``opened`` interaction
        "brain_related",  # "No embed call is made"
        # Listings and counts.
        "brain_list",
        "brain_status",
        "brain_gaps",  # mines the search log
        "brain_resurface",  # scores; records no access
        "brain_backlinks",
        "brain_links",
        "brain_orphans",
        "brain_tags_list",
        "brain_tags_suggest_merges",
        "brain_dedup_list",
        "brain_watch_list",
        "brain_watch_check",  # "WITHOUT recording it"
        "brain_brief",  # assembles; no vault write, no watch recorded
        "brain_review_findings_list",  # "without scanning"
        "brain_connect_list",  # lists suggestions; no refresh
        "brain_link_proposal",  # "Writes nothing"
        # The graph, read side.
        "brain_graphrag_search",
        "brain_graphrag_themes",
        "brain_graphrag_entity",
        "brain_graphrag_entities",
        "brain_graphrag_communities",  # "Read-only and fast"
        "brain_graphrag_stats",
        "brain_timeline",
    }
)


class ToolRegistry(Protocol):
    """The slice of the SDK server this module needs (``MCPServer`` on mcp 2.x)."""

    async def list_tools(self) -> list[Any]:
        """The registered tool descriptors."""
        ...

    def remove_tool(self, name: str) -> None:
        """Remove one registered tool."""
        ...


async def registered_tool_names(server: ToolRegistry) -> tuple[str, ...]:
    """Every tool ``server`` registers, sorted."""
    return tuple(sorted(tool.name for tool in await server.list_tools()))


def write_tool_names(registered: Iterable[str]) -> tuple[str, ...]:
    """The names in ``registered`` that are not read-only, sorted and unique."""
    return tuple(sorted({name for name in registered if name not in READ_ONLY_TOOLS}))


def apply_read_only(server: ToolRegistry, registered: Iterable[str]) -> tuple[str, ...]:
    """Remove every tool in ``registered`` that is not read-only; return them, sorted.

    ``registered`` comes from :func:`registered_tool_names` on the same server,
    so every name removed exists (``remove_tool`` raises for one that does not).
    """
    withheld = write_tool_names(registered)
    for name in withheld:
        server.remove_tool(name)
    return withheld
