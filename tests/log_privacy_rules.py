"""The AST rules behind the G1 log-privacy guard (``tests/test_log_privacy_rules.py``).

G1 and its self-tests live in ``tests/test_log_privacy_rules.py``;
``tests/test_log_privacy.py`` keeps the leak reproductions. The guard reports
three kinds of hit in ``src/brain``:

- ``raw``: a logger call any of whose arguments can carry a caught exception's
  MESSAGE, which can quote document content;
- ``traceback``: a traceback logged (``.exception``, a truthy ``exc_info``) or
  printed (``traceback.print_exc`` and kin) outside ``TRACEBACK_ALLOWLIST``;
- ``store`` (T2, RM-28): a tainted value stored where it can be logged later: an
  attribute (``obj.attr = e``), a subscript on an attribute (``self.errors[k] =
  str(e)``), ``setattr``/``object.__setattr__``, or an attribute container's
  ``append``/``extend``/``add``/``insert``/``update`` (``report.errors.append(…)``).

A value is *tainted* when it can hold an exception's message:

- the ``except … as e`` name (``except*`` too), inside its handler;
- a parameter annotated as an exception: an identifier in the annotation ends with
  ``Error`` or ``Exception``, is a builtin exception or one of
  ``EXCEPTION_CLASS_NAMES``, or the annotation names ``psycopg.errors.…``;
- a call that returns the current exception or its text: ``traceback.format_exc``,
  ``format_exception``, ``format_exception_only``, ``format_tb``,
  ``sys.exc_info``, ``sys.exception``, and an argument-less ``.exception()`` (a
  future's);
- **(T1, RM-28)** a name bound from a tainted value, by any binding form:
  assignment (plain, annotated, augmented, ``:=``), a ``for`` or comprehension
  target, ``with … as``, or a ``match`` capture. A name bound inside a handler
  stays tainted for the WHOLE function, and the scope is flow-insensitive: inside
  a loop, "after the handler" includes the code above it. The handler's own name
  is not carried out of the handler, because Python unbinds it there. Only NAME
  targets are tainted; an attribute or subscript target is reported as a store
  instead, and its base (``self``, ``job``) stays clean.
- **(T3, RM-28)** inside a ``def`` or ``lambda``, every name tainted where it is
  defined, minus its own parameters, plus any parameter whose DEFAULT is tainted.
  Its decorators, annotations and defaults are scanned with the enclosing taint.

A content-free call (``describe_failure``, ``type``, ``isinstance``, ``len``)
exempts only its FIRST positional argument; its other arguments are still scanned. A
comparison yields a bool, so it never taints.

**Still not covered** (BL-28 tracks the first two classes):

- ``raise X(f"…{e}")`` and ``X(str(e))``: an exception's text interpolated into a
  new exception's message (``sync_vault``'s ``_SyncError``, the Y5 shape). About
  240 ``{exc}``/``str(exc)`` sites in ``src/brain`` do this; whether one reaches a
  log depends on who catches it.
- ``print``, ``typer.echo`` and ``typer.secho(err=True)`` of exception text: their
  stderr is the daemons' launchd log (BL-25 lists the CLI echoes already found).
- a constructor or return value built from the exception: a mapper returning
  ``JobError(code, str(exc))`` would pass, and so would any function returning a
  tainted value to a caller that logs it;
- a local container (``errors.append(str(e))`` on a NAME) logged later;
- an exception parameter with no annotation, or one annotated with a class from
  outside ``src/brain`` whose name does not end with ``Error``/``Exception``;
- any logger whose receiver's name does not contain ``log``.

Moved out of ``tests/test_log_privacy.py`` for the 800-line ceiling (RM-28). Plan:
``docs/plans/2026-10-30-rm-28-log-privacy-guard.md``.
"""

from __future__ import annotations

import ast
import builtins
from collections.abc import Iterator

from tests.log_privacy_allowlists import RAW_ALLOWLIST, STORE_ALLOWLIST, TRACEBACK_ALLOWLIST

