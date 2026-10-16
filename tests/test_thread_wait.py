"""Tests for ``tests/thread_wait.py`` and the backstop guard over every importer.

``wait_for`` is shared by the vault-watcher, the ``brain vault sync --watch``
CLI and the wiki build-watcher tests (2026-10-16). One guard here covers all
of them. Importers are DISCOVERED by parsing every ``tests/**/*.py`` rather than
listed, because PR #31's per-file guard (2026-10-15) never saw
``test_cli_vault_watch.py``, which imported the helper from another test module
and passed ``timeout=5.0``.
"""
from __future__ import annotations

import ast
import functools
import inspect
from pathlib import Path

import pytest

from tests import thread_wait
from tests.thread_wait import WAIT_FOR_BACKSTOP_S, wait_for

_TESTS_ROOT = Path(__file__).resolve().parent
_HELPER_MODULE = "tests.thread_wait"
_HELPER_FILE = _TESTS_ROOT / "thread_wait.py"
# Exempt from the call-site check ONLY: the raise-path test below must pass a
# short timeout, and this file starts no threads.
_SELF = Path(__file__).resolve()
_WAIT_NAMES = frozenset({"wait_for", "_wait_for"})
# Anything under this reintroduces the latency budget the backstop replaced.
_FLOOR_S = 5.0
# Non-vacuity: if discovery stops finding these, the guard must go red rather
# than pass over nothing.
_KNOWN_IMPORTERS = frozenset(
    {
        "test_vault_watcher.py",
        "test_cli_vault_watch.py",
        "wiki/test_build_watcher.py",
    }
)


def _site(path: Path, node: ast.AST) -> str:
    return f"{path.relative_to(_TESTS_ROOT).as_posix()}:{getattr(node, 'lineno', 0)}"


def _helper_bindings(tree: ast.Module) -> tuple[set[str], set[str]]:
    """Return (names bound to ``wait_for``, dotted names bound to its module)."""
    direct: set[str] = set()
    modules: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.level == 0:
            for alias in node.names:
                if node.module == _HELPER_MODULE and alias.name == "wait_for":
                    direct.add(alias.asname or alias.name)
                elif node.module == "tests" and alias.name == "thread_wait":
                    modules.add(alias.asname or alias.name)
        elif isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name == _HELPER_MODULE:
                    modules.add(alias.asname or alias.name)
    return direct, modules


def _is_helper_call(node: ast.Call, direct: set[str], modules: set[str]) -> bool:
    func = node.func
    if isinstance(func, ast.Name):
        return func.id in direct
    if isinstance(func, ast.Attribute) and func.attr == "wait_for":
        return ast.unparse(func.value) in modules
    return False


def _passes_timeout(node: ast.Call) -> bool:
    return (
        len(node.args) > 1
        # A ``*args`` splat can carry a positional timeout.
        or any(isinstance(arg, ast.Starred) for arg in node.args)
        # ``k.arg is None`` is a ``**kwargs`` splat, which can carry a timeout
        # the keyword-name check alone would miss (PR #31 review R1).
        or any(k.arg in (None, "timeout") for k in node.keywords)
    )


@functools.cache
def _scan() -> dict[str, list[str]]:
    """One pass over every test module; each list holds ``path:line`` sites."""
    found: dict[str, list[str]] = {
        "overrides": [],
        "mirrors": [],
        "reexports": [],
        "importers": [],
    }
    for path in sorted(_TESTS_ROOT.rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        if path != _HELPER_FILE:
            for node in ast.walk(tree):
                if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    if node.name in _WAIT_NAMES:
                        found["mirrors"].append(_site(path, node))
                elif isinstance(node, ast.Assign):
                    if any(
                        isinstance(t, ast.Name) and t.id in _WAIT_NAMES
                        for t in node.targets
                    ):
                        found["mirrors"].append(_site(path, node))
                elif isinstance(node, ast.ImportFrom) and (
                    node.level != 0 or node.module != _HELPER_MODULE
                ):
                    for alias in node.names:
                        if alias.name in _WAIT_NAMES:
                            found["reexports"].append(
                                f"{_site(path, node)} (imports {alias.name} "
                                f"from {'.' * node.level}{node.module or ''})"
                            )
        direct, modules = _helper_bindings(tree)
        if not (direct or modules):
            continue
        calls = [
            node
            for node in ast.walk(tree)
            if isinstance(node, ast.Call) and _is_helper_call(node, direct, modules)
        ]
        if calls:
            found["importers"].append(path.relative_to(_TESTS_ROOT).as_posix())
        if path == _SELF:
            continue
        found["overrides"].extend(_site(path, n) for n in calls if _passes_timeout(n))
    return found


def test_wait_for_returns_as_soon_as_the_predicate_holds() -> None:
    """A passing wait costs the time to truth, never the backstop."""
    polls: list[int] = []

    def _third_poll_is_true() -> bool:
        polls.append(1)
        return len(polls) >= 3

    wait_for(_third_poll_is_true)

    assert len(polls) == 3


def test_wait_for_raises_a_backstop_message_on_its_deadline() -> None:
    """The failure names itself a backstop so nobody "fixes" it by shortening it."""
    with pytest.raises(AssertionError, match=r"backstop, not a latency assertion"):
        thread_wait.wait_for(lambda: False, timeout=0.05)


def test_wait_for_backstop_floor() -> None:
    """The constant and the signature default are pinned separately.

    A literal default (``timeout: float = 2.0``) with the constant left at 10.0
    would satisfy a constant-only check.
    """
    default = inspect.signature(wait_for).parameters["timeout"].default

    assert WAIT_FOR_BACKSTOP_S >= _FLOOR_S, (
        f"wait_for's timeout backstop was lowered to {WAIT_FOR_BACKSTOP_S}s; "
        "see its docstring — this is not a latency assertion"
    )
    assert default >= _FLOOR_S, (
        f"wait_for's signature default was lowered to {default}s; "
        "it must be WAIT_FOR_BACKSTOP_S"
    )


def test_wait_for_timeout_is_a_backstop_at_every_call_site() -> None:
    """No importer of ``wait_for`` passes a timeout, in any spelling.

    Moved here from ``test_vault_watcher.py`` (PR #31) and widened from one
    file to every importer. Pinning the default alone would not hold: one
    ``timeout=2.0`` at a call site brings the flake straight back.
    """
    overrides = _scan()["overrides"]

    assert overrides == [], (
        f"wait_for called with an explicit timeout at {overrides}; "
        "it is a backstop — let the default apply"
    )


def test_no_test_module_defines_its_own_wait_helper() -> None:
    """A local ``_wait_for`` is how the build-watcher tests kept a 2.0s budget."""
    mirrors = _scan()["mirrors"]

    assert mirrors == [], (
        f"a test module defines its own wait helper at {mirrors}; "
        f"import wait_for from {_HELPER_MODULE} instead"
    )


def test_no_test_module_imports_a_wait_helper_from_elsewhere() -> None:
    """Importing the helper through another test module hides it from the scan."""
    reexports = _scan()["reexports"]

    assert reexports == [], (
        f"wait helper imported from a module other than {_HELPER_MODULE}: {reexports}"
    )


def test_guard_discovers_every_known_importer() -> None:
    """The scan must find the files it exists to police, or it proves nothing."""
    importers = set(_scan()["importers"])

    missing = sorted(_KNOWN_IMPORTERS - importers)

    assert missing == [], (
        f"importers not discovered: {missing}; the call-site guard is scanning nothing "
        "for them"
    )
