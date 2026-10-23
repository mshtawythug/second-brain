"""No ``src/brain`` log line carries an exception's MESSAGE (G1-G3, Y1-Y4, H1-H2).

Plan: ``docs/plans/2026-10-22-watcher-repair-hint-and-yaml-log.md``. An exception's
text can quote what it failed on:

- a PyYAML ``MarkedYAMLError`` built from a STRING quotes the offending line;
- a psycopg DETAIL quotes row values;
- an ``EnrichmentError`` can embed the model's reply.

So every logger call in ``src/brain`` names a caught exception through
``brain.errors.describe_failure``. That gives the class plus content-free detail:
an errno symbol, a YAML line:col, or a raise-site ``log_detail``.

- **G1** is the guard: an AST scan of ``src/brain``. A traceback (``.exception`` or
  a truthy ``exc_info``) is allowed only at the sites in ``_TRACEBACK_ALLOWLIST``,
  each one with its reason.
- **G2/G3** prove the scan can fail, and what it lets through.
- **Y1-Y4** reproduce the YAML leak at the sites that parse a vault file.
- **H1-H2** check the gws refresh WARNINGs.

All data is synthetic.
"""

from __future__ import annotations

import ast
import logging
import stat
import uuid
from pathlib import Path
from typing import Any

import psycopg
import pytest

from brain.vault.frontmatter import dump_frontmatter

_SRC = Path(__file__).resolve().parent.parent / "src" / "brain"
_LEVELS = {"debug", "info", "warning", "error", "critical", "exception", "log"}
#: Calls that reduce an exception to content-free text (or to a bool).
_CONTENT_FREE_CALLS = {"describe_failure", "type", "isinstance"}
#: A line of YAML that does not parse, and that PyYAML's error text quotes back.
_SECRET = "synthetic-secret-value-42"
_MALFORMED = f"---\ntitle: a: {_SECRET}\n---\nbody\n"

