"""Tests for the editor-mode of `brain edit` (no flags → opens $EDITOR)."""
import os
import stat
import tempfile
from collections.abc import Callable
from pathlib import Path
from typing import Any

import psycopg
import pytest
from typer.testing import CliRunner

from brain.cli import app

TEST_DATABASE_URL = os.environ.get(
    "TEST_DATABASE_URL",
    "postgresql://brain:brain@localhost:5433/second_brain_test",
)


def _make_fake_editor(
    tmp_path: Path,
    *,
    body: str,
    name: str = "fake_editor.sh",
) -> Path:
    """Write an executable shell script that overwrites its argument with `body`.

    The script accepts the file path as $1; tests typically write it to use a
    heredoc to populate the file with a fixed payload.
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
    counting_embedder: Any,
    patch_embedder: Callable[[object], None],
    seed_doc: Callable[..., str],
    tmp_path: Path,
) -> None:
    patch_embedder(counting_embedder)
    doc_id = seed_doc(title="Hello", content="body content here")
    before = _read(doc_id)
    # No-op editor — exits 0 without touching the file.
    editor = _make_fake_editor(tmp_path, body="#!/bin/sh\nexit 0\n")
    monkeypatch.setenv("EDITOR", str(editor))
    monkeypatch.delenv("VISUAL", raising=False)
    result = CliRunner().invoke(app, ["edit", doc_id[:8]])
    assert result.exit_code == 0, result.output
    assert "no changes" in result.output
    assert _read(doc_id) == before
    assert counting_embedder.embed_calls == 0


def test_editor_title_only_change(
    monkeypatch: pytest.MonkeyPatch,
    counting_embedder: Any,
    patch_embedder: Callable[[object], None],
    seed_doc: Callable[..., str],
    tmp_path: Path,
) -> None:
    patch_embedder(counting_embedder)
    doc_id = seed_doc(
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
    assert counting_embedder.embed_calls == 0


def test_editor_body_change_reembeds(
    monkeypatch: pytest.MonkeyPatch,
    counting_embedder: Any,
    patch_embedder: Callable[[object], None],
    seed_doc: Callable[..., str],
    test_db: psycopg.Connection,
    tmp_path: Path,
) -> None:
    patch_embedder(counting_embedder)
    doc_id = seed_doc(
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
    assert counting_embedder.embed_calls >= 1
    # Body is preserved exactly (modulo the trailing \n the heredoc adds —
    # see test_editor_preserves_user_trailing_newlines for that contract).
    assert _read(doc_id)[1].rstrip("\n") == "BRAND NEW BODY ABOUT company-id AND person-a."


def test_editor_preserves_user_trailing_newlines(
    monkeypatch: pytest.MonkeyPatch,
    fake_embedder: Any,
    patch_embedder: Callable[[object], None],
    seed_doc: Callable[..., str],
    tmp_path: Path,
) -> None:
    """Whatever trailing-newline shape the editor produces is what we store —
    the rstrip is for the no-op gate only, not silent normalization."""
    patch_embedder(fake_embedder)
    doc_id = seed_doc(content="seed body")
    body_with_newlines = "new body line\nsecond line\n\n\n"
    new_payload = (
        '{\n'
        '  "content_type": "note",\n'
        '  "metadata": {},\n'
        '  "tags": [],\n'
        '  "title": "Initial Title"\n'
        '}\n'
        '---\n'
        f'{body_with_newlines}'
    )
    # Use printf instead of heredoc so we control trailing-newline precisely.
    # printf does NOT add a final newline; the body literal already includes
    # the three trailing \n we want to assert on.
    script = (
        '#!/bin/sh\n'
        f"printf '%s' '{new_payload}' > \"$1\"\n"
        'exit 0\n'
    )
    editor = _make_fake_editor(tmp_path, body=script)
    monkeypatch.setenv("EDITOR", str(editor))
    monkeypatch.delenv("VISUAL", raising=False)
    result = CliRunner().invoke(app, ["edit", doc_id[:8]])
    assert result.exit_code == 0, result.output
    stored = _read(doc_id)[1]
    assert stored == body_with_newlines


def test_editor_non_zero_exit_aborts(
    monkeypatch: pytest.MonkeyPatch,
    counting_embedder: Any,
    patch_embedder: Callable[[object], None],
    seed_doc: Callable[..., str],
    tmp_path: Path,
) -> None:
    patch_embedder(counting_embedder)
    doc_id = seed_doc(title="Untouched", content="untouched body")
    before = _read(doc_id)
    editor = _make_fake_editor(tmp_path, body="#!/bin/sh\nexit 1\n")
    monkeypatch.setenv("EDITOR", str(editor))
    monkeypatch.delenv("VISUAL", raising=False)
    result = CliRunner().invoke(app, ["edit", doc_id[:8]])
    assert result.exit_code == 1, result.output
    assert "aborted" in result.output.lower()
    assert _read(doc_id) == before
    assert counting_embedder.embed_calls == 0


def test_editor_missing_editor_env_errors(
    monkeypatch: pytest.MonkeyPatch,
    fake_embedder: Any,
    patch_embedder: Callable[[object], None],
    seed_doc: Callable[..., str],
) -> None:
    patch_embedder(fake_embedder)
    monkeypatch.delenv("VISUAL", raising=False)
    monkeypatch.delenv("EDITOR", raising=False)
    monkeypatch.setenv("PATH", "/nonexistent")
    doc_id = seed_doc(title="X", content="x")
    result = CliRunner().invoke(app, ["edit", doc_id[:8]])
    assert result.exit_code == 1, result.output
    assert "editor" in result.output.lower()


def test_editor_malformed_json_header_recovery(
    monkeypatch: pytest.MonkeyPatch,
    counting_embedder: Any,
    patch_embedder: Callable[[object], None],
    seed_doc: Callable[..., str],
    tmp_path: Path,
) -> None:
    """First save has invalid JSON; second save is valid → update applied."""
    patch_embedder(counting_embedder)
    doc_id = seed_doc(
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
    fake_embedder: Any,
    patch_embedder: Callable[[object], None],
    seed_doc: Callable[..., str],
    tmp_path: Path,
) -> None:
    """Two malformed saves → abort with preserved-path message; DB untouched.

    Also asserts the temp file is removed (not orphaned alongside the
    preserved draft) and the preserved file holds the user's last draft.
    """
    patch_embedder(fake_embedder)
    doc_id = seed_doc(title="Stuck", content="stuck body")
    before = _read(doc_id)

    # Spy on make_temp_file so we can assert the temp path is gone afterwards.
    import brain.edit_session as _es

    captured: list[Path] = []
    real = _es.make_temp_file

    def spy(initial: str, *, suffix: str = ".brain.json") -> Path:
        p = real(initial, suffix=suffix)
        captured.append(p)
        return p

    monkeypatch.setattr(_es, "make_temp_file", spy)

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

    preserved = Path(tempfile.gettempdir()) / f"brain-edit-{doc_id[:8]}.json"
    if preserved.exists():
        preserved.unlink()  # clean slate from any prior run

    try:
        result = CliRunner().invoke(app, ["edit", doc_id[:8]])
        assert result.exit_code == 1, result.output
        assert "preserved" in result.output
        assert _read(doc_id) == before

        # Temp file must NOT survive alongside the preserved draft.
        assert captured, "expected make_temp_file to have been called"
        for tp in captured:
            assert not tp.exists(), f"orphaned temp file: {tp}"

        # Preserved draft holds the user's last (still-bad) save.
        assert preserved.exists()
        assert "still not json" in preserved.read_text()
    finally:
        if preserved.exists():
            preserved.unlink()


def test_parse_editor_payload_branches() -> None:
    """Direct unit tests on the header parser cover all rejection branches."""
    from brain.edit_session import parse_payload

    with pytest.raises(ValueError, match="separator"):
        parse_payload('{"title": "x"}')
    with pytest.raises(ValueError, match="invalid JSON"):
        parse_payload("{not json\n---\nbody")
    with pytest.raises(ValueError, match="object"):
        parse_payload('"a string"\n---\nbody')
    with pytest.raises(ValueError, match="metadata"):
        parse_payload('{"metadata": "x"}\n---\nbody')
    with pytest.raises(ValueError, match="tags"):
        parse_payload('{"tags": "nope"}\n---\nbody')


def test_editor_second_attempt_aborts_on_nonzero_exit(
    monkeypatch: pytest.MonkeyPatch,
    fake_embedder: Any,
    patch_embedder: Callable[[object], None],
    seed_doc: Callable[..., str],
    tmp_path: Path,
) -> None:
    """Recovery flow: first save bad, second editor invocation exits non-zero."""
    patch_embedder(fake_embedder)
    doc_id = seed_doc(title="X", content="x")
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
    fake_embedder: Any,
    patch_embedder: Callable[[object], None],
    seed_doc: Callable[..., str],
    tmp_path: Path,
) -> None:
    """Editor change to a body that collides with another doc → exit 1."""
    patch_embedder(fake_embedder)
    # Heredoc adds a trailing newline; seed `other` to match that exact body
    # so the editor-produced payload computes the same hash.
    other = seed_doc(content="ALPHA\n")
    target = seed_doc(title="Target", content="BRAVO")
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
    fake_embedder: Any,
    patch_embedder: Callable[[object], None],
    seed_doc: Callable[..., str],
    tmp_path: Path,
) -> None:
    """Bad tags type triggers the recovery flow; if still bad, abort."""
    patch_embedder(fake_embedder)
    doc_id = seed_doc(title="Tagless", content="x", tags=[])
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
