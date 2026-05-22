"""Tests for the flag-driven mode of `brain edit`."""
import hashlib
import json
import os
from collections.abc import Callable
from typing import Any

import psycopg
import pytest
from typer.testing import CliRunner

from brain.cli import app

TEST_DATABASE_URL = os.environ.get(
    "TEST_DATABASE_URL",
    "postgresql://brain:brain@localhost:5434/second_brain_test",
)


def _row(doc_id: str) -> tuple[str, str, str, dict[str, Any], list[str], str]:
    with psycopg.connect(TEST_DATABASE_URL) as conn:
        row = conn.execute(
            "SELECT title, content, content_type, metadata, tags, content_hash "
            "FROM documents WHERE id=%s",
            (doc_id,),
        ).fetchone()
    assert row is not None
    return row[0], row[1], row[2], dict(row[3] or {}), list(row[4] or []), row[5]


def _content_hash(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def test_edit_title_only_no_embed_call(
    counting_embedder: Any,
    patch_embedder: Callable[[object], None],
    seed_doc: Callable[..., str],
) -> None:
    patch_embedder(counting_embedder)
    doc_id = seed_doc(title="Old Title")
    result = CliRunner().invoke(
        app, ["edit", doc_id[:8], "--title", "Brand New Title"]
    )
    assert result.exit_code == 0, result.output
    assert "title" in result.output
    assert _row(doc_id)[0] == "Brand New Title"
    assert counting_embedder.embed_calls == 0


def test_edit_metadata_merge_keeps_other_keys(
    fake_embedder: Any,
    patch_embedder: Callable[[object], None],
    seed_doc: Callable[..., str],
) -> None:
    patch_embedder(fake_embedder)
    doc_id = seed_doc(metadata={"a": 1, "b": 2})
    result = CliRunner().invoke(
        app, ["edit", doc_id[:8], "--metadata", json.dumps({"b": 3})]
    )
    assert result.exit_code == 0, result.output
    assert _row(doc_id)[3] == {"a": 1, "b": 3}


def test_edit_metadata_replace_swaps_blob(
    fake_embedder: Any,
    patch_embedder: Callable[[object], None],
    seed_doc: Callable[..., str],
) -> None:
    patch_embedder(fake_embedder)
    doc_id = seed_doc(metadata={"a": 1, "b": 2})
    result = CliRunner().invoke(
        app,
        [
            "edit",
            doc_id[:8],
            "--metadata",
            json.dumps({"c": 4}),
            "--replace-metadata",
        ],
    )
    assert result.exit_code == 0, result.output
    assert _row(doc_id)[3] == {"c": 4}


def test_edit_content_rechunks_and_reembeds(
    counting_embedder: Any,
    patch_embedder: Callable[[object], None],
    seed_doc: Callable[..., str],
    tmp_path: Any,
) -> None:
    patch_embedder(counting_embedder)
    doc_id = seed_doc(content="paragraph one body.\n\nparagraph two.")
    _, _, _, _, _, old_hash = _row(doc_id)
    payload = tmp_path / "new.txt"
    payload.write_text("totally fresh content about company-id and person-a.")
    result = CliRunner().invoke(
        app, ["edit", doc_id[:8], "--content-file", str(payload)]
    )
    assert result.exit_code == 0, result.output
    assert "content" in result.output
    _, _, _, _, _, new_hash = _row(doc_id)
    assert new_hash != old_hash
    assert counting_embedder.embed_calls >= 1


def test_edit_content_collision_aborts(
    fake_embedder: Any,
    patch_embedder: Callable[[object], None],
    seed_doc: Callable[..., str],
    tmp_path: Any,
) -> None:
    patch_embedder(fake_embedder)
    a_id = seed_doc(content="alpha body content.")
    b_id = seed_doc(content="bravo body content.")
    _, _, _, _, _, b_hash_before = _row(b_id)
    payload = tmp_path / "clash.txt"
    payload.write_text("alpha body content.")
    result = CliRunner().invoke(
        app, ["edit", b_id[:8], "--content-file", str(payload)]
    )
    assert result.exit_code == 1, result.output
    assert "collides" in result.output
    # neither doc changed
    _, _, _, _, _, a_hash_after = _row(a_id)
    _, _, _, _, _, b_hash_after = _row(b_id)
    assert b_hash_after == b_hash_before
    assert a_hash_after == _content_hash("alpha body content.")


def test_edit_empty_content_rejected(
    fake_embedder: Any,
    patch_embedder: Callable[[object], None],
    seed_doc: Callable[..., str],
    tmp_path: Any,
) -> None:
    patch_embedder(fake_embedder)
    doc_id = seed_doc()
    payload = tmp_path / "empty.txt"
    payload.write_text("")
    result = CliRunner().invoke(
        app, ["edit", doc_id[:8], "--content-file", str(payload)]
    )
    assert result.exit_code == 1, result.output
    assert "empty" in result.output.lower()


def test_edit_no_flags_errors_when_no_editor(
    monkeypatch: pytest.MonkeyPatch,
    fake_embedder: Any,
    patch_embedder: Callable[[object], None],
    seed_doc: Callable[..., str],
) -> None:
    """Editor mode is the no-flag path; with no editor available it must error."""
    patch_embedder(fake_embedder)
    monkeypatch.delenv("VISUAL", raising=False)
    monkeypatch.delenv("EDITOR", raising=False)
    monkeypatch.setenv("PATH", "/nonexistent")
    doc_id = seed_doc()
    result = CliRunner().invoke(app, ["edit", doc_id[:8]])
    assert result.exit_code == 1, result.output
    assert "editor" in result.output.lower()


def test_edit_replace_metadata_alone_errors(
    fake_embedder: Any,
    patch_embedder: Callable[[object], None],
    seed_doc: Callable[..., str],
) -> None:
    patch_embedder(fake_embedder)
    doc_id = seed_doc()
    result = CliRunner().invoke(app, ["edit", doc_id[:8], "--replace-metadata"])
    assert result.exit_code != 0
    assert "--replace-metadata" in result.output


def test_edit_replace_metadata_with_other_flags_but_no_metadata_errors(
    fake_embedder: Any,
    patch_embedder: Callable[[object], None],
    seed_doc: Callable[..., str],
) -> None:
    """`--title X --replace-metadata` (no --metadata) must reject — silently
    dropping the replace intent would mislead the user."""
    patch_embedder(fake_embedder)
    doc_id = seed_doc(title="Old", metadata={"a": 1})
    before = _row(doc_id)
    result = CliRunner().invoke(
        app, ["edit", doc_id[:8], "--title", "New", "--replace-metadata"]
    )
    assert result.exit_code != 0, result.output
    assert "--replace-metadata" in result.output
    # Title must NOT have been applied either — the BadParameter halts
    # before any DB write.
    assert _row(doc_id) == before


def test_edit_invalid_metadata_json_errors(
    fake_embedder: Any,
    patch_embedder: Callable[[object], None],
    seed_doc: Callable[..., str],
) -> None:
    patch_embedder(fake_embedder)
    doc_id = seed_doc()
    before = _row(doc_id)
    result = CliRunner().invoke(
        app, ["edit", doc_id[:8], "--metadata", "{not json"]
    )
    assert result.exit_code == 1, result.output
    assert "valid JSON" in result.output
    assert _row(doc_id) == before


def test_edit_metadata_must_be_object(
    fake_embedder: Any,
    patch_embedder: Callable[[object], None],
    seed_doc: Callable[..., str],
) -> None:
    patch_embedder(fake_embedder)
    doc_id = seed_doc(metadata={"a": 1})
    result = CliRunner().invoke(app, ["edit", doc_id[:8], "--metadata", '"x"'])
    assert result.exit_code == 1, result.output
    assert "object" in result.output
    assert _row(doc_id)[3] == {"a": 1}


def test_edit_ambiguous_prefix_errors(
    test_db: psycopg.Connection,
    fake_embedder: Any,
    patch_embedder: Callable[[object], None],
) -> None:
    patch_embedder(fake_embedder)
    # Force two documents whose IDs share a 6-char prefix. We insert directly
    # to bypass the chunk FK that an UPDATE on documents.id would dangle.
    for new_id, content in (
        ("aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa", "alpha"),
        ("aaaaaabb-bbbb-bbbb-bbbb-bbbbbbbbbbbb", "bravo"),
    ):
        test_db.execute(
            "INSERT INTO documents (id, title, content, content_hash, "
            "content_type) VALUES (%s, %s, %s, %s, %s)",
            (new_id, content, content, content + "_h", "note"),
        )
    result = CliRunner().invoke(app, ["edit", "aaaaaa", "--title", "x"])
    assert result.exit_code != 0
    assert "ambiguous" in result.output


def test_edit_title_updates_fts(
    test_db: psycopg.Connection,
    fake_embedder: Any,
    patch_embedder: Callable[[object], None],
    seed_doc: Callable[..., str],
) -> None:
    patch_embedder(fake_embedder)
    doc_id = seed_doc(title="Original Title", content="some body content.")
    # Confirm the original title is searchable by FTS.
    pre = test_db.execute(
        "SELECT count(*) FROM documents "
        "WHERE tsv @@ plainto_tsquery('english', %s) AND id=%s",
        ("Original", doc_id),
    ).fetchone()[0]
    assert pre == 1
    result = CliRunner().invoke(
        app, ["edit", doc_id[:8], "--title", "Renamed Document"]
    )
    assert result.exit_code == 0, result.output
    # Old title no longer matches the FTS index for this row.
    after_old = test_db.execute(
        "SELECT count(*) FROM documents "
        "WHERE tsv @@ plainto_tsquery('english', %s) AND id=%s",
        ("Original", doc_id),
    ).fetchone()[0]
    assert after_old == 0
    # New title matches.
    after_new = test_db.execute(
        "SELECT count(*) FROM documents "
        "WHERE tsv @@ plainto_tsquery('english', %s) AND id=%s",
        ("Renamed", doc_id),
    ).fetchone()[0]
    assert after_new == 1


def test_edit_no_op_reports_no_changes(
    fake_embedder: Any,
    patch_embedder: Callable[[object], None],
    seed_doc: Callable[..., str],
) -> None:
    """Edit with same title is treated as a successful no-op."""
    patch_embedder(fake_embedder)
    doc_id = seed_doc(title="Same Title")
    result = CliRunner().invoke(
        app, ["edit", doc_id[:8], "--title", "Same Title"]
    )
    assert result.exit_code == 0, result.output
    assert "no changes" in result.output


def test_edit_content_file_and_stdin_mutually_exclusive(
    fake_embedder: Any,
    patch_embedder: Callable[[object], None],
    seed_doc: Callable[..., str],
    tmp_path: Any,
) -> None:
    patch_embedder(fake_embedder)
    doc_id = seed_doc()
    payload = tmp_path / "x.txt"
    payload.write_text("body")
    result = CliRunner().invoke(
        app,
        [
            "edit",
            doc_id[:8],
            "--content-file",
            str(payload),
            "--content-stdin",
        ],
        input="from stdin",
    )
    assert result.exit_code != 0
    assert "mutually exclusive" in result.output


def test_edit_content_stdin(
    counting_embedder: Any,
    patch_embedder: Callable[[object], None],
    seed_doc: Callable[..., str],
) -> None:
    patch_embedder(counting_embedder)
    doc_id = seed_doc(content="old body of text.")
    _, _, _, _, _, old_hash = _row(doc_id)
    result = CliRunner().invoke(
        app,
        ["edit", doc_id[:8], "--content-stdin"],
        input="brand new body via stdin.",
    )
    assert result.exit_code == 0, result.output
    _, _, _, _, _, new_hash = _row(doc_id)
    assert new_hash != old_hash


# ---------------------------------------------------------------------------
# Codex finding 1 follow-up — `brain edit` end-to-end summary refresh.
# ---------------------------------------------------------------------------


def test_edit_content_file_refreshes_documents_summary(
    test_db: psycopg.Connection,
    fake_embedder: Any,
    patch_embedder: Callable[[object], None],
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Any,
) -> None:
    """End-to-end regression for the Codex finding 1 user-visible bug.

    Smoke-equivalent of the manual ``brain edit --content-file`` flow.
    Wires a fake :class:`OllamaEnricher` via the public seam
    (``brain.cli._build_enricher``) so the CLI's lazy enricher build
    returns the fake instead of trying to reach Ollama. The fake
    returns a v1 summary on the first call (during initial ingest) and
    a v2 summary on the second (during the body-changing edit).

    Before the wave's Q2 follow-up fix:
    - ``brain edit`` invoked ``update_document(...)`` WITHOUT passing
      ``enricher=``, so ``_enrich_post_ingest_hook`` hit the
      "no enricher supplied" skip and ``documents.summary`` stayed at v1
      even though the body refreshed to v2.

    After the fix:
    - The CLI lazily builds the (fake) enricher when ``new_content`` is
      not None and threads it into ``update_document``. The hook now
      sees a real enricher + ``body_changed=True`` and refreshes the
      summary in the same transaction.
    """
    from dataclasses import dataclass

    from brain.enrichment import SummaryResult
    from brain.ingest import ExtractedDoc, ingest_document

    @dataclass
    class _ScriptedEnricher:
        """Returns a different summary on each ``summarize`` call.

        NOT module-level monkey-patching — injected via the
        ``_build_enricher`` seam. The hook reads ``.model`` directly
        for the D11 model-fingerprint check; ``calls`` lets the test
        assert exactly how many round-trips happened.
        """

        model: str = "fake-test-model"
        summaries: tuple[str, ...] = (
            "v1 summary about the original body.",
            "v2 summary about the new body.",
        )
        calls: int = 0

        def count_tokens(self, text: str) -> int:
            return max(1, len(text) // 4)

        def summarize(self, title: str, content: str) -> SummaryResult:
            idx = self.calls
            self.calls += 1
            return SummaryResult(
                summary=self.summaries[idx], model=self.model
            )

    patch_embedder(fake_embedder)
    fake_enricher = _ScriptedEnricher()
    monkeypatch.setattr(
        "brain.cli._build_enricher", lambda cfg: fake_enricher
    )

    # Seed via direct ingest_document so the test owns the enricher
    # call surface end-to-end. ``enricher=fake_enricher`` populates the
    # initial summary in the same transaction as the INSERT.
    body_v1 = "Original body content for the Q2 edit-refresh test. " * 20
    result = ingest_document(
        test_db,
        embedder=fake_embedder,
        doc=ExtractedDoc(
            title="Q2 edit refresh fixture",
            content=body_v1,
            content_type="note",
            source_path=None,
            metadata={},
        ),
        source_kind="manual",
        enricher=fake_enricher,  # type: ignore[arg-type]
    )
    doc_id = result.document_id
    assert doc_id is not None

    summary_before_row = test_db.execute(
        "SELECT summary FROM documents WHERE id=%s", (doc_id,)
    ).fetchone()
    assert summary_before_row is not None
    assert summary_before_row[0] == "v1 summary about the original body."
    assert fake_enricher.calls == 1

    # Body-changing edit via the CLI.
    body_v2 = "Completely rewritten body for the v2 summary check. " * 20
    payload = tmp_path / "v2-body.txt"
    payload.write_text(body_v2)
    cli = CliRunner().invoke(
        app, ["edit", doc_id[:8], "--content-file", str(payload)]
    )
    assert cli.exit_code == 0, cli.output

    summary_after_row = test_db.execute(
        "SELECT summary, summary_model FROM documents WHERE id=%s", (doc_id,)
    ).fetchone()
    assert summary_after_row is not None
    summary_after, model_after = summary_after_row
    assert summary_after == "v2 summary about the new body.", (
        "`brain edit --content-file` must refresh documents.summary; the "
        "Codex finding 1 fix wires the enricher into update_document so "
        "_enrich_post_ingest_hook fires with body_changed=True"
    )
    assert model_after == "fake-test-model"
    assert fake_enricher.calls == 2, (
        "the hook must fire exactly once on the edit — once for the "
        "initial ingest plus once for the body change"
    )


def test_edit_title_only_does_not_invoke_enricher(
    test_db: psycopg.Connection,
    fake_embedder: Any,
    patch_embedder: Callable[[object], None],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Lazy-build contract: title-only edits must NOT probe Ollama.

    The fix wires the enricher only when the body actually changes —
    a title-only / metadata-only edit should never construct the
    enricher (which probes Ollama at construction time in production).
    Captures the lazy-build guarantee at the seam.
    """
    from brain.ingest import ExtractedDoc, ingest_document

    build_calls = {"count": 0}

    def _spy_build_enricher(cfg: object) -> object:
        build_calls["count"] += 1
        raise AssertionError(
            "title-only edit must NOT build the enricher — "
            "Ollama probe would block on a sluggish server"
        )

    patch_embedder(fake_embedder)
    monkeypatch.setattr("brain.cli._build_enricher", _spy_build_enricher)

    result = ingest_document(
        test_db,
        embedder=fake_embedder,
        doc=ExtractedDoc(
            title="Title-only edit fixture",
            content="Body content that stays put across the title edit.",
            content_type="note",
            source_path=None,
            metadata={},
        ),
        source_kind="manual",
    )
    doc_id = result.document_id
    assert doc_id is not None

    cli = CliRunner().invoke(
        app, ["edit", doc_id[:8], "--title", "Edited title only"]
    )
    assert cli.exit_code == 0, cli.output
    assert build_calls["count"] == 0
