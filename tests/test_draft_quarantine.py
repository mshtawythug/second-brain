"""Tests for the draft / seed quarantine pipeline (P1.6).

End-to-end coverage for the four moving parts:

1. ``update_document(... new_draft=True)`` flips the ``documents.draft``
   column without touching the body or chunks.
2. ``brain mark-draft <id-prefix>`` / ``brain mark-published <id-prefix>``
   CLI commands resolve the prefix, flip the flag, and regenerate the
   on-disk mirror via the existing ``vault_root`` opt-in path.
3. The vault-export frontmatter writer emits ``draft: true`` only when the
   column is ``True`` (omitted otherwise — visual-noise avoidance).
4. Idempotency: re-running ``mark-draft`` / ``mark-published`` on a doc
   already in the target state is a no-op and prints
   ``<short-id> is already <state>``.
5. Error paths: unknown / ambiguous prefixes exit non-zero via the shared
   ``_resolve_id`` plumbing.
"""
from __future__ import annotations

import os
from collections.abc import Callable
from pathlib import Path
from typing import Any

import psycopg
import pytest
from typer.testing import CliRunner

from brain.cli import app
from brain.ingest import ExtractedDoc, ingest_document, update_document
from brain.vault.export import regenerate_vault_file
from brain.vault.frontmatter import parse_frontmatter

TEST_DATABASE_URL = os.environ.get(
    "TEST_DATABASE_URL",
    "postgresql://brain:brain@localhost:5433/second_brain_test",
)


def _set_env(monkeypatch: pytest.MonkeyPatch, vault: Path) -> None:
    """Wire ``DATABASE_URL`` + ``BRAIN_VAULT_PATH`` for the CLI under test.

    ``Config.load()`` reads both at command entry; the mark-draft path
    additionally builds ``cfg.vault_path`` to thread into
    ``update_document(vault_root=...)`` for the mirror writeback.
    """
    monkeypatch.setenv("DATABASE_URL", TEST_DATABASE_URL)
    monkeypatch.setenv("BRAIN_VAULT_PATH", str(vault))


def _draft_for(doc_id: str) -> bool:
    """Read ``documents.draft`` for ``doc_id`` via a fresh connection."""
    with psycopg.connect(TEST_DATABASE_URL) as conn:
        row = conn.execute(
            "SELECT draft FROM documents WHERE id=%s", (doc_id,)
        ).fetchone()
    assert row is not None, f"document {doc_id} disappeared"
    return bool(row[0])


def _ingest_manual(
    test_db: psycopg.Connection[Any],
    fake_embedder: Any,
    *,
    title: str = "draft test",
    content: str = "body content for draft test\n",
    external_id: str = "ext-draft-1",
    vault_root: Path | None = None,
) -> str:
    """Ingest a manual doc with deterministic source kind so the export
    path emits a real mirror file under ``_ingested/manual/``.
    """
    res = ingest_document(
        test_db,
        embedder=fake_embedder,
        doc=ExtractedDoc(
            title=title,
            content=content,
            content_type="note",
            source_path=None,
            metadata={},
        ),
        source_kind="manual",
        source_external_id=external_id,
        vault_root=vault_root,
    )
    assert res.document_id is not None
    return res.document_id


# ---------------------------------------------------------------------------
# update_document — direct API tests (no CLI runner)
# ---------------------------------------------------------------------------


