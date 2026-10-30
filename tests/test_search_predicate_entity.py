"""Pure-logic tests for the ``--entity`` clause of the shared search predicate (RM-24).

No database: this is string assembly. The SQL behaviour is covered by
``tests/test_search_entity_filter.py``.
"""
from __future__ import annotations

import pytest

from brain.search_predicate import build_predicate

_IDS = [
    "00000000-0000-4000-8000-000000000001",
    "00000000-0000-4000-8000-000000000002",
]

_CLAUSE = (
    "d.id IN (SELECT gem.document_id FROM graph_entity_mentions gem "
    "WHERE gem.tenant_id = %s AND gem.entity_id = ANY(%s::uuid[]))"
)


def test_entity_ids_add_the_mentions_clause_and_bind_both_values() -> None:
    predicate = build_predicate(entity_ids=_IDS, entity_tenant="default")

    assert predicate.where_sql == f"TRUE AND {_CLAUSE}"
    assert predicate.where_params == ("default", _IDS)
    assert predicate.has_filters is True
    assert predicate.join_clause == "JOIN documents d ON d.id = c.document_id"


def test_the_clause_composes_with_other_filters_in_order() -> None:
    predicate = build_predicate(tag="planning", entity_ids=_IDS, entity_tenant="default")

    assert predicate.where_params == ("planning", "default", _IDS)


@pytest.mark.parametrize("entity_ids", [None, []])
def test_no_entity_ids_leave_the_fast_path_untouched(entity_ids: list[str] | None) -> None:
    predicate = build_predicate(entity_ids=entity_ids, entity_tenant="default")

    assert predicate.where_sql == "TRUE"
    assert predicate.where_params == ()
    assert predicate.prepare_flag is True


def test_entity_ids_without_a_tenant_are_a_caller_bug() -> None:
    with pytest.raises(ValueError, match="entity_tenant"):
        build_predicate(entity_ids=_IDS)