_LEVELS = {"debug", "info", "warning", "warn", "error", "critical", "fatal", "exception", "log"}
#: Calls that reduce their first argument to content-free text, a bool or a count.
_CONTENT_FREE_CALLS = {"describe_failure", "type", "isinstance", "len"}
#: ``(module, function)`` calls that return the current exception or its text.
_EXCEPTION_SOURCES = {
    ("traceback", "format_exc"),
    ("traceback", "format_exception"),
    ("traceback", "format_exception_only"),
    ("traceback", "format_tb"),
    ("sys", "exc_info"),
    ("sys", "exception"),
}
_TRACEBACK_PRINTS = {"print_exc", "print_exception", "print_tb", "print_last"}
_CONTAINER_METHODS = {"append", "extend", "add", "insert", "update"}
_SCOPES = (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda)
_BUILTIN_EXCEPTIONS = frozenset(
    name
    for name, value in vars(builtins).items()
    if isinstance(value, type) and issubclass(value, BaseException)
)

#: ``src/brain``'s exception classes whose names do not END with ``Error`` or
#: ``Exception``, so the annotation-name rule cannot see them. G1's self-tests re-derive this
#: set from ``src/brain`` and fail when a new one is missing.
EXCEPTION_CLASS_NAMES = frozenset(
    {
        "AgentIdInvalid", "AskTimedOut", "ChatOutputTruncated", "DocumentStillExists",
        "DraftSkipped", "GraphModeUnavailable", "HostRefused", "IdPrefixAmbiguous",
        "IdPrefixNotFound", "IdPrefixNotHex", "IdPrefixTooShort", "IngestAmbiguousSource",
        "InvalidSourceKind", "JobCancelled", "JobUnknown", "MigrationLockTimeout",
        "NotCancellable", "OllamaContextOverflow", "OllamaEmbedContextOverflow",
        "OllamaUnavailable", "PayloadCeilingExceeded", "PersonAmbiguous", "PersonNotFound",
        "PgToolUnavailable", "QueueFull", "RebuildLockHeld", "ReconnectAbandoned",
        "RestoreAborted", "RestoreIncompatible", "SavedSearchExists", "SavedSearchNotFound",
        "ShuttingDown", "StageFailed", "UiBadRequest", "UiConflict", "UiForbidden",
        "UiNotFound", "UiPayloadTooLarge", "UiTooManyRequests", "UiUnavailable",
        "UiUnsupportedMedia", "UnreadableFile", "VaultPathEscape", "_PortBusy", "_Rollback",
        "_Skip",
    }
)  # fmt: skip


def _called_name(call: ast.Call) -> str | None:
    fn = call.func
    if isinstance(fn, ast.Name):
        return fn.id
    return fn.attr if isinstance(fn, ast.Attribute) else None


def _dotted(call: ast.Call) -> tuple[str, str] | None:
    """``(receiver, method)`` for a ``name.method(...)`` call."""
    fn = call.func
    if isinstance(fn, ast.Attribute) and isinstance(fn.value, ast.Name):
        return fn.value.id, fn.attr
    return None


def _is_exception_source(call: ast.Call) -> bool:
    if _dotted(call) in _EXCEPTION_SOURCES:
        return True
    if isinstance(call.func, ast.Name) and call.func.id.startswith("format_exc"):
        return True  # ``from traceback import format_exc``
    bare = not call.args and not call.keywords
    return bare and isinstance(call.func, ast.Attribute) and call.func.attr == "exception"


def uses(node: ast.AST, names: set[str]) -> bool:
    """True if ``node`` can carry a tainted value: one of ``names``, or an exception source."""
    if isinstance(node, ast.Name):
        return node.id in names
    if isinstance(node, ast.Compare):
        return False  # a comparison yields a bool, never the exception's text
    if isinstance(node, ast.Call):
        if _is_exception_source(node):
            return True
        if _called_name(node) in _CONTENT_FREE_CALLS:
            rest = [node.func, *node.args[1:], *(k.value for k in node.keywords)]
            return any(uses(child, names) for child in rest)
    return any(uses(child, names) for child in ast.iter_child_nodes(node))


