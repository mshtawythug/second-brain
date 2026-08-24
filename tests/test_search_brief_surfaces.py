"""Surface tests for brief mode: ``brain search --brief`` and MCP ``brief=true``.

The pure projection logic is covered by ``tests/test_search_brief_projection.py``.
What is pinned HERE is the wiring — that each surface reaches the brief
projection, that the DEFAULT path is untouched by the new code, and that brief
mode cannot become a confidentiality bypass.

``tests/test_search_output_unchanged.py`` is the backward-compatibility
firewall for the default shape and must never be edited. This file deliberately
DUPLICATES its seven-key intent
(:func:`test_cli_default_json_is_unchanged_by_the_brief_code_path`) inside a
file we are allowed to edit, so a future refactor is caught here first and
nobody is ever tempted to "fix" the firewall.

All fixture data is synthetic.
"""
from __future__ import annotations

import json
import os
from collections.abc import Iterator
from typing import Any

import psycopg
import pytest
from typer.testing import CliRunner

from brain import mcp_server
from brain.cli import app
from brain.config import Config
from brain.ingest import ExtractedDoc, ingest_document
from brain.queries import set_document_sensitivity

TEST_DATABASE_URL = os.environ.get(
    "TEST_DATABASE_URL",
    "postgresql://brain:brain@localhost:5434/second_brain_test",
)

#: The frozen public shape of one search result on BOTH surfaces. Kept as a
#: literal (not imported from the firewall) so this file has no dependency on
#: a module that must not change.
SEVEN_KEYS = {
    "id",
    "title",
    "source_kind",
    "snippet",
    "score",
    "content_type",
    "tags",
}

#: Brief mode's shape: the same seven plus exactly one additive key.
EIGHT_KEYS = SEVEN_KEYS | {"snippet_source"}

#: Long enough that ``hybrid_search`` returns a snippet at its 400-char
#: ``SNIPPET_LENGTH`` cap, so a short summary is a materially cheaper choice.
_LONG_BODY = "The quarterly review covered budget, hiring and roadmap. " * 12

#: Short enough to win the cost comparison against that snippet by a wide
#: margin, in tokens or characters.
_SHORT_SUMMARY = "Quarterly planning notes."


def _seed(conn: psycopg.Connection[Any], embedder: Any, count: int = 3) -> None:
    """Ingest ``count`` synthetic documents all matching the word 'quarterly'.

    Copied from ``tests/test_search_output_unchanged.py`` (READ ONLY — never
    import from a firewall we must not perturb). Bodies differ per document:
    ``documents.content_hash`` is UNIQUE, so identical bodies would dedup into
    one row.
    """
    for i in range(count):
        ingest_document(
            conn,
            embedder=embedder,
            doc=ExtractedDoc(
                title=f"Quarterly note {i}",
                content=f"The quarterly review covered budget and hiring {i}.",
                content_type="note",
                source_path=None,
                metadata={},
            ),
            source_kind="manual",
            source_external_id=f"brief-surface-quarterly-{i}",
            tags=["planning"],
        )


def _seed_summarized(
    conn: psycopg.Connection[Any],
    embedder: Any,
    *,
    title: str = "Quarterly note with summary",
    external_id: str = "brief-surface-summarized",
    summary: str = _SHORT_SUMMARY,
) -> str:
    """Ingest one long 'quarterly' document and give it a short summary.

    ``ingest_document`` is called with **no enricher**, which makes enrichment
    a documented no-op — so nothing in these fixtures has a summary unless it
    is written here. A parameterized ``UPDATE`` rather than an Ollama call:
    this is a wiring test, not an enrichment test.

    ``external_id`` is woven into the BODY as well as the source key:
    ``documents.content_hash`` is UNIQUE, so two calls sharing ``_LONG_BODY``
    verbatim would dedup into a single row and silently halve the corpus.
    """
    result = ingest_document(
        conn,
        embedder=embedder,
        doc=ExtractedDoc(
            title=title,
            content=f"{_LONG_BODY}Filed under {external_id}.",
            content_type="note",
            source_path=None,
            metadata={},
        ),
        source_kind="manual",
        source_external_id=external_id,
        tags=["planning"],
    )
    doc_id = result.document_id
    assert doc_id is not None
    conn.execute(
        "UPDATE documents SET summary = %s WHERE id = %s", (summary, doc_id)
    )
    return str(doc_id)


