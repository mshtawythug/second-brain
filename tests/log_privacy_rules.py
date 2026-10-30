"""The AST rules behind the G1 log-privacy guard (``tests/test_log_privacy.py``).

The guard flags a logger call in ``src/brain`` that can carry a caught exception's
MESSAGE, and a traceback logged outside ``TRACEBACK_ALLOWLIST``. A name is
*tainted* when it can hold that message:

- the ``except … as e`` name, inside its handler;
- a parameter annotated as an exception type (a helper that logs what it is handed);
- **(T1, RM-28)** a name assigned inside a handler from a tainted value. It stays
  tainted for the WHOLE function, not just the handler, so logging it after the
  handler is caught. The scope is flow-insensitive: inside a loop, "after the
  handler" includes the next iteration's code above it. Any further assignment
  from a tainted name in the same function is tainted too.
- **(T3, RM-28)** in a ``def`` or ``lambda``, every name tainted where it is
  defined, minus its own parameters (a closure sees its enclosing names).

**(T2, RM-28)** A store of a tainted value into an attribute (``obj.attr = e``)
is flagged too, because the attribute can be logged anywhere. A store that goes
through ``describe_failure`` or ``type`` is class-only and passes.

``describe_failure``, ``type`` and ``isinstance`` reduce an exception to
content-free text (or a bool), and a comparison yields a bool, so none of them
taints.

Still not covered: an exception put into a container (``errors.append(str(e))``)
and the container logged later, and a tainted value returned from a function.

Moved out of ``tests/test_log_privacy.py`` for the 800-line ceiling (RM-28). Plan:
``docs/plans/2026-10-30-rm-28-log-privacy-guard.md``.
"""

from __future__ import annotations

import ast
from collections.abc import Iterator

_LEVELS = {"debug", "info", "warning", "error", "critical", "exception", "log"}
#: Calls that reduce an exception to content-free text (or to a bool).
_CONTENT_FREE_CALLS = {"describe_failure", "type", "isinstance"}
_SCOPES = (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda)
_ASSIGNS = (ast.Assign, ast.AnnAssign, ast.AugAssign, ast.NamedExpr)

#: ``(module, enclosing function, message) -> reason``. These are the only
#: places a traceback may be logged. A traceback carries the exception message,
#: so each site is a deliberate trade of that risk for diagnosability.
TRACEBACK_ALLOWLIST: dict[tuple[str, str, str], str] = {
    (
        "vault/watch.py",
        "on_any_event",
        "vault watcher: classify failed for %r",
    ): "the daemon's only diagnosis of an unexpected observer-thread failure",
    (
        "vault/watch.py",
        "on_any_event",
        "vault watcher: schedule failed for %s %s",
    ): "the same, for scheduling",
    (
        "vault/watch.py",
        "_schedule_debounced",
        "vault watcher: debounce timer failed to start; enqueued directly",
    ): "a thread-start failure: OS-level, no document text",
    (
        "vault/watch.py",
        "_enqueue",
        "vault watcher: on_event hook raised",
    ): "a test-only hook; production passes None",
    (
        "vault/watch.py",
        "_worker_loop",
        "vault watcher: worker failed on %s %s",
    ): "the daemon's only diagnosis of an unexpected job failure",
    (
        "mcp_server.py",
        "_wrap_db_error",
        "database error",
    ): "server-log detail by design; the MCP wire gets the class only (DB-08)",
    ("mcp_server.py", "_wrap_embed_error", "embedding failed"): "the same design as _wrap_db_error",
    (
        "mcp_server.py",
        "_wrap_graph_backend_error",
        "graph backend error",
    ): "the same design; spec D9 keeps Cypher off the wire, not out of the server log",
    (
        "ui/app.py",
        "_unhandled_error_handler",
        "brain ui: unhandled error serving %s",
    ): "last-resort handler; the browser gets the class only",
}

#: ``(module, enclosing function, message) -> reason``. The only places a value
#: DERIVED from a caught exception may be logged raw, because the derivation is
#: content-free by construction.
RAW_ALLOWLIST: dict[tuple[str, str, str], str] = {
    (
        "gaps.py",
        "record_search_query",
        "search-query logging skipped: search_queries.%s column missing (migration %s not "
        "applied) — run `brain init` to restore full `brain gaps` signal",
    ): "`missing` is a key of _ADDITIVE_COLUMNS (_missing_additive_column returns one or None)",
}

#: ``(module, enclosing function, stored target) -> reason``. The only places a
#: value derived from a caught exception may be stored on an attribute (T2).
STORE_ALLOWLIST: dict[tuple[str, str, str], str] = {
    ("ui/jobs.py", "_fail", "job.error"): (
        "`error` is a JobError, not an exception: the parameter heuristic matches its name. "
        "It is a frozen (code, message) record whose message is fixed per code and never "
        "carries exception text (its docstring; generic_failure uses the class name only)"
    ),
}


def uses(node: ast.AST, names: set[str]) -> bool:
    """True if any of ``names`` appears in ``node``, other than inside a content-free call."""
    if isinstance(node, ast.Name):
        return node.id in names
    if isinstance(node, ast.Compare):
        return False  # a comparison yields a bool, never the exception's text
    if isinstance(node, ast.Call):
        fn = node.func
        called = fn.id if isinstance(fn, ast.Name) else getattr(fn, "attr", None)
        if called in _CONTENT_FREE_CALLS:
            return False
    return any(uses(child, names) for child in ast.iter_child_nodes(node))


def _is_log_call(node: ast.AST) -> bool:
    """``<x>.<level>(...)`` where the receiver's name says log: ``logger``, ``self._logger``..."""
    if not (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)):
        return False
    if node.func.attr not in _LEVELS:
        return False
    receiver = node.func.value
    label = receiver.id if isinstance(receiver, ast.Name) else getattr(receiver, "attr", "")
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


