"""Wave 3 — ``brain_show`` payload ceiling (``summary_only`` / ``max_content_tokens``).

The hole this closes: ``brain_show`` returned ``doc.content`` verbatim with no
bound at all. The largest live document MEASURED at **67,410 tokens** (266,888
chars, re-measured read-only on prod 2026-08-13) — a third of a 200k context
window spent on one tool call the agent could not size in advance.

Two rules the tests below exist to pin:

1. **No silent cut.** Every reduction is announced (``content_truncated`` +
   ``content_tokens``, or ``content_omitted``, or ``summary_unavailable``) and
   the payload always still carries ``id``, so the recovery path is reachable.
2. **The normal path is byte-identical.** Every new key appears ONLY on the
   path that produces it — the same additive discipline
   ``test_normal_doc_payload_is_byte_identical`` pins for F6.

Ordering also matters: F6 confidential withholding runs LAST and wins over
both new parameters.

All fixture data is synthetic.
"""
from __future__ import annotations

import os
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import psycopg
import pytest
from mcp.types import INVALID_PARAMS

from brain import mcp_server
from brain.config import Config
from brain.mcp_compat import MCPError
from brain.mcp_limits import CONTENT_MARKERS

TEST_DATABASE_URL = os.environ.get(
    "TEST_DATABASE_URL",
    "postgresql://brain:brain@localhost:5434/second_brain_test",
)

# ``fake_embedder.count_tokens`` is len//4, so this body costs ~2,000 tokens.
_LONG_BODY = "Quarterly planning notes for the platform group. " * 167
_SHORT_BODY = "A short synthetic note body."
_SUMMARY = "Synthetic one-line summary of the planning notes."


@pytest.fixture
def mcp_state(
    monkeypatch: pytest.MonkeyPatch,
    test_db: psycopg.Connection,  # noqa: ARG001 — keeps the schema fresh
    fake_embedder: object,
    tmp_path: Path,
) -> Iterator[mcp_server._State]:
    state = mcp_server._State(
        cfg=Config(database_url=TEST_DATABASE_URL, vault_path=tmp_path),
        embedder=fake_embedder,  # type: ignore[arg-type]
    )
    monkeypatch.setattr(mcp_server, "_state", state)
    yield state


def _seed(
    conn: psycopg.Connection[Any],
    *,
    title: str,
    content_hash: str,
    content: str = _SHORT_BODY,
    summary: str | None = None,
    sensitivity: str = "normal",
) -> str:
    row = conn.execute(
        "INSERT INTO documents "
        "(title, content, content_type, kind, content_hash, sensitivity, summary) "
        "VALUES (%s, %s, 'note', 'vault', %s, %s, %s) RETURNING id::text",
        (title, content, content_hash, sensitivity, summary),
    ).fetchone()
    assert row is not None
    return str(row[0])


def _with_ceiling(state: mcp_server._State, tokens: int) -> None:
    """Point the state's Config at a specific ``show_max_content_tokens``.

    A dataclass replace on the fixture's own Config — production code is never
    reopened (CLAUDE.md rule 13).
    """
    import dataclasses

    state.cfg = dataclasses.replace(state.cfg, show_max_content_tokens=tokens)


# ---------------------------------------------------------------------------
# The backward-compatibility guarantee
# ---------------------------------------------------------------------------


def test_show_below_ceiling_payload_is_byte_identical(
    test_db: psycopg.Connection[Any], mcp_state: mcp_server._State
) -> None:
    """A normal, small document's payload must not grow a single key.

    Mirrors ``test_normal_doc_payload_is_byte_identical`` (F6). If this fails,
    every existing consumer of ``brain_show`` sees a shape change.
    """
    doc_id = _seed(test_db, title="Team Notes", content_hash="w3-show-1")

    payload = mcp_server.brain_show(id_prefix=doc_id)

    assert payload["content"] == _SHORT_BODY
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
# summary_only
# ---------------------------------------------------------------------------


def test_show_summary_only_omits_content(
    test_db: psycopg.Connection[Any], mcp_state: mcp_server._State
) -> None:
    doc_id = _seed(
        test_db,
        title="Planning",
        content_hash="w3-show-2",
        content=_LONG_BODY,
        summary=_SUMMARY,
    )

    payload = mcp_server.brain_show(id_prefix=doc_id, summary_only=True)

    assert payload["content"] is None
    assert payload["summary"] == _SUMMARY
    # The marker must name the recovery path, not just report a fact.
    assert "summary_only=false" in payload["content_omitted"]
    assert payload["id"] == doc_id


def test_show_summary_only_falls_back_when_summary_is_null(
    test_db: psycopg.Connection[Any], mcp_state: mcp_server._State
) -> None:
    """The ~7% NULL-summary tail.

    ``summary_only=True`` on a document with no summary would otherwise return
    a payload with no content AND no summary — an empty answer to a
    well-formed request. It degrades to the ceiling-bounded body instead, and
    says so.
    """
    doc_id = _seed(test_db, title="No Summary", content_hash="w3-show-3")

    payload = mcp_server.brain_show(id_prefix=doc_id, summary_only=True)

    assert payload["content"] == _SHORT_BODY
    assert payload["summary_unavailable"] is True
    assert "content_omitted" not in payload


# ---------------------------------------------------------------------------
# max_content_tokens / the configured ceiling
# ---------------------------------------------------------------------------


