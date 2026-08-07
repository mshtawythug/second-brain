"""Ingest-pipeline integration tests for the secret guard (F4), against a real DB.

The headline test here is the **red-first** one: before the guard existed, a
credential pasted into a note round-tripped verbatim into ``documents.content``
(and from there into ``chunks.content``, the vault mirror, and — for a hosted
embedder — an outbound HTTPS POST). That is the bug this task fixes.
"""
from __future__ import annotations

import os
from pathlib import Path

import psycopg
import pytest
from typer.testing import CliRunner

from brain.cli import app
from brain.errors import SecretGuardError
from brain.ingest import ExtractedDoc, ingest_document, update_document
from tests.secret_fixtures import CLEAN_PROSE, SYNTHETIC_AWS_KEY

TEST_DATABASE_URL = os.environ.get(
    "TEST_DATABASE_URL",
    "postgresql://brain:brain@localhost:5434/second_brain_test",
)


def _patch_embedder(monkeypatch: pytest.MonkeyPatch) -> None:
    """Deterministic embedder + no LLM post-ingest hooks (same shape as
    ``tests/test_cli_ingest.py::_patch_embedder``)."""
    from tests.conftest import FakeEmbedder

    monkeypatch.setattr("brain.cli._build_embedder", lambda cfg: FakeEmbedder())
    monkeypatch.setattr("brain.cli._build_enricher", lambda cfg: None)
    monkeypatch.setenv("BRAIN_GRAPH_ENABLED", "false")


def _stored_content(conn: psycopg.Connection, doc_id: str) -> str:
    row = conn.execute(
        "SELECT content FROM documents WHERE id=%s", (doc_id,)
    ).fetchone()
    assert row is not None, f"document not found: {doc_id}"
    return str(row[0])


def _only_document_id(conn: psycopg.Connection) -> str:
    row = conn.execute("SELECT id::text FROM documents").fetchone()
    assert row is not None, "expected exactly one document"
    return str(row[0])


def test_ingested_document_body_retains_pasted_api_key(
    monkeypatch: pytest.MonkeyPatch,
    test_db: psycopg.Connection,
    tmp_path: Path,
) -> None:
    """RED-FIRST: a pasted credential must not survive into ``documents.content``.

    Before F4 this failed — nothing between ``extract_path`` and the INSERT
    inspected the body, so the key was stored verbatim and duplicated into
    every chunk row.
    """
    # --- setup
    monkeypatch.setenv("DATABASE_URL", TEST_DATABASE_URL)
    monkeypatch.setenv("BRAIN_VAULT_PATH", str(tmp_path / "vault"))
    monkeypatch.setenv("BRAIN_SECRET_GUARD", "redact")
    _patch_embedder(monkeypatch)
    note = tmp_path / "runbook.md"
    note.write_text(
        f"# Deploy runbook\n\nExport the key:\n\n    AWS_ACCESS_KEY_ID={SYNTHETIC_AWS_KEY}\n"
    )

    # --- exercise
    result = CliRunner().invoke(app, ["ingest", str(note)])

    # --- verify
    assert result.exit_code == 0, result.output
    doc_id = _only_document_id(test_db)
    assert SYNTHETIC_AWS_KEY not in _stored_content(test_db, doc_id)


def test_redact_mode_strips_the_key_from_document_and_chunks(
    test_db: psycopg.Connection,
    fake_embedder: object,
) -> None:
    """Library-level twin of the red test: the guard is in the pipeline, not the CLI.

    Asserting on ``chunks.content`` too is what proves the guard runs *upstream*
    of chunking — a CLI-only guard would leave the secret in every chunk row.
    """
    # --- setup
    doc = ExtractedDoc(
        title="Deploy runbook",
        content=f"Export the key:\n\n    AWS_ACCESS_KEY_ID={SYNTHETIC_AWS_KEY}\n",
        content_type="markdown",
        source_path=None,
        metadata={},
    )

    # --- exercise
    result = ingest_document(
        test_db,
        embedder=fake_embedder,  # type: ignore[arg-type]
        doc=doc,
        source_kind="manual",
        source_external_id="runbook-1",
        secret_guard="redact",
    )

    # --- verify
    assert result.document_id is not None
    assert SYNTHETIC_AWS_KEY not in _stored_content(test_db, result.document_id)
    chunk_bodies = [
        r[0]
        for r in test_db.execute(
            "SELECT content FROM chunks WHERE document_id=%s", (result.document_id,)
        ).fetchall()
    ]
    assert chunk_bodies, "expected at least one chunk"
    assert all(SYNTHETIC_AWS_KEY not in body for body in chunk_bodies)


