"""A vault mirror must never be written from inside a database transaction.

**The subject is mirror WRITES ordered against the transaction** — not any one
function. The first version of this module was named for the mechanism it
happened to check (``update_document`` calls) rather than the property it was
protecting, and that framing hid a second way to commit the identical defect:
calling ``write_vault_mirror`` directly inside a transaction. The guard was
green over it. Naming the property rather than the mechanism is what closes that
class, so the two policed spellings are listed together in
:data:`_MIRROR_WRITERS` and any third one belongs there too.

The invariant, and why prose was not enough to hold it:

``update_document`` writes the vault mirror to DISK after its own transaction
closes. That is correct while it owns the outermost transaction. A caller that
wraps it in a ``conn.transaction()`` of its own turns that inner block into a
SAVEPOINT which commits nothing, so the mirror lands while the database work is
still uncommitted — and a rollback **cannot unwrite a file**. The row keeps
``vault_path = NULL``, so the database has no record the file exists and no
cleanup path can ever reclaim it: not ``vault sync``, not ``vault export``, not
a later successful edit, which derives a different filename from a different
title. The orphan sits in ``_ingested/``, which is what Quartz publishes.

Such a caller must pass ``vault_root=None`` and call
``brain.ingest.write_vault_mirror`` itself after its outer transaction commits.

This guard exists because that contract was violated the first time a caller
opened a transaction — the D3 atomicity fix — and **every test passed**. Both
tests covering that edit asserted the database row and neither looked at the
directory, so nothing failed until someone wrote a disk-aware probe. The
contract was documented in ``write_vault_mirror``'s docstring at the time. A
docstring is not a control: three separate comments in this codebase were found
stating reasons narrower or wider than the truth in a single day. Prose that a
future author must read and obey is exactly the enforcement that already failed.

**Two ways to write a mirror, both policed:**

* ``update_document(...)`` inside a transaction must pass ``vault_root=None``,
  which suppresses the write it would otherwise do on the way out.
* ``write_vault_mirror(...)`` inside a transaction is a violation outright —
  there is no argument that makes it safe; the call IS the write.

**What this guard is and is not.** It is a syntactic check on the call graph: it
finds those calls lexically inside a ``with conn.transaction()``
block in the same function. It therefore cannot
see a transaction opened in a CALLER of the function making the call, nor one
entered dynamically (an ``ExitStack``, a decorator, a context manager stored in
a variable). Those remain uncovered, and a violation through one of them would
be as silent as the original bug. The check is worth having anyway because the
straightforward spelling — the one a future author will actually write, and the
one that produced the original defect — is exactly the shape it catches.
"""
from __future__ import annotations

import ast
from pathlib import Path

import pytest

pytestmark = pytest.mark.nodb

SRC = Path(__file__).resolve().parents[1] / "src" / "brain"


#: ``with`` and ``async with`` are DIFFERENT ast node types, and every route
#: handler in ``brain.ui`` is ``async def``. A guard that checked only
#: ``ast.With`` would therefore be blind in exactly the code most likely to grow
#: a transaction next — not an exotic spelling, the ordinary one for this
#: codebase. Missed on the first cut of this file.
_WITH_NODES = (ast.With, ast.AsyncWith)


def _opens_a_transaction(node: ast.With | ast.AsyncWith) -> bool:
    """True for ``with <anything>.transaction():`` — the psycopg spelling.

    Matched on the attribute name rather than on the receiver, so a connection
    held in ``conn``, ``self._conn`` or ``db`` is caught identically. A caller
    who binds the transaction to a different name entirely is out of scope; see
    the module docstring on what this cannot see.
    """
    for item in node.items:
        call = item.context_expr
        if (
            isinstance(call, ast.Call)
            and isinstance(call.func, ast.Attribute)
            and call.func.attr == "transaction"
        ):
            return True
    return False


def _passes_vault_root_none(call: ast.Call) -> bool:
    for keyword in call.keywords:
        if keyword.arg == "vault_root":
            return isinstance(keyword.value, ast.Constant) and keyword.value.value is None
    return False  # absent entirely is also a violation: the default is not None-safe


#: Every function whose call can put a mirror on disk. Listed rather than
#: hardcoded into the scan so that adding a third writer is one edit HERE and
#: is picked up by the invariant scan, the vacuity count and the fixtures at
#: once — the arrangement that stops a scanner and its counter drifting apart.
_MIRROR_WRITERS = ("update_document", "write_vault_mirror")


