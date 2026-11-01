"""No tool that reads the entity graph is served in hide mode without a judgement (RM-46 sec-118e).

Round 5 withheld the ``brain_graphrag_*`` family in HTTP hide mode, matched by
prefix. sec-118e found ``brain_review_weekly`` returning community names and
summaries under another name, so the prefix alone is not a guard. This test
walks each registered tool's calls: the functions it names, recursively, through
``brain`` code. It flags a tool when a reached function holds graph-table SQL or
imports ``brain.graph_rag``. Each flagged tool must be in the withheld family
(``hide_mode_withheld``) or in ``GRAPH_TOUCHING_KEPT``, with the reason its output
is built from gated documents only.

The walk is a static over-approximation within a function's own names. It does
not follow dynamic dispatch, so ``GRAPH_TOUCHING_KEPT`` may also list tools
sec-118e judged by reading their queries.
"""
from __future__ import annotations

import ast
import importlib
import inspect
import re
import sys
import textwrap
import types
from collections.abc import Callable
from typing import Any

import anyio
import pytest

from brain import mcp_server
from brain.mcp_tool_classes import (
    GRAPH_TOUCHING_KEPT,
    HIDE_MODE_WITHHELD_EXTRA,
    hide_mode_withheld,
    registered_tool_names,
)

pytestmark = pytest.mark.nodb

#: The entity graph's tables, and the graph-derived columns and tables other code
#: stores: contradiction gaps name an entity, and a link suggestion's score is a
#: shared-mention count (sec-118f LOW-2).
GRAPH_SQL = re.compile(
    r"\bgraph_(entities|entity_mentions|edge_contributions|communities|community_members"
    r"|relationships|index_state)\b|\belicitation_gaps\b|\bgraph_score\b"
)
#: Calls followed from a tool function. Every flagged path at this commit is
#: at most four deep; eight leaves room without walking the whole package.
MAX_DEPTH = 8


def _is_brain(obj: Any) -> bool:
    module = getattr(obj, "__module__", None)
    return isinstance(module, str) and (module == "brain" or module.startswith("brain."))


def _code(tree: ast.AST) -> str:
    """``tree`` as source without docstrings or comments, so prose cannot flag a tool.

    sec-118f LOW-2: four tools were flagged only because a docstring named a table.
    """
    for node in ast.walk(tree):
        body = getattr(node, "body", None)
        if (
            isinstance(node, ast.Module | ast.ClassDef | ast.FunctionDef | ast.AsyncFunctionDef)
            and body
            and isinstance(body[0], ast.Expr)
            and isinstance(body[0].value, ast.Constant)
            and isinstance(body[0].value.value, str)
        ):
            node.body = body[1:] or [ast.Pass()]
    return ast.unparse(tree)


def _lazy_bindings(tree: ast.AST, package: str) -> tuple[bool, dict[str, Any]]:
    """Names bound by the function's own imports, and whether one is graph code."""
    graph, bound = False, {}
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            graph = graph or any(a.name.startswith("brain.graph_rag") for a in node.names)
            continue
        if not isinstance(node, ast.ImportFrom):
            continue
        try:
            base = importlib.util.resolve_name("." * node.level + (node.module or ""), package)
        except ImportError:
            continue
        graph = graph or base.startswith("brain.graph_rag")
        try:
            module = importlib.import_module(base)
        except ImportError:
            continue
        for alias in node.names:
            bound[alias.asname or alias.name] = getattr(module, alias.name, None)
    return graph, bound


