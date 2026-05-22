"""Tests for ``brain.graph_rag.tenancy.resolve_tenant`` (wave G2-a).

Pure-logic unit tests over the single tenant-resolution helper the G2 retrieval
CLI / MCP / themes paths share. ``Config`` is a frozen dataclass with only
``database_url`` required, so each test constructs a minimal one directly (no
env / DB needed) and pins ``graph_tenant_id`` explicitly — no mystery guests.
"""
from __future__ import annotations

import pytest

from brain.config import Config
from brain.errors import GraphTenantError
from brain.graph_rag.tenancy import resolve_tenant


def _cfg(tenant: str = "default") -> Config:
    """Minimal Config carrying just the graph tenant default under test."""
    return Config(database_url="postgresql://x:y@h:5432/d", graph_tenant_id=tenant)


def test_uses_config_default_when_no_override() -> None:
    assert resolve_tenant(_cfg("acme")) == "acme"


def test_default_default_tenant() -> None:
    # The local single-user default flows straight through.
    assert resolve_tenant(_cfg()) == "default"


def test_explicit_none_override_uses_default() -> None:
    assert resolve_tenant(_cfg("acme"), None) == "acme"


@pytest.mark.parametrize("override", ["", "   ", "\t", "\n"])
def test_blank_override_falls_back_to_default(override: str) -> None:
    # A stray ``--tenant ""`` is "not supplied" -> default (cli precedent),
    # never an empty-tenant query.
    assert resolve_tenant(_cfg("acme"), override) == "acme"


def test_override_wins_over_default() -> None:
    assert resolve_tenant(_cfg("acme"), "globex") == "globex"


def test_override_is_trimmed() -> None:
    assert resolve_tenant(_cfg("acme"), "  globex  ") == "globex"


def test_override_wins_even_when_default_is_blank() -> None:
    # A non-blank override is sufficient on its own; the blank default is moot.
    assert resolve_tenant(_cfg(""), "globex") == "globex"


def test_empty_resolved_tenant_raises() -> None:
    # Blank config default + no override => no valid tenant to scope by.
    with pytest.raises(GraphTenantError, match="tenant_id is empty"):
        resolve_tenant(_cfg(""))


def test_whitespace_default_with_blank_override_raises() -> None:
    with pytest.raises(GraphTenantError, match="tenant_id is empty"):
        resolve_tenant(_cfg("   "), "  ")