@pytest.fixture
def mcp_state(
    monkeypatch: pytest.MonkeyPatch,
    test_db: psycopg.Connection[Any],  # noqa: ARG001 — keeps the schema fresh
    fake_embedder: Any,
) -> Iterator[mcp_server._State]:
    state = mcp_server._State(
        cfg=Config(database_url=TEST_DATABASE_URL),
        embedder=fake_embedder,
    )
    monkeypatch.setattr(mcp_server, "_state", state)
    yield state


# ---------------------------------------------------------------------------
# CLI — `brain search --brief`
# ---------------------------------------------------------------------------


def test_cli_brief_json_emits_snippet_source(
    test_db: psycopg.Connection[Any],
    fake_embedder: Any,
    patch_embedder: Any,
) -> None:
    """``--brief --json`` returns eight keys and really substitutes a summary."""
    # Arrange — one summarized document plus a NULL-summary tail, so the same
    # payload exercises BOTH the substitution and the fallback.
    _seed(test_db, fake_embedder)
    _seed_summarized(test_db, fake_embedder)
    patch_embedder(fake_embedder)

    # Act
    result = CliRunner().invoke(
        app, ["search", "quarterly", "--fts-only", "--json", "--brief"]
    )

    # Assert
    assert result.exit_code == 0, result.output
    payload = json.loads(result.stdout)
    assert isinstance(payload, list)
    assert payload, "the seeded corpus must produce at least one hit"
    assert all(set(entry) == EIGHT_KEYS for entry in payload)
    sources = {entry["snippet_source"] for entry in payload}
    # Non-vacuous: a run where nothing was substituted would pass a
    # keys-only assertion while proving nothing about brief mode.
    assert "summary" in sources, f"no result took the summary: {payload}"
    assert "chunk" in sources, f"the NULL-summary fallback never ran: {payload}"
    substituted = [e for e in payload if e["snippet_source"] == "summary"]
    assert all(e["snippet"] == _SHORT_SUMMARY for e in substituted)


def test_cli_default_json_is_unchanged_by_the_brief_code_path(
    test_db: psycopg.Connection[Any],
    fake_embedder: Any,
    patch_embedder: Any,
) -> None:
    """Without ``--brief`` the payload is still exactly the seven keys.

    Deliberately duplicates the firewall's intent inside a file we may edit:
    if a refactor breaks the default projection, it goes red HERE, and nobody
    reaches for ``tests/test_search_output_unchanged.py`` to "fix" it.
    """
    # Arrange — the summarized document is present, so a brief projection
    # leaking into the default path would be visible rather than silent.
    _seed(test_db, fake_embedder)
    _seed_summarized(test_db, fake_embedder)
    patch_embedder(fake_embedder)

    # Act
    result = CliRunner().invoke(app, ["search", "quarterly", "--fts-only", "--json"])

    # Assert
    assert result.exit_code == 0, result.output
    payload = json.loads(result.stdout)
    assert payload, "the seeded corpus must produce at least one hit"
    assert all(set(entry) == SEVEN_KEYS for entry in payload)
    assert all("snippet_source" not in entry for entry in payload)


