"""Tests for the flag-driven mode of `brain edit`."""
import json
import os
from typing import Any

import psycopg
import pytest
from typer.testing import CliRunner

from brain.cli import app
from brain.ingest import ExtractedDoc, ingest_document

TEST_DATABASE_URL = os.environ.get(
    "TEST_DATABASE_URL",
    "postgresql://brain:brain@localhost:5433/second_brain_test",
)


class CountingEmbedder:
    """Wraps the fake embedder so tests can assert on Voyage call counts."""

    def __init__(self, inner: Any) -> None:
        self._inner = inner
        self.embed_calls = 0

    def embed(self, texts: list[str], *, input_type: str = "document") -> list[list[float]]:
        self.embed_calls += 1
        return self._inner.embed(texts, input_type=input_type)

    def count_tokens(self, text: str) -> int:
        return self._inner.count_tokens(text)


def _patch_embedder(monkeypatch: pytest.MonkeyPatch, embedder: object) -> None:
    monkeypatch.setenv("DATABASE_URL", TEST_DATABASE_URL)
    monkeypatch.setenv("VOYAGE_API_KEY", "fake")
    monkeypatch.setattr("brain.cli._build_embedder", lambda cfg: embedder)


def _seed(
    test_db: psycopg.Connection,
    fake_embedder: Any,
    *,
    title: str = "Original Title",
    content: str = "Hello world. This is the body.",
    content_type: str = "note",
    metadata: dict[str, Any] | None = None,
    tags: list[str] | None = None,
) -> str:
    res = ingest_document(
        test_db,
        embedder=fake_embedder,
        doc=ExtractedDoc(
            title=title,
            content=content,
            content_type=content_type,
            source_path=None,
            metadata=metadata or {},
        ),
        source_kind="manual",
        tags=tags or [],
    )
    assert res.document_id is not None
    return res.document_id


def _row(doc_id: str) -> tuple[str, str, str, dict[str, Any], list[str], str]:
    with psycopg.connect(TEST_DATABASE_URL) as conn:
        row = conn.execute(
            "SELECT title, content, content_type, metadata, tags, content_hash "
            "FROM documents WHERE id=%s",
            (doc_id,),
        ).fetchone()
    assert row is not None
    return row[0], row[1], row[2], dict(row[3] or {}), list(row[4] or []), row[5]


def test_edit_title_only_no_voyage_call(
    monkeypatch: pytest.MonkeyPatch,
    test_db: psycopg.Connection,
    fake_embedder: Any,
) -> None:
    counter = CountingEmbedder(fake_embedder)
    _patch_embedder(monkeypatch, counter)
    doc_id = _seed(test_db, fake_embedder, title="Old Title")
    result = CliRunner().invoke(
        app, ["edit", doc_id[:8], "--title", "Brand New Title"]
    )
    assert result.exit_code == 0, result.output
    assert "title" in result.output
    assert _row(doc_id)[0] == "Brand New Title"
    assert counter.embed_calls == 0


def test_edit_metadata_merge_keeps_other_keys(
    monkeypatch: pytest.MonkeyPatch,
    test_db: psycopg.Connection,
    fake_embedder: Any,
) -> None:
    _patch_embedder(monkeypatch, fake_embedder)
    doc_id = _seed(test_db, fake_embedder, metadata={"a": 1, "b": 2})
    result = CliRunner().invoke(
        app, ["edit", doc_id[:8], "--metadata", json.dumps({"b": 3})]
    )
    assert result.exit_code == 0, result.output
    assert _row(doc_id)[3] == {"a": 1, "b": 3}


def test_edit_metadata_replace_swaps_blob(
    monkeypatch: pytest.MonkeyPatch,
    test_db: psycopg.Connection,
    fake_embedder: Any,
) -> None:
    _patch_embedder(monkeypatch, fake_embedder)
    doc_id = _seed(test_db, fake_embedder, metadata={"a": 1, "b": 2})
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
    monkeypatch: pytest.MonkeyPatch,
    test_db: psycopg.Connection,
    fake_embedder: Any,
    tmp_path: Any,
) -> None:
    counter = CountingEmbedder(fake_embedder)
    _patch_embedder(monkeypatch, counter)
    doc_id = _seed(
        test_db, fake_embedder, content="paragraph one body.\n\nparagraph two."
    )
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
    assert counter.embed_calls >= 1


def test_edit_content_collision_aborts(
    monkeypatch: pytest.MonkeyPatch,
    test_db: psycopg.Connection,
    fake_embedder: Any,
    tmp_path: Any,
) -> None:
    counter = CountingEmbedder(fake_embedder)
    _patch_embedder(monkeypatch, counter)
    a_id = _seed(test_db, fake_embedder, content="alpha body content.")
    b_id = _seed(test_db, fake_embedder, content="bravo body content.")
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
    monkeypatch: pytest.MonkeyPatch,
    test_db: psycopg.Connection,
    fake_embedder: Any,
    tmp_path: Any,
) -> None:
    _patch_embedder(monkeypatch, fake_embedder)
    doc_id = _seed(test_db, fake_embedder)
    payload = tmp_path / "empty.txt"
    payload.write_text("")
    result = CliRunner().invoke(
        app, ["edit", doc_id[:8], "--content-file", str(payload)]
    )
    assert result.exit_code == 1, result.output
    assert "empty" in result.output.lower()


