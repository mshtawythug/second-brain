"""CLI↔MCP parity guard for the GraphRAG surface (T4).

Asserts a strict 1:1 mapping between every ``brain graphrag …`` Typer
subcommand (including the nested ``communities {build,refresh,list}`` group)
and every ``brain_graphrag_*`` FastMCP tool. The test is purely introspective —
it reads the live Typer command registry and the live FastMCP tool registry, so
it needs no database, no Ollama, and no AGE: it is deterministic and runs in the
default (non-eval) suite.

Why this matters: the GraphRAG capability is exposed twice — once on the CLI for
humans and once over MCP for Claude. Spec §9 requires full parity. This guard
fails the moment the two surfaces drift:

* add a CLI ``graphrag`` subcommand without the matching MCP tool → the CLI set
  no longer matches ``EXPECTED_MAPPING`` keys → FAIL;
* add an MCP ``brain_graphrag_*`` tool without the matching CLI subcommand → the
  MCP set no longer matches ``EXPECTED_MAPPING`` values → FAIL.

The name mapping is irregular in exactly one place — ``communities list`` maps
to ``brain_graphrag_communities`` (the bare list verb is dropped) — so the
mapping is declared explicitly rather than derived by a naming rule.
"""
from __future__ import annotations

import asyncio

import typer

from brain.cli import graphrag_app
from brain.mcp_server import mcp_app

# Explicit, authoritative CLI-command → MCP-tool mapping (spec §9 parity set).
# Keys are fully-qualified CLI subcommands under ``brain graphrag``; nested group
# commands are written "<group> <command>". Values are the FastMCP tool names.
# This dict is the single source of truth for the 10↔10 parity contract — adding
# a capability to one surface means adding its row here AND to the other surface.
EXPECTED_MAPPING: dict[str, str] = {
    "build": "brain_graphrag_build",
    "refresh": "brain_graphrag_refresh",
    "search": "brain_graphrag_search",
    "themes": "brain_graphrag_themes",
    "entity": "brain_graphrag_entity",
    "entities": "brain_graphrag_entities",
    "stats": "brain_graphrag_stats",
    "communities build": "brain_graphrag_communities_build",
    "communities refresh": "brain_graphrag_communities_refresh",
    "communities list": "brain_graphrag_communities",
    # Wave C4 — curated entity alias/merge admin (Phase C of the
    # 2026-05-25 graphrag-entity-quality plan). The nested `aliases apply`
    # subcommand mirrors the `communities {build,refresh,list}` group shape;
    # its MCP twin is `brain_graphrag_aliases_apply`.
    "aliases apply": "brain_graphrag_aliases_apply",
}

_MCP_TOOL_PREFIX = "brain_graphrag_"


def _collect_cli_commands(app: typer.Typer, prefix: str = "") -> set[str]:
    """Recursively collect fully-qualified command names from a Typer app.

    Top-level commands return their bare name; commands inside a sub-group return
    "<group-name> <command-name>". Group callbacks (e.g. the ``communities``
    ``invoke_without_command`` callback) are NOT commands and are excluded.
    """
    commands: set[str] = set()
    for command in app.registered_commands:
        # A command's invoked name defaults to the callback function name when
        # not given explicitly; every graphrag command sets it explicitly.
        name = command.name or (command.callback.__name__ if command.callback else None)
        if name is None:  # pragma: no cover - defensive; all commands are named
            raise AssertionError("encountered a Typer command with no resolvable name")
        commands.add(f"{prefix}{name}")
    for group in app.registered_groups:
        group_prefix = f"{prefix}{group.name} "
        commands |= _collect_cli_commands(group.typer_instance, group_prefix)
    return commands


def _collect_mcp_graphrag_tools() -> set[str]:
    """Collect the live ``brain_graphrag_*`` tool names from the FastMCP app."""
    tools = asyncio.run(mcp_app.list_tools())
    return {tool.name for tool in tools if tool.name.startswith(_MCP_TOOL_PREFIX)}


def test_cli_graphrag_commands_match_expected_mapping() -> None:
    """Every actual CLI graphrag subcommand is accounted for in the mapping."""
    actual = _collect_cli_commands(graphrag_app)
    expected = set(EXPECTED_MAPPING.keys())
    assert actual == expected, (
        "brain graphrag CLI subcommands drifted from the parity mapping. "
        f"missing-from-mapping={sorted(actual - expected)} "
        f"stale-in-mapping={sorted(expected - actual)}"
    )


def test_mcp_graphrag_tools_match_expected_mapping() -> None:
    """Every actual MCP graphrag tool is accounted for in the mapping."""
    actual = _collect_mcp_graphrag_tools()
    expected = set(EXPECTED_MAPPING.values())
    assert actual == expected, (
        "brain_graphrag_* MCP tools drifted from the parity mapping. "
        f"missing-from-mapping={sorted(actual - expected)} "
        f"stale-in-mapping={sorted(expected - actual)}"
    )


def test_cli_mcp_graphrag_parity_is_bijective() -> None:
    """The CLI and MCP surfaces are a strict 1:1 (10↔10) mapping.

    This is the headline guard: it cross-checks the live CLI registry against the
    live MCP registry *through* the mapping, so a new command on either side that
    lacks its twin breaks the build.
    """
    cli_commands = _collect_cli_commands(graphrag_app)
    mcp_tools = _collect_mcp_graphrag_tools()

    # Mapping integrity: bijective (no duplicate targets).
    assert len(EXPECTED_MAPPING) == len(set(EXPECTED_MAPPING.values())), (
        "EXPECTED_MAPPING is not bijective — two CLI commands map to one MCP tool"
    )

    # Both surfaces match the mapping exactly.
    assert cli_commands == set(EXPECTED_MAPPING.keys())
    assert mcp_tools == set(EXPECTED_MAPPING.values())

    # And the counts line up (10↔10 today; the assertion tracks the mapping).
    assert len(cli_commands) == len(mcp_tools) == len(EXPECTED_MAPPING)

    # Every CLI command maps to a real MCP tool and vice-versa.
    mapped_from_cli = {EXPECTED_MAPPING[name] for name in cli_commands}
    assert mapped_from_cli == mcp_tools
