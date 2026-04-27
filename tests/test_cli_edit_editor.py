"""Tests for the editor-mode of `brain edit` (no flags → opens $EDITOR)."""
import os
import stat
from pathlib import Path
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
    """Wrap the fake embedder so tests can assert whether Voyage was called."""

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
    title: str = "Initial Title",
    content: str = "Initial body content.",
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


def _make_fake_editor(
    tmp_path: Path,
    *,
    body: str,
    name: str = "fake_editor.sh",
) -> Path:
    """Write an executable shell script that overwrites its argument with `body`.

    Use ``EXIT_<n>`` lines to override the script exit code per call. The script
    accepts the file path as $1 and writes to it via cat-from-heredoc.
    """
    script = tmp_path / name
    script.write_text(body)
    script.chmod(script.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    return script


def _read(doc_id: str) -> tuple[str, str, str, dict[str, Any], list[str]]:
    with psycopg.connect(TEST_DATABASE_URL) as conn:
        row = conn.execute(
            "SELECT title, content, content_type, metadata, tags "
            "FROM documents WHERE id=%s",
            (doc_id,),
        ).fetchone()
    assert row is not None
    return row[0], row[1], row[2], dict(row[3] or {}), list(row[4] or [])


def test_editor_no_change_is_noop(
    monkeypatch: pytest.MonkeyPatch,
    test_db: psycopg.Connection,
    fake_embedder: Any,
    tmp_path: Path,
) -> None:
    counter = CountingEmbedder(fake_embedder)
    _patch_embedder(monkeypatch, counter)
    doc_id = _seed(test_db, fake_embedder, title="Hello", content="body content here")
    before = _read(doc_id)
    # No-op editor — exits 0 without touching the file.
    editor = _make_fake_editor(tmp_path, body="#!/bin/sh\nexit 0\n")
    monkeypatch.setenv("EDITOR", str(editor))
    monkeypatch.delenv("VISUAL", raising=False)
    result = CliRunner().invoke(app, ["edit", doc_id[:8]])
    assert result.exit_code == 0, result.output
    assert "(no changes)" in result.output
    assert _read(doc_id) == before
    assert counter.embed_calls == 0


def test_editor_title_only_change(
    monkeypatch: pytest.MonkeyPatch,
    test_db: psycopg.Connection,
    fake_embedder: Any,
    tmp_path: Path,
) -> None:
    counter = CountingEmbedder(fake_embedder)
    _patch_embedder(monkeypatch, counter)
    doc_id = _seed(
        test_db,
        fake_embedder,
        title="Old Title",
        content="some body lines.",
        metadata={"a": 1},
        tags=["one"],
    )
    new_payload = (
        '{\n'
        '  "content_type": "note",\n'
        '  "metadata": {"a": 1},\n'
        '  "tags": ["one"],\n'
        '  "title": "Renamed Doc"\n'
        '}\n'
        '---\n'
        'some body lines.'
    )
    editor = _make_fake_editor(
        tmp_path,
        body=f"#!/bin/sh\ncat > \"$1\" <<'BRAIN_EOF'\n{new_payload}\nBRAIN_EOF\nexit 0\n",
    )
    monkeypatch.setenv("EDITOR", str(editor))
    monkeypatch.delenv("VISUAL", raising=False)
    result = CliRunner().invoke(app, ["edit", doc_id[:8]])
    assert result.exit_code == 0, result.output
    assert _read(doc_id)[0] == "Renamed Doc"
    assert counter.embed_calls == 0


def test_editor_body_change_reembeds(
    monkeypatch: pytest.MonkeyPatch,
    test_db: psycopg.Connection,
    fake_embedder: Any,
    tmp_path: Path,
) -> None:
    counter = CountingEmbedder(fake_embedder)
    _patch_embedder(monkeypatch, counter)
    doc_id = _seed(
        test_db,
        fake_embedder,
        title="Same Title",
        content="OLD BODY OF TEXT.",
        metadata={},
        tags=[],
    )
    old_chunks = test_db.execute(
        "SELECT id FROM chunks WHERE document_id=%s", (doc_id,)
    ).fetchall()
    assert old_chunks
    new_payload = (
        '{\n'
        '  "content_type": "note",\n'
        '  "metadata": {},\n'
        '  "tags": [],\n'
        '  "title": "Same Title"\n'
        '}\n'
        '---\n'
        'BRAND NEW BODY ABOUT company-id AND person-a.'
    )
    editor = _make_fake_editor(
        tmp_path,
        body=f"#!/bin/sh\ncat > \"$1\" <<'BRAIN_EOF'\n{new_payload}\nBRAIN_EOF\nexit 0\n",
    )
    monkeypatch.setenv("EDITOR", str(editor))
    monkeypatch.delenv("VISUAL", raising=False)
    result = CliRunner().invoke(app, ["edit", doc_id[:8]])
    assert result.exit_code == 0, result.output
    assert "content" in result.output
    new_chunks = {
        r[0]
        for r in test_db.execute(
            "SELECT id FROM chunks WHERE document_id=%s", (doc_id,)
        ).fetchall()
    }
    assert new_chunks and new_chunks.isdisjoint({c[0] for c in old_chunks})
    assert counter.embed_calls >= 1
    assert _read(doc_id)[1] == "BRAND NEW BODY ABOUT company-id AND person-a."


def test_editor_non_zero_exit_aborts(
    monkeypatch: pytest.MonkeyPatch,
    test_db: psycopg.Connection,
    fake_embedder: Any,
    tmp_path: Path,
) -> None:
    counter = CountingEmbedder(fake_embedder)
    _patch_embedder(monkeypatch, counter)
    doc_id = _seed(test_db, fake_embedder, title="Untouched", content="untouched body")
    before = _read(doc_id)
    editor = _make_fake_editor(tmp_path, body="#!/bin/sh\nexit 1\n")
    monkeypatch.setenv("EDITOR", str(editor))
    monkeypatch.delenv("VISUAL", raising=False)
    result = CliRunner().invoke(app, ["edit", doc_id[:8]])
    assert result.exit_code == 1, result.output
    assert "aborted" in result.output.lower()
    assert _read(doc_id) == before
    assert counter.embed_calls == 0


def test_editor_missing_editor_env_errors(
    monkeypatch: pytest.MonkeyPatch,
    test_db: psycopg.Connection,
    fake_embedder: Any,
) -> None:
    _patch_embedder(monkeypatch, fake_embedder)
    monkeypatch.delenv("VISUAL", raising=False)
    monkeypatch.delenv("EDITOR", raising=False)
    monkeypatch.setenv("PATH", "/nonexistent")
    doc_id = _seed(test_db, fake_embedder, title="X", content="x")
    result = CliRunner().invoke(app, ["edit", doc_id[:8]])
    assert result.exit_code == 1, result.output
    assert "editor" in result.output.lower()


def test_editor_malformed_json_header_recovery(
    monkeypatch: pytest.MonkeyPatch,
    test_db: psycopg.Connection,
    fake_embedder: Any,
    tmp_path: Path,
) -> None:
    """First save has invalid JSON; second save is valid → update applied."""
    counter = CountingEmbedder(fake_embedder)
    _patch_embedder(monkeypatch, counter)
    doc_id = _seed(
        test_db,
        fake_embedder,
        title="Recoverable",
        content="recovery body content.",
        metadata={},
        tags=[],
    )
    state = tmp_path / "state.txt"
    state.write_text("0")  # call counter
    bad_payload = '{ this is not json\n---\nrecovery body content.'
    good_payload = (
        '{\n'
        '  "content_type": "note",\n'
        '  "metadata": {},\n'
        '  "tags": [],\n'
        '  "title": "Recovered Title"\n'
        '}\n'
        '---\n'
        'recovery body content.'
    )
    # The script writes bad payload on first call, good payload on second.
    script = (
        "#!/bin/sh\n"
        f"STATE=\"{state}\"\n"
        "n=$(cat \"$STATE\")\n"
        "if [ \"$n\" = \"0\" ]; then\n"
        "  cat > \"$1\" <<'BRAIN_EOF'\n"
        f"{bad_payload}\n"
        "BRAIN_EOF\n"
        "  echo 1 > \"$STATE\"\n"
        "else\n"
        "  cat > \"$1\" <<'BRAIN_EOF'\n"
        f"{good_payload}\n"
        "BRAIN_EOF\n"
        "fi\n"
        "exit 0\n"
    )
    editor = _make_fake_editor(tmp_path, body=script)
    monkeypatch.setenv("EDITOR", str(editor))
    monkeypatch.delenv("VISUAL", raising=False)
    result = CliRunner().invoke(app, ["edit", doc_id[:8]])
    assert result.exit_code == 0, result.output
    assert _read(doc_id)[0] == "Recovered Title"


def test_editor_repeatedly_malformed_preserves_draft(
    monkeypatch: pytest.MonkeyPatch,
    test_db: psycopg.Connection,
    fake_embedder: Any,
    tmp_path: Path,
) -> None:
    """Two malformed saves → abort with preserved-path message; DB untouched."""
    _patch_embedder(monkeypatch, fake_embedder)
    doc_id = _seed(test_db, fake_embedder, title="Stuck", content="stuck body")
    before = _read(doc_id)
    body = (
        "#!/bin/sh\n"
        "cat > \"$1\" <<'BRAIN_EOF'\n"
        "still not json\n"
        "---\n"
        "stuck body\n"
        "BRAIN_EOF\n"
        "exit 0\n"
    )
    editor = _make_fake_editor(tmp_path, body=body)
    monkeypatch.setenv("EDITOR", str(editor))
    monkeypatch.delenv("VISUAL", raising=False)
    result = CliRunner().invoke(app, ["edit", doc_id[:8]])
    assert result.exit_code == 1, result.output
    assert "preserved" in result.output
    assert _read(doc_id) == before


def test_parse_editor_payload_branches() -> None:
    """Direct unit tests on the header parser cover all rejection branches."""
    from brain.cli import _parse_editor_payload

    with pytest.raises(ValueError, match="separator"):
        _parse_editor_payload('{"title": "x"}')
    with pytest.raises(ValueError, match="invalid JSON"):
        _parse_editor_payload("{not json\n---\nbody")
    with pytest.raises(ValueError, match="object"):
        _parse_editor_payload('"a string"\n---\nbody')
    with pytest.raises(ValueError, match="metadata"):
        _parse_editor_payload('{"metadata": "x"}\n---\nbody')
    with pytest.raises(ValueError, match="tags"):
        _parse_editor_payload('{"tags": "nope"}\n---\nbody')


def test_editor_second_attempt_aborts_on_nonzero_exit(
    monkeypatch: pytest.MonkeyPatch,
    test_db: psycopg.Connection,
    fake_embedder: Any,
    tmp_path: Path,
) -> None:
    """Recovery flow: first save bad, second editor invocation exits non-zero."""
    _patch_embedder(monkeypatch, fake_embedder)
    doc_id = _seed(test_db, fake_embedder, title="X", content="x")
    before = _read(doc_id)
    state = tmp_path / "n.txt"
    state.write_text("0")
    bad_payload = "{ bad json\n---\nx"
    script = (
        "#!/bin/sh\n"
        f"STATE=\"{state}\"\n"
        "n=$(cat \"$STATE\")\n"
        "if [ \"$n\" = \"0\" ]; then\n"
        "  cat > \"$1\" <<'BRAIN_EOF'\n"
        f"{bad_payload}\n"
        "BRAIN_EOF\n"
        "  echo 1 > \"$STATE\"\n"
        "  exit 0\n"
        "fi\n"
        "exit 1\n"
    )
    editor = _make_fake_editor(tmp_path, body=script)
    monkeypatch.setenv("EDITOR", str(editor))
    monkeypatch.delenv("VISUAL", raising=False)
    result = CliRunner().invoke(app, ["edit", doc_id[:8]])
    assert result.exit_code == 1, result.output
    assert "aborted" in result.output.lower()
    assert _read(doc_id) == before


def test_editor_body_collision_aborts(
    monkeypatch: pytest.MonkeyPatch,
    test_db: psycopg.Connection,
    fake_embedder: Any,
    tmp_path: Path,
) -> None:
    """Editor change to a body that collides with another doc → exit 1."""
    _patch_embedder(monkeypatch, fake_embedder)
    other = _seed(test_db, fake_embedder, content="ALPHA")
    target = _seed(test_db, fake_embedder, title="Target", content="BRAVO")
    _ = other  # silence unused-var warnings
    new_payload = (
        '{\n'
        '  "content_type": "note",\n'
        '  "metadata": {},\n'
        '  "tags": [],\n'
        '  "title": "Target"\n'
        '}\n'
        '---\n'
        'ALPHA'
    )
    editor = _make_fake_editor(
        tmp_path,
        body=f"#!/bin/sh\ncat > \"$1\" <<'BRAIN_EOF'\n{new_payload}\nBRAIN_EOF\nexit 0\n",
    )
    monkeypatch.setenv("EDITOR", str(editor))
    monkeypatch.delenv("VISUAL", raising=False)
    result = CliRunner().invoke(app, ["edit", target[:8]])
    assert result.exit_code == 1, result.output
    assert "collides" in result.output.lower()


def test_editor_tags_must_be_list_of_strings(
    monkeypatch: pytest.MonkeyPatch,
    test_db: psycopg.Connection,
    fake_embedder: Any,
    tmp_path: Path,
) -> None:
    """Bad tags type triggers the recovery flow; if still bad, abort."""
    _patch_embedder(monkeypatch, fake_embedder)
    doc_id = _seed(test_db, fake_embedder, title="Tagless", content="x", tags=[])
    bad_payload = (
        '{\n'
        '  "content_type": "note",\n'
        '  "metadata": {},\n'
        '  "tags": "not-a-list",\n'
        '  "title": "Tagless"\n'
        '}\n'
        '---\n'
        'x'
    )
    editor = _make_fake_editor(
        tmp_path,
        body=f"#!/bin/sh\ncat > \"$1\" <<'BRAIN_EOF'\n{bad_payload}\nBRAIN_EOF\nexit 0\n",
    )
    monkeypatch.setenv("EDITOR", str(editor))
    monkeypatch.delenv("VISUAL", raising=False)
    result = CliRunner().invoke(app, ["edit", doc_id[:8]])
    assert result.exit_code == 1, result.output
    assert "tags" in result.output.lower()
