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
not obvious, its code; a scan of the read tools' parameters then moved
``brain_gaps`` to write (plan ``docs/plans/2026-10-31-rm-46-mcp-http.md``).
``tests/test_mcp_http_readonly.py`` pins the judgement calls to the docstring
phrases they rest on. stdio never calls :func:`apply_read_only`.

**The graph family needs ``--include-confidential`` (RM-46 round 5).** The entity
graph is derived from EVERY document: entity names, co-occurrence edges and
weights, themes and communities are all computed over confidential documents
too. Four security rounds each found one more way a graph output carried that,
so filtering outputs field by field did not converge. Without
``--include-confidential`` the graph family is withheld (:func:`apply_hide_mode`),
and ``brain_ask`` answers only in ``hybrid`` mode (:data:`ASK_GRAPH_MODES_REFUSAL`).

The withheld set is the ``brain_graphrag_`` prefix PLUS :data:`HIDE_MODE_WITHHELD_EXTRA`,
pinned tools whose output carries confidential-derived data under another name:
graph communities (sec-118e: ``brain_review_weekly``) or the owner's logged search
terms from every lens (sec-118f: ``brain_gaps``). The prefix alone does not catch a
future graph tool named otherwise. ``tests/test_mcp_http_graph_reach.py`` walks every
registered tool's calls for graph SQL and ``brain.graph_rag``, and fails on one
that is in neither this family nor :data:`GRAPH_TOUCHING_KEPT`, where each served
tool that reads graph tables carries the reason its output is built from gated
documents only. A new tool that reads graph tables needs its own judgement there.
"""
from __future__ import annotations

from collections.abc import Iterable
from typing import Any, Protocol

READ_ONLY_TOOLS: frozenset[str] = frozenset(
    {
        # NOT here: ``brain_gaps``. It reads the search log, but ``push=true`` upserts
        # the ``elicitation_gaps`` queue (``brain gaps push``), so it is a write.
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


#: Every graph tool's name starts with this.
GRAPH_TOOL_PREFIX = "brain_graphrag_"

#: ``brain_ask``'s answer to a graph mode when confidential documents are hidden.
ASK_GRAPH_MODES_REFUSAL = (
    "this server runs brain_ask in mode='hybrid' only: the entity graph is built from "
    "every document, confidential ones included, so its modes need "
    "brain mcp serve --include-confidential"
)


#: Tools without the prefix that are withheld in hide mode too, each because its
#: output carries confidential-derived data. Both are write tools, so they matter
#: only with --allow-writes.
HIDE_MODE_WITHHELD_EXTRA: frozenset[str] = frozenset(
    {
        # Its graph themes are communities: member names and stored summaries
        # (sec-118e). Gated on stdio too now, and withheld here regardless.
        "brain_review_weekly",
        # It clusters the owner's logged search terms, typed under every lens, so
        # a confidential-only term would leave (sec-118f LOW-3).
        "brain_gaps",
    }
)

#: Served tools that read graph tables, and why their output is built from gated
#: documents only. Judged per tool; the reach test holds every graph-touching
#: tool to this list or the family.
GRAPH_TOUCHING_KEPT: dict[str, str] = {
    "brain_ask": "hybrid mode only in hide mode; its graph modes are refused",
    "brain_timeline": (
        "entities match only with a non-confidential mention; buckets are lensed "
        "documents; co-topics count edge contributions of those documents only"
    ),
    "brain_brief": (
        "new entities are named, counted and dated from non-confidential, non-draft "
        "documents only (one absence bit is a documented residual)"
    ),
    "brain_search": "the entity filter is an exact match that returns lensed documents only",
    "brain_explain": "brain_search's ranking, with the same entity filter",
    "brain_recall": "the same search predicate as brain_search",
    "brain_watch_check": "runs a saved search through the same predicate",
    "brain_watch_add": "validates a saved search through the same predicate",
    "brain_review_findings_list": (
        "a finding is withheld whole unless every evidence document is non-confidential"
    ),
    "brain_review_scan": (
        "scans entities and documents through the confidential lens; writes findings"
    ),
    "brain_connect_list": (
        "graph_score is the pair's own shared mentions; both documents are lensed"
    ),
}


def hide_mode_withheld(registered: Iterable[str]) -> tuple[str, ...]:
    """The hide-mode withheld set among ``registered``: the prefix and the extra set."""
    return tuple(
        sorted(
            name
            for name in set(registered)
            if name.startswith(GRAPH_TOOL_PREFIX) or name in HIDE_MODE_WITHHELD_EXTRA
        )
    )


def apply_hide_mode(server: ToolRegistry, registered: Iterable[str]) -> tuple[str, ...]:
    """Remove the hide-mode withheld set (:func:`hide_mode_withheld`); return it."""
    withheld = hide_mode_withheld(registered)
    for name in withheld:
        server.remove_tool(name)
    return withheld


def apply_read_only(server: ToolRegistry, registered: Iterable[str]) -> tuple[str, ...]:
    """Remove every tool in ``registered`` that is not read-only; return them, sorted.

    ``registered`` comes from :func:`registered_tool_names` on the same server,
    so every name removed exists (``remove_tool`` raises for one that does not).
    """
    withheld = write_tool_names(registered)
    for name in withheld:
        server.remove_tool(name)
    return withheld