def test_show_truncates_above_max_content_tokens_and_marks_it(
    test_db: psycopg.Connection[Any], mcp_state: mcp_server._State
) -> None:
    """The ceiling bites, and the cut is visible.

    Mutation for this guard: raise ``show_max_content_tokens`` to
    ``sys.maxsize`` (or delete the ``apply_content_ceiling`` call in
    ``brain_show``) and this test goes red on the ``content_truncated``
    assertion.
    """
    doc_id = _seed(
        test_db, title="Long", content_hash="w3-show-4", content=_LONG_BODY
    )
    _with_ceiling(mcp_state, 50)

    payload = mcp_server.brain_show(id_prefix=doc_id)

    assert payload["content_truncated"] is True
    assert payload["content_tokens"] <= 50
    # Task 3.3: the marker must NAME the recovery path, not just announce the
    # cut. An agent that is told "truncated" and nothing else has to guess.
    recovery = payload["content_truncated_recovery"]
    assert "summary_only=true" in recovery, (
        "the marker must name a concrete next call, not describe the problem"
    )
    assert "brain show" in recovery, "and the path to the whole body"
    assert len(payload["content"]) < len(_LONG_BODY)
    assert payload["content"] == _LONG_BODY[: len(payload["content"])], (
        "the cut must be a prefix, not a re-rendering"
    )


def test_show_explicit_max_cannot_raise_the_configured_ceiling(
    test_db: psycopg.Connection[Any], mcp_state: mcp_server._State
) -> None:
    """A caller may LOWER the ceiling, never raise it."""
    doc_id = _seed(
        test_db, title="Long", content_hash="w3-show-5", content=_LONG_BODY
    )
    _with_ceiling(mcp_state, 100)

    # Lowering is fine.
    lowered = mcp_server.brain_show(id_prefix=doc_id, max_content_tokens=10)
    assert lowered["content_tokens"] <= 10

    # Raising is rejected, with the ceiling named.
    with pytest.raises(MCPError) as excinfo:
        mcp_server.brain_show(id_prefix=doc_id, max_content_tokens=101)
    assert excinfo.value.error.code == INVALID_PARAMS
    assert "100" in excinfo.value.error.message


def test_show_rejects_zero_max_content_tokens(
    test_db: psycopg.Connection[Any], mcp_state: mcp_server._State
) -> None:
    """``0`` is the OPERATOR's opt-out (env var), never the caller's.

    ``check_ceiling`` treats a ceiling of 0 as unbounded, so a caller-supplied
    0 would be an escape hatch from the very ceiling it is subject to.
    """
    doc_id = _seed(test_db, title="Long", content_hash="w3-show-6")
    with pytest.raises(MCPError) as excinfo:
        mcp_server.brain_show(id_prefix=doc_id, max_content_tokens=0)
    assert excinfo.value.error.code == INVALID_PARAMS


def test_show_ceiling_of_zero_disables_the_cap(
    test_db: psycopg.Connection[Any], mcp_state: mcp_server._State
) -> None:
    """``BRAIN_SHOW_MAX_CONTENT_TOKENS=0`` restores the pre-Wave-3 behaviour.

    This is the documented rollback path, so it needs a test of its own —
    otherwise "set the knob to 0" is a claim, not a guarantee.
    """
    doc_id = _seed(
        test_db, title="Long", content_hash="w3-show-7", content=_LONG_BODY
    )
    _with_ceiling(mcp_state, 0)

    payload = mcp_server.brain_show(id_prefix=doc_id)

    assert payload["content"] == _LONG_BODY
    assert "content_truncated" not in payload


# ---------------------------------------------------------------------------
# Ordering: F6 confidentiality wins
# ---------------------------------------------------------------------------


def test_confidential_withhold_wins_over_summary_only(
    test_db: psycopg.Connection[Any], mcp_state: mcp_server._State
) -> None:
    """A confidential document + ``summary_only=True`` returns NEITHER.

    And no Wave-3 marker may survive onto the withheld payload: a
    ``content_omitted`` saying "re-call with summary_only=false for the body"
    would be a false promise on a document whose body is withheld regardless.
    """
    doc_id = _seed(
        test_db,
        title="Comp Bands",
        content_hash="w3-show-8",
        content=_LONG_BODY,
        summary=_SUMMARY,
        sensitivity="confidential",
    )

    payload = mcp_server.brain_show(id_prefix=doc_id, summary_only=True)

    assert payload["content"] is None
    assert "summary" not in payload
    assert payload["withheld"].startswith("body and summary withheld")
    # Driven from CONTENT_MARKERS rather than a hand-copied list: this loop
    # previously enumerated four keys by hand, so adding a fifth marker
    # (`content_truncated_recovery`) would have silently escaped the check
    # while the test still read as exhaustive. Sourcing the constant means a
    # new marker is covered the moment it is declared.
    assert len(CONTENT_MARKERS) >= 5, "guard the guard: markers must be non-trivial"
    for marker in CONTENT_MARKERS:
        assert marker not in payload, f"{marker} leaked onto a withheld payload"


def test_confidential_withhold_wins_over_truncation(
    test_db: psycopg.Connection[Any], mcp_state: mcp_server._State
) -> None:
    doc_id = _seed(
        test_db,
        title="Comp Bands",
        content_hash="w3-show-9",
        content=_LONG_BODY,
        sensitivity="confidential",
    )
    _with_ceiling(mcp_state, 10)

    payload = mcp_server.brain_show(id_prefix=doc_id)

    assert payload["content"] is None
    assert "content_truncated" not in payload
    assert "content_truncated_recovery" not in payload, (
        "a recovery hint on a withheld payload would tell the agent to re-call "
        "for a body it is not allowed to have"
    )