def test_edit_no_flags_errors_when_no_editor(
    monkeypatch: pytest.MonkeyPatch,
    test_db: psycopg.Connection,
    fake_embedder: Any,
) -> None:
    """Editor mode is the no-flag path; with no editor available it must error."""
    _patch_embedder(monkeypatch, fake_embedder)
    monkeypatch.delenv("VISUAL", raising=False)
    monkeypatch.delenv("EDITOR", raising=False)
    monkeypatch.setenv("PATH", "/nonexistent")
    doc_id = _seed(test_db, fake_embedder)
    result = CliRunner().invoke(app, ["edit", doc_id[:8]])
    assert result.exit_code == 1, result.output
    assert "editor" in result.output.lower()


def test_edit_replace_metadata_alone_errors(
    monkeypatch: pytest.MonkeyPatch,
    test_db: psycopg.Connection,
    fake_embedder: Any,
) -> None:
    _patch_embedder(monkeypatch, fake_embedder)
    doc_id = _seed(test_db, fake_embedder)
    result = CliRunner().invoke(
        app, ["edit", doc_id[:8], "--replace-metadata"]
    )
    assert result.exit_code != 0
    assert "--replace-metadata" in result.output


def test_edit_invalid_metadata_json_errors(
    monkeypatch: pytest.MonkeyPatch,
    test_db: psycopg.Connection,
    fake_embedder: Any,
) -> None:
    _patch_embedder(monkeypatch, fake_embedder)
    doc_id = _seed(test_db, fake_embedder)
    before = _row(doc_id)
    result = CliRunner().invoke(
        app, ["edit", doc_id[:8], "--metadata", "{not json"]
    )
    assert result.exit_code == 1, result.output
    assert "valid JSON" in result.output
    assert _row(doc_id) == before


def test_edit_metadata_must_be_object(
    monkeypatch: pytest.MonkeyPatch,
    test_db: psycopg.Connection,
    fake_embedder: Any,
) -> None:
    _patch_embedder(monkeypatch, fake_embedder)
    doc_id = _seed(test_db, fake_embedder, metadata={"a": 1})
    result = CliRunner().invoke(
        app, ["edit", doc_id[:8], "--metadata", '"x"']
    )
    assert result.exit_code == 1, result.output
    assert "object" in result.output
    assert _row(doc_id)[3] == {"a": 1}


def test_edit_ambiguous_prefix_errors(
    monkeypatch: pytest.MonkeyPatch,
    test_db: psycopg.Connection,
    fake_embedder: Any,
) -> None:
    _patch_embedder(monkeypatch, fake_embedder)
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
    result = CliRunner().invoke(
        app, ["edit", "aaaaaa", "--title", "x"]
    )
    assert result.exit_code != 0
    assert "ambiguous" in result.output


def test_edit_title_updates_fts(
    monkeypatch: pytest.MonkeyPatch,
    test_db: psycopg.Connection,
    fake_embedder: Any,
) -> None:
    _patch_embedder(monkeypatch, fake_embedder)
    doc_id = _seed(
        test_db, fake_embedder, title="Original Title", content="some body content."
    )
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
    monkeypatch: pytest.MonkeyPatch,
    test_db: psycopg.Connection,
    fake_embedder: Any,
) -> None:
    """Edit with same title is treated as a successful no-op."""
    _patch_embedder(monkeypatch, fake_embedder)
    doc_id = _seed(test_db, fake_embedder, title="Same Title")
    result = CliRunner().invoke(
        app, ["edit", doc_id[:8], "--title", "Same Title"]
    )
    assert result.exit_code == 0, result.output
    assert "no changes" in result.output


def test_edit_content_file_and_stdin_mutually_exclusive(
    monkeypatch: pytest.MonkeyPatch,
    test_db: psycopg.Connection,
    fake_embedder: Any,
    tmp_path: Any,
) -> None:
    _patch_embedder(monkeypatch, fake_embedder)
    doc_id = _seed(test_db, fake_embedder)
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
    monkeypatch: pytest.MonkeyPatch,
    test_db: psycopg.Connection,
    fake_embedder: Any,
) -> None:
    counter = CountingEmbedder(fake_embedder)
    _patch_embedder(monkeypatch, counter)
    doc_id = _seed(test_db, fake_embedder, content="old body of text.")
    _, _, _, _, _, old_hash = _row(doc_id)
    result = CliRunner().invoke(
        app, ["edit", doc_id[:8], "--content-stdin"], input="brand new body via stdin."
    )
    assert result.exit_code == 0, result.output
    _, _, _, _, _, new_hash = _row(doc_id)
    assert new_hash != old_hash
    assert counter.embed_calls >= 1


def _content_hash(text: str) -> str:
    import hashlib

    return hashlib.sha256(text.encode("utf-8")).hexdigest()