def _secret_doc(*, source_path: str | None = None, **overrides: object) -> ExtractedDoc:
    """An ExtractedDoc whose body carries one synthetic credential."""
    fields: dict[str, object] = {
        "title": "Deploy runbook",
        "content": f"Export the key:\n\n    AWS_ACCESS_KEY_ID={SYNTHETIC_AWS_KEY}\n",
        "content_type": "markdown",
        "source_path": source_path,
        "metadata": {},
    }
    fields.update(overrides)
    return ExtractedDoc(**fields)  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# The ordering regression test — why the guard runs BEFORE _content_hash
# ---------------------------------------------------------------------------


def test_reingesting_the_same_file_under_redact_is_a_clean_skip(
    test_db: psycopg.Connection,
    fake_embedder: object,
    tmp_path: Path,
) -> None:
    """Guard-then-hash makes redaction an ordinary content transform.

    If redaction ran AFTER hashing, the stored hash would describe the
    un-redacted text while the stored body was redacted: the second ingest
    would hash the ORIGINAL text, match the stored row, short-circuit to skip,
    and freeze the redacted body while reporting it up to date —
    with ``body_changed`` wrong, so neither the vault mirror nor the graph sync
    would ever fire again for that document.
    """
    # --- setup
    source_path = str(tmp_path / "runbook.md")

    # --- exercise
    first = ingest_document(
        test_db,
        embedder=fake_embedder,  # type: ignore[arg-type]
        doc=_secret_doc(source_path=source_path),
        source_kind="manual",
        secret_guard="redact",
    )
    second = ingest_document(
        test_db,
        embedder=fake_embedder,  # type: ignore[arg-type]
        doc=_secret_doc(source_path=source_path),
        source_kind="manual",
        secret_guard="redact",
    )

    # --- verify
    assert first.created is True
    assert second.created is False
    assert second.body_changed is False
    assert second.document_id == first.document_id


def test_stored_hash_describes_the_redacted_body_not_the_original(
    test_db: psycopg.Connection,
    fake_embedder: object,
) -> None:
    """The hash must be SHA-256 of exactly what is stored."""
    import hashlib

    # --- exercise
    result = ingest_document(
        test_db,
        embedder=fake_embedder,  # type: ignore[arg-type]
        doc=_secret_doc(),
        source_kind="manual",
        source_external_id="hash-check",
        secret_guard="redact",
    )

    # --- verify
    assert result.document_id is not None
    row = test_db.execute(
        "SELECT content, content_hash FROM documents WHERE id=%s",
        (result.document_id,),
    ).fetchone()
    assert row is not None
    content, content_hash = row
    assert content_hash == hashlib.sha256(content.encode("utf-8")).hexdigest()


# ---------------------------------------------------------------------------
# Mode behaviour through the pipeline
# ---------------------------------------------------------------------------


def test_warn_mode_stores_the_body_unchanged_and_reports(
    test_db: psycopg.Connection,
    fake_embedder: object,
) -> None:
    """The default mode is lossless — that is the whole argument for it."""
    # --- setup
    doc = _secret_doc()

    # --- exercise
    result = ingest_document(
        test_db,
        embedder=fake_embedder,  # type: ignore[arg-type]
        doc=doc,
        source_kind="manual",
        source_external_id="warn-1",
        secret_guard="warn",
    )

    # --- verify
    assert result.document_id is not None
    assert _stored_content(test_db, result.document_id) == doc.content
    assert [f.kind for f in result.secret_findings] == ["aws_access_key_id"]
    assert "stored UNCHANGED" in result.secret_notice
    assert SYNTHETIC_AWS_KEY not in result.secret_notice


def test_reject_mode_writes_nothing_at_all(
    test_db: psycopg.Connection,
    fake_embedder: object,
) -> None:
    """A refusal must leave no document, no source row, and no chunk behind."""
    # --- exercise / verify
    with pytest.raises(SecretGuardError):
        ingest_document(
            test_db,
            embedder=fake_embedder,  # type: ignore[arg-type]
            doc=_secret_doc(),
            source_kind="manual",
            source_external_id="reject-1",
            secret_guard="reject",
        )

    for table in ("documents", "chunks", "sources"):
        count = test_db.execute(f"SELECT count(*) FROM {table}").fetchone()  # noqa: S608
        assert count is not None
        assert count[0] == 0, f"{table} was written despite the refusal"


def test_clean_content_reports_no_findings_and_no_notice(
    test_db: psycopg.Connection,
    fake_embedder: object,
) -> None:
    # --- exercise
    result = ingest_document(
        test_db,
        embedder=fake_embedder,  # type: ignore[arg-type]
        doc=_secret_doc(content=CLEAN_PROSE),
        source_kind="manual",
        source_external_id="clean-1",
        secret_guard="reject",
    )

    # --- verify
    assert result.created is True
    assert result.secret_findings == ()
    assert result.secret_notice == ""