def _called_name(call: ast.Call) -> str | None:
    """The final name of a call, for both ``f(...)`` and ``mod.f(...)``.

    Requiring ``ast.Name`` alone let the attribute spelling through. That
    interacted badly with the vacuity guard below: a WHOLESALE migration to
    ``ingest.update_document`` would trip it, but a MIXED tree — today's direct
    calls plus one new attribute call inside a transaction — passed both tests
    silently. Matching on the final name covers both spellings, at the cost of
    also matching an unrelated method that happens to share the name, which is
    the safe direction for a guard to err.
    """
    func = call.func
    if isinstance(func, ast.Name):
        return func.id
    if isinstance(func, ast.Attribute):
        return func.attr
    return None


def _is_mirror_writer(call: ast.Call) -> bool:
    return _called_name(call) in _MIRROR_WRITERS


def _violates(call: ast.Call) -> bool:
    """Whether this in-transaction call would put a mirror on disk.

    The two writers have DIFFERENT escape conditions, which is why this is not a
    single membership test: ``update_document`` is fine inside a transaction as
    long as ``vault_root=None`` suppresses its write, whereas
    ``write_vault_mirror`` has no safe form — the call *is* the write.
    """
    name = _called_name(call)
    # Gated on _MIRROR_WRITERS rather than on hardcoded names, so the tuple is
    # the SINGLE source both this scan and the vacuity count read. It was not,
    # briefly: `_violates` hardcoded both names while only the counter consulted
    # the tuple, so deleting a writer from it disarmed the counter and left the
    # scan working — the two disagreeing silently, in the very structure added
    # to stop them disagreeing. Caught by mutating the tuple and finding all
    # twelve tests still green.
    if name not in _MIRROR_WRITERS:
        return False
    if name == "update_document":
        return not _passes_vault_root_none(call)
    return True


def _violations(tree: ast.AST, path: Path) -> list[str]:
    """Every in-transaction call that would write a mirror before the commit."""
    found: list[str] = []
    for node in ast.walk(tree):
        if not isinstance(node, _WITH_NODES) or not _opens_a_transaction(node):
            continue
        for inner in ast.walk(node):
            if isinstance(inner, ast.Call) and _violates(inner):
                found.append(f"{path}:{inner.lineno}")
    return found


def _scan(root: Path) -> list[str]:
    violations: list[str] = []
    for path in sorted(root.rglob("*.py")):
        violations.extend(_violations(ast.parse(path.read_text(encoding="utf-8")), path))
    return violations


def test_no_call_site_writes_the_mirror_from_inside_a_transaction() -> None:
    """The invariant, over the whole of ``src/brain``."""
    violations = _scan(SRC)
    assert violations == [], (
        "these `update_document` calls run inside a `conn.transaction()` while "
        "still asking it to write the vault mirror:\n  "
        + "\n  ".join(violations)
        + "\n\nThe mirror is a FILE — no rollback can unwrite it — so a failure "
        "later in the transaction leaves an orphan the database never recorded "
        "and nothing can ever reclaim. Pass `vault_root=None` here and call "
        "`brain.ingest.write_vault_mirror(...)` after the outer transaction "
        "commits. See `_update_ingested_note` in brain/ui/notes_service.py."
    )


def test_the_guard_finds_a_violation_when_one_exists(tmp_path: Path) -> None:
    """Clause (c): point it at a violating call site and watch it fire.

    Without this, the guard above passes on a clean tree and on a tree it is
    structurally incapable of reading — a scanner with a typo'd function name
    finds nothing and reports success.
    """
    offender = tmp_path / "offender.py"
    offender.write_text(
        "def edit(conn, doc_id, cfg):\n"
        "    with conn.transaction():\n"
        "        update_document(conn, document_id=doc_id, vault_root=cfg.vault_path)\n",
        encoding="utf-8",
    )

    violations = _scan(tmp_path)

    assert len(violations) == 1, f"expected exactly one violation, got {violations}"
    assert violations[0].endswith(":3")


