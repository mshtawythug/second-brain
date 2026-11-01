"""The MCP server's state builder, shared by stdio and HTTP (RM-46, D5/D8).

``mcp_state.build_state`` is the block ``mcp_server.main()`` used to inline. It
moved to a leaf so ``brain mcp serve`` can build the same state without
duplicating startup; ``mcp_server._State`` stays importable for the tests that
build state by hand. No database is opened: the persistent connection is lazy.
"""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

from brain import mcp_server, mcp_state
from brain.config import Config
from brain.db import PersistentConnection
from brain.embeddings import NullEmbedder
from tests.conftest import isolate_config_dotenv

pytestmark = pytest.mark.nodb


@pytest.fixture
def none_cfg(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Config:
    """A config with the FTS-only embedder, so no Ollama is touched."""
    isolate_config_dotenv(monkeypatch, tmp_path)
    monkeypatch.setenv("BRAIN_EMBEDDER", "none")
    monkeypatch.setenv("DATABASE_URL", "postgresql://synthetic@localhost:1/none")
    return Config.load()


def test_mcp_server_state_class_is_the_leafs_class() -> None:
    assert mcp_server._State is mcp_state.McpState


def test_build_state_wires_every_dependency(none_cfg: Config) -> None:
    state = mcp_state.build_state(none_cfg)

    assert state.cfg is none_cfg
    assert isinstance(state.embedder, NullEmbedder)
    assert state.enricher is not None
    assert state.graph_syncer is not None
    assert isinstance(state.db_conn, PersistentConnection)


def test_build_state_without_a_persistent_connection(none_cfg: Config) -> None:
    # HTTP (RM-46.D5): each tool call opens its own connection through
    # ``_mcp_conn``'s per-call path, because calls run concurrently on threads.
    state = mcp_state.build_state(none_cfg, persistent_db=False)

    assert state.db_conn is None


def test_install_state_sets_the_global_the_tools_read(
    none_cfg: Config, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(mcp_server, "_state", None)
    state = mcp_state.build_state(none_cfg, persistent_db=False)

    mcp_server.install_state(state)

    assert mcp_server._get_state() is state


def test_mcp_state_imports_cold_without_the_server() -> None:
    # A leaf: importing it must not drag in the 5,000-line server module (or
    # cycle back into it).
    code = (
        "import sys, brain.mcp_state;"
        "sys.exit(1 if 'brain.mcp_server' in sys.modules else 0)"
    )
    proc = subprocess.run([sys.executable, "-c", code], capture_output=True, check=False)
    assert proc.returncode == 0, proc.stderr.decode()