def _is_log_call(node: ast.AST) -> bool:
    """``<x>.<level>(...)`` where the receiver's name says log: ``logger``, ``getLogger(...)``."""
    if not (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)):
        return False
    if node.func.attr not in _LEVELS:
        return False
    receiver = node.func.value
    if isinstance(receiver, ast.Name):
        label = receiver.id
    elif isinstance(receiver, ast.Call):
        label = _called_name(receiver) or ""
    else:
        label = getattr(receiver, "attr", "")
    return "log" in label.lower()


def _shows_traceback(call: ast.Call) -> bool:
    assert isinstance(call.func, ast.Attribute)
    if call.func.attr == "exception":
        return True
    for kw in call.keywords:
        if kw.arg == "exc_info":
            falsy = isinstance(kw.value, ast.Constant) and not kw.value.value
            if not falsy:
                return True
    return False


def _is_traceback_print(call: ast.Call) -> bool:
    dotted = _dotted(call)
    if dotted is not None:
        return dotted[0] == "traceback" and dotted[1] in _TRACEBACK_PRINTS
    return isinstance(call.func, ast.Name) and call.func.id in _TRACEBACK_PRINTS


def _own_nodes(nodes: list[ast.stmt]) -> Iterator[ast.AST]:
    """Every node under ``nodes`` in this scope: nested defs and lambdas are skipped (S8)."""
    stack: list[ast.AST] = [n for n in nodes if not isinstance(n, _SCOPES)]
    while stack:
        node = stack.pop()
        yield node
        stack.extend(c for c in ast.iter_child_nodes(node) if not isinstance(c, _SCOPES))


def _bindings(node: ast.AST) -> list[tuple[ast.AST, list[ast.AST]]]:
    """``(value, targets)`` for every form that binds a name from a value (T1)."""
    if isinstance(node, ast.Assign):
        return [(node.value, list(node.targets))]
    if isinstance(node, (ast.AnnAssign, ast.AugAssign, ast.NamedExpr)) and node.value:
        return [(node.value, [node.target])]
    if isinstance(node, (ast.For, ast.AsyncFor, ast.comprehension)):
        return [(node.iter, [node.target])]
    if isinstance(node, ast.withitem) and node.optional_vars is not None:
        return [(node.context_expr, [node.optional_vars])]
    if isinstance(node, ast.Match):
        return [(node.subject, [case.pattern for case in node.cases])]
    return []


def _bound_names(target: ast.AST) -> set[str]:
    """The NAMES a target binds. An attribute or subscript binds none (A5): T2 reports it."""
    if isinstance(target, ast.Name):
        return {target.id}
    if isinstance(target, ast.Starred):
        return _bound_names(target.value)
    if isinstance(target, (ast.Tuple, ast.List)):
        return {name for elt in target.elts for name in _bound_names(elt)}
    if isinstance(target, ast.pattern):
        names = set()
        for node in ast.walk(target):
            if isinstance(node, (ast.MatchAs, ast.MatchStar)) and node.name:
                names.add(node.name)
            if isinstance(node, ast.MatchMapping) and node.rest:
                names.add(node.rest)
        return names
    return set()


def _propagate(nodes: list[ast.AST], tainted: set[str]) -> set[str]:
    """``tainted`` plus every name bound (in ``nodes``) from a tainted value, transitively."""
    pairs = [pair for node in nodes for pair in _bindings(node)]
    tainted = set(tainted)
    grew = True
    while grew:
        grew = False
        for value, targets in pairs:
            if not uses(value, tainted):
                continue
            new = {n for target in targets for n in _bound_names(target)} - tainted
            if new:
                tainted |= new
                grew = True
    return tainted


def _handler_taint(handler: ast.ExceptHandler, tainted: set[str]) -> set[str]:
    """``tainted``, the handler's name, and every name its body binds from a tainted value."""
    seed = tainted | ({handler.name} if handler.name else set())
    return _propagate(list(_own_nodes(handler.body)), seed)