def test_an_async_with_transaction_is_caught_too(tmp_path: Path) -> None:
    """``ast.AsyncWith`` is a DIFFERENT node type, and this codebase is async.

    Every route handler in ``brain.ui`` is ``async def``, so a transaction moved
    into one would be written ``async with conn.transaction():`` — the ordinary
    spelling here, not an exotic one. The first cut of this guard checked
    ``ast.With`` only and was therefore blind in precisely the code most likely
    to grow the next transaction, while its docstring claimed to catch "the
    spelling a future author will actually write".
    """
    offender = tmp_path / "async_offender.py"
    offender.write_text(
        "async def edit(conn, doc_id, cfg):\n"
        "    async with conn.transaction():\n"
        "        update_document(conn, document_id=doc_id, vault_root=cfg.vault_path)\n",
        encoding="utf-8",
    )

    violations = _scan(tmp_path)

    assert len(violations) == 1, f"expected exactly one violation, got {violations}"
    assert violations[0].endswith(":3")


def test_the_attribute_spelling_is_caught_too(tmp_path: Path) -> None:
    """``ingest.update_document(...)`` must not evade the scan.

    Requiring ``ast.Name`` missed this. The dangerous case is a MIXED tree: the
    vacuity guard would notice a wholesale migration to the attribute spelling
    (the count would collapse), but one new attribute call added beside today's
    direct calls kept both this scan and that count happy.
    """
    offender = tmp_path / "attribute_offender.py"
    offender.write_text(
        "def edit(conn, doc_id, cfg):\n"
        "    with conn.transaction():\n"
        "        ingest.update_document(conn, document_id=doc_id, "
        "vault_root=cfg.vault_path)\n",
        encoding="utf-8",
    )

    assert len(_scan(tmp_path)) == 1


def test_an_async_compliant_call_is_still_accepted(tmp_path: Path) -> None:
    """Widening to ``AsyncWith`` must not start flagging correct async callers."""
    compliant = tmp_path / "async_compliant.py"
    compliant.write_text(
        "async def edit(conn, doc_id):\n"
        "    async with conn.transaction():\n"
        "        update_document(conn, document_id=doc_id, vault_root=None)\n",
        encoding="utf-8",
    )

    assert _scan(tmp_path) == []


@pytest.mark.parametrize(
    ("spelling", "call"),
    [
        ("bare name", "write_vault_mirror(conn, doc_id, vault_root=cfg.vault_path)"),
        ("attribute", "ingest.write_vault_mirror(conn, doc_id, vault_root=cfg.vault_path)"),
    ],
)
def test_writing_the_mirror_directly_inside_a_transaction_is_a_violation(
    tmp_path: Path, spelling: str, call: str
) -> None:
    """The successor gap: the guard policed the MECHANISM, not the property.

    ``update_document`` is only one of two ways to put a mirror on disk. Calling
    ``write_vault_mirror`` inside a transaction commits the identical defect —
    a file written while the DB work is uncommitted, which no rollback can
    unwrite — and the original guard was green over both spellings of it.

    There is no ``vault_root=None`` escape here, unlike ``update_document``:
    this call IS the write, so being inside a transaction at all is the
    violation.
    """
    offender = tmp_path / f"direct_{spelling.replace(' ', '_')}.py"
    offender.write_text(
        "def edit(conn, doc_id, cfg):\n"
        "    with conn.transaction():\n"
        f"        {call}\n",
        encoding="utf-8",
    )

    violations = _scan(tmp_path)

    assert len(violations) == 1, f"expected exactly one violation, got {violations}"
    assert violations[0].endswith(":3")


def test_writing_the_mirror_after_the_transaction_is_the_correct_shape(
    tmp_path: Path,
) -> None:
    """The fix's own shape must not be flagged.

    This is what ``_update_ingested_note`` does — suppress the write inside the
    transaction, perform it after the commit. A guard that flagged this would be
    demanding the defect it exists to prevent.
    """
    compliant = tmp_path / "after_commit.py"
    compliant.write_text(
        "def edit(conn, doc_id, cfg):\n"
        "    with conn.transaction():\n"
        "        update_document(conn, document_id=doc_id, vault_root=None)\n"
        "    write_vault_mirror(conn, doc_id, vault_root=cfg.vault_path)\n",
        encoding="utf-8",
    )

    assert _scan(tmp_path) == []


