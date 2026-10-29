"""Only the pipeline (and a short, reasoned allowlist) may CALL the search (RM-09).

Every user-facing surface goes through :func:`brain.search_pipeline.run_search`,
so a stage wired in ``build_stages`` reaches all of them. This guard walks the
AST of every module under ``src/brain`` and fails on any call of
``hybrid_search(…)`` or ``search_with_fts_fallback(…)``, as a bare name or an
attribute, outside the allowlist. Guarding the fallback too is what stops a
seventh surface from getting the fallback while silently skipping the stages.

Importing or PASSING ``hybrid_search`` (the patch points in ``cli.py``,
``cli_search.py``, ``ui/server.py``, ``mcp_server.py``, ``recall.py`` and
``ask.py``) is allowed: only a call is a search.

The checker is also run on synthetic modules in ``tmp_path`` (acceptance
criterion 4), so the guard is shown able to fail.
"""
from __future__ import annotations

import ast
from pathlib import Path

import pytest

pytestmark = pytest.mark.nodb

SRC = Path(__file__).resolve().parent.parent / "src" / "brain"

#: The calls this guard is about.
GUARDED = frozenset({"hybrid_search", "search_with_fts_fallback"})

#: Modules that may call them, each for its recorded reason.
ALLOWED_MODULES = frozenset(
    {
        "search.py",  # defines hybrid_search
        "search_fallback.py",  # the fallback policy itself
        "search_pipeline.py",  # the one composition point
        "eval/runner.py",  # measures the real pipeline, never falls back (§2.7)
        "graph_rag/fuse.py",  # consumes the hybrid ranking unchanged
        "demo/__init__.py",  # the sandbox's own query path
    }
)

#: ``(module, function)`` pairs that may call them: the patch-point shim that
#: forwards to ``cli.hybrid_search``.
ALLOWED_FUNCTIONS = frozenset({("cli_search.py", "hybrid_search")})


def _called_name(node: ast.Call) -> str | None:
    func = node.func
    if isinstance(func, ast.Name):
        return func.id
    if isinstance(func, ast.Attribute):
        return func.attr
    return None


def violations(root: Path) -> list[str]:
    """Every guarded call under ``root`` outside the allowlist, as ``path:line``."""
    found: list[str] = []
    for path in sorted(root.rglob("*.py")):
        rel = path.relative_to(root).as_posix()
        if rel in ALLOWED_MODULES:
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        found.extend(_violations_in(tree, rel, enclosing=None))
    return found


def _violations_in(node: ast.AST, rel: str, *, enclosing: str | None) -> list[str]:
    found: list[str] = []
    for child in ast.iter_child_nodes(node):
        scope = enclosing
        if isinstance(child, ast.FunctionDef | ast.AsyncFunctionDef):
            scope = child.name if enclosing is None else enclosing
        if (
            isinstance(child, ast.Call)
            and _called_name(child) in GUARDED
            and (rel, scope) not in ALLOWED_FUNCTIONS
        ):
            found.append(f"{rel}:{child.lineno}")
        found.extend(_violations_in(child, rel, enclosing=scope))
    return found


def test_no_module_calls_the_search_outside_the_pipeline() -> None:
    assert violations(SRC) == [], (
        "call brain.search_pipeline.run_search instead, so the configured "
        "stages apply; add to the allowlist here only with a recorded reason"
    )


def test_every_allowlisted_module_exists() -> None:
    """A renamed module must not leave a stale exemption behind."""
    for rel in ALLOWED_MODULES | {module for module, _ in ALLOWED_FUNCTIONS}:
        assert (SRC / rel).is_file(), rel


# --- the checker can fail (criterion 4) -----------------------------------


def _write(root: Path, rel: str, source: str) -> None:
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(source, encoding="utf-8")


@pytest.mark.parametrize(
    "source",
    [
        "from brain.search import hybrid_search\n"
        "def f(conn):\n    return hybrid_search(conn, query='q')\n",
        "import brain.search\n"
        "def f(conn):\n    return brain.search.hybrid_search(conn, query='q')\n",
        "from brain.search_fallback import search_with_fts_fallback\n"
        "def f(fn, conn):\n    return search_with_fts_fallback(fn, conn, query='q')\n",
        "from brain import search\nRESULTS = search.hybrid_search(None)\n",
    ],
    ids=["bare-name", "attribute", "fallback", "module-level"],
)
def test_a_direct_call_is_caught(tmp_path: Path, source: str) -> None:
    # Arrange
    _write(tmp_path, "new_surface.py", source)

    # Act
    found = violations(tmp_path)

    # Assert
    assert len(found) == 1
    assert found[0].startswith("new_surface.py:")


def test_importing_or_passing_is_allowed(tmp_path: Path) -> None:
    # Arrange
    _write(
        tmp_path,
        "new_surface.py",
        "from brain.search import hybrid_search\n"
        "from brain.search_pipeline import run_search\n"
        "def f(conn, embedder):\n"
        "    return run_search(hybrid_search, conn, embedder=embedder, query='q', limit=5)\n",
    )

    # Act / Assert
    assert violations(tmp_path) == []


def test_the_shim_exemption_is_one_function_not_the_module(tmp_path: Path) -> None:
    # Arrange: the shim's own call is exempt; any other call in that module is not.
    _write(
        tmp_path,
        "cli_search.py",
        "def hybrid_search(*args, **kwargs):\n"
        "    from brain import cli as _cli\n"
        "    return _cli.hybrid_search(*args, **kwargs)\n"
        "def search(conn):\n"
        "    return hybrid_search(conn, query='q')\n",
    )

    # Act
    found = violations(tmp_path)

    # Assert
    assert found == ["cli_search.py:5"]


def test_an_allowlisted_module_may_call(tmp_path: Path) -> None:
    # Arrange
    _write(
        tmp_path,
        "eval/runner.py",
        "from brain.search import hybrid_search\n"
        "def run(conn):\n    return hybrid_search(conn, query='q')\n",
    )

    # Act / Assert
    assert violations(tmp_path) == []