#: ``(module, enclosing function, message) -> reason``. These are the only
#: places a traceback may be logged. A traceback carries the exception message,
#: so each site is a deliberate trade of that risk for diagnosability.
_TRACEBACK_ALLOWLIST: dict[tuple[str, str, str], str] = {
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
_RAW_ALLOWLIST: dict[tuple[str, str, str], str] = {
    (
        "gaps.py",
        "record_search_query",
        "search-query logging skipped: search_queries.%s column missing (migration %s not "
        "applied) — run `brain init` to restore full `brain gaps` signal",
    ): "`missing` is a key of _ADDITIVE_COLUMNS (_missing_additive_column returns one or None)",
}


def _uses(node: ast.AST, names: set[str]) -> bool:
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
    return any(_uses(child, names) for child in ast.iter_child_nodes(node))


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


def _tainted_names(handler: ast.ExceptHandler) -> set[str]:
    """The handler's name plus every name assigned (in its body) from a tainted value."""
    tainted = {handler.name} if handler.name else set()
    assigns = [
        n
        for stmt in handler.body
        for n in ast.walk(stmt)
        if isinstance(n, (ast.Assign, ast.AnnAssign, ast.AugAssign)) and n.value is not None
    ]
    grew = True
    while grew:
        grew = False
        for node in assigns:
            if node.value is not None and _uses(node.value, tainted):
                targets = node.targets if isinstance(node, ast.Assign) else [node.target]
                for target in targets:
                    for name in ast.walk(target):
                        if isinstance(name, ast.Name) and name.id not in tainted:
                            tainted.add(name.id)
                            grew = True
    return tainted


def _exception_params(func: ast.FunctionDef | ast.AsyncFunctionDef) -> set[str]:
    """Parameters annotated as an exception type: a helper that logs what it is handed."""
    params = [*func.args.posonlyargs, *func.args.args, *func.args.kwonlyargs]
    return {
        a.arg
        for a in params
        if a.annotation is not None
        and any(word in ast.unparse(a.annotation) for word in ("Error", "Exception"))
    }


def log_privacy_violations(source: str, module: str = "<src>") -> list[str]:
    """``module:line function kind`` for each logger call that can carry an exception's text.

    A call is a violation if any argument (the message included; ``exc_info``
    excluded) uses a caught exception, a name assigned from one inside its
    handler, or a parameter annotated as an exception type, other than through
    ``describe_failure``/``type``; or if it logs a traceback outside
    ``_TRACEBACK_ALLOWLIST``.
    """
    hits: list[str] = []

    def visit(node: ast.AST, tainted: set[str], func: str) -> None:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            for child in node.body:
                visit(child, _exception_params(node), node.name)
            return
        if isinstance(node, ast.ExceptHandler):
            inner = tainted | _tainted_names(node)
            for child in node.body:
                visit(child, inner, func)
            return
        if _is_log_call(node):
            assert isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
            args = [*node.args, *(k.value for k in node.keywords if k.arg != "exc_info")]
            first = node.args[0] if node.args else None
            message = first.value if isinstance(first, ast.Constant) else None
            key = (module, func, message)
            if tainted and any(_uses(arg, tainted) for arg in args) and key not in _RAW_ALLOWLIST:
                hits.append(f"{module}:{node.lineno} {func} raw")
            if _shows_traceback(node) and key not in _TRACEBACK_ALLOWLIST:
                hits.append(f"{module}:{node.lineno} {func} traceback")
        for child in ast.iter_child_nodes(node):
            visit(child, tainted, func)

    visit(ast.parse(source, filename=module), set(), "<module>")
    return hits


def test_no_src_brain_logger_call_carries_an_exception_message() -> None:
    """G1: the guard over every module in ``src/brain``."""
    hits = [
        hit
        for path in sorted(_SRC.rglob("*.py"))
        for hit in log_privacy_violations(path.read_text(), path.relative_to(_SRC).as_posix())
    ]

    assert hits == [], f"logger calls that can carry an exception's text: {hits}"


def _handler(body: str) -> str:
    """``body`` (one statement per line) inside ``def f(): try/except OSError as e:``."""
    lines = "".join(f"        {line}\n" for line in body.splitlines())
    return f"def f():\n    try:\n        pass\n    except OSError as e:\n{lines}"


@pytest.mark.parametrize(
    "body",
    [
        "logger.warning('x %s', e)",
        "_logger.error('x %s', str(e))",
        "logger.warning(f'x {e}')",
        "logger.warning('x %s' % e)",
        "logger.warning('x {}'.format(e))",
        "m = f'x {e}'\nlogger.warning('%s', m)",
        "self._logger.warning('x %s', e)",
        "logger.exception('not on the allowlist')",
        "logger.error('x', exc_info=True)",
        "logger.log(30, 'x %s', e)",
    ],
    ids=[
        "bare",
        "nested",
        "fstring",
        "percent",
        "format",
        "alias",
        "attr-receiver",
        "exception",
        "exc-info",
        "log-level",
    ],
)
def test_the_guard_flags_every_form_that_carries_the_message(body: str) -> None:
    """G2: the scan can fail, on each way an exception's text reaches a log line."""
    assert log_privacy_violations(_handler(body)) != [], f"the guard missed: {body!r}"


def test_the_guard_flags_a_helper_that_logs_the_exception_it_is_handed() -> None:
    """G2: no handler binds ``exc`` here; its annotation says what it is."""
    source = "def _warn(exc: OSError) -> None:\n    logger.warning('x %s', exc)\n"

    assert log_privacy_violations(source) != [], "the guard missed an exception parameter"


@pytest.mark.parametrize(
    "body",
    [
        "logger.warning('x %s', describe_failure(e))",
        "logger.warning('x %s', errors.describe_failure(e))",
        "logger.warning('x %s', type(e).__name__)",
        "logger.warning(f'x {describe_failure(e)}')",
        "logger.error('x', exc_info=False)",
        "logger.warning('x %s', describe_failure(e) if e is not None else 'none')",
    ],
    ids=[
        "describe",
        "describe-attr",
        "type-name",
        "fstring-describe",
        "exc-info-false",
        "conditional",
    ],
)
def test_the_guard_accepts_content_free_forms(body: str) -> None:
    """G3: what the guard lets through."""
    assert log_privacy_violations(_handler(body)) == []


def test_the_guard_ignores_names_outside_a_handler_and_honours_the_allowlist() -> None:
    """G3: an unbound ``e`` is not an exception, and an allowlisted traceback passes."""
    source = (
        "e = 1\nlogger.warning('x %s', e)\n"
        "def _worker_loop():\n"
        "    logger.exception('vault watcher: worker failed on %s %s', 1, 2)\n"
    )

    assert log_privacy_violations(source, "vault/watch.py") == []
    assert log_privacy_violations(source, "elsewhere.py") != [], (
        "the allowlist is not keyed by module"
    )


def test_the_raw_allowlist_is_keyed_by_module_function_and_message() -> None:
    """G3: an allowlisted derived value passes only at its own site."""
    ((module, func, message),) = _RAW_ALLOWLIST
    source = (
        f"def {func}():\n    try:\n        pass\n    except OSError as exc:\n"
        f"        missing = f(exc)\n        logger.warning({message!r}, missing, 1)\n"
    )

    assert log_privacy_violations(source, module) == []
    assert log_privacy_violations(source, "elsewhere.py") != [], (
        "the raw allowlist is not keyed by module"
    )


def _logged(caplog: pytest.LogCaptureFixture) -> str:
    return " ".join(r.getMessage() for r in caplog.records)


def test_a_malformed_people_yml_is_logged_by_class_only(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """Y1: ``load_people_yml`` (people names and emails live in that file)."""
    from brain.vault.derived_links.directory import load_people_yml

    (tmp_path / "_people.yml").write_text(f"name: a: {_SECRET}\n")
    with caplog.at_level(logging.WARNING, logger="brain.vault.derived_links.directory"):
        assert load_people_yml(tmp_path) == {}

    logged = _logged(caplog)
    assert "ScannerError" in logged and _SECRET not in logged, (
        f"a malformed _people.yml was not logged by class only: {logged!r}"
    )


def test_a_malformed_frontmatter_is_logged_by_class_only_in_link_rewrite(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """Y2: ``rewrite_vault_links``. It skips before any DB use, so no connection is needed."""
    from brain.vault.link_rewrite import rewrite_vault_links

    note = tmp_path / "note.md"
    note.write_text(_MALFORMED)
    with caplog.at_level(logging.WARNING, logger="brain.vault.link_rewrite"):
        changed = rewrite_vault_links(
            note,
            document_id=str(uuid.uuid4()),
            conn=None,  # type: ignore[arg-type]
        )

    logged = _logged(caplog)
    assert changed is False, "fixture: a malformed file must be skipped"
    assert "ScannerError" in logged and _SECRET not in logged, (
        f"malformed frontmatter was not logged by class only: {logged!r}"
    )


def test_a_malformed_frontmatter_is_logged_by_class_only_in_the_fence_rewrite(
    test_db: psycopg.Connection[Any],
    fake_embedder: Any,
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Y3: ``rewrite_derived_fences``, on a synced note whose file was then broken."""
    from brain.vault.derived_links.fence import rewrite_derived_fences
    from brain.vault.sync import sync_vault

    vault = tmp_path / "vault"
    vault.mkdir()
    doc_id = str(uuid.uuid4())
    note = vault / "note.md"
    note.write_text(dump_frontmatter({"id": doc_id, "title": "Note"}, "body\n"))
    sync_vault(test_db, embedder=fake_embedder, vault_path=vault)
    # The fence rewrite only visits ingested mirrors; mark the synced row as one.
    test_db.execute("UPDATE documents SET kind = 'ingested' WHERE id = %s", (doc_id,))
    note.write_text(_MALFORMED)

    with caplog.at_level(logging.WARNING, logger="brain.vault.derived_links.fence"):
        written = rewrite_derived_fences(test_db, {doc_id}, vault_path=vault)

    logged = _logged(caplog)
    assert written == 0, "fixture: a malformed file must be skipped"
    assert "ScannerError" in logged and _SECRET not in logged, (
        f"malformed frontmatter was not logged by class only: {logged!r}"
    )


def test_a_malformed_frontmatter_is_reported_by_class_only_in_sync_summaries(
    test_db: psycopg.Connection[Any],
    fake_embedder: Any,
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Y4: ``sync_summaries`` built its WARNING (and ``report.errors``) from ``f"...({exc})"``.

    PR #58 audit F1: the alias form, which the first G1 missed. ``report.errors`` is
    printed by ``brain vault sync-summaries`` and can land in a daemon's log, so it
    is class-only too (ruling in the plan); it keeps the path and the line:col.
    """
    from brain.vault.sync import sync_vault
    from brain.vault.sync_summaries import sync_summaries

    vault = tmp_path / "vault"
    vault.mkdir()
    doc_id = str(uuid.uuid4())
    note = vault / "note.md"
    note.write_text(dump_frontmatter({"id": doc_id, "title": "Note"}, "body\n"))
    sync_vault(test_db, embedder=fake_embedder, vault_path=vault)
    test_db.execute("UPDATE documents SET summary = 'A summary.' WHERE id = %s", (doc_id,))
    note.write_text(_MALFORMED)

    with caplog.at_level(logging.WARNING, logger="brain.vault.sync_summaries"):
        report = sync_summaries(test_db, vault_root=vault)

    logged = _logged(caplog)
    assert report.errored == 1, "fixture: the malformed file must be counted as errored"
    assert "ScannerError" in logged and _SECRET not in logged, (
        f"sync-summaries did not log by class only: {logged!r}"
    )
    assert all(_SECRET not in e for e in report.errors), (
        f"sync-summaries' report.errors quotes the file: {report.errors!r}"
    )


def _gws_path(tmp_path: Path, script: str | None) -> str:
    """A PATH holding only a fake ``gws`` (or nothing), for ``real_gws_runner``."""
    bindir = tmp_path / "bin"
    bindir.mkdir()
    if script is not None:
        gws = bindir / "gws"
        gws.write_text(script)
        gws.chmod(gws.stat().st_mode | stat.S_IXUSR)
    return str(bindir)


def test_a_missing_gws_is_logged_with_its_cause(
    test_db: psycopg.Connection[Any],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """H1: class-only must not lose the operator's diagnosis: name the missing CLI."""
    from brain.vault.derived_links.directory import refresh_contacts
    from brain.vault.derived_links.gws import real_gws_runner

    monkeypatch.setenv("PATH", _gws_path(tmp_path, None))
    with caplog.at_level(logging.WARNING, logger="brain.vault.derived_links.directory"):
        assert refresh_contacts(test_db, runner=real_gws_runner) == 0

    logged = _logged(caplog)
    assert "gws CLI not found on PATH" in logged, f"the missing gws was not named: {logged!r}"


def test_a_failing_gws_is_logged_with_its_exit_code_not_its_stderr(
    test_db: psycopg.Connection[Any],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """H2: the exit code is content-free; gws's stderr is not (it can echo account data)."""
    from brain.vault.derived_links.directory import refresh_contacts
    from brain.vault.derived_links.gws import real_gws_runner

    script = f"#!/bin/sh\necho '{_SECRET}' >&2\nexit 2\n"
    monkeypatch.setenv("PATH", _gws_path(tmp_path, script))
    with caplog.at_level(logging.WARNING, logger="brain.vault.derived_links.directory"):
        assert refresh_contacts(test_db, runner=real_gws_runner) == 0

    logged = _logged(caplog)
    assert "exit 2" in logged and _SECRET not in logged, (
        f"a failing gws was not logged by exit code alone: {logged!r}"
    )
