"""Tests for ``tests/thread_wait.py`` and the backstop guard over every test module.

``wait_for`` is shared by the vault-watcher, the ``brain vault sync --watch``
CLI and the wiki build-watcher tests (2026-10-16). One guard here covers all
of them, and ``_wait_for_observer`` too. It parses every ``tests/**/*.py``
rather than a list of files, because PR #31's per-file guard (2026-10-15) never
saw ``test_cli_vault_watch.py``, which imported the helper from another test
module and passed ``timeout=5.0``.

The bare name ``wait_for`` is NOT ours alone: Playwright's ``Locator.wait_for``
and ``asyncio.wait_for`` take a legitimate ``timeout``. So the underscore names
are matched anywhere, and ``wait_for`` only where it provably resolves to
``tests.thread_wait`` (PR #35 re-review).

What it does NOT see, deliberately: the real helper under its public name
through a base it cannot recognise (``holder.wait_for``) or a star import —
the price of not flagging Playwright; dynamic access (``importlib``,
``__import__``, ``getattr``, ``globals()``); a rebinding chain deeper than one
level, or one made through a parameter default (``def go(w=wait_for):``); a
helper stored under a new name inside a container or a class body
(``staticmethod(wait_for)``); and a hand-rolled poll loop that never calls the
helper at all. Each needs data-flow analysis out of proportion for a
test-suite convention; the plan records them.
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
# ``_wait_for_observer``'s one legitimate definition; anywhere else it is a mirror.
_OBSERVER_HOME = _TESTS_ROOT / "test_vault_watcher.py"
# Exempt from the call-site check ONLY: the raise-path test below must pass a
# short timeout, and this file starts no threads.
_SELF = Path(__file__).resolve()
# This suite's own waits, matched as a bare name or an attribute on ANY base
# (``tvw._wait_for``, ``self._wait_for``). Value: ``timeout``'s positional
# index. ``_wait_for_observer`` lives in ``tests/test_vault_watcher.py``, whose
# ``test_wait_for_observer_timeout_default_is_a_backstop`` pins its floor.
_PRIVATE_WAITS = {"_wait_for": 1, "_wait_for_observer": 0}
# The shared helper's public name — also Playwright's and asyncio's, so it is
# matched only through an import from ``tests.thread_wait``.
_PUBLIC_WAIT = "wait_for"
_HELPER_TIMEOUT_INDEX = 1
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


def _site(label: str, node: ast.AST) -> str:
    return f"{label}:{getattr(node, 'lineno', 0)}"


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


def _helper_index(
    expr: ast.AST | None, direct: dict[str, int], modules: set[str]
) -> int | None:
    """``timeout``'s positional index if ``expr`` names a guarded wait, else None."""
    if isinstance(expr, ast.Name):
        if expr.id in direct:
            return direct[expr.id]
        return _PRIVATE_WAITS.get(expr.id)
    if isinstance(expr, ast.Attribute):
        if expr.attr in _PRIVATE_WAITS:
            return _PRIVATE_WAITS[expr.attr]
        if expr.attr == _PUBLIC_WAIT and ast.unparse(expr.value) in modules:
            return _HELPER_TIMEOUT_INDEX
    return None


def _assignments(tree: ast.Module) -> list[ast.Assign | ast.AnnAssign | ast.NamedExpr]:
    return [
        node
        for node in ast.walk(tree)
        if isinstance(node, (ast.Assign, ast.AnnAssign, ast.NamedExpr))
    ]


def _bindings(tree: ast.Module) -> tuple[bool, dict[str, int], set[str]]:
    """Return (imports the helper?, guarded callee names → index, module names).

    Resolves import aliases plus ONE level of rebinding — ``w = _wait_for``,
    ``obs = _wait_for_observer``, ``tw = thread_wait`` (PR #35 reviews).
    Deeper chains are recorded residue.
    """
    imported: dict[str, int] = {}
    modules: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.level == 0:
            for alias in node.names:
                if node.module == _HELPER_MODULE and alias.name == _PUBLIC_WAIT:
                    imported[alias.asname or alias.name] = _HELPER_TIMEOUT_INDEX
                elif node.module == "tests" and alias.name == "thread_wait":
                    modules.add(alias.asname or alias.name)
        elif isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name == _HELPER_MODULE:
                    modules.add(alias.asname or alias.name)
    direct = dict(imported)
    rebound_modules: set[str] = set()
    for node in _assignments(tree):
        if isinstance(node.value, ast.Name) and node.value.id in modules:
            rebound_modules.update(_bound_names(node))
            continue
        index = _helper_index(node.value, imported, modules)
        if index is not None:
            direct.update(dict.fromkeys(_bound_names(node), index))
    return bool(imported or modules), direct, modules | rebound_modules


