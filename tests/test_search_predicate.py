"""Pure-logic tests for the shared search WHERE/JOIN predicate.

``build_predicate`` is the single construction site for every metadata filter
in hybrid search, so it is also the single place a caller value could leak into
SQL *text*. The injection guard below is the assertion that matters most.

No database, no fixtures — this module is pure string assembly.
"""
from __future__ import annotations

from datetime import UTC, datetime

import pytest

from brain.search_predicate import SearchPredicate, _ensure_utc, build_predicate

#: Every filter, with a representative synthetic value.
ALL_FILTERS: list[tuple[str, object]] = [
    ("source_kind", "gmail"),
    ("tag", "planning"),
    ("since_days", 7),
    ("person_keys", ["someone@example.com"]),
    ("after", datetime(2026, 1, 1, tzinfo=UTC)),
    ("before", datetime(2026, 2, 1, tzinfo=UTC)),
    ("content_type", "transcript"),
    ("thread_id", "thread-abc"),
    ("draft", True),
    ("without_tag", "archive"),
    ("updated_after", datetime(2026, 6, 1, tzinfo=UTC)),
    ("updated_before", datetime(2026, 7, 1, tzinfo=UTC)),
    ("sensitivity", "confidential"),
]


def test_no_filters_yields_true_predicate_and_prepare_flag() -> None:
    """The unfiltered fast path: no JOIN, no params, prepared statement on."""
    # Arrange / Act
    predicate = build_predicate()

    # Assert
    assert predicate.where_sql == "TRUE"
    assert predicate.where_params == ()
    assert predicate.has_filters is False
    assert predicate.join_clause == ""
    assert predicate.fts_filter == ""
    assert predicate.prepare_flag is True


@pytest.mark.parametrize(("name", "value"), ALL_FILTERS)
def test_each_filter_appends_one_clause_and_one_param(
    name: str, value: object
) -> None:
    """Every filter contributes exactly one bound parameter per placeholder."""
    # Arrange / Act
    predicate = build_predicate(**{name: value})  # type: ignore[arg-type]

    # Assert
    assert predicate.has_filters is True, f"{name} should register as a filter"
    assert predicate.where_sql.startswith("TRUE AND ")
    assert predicate.where_sql.count("%s") == len(predicate.where_params)
    assert predicate.join_clause == "JOIN documents d ON d.id = c.document_id"
    assert predicate.fts_filter == f" AND {predicate.where_sql}"
    assert predicate.prepare_flag is None


def test_all_filters_together_bind_one_param_each() -> None:
    """Every filter combined binds exactly one placeholder per value.

    Deliberately counted off ``ALL_FILTERS`` rather than a literal, so a wave
    that appends a filter (F9 added ``updated_after`` / ``updated_before``)
    extends the assertion by adding one row to that list.
    """
    # Arrange / Act
    predicate = build_predicate(**dict(ALL_FILTERS))  # type: ignore[arg-type]

    # Assert
    assert len(predicate.where_params) == len(ALL_FILTERS)
    assert predicate.where_sql.count("%s") == len(ALL_FILTERS)


def test_predicate_fields_never_contain_caller_values() -> None:
    """THE INJECTION GUARD: caller text binds as a param, never as SQL text."""
    # Arrange
    payload = "'; DROP TABLE documents; --"

    # Act
    predicate = build_predicate(tag=payload, thread_id=payload, without_tag=payload)

    # Assert — the value travels only in the bound params...
    assert predicate.where_params.count(payload) == 3
    # ...and appears in NO field that is ever spliced into SQL text.
    for field in (
        predicate.where_sql,
        predicate.fts_filter,
        predicate.join_clause,
    ):
        assert payload not in field
        assert "DROP TABLE" not in field


def test_predicate_is_frozen_and_params_are_a_tuple() -> None:
    """Immutability: one shared instance cannot be mutated by a consumer."""
    # Arrange
    predicate = build_predicate(tag="planning")

    # Act / Assert
    assert isinstance(predicate.where_params, tuple)
    with pytest.raises(AttributeError):
        predicate.where_sql = "TRUE"  # type: ignore[misc]


def test_naive_after_is_stamped_utc() -> None:
    """A naive boundary must not shift with the session TimeZone."""
    # Arrange
    naive = datetime(2026, 1, 1)  # noqa: DTZ001 — the naive input IS the case

    # Act
    predicate = build_predicate(after=naive)

    # Assert
    assert predicate.where_params[0] == datetime(2026, 1, 1, tzinfo=UTC)
    assert _ensure_utc(naive).tzinfo is UTC


def test_aware_datetime_passes_through_unchanged() -> None:
    """An already-aware datetime is not re-stamped."""
    # Arrange
    aware = datetime(2026, 1, 1, 3, 0, tzinfo=UTC)

    # Act / Assert
    assert _ensure_utc(aware) is aware


def test_empty_person_keys_is_not_a_filter() -> None:
    """An explicitly empty key list means 'no person filter', not 'match none'."""
    # Arrange / Act
    predicate = build_predicate(person_keys=[])

    # Assert
    assert predicate.has_filters is False
    assert predicate.where_sql == "TRUE"


def test_draft_false_is_a_filter_not_a_falsy_skip() -> None:
    """``draft=False`` means 'published only' — a three-state filter."""
    # Arrange / Act
    predicate = build_predicate(draft=False)

    # Assert
    assert predicate.has_filters is True
    assert predicate.where_params == (False,)


def test_search_predicate_is_constructible_directly() -> None:
    """The dataclass is part of the published contract for downstream waves."""
    # Arrange / Act
    predicate = SearchPredicate(
        where_sql="TRUE",
        where_params=(),
        has_filters=False,
        join_clause="",
        fts_filter="",
        prepare_flag=True,
    )

    # Assert
    assert predicate.has_filters is False
