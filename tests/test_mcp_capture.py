"""Tests for the ``brain_capture`` MCP tool (Plan 09 Phase 3).

Each test installs a fresh ``_State`` (real test-DB Config sandboxed to a
``tmp_path`` vault + fake embedder) and calls the tool function directly,
mirroring ``tests/test_mcp_server.py``. ``enricher``/``graph_syncer`` are left
``None`` so the suite never depends on a live Ollama. All content is synthetic.

Captures now author vault-tier notes via ``create_vault_note`` (no
``ingest_document``). Regression assertions:

  * Invisibility bug: ``kind='vault'``, ``vault_path`` starts ``capture/``.
  * Hang bug: ``brain_capture`` never accesses ``state.graph_syncer``.
  * No dedup: two captures of identical text create two distinct notes.
"""
from __future__ import annotations

import os
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import psycopg
import pytest

from brain import mcp_server
from brain import vault as vault_module
from brain.config import Config
from brain.mcp_compat import MCPError

TEST_DATABASE_URL = os.environ.get(
    "TEST_DATABASE_URL",
    "postgresql://brain:brain@localhost:5434/second_brain_test",
)


@pytest.fixture
def capture_state(
    monkeypatch: pytest.MonkeyPatch,
    test_db: psycopg.Connection,  # noqa: ARG001 — keeps the schema fresh
    fake_embedder: object,
    tmp_path: Path,
) -> Iterator[mcp_server._State]:
    """Install a server state pointing at the test DB + an initialized vault.

    ``vault_path`` is forced to ``tmp_path`` (initialized via ``init_vault``
    so ``_templates/note.md`` is present for ``create_vault_note``).
    ``enricher``/``graph_syncer`` stay ``None`` (no Ollama / graph round-trip).
    """
    vault_module.init_vault(tmp_path)
    state = mcp_server._State(
        cfg=Config(database_url=TEST_DATABASE_URL, vault_path=tmp_path),
        embedder=fake_embedder,  # type: ignore[arg-type]
    )
    monkeypatch.setattr(mcp_server, "_state", state)
    yield state


def _doc_tags(doc_id: str) -> list[str]:
    """Read the current tag list for a document directly from Postgres."""
    with psycopg.connect(TEST_DATABASE_URL) as conn:
        row = conn.execute(
            "SELECT tags FROM documents WHERE id=%s", (doc_id,)
        ).fetchone()
    assert row is not None
    return list(row[0] or [])


def _doc_title(doc_id: str) -> str:
    with psycopg.connect(TEST_DATABASE_URL) as conn:
        row = conn.execute(
            "SELECT title FROM documents WHERE id=%s", (doc_id,)
        ).fetchone()
    assert row is not None
    return str(row[0])


def _doc_kind_and_vault_path(doc_id: str) -> tuple[str, str | None]:
    with psycopg.connect(TEST_DATABASE_URL) as conn:
        row = conn.execute(
            "SELECT kind, vault_path FROM documents WHERE id=%s", (doc_id,)
        ).fetchone()
    assert row is not None
    return str(row[0]), row[1]


def test_brain_capture_creates_inbox_document(
    capture_state: mcp_server._State,  # noqa: ARG001 — fixture installs state
) -> None:
    """A non-empty capture creates a document tagged ``inbox`` → status ingested."""
    payload = mcp_server.brain_capture(
        content="a quick thought about project-ko follow-ups",
    )
    assert payload["status"] == "ingested"
    assert payload["document_id"] is not None
    assert _doc_tags(payload["document_id"]) == ["inbox"]


def test_brain_capture_writes_vault_tier_document(
    capture_state: mcp_server._State,
) -> None:
    """Regression (invisibility bug): capture writes kind='vault' under capture/.

    Before the fix captures wrote kind='ingested' under _ingested/manual/,
    hidden behind the Quartz "Show ingested" toggle.
    """
    payload = mcp_server.brain_capture(
        content="vault tier regression check via mcp",
    )
    doc_id = payload["document_id"]
    assert doc_id is not None

    kind, vault_path_rel = _doc_kind_and_vault_path(doc_id)
    assert kind == "vault", f"expected kind='vault', got {kind!r}"
    assert vault_path_rel is not None, "vault_path should be set"
    assert vault_path_rel.startswith("capture/"), (
        f"expected path under capture/, got {vault_path_rel!r}"
    )
    # File must exist on disk under the tmp vault.
    assert (capture_state.cfg.vault_path / vault_path_rel).exists()


