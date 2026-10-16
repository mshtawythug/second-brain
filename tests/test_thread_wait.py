"""Tests for ``tests/thread_wait.py`` and the backstop guard over every test module.

``wait_for`` is shared by the vault-watcher, the ``brain vault sync --watch``
CLI and the wiki build-watcher tests (2026-10-16). One guard here covers all
of them, and ``_wait_for_observer`` too. It parses every ``tests/**/*.py``
rather than a list of files, because PR #31's per-file guard (2026-10-15) never
saw ``test_cli_vault_watch.py``, which imported the helper from another test
module and passed ``timeout=5.0``.

What it does NOT see, deliberately: dynamic access (``importlib``,
``__import__``, ``getattr``, ``globals()``), a rebinding chain deeper than one
level, a helper stored under a new name inside a container or a class body
(``staticmethod(wait_for)``), and a hand-rolled poll loop that never calls the
helper at all. Each needs data-flow analysis out of proportion for a test-suite
convention; the plan records them.
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
# ``tests/test_vault_watcher.py``'s observer-start wait. Same backstop rule; its
# floor is pinned there by ``test_wait_for_observer_timeout_default_is_a_backstop``.
_OBSERVER_WAIT = "_wait_for_observer"
# Where ``timeout`` sits positionally in each guarded signature.
_HELPER_TIMEOUT_INDEX = 1
_OBSERVER_TIMEOUT_INDEX = 0
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


def _bound_names(node: ast.AST) -> list[str]:
    """Names an assignment binds, through tuple, list and starred targets."""
    if isinstance(node, ast.Assign):
        targets: list[ast.expr] = list(node.targets)
    elif isinstance(node, (ast.AnnAssign, ast.NamedExpr)):
        targets = [node.target]
    else:
        return []
    return [
        n.id
        for target in targets
        for n in ast.walk(target)
        if isinstance(n, ast.Name) and isinstance(n.ctx, ast.Store)
    ]


def _names_helper(expr: ast.AST | None, direct: set[str], modules: set[str]) -> bool:
    """Is ``expr`` a reference to the shared helper itself (not a call of it)?"""
    if isinstance(expr, ast.Name):
        return expr.id in direct or expr.id in _WAIT_NAMES
    if isinstance(expr, ast.Attribute):
        return expr.attr in _WAIT_NAMES or (
            expr.attr == "wait_for" and ast.unparse(expr.value) in modules
        )
    return False


def _helper_bindings(tree: ast.Module) -> tuple[set[str], set[str], set[str]]:
    """Return (imported names, those plus one level of rebinding, module names)."""
    imported: set[str] = set()
    modules: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.level == 0:
            for alias in node.names:
                if node.module == _HELPER_MODULE and alias.name == "wait_for":
                    imported.add(alias.asname or alias.name)
                elif node.module == "tests" and alias.name == "thread_wait":
                    modules.add(alias.asname or alias.name)
        elif isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name == _HELPER_MODULE:
                    modules.add(alias.asname or alias.name)
    # One level of rebinding: ``w = _wait_for`` makes ``w(p, timeout=1.0)`` a
    # helper call (PR #35 review). Deeper chains are recorded residue.
    rebound = {
        name
        for node in ast.walk(tree)
        if isinstance(node, (ast.Assign, ast.AnnAssign, ast.NamedExpr))
        and _names_helper(node.value, imported, modules)
        for name in _bound_names(node)
    }
    return imported, imported | rebound, modules


def _timeout_index(func: ast.expr, direct: set[str]) -> int | None:
    """``timeout``'s positional index if ``func`` is a guarded wait, else None.

    An attribute call is matched on its attribute name whatever its base, so
    ``tvw._wait_for(...)`` through a test module and ``self._wait_for(...)``
    are both checked (PR #35 review).
    """
    if isinstance(func, ast.Name):
        name = func.id
        if name in direct:
            return _HELPER_TIMEOUT_INDEX
    elif isinstance(func, ast.Attribute):
        name = func.attr
    else:
        return None
    if name in _WAIT_NAMES:
        return _HELPER_TIMEOUT_INDEX
    if name == _OBSERVER_WAIT:
        return _OBSERVER_TIMEOUT_INDEX
    return None


def _has_timeout_keyword(node: ast.Call) -> bool:
    # ``k.arg is None`` is a ``**kwargs`` splat, which can carry a timeout the
    # keyword-name check alone would miss (PR #31 review R1).
    return any(k.arg in (None, "timeout") for k in node.keywords)


def _passes_timeout(node: ast.Call, index: int) -> bool:
    return (
        len(node.args) > index
        # A ``*args`` splat can carry a positional timeout.
        or any(isinstance(arg, ast.Starred) for arg in node.args)
        or _has_timeout_keyword(node)
    )


def _binds_a_timeout_to_the_helper(
    node: ast.Call, direct: set[str], modules: set[str]
) -> bool:
    """``functools.partial(wait_for, timeout=2.0)`` under any name, annotated or not."""
    for i, arg in enumerate(node.args):
        if _names_helper(arg, direct, modules):
            rest = node.args[i + 1 :]
            return (
                _has_timeout_keyword(node)
                or len(rest) >= 2
                or any(isinstance(a, ast.Starred) for a in rest)
            )
    return False


def _mirror_sites(path: Path, tree: ast.Module) -> list[str]:
    sites = []
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            if node.name in _WAIT_NAMES:
                sites.append(_site(path, node))
        elif any(name in _WAIT_NAMES for name in _bound_names(node)):
            sites.append(_site(path, node))
    return sites


def _reexport_sites(path: Path, tree: ast.Module) -> list[str]:
    return [
        f"{_site(path, node)} (imports {alias.name} "
        f"from {'.' * node.level}{node.module or ''})"
        for node in ast.walk(tree)
        if isinstance(node, ast.ImportFrom)
        and (node.level != 0 or node.module != _HELPER_MODULE)
        for alias in node.names
        if alias.name in _WAIT_NAMES
    ]


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
            found["mirrors"].extend(_mirror_sites(path, tree))
            found["reexports"].extend(_reexport_sites(path, tree))
        imported, direct, modules = _helper_bindings(tree)
        calls = [node for node in ast.walk(tree) if isinstance(node, ast.Call)]
        if (imported or modules) and any(
            _timeout_index(c.func, direct) == _HELPER_TIMEOUT_INDEX for c in calls
        ):
            found["importers"].append(path.relative_to(_TESTS_ROOT).as_posix())
        if path == _SELF:
            continue
        for call in calls:
            index = _timeout_index(call.func, direct)
            if (index is not None and _passes_timeout(call, index)) or (
                _binds_a_timeout_to_the_helper(call, direct, modules)
            ):
                found["overrides"].append(_site(path, call))
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
    """No call of ``wait_for`` or ``_wait_for_observer`` passes a timeout.

    Moved here from ``test_vault_watcher.py`` (PR #31) and widened from one
    file to every test module. Pinning the default alone would not hold: one
    ``timeout=2.0`` at a call site brings the flake straight back. "Any
    spelling" covers a keyword, a positional, ``*``/``**`` splats, an import
    alias, one level of rebinding (``w = _wait_for``), an attribute call on any
    base (``tvw._wait_for``, ``self._wait_for``) and a timeout bound in with
    ``functools.partial``.
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
