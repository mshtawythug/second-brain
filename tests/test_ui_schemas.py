"""Request validation, and the source-kind drift guard."""
from __future__ import annotations

from datetime import UTC, datetime

import pytest

from brain.search import SearchResult
from brain.ui.errors import UiBadRequest
from brain.ui.schemas import (
    MAX_BODY_BYTES,
    VALID_SOURCE_KINDS,
    parse_iso_date,
    parse_note_create,
    parse_note_patch,
    parse_search_params,
    require_confirm,
    search_result_payload,
)


def test_source_kinds_match_the_cli_exactly() -> None:
    """The drift guard for a knowingly duplicated constant.

    ``brain.ui.schemas.VALID_SOURCE_KINDS`` duplicates
    ``brain.cli._VALID_SOURCE_KINDS`` because importing the 9,800-line Typer
    module into an HTTP handler is unacceptable startup cost, and the extraction
    to ``brain/source_kinds.py`` that the design called for was never landed.

    Duplication is only safe if divergence is loud. This test is what makes it
    loud.
    """
    from brain.cli import _VALID_SOURCE_KINDS

    assert VALID_SOURCE_KINDS == _VALID_SOURCE_KINDS, (
        "source-kind vocabularies have diverged.\n"
        "  brain/ui/schemas.py::VALID_SOURCE_KINDS = "
        f"{sorted(VALID_SOURCE_KINDS)}\n"
        "  brain/cli.py::_VALID_SOURCE_KINDS        = "
        f"{sorted(_VALID_SOURCE_KINDS)}\n"
        "Update BOTH, or land the brain/source_kinds.py extraction."
    )


# ------------------------------------------------------------------ search --


def test_query_is_required() -> None:
    with pytest.raises(UiBadRequest) as exc:
        parse_search_params({})
    assert exc.value.code == "missing_query"


def test_query_length_is_capped() -> None:
    with pytest.raises(UiBadRequest) as exc:
        parse_search_params({"q": "x" * 513})
    assert exc.value.code == "query_too_long"


@pytest.mark.parametrize("limit", ["0", "51", "-1", "abc"])
def test_bad_limits_are_rejected_not_clamped(limit: str) -> None:
    """A silently clamped limit hides the caller's bug; a 400 surfaces it."""
    with pytest.raises(UiBadRequest) as exc:
        parse_search_params({"q": "x", "limit": limit})
    assert exc.value.code == "invalid_limit"


def test_default_limit_is_applied() -> None:
    assert parse_search_params({"q": "x"}).limit == 25


def test_unknown_source_is_rejected_with_the_legal_values() -> None:
    with pytest.raises(UiBadRequest) as exc:
        parse_search_params({"q": "x", "source": "notion"})
    assert exc.value.code == "invalid_source"
    assert "krisp" in str(exc.value)


def test_tag_is_normalized() -> None:
    assert parse_search_params({"q": "x", "tag": "Interview Prep"}).tag == "interview-prep"


def test_dates_parse_and_order_is_validated() -> None:
    spec = parse_search_params({"q": "x", "after": "2026-01-01", "before": "2026-02-01"})
    assert spec.after is not None and spec.after.year == 2026
    with pytest.raises(UiBadRequest) as exc:
        parse_search_params({"q": "x", "after": "2026-03-01", "before": "2026-01-01"})
    assert exc.value.code == "invalid_date"


def test_naive_dates_are_treated_as_utc() -> None:
    """So one query string means one thing regardless of the server's zone."""
    parsed = parse_iso_date("2026-07-26", "after")
    assert parsed.tzinfo is not None
    assert parsed.utcoffset().total_seconds() == 0  # type: ignore[union-attr]


def test_garbage_date_is_a_400() -> None:
    with pytest.raises(UiBadRequest) as exc:
        parse_iso_date("last tuesday", "after")
    assert exc.value.code == "invalid_date"


# ------------------------------------------------------------------- patch --


def test_body_hash_is_required() -> None:
    with pytest.raises(UiBadRequest) as exc:
        parse_note_patch({"body": "x"})
    assert exc.value.code == "missing_body_hash"


def test_oversized_body_is_rejected() -> None:
    with pytest.raises(UiBadRequest) as exc:
        parse_note_patch({"body_hash": "h", "body": "x" * (MAX_BODY_BYTES + 1)})
    assert exc.value.code == "body_too_large"


def test_patch_tags_are_normalized() -> None:
    patch = parse_note_patch({"body_hash": "h", "tags": ["Interview Prep", "A B"]})
    assert patch.tags == ["interview-prep", "a-b"]


def test_empty_patch_is_detected() -> None:
    assert parse_note_patch({"body_hash": "h"}).is_empty()


def test_non_object_body_is_rejected() -> None:
    with pytest.raises(UiBadRequest):
        parse_note_patch(["not", "an", "object"])


