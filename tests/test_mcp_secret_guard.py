"""The ingest-time secret guard reaches the MCP write paths (F4, task #8).

`ingest_document` / `update_document` default `secret_guard` to the *module*
constant, so a call that omits the argument silently ignores the operator's
`BRAIN_SECRET_GUARD` setting. That is the failure this closes: someone who
sets `reject` had it enforced on the CLI and **not** over MCP — on precisely
the path Krisp transcripts, Slack threads and Gmail arrive through, which is
where credentials actually turn up.

The guard is only meaningful if a `reject` writes nothing, so that is asserted
against the DB rather than inferred from the exception.

Credentials here are the canonical synthetic `AKIAIOSFODNN7EXAMPLE` form — a
documented non-resolving example key, never a real one.
"""
from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path
from typing import Any

import psycopg
import pytest
from mcp import McpError

from brain import mcp_server
from brain import vault as vault_module
from brain.config import Config
from tests.conftest import TEST_DATABASE_URL

#: AWS's own documented example key. Matches the guard's pattern; resolves to
#: nothing.
SYNTHETIC_KEY = "AKIAIOSFODNN7EXAMPLE"


@pytest.fixture
def vault_dir(tmp_path: Path) -> Path:
    vault = tmp_path / "vault"
    vault_module.init_vault(vault)
    return vault


def _install(
    monkeypatch: pytest.MonkeyPatch,
    fake_embedder: object,
    vault_dir: Path,
    mode: str,
) -> mcp_server._State:
    state = mcp_server._State(
        cfg=Config(
            database_url=TEST_DATABASE_URL,
            vault_path=vault_dir,
            secret_guard=mode,
        ),
        embedder=fake_embedder,  # type: ignore[arg-type]
    )
    monkeypatch.setattr(mcp_server, "_state", state)
    return state


@pytest.fixture
def warn_mode(
    monkeypatch: pytest.MonkeyPatch,
    test_db: psycopg.Connection,  # noqa: ARG001 — keeps the schema fresh
    fake_embedder: object,
    vault_dir: Path,
) -> Iterator[mcp_server._State]:
    yield _install(monkeypatch, fake_embedder, vault_dir, "warn")


@pytest.fixture
def reject_mode(
    monkeypatch: pytest.MonkeyPatch,
    test_db: psycopg.Connection,  # noqa: ARG001 — keeps the schema fresh
    fake_embedder: object,
    vault_dir: Path,
) -> Iterator[mcp_server._State]:
    yield _install(monkeypatch, fake_embedder, vault_dir, "reject")


def _count(conn: psycopg.Connection[Any], title: str) -> int:
    row = conn.execute(
        "SELECT count(*) FROM documents WHERE title = %s", (title,)
    ).fetchone()
    assert row is not None
    return int(row[0])


# ---------------------------------------------------------------------------
# brain_ingest_stdin
# ---------------------------------------------------------------------------


def test_warn_mode_stores_the_document_and_reports_the_finding(
    test_db: psycopg.Connection[Any], warn_mode: mcp_server._State
) -> None:
    """`warn` must not block the write — it informs."""
    payload = mcp_server.brain_ingest_stdin(
        source="slack",
        external_id="w5-guard-1",
        title="Leaky Thread",
        content=f"deploy key {SYNTHETIC_KEY} please rotate",
    )

    assert payload["document_id"] is not None
    assert "secret_notice" in payload, "the caller must be told what was found"
    assert _count(test_db, "Leaky Thread") == 1


def test_reject_mode_refuses_and_writes_nothing(
    test_db: psycopg.Connection[Any], reject_mode: mcp_server._State
) -> None:
    """The DB assertion is the point — an exception alone proves nothing."""
    with pytest.raises(McpError, match="secret guard"):
        mcp_server.brain_ingest_stdin(
            source="slack",
            external_id="w5-guard-2",
            title="Refused Thread",
            content=f"deploy key {SYNTHETIC_KEY} please rotate",
        )

    assert _count(test_db, "Refused Thread") == 0


