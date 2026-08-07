"""Every write path that must — and must NOT — bump ``documents.updated_at``.

``updated_at`` answers "when did the user's knowledge in this document last
change". That contract is only worth anything if both halves hold:

- **Bump:** a real edit advances it, or ``--updated-after`` misses the very
  documents the user is looking for.
- **No bump:** maintenance advances nothing. A single ``brain enrich
  --backfill`` over the whole corpus would otherwise restamp every row and
  make the filter permanently useless. The negative tests below exist
  specifically to fail when a later contributor "helpfully" adds a bump to a
  maintenance job.

Each test rewinds ``updated_at`` to a sentinel far in the past, performs one
operation, and asserts the timestamp did or did not move. All documents are
synthetic.
"""
from __future__ import annotations

import hashlib
import os
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import psycopg
import pytest
from typer.testing import CliRunner

from brain.backfill.source_rows import backfill_source_rows
from brain.cli import _backfill_krisp_participant_keys, app
from brain.enrichment import SummaryResult
from brain.ingest import (
    ExtractedDoc,
    _enrich_post_ingest_hook,
    apply_tags,
    ingest_document,
    update_document,
)
from brain.vault.export import regenerate_vault_file
from brain.vault.frontmatter import dump_frontmatter, parse_frontmatter
from brain.vault.sync import _legacy_body_hash, sync_one_file

TEST_DATABASE_URL = os.environ.get(
    "TEST_DATABASE_URL",
    "postgresql://brain:brain@localhost:5434/second_brain_test",
)

#: Sentinel "long ago". Any bump lands at NOW(), so ``> _AGED`` is unambiguous.
_AGED = datetime(2020, 1, 1, 0, 0, 0, tzinfo=UTC)