# ---------------------------------------------------------------------------
# Escape hatches
# ---------------------------------------------------------------------------


def test_allow_secrets_bypasses_reject_but_still_reports(
    test_db: psycopg.Connection,
    fake_embedder: object,
) -> None:
    # --- setup
    doc = _secret_doc()

    # --- exercise
    result = ingest_document(
        test_db,
        embedder=fake_embedder,  # type: ignore[arg-type]
        doc=doc,
        source_kind="manual",
        source_external_id="allow-1",
        secret_guard="reject",
        allow_secrets=True,
    )

    # --- verify
    assert result.document_id is not None
    assert _stored_content(test_db, result.document_id) == doc.content
    assert [f.kind for f in result.secret_findings] == ["aws_access_key_id"]
    assert "guard bypassed" in result.secret_notice


def test_frontmatter_allow_secrets_bypasses_reject(
    test_db: psycopg.Connection,
    fake_embedder: object,
) -> None:
    """``allow_secrets: true`` in a note's YAML lands in documents.metadata.

    For a rotation runbook that legitimately quotes a key format, the opt-out
    belongs with the note, not with every future invocation.
    """
    # --- setup
    doc = _secret_doc(metadata={"allow_secrets": True})

    # --- exercise
    result = ingest_document(
        test_db,
        embedder=fake_embedder,  # type: ignore[arg-type]
        doc=doc,
        source_kind="manual",
        source_external_id="fm-allow-1",
        secret_guard="reject",
    )

    # --- verify
    assert result.document_id is not None
    assert _stored_content(test_db, result.document_id) == doc.content
    assert "guard bypassed" in result.secret_notice


@pytest.mark.parametrize(
    "value", ["true", "yes", 1, "True"], ids=["str-true", "str-yes", "int-1", "str-True"]
)
def test_frontmatter_opt_out_requires_boolean_true(
    test_db: psycopg.Connection,
    fake_embedder: object,
    value: object,
) -> None:
    """A truthy-but-not-True value must NOT disable the guard.

    ``allow_secrets: "false"`` is a truthy string; a typo that silently turns
    the guard off is the worst failure this feature has.
    """
    # --- exercise / verify
    with pytest.raises(SecretGuardError):
        ingest_document(
            test_db,
            embedder=fake_embedder,  # type: ignore[arg-type]
            doc=_secret_doc(metadata={"allow_secrets": value}),
            source_kind="manual",
            source_external_id="fm-bad-1",
            secret_guard="reject",
        )


# ---------------------------------------------------------------------------
# update_document
# ---------------------------------------------------------------------------


def test_update_document_guards_new_content_before_hashing(
    test_db: psycopg.Connection,
    fake_embedder: object,
    seed_doc: object,
) -> None:
    # --- setup
    doc_id = seed_doc(title="Runbook", content="Original body.")  # type: ignore[operator]

    # --- exercise
    result = update_document(
        test_db,
        document_id=doc_id,
        embedder=fake_embedder,  # type: ignore[arg-type]
        new_content=f"Rotated: {SYNTHETIC_AWS_KEY}\n",
        secret_guard="redact",
    )

    # --- verify
    assert "content" in result.fields_changed
    assert SYNTHETIC_AWS_KEY not in _stored_content(test_db, doc_id)
    assert [f.kind for f in result.secret_findings] == ["aws_access_key_id"]


def test_update_document_reject_leaves_the_original_body_intact(
    test_db: psycopg.Connection,
    fake_embedder: object,
    seed_doc: object,
) -> None:
    # --- setup
    doc_id = seed_doc(title="Runbook", content="Original body.")  # type: ignore[operator]

    # --- exercise / verify
    with pytest.raises(SecretGuardError):
        update_document(
            test_db,
            document_id=doc_id,
            embedder=fake_embedder,  # type: ignore[arg-type]
            new_content=f"Rotated: {SYNTHETIC_AWS_KEY}\n",
            secret_guard="reject",
        )

    assert _stored_content(test_db, doc_id) == "Original body."


def test_update_document_without_new_content_reports_no_findings(
    test_db: psycopg.Connection,
    fake_embedder: object,
    seed_doc: object,
) -> None:
    """A title-only edit never re-inspects the stored body."""
    # --- setup
    doc_id = seed_doc(title="Runbook", content=f"Body with {SYNTHETIC_AWS_KEY}\n")  # type: ignore[operator]

    # --- exercise
    result = update_document(
        test_db,
        document_id=doc_id,
        embedder=fake_embedder,  # type: ignore[arg-type]
        new_title="Renamed runbook",
        secret_guard="reject",
    )

    # --- verify
    assert result.fields_changed == ["title"]
    assert result.secret_findings == ()
    assert result.secret_notice == ""