def _step(fn: Callable[..., Any]) -> tuple[bool, set[Callable[..., Any]]]:
    """Whether ``fn`` itself touches the graph, and the ``brain`` functions it names."""
    try:
        source = textwrap.dedent(inspect.getsource(fn))
    except (OSError, TypeError):
        return False, set()
    tree = ast.parse(source)
    package = sys.modules[fn.__module__].__package__ or ""
    graph, bound = _lazy_bindings(tree, package)
    graph = graph or bool(GRAPH_SQL.search(_code(ast.parse(source))))
    names = {**getattr(fn, "__globals__", {}), **bound}
    callees: set[Callable[..., Any]] = set()
    for node in ast.walk(tree):
        target = None
        if isinstance(node, ast.Name):
            target = names.get(node.id)
        elif isinstance(node, ast.Attribute) and isinstance(node.value, ast.Name):
            owner = names.get(node.value.id)
            if isinstance(owner, types.ModuleType):
                target = getattr(owner, node.attr, None)
        if isinstance(target, str):
            # A module-level SQL constant the function names (sec-118f LOW-2).
            graph = graph or bool(GRAPH_SQL.search(target))
        elif isinstance(target, types.ModuleType):
            graph = graph or target.__name__.startswith("brain.graph_rag")
        elif _is_brain(target):
            graph = graph or target.__module__.startswith("brain.graph_rag")
            if inspect.isfunction(target):
                callees.add(target)
            elif inspect.isclass(target):
                callees |= {m for _, m in inspect.getmembers(target, inspect.isfunction)}
    return graph, {c for c in callees if _is_brain(c)}


def touches_graph(fn: Callable[..., Any]) -> bool:
    """Whether any function reachable from ``fn`` within :data:`MAX_DEPTH` touches the graph."""
    seen, frontier = {fn}, [fn]
    for _ in range(MAX_DEPTH):
        reached: list[Callable[..., Any]] = []
        for current in frontier:
            graph, callees = _step(current)
            if graph:
                return True
            reached += [c for c in callees if c not in seen]
            seen.update(callees)
        frontier = reached
    return False


@pytest.fixture(scope="module")
def flagged() -> set[str]:
    names = anyio.run(registered_tool_names, mcp_server.mcp_app)
    manager = mcp_server.mcp_app._tool_manager
    return {name for name in names if touches_graph(manager.get_tool(name).fn)}


def test_every_graph_touching_tool_is_withheld_or_judged(flagged: set[str]) -> None:
    family = set(hide_mode_withheld(flagged))
    unjudged = flagged - family - set(GRAPH_TOUCHING_KEPT)

    assert unjudged == set(), (
        f"{sorted(unjudged)} read the entity graph but are neither withheld in HTTP hide "
        "mode nor in GRAPH_TOUCHING_KEPT (brain.mcp_tool_classes)"
    )


def test_the_walk_sees_the_tools_it_exists_for(flagged: set[str]) -> None:
    # Non-vacuity: the tool sec-118e found, a kept one, and the prefix family.
    assert {"brain_review_weekly", "brain_timeline", "brain_graphrag_search"} <= flagged
    # sec-118f LOW-2: these reach the graph only through a module SQL constant
    # (search_predicate's entity clause); they were flagged by a docstring before.
    assert {"brain_recall", "brain_explain", "brain_watch_check", "brain_watch_add"} <= flagged
    # And these only through a graph-derived table or column.
    assert {"brain_review_findings_list", "brain_connect_list"} <= flagged
    assert "brain_backlinks" not in flagged  # the wikilink graph is not the entity graph


_GRAPH_CONSTANT = "SELECT 1 FROM graph_entity_mentions"


def _prose_only() -> int:
    """Mentions graph_entity_mentions and elicitation_gaps, in prose only."""
    # graph_communities, in a comment only.
    return 1


def _names_a_constant() -> str:
    return _GRAPH_CONSTANT


def test_prose_never_flags_a_function_and_a_named_sql_constant_does() -> None:
    # Removing every docstring and comment cannot change what is flagged: the walk
    # never reads them. A table named in code, even through a constant, still counts.
    assert touches_graph(_prose_only) is False
    assert touches_graph(_names_a_constant) is True


def test_the_judgement_lists_name_real_tools_once() -> None:
    names = set(anyio.run(registered_tool_names, mcp_server.mcp_app))

    assert set(GRAPH_TOUCHING_KEPT) <= names
    assert names >= HIDE_MODE_WITHHELD_EXTRA
    assert not set(GRAPH_TOUCHING_KEPT) & set(hide_mode_withheld(names))
    assert all(reason.strip() for reason in GRAPH_TOUCHING_KEPT.values())
