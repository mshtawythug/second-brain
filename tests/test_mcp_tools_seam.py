"""RM-06: the ``brain.mcp_tools`` registration seam.

A tool module receives the server's helpers as a :class:`~brain.mcp_tools.ToolContext` instead of
importing ``brain.mcp_server`` (which, under ``python -m brain.mcp_server``, would be a second
copy whose ``_state`` is ``None``). These tests drive the seam on a throwaway server with the
test-only probe module, so no real tool module is touched.
"""

from __future__ import annotations

import ast
import asyncio
import importlib.util
import types
from collections.abc import Iterator
from pathlib import Path

import pytest

from brain import mcp_server, mcp_tools
from brain.errors import BrainError
from brain.mcp_compat import make_server
from brain.mcp_tools import ContextSlot, ToolContext, ToolRegistrationError, _context
from tests import mcp_extension_probe as probe


class _Sentinel:
    """Stands in for ``mcp_server._State``; the probe reports this class name."""


def _server_context() -> ToolContext:
    """The context ``mcp_server`` builds for its extensions: its own helpers."""
    return ToolContext(
        get_state=mcp_server._get_state,
        connection=mcp_server._mcp_conn,
        error=mcp_server._mcp_error,
        wrap_db_error=mcp_server._wrap_db_error,
        wrap_embed_error=mcp_server._wrap_embed_error,
        confidential_lens=mcp_server._confidential_lens,
    )


def _other_context() -> ToolContext:
    """A context from somewhere else (as a second module copy would build one)."""

    def _get_state() -> object:
        raise BrainError("the other copy's state")

    return ToolContext(
        get_state=_get_state,
        connection=mcp_server._mcp_conn,
        error=mcp_server._mcp_error,
        wrap_db_error=mcp_server._wrap_db_error,
        wrap_embed_error=mcp_server._wrap_embed_error,
        confidential_lens=mcp_server._confidential_lens,
    )


def _tool_names(app: object) -> set[str]:
    return {tool.name for tool in asyncio.run(app.list_tools())}  # type: ignore[attr-defined]


@pytest.fixture
def unbound_probe() -> Iterator[None]:
    """Start and end each test with the probe module holding no context."""
    mcp_tools._reset_contexts_for_tests([probe])
    yield
    mcp_tools._reset_contexts_for_tests([probe])


#: A real tool module that must be discovered and bound (RM-12 shipped the first). The tests
#: that iterate the real modules assert it is among them, so none can pass on an empty list
#: (they did, vacuously, from RM-06 until RM-12: aud-68's carry-forward).
REAL_TOOL_MODULE = "brain.mcp_tools.related"


def _real_contexts() -> dict[str, ToolContext | None]:
    return {
        module.__name__: module._CONTEXT.current
        for module in mcp_tools.discover_tool_modules()
    }


# --- AC2: register a test module on a throwaway server ---------------------


@pytest.mark.usefixtures("unbound_probe")
def test_register_extension_tools_with_modules_registers_only_those() -> None:
    # Arrange
    app = make_server("rm06-throwaway")

    # Act
    mcp_tools.register_extension_tools(app, _server_context(), modules=[probe])

    # Assert
    assert _tool_names(app) == {"brain_probe_state"}
    assert "brain_probe_state" not in _tool_names(mcp_server.mcp_app)


