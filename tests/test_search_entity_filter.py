"""``hybrid_search`` with the ``--entity`` predicate (RM-24 ACs 1 and 3), against the test DB.

Four synthetic notes all carry the shared term; two of them mention the synthetic entity. Each leg
is exercised alone, so a leg that dropped the filter would return all four.
"""
from __future__ import annotations

from typing import Any

import psycopg
import pytest

from brain.entity_filter import resolve_entity_ids
from brain.facets import compute_facets
from brain.graph_rag.aliases import AliasRule
from brain.ingest import Embedder
from brain.search import SearchDiagnostics, build_tsquery, hybrid_search
from brain.search_predicate import build_predicate
from tests.entity_filter_seed import (
    OTHER_TENANT,
    SHARED_TERM,
    TENANT,
    seed_document,
    seed_entity,
    seed_mention,
)

#: A query with no lexical match, so only the vector leg can return rows.
_VECTOR_ONLY_QUERY = "zqxvwk"


@pytest.fixture
def corpus(test_db: psycopg.Connection[Any], fake_embedder: Embedder) -> dict[str, Any]:
    """Seed four notes; the entity is mentioned by two of them."""
    docs = [seed_document(test_db, fake_embedder, f"Survey note {n}") for n in range(4)]
    entity = seed_entity(test_db, name="Quillmoor", canonical_key="quillmoor")
    for doc in docs[:2]:
        seed_mention(test_db, entity, doc)
    return {"docs": docs, "entity": entity, "mentioned": set(docs[:2])}


def _search(conn: psycopg.Connection[Any], embedder: Embedder, **kwargs: Any) -> set[str]:
    results = hybrid_search(conn, embedder=embedder, limit=20, **kwargs)
    return {r.document_id for r in results}


def test_fts_leg_returns_only_the_entity_documents(
    test_db: psycopg.Connection[Any], fake_embedder: Embedder, corpus: dict[str, Any]
) -> None:
    got = _search(
        test_db,
        fake_embedder,
        query=SHARED_TERM,
        fts_only=True,
        entity_ids=[corpus["entity"]],
        entity_tenant=TENANT,
    )

    assert got == corpus["mentioned"]


def test_vector_leg_returns_only_the_entity_documents(
    test_db: psycopg.Connection[Any], fake_embedder: Embedder, corpus: dict[str, Any]
) -> None:
    unfiltered = _search(
        test_db, fake_embedder, query=_VECTOR_ONLY_QUERY, vector_sim_floor=-1.0
    )
    got = _search(
        test_db,
        fake_embedder,
        query=_VECTOR_ONLY_QUERY,
        vector_sim_floor=-1.0,
        entity_ids=[corpus["entity"]],
        entity_tenant=TENANT,
    )

    assert unfiltered == set(corpus["docs"])
    assert got == corpus["mentioned"]


def test_the_tenant_is_part_of_the_filter(
    test_db: psycopg.Connection[Any], fake_embedder: Embedder, corpus: dict[str, Any]
) -> None:
    # This tenant's entity id under another tenant matches no mention row: the
    # composite key is (tenant_id, entity_id, document_id).
    got = _search(
        test_db,
        fake_embedder,
        query=SHARED_TERM,
        fts_only=True,
        entity_ids=[corpus["entity"]],
        entity_tenant=OTHER_TENANT,
    )

    assert got == set()


def test_total_and_facets_agree_with_the_filtered_set(
    test_db: psycopg.Connection[Any], fake_embedder: Embedder, corpus: dict[str, Any]
) -> None:
    entity_kw = {"entity_ids": [corpus["entity"]], "entity_tenant": TENANT}
    diagnostics = SearchDiagnostics()

    hybrid_search(
        test_db,
        embedder=fake_embedder,
        query=SHARED_TERM,
        fts_only=True,
        diagnostics=diagnostics,
        total_count=True,
        **entity_kw,
    )
    facets = compute_facets(
        test_db,
        predicate=build_predicate(**entity_kw),
        tsquery=build_tsquery(test_db, SHARED_TERM),
    )

    assert diagnostics.total_documents == len(corpus["mentioned"])
    assert facets.total_documents == len(corpus["mentioned"])


def test_an_unmerged_alias_source_keeps_its_documents(
    test_db: psycopg.Connection[Any], fake_embedder: Embedder
) -> None:
    """Regression (rev-91 b): both entities exist and neither is merged yet.

    One note mentions only the source, one only the target, one neither; the
    search must return the first two.
    """
    only_source, only_target, neither = (
        seed_document(test_db, fake_embedder, f"Harbor note {n}") for n in range(3)
    )
    source = seed_entity(test_db, name="Harbor", canonical_key="harbor")
    target = seed_entity(test_db, name="Harbor Light", canonical_key="harborlight")
    seed_mention(test_db, source, only_source)
    seed_mention(test_db, target, only_target)
    rules = [AliasRule("project", "harbor", "project", "harborlight")]

    ids = resolve_entity_ids(
        test_db, tenant_id=TENANT, name="Harbor", entity_type=None, rules=rules
    )
    got = _search(
        test_db,
        fake_embedder,
        query=SHARED_TERM,
        fts_only=True,
        entity_ids=ids,
        entity_tenant=TENANT,
    )

    assert got == {only_source, only_target}
    assert neither not in got