def _has_timeout_keyword(node: ast.Call) -> bool:
    # ``k.arg is None`` is a ``**kwargs`` splat, which can carry a timeout the
    # keyword-name check alone would miss (PR #31 review R1).
    return any(k.arg in (None, "timeout") for k in node.keywords)


def _passes_timeout(args: list[ast.expr], node: ast.Call, index: int) -> bool:
    return (
        len(args) > index
        # A ``*args`` splat can carry a positional timeout.
        or any(isinstance(arg, ast.Starred) for arg in args)
        or _has_timeout_keyword(node)
    )


def _is_override(node: ast.Call, direct: dict[str, int], modules: set[str]) -> bool:
    """A guarded wait called with a timeout, or one bound in via ``partial``."""
    index = _helper_index(node.func, direct, modules)
    if index is not None:
        return _passes_timeout(node.args, node, index)
    # ``functools.partial(_wait_for, timeout=2.0)`` under any name: the helper
    # is an argument, and what follows it reaches the helper's parameters.
    for i, arg in enumerate(node.args):
        index = _helper_index(arg, direct, modules)
        if index is not None:
            return _passes_timeout(node.args[i + 1 :], node, index)
    return False


def _mirror_sites(label: str, tree: ast.Module, defines: frozenset[str]) -> list[str]:
    """A non-import binding of an underscore wait anywhere, or of ``wait_for`` at module level.

    ``defines`` names the waits this module legitimately owns. A local
    ``_wait_for_observer`` would carry its own default, which the floor pin in
    ``test_vault_watcher.py`` never inspects (PR #35 re-review 3). A
    ``def wait_for`` METHOD (a page object's, say) is not a mirror, so the
    public name is checked only among top-level statements.
    """
    top_level = {id(node) for node in tree.body}
    sites = []
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            names = [node.name]
        else:
            names = _bound_names(node)
        for name in names:
            if name in defines:
                continue
            if name in _PRIVATE_WAITS or (name == _PUBLIC_WAIT and id(node) in top_level):
                sites.append(_site(label, node))
    return sites


def _reexport_sites(label: str, tree: ast.Module) -> list[str]:
    """``_wait_for`` from anywhere but the helper; ``wait_for`` from a test module.

    ``from asyncio import wait_for`` is somebody else's ``wait_for``.
    """
    sites = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.ImportFrom) or (
            node.level == 0 and node.module == _HELPER_MODULE
        ):
            continue
        source = f"{'.' * node.level}{node.module or ''}"
        from_tests = node.level != 0 or source == "tests" or source.startswith("tests.")
        for alias in node.names:
            if alias.name == "_wait_for" or (alias.name == _PUBLIC_WAIT and from_tests):
                sites.append(f"{_site(label, node)} (imports {alias.name} from {source})")
    return sites


def _scan_source(
    label: str,
    source: str,
    *,
    check_calls: bool = True,
    defines: frozenset[str] = frozenset(),
) -> dict[str, list[str]]:
    """Scan one module's source; the unit the probes below exercise directly."""
    tree = ast.parse(source)
    imports_helper, direct, modules = _bindings(tree)
    calls = [node for node in ast.walk(tree) if isinstance(node, ast.Call)]
    uses_helper = any(
        _helper_index(c.func, direct, modules) == _HELPER_TIMEOUT_INDEX for c in calls
    )
    return {
        "overrides": [
            _site(label, c) for c in calls if check_calls and _is_override(c, direct, modules)
        ],
        "mirrors": _mirror_sites(label, tree, defines),
        "reexports": _reexport_sites(label, tree),
        "importers": [label] if imports_helper and uses_helper else [],
    }


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
        label = path.relative_to(_TESTS_ROOT).as_posix()
        one = _scan_source(
            label,
            path.read_text(encoding="utf-8"),
            check_calls=path != _SELF,
            defines=frozenset({"_wait_for_observer"}) if path == _OBSERVER_HOME else frozenset(),
        )
        if path == _HELPER_FILE:
            one["mirrors"] = []
            one["reexports"] = []
        for key, sites in one.items():
            found[key].extend(sites)
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
    alias, one level of rebinding (``w = _wait_for``), an underscore-named call
    on any base (``tvw._wait_for``, ``self._wait_for``) and a timeout bound in
    with ``functools.partial``.
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
        f"a test module defines its own wait helper at {mirrors}; import it from its "
        f"defining module ({_HELPER_MODULE}, or {_OBSERVER_HOME.stem} for _wait_for_observer)"
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