def test_clean_content_payload_is_unchanged(
    test_db: psycopg.Connection[Any], warn_mode: mcp_server._State
) -> None:
    """`secret_notice` is additive: absent when there is nothing to report.

    Existing MCP callers parse this payload, so a document with no findings
    must return exactly the pre-F4 key set.
    """
    payload = mcp_server.brain_ingest_stdin(
        source="slack",
        external_id="w5-guard-3",
        title="Clean Thread",
        content="nothing sensitive here at all, just ordinary notes",
    )

    assert set(payload) == {"document_id", "created"}


def test_reject_mode_still_accepts_clean_content(
    test_db: psycopg.Connection[Any], reject_mode: mcp_server._State
) -> None:
    """The guard must refuse findings, not refuse ingest."""
    payload = mcp_server.brain_ingest_stdin(
        source="krisp",
        external_id="w5-guard-4",
        title="Clean Standup",
        content="standup notes about the migration runway",
    )

    assert payload["document_id"] is not None
    assert _count(test_db, "Clean Standup") == 1


def test_the_configured_mode_is_what_reaches_the_pipeline(
    test_db: psycopg.Connection[Any],
    monkeypatch: pytest.MonkeyPatch,
    fake_embedder: object,
    vault_dir: Path,
) -> None:
    """The actual regression: identical content, opposite outcomes by config.

    If the tool ignored `cfg.secret_guard` and used the module default, both
    calls below would behave the same — which is exactly how this shipped
    unguarded.
    """
    _install(monkeypatch, fake_embedder, vault_dir, "warn")
    mcp_server.brain_ingest_stdin(
        source="slack",
        external_id="w5-guard-5a",
        title="Mode Probe Warn",
        content=f"key {SYNTHETIC_KEY} here",
    )

    _install(monkeypatch, fake_embedder, vault_dir, "reject")
    with pytest.raises(McpError):
        mcp_server.brain_ingest_stdin(
            source="slack",
            external_id="w5-guard-5b",
            title="Mode Probe Reject",
            content=f"key {SYNTHETIC_KEY} here",
        )

    assert _count(test_db, "Mode Probe Warn") == 1
    assert _count(test_db, "Mode Probe Reject") == 0


# ---------------------------------------------------------------------------
# brain_edit — a body replacement can introduce a credential too
# ---------------------------------------------------------------------------


def test_edit_refuses_a_body_that_introduces_a_credential(
    test_db: psycopg.Connection[Any],
    monkeypatch: pytest.MonkeyPatch,
    fake_embedder: object,
    vault_dir: Path,
) -> None:
    """Ingest is not the only write path into the corpus."""
    _install(monkeypatch, fake_embedder, vault_dir, "warn")
    created = mcp_server.brain_ingest_stdin(
        source="slack",
        external_id="w5-guard-6",
        title="Editable Thread",
        content="originally clean content about the runway",
    )
    doc_id = created["document_id"]

    _install(monkeypatch, fake_embedder, vault_dir, "reject")
    with pytest.raises(McpError, match="secret guard"):
        mcp_server.brain_edit(
            id_prefix=doc_id, content=f"now contains {SYNTHETIC_KEY} oops"
        )

    row = test_db.execute(
        "SELECT content FROM documents WHERE id = %s", (doc_id,)
    ).fetchone()
    assert row is not None
    assert SYNTHETIC_KEY not in row[0], "the refused body must not persist"
    assert "originally clean" in row[0]


def test_edit_allows_a_clean_body(
    test_db: psycopg.Connection[Any],
    monkeypatch: pytest.MonkeyPatch,
    fake_embedder: object,
    vault_dir: Path,
) -> None:
    _install(monkeypatch, fake_embedder, vault_dir, "reject")
    created = mcp_server.brain_ingest_stdin(
        source="slack",
        external_id="w5-guard-7",
        title="Editable Clean Thread",
        content="originally clean content",
    )

    mcp_server.brain_edit(
        id_prefix=created["document_id"], content="still perfectly clean content"
    )

    row = test_db.execute(
        "SELECT content FROM documents WHERE id = %s", (created["document_id"],)
    ).fetchone()
    assert row is not None
    assert "still perfectly clean" in row[0]