@pytest.mark.usefixtures("unbound_probe")
def test_extension_tool_reads_the_state_installed_on_mcp_server(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Arrange
    app = make_server("rm06-throwaway")
    mcp_tools.register_extension_tools(app, _server_context(), modules=[probe])
    monkeypatch.setattr(mcp_server, "_state", _Sentinel())

    # Act
    result = probe.brain_probe_state()

    # Assert
    assert result["state_type"] == "_Sentinel"
    assert result["context"]["get_state"] == "brain.mcp_server._get_state"


@pytest.mark.usefixtures("unbound_probe")
def test_modules_filter_leaves_real_tool_modules_untouched() -> None:
    # Arrange
    before = _real_contexts()
    assert before[REAL_TOOL_MODULE] == _server_context(), "the real module must start bound"
    app = make_server("rm06-throwaway")

    # Act
    mcp_tools.register_extension_tools(app, _other_context(), modules=[probe])

    # Assert
    assert _real_contexts() == before


@pytest.mark.usefixtures("unbound_probe")
def test_a_module_without_register_is_refused() -> None:
    # Arrange
    app = make_server("rm06-throwaway")
    bare = types.ModuleType("brain.mcp_tools.bare")

    # Act / Assert
    with pytest.raises(ToolRegistrationError, match=r"brain\.mcp_tools\.bare"):
        mcp_tools.register_extension_tools(app, _server_context(), modules=[bare])


# --- AC3: the refusal to rebind ---------------------------------------------


@pytest.mark.usefixtures("unbound_probe")
def test_rebinding_a_different_context_raises() -> None:
    # Arrange
    first = _server_context()
    probe.register(make_server("rm06-first"), first)
    second_app = make_server("rm06-second")

    # Act / Assert: a BrainError, so it reaches an MCP caller as a clean error.
    with pytest.raises(BrainError, match="different ToolContext") as refused:
        probe.register(second_app, _other_context())
    assert refused.type is ToolRegistrationError
    assert probe._CONTEXT.current is first
    assert _tool_names(second_app) == set()


@pytest.mark.usefixtures("unbound_probe")
def test_rebinding_after_reset_is_allowed() -> None:
    # Arrange
    probe.register(make_server("rm06-first"), _server_context())
    other = _other_context()

    # Act
    mcp_tools._reset_contexts_for_tests([probe])
    probe.register(make_server("rm06-second"), other)

    # Assert
    assert probe._CONTEXT.current is other


@pytest.mark.usefixtures("unbound_probe")
def test_rebinding_the_same_context_is_a_no_op() -> None:
    # Arrange
    ctx = _server_context()
    probe.register(make_server("rm06-first"), ctx)

    # Act
    probe.register(make_server("rm06-second"), _server_context())

    # Assert
    assert probe._CONTEXT.current is ctx


@pytest.mark.usefixtures("unbound_probe")
def test_an_unregistered_tool_module_fails_loudly() -> None:
    # Arrange: the fixture left the probe unbound.

    # Act / Assert
    with pytest.raises(ToolRegistrationError, match="not registered"):
        probe.brain_probe_state()


_OTHER_SLOT = "tests.rm06_other_slot"


@pytest.mark.usefixtures("unbound_probe")
def test_reset_with_modules_unbinds_only_those_slots(monkeypatch: pytest.MonkeyPatch) -> None:
    # Arrange: a second slot in the registry. The setitem records that the key was absent,
    # so teardown removes it; the ContextSlot constructor then replaces the placeholder.
    monkeypatch.setitem(_context._SLOTS, _OTHER_SLOT, None)
    other = ContextSlot(_OTHER_SLOT)
    ctx = _server_context()
    other.bind(ctx)
    probe.register(make_server("rm06-first"), ctx)

    # Act
    mcp_tools._reset_contexts_for_tests([probe])

    # Assert
    assert probe._CONTEXT.current is None
    assert other.current is ctx


@pytest.mark.usefixtures("unbound_probe")
def test_reset_with_no_argument_unbinds_every_slot() -> None:
    # Arrange
    probe.register(make_server("rm06-first"), _server_context())
    before = _real_contexts()

    # Act
    mcp_tools._reset_contexts_for_tests()

    # Assert
    assert probe._CONTEXT.current is None
    # Re-register the real modules so the rest of the suite sees them bound.
    for module in mcp_tools.discover_tool_modules():
        ctx = before[module.__name__]
        assert ctx is not None
        module._CONTEXT.bind(ctx)


# --- discovery ---------------------------------------------------------------


def test_discovery_skips_private_modules_and_returns_registrable_ones() -> None:
    # Act
    modules = mcp_tools.discover_tool_modules()

    # Assert
    names = [module.__name__ for module in modules]
    assert names == sorted(names)
    assert all(name.startswith("brain.mcp_tools.") for name in names)
    assert not [name for name in names if name.rsplit(".", 1)[1].startswith("_")]
    assert all(callable(getattr(module, "register", None)) for module in modules)


def test_every_real_tool_module_is_bound_to_the_importable_server() -> None:
    # Act
    contexts = _real_contexts()

    # Assert
    assert REAL_TOOL_MODULE in contexts, sorted(contexts)
    for name, ctx in contexts.items():
        assert ctx is not None, f"{name} was discovered but never registered"
        assert ctx == _server_context(), name


def _imported_modules(tree: ast.Module, package: str) -> Iterator[str]:
    """Every module an import statement in ``tree`` names, resolved to absolute form."""
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            yield from (alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            relative = "." * node.level + (node.module or "")
            base = importlib.util.resolve_name(relative, package) if node.level else relative
            yield base
            yield from (f"{base}.{alias.name}" for alias in node.names)


def _offenders(package_dir: Path, package: str) -> list[str]:
    """Files under ``package_dir`` (subpackages included) that import ``brain.mcp_server``.

    Each file's relative imports resolve against that file's OWN package, which is
    ``package`` plus its subdirectory path: a module in a subpackage that writes
    ``from ... import mcp_server`` reaches ``brain.mcp_server`` too.
    """
    offenders = []
    for path in sorted(package_dir.rglob("*.py")):
        relative_dir = path.parent.relative_to(package_dir)
        own_package = ".".join([package, *relative_dir.parts])
        tree = ast.parse(path.read_text(encoding="utf-8"))
        if "brain.mcp_server" in set(_imported_modules(tree, own_package)):
            offenders.append(path.relative_to(package_dir).as_posix())
    return offenders


def test_no_tool_module_imports_brain_mcp_server() -> None:
    """A tool module importing ``brain.mcp_server`` gets a second copy under ``python -m``."""
    # Arrange
    package_dir = Path(mcp_tools.__file__).parent

    # Act
    offenders = _offenders(package_dir, "brain.mcp_tools")

    # Assert
    assert not offenders, f"these mcp_tools modules import brain.mcp_server: {offenders}"


def test_the_scan_reaches_subpackages_with_their_own_package(tmp_path: Path) -> None:
    """Discovery imports subpackages too, so the scan must read them, resolved correctly."""
    # Arrange: a subpackage whose module reaches brain.mcp_server only through ``...``,
    # and a sibling whose ``..`` names brain.mcp_tools.mcp_server (not the server).
    sub = tmp_path / "sub"
    sub.mkdir()
    (tmp_path / "__init__.py").write_text("", encoding="utf-8")
    (sub / "__init__.py").write_text("", encoding="utf-8")
    (sub / "leaky.py").write_text("from ... import mcp_server\n", encoding="utf-8")
    (sub / "fine.py").write_text("from .. import mcp_server\n", encoding="utf-8")

    # Act
    offenders = _offenders(tmp_path, "brain.mcp_tools")

    # Assert
    assert offenders == ["sub/leaky.py"]


def test_the_import_scan_sees_every_spelling() -> None:
    """The scan above is only a guard if it recognises each way of writing the import."""
    for source in (
        "import brain.mcp_server",
        "from brain import mcp_server",
        "from brain.mcp_server import _get_state",
        "from .. import mcp_server",
        "from ..mcp_server import _get_state",
    ):
        assert "brain.mcp_server" in set(
            _imported_modules(ast.parse(source), "brain.mcp_tools")
        ), source