@dataclass
class _FakeEnricher:
    """In-memory enricher matching the :class:`OllamaEnricher` surface.

    The same explicit test double the enrichment suites use — passed through
    the public ``enricher=`` kwarg, never patched onto a module.
    """

    model: str = "llama3.1:8b"
    summary_text: str = "Canned synthetic summary."

    def count_tokens(self, text: str) -> int:
        return max(1, len(text) // 4)

    def summarize(self, title: str, content: str) -> SummaryResult:
        return SummaryResult(summary=self.summary_text, model=self.model)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _updated_at(conn: psycopg.Connection[Any], doc_id: str) -> datetime:
    row = conn.execute(
        "SELECT updated_at FROM documents WHERE id = %s", (doc_id,)
    ).fetchone()
    assert row is not None, f"document {doc_id} vanished"
    assert row[0] is not None, "updated_at is NOT NULL — a NULL here is a bug"
    return row[0]  # type: ignore[no-any-return]


def _age(conn: psycopg.Connection[Any], doc_id: str) -> None:
    """Rewind ``updated_at`` to the sentinel so any bump is unmistakable."""
    conn.execute(
        "UPDATE documents SET updated_at = %s WHERE id = %s", (_AGED, doc_id)
    )


def _seed(
    conn: psycopg.Connection[Any],
    embedder: object,
    *,
    title: str = "Synthetic edited note",
    content: str = "original body text",
    content_type: str = "note",
    source_kind: str = "manual",
    source_external_id: str | None = None,
    tags: list[str] | None = None,
    metadata: dict[str, Any] | None = None,
) -> str:
    result = ingest_document(
        conn,
        embedder=embedder,  # type: ignore[arg-type]
        doc=ExtractedDoc(
            title=title,
            content=content,
            content_type=content_type,
            source_path=None,
            metadata=metadata or {},
        ),
        source_kind=source_kind,
        source_external_id=source_external_id or f"{source_kind}:{title}",
        tags=tags or [],
    )
    assert result.document_id is not None
    return result.document_id


def _write_vault_note(
    vault: Path, *, relative: str, doc_id: str, title: str, body: str
) -> Path:
    path = vault / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(dump_frontmatter({"id": doc_id, "title": title}, body))
    return path


# ===========================================================================
# BUMP — a real edit must advance updated_at
# ===========================================================================


def test_ingest_insert_stamps_updated_at_from_the_column_default(
    test_db: psycopg.Connection[Any], fake_embedder: Any
) -> None:
    """Bump site #1: the INSERT names no ``updated_at`` — the default supplies it.

    This is why site #1 needs no code change; asserting it keeps that true.
    """
    # Arrange / Act
    doc_id = _seed(test_db, fake_embedder)

    # Assert
    row = test_db.execute(
        "SELECT NOW() - updated_at FROM documents WHERE id = %s", (doc_id,)
    ).fetchone()
    assert row is not None
    assert row[0].total_seconds() < 60, "a fresh ingest must be stamped NOW()"


def test_thread_upsert_bumps_updated_at(
    test_db: psycopg.Connection[Any], fake_embedder: Any
) -> None:
    """Bump site #2: ``_update_doc_in_place`` — the gmail-thread upsert.

    A new message arriving on an existing thread genuinely changes what the
    user knows about that thread.
    """
    # Arrange
    thread_id = "thread-synthetic-025"
    doc_id = _seed(
        test_db, fake_embedder,
        title="Subject v1",
        content="first message body",
        content_type="email_thread",
        source_kind="gmail",
        source_external_id="msg-1",
        metadata={"thread_id": thread_id},
    )
    _age(test_db, doc_id)

    # Act — same thread, new body: routes to the in-place update path.
    second = ingest_document(
        test_db,
        embedder=fake_embedder,
        doc=ExtractedDoc(
            title="Subject v2",
            content="first message body plus a reply",
            content_type="email_thread",
            source_path=None,
            metadata={"thread_id": thread_id},
        ),
        source_kind="gmail",
        source_external_id="msg-2",
    )

    # Assert
    assert second.document_id == doc_id
    assert second.created is False
    assert _updated_at(test_db, doc_id) > _AGED


def test_update_document_bumps_updated_at(
    test_db: psycopg.Connection[Any], fake_embedder: Any
) -> None:
    """Bump site #3: ``update_document`` — the ``brain edit`` path."""
    # Arrange
    doc_id = _seed(test_db, fake_embedder)
    _age(test_db, doc_id)

    # Act
    update_document(
        test_db,
        document_id=doc_id,
        embedder=fake_embedder,
        new_content="a genuinely different body",
    )

    # Assert
    assert _updated_at(test_db, doc_id) > _AGED


def test_set_draft_bumps_updated_at_transitively(
    test_db: psycopg.Connection[Any], fake_embedder: Any
) -> None:
    """``brain mark-draft`` inherits the bump — it routes through site #3.

    ``_set_draft`` issues no UPDATE of its own, so this covers the boundary
    rather than a second code change. Wave 3's ``_set_sensitivity`` uses a
    *direct* UPDATE and therefore must bump explicitly.
    """
    # Arrange
    doc_id = _seed(test_db, fake_embedder)
    _age(test_db, doc_id)

    # Act
    update_document(test_db, document_id=doc_id, new_draft=True)

    # Assert
    assert _updated_at(test_db, doc_id) > _AGED


def test_apply_tags_add_bumps_updated_at(
    test_db: psycopg.Connection[Any], fake_embedder: Any
) -> None:
    """Bump site #4a: ``brain tag <id> +x`` is a deliberate user classification."""
    # Arrange
    doc_id = _seed(test_db, fake_embedder)
    _age(test_db, doc_id)

    # Act
    apply_tags(test_db, doc_id, add=["planning"])

    # Assert
    assert _updated_at(test_db, doc_id) > _AGED


def test_apply_tags_remove_bumps_updated_at(
    test_db: psycopg.Connection[Any], fake_embedder: Any
) -> None:
    """Bump site #4b: removing a tag is equally deliberate."""
    # Arrange
    doc_id = _seed(test_db, fake_embedder, tags=["planning"])
    _age(test_db, doc_id)

    # Act
    apply_tags(test_db, doc_id, remove=["planning"])

    # Assert
    assert _updated_at(test_db, doc_id) > _AGED


def test_vault_sync_update_bumps_updated_at(
    test_db: psycopg.Connection[Any], fake_embedder: Any, tmp_path: Path
) -> None:
    """Bump site #5: the vault-tier upsert UPDATE in ``vault/sync.py``.

    Editing a note in the vault and letting the watcher reconcile it is the
    single most common way a document actually changes.
    """
    # Arrange
    vault = tmp_path / "vault"
    doc_id = _seed(test_db, fake_embedder, title="Vault note")
    note = _write_vault_note(
        vault, relative="notes/vault-note.md", doc_id=doc_id,
        title="Vault note", body="original body text",
    )
    sync_one_file(
        test_db, embedder=fake_embedder, vault_path=vault, file_path=note
    )
    _age(test_db, doc_id)

    # Act — a real body edit on disk.
    note.write_text(
        dump_frontmatter(
            {"id": doc_id, "title": "Vault note"},
            "an edited body, materially different",
        )
    )
    report = sync_one_file(
        test_db, embedder=fake_embedder, vault_path=vault, file_path=note
    )

    # Assert
    assert report.updated == 1, report
    assert _updated_at(test_db, doc_id) > _AGED


# ===========================================================================
# NO BUMP — maintenance must leave updated_at alone
# ===========================================================================


def test_post_ingest_enrich_hook_does_not_bump_updated_at(
    test_db: psycopg.Connection[Any], fake_embedder: Any
) -> None:
    """The auto-summary hook writes derived metadata, not user knowledge.

    Called directly because every *public* route into this hook
    (``ingest_document`` / ``update_document``) legitimately bumps for its own
    reasons — the hook's own statement is the thing under test.
    """
    # Arrange
    body = "Body content. " * 50  # comfortably over the min-token threshold
    doc_id = _seed(test_db, fake_embedder, content=body)
    _age(test_db, doc_id)

    # Act
    _enrich_post_ingest_hook(
        test_db,
        document_id=doc_id,
        doc=ExtractedDoc(
            title="Synthetic edited note",
            content=body,
            content_type="note",
            source_path=None,
            metadata={},
        ),
        enricher=_FakeEnricher(),  # type: ignore[arg-type]
        enrich=True,
        min_tokens=1,
        content_hash=hashlib.sha256(body.encode()).hexdigest(),
    )

    # Assert — the summary landed, the edit timestamp did not move.
    summary = test_db.execute(
        "SELECT summary FROM documents WHERE id = %s", (doc_id,)
    ).fetchone()
    assert summary is not None and summary[0] == "Canned synthetic summary."
    assert _updated_at(test_db, doc_id) == _AGED


def test_enrich_backfill_does_not_bump_updated_at(
    test_db: psycopg.Connection[Any],
    fake_embedder: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``brain enrich --backfill`` over the corpus must not restamp every row.

    This is the negative test the whole feature hinges on: one backfill that
    bumped would make ``--updated-after`` permanently useless.
    """
    # Arrange
    monkeypatch.setenv("DATABASE_URL", TEST_DATABASE_URL)
    monkeypatch.setattr("brain.cli._build_enricher", lambda cfg: _FakeEnricher())
    doc_id = _seed(test_db, fake_embedder, content="Body content. " * 50)
    _age(test_db, doc_id)

    # Act
    result = CliRunner().invoke(app, ["enrich", "--backfill"])

    # Assert
    assert result.exit_code == 0, result.output
    summary = test_db.execute(
        "SELECT summary FROM documents WHERE id = %s", (doc_id,)
    ).fetchone()
    assert summary is not None and summary[0] is not None, "backfill did nothing"
    assert _updated_at(test_db, doc_id) == _AGED


def test_bulk_tag_normalization_does_not_bump_updated_at(
    test_db: psycopg.Connection[Any],
    fake_embedder: Any,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Canonicalizing ``Mixed_Case`` → ``mixed-case`` is not a knowledge change."""
    # Arrange
    monkeypatch.setenv("DATABASE_URL", TEST_DATABASE_URL)
    monkeypatch.setenv("BRAIN_VAULT_PATH", str(tmp_path / "vault"))
    doc_id = _seed(test_db, fake_embedder)
    # Write a non-canonical tag straight to the column, bypassing the
    # normalizing write boundary — exactly the legacy state the backfill
    # exists to repair.
    test_db.execute(
        "UPDATE documents SET tags = %s WHERE id = %s", (["Mixed_Case"], doc_id)
    )
    _age(test_db, doc_id)

    # Act
    result = CliRunner().invoke(app, ["backfill", "normalize-tags"])

    # Assert
    assert result.exit_code == 0, result.output
    tags = test_db.execute(
        "SELECT tags FROM documents WHERE id = %s", (doc_id,)
    ).fetchone()
    assert tags is not None and tags[0] == ["mixed-case"], "backfill did nothing"
    assert _updated_at(test_db, doc_id) == _AGED


def test_krisp_participant_key_backfill_does_not_bump_updated_at(
    test_db: psycopg.Connection[Any], fake_embedder: Any
) -> None:
    """One-shot data repair over historical transcripts, not an edit."""
    # Arrange — a Krisp doc whose metadata predates the participant-key hook.
    doc_id = _seed(
        test_db, fake_embedder,
        title="Synthetic standup",
        content="Alice Example: morning all\nBob Example: morning",
        content_type="transcript",
        source_kind="krisp",
    )
    test_db.execute(
        "UPDATE documents SET metadata = '{}'::jsonb WHERE id = %s", (doc_id,)
    )
    _age(test_db, doc_id)

    # Act
    updated = _backfill_krisp_participant_keys(test_db)

    # Assert
    assert updated == 1
    assert _updated_at(test_db, doc_id) == _AGED


def test_source_row_backfill_does_not_bump_updated_at(
    test_db: psycopg.Connection[Any], fake_embedder: Any
) -> None:
    """Repointing a legacy row at a ``sources`` row changes no knowledge."""
    # Arrange — the backfill's selector: markdown + source_path + NULL source_id.
    doc_id = _seed(test_db, fake_embedder, content_type="markdown")
    test_db.execute(
        "UPDATE documents SET source_id = NULL, source_path = %s WHERE id = %s",
        ("/synthetic/legacy/note.md", doc_id),
    )
    _age(test_db, doc_id)

    # Act
    report = backfill_source_rows(test_db)

    # Assert
    assert report.documents_updated == 1
    assert _updated_at(test_db, doc_id) == _AGED


def test_vault_path_writeback_does_not_bump_updated_at(
    test_db: psycopg.Connection[Any], fake_embedder: Any, tmp_path: Path
) -> None:
    """Recording where a mirror file lives is path bookkeeping."""
    # Arrange
    vault = tmp_path / "vault"
    vault.mkdir()
    doc_id = _seed(test_db, fake_embedder, title="Mirror me")
    _age(test_db, doc_id)

    # Act
    regenerate_vault_file(test_db, doc_id, vault_path=vault, force=True)

    # Assert — vault_path was written, updated_at was not.
    row = test_db.execute(
        "SELECT vault_path FROM documents WHERE id = %s", (doc_id,)
    ).fetchone()
    assert row is not None and row[0] is not None, "writeback did nothing"
    assert _updated_at(test_db, doc_id) == _AGED


def test_content_hash_format_migration_does_not_bump_updated_at(
    test_db: psycopg.Connection[Any], fake_embedder: Any, tmp_path: Path
) -> None:
    """The body is byte-equivalent; only the stored hash's *form* changed.

    ``vault/sync.py`` silently migrates a legacy raw-body hash to the
    normalized form on first sync. Bumping there would restamp every
    pre-normalization document on one ``brain vault sync``.
    """
    # Arrange — sync once so the row exists under the normalized hash, then
    # rewind the stored hash to the legacy form a Phase 1 export would leave.
    #
    # The two forms differ only where normalization bites: ``body_hash``
    # strips leading/trailing whitespace, ``_legacy_body_hash`` does not. So
    # padding the file with trailing newlines leaves the NEW hash untouched
    # while changing the LEGACY one — precisely the "byte-equivalent body,
    # different hash form" state this branch exists to repair.
    vault = tmp_path / "vault"
    doc_id = _seed(test_db, fake_embedder, title="Legacy hashed note")
    note = _write_vault_note(
        vault, relative="notes/legacy.md", doc_id=doc_id,
        title="Legacy hashed note", body="original body text",
    )
    sync_one_file(
        test_db, embedder=fake_embedder, vault_path=vault, file_path=note
    )
    note.write_text(note.read_text().rstrip("\n") + "\n\n\n")
    _, body = parse_frontmatter(note.read_text())
    legacy = _legacy_body_hash(body)
    current = test_db.execute(
        "SELECT content_hash FROM documents WHERE id = %s", (doc_id,)
    ).fetchone()
    assert current is not None and legacy != current[0], (
        "arrangement is a no-op — the legacy and normalized hashes must "
        "differ or the silent-migration branch is never reached"
    )
    test_db.execute(
        "UPDATE documents SET content_hash = %s WHERE id = %s", (legacy, doc_id)
    )
    _age(test_db, doc_id)

    # Act — nothing on disk changed, so this takes the silent-migration branch.
    report = sync_one_file(
        test_db, embedder=fake_embedder, vault_path=vault, file_path=note
    )

    # Assert
    assert report.skipped == 1, report
    migrated = test_db.execute(
        "SELECT content_hash FROM documents WHERE id = %s", (doc_id,)
    ).fetchone()
    assert migrated is not None and migrated[0] != legacy, "hash was not migrated"
    assert _updated_at(test_db, doc_id) == _AGED