def _scope_taint(body: list[ast.stmt], inherited: set[str]) -> set[str]:
    """T1: the names tainted throughout one function (or the module), whatever the order."""
    own = list(_own_nodes(body))
    seed = set(inherited)
    for handler in (n for n in own if isinstance(n, ast.ExceptHandler)):
        seed |= _handler_taint(handler, inherited) - {handler.name}
    return _propagate(own, seed)


def _params(args: ast.arguments) -> list[ast.arg]:
    every = [*args.posonlyargs, *args.args, *args.kwonlyargs, args.vararg, args.kwarg]
    return [a for a in every if a is not None]


def _defaults(args: ast.arguments) -> list[tuple[ast.arg, ast.expr]]:
    positional = [*args.posonlyargs, *args.args]
    pairs = list(
        zip(positional[len(positional) - len(args.defaults) :], args.defaults, strict=True)
    )
    return pairs + [
        (a, d) for a, d in zip(args.kwonlyargs, args.kw_defaults, strict=True) if d is not None
    ]


def _closure(args: ast.arguments, tainted: set[str]) -> set[str]:
    """T3: the enclosing taint, minus shadowing parameters, plus tainted-default ones (S1)."""
    shadowed = {a.arg for a in _params(args)}
    defaulted = {a.arg for a, default in _defaults(args) if uses(default, tainted)}
    return (tainted - shadowed) | defaulted


def _names_an_exception(annotation: ast.expr) -> bool:
    """An identifier in ``annotation`` ENDS with ``Error``/``Exception``, or is a known one.

    "Ends with", not "contains": ``ErrorMapper`` (``ui/jobs.py``) is a callable
    alias. No exception class in ``src/brain`` escapes the rule and the set
    together (G1's self-tests re-derive this).
    """
    if "psycopg.errors." in ast.unparse(annotation):
        return True
    words = {n.id for n in ast.walk(annotation) if isinstance(n, ast.Name)}
    words |= {n.attr for n in ast.walk(annotation) if isinstance(n, ast.Attribute)}
    known = EXCEPTION_CLASS_NAMES | _BUILTIN_EXCEPTIONS
    return any(w.endswith(("Error", "Exception")) or w in known for w in words)


def _exception_params(func: ast.FunctionDef | ast.AsyncFunctionDef) -> set[str]:
    """Parameters annotated as an exception: a helper that logs what it is handed."""
    return {
        a.arg
        for a in _params(func.args)
        if a.annotation is not None and _names_an_exception(a.annotation)
    }


def _attribute_based(target: ast.AST) -> bool:
    """An attribute, or a subscript whose base is one (``self.errors[k]``)."""
    if isinstance(target, (ast.Tuple, ast.List)):
        return any(_attribute_based(elt) for elt in target.elts)
    if isinstance(target, ast.Starred):
        return _attribute_based(target.value)
    while isinstance(target, ast.Subscript):
        target = target.value
    return isinstance(target, ast.Attribute)


def _stored_values(node: ast.AST) -> list[ast.AST]:
    """T2: the values ``node`` stores where a later log line can reach them."""
    if isinstance(node, (ast.Assign, ast.AnnAssign, ast.AugAssign)) and node.value:
        targets = node.targets if isinstance(node, ast.Assign) else [node.target]
        return [node.value] if any(_attribute_based(t) for t in targets) else []
    if not isinstance(node, ast.Call):
        return []
    name = _called_name(node)
    if name == "setattr" or (name == "__setattr__" and _dotted(node) == ("object", "__setattr__")):
        return node.args[2:3]
    fn = node.func
    attribute_receiver = isinstance(fn, ast.Attribute) and isinstance(fn.value, ast.Attribute)
    if name in _CONTAINER_METHODS and attribute_receiver:
        return [*node.args, *(k.value for k in node.keywords)]
    return []