# ------------------------------------------------------------------ create --


def test_title_is_required_and_capped() -> None:
    with pytest.raises(UiBadRequest):
        parse_note_create({})
    with pytest.raises(UiBadRequest):
        parse_note_create({"title": "   "})
    with pytest.raises(UiBadRequest) as exc:
        parse_note_create({"title": "x" * 201})
    assert exc.value.code == "invalid_title"


@pytest.mark.parametrize("folder", ["../etc", "a/../../b", "/absolute"])
def test_traversal_shaped_folders_are_rejected(folder: str) -> None:
    """Defence in depth: assert_within_vault is the real control."""
    with pytest.raises(UiBadRequest) as exc:
        parse_note_create({"title": "ok", "folder": folder})
    assert exc.value.code == "folder_escapes_vault"


@pytest.mark.parametrize("template", ["../evil", "a/b", ".hidden"])
def test_template_names_cannot_be_paths(template: str) -> None:
    with pytest.raises(UiBadRequest) as exc:
        parse_note_create({"title": "ok", "template": template})
    assert exc.value.code == "invalid_template"


def test_create_defaults() -> None:
    spec = parse_note_create({"title": "Retro"})
    assert spec.template == "note"
    assert spec.folder == ""
    assert spec.tags == []


# ----------------------------------------------------------------- confirm --


def test_confirm_is_required_for_destructive_calls() -> None:
    for payload in ({}, {"confirm": False}, {"confirm": "yes"}):
        with pytest.raises(UiBadRequest) as exc:
            require_confirm(payload)
        assert exc.value.code == "confirm_required"
    require_confirm({"confirm": True})   # does not raise

# ------------------------------------------------------------ ledger payload --


def _result(**overrides: object) -> SearchResult:
    """One synthetic hit. No PII: invented title, invented id."""
    fields: dict[str, object] = {
        "document_id": "0f1e2d3c4b5a69788796a5b4c3d2e1f0",
        "title": "Quarterly Vendor Review",
        "source_kind": "manual",
        "snippet": "three unresolved threads",
        "score": 0.5,
        "content_type": "note",
        "tags": ["vendors"],
        "recency_ts": datetime(2026, 3, 9, 17, 45, tzinfo=UTC),
    }
    fields.update(overrides)
    return SearchResult(**fields)  # type: ignore[arg-type]


def test_ledger_payload_carries_the_document_date() -> None:
    """D6: the gutter had eight hex characters where a date belongs.

    The date has to reach the client before the ledger can render it, so the
    payload is where the defect actually lives.
    """
    payload = search_result_payload(_result())
    assert payload["date"] == "2026-03-09"


def test_ledger_payload_date_is_the_calendar_day_not_a_timestamp() -> None:
    """A 5.5rem gutter cannot hold an ISO datetime, and the time is noise."""
    payload = search_result_payload(_result())
    assert "T" not in payload["date"]
    assert len(payload["date"]) == len("YYYY-MM-DD")


def test_ledger_payload_date_is_none_when_recency_ts_was_never_set() -> None:
    """The unset case, spelled honestly.

    An earlier version of this docstring claimed to cover "a document with
    neither timestamp". That state is UNREACHABLE: ``documents.ingested_at`` is
    ``TIMESTAMPTZ NOT NULL DEFAULT NOW()`` (``001_init.sql:23``), so
    ``coalesce(sent_at, ingested_at)`` is never NULL. What this actually covers
    is a ``SearchResult`` whose ``recency_ts`` was never populated — which the
    sibling test below names concretely as the graph legs.

    ``None`` rather than ``"-"``: a server-side placeholder would be
    indistinguishable from a real value to every other consumer.
    """
    assert search_result_payload(_result(recency_ts=None))["date"] is None


def test_ledger_payload_date_survives_a_graph_shaped_result() -> None:
    """``brain.graph_rag`` builds its own SearchResults and sets no timestamp.

    ``recency_ts`` defaults to ``None`` there, so the projection must not assume
    the attribute is populated — or every graph-fed surface would raise.
    """
    graph_hit = SearchResult(
        document_id="aaaabbbbccccdddd",
        title="Concept: retrieval",
        source_kind=None,
        snippet="",
        score=1.0,
        content_type="note",
        tags=[],
    )
    assert search_result_payload(graph_hit)["date"] is None


def test_ledger_payload_keeps_its_established_keys() -> None:
    """Adding ``date`` must not drop or rename anything the ledger reads."""
    payload = search_result_payload(_result())
    assert set(payload) == {
        "id", "title", "source_kind", "date", "snippet",
        "score", "content_type", "tags",
    }
    assert payload["id"] == "0f1e2d3c4b5a69788796a5b4c3d2e1f0"
    assert payload["title"] == "Quarterly Vendor Review"
    assert payload["tags"] == ["vendors"]