def test_cli_brief_with_meta_envelope_uses_the_brief_projection(
    test_db: psycopg.Connection[Any],
    fake_embedder: Any,
    patch_embedder: Any,
) -> None:
    """``--brief --meta`` must not describe the same search two ways.

    An envelope whose ``results`` stayed full-fat while the bare list went
    brief is exactly the drift ``search_envelope_json``'s "same call"
    guarantee exists to prevent.
    """
    # Arrange
    _seed(test_db, fake_embedder)
    _seed_summarized(test_db, fake_embedder)
    patch_embedder(fake_embedder)

    # Act
    enveloped = CliRunner().invoke(
        app, ["search", "quarterly", "--fts-only", "--json", "--meta", "--brief"]
    )
    bare = CliRunner().invoke(
        app, ["search", "quarterly", "--fts-only", "--json", "--brief"]
    )

    # Assert
    assert enveloped.exit_code == 0, enveloped.output
    assert bare.exit_code == 0, bare.output
    envelope = json.loads(enveloped.stdout)
    assert isinstance(envelope, dict)
    assert envelope["query"] == "quarterly"
    results = envelope["results"]
    assert results, "the seeded corpus must produce at least one hit"
    assert all(set(entry) == EIGHT_KEYS for entry in results)
    assert "summary" in {entry["snippet_source"] for entry in results}
    # The envelope's entries ARE the bare list's entries. ``score`` is dropped
    # from the comparison, not because it may differ in shape but because
    # ``BRAIN_RECENCY_HALFLIFE_DAYS`` (180 by default) makes it a
    # ``now()``-derived decay term: two invocations milliseconds apart
    # legitimately serialize the same ranking to floats differing in the 10th
    # decimal. Everything brief mode touches is compared exactly.
    def _without_score(entries: list[dict[str, Any]]) -> list[dict[str, Any]]:
        return [{k: v for k, v in e.items() if k != "score"} for e in entries]

    assert _without_score(results) == _without_score(json.loads(bare.stdout))


def test_cli_meta_envelope_without_brief_keeps_seven_keys(
    test_db: psycopg.Connection[Any],
    fake_embedder: Any,
    patch_embedder: Any,
) -> None:
    """The ``brief=False`` default leaves the envelope byte-identical."""
    # Arrange
    _seed(test_db, fake_embedder)
    _seed_summarized(test_db, fake_embedder)
    patch_embedder(fake_embedder)

    # Act
    result = CliRunner().invoke(
        app, ["search", "quarterly", "--fts-only", "--json", "--meta"]
    )

    # Assert
    assert result.exit_code == 0, result.output
    envelope = json.loads(result.stdout)
    assert envelope["results"], "the seeded corpus must produce at least one hit"
    assert all(set(entry) == SEVEN_KEYS for entry in envelope["results"])


def test_cli_brief_is_a_no_op_without_json(
    test_db: psycopg.Connection[Any],
    fake_embedder: Any,
    patch_embedder: Any,
) -> None:
    """On the human table path ``--brief`` changes nothing (documented no-op)."""
    # Arrange
    _seed(test_db, fake_embedder)
    _seed_summarized(test_db, fake_embedder)
    patch_embedder(fake_embedder)

    # Act
    plain = CliRunner().invoke(app, ["search", "quarterly", "--fts-only"])
    briefed = CliRunner().invoke(app, ["search", "quarterly", "--fts-only", "--brief"])

    # Assert
    assert plain.exit_code == 0, plain.output
    assert briefed.exit_code == 0, briefed.output
    # Non-vacuity: two "(no results)" tables also compare equal, which would
    # make the equality below prove nothing about brief mode.
    assert "(no results)" not in plain.stdout, "the table must have rendered hits"
    assert briefed.stdout == plain.stdout


# ---------------------------------------------------------------------------
# MCP — `brain_search(brief=...)`
# ---------------------------------------------------------------------------


def test_mcp_brain_search_brief_returns_eight_keys(
    mcp_state: mcp_server._State,  # noqa: ARG001 — installs the fake state
    test_db: psycopg.Connection[Any],
    fake_embedder: Any,
) -> None:
    """MCP brief mode mirrors the CLI's projection exactly."""
    # Arrange
    _seed(test_db, fake_embedder)
    _seed_summarized(test_db, fake_embedder)

    # Act
    payload = mcp_server.brain_search(query="quarterly", fts_only=True, brief=True)

    # Assert
    results = payload["results"]
    assert results, "the seeded corpus must produce at least one hit"
    assert all(set(entry) == EIGHT_KEYS for entry in results)
    assert "summary" in {entry["snippet_source"] for entry in results}
    assert "chunk" in {entry["snippet_source"] for entry in results}
    # The top-level envelope is untouched by brief mode.
    assert "session_id" in payload