def test_brain_capture_does_not_use_graph_syncer(
    capture_state: mcp_server._State,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Regression (hang bug): brain_capture never touches state.graph_syncer.

    Before the fix ingest_document called graph_syncer.reconcile() which
    serialized against the brain-mcp server's long-held graph transaction.
    """

    class _Boom:
        """Explodes on any attribute access — proves the syncer is never touched."""

        def __getattr__(self, name: str) -> Any:
            raise AssertionError(
                f"brain_capture must NOT access graph_syncer.{name}"
            )

    monkeypatch.setattr(capture_state, "graph_syncer", _Boom())

    # Must succeed without triggering the _Boom sentinel.
    payload = mcp_server.brain_capture(content="a thought that must not touch graph")
    assert payload["status"] == "ingested"


def test_brain_capture_merges_extra_tags_with_inbox(
    capture_state: mcp_server._State,  # noqa: ARG001
) -> None:
    """Caller tags are normalized + unioned with the always-on ``inbox`` tag."""
    payload = mcp_server.brain_capture(
        content="capture body with extra routing tags",
        tags=["Interview", "inbox"],  # mixed case + explicit dup of inbox
    )
    doc_id = payload["document_id"]
    assert doc_id is not None
    assert sorted(_doc_tags(doc_id)) == ["inbox", "interview"]


def test_brain_capture_two_identical_captures_create_distinct_notes(
    capture_state: mcp_server._State,  # noqa: ARG001
) -> None:
    """Re-capturing identical content creates two distinct vault notes (no dedup).

    The old ingest_document path deduped by content_hash; create_vault_note
    does not — every capture is a fresh note.
    """
    body = "a repeated capture body for the no-dedup path"
    first = mcp_server.brain_capture(content=body)
    second = mcp_server.brain_capture(content=body)
    assert first["status"] == "ingested"
    assert second["status"] == "ingested"
    assert first["document_id"] != second["document_id"], (
        "two captures of identical text must produce distinct document IDs"
    )
    # Both carry inbox exactly once.
    assert _doc_tags(first["document_id"]) == ["inbox"]
    assert _doc_tags(second["document_id"]) == ["inbox"]


@pytest.mark.parametrize("empty", ["", "   ", "\n\n", "\t  \n"])
def test_brain_capture_empty_content_errors(
    empty: str,
    capture_state: mcp_server._State,  # noqa: ARG001
) -> None:
    """Empty / whitespace-only content is rejected as an MCP INVALID_PARAMS error."""
    with pytest.raises(MCPError) as exc_info:
        mcp_server.brain_capture(content=empty)
    assert "content is empty" in exc_info.value.error.message


def test_brain_capture_auto_titles_when_title_blank(
    capture_state: mcp_server._State,  # noqa: ARG001
) -> None:
    """A blank title falls back to the deterministic date-stamped auto-title."""
    payload = mcp_server.brain_capture(
        title="   ",
        content="remember to draft the q3 planning note",
    )
    doc_id = payload["document_id"]
    assert doc_id is not None
    title = _doc_title(doc_id)
    # Shape: ``<iso date>-capture-<slug>`` (see brain.capture.make_capture_title).
    assert "-capture-" in title
    assert title.endswith("remember-to-draft-the-q3-planning")


def test_brain_capture_uses_explicit_title_verbatim(
    capture_state: mcp_server._State,  # noqa: ARG001
) -> None:
    """A non-blank title is used as-is (stripped), not auto-generated."""
    payload = mcp_server.brain_capture(
        title="My Deliberate Title",
        content="body under an explicit title",
    )
    doc_id = payload["document_id"]
    assert doc_id is not None
    assert _doc_title(doc_id) == "My Deliberate Title"


def test_brain_capture_writes_vault_file(
    capture_state: mcp_server._State,
) -> None:
    """The capture materializes a ``capture/`` vault note under the vault."""
    payload = mcp_server.brain_capture(
        content="mirror this capture via the MCP tool",
    )
    assert payload["status"] == "ingested"
    capture_dir = capture_state.cfg.vault_path / "capture"
    assert capture_dir.is_dir(), f"missing capture dir: {capture_dir}"
    # Exclude the README.md written by init_vault; only capture notes remain.
    captures = [f for f in capture_dir.glob("*.md") if f.name != "README.md"]
    assert len(captures) == 1, f"expected one capture note file, got {captures}"
    assert "mirror this capture via the MCP tool" in captures[0].read_text(
        encoding="utf-8"
    )


def test_brain_capture_raises_mcp_error_when_vault_path_none(
    monkeypatch: pytest.MonkeyPatch,
    test_db: psycopg.Connection,  # noqa: ARG001 — keeps schema fresh
    fake_embedder: object,
    tmp_path: Path,
) -> None:
    """With vault_path=None on the cfg, brain_capture raises MCPError INVALID_PARAMS.

    Constructs a _State directly with vault_path=None bypassing Config.load()
    (which always resolves a default path).
    """
    import dataclasses

    from brain.config import Config as _Cfg

    no_vault_cfg = dataclasses.replace(
        _Cfg(database_url=TEST_DATABASE_URL),
        vault_path=None,  # type: ignore[arg-type]
    )
    state = mcp_server._State(
        cfg=no_vault_cfg,  # type: ignore[arg-type]
        embedder=fake_embedder,  # type: ignore[arg-type]
    )
    monkeypatch.setattr(mcp_server, "_state", state)

    with pytest.raises(MCPError) as exc_info:
        mcp_server.brain_capture(content="this should never reach the db")
    assert "vault path is not configured" in exc_info.value.error.message


def test_brain_capture_raises_mcp_error_on_vault_sync_error(
    monkeypatch: pytest.MonkeyPatch,
    test_db: psycopg.Connection,  # noqa: ARG001 — keeps schema fresh
    fake_embedder: object,
    tmp_path: Path,
) -> None:
    """A VaultNoteSyncError from create_vault_note surfaces as MCPError INVALID_PARAMS.

    Simulated by providing a vault without _templates/note.md (init_vault
    intentionally skipped) so create_vault_note raises VaultNoteSyncError.
    """
    from brain.config import Config as _Cfg

    # Vault dir exists but _templates/note.md is absent — triggers VaultNoteSyncError.
    (tmp_path / "capture").mkdir(parents=True, exist_ok=True)

    state = mcp_server._State(
        cfg=_Cfg(database_url=TEST_DATABASE_URL, vault_path=tmp_path),
        embedder=fake_embedder,  # type: ignore[arg-type]
    )
    monkeypatch.setattr(mcp_server, "_state", state)

    with pytest.raises(MCPError) as exc_info:
        mcp_server.brain_capture(content="a sync error probe via mcp")
    assert "vault note sync" in exc_info.value.error.message.lower()


class _BoomConnect:
    """Stub mimicking ``connect()``: entering the context raises immediately."""

    def __init__(self, exc: BaseException | None = None) -> None:
        self._exc = exc or psycopg.OperationalError("simulated outage")

    def __call__(self, _url: str) -> _BoomConnect:
        return self

    def __enter__(self) -> Any:
        raise self._exc

    def __exit__(self, *_: object) -> None:
        return None


def test_brain_capture_wraps_db_error(
    monkeypatch: pytest.MonkeyPatch,
    capture_state: mcp_server._State,  # noqa: ARG001
) -> None:
    """A Postgres failure surfaces as MCPError, never a raw psycopg.Error."""
    monkeypatch.setattr(mcp_server, "connect", _BoomConnect())
    with pytest.raises(MCPError) as exc_info:
        mcp_server.brain_capture(content="content that never reaches the db")
    assert "database error" in exc_info.value.error.message


def test_brain_capture_strips_whitespace_from_explicit_title(
    capture_state: mcp_server._State,  # noqa: ARG001
) -> None:
    """F2: an explicit ``title`` with surrounding whitespace is stored trimmed.

    ``brain_capture(title="  Padded Title  ", ...)`` must store ``"Padded Title"``
    (not the padded string) in ``documents.title``.
    """
    payload = mcp_server.brain_capture(
        title="  Padded Title  ",
        content="some capture body under a padded title",
    )
    doc_id = payload["document_id"]
    assert doc_id is not None
    assert _doc_title(doc_id) == "Padded Title"