def test_omitting_vault_root_entirely_is_also_a_violation(tmp_path: Path) -> None:
    """The default is ``None``, but silence must not be read as intent.

    ``update_document``'s ``vault_root`` defaults to ``None``, so an omitted
    argument is harmless TODAY. It is still reported: the guard's subject is
    whether the author decided, and a call that never mentions ``vault_root``
    would silently start writing mirrors inside a transaction the day that
    default changes.
    """
    offender = tmp_path / "silent.py"
    offender.write_text(
        "def edit(conn, doc_id):\n"
        "    with conn.transaction():\n"
        "        update_document(conn, document_id=doc_id)\n",
        encoding="utf-8",
    )

    assert len(_scan(tmp_path)) == 1


def test_a_compliant_call_inside_a_transaction_is_accepted(tmp_path: Path) -> None:
    """Clause (a) at the unit level: the guard must not fire on the fix itself.

    A check that flagged the compliant spelling too would be indistinguishable
    from one that flags everything, and the whole-tree test above would then be
    passing only because the tree happens to be empty of calls it recognises.
    """
    compliant = tmp_path / "compliant.py"
    compliant.write_text(
        "def edit(conn, doc_id):\n"
        "    with conn.transaction():\n"
        "        update_document(conn, document_id=doc_id, vault_root=None)\n",
        encoding="utf-8",
    )

    assert _scan(tmp_path) == []


def test_a_call_outside_any_transaction_is_not_the_guard_s_business(
    tmp_path: Path,
) -> None:
    """The five CLI/MCP call sites pass ``vault_root=cfg.vault_path`` legitimately.

    They own no outer transaction, so ``update_document`` is the outermost and
    writes its mirror post-commit exactly as designed. A guard that flagged them
    would be demanding they break themselves.
    """
    fine = tmp_path / "fine.py"
    fine.write_text(
        "def edit(conn, doc_id, cfg):\n"
        "    update_document(conn, document_id=doc_id, vault_root=cfg.vault_path)\n",
        encoding="utf-8",
    )

    assert _scan(tmp_path) == []


def test_the_guard_actually_reads_the_real_tree() -> None:
    """Anti-vacuity: the whole-tree test proves nothing over zero call sites.

    If a rename ever made ``update_document`` invisible to this scanner — the
    function renamed, or every call routed through an alias — the invariant test
    above would keep passing while checking nothing at all. So assert the
    scanner still SEES the call sites it is meant to police.
    """
    seen: dict[str, int] = dict.fromkeys(_MIRROR_WRITERS, 0)
    for path in sorted(SRC.rglob("*.py")):
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            # Counted with the SAME matcher `_violations` uses, and over the
            # SAME set of writers. Both halves of that matter and both were
            # wrong once: counting only `ast.Name` while the scanner matched
            # only `ast.Name` was self-consistent and jointly blind to the
            # attribute spelling; counting only `update_document` while the
            # scanner policed two writers would be jointly blind to the second.
            # A counter that knows less than its scanner cannot detect the
            # scanner going quiet.
            if isinstance(node, ast.Call) and _is_mirror_writer(node):
                seen[_called_name(node)] += 1  # type: ignore[index]

    # Per-writer floors, not a total: a total would let one writer's call sites
    # vanish while another's grew, and report health.
    expected = {"update_document": 6, "write_vault_mirror": 2}

    # Removing a writer from _MIRROR_WRITERS must BREAK something, not silently
    # narrow what is policed. Without this, deleting an entry left the scan and
    # this count agreeing that everything was fine while one writer went
    # entirely unguarded — proven by mutation, twelve tests green.
    assert set(expected) == set(_MIRROR_WRITERS), (
        f"_MIRROR_WRITERS is {sorted(_MIRROR_WRITERS)} but this guard has floors "
        f"for {sorted(expected)}. Adding a mirror writer means adding its floor "
        "here; removing one means removing its floor deliberately, not by "
        "letting this check quietly stop covering it."
    )
    short = {n: c for n, c in seen.items() if c < expected[n]}
    assert not short, (
        f"the scanner sees too few mirror-writer call sites in src/brain: {short} "
        f"(found {seen}, floors {expected}). When this guard was written there "
        "were 7 `update_document` calls (2 in cli.py, 1 each in cli_docs.py, "
        "cli_sensitivity.py, mcp_server.py, 2 in ui/notes_service.py) and 2 "
        "`write_vault_mirror` calls (ingest/__init__.py, ui/notes_service.py). "
        "Either they were removed, or they are now spelled in a way this guard "
        "cannot see — in which case the invariant test above is passing "
        "vacuously over the ones it can no longer find."
    )