def test_mcp_brain_search_default_returns_seven_keys(
    mcp_state: mcp_server._State,  # noqa: ARG001 — installs the fake state
    test_db: psycopg.Connection[Any],
    fake_embedder: Any,
) -> None:
    """``brief`` defaults to False and the seven-key shape is byte-identical."""
    # Arrange
    _seed(test_db, fake_embedder)
    _seed_summarized(test_db, fake_embedder)

    # Act
    payload = mcp_server.brain_search(query="quarterly", fts_only=True)

    # Assert
    results = payload["results"]
    assert results, "the seeded corpus must produce at least one hit"
    assert all(set(entry) == SEVEN_KEYS for entry in results)


# ---------------------------------------------------------------------------
# F6 — brief mode must not become a confidentiality bypass
# ---------------------------------------------------------------------------

#: Present ONLY in the confidential document's summary and in no query, so
#: finding it in a response is unambiguous evidence of egress.
_CONF_SUMMARY_MARKER = "quokkavolt"
_CONF_SUMMARY = f"Severance bands filed under {_CONF_SUMMARY_MARKER} terms."


def test_brief_never_leaks_confidential_summary_over_mcp(
    mcp_state: mcp_server._State,  # noqa: ARG001 — installs the fake state
    test_db: psycopg.Connection[Any],
    fake_embedder: Any,
) -> None:
    """Brief mode reads ``documents.summary`` — F6's exclusion still governs.

    ``documents.summary`` is LLM-generated *from* the body, so a brief payload
    that carried a confidential document's summary would hand back the same
    substance, condensed. The guarantee is stronger than redaction: the
    confidential row must be ABSENT from ``results`` entirely, because a
    present-but-redacted row is itself an oracle — a hit for "severance"
    proves the withheld body contains it.

    The assertion serializes the WHOLE response rather than the fields we
    thought of, matching ``tests/test_mcp_confidential_egress.py``.
    """
    # Arrange — a confidential document and a normal one that BOTH match, so
    # an empty result set cannot be mistaken for "the query matched nothing".
    conf_id = _seed_summarized(
        test_db,
        fake_embedder,
        title="Confidential quarterly comp",
        external_id="brief-surface-confidential",
        summary=_CONF_SUMMARY,
    )
    _seed_summarized(test_db, fake_embedder)
    assert set_document_sensitivity(
        test_db, document_id=conf_id, level="confidential"
    )
    row = test_db.execute(
        "SELECT sensitivity, summary FROM documents WHERE id = %s", (conf_id,)
    ).fetchone()
    assert row is not None
    assert row[0] == "confidential", "fixture must be confidential"
    assert row[1] == _CONF_SUMMARY, "a null summary would make this vacuous"

    # Act
    withheld = mcp_server.brain_search(
        query="quarterly", fts_only=True, limit=10, brief=True
    )
    opted_in = mcp_server.brain_search(
        query="quarterly",
        fts_only=True,
        limit=10,
        brief=True,
        include_confidential=True,
    )

    # Assert — (1) absent from the match set entirely, not merely redacted.
    titles = [r["title"] for r in withheld["results"]]
    assert "Confidential quarterly comp" not in titles
    assert _CONF_SUMMARY_MARKER not in json.dumps(withheld, default=str).lower()
    assert withheld["results"], "the normal document must still be returned"

    # (2) the explicit opt-in still works and may carry the summary. Asserting
    # the marker IS present here is the positive control for (1): without it a
    # marker that never reached ANY payload would satisfy the exclusion check
    # vacuously. (The exclusion proof itself rests on the title assertion.)
    opted_titles = [r["title"] for r in opted_in["results"]]
    assert "Confidential quarterly comp" in opted_titles
    assert _CONF_SUMMARY_MARKER in json.dumps(opted_in, default=str).lower()
