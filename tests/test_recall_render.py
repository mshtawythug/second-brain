"""``RecallResult`` rendering and JSON projection (F2).

The context block is the artifact an agent actually consumes, so its shape is
a contract, not cosmetics:

- ``[N]`` citation markers matching what ``brain ask`` already teaches models;
- a header line per passage carrying id / date / source / title, so a model
  can attribute a claim without a second lookup;
- ``unknown`` for a missing date in the *human* block, but a real ``null`` in
  JSON — a machine consumer must be able to tell "no date" from a document
  titled "unknown".

These are unit tests over hand-built dataclasses: no DB, no search, so a
rendering regression cannot hide behind retrieval noise.

All fixture data is synthetic.
"""
from __future__ import annotations

import json
from datetime import UTC, datetime

import pytest

from brain.recall import RecallPassage, RecallResult


def _passage(
    ref: int = 1,
    *,
    document_id: str = "3f2a1c9d4b5e6f70",
    title: str = "Quarterly Planning",
    date: datetime | None = datetime(2026, 7, 14, tzinfo=UTC),
    source_kind: str | None = "krisp",
    text: str = "We walked through the platform staffing plan.",
    tokens: int = 12,
    truncated: bool = False,
) -> RecallPassage:
    return RecallPassage(
        ref=ref,
        document_id=document_id,
        title=title,
        date=date,
        source_kind=source_kind,
        content_type="transcript",
        tags=["planning"],
        score=0.031,
        text=text,
        tokens=tokens,
        truncated=truncated,
    )


def _result(
    passages: list[RecallPassage] | None = None,
    *,
    query: str = "platform staffing",
    budget_tokens: int = 2000,
    used_tokens: int = 12,
    candidates_considered: int = 1,
    dropped: int = 0,
    truncated: bool = False,
    fts_count: int | None = 3,
) -> RecallResult:
    return RecallResult(
        query=query,
        budget_tokens=budget_tokens,
        used_tokens=used_tokens,
        candidates_considered=candidates_considered,
        dropped=dropped,
        truncated=truncated,
        fts_count=fts_count,
        passages=[_passage()] if passages is None else passages,
    )


# ---------------------------------------------------------------------------
# context_block
# ---------------------------------------------------------------------------


def test_block_starts_with_a_header_naming_the_query() -> None:
    block = _result().context_block()

    assert block.splitlines()[0].startswith("# recall: platform staffing")


def test_passage_header_carries_id_date_source_and_title() -> None:
    block = _result().context_block()

    assert "[1] 3f2a1c9d | 2026-07-14 | krisp | Quarterly Planning" in block


def test_passage_body_follows_its_header() -> None:
    block = _result().context_block()
    lines = block.splitlines()
    header_index = next(i for i, ln in enumerate(lines) if ln.startswith("[1]"))

    assert lines[header_index + 1] == (
        "We walked through the platform staffing plan."
    )


def test_missing_date_renders_as_unknown_in_the_human_block() -> None:
    block = _result([_passage(date=None)]).context_block()

    assert "| unknown |" in block


def test_missing_source_kind_renders_as_manual() -> None:
    """Matches ``search_table``'s display fallback, so surfaces agree."""
    block = _result([_passage(source_kind=None)]).context_block()

    assert "| manual |" in block


def test_refs_appear_in_order() -> None:
    block = _result(
        [_passage(1), _passage(2, document_id="aaaabbbbccccdddd"), _passage(3)]
    ).context_block()

    assert [ln[:3] for ln in block.splitlines() if ln.startswith("[")] == [
        "[1]",
        "[2]",
        "[3]",
    ]


def test_header_reports_dropped_and_truncated_when_they_happened() -> None:
    """The user must be told the answer is partial — silently truncating is worse."""
    block = _result(dropped=4, truncated=True).context_block()
    header = block.splitlines()[0]

    assert "4 dropped" in header
    assert "truncated" in header


def test_header_omits_dropped_and_truncated_when_they_did_not() -> None:
    header = _result().context_block().splitlines()[0]

    assert "dropped" not in header
    assert "truncated" not in header


def test_empty_recall_still_renders_a_header() -> None:
    """Zero passages is an answer; it should say so rather than emit nothing."""
    block = _result([], used_tokens=0, candidates_considered=0).context_block()

    assert block.startswith("# recall: platform staffing")
    assert "0 passage(s)" in block


def test_block_contains_no_rich_markup_hazard() -> None:
    """``[1]`` would be parsed as a style tag by ``console.print``.

    The CLI must emit this with plain ``typer.echo``. This test documents the
    hazard at the source so the constraint travels with the artifact.
    """
    block = _result().context_block()

    assert "[1]" in block, (
        "citation markers are Rich style-tag shaped — render with typer.echo, "
        "never console.print, or Rich raises MissingStyle"
    )


# ---------------------------------------------------------------------------
# to_dict
# ---------------------------------------------------------------------------


def test_to_dict_has_the_documented_key_set() -> None:
    payload = _result().to_dict()

    assert set(payload) == {
        "query",
        "budget_tokens",
        "used_tokens",
        "candidates_considered",
        "dropped",
        "truncated",
        "fts_count",
        "passages",
    }


def test_passage_dict_has_the_documented_key_set() -> None:
    payload = _result().to_dict()

    assert set(payload["passages"][0]) == {
        "ref",
        "document_id",
        "title",
        "date",
        "source_kind",
        "content_type",
        "tags",
        "score",
        "text",
        "tokens",
        "truncated",
    }


def test_json_date_is_iso_not_the_display_placeholder() -> None:
    payload = _result().to_dict()

    assert payload["passages"][0]["date"] == "2026-07-14T00:00:00+00:00"


def test_json_missing_date_is_null_not_unknown() -> None:
    """A machine consumer must tell "no date" from a doc titled ``unknown``."""
    payload = _result([_passage(date=None)]).to_dict()

    assert payload["passages"][0]["date"] is None


def test_to_dict_is_json_serializable() -> None:
    """The projection is what ``--json`` and MCP both emit."""
    encoded = json.dumps(_result().to_dict())

    assert json.loads(encoded)["query"] == "platform staffing"


def test_full_document_id_is_preserved_in_json() -> None:
    """The block shows 8 chars for readability; JSON must carry the full id."""
    payload = _result().to_dict()

    assert payload["passages"][0]["document_id"] == "3f2a1c9d4b5e6f70"


def test_passage_is_frozen() -> None:
    with pytest.raises(AttributeError):
        _passage().ref = 9  # type: ignore[misc]