def test_update_document_supports_draft_kwarg(
    test_db: psycopg.Connection[Any], fake_embedder: Any
) -> None:
    """``update_document(... new_draft=True)`` flips the column.

    Asserts the minimal contract: the typed column moves from FALSE → TRUE,
    ``fields_changed`` reports ``draft``, and unrelated columns (title,
    metadata, content_hash) plus the chunk count are untouched. This is
    the regression for the "rule-9 mypy will catch a missing kwarg, but
    only if a test exercises it" lesson — without this test the kwarg
    could silently no-op without breaking type checks.
    """
    doc_id = _ingest_manual(test_db, fake_embedder, title="untouched")
    pre = test_db.execute(
        "SELECT title, metadata, content_hash, "
        "(SELECT count(*) FROM chunks WHERE document_id=%s), draft "
        "FROM documents WHERE id=%s",
        (doc_id, doc_id),
    ).fetchone()
    assert pre is not None
    title_before, meta_before, hash_before, chunks_before, draft_before = pre
    assert draft_before is False  # default

    result = update_document(test_db, document_id=doc_id, new_draft=True)
    assert result.fields_changed == ["draft"]
    assert result.rechunked is False

    post = test_db.execute(
        "SELECT title, metadata, content_hash, "
        "(SELECT count(*) FROM chunks WHERE document_id=%s), draft "
        "FROM documents WHERE id=%s",
        (doc_id, doc_id),
    ).fetchone()
    assert post is not None
    assert post[0] == title_before
    assert post[1] == meta_before
    assert post[2] == hash_before
    assert post[3] == chunks_before  # no rechunk
    assert post[4] is True


def test_update_document_draft_false_is_noop(
    test_db: psycopg.Connection[Any], fake_embedder: Any
) -> None:
    """Passing ``new_draft=False`` to a doc already at False reports no fields.

    Mirrors the existing ``new_title=current_title`` path — the function
    shortcuts when the new value matches the current one. Important so
    ``brain mark-published`` on an already-published doc doesn't show a
    spurious ``(draft)`` field-changed line.
    """
    doc_id = _ingest_manual(test_db, fake_embedder)
    result = update_document(test_db, document_id=doc_id, new_draft=False)
    assert result.fields_changed == []


def test_update_document_draft_kwarg_triggers_mirror_rewrite(
    test_db: psycopg.Connection[Any], fake_embedder: Any, tmp_path: Path
) -> None:
    """When ``vault_root`` is set, flipping ``draft`` regenerates the file.

    Catches the regression where ``draft`` would be added to the SET
    clause but forgotten in ``_MIRROR_FRONTMATTER_FIELDS`` — without it,
    the on-disk mirror's frontmatter would never get ``draft: true`` and
    the Quartz emitter's filter would have nothing to fire on.
    """
    vault = tmp_path / "vault"
    doc_id = _ingest_manual(test_db, fake_embedder, vault_root=vault)
    # Find the mirror file the ingest emitted.
    mirrors = list((vault / "_ingested" / "manual").glob("*.md"))
    assert len(mirrors) == 1
    mirror = mirrors[0]
    fm_before, _ = parse_frontmatter(mirror.read_text(encoding="utf-8"))
    assert "draft" not in fm_before

    update_document(
        test_db,
        document_id=doc_id,
        new_draft=True,
        vault_root=vault,
    )

    fm_after, _ = parse_frontmatter(mirror.read_text(encoding="utf-8"))
    assert fm_after.get("draft") is True


def test_published_doc_omits_draft_frontmatter(
    test_db: psycopg.Connection[Any], fake_embedder: Any, tmp_path: Path
) -> None:
    """A fresh ingest (draft default-False) writes NO ``draft:`` key.

    Visual-noise guard: vast majority of docs are published; writing
    ``draft: false`` on every export would clutter the YAML. Frontmatter
    must only carry the key when it's True.
    """
    vault = tmp_path / "vault"
    doc_id = _ingest_manual(test_db, fake_embedder, vault_root=vault)
    written = regenerate_vault_file(test_db, doc_id, vault_path=vault)
    fm, _ = parse_frontmatter(written.read_text(encoding="utf-8"))
    assert "draft" not in fm


def test_draft_doc_emits_draft_true_in_frontmatter(
    test_db: psycopg.Connection[Any], fake_embedder: Any, tmp_path: Path
) -> None:
    """A doc with ``draft=True`` writes ``draft: true`` into frontmatter.

    Symmetric with the omit-when-false test above; together they pin the
    asymmetry that the Quartz filter relies on.
    """
    vault = tmp_path / "vault"
    doc_id = _ingest_manual(test_db, fake_embedder, vault_root=vault)
    test_db.execute(
        "UPDATE documents SET draft=TRUE WHERE id=%s", (doc_id,)
    )
    written = regenerate_vault_file(
        test_db, doc_id, vault_path=vault, force=True
    )
    fm, _ = parse_frontmatter(written.read_text(encoding="utf-8"))
    assert fm.get("draft") is True


