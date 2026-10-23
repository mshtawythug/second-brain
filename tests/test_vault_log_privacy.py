"""No vault log line carries an exception's MESSAGE (G1-G3, Y1-Y3).

Plan: ``docs/plans/2026-10-22-watcher-repair-hint-and-yaml-log.md``. An
exception's text can quote what it failed on. A PyYAML ``MarkedYAMLError`` quotes
the offending line of the file, and a psycopg DETAIL quotes row values. So every
``src/brain/vault`` logger call names a caught exception by class only, through
``brain.errors.describe_failure``.

- **G1** is the guard: an AST scan of ``src/brain/vault`` for a logger call that
  passes an ``except ... as NAME`` binding raw.
- **G2/G3** prove the scan can fail.
- **Y1-Y3** reproduce the YAML leak at the three sites that parse a vault file.

All data is synthetic.
"""

from __future__ import annotations

import ast
import logging
import uuid
from pathlib import Path
from typing import Any

import psycopg
import pytest

from brain.vault.frontmatter import dump_frontmatter

_VAULT_SRC = Path(__file__).resolve().parent.parent / "src" / "brain" / "vault"
_LEVELS = {"debug", "info", "warning", "error", "critical", "exception"}
#: A line of YAML that does not parse, and that PyYAML's error text quotes back.
_SECRET = "synthetic-secret-value-42"
_MALFORMED = f"---\ntitle: a: {_SECRET}\n---\nbody\n"


def _passes_raw(node: ast.AST, name: str) -> bool:
    """True if ``name`` appears in ``node`` other than inside ``describe_failure(...)``."""
    if isinstance(node, ast.Name):
        return node.id == name
    if isinstance(node, ast.Call):
        fn = node.func
        called = fn.id if isinstance(fn, ast.Name) else getattr(fn, "attr", None)
        if called == "describe_failure":
            return False
    return any(_passes_raw(child, name) for child in ast.iter_child_nodes(node))


def raw_exception_logs(source: str, filename: str = "<src>") -> list[str]:
    """``file:line`` of each logger call that passes a caught exception raw."""
    hits: list[str] = []

    def visit(node: ast.AST, bound: tuple[str, ...]) -> None:
        if isinstance(node, ast.ExceptHandler):
            names = bound + ((node.name,) if node.name else ())
            for child in node.body:
                visit(child, names)
            return
        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr in _LEVELS
            and isinstance(node.func.value, ast.Name)
            and "log" in node.func.value.id.lower()
        ):
            args = [*node.args[1:], *(k.value for k in node.keywords if k.arg != "exc_info")]
            if any(_passes_raw(arg, name) for name in bound for arg in args):
                hits.append(f"{filename}:{node.lineno}")
        for child in ast.iter_child_nodes(node):
            visit(child, bound)

    visit(ast.parse(source, filename=filename), ())
    return hits


def test_no_vault_logger_call_passes_an_exception_raw() -> None:
    """G1: the guard over every module in ``src/brain/vault``."""
    hits = [
        hit
        for path in sorted(_VAULT_SRC.rglob("*.py"))
        for hit in raw_exception_logs(path.read_text(), str(path.relative_to(_VAULT_SRC)))
    ]

    assert hits == [], f"vault logger calls that pass an exception raw: {hits}"


def test_the_guard_flags_a_raw_exception_argument() -> None:
    """G2: the scan can fail, on a bare name and on one nested in an expression."""
    source = (
        "try:\n    pass\nexcept OSError as e:\n"
        "    logger.warning('x %s', e)\n"
        "    _logger.error('y %s', str(e))\n"
    )

    assert raw_exception_logs(source) == ["<src>:4", "<src>:5"]


def test_the_guard_accepts_describe_failure_and_ignores_unbound_names() -> None:
    """G3: ``describe_failure(e)`` is the fix, and a name outside a handler is not one."""
    source = (
        "e = 1\nlogger.warning('x %s', e)\n"
        "try:\n    pass\nexcept OSError as exc:\n"
        "    logger.warning('y %s', describe_failure(exc))\n"
        "    logger.warning('z %s', errors.describe_failure(exc))\n"
    )

    assert raw_exception_logs(source) == []


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
