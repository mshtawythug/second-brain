"""MCP ``brain_show`` withholds confidential bodies (F6, task #17).

The boundary is at *egress*, not at existence: a confidential document still
returns its title, tags, id, source and summary, so a model can see that it
exists and ask the user. Only the body is withheld.

Be honest about the threat model this addresses. A cooperating LLM can pass
``include_confidential=true``; the flag is a **speed bump and an audit
signal**, not authentication. What it actually prevents is a confidential body
being pulled into context incidentally — by a search that happened to rank it
first and an automatic open — which is the realistic failure mode.

The byte-identical test is the load-bearing one: both new keys must appear
ONLY on the withheld path, or every existing consumer of a normal document's
payload sees a shape change.

All fixture data is synthetic.
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

TEST_DATABASE_URL = os.environ.get(
    "TEST_DATABASE_URL",
    "postgresql://brain:brain@localhost:5434/second_brain_test",
)

_BODY = "Compensation bands and the reorg plan for the platform group."


@pytest.fixture
def vault_dir(tmp_path: Path) -> Path:
    vault = tmp_path / "vault"
    vault_module.init_vault(vault)
    return vault


@pytest.fixture
def mcp_state(
    monkeypatch: pytest.MonkeyPatch,
    test_db: psycopg.Connection,  # noqa: ARG001 — keeps the schema fresh
    fake_embedder: object,
    vault_dir: Path,
) -> Iterator[mcp_server._State]:
    state = mcp_server._State(
        cfg=Config(database_url=TEST_DATABASE_URL, vault_path=vault_dir),
        embedder=fake_embedder,  # type: ignore[arg-type]
    )
    monkeypatch.setattr(mcp_server, "_state", state)
    yield state


def _seed(
    conn: psycopg.Connection[Any],
    *,
    title: str,
    content_hash: str,
    sensitivity: str = "normal",
    summary: str | None = None,
) -> str:
    row = conn.execute(
        "INSERT INTO documents "
        "(title, content, content_type, kind, content_hash, sensitivity, summary) "
        "VALUES (%s, %s, 'note', 'vault', %s, %s, %s) RETURNING id::text",
        (title, _BODY, content_hash, sensitivity, summary),
    ).fetchone()
    assert row is not None
    return str(row[0])


# ---------------------------------------------------------------------------
# The additive-shape contract
# ---------------------------------------------------------------------------


def test_normal_doc_payload_is_byte_identical(
    test_db: psycopg.Connection[Any], mcp_state: mcp_server._State
) -> None:
    """Required by the F6 handoff. Both new keys are withheld-path only."""
    doc_id = _seed(test_db, title="Team Notes", content_hash="w4-sens-1")

    payload = mcp_server.brain_show(id_prefix=doc_id)

    assert payload["content"] == _BODY
    assert "sensitivity" not in payload, (
        "a normal document's payload must not grow a key — existing consumers "
        "parse this shape"
    )
    assert "withheld" not in payload


def test_normal_doc_key_set_is_exactly_the_pre_f6_set(
    test_db: psycopg.Connection[Any], mcp_state: mcp_server._State
) -> None:
    doc_id = _seed(test_db, title="Team Notes", content_hash="w4-sens-2")

    payload = mcp_server.brain_show(id_prefix=doc_id)

    assert set(payload) == {
        "id",
        "title",
        "content",
        "content_type",
        "tags",
        "source_path",
        "ingested_at",
        "source_kind",
    }


# ---------------------------------------------------------------------------
# Withholding
# ---------------------------------------------------------------------------


def test_confidential_body_is_withheld_by_default(
    test_db: psycopg.Connection[Any], mcp_state: mcp_server._State
) -> None:
    doc_id = _seed(
        test_db,
        title="Comp Review",
        content_hash="w4-sens-3",
        sensitivity="confidential",
    )

    payload = mcp_server.brain_show(id_prefix=doc_id)

    assert payload["content"] is None
    assert payload["sensitivity"] == "confidential"
    assert "include_confidential=true" in payload["withheld"]
    assert _BODY not in str(payload), "the body must not leak via any other key"


def test_structural_metadata_still_returns_so_the_model_can_ask(
    test_db: psycopg.Connection[Any], mcp_state: mcp_server._State
) -> None:
    """Withholding the body, not the document's existence.

    Title / id / content_type are user-authored or structural, so they return
    — that is what lets a model say "there is a document called X, ask the
    user for it" instead of being blind to it.
    """
    doc_id = _seed(
        test_db,
        title="Comp Review",
        content_hash="w4-sens-4",
        sensitivity="confidential",
        summary="A synthetic summary line.",
    )

    payload = mcp_server.brain_show(id_prefix=doc_id)

    assert payload["title"] == "Comp Review"
    assert payload["id"] == doc_id
    assert payload["content_type"] == "note"


def test_the_llm_summary_is_withheld_with_the_body(
    test_db: psycopg.Connection[Any], mcp_state: mcp_server._State
) -> None:
    """The summary is body-derived, so returning it would leak the body.

    ``documents.summary`` is generated by an LLM *from the content*. Handing
    it back on a withheld document returns the body's substance in condensed
    form — a leak wearing the shape of a compromise. This deliberately
    departs from the F6 handoff's "summary still returns".
    """
    doc_id = _seed(
        test_db,
        title="Comp Review",
        content_hash="w4-sens-8",
        sensitivity="confidential",
        summary="Bands are 180-220k for staff engineers.",
    )

    payload = mcp_server.brain_show(id_prefix=doc_id)

    assert "summary" not in payload
    assert "180-220k" not in str(payload), "no body-derived text may survive"
    assert "summary" in payload["withheld"], "say what was withheld"


def test_include_confidential_returns_the_summary_too(
    test_db: psycopg.Connection[Any], mcp_state: mcp_server._State
) -> None:
    """The opt-in restores everything, not just the body."""
    doc_id = _seed(
        test_db,
        title="Comp Review",
        content_hash="w4-sens-9",
        sensitivity="confidential",
        summary="Bands are 180-220k for staff engineers.",
    )

    payload = mcp_server.brain_show(id_prefix=doc_id, include_confidential=True)

    assert payload["summary"] == "Bands are 180-220k for staff engineers."
    assert payload["content"] == _BODY


def test_normal_doc_summary_is_unaffected(
    test_db: psycopg.Connection[Any], mcp_state: mcp_server._State
) -> None:
    """The withholding must not touch the normal path's summary key."""
    doc_id = _seed(
        test_db,
        title="Team Notes",
        content_hash="w4-sens-10",
        summary="A synthetic summary line.",
    )

    payload = mcp_server.brain_show(id_prefix=doc_id)

    assert payload["summary"] == "A synthetic summary line."