def _own_nodes(nodes: list[ast.stmt]) -> Iterator[ast.AST]:
    """Every node under ``nodes`` in this scope: nested defs and lambdas are skipped."""
    stack: list[ast.AST] = list(nodes)
    while stack:
        node = stack.pop()
        yield node
        stack.extend(c for c in ast.iter_child_nodes(node) if not isinstance(c, _SCOPES))


def _assign_targets(node: ast.AST) -> list[ast.expr]:
    if isinstance(node, ast.Assign):
        return node.targets
    if isinstance(node, (ast.AnnAssign, ast.AugAssign, ast.NamedExpr)):
        return [node.target]
    return []


def _propagate(assigns: list[ast.AST], tainted: set[str]) -> set[str]:
    """``tainted`` plus every name assigned (in ``assigns``) from a tainted value, transitively."""
    tainted = set(tainted)
    grew = True
    while grew:
        grew = False
        for node in assigns:
            value = getattr(node, "value", None)
            if value is None or not uses(value, tainted):
                continue
            for target in _assign_targets(node):
                for name in ast.walk(target):
                    if isinstance(name, ast.Name) and name.id not in tainted:
                        tainted.add(name.id)
                        grew = True
    return tainted


def _handler_taint(handler: ast.ExceptHandler, tainted: set[str]) -> set[str]:
    """``tainted``, the handler's name, and every name its body assigns from a tainted value."""
    seed = tainted | ({handler.name} if handler.name else set())
    return _propagate([n for n in _own_nodes(handler.body) if isinstance(n, _ASSIGNS)], seed)


def _scope_taint(body: list[ast.stmt], inherited: set[str]) -> set[str]:
    """T1: the names tainted throughout one function (or the module), whatever the statement order.

    The seed is ``inherited`` plus each handler's aliases: names assigned inside the
    handler from its exception. The handler's own name is not carried out of it,
    because Python unbinds it when the handler ends and a later reuse is unrelated.
    """
    own = list(_own_nodes(body))
    handlers = [n for n in own if isinstance(n, ast.ExceptHandler)]
    seed = set(inherited)
    for handler in handlers:
        seed |= _handler_taint(handler, inherited) - {handler.name}
    return _propagate([n for n in own if isinstance(n, _ASSIGNS)], seed)


def _param_names(args: ast.arguments) -> set[str]:
    every = [*args.posonlyargs, *args.args, *args.kwonlyargs, args.vararg, args.kwarg]
    return {a.arg for a in every if a is not None}


def _exception_params(func: ast.FunctionDef | ast.AsyncFunctionDef) -> set[str]:
    """Parameters annotated as an exception type: a helper that logs what it is handed."""
    params = [*func.args.posonlyargs, *func.args.args, *func.args.kwonlyargs]
    return {
        a.arg
        for a in params
        if a.annotation is not None
        and any(word in ast.unparse(a.annotation) for word in ("Error", "Exception"))
    }


def _stored_attributes(node: ast.AST) -> list[ast.Attribute]:
    """The attribute targets of an assignment, including inside a tuple/list unpack."""
    return [
        t
        for target in _assign_targets(node)
        for t in ast.walk(target)
        if isinstance(t, ast.Attribute) and isinstance(t.ctx, ast.Store)
    ]


def log_privacy_violations(source: str, module: str = "<src>") -> list[str]:
    """``module:line function kind`` for each site that can carry an exception's text.

    ``raw``: a logger call any of whose arguments (the message included,
    ``exc_info`` excluded) uses a tainted name, other than through
    ``describe_failure``/``type``. ``traceback``: a traceback logged outside
    ``TRACEBACK_ALLOWLIST``. ``store``: a tainted value stored on an attribute,
    outside ``STORE_ALLOWLIST``. The module docstring says what is tainted.
    """
    hits: list[str] = []

    def check(node: ast.AST, tainted: set[str], func: str) -> None:
        for attr in _stored_attributes(node):
            value = getattr(node, "value", None)
            key = (module, func, ast.unparse(attr))
            if value is not None and uses(value, tainted) and key not in STORE_ALLOWLIST:
                hits.append(f"{module}:{getattr(node, 'lineno', 0)} {func} store")
        if not _is_log_call(node):
            return
        assert isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
        args = [*node.args, *(k.value for k in node.keywords if k.arg != "exc_info")]
        first = node.args[0] if node.args else None
        message = first.value if isinstance(first, ast.Constant) else None
        key = (module, func, message)
        if tainted and any(uses(arg, tainted) for arg in args) and key not in RAW_ALLOWLIST:
            hits.append(f"{module}:{node.lineno} {func} raw")
        if _shows_traceback(node) and key not in TRACEBACK_ALLOWLIST:
            hits.append(f"{module}:{node.lineno} {func} traceback")

    def scope(body: list[ast.stmt], inherited: set[str], func: str) -> None:
        tainted = _scope_taint(body, inherited)
        for stmt in body:
            visit(stmt, tainted, func)

    def visit(node: ast.AST, tainted: set[str], func: str) -> None:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            for default in [*node.args.defaults, *node.args.kw_defaults]:
                if default is not None:
                    visit(default, tainted, func)
            closure = (tainted - _param_names(node.args)) | _exception_params(node)
            scope(node.body, closure, node.name)
            return
        if isinstance(node, ast.Lambda):
            body = ast.Expr(node.body, lineno=node.lineno, col_offset=node.col_offset)
            scope([body], tainted - _param_names(node.args), func)
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
    return hits