# ---------------------------------------------------------------------------
# CLI: brain mark-draft / brain mark-published
# ---------------------------------------------------------------------------


def test_mark_draft_sets_column_true(
    test_db: psycopg.Connection[Any],
    fake_embedder: Any,
    tmp_path: Path,
    patch_embedder: Callable[[object], None],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``brain mark-draft <prefix>`` flips ``documents.draft`` to True."""
    patch_embedder(fake_embedder)
    vault = tmp_path / "vault"
    doc_id = _ingest_manual(test_db, fake_embedder, vault_root=vault)
    _set_env(monkeypatch, vault)

    result = CliRunner().invoke(app, ["mark-draft", doc_id[:8]])
    assert result.exit_code == 0, result.output
    assert "marked" in result.output
    assert "as draft" in result.output
    assert "(was published)" in result.output
    assert _draft_for(doc_id) is True


def test_mark_published_sets_column_false(
    test_db: psycopg.Connection[Any],
    fake_embedder: Any,
    tmp_path: Path,
    patch_embedder: Callable[[object], None],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``brain mark-published <prefix>`` flips a draft doc back to published."""
    patch_embedder(fake_embedder)
    vault = tmp_path / "vault"
    doc_id = _ingest_manual(test_db, fake_embedder, vault_root=vault)
    test_db.execute(
        "UPDATE documents SET draft=TRUE WHERE id=%s", (doc_id,)
    )
    _set_env(monkeypatch, vault)

    result = CliRunner().invoke(app, ["mark-published", doc_id[:8]])
    assert result.exit_code == 0, result.output
    assert "marked" in result.output
    assert "as published" in result.output
    assert "(was draft)" in result.output
    assert _draft_for(doc_id) is False


def test_mark_draft_rewrites_mirror_with_frontmatter(
    test_db: psycopg.Connection[Any],
    fake_embedder: Any,
    tmp_path: Path,
    patch_embedder: Callable[[object], None],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """After ``mark-draft`` the on-disk mirror's frontmatter has ``draft: true``.

    End-to-end check that the auto-mirror writeback contract fires through
    the new CLI command — the user shouldn't have to run a separate
    ``brain vault export --force`` to see the change reflected on disk.
    """
    patch_embedder(fake_embedder)
    vault = tmp_path / "vault"
    doc_id = _ingest_manual(test_db, fake_embedder, vault_root=vault)
    mirrors = list((vault / "_ingested" / "manual").glob("*.md"))
    assert len(mirrors) == 1
    mirror = mirrors[0]

    fm_before, _ = parse_frontmatter(mirror.read_text(encoding="utf-8"))
    assert "draft" not in fm_before

    _set_env(monkeypatch, vault)
    result = CliRunner().invoke(app, ["mark-draft", doc_id[:8]])
    assert result.exit_code == 0, result.output

    fm_after, _ = parse_frontmatter(mirror.read_text(encoding="utf-8"))
    assert fm_after.get("draft") is True


def test_mark_draft_idempotent(
    test_db: psycopg.Connection[Any],
    fake_embedder: Any,
    tmp_path: Path,
    patch_embedder: Callable[[object], None],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Running ``mark-draft`` twice prints ``already draft`` the second time."""
    patch_embedder(fake_embedder)
    vault = tmp_path / "vault"
    doc_id = _ingest_manual(test_db, fake_embedder, vault_root=vault)
    _set_env(monkeypatch, vault)

    runner = CliRunner()
    first = runner.invoke(app, ["mark-draft", doc_id[:8]])
    assert first.exit_code == 0, first.output

    second = runner.invoke(app, ["mark-draft", doc_id[:8]])
    assert second.exit_code == 0, second.output
    assert "is already draft" in second.output
    # Column stays True; no churn.
    assert _draft_for(doc_id) is True


def test_mark_published_idempotent(
    test_db: psycopg.Connection[Any],
    fake_embedder: Any,
    tmp_path: Path,
    patch_embedder: Callable[[object], None],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Running ``mark-published`` on an already-published doc is a no-op."""
    patch_embedder(fake_embedder)
    vault = tmp_path / "vault"
    doc_id = _ingest_manual(test_db, fake_embedder, vault_root=vault)
    _set_env(monkeypatch, vault)

    result = CliRunner().invoke(app, ["mark-published", doc_id[:8]])
    assert result.exit_code == 0, result.output
    assert "is already published" in result.output
    assert _draft_for(doc_id) is False


def test_mark_draft_unknown_prefix_raises(
    test_db: psycopg.Connection[Any],
    fake_embedder: Any,
    tmp_path: Path,
    patch_embedder: Callable[[object], None],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A prefix that matches no row exits non-zero via ``IdPrefixNotFound``."""
    patch_embedder(fake_embedder)
    _set_env(monkeypatch, tmp_path / "vault")
    # Use a syntactically valid prefix (hex, ≥6 chars) that won't match.
    result = CliRunner().invoke(app, ["mark-draft", "deadbe"])
    assert result.exit_code == 1
    # The error message comes from BrainError → "document not found: …".
    assert "not found" in result.output or "not found" in result.stderr


def test_mark_draft_ambiguous_prefix_raises(
    test_db: psycopg.Connection[Any],
    patch_embedder: Callable[[object], None],
    fake_embedder: Any,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Two docs sharing a UUID prefix → ambiguity error, exit 1.

    Postgres mints random UUIDs so we can't rely on natural collisions
    in a 6-char prefix; we craft two rows with explicit UUIDs that share
    a long prefix so the resolver hits the ``IdPrefixAmbiguous`` branch
    deterministically.
    """
    patch_embedder(fake_embedder)
    # 8-hex-char shared head of two valid UUIDs → resolver sees both at
    # any 6-char query within that head and raises IdPrefixAmbiguous.
    shared_head = "deadbeef"
    shared_prefix = "deadbe"  # ≥6 chars, fits within shared_head
    full_a = f"{shared_head}-0000-4000-8000-000000000001"
    full_b = f"{shared_head}-0000-4000-8000-000000000002"
    test_db.execute(
        "INSERT INTO documents "
        "(id, title, content, content_hash, content_type, kind) "
        "VALUES (%s, 'A', 'body a', 'h-a', 'note', 'ingested'), "
        "(%s, 'B', 'body b', 'h-b', 'note', 'ingested')",
        (full_a, full_b),
    )
    _set_env(monkeypatch, tmp_path / "vault")
    result = CliRunner().invoke(app, ["mark-draft", shared_prefix])
    assert result.exit_code == 1
    assert "ambiguous" in (result.output + (result.stderr or ""))


def test_mark_draft_db_only_when_vault_path_is_null(
    test_db: psycopg.Connection[Any],
    fake_embedder: Any,
    tmp_path: Path,
    patch_embedder: Callable[[object], None],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A doc ingested without ``vault_root`` has no mirror; the CLI still works.

    Regression: ``update_document`` skips the mirror write when
    ``cur_kind != 'vault'`` AND the row has no on-disk mirror — the
    DB write must still succeed even without a vault file to rewrite.
    Catches the failure mode where a ``vault_root=None`` ingest leaves
    the user unable to mark-draft because of an over-eager mirror call.
    """
    patch_embedder(fake_embedder)
    # No vault_root → no mirror file written.
    doc_id = _ingest_manual(test_db, fake_embedder)
    assert _draft_for(doc_id) is False

    _set_env(monkeypatch, tmp_path / "vault")  # vault dir exists but is empty
    result = CliRunner().invoke(app, ["mark-draft", doc_id[:8]])
    assert result.exit_code == 0, result.output
    assert _draft_for(doc_id) is True