def scan(source: str, module: str = "<src>") -> tuple[list[str], set[tuple[str, tuple[str, ...]]]]:
    """``(hits, allowlist keys used)`` for one module; see the module docstring for the rules.

    A hit is ``module:line function kind``. A logger argument is ``raw`` when it
    uses a tainted value other than through a content-free call or a comparison.
    Each used key is ``(allowlist, key)``, for G1's stale-entry check (S7).
    """
    hits: list[str] = []
    used: set[tuple[str, tuple[str, ...]]] = set()

    def allowed(kind: str, table: dict[tuple[str, str, str], str], key: tuple) -> bool:
        if key in table:
            used.add((kind, key))
            return True
        return False

    def check(node: ast.AST, tainted: set[str], func: str) -> None:
        line = getattr(node, "lineno", 0)
        stores = any(uses(v, tainted) for v in _stored_values(node))
        if stores and not allowed("store", STORE_ALLOWLIST, (module, func, ast.unparse(node))):
            hits.append(f"{module}:{line} {func} store")
        prints = isinstance(node, ast.Call) and _is_traceback_print(node)
        if prints and not allowed(
            "traceback", TRACEBACK_ALLOWLIST, (module, func, ast.unparse(node))
        ):
            hits.append(f"{module}:{line} {func} traceback")
        if not _is_log_call(node):
            return
        assert isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
        args = [*node.args, *(k.value for k in node.keywords if k.arg != "exc_info")]
        first = node.args[0] if node.args else None
        key = (module, func, first.value if isinstance(first, ast.Constant) else None)
        if any(uses(arg, tainted) for arg in args) and not allowed("raw", RAW_ALLOWLIST, key):
            hits.append(f"{module}:{line} {func} raw")
        if _shows_traceback(node) and not allowed("traceback", TRACEBACK_ALLOWLIST, key):
            hits.append(f"{module}:{line} {func} traceback")

    def scope(body: list[ast.stmt], inherited: set[str], func: str) -> None:
        tainted = _scope_taint(body, inherited)
        for stmt in body:
            visit(stmt, tainted, func)

    def visit(node: ast.AST, tainted: set[str], func: str) -> None:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            annotations = [a.annotation for a in _params(node.args)]
            outer = [*node.decorator_list, *annotations, node.returns]  # R2
            outer += [d for _, d in _defaults(node.args)]
            for child in outer:
                if child is not None:
                    visit(child, tainted, func)
            closure = _closure(node.args, tainted) | _exception_params(node)
            scope(node.body, closure, node.name)
            return
        if isinstance(node, ast.Lambda):
            for _, default in _defaults(node.args):
                visit(default, tainted, func)
            body = ast.Expr(node.body, lineno=node.lineno, col_offset=node.col_offset)
            scope([body], _closure(node.args, tainted), func)
            return
        if isinstance(node, ast.ExceptHandler):
            inner = _handler_taint(node, tainted)
            for child in node.body:
                visit(child, inner, func)
            return
        check(node, tainted, func)
        for child in ast.iter_child_nodes(node):
            visit(child, tainted, func)

    scope(ast.parse(source, filename=module).body, set(), "<module>")
    return hits, used


def stale_entries(used: set[tuple[str, tuple[str, ...]]]) -> list[tuple[str, tuple]]:
    """Allowlist entries no site in ``used`` matched (S7): G1 fails on any."""
    tables = {"raw": RAW_ALLOWLIST, "traceback": TRACEBACK_ALLOWLIST, "store": STORE_ALLOWLIST}
    return [
        (kind, key) for kind, table in tables.items() for key in table if (kind, key) not in used
    ]


def log_privacy_violations(source: str, module: str = "<src>") -> list[str]:
    """The hits of :func:`scan` alone."""
    return scan(source, module)[0]


def exception_class_names(sources: list[str]) -> set[str]:
    """Every class in ``sources`` that derives (transitively) from an exception.

    A base counts when its last dotted part is a builtin exception, names
    ``Error``/``Exception``, or is another class this finds.
    """
    classes = [
        (node.name, [ast.unparse(b).rsplit(".", 1)[-1] for b in node.bases])
        for source in sources
        for node in ast.walk(ast.parse(source))
        if isinstance(node, ast.ClassDef)
    ]
    found: set[str] = set()
    grew = True
    while grew:
        grew = False
        for name, bases in classes:
            if name in found:
                continue
            if any(
                b in _BUILTIN_EXCEPTIONS | found or "Error" in b or "Exception" in b for b in bases
            ):
                found.add(name)
                grew = True
    return found