def test_include_confidential_returns_the_body(
    test_db: psycopg.Connection[Any], mcp_state: mcp_server._State
) -> None:
    doc_id = _seed(
        test_db,
        title="Comp Review",
        content_hash="w4-sens-5",
        sensitivity="confidential",
    )

    payload = mcp_server.brain_show(id_prefix=doc_id, include_confidential=True)

    assert payload["content"] == _BODY
    assert "withheld" not in payload


def test_include_confidential_defaults_to_false() -> None:
    """A tool that defaults open is not a boundary."""
    import inspect

    default = inspect.signature(mcp_server.brain_show).parameters[
        "include_confidential"
    ].default

    assert default is False


def test_a_withheld_open_is_still_logged(
    test_db: psycopg.Connection[Any], mcp_state: mcp_server._State
) -> None:
    """Dropping the log would blind usage analytics to the most notable opens."""
    doc_id = _seed(
        test_db,
        title="Comp Review",
        content_hash="w4-sens-6",
        sensitivity="confidential",
    )

    mcp_server.brain_show(id_prefix=doc_id, originating_query="comp bands")

    row = test_db.execute(
        "SELECT action, source FROM interactions WHERE document_id = %s",
        (doc_id,),
    ).fetchone()
    assert row == ("opened", "mcp")


def test_agent_id_is_recorded_on_the_open(
    test_db: psycopg.Connection[Any], mcp_state: mcp_server._State
) -> None:
    """F10 attribution reaches ``brain_show``."""
    doc_id = _seed(test_db, title="Team Notes", content_hash="w4-sens-7")

    mcp_server.brain_show(
        id_prefix=doc_id,
        originating_query="team notes",
        agent_id="research-agent",
    )

    row = test_db.execute(
        "SELECT agent_id FROM interactions WHERE document_id = %s", (doc_id,)
    ).fetchone()
    assert row is not None
    assert row[0] == "research-agent"