# Third-party ``wait_for``s the guard must leave alone (PR #35 re-review). Each
# would be wrong advice if flagged: their ``timeout`` is a real API argument.
_THIRD_PARTY_WAIT_FOR = {
    "playwright_locator": (
        "def t(page):\n"
        "    page.locator('#a').wait_for(state='visible', timeout=5000)\n"
    ),
    "asyncio_attribute": (
        "import asyncio\n"
        "async def t(c):\n"
        "    await asyncio.wait_for(c, timeout=1.0)\n"
    ),
    "asyncio_bare_import": (
        "from asyncio import wait_for\n"
        "async def t(c):\n"
        "    await wait_for(c, 1.0)\n"
    ),
    "threading_condition": (
        "import threading\n"
        "cond = threading.Condition()\n"
        "cond.wait_for(lambda: True, timeout=1.0)\n"
    ),
    "page_object_method": (
        "class Page:\n"
        "    def wait_for(self, selector, timeout=2.0):\n"
        "        return None\n"
    ),
}

# True positives that must stay red under the narrowed matching. Value: the
# finding kind each must produce.
_TRUE_POSITIVES = {
    "imported_keyword": (
        "from tests.thread_wait import wait_for\nwait_for(p, timeout=1.0)\n",
        "overrides",
    ),
    "module_alias_positional": (
        "from tests import thread_wait as tw\ntw.wait_for(p, 1.0)\n",
        "overrides",
    ),
    "module_rebind": (
        "from tests import thread_wait\ntw = thread_wait\ntw.wait_for(p, timeout=1.0)\n",
        "overrides",
    ),
    "underscore_any_base": ("tvw._wait_for(p, timeout=1.0)\n", "overrides"),
    "observer_partial": (
        "import functools\nquick = functools.partial(_wait_for_observer, timeout=1.0)\n",
        "overrides",
    ),
    "observer_positional_partial": (
        "import functools\nquick = functools.partial(_wait_for_observer, 1.0)\n",
        "overrides",
    ),
    "observer_rebind": ("obs = _wait_for_observer\nobs(1.0)\n", "overrides"),
    "observer_mirror": (
        "def _wait_for_observer(timeout: float = 1.0):\n    return None\n_wait_for_observer()\n",
        "mirrors",
    ),
    "top_level_public_mirror": (
        "def wait_for(predicate, timeout=2.0):\n    return None\n",
        "mirrors",
    ),
    "public_reexport_from_test_module": (
        "from tests.test_vault_watcher import wait_for\n",
        "reexports",
    ),
}


@pytest.mark.parametrize("case", sorted(_THIRD_PARTY_WAIT_FOR))
def test_guard_leaves_third_party_wait_for_alone(case: str) -> None:
    """Playwright's and asyncio's ``wait_for`` are not the backstop helper."""
    found = _scan_source(f"<{case}>", _THIRD_PARTY_WAIT_FOR[case])

    flagged = {kind: sites for kind, sites in found.items() if sites and kind != "importers"}

    assert flagged == {}, f"false positive on a third-party wait_for ({case}): {flagged}"


@pytest.mark.parametrize("case", sorted(_TRUE_POSITIVES))
def test_guard_still_flags_the_helper_under_narrowed_matching(case: str) -> None:
    """Narrowing ``wait_for`` must not open a hole for the real helper."""
    source, kind = _TRUE_POSITIVES[case]

    found = _scan_source(f"<{case}>", source)

    assert found[kind], f"true positive not flagged as {kind} ({case}): {found}"
