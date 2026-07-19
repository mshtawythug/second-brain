"""Integration tests for `brain demo` seeding against the real test Postgres.

These exercise the full ingest + FTS retrieval path with the deterministic
:class:`DemoEmbedder` — no Ollama. They mutate the embedding column's dim
(``ensure_embedding_column`` inside :func:`seed_demo`), so each carries
``fresh_schema`` to route through the full drop+migrate reset and let the
autouse teardown restore the migrated-once baseline.
"""
import psycopg
import pytest

from brain.demo import seed_demo
from brain.demo.embedder import DemoEmbedder
from brain.search import hybrid_search
from tests.conftest import TEST_DATABASE_URL

HERO_QUERY = "compliance horror stories"

# Titles of the six "horror story" docs the hero query must rank (docs
# 1/2/4/7/11/20 in the manifest). The seed test asserts the FTS-only top-5
# overlaps this set by ≥3.
HERO_TITLES = frozenset(
    {
        "Compliance Horror Stories — collected war stories",
        "SOC 2 Type II readiness sync — Sam & Priya",
        "#compliance — audit war stories thread",
        "PCI scope creep review — Marcus & Sam",
        "Vendor risk committee — Q2",
        "#random — worst on-call ever",
    }
)


@pytest.mark.fresh_schema
def test_seed_ingests_all_22(test_db: psycopg.Connection) -> None:
    report = seed_demo(TEST_DATABASE_URL)

    assert report.total == 22
    assert report.ingested == 22
    assert report.skipped == 0
    row = test_db.execute("SELECT count(*) FROM documents").fetchone()
    assert row is not None and row[0] == 22


@pytest.mark.fresh_schema
def test_hero_query_surfaces_horror_docs(test_db: psycopg.Connection) -> None:
    seed_demo(TEST_DATABASE_URL)

    results = hybrid_search(
        test_db,
        embedder=DemoEmbedder(),
        query=HERO_QUERY,
        limit=5,
        fts_only=True,
    )

    titles = {r.title for r in results}
    overlap = titles & HERO_TITLES
    assert len(overlap) >= 3, f"hero query top-5 only matched {sorted(overlap)}"


@pytest.mark.fresh_schema
def test_source_filter_returns_only_slack(test_db: psycopg.Connection) -> None:
    seed_demo(TEST_DATABASE_URL)

    results = hybrid_search(
        test_db,
        embedder=DemoEmbedder(),
        query=HERO_QUERY,
        limit=10,
        source_kind="slack",
        fts_only=True,
    )

    assert results, "expected at least one slack hero doc"
    assert all(r.source_kind == "slack" for r in results)


@pytest.mark.fresh_schema
def test_tag_filter_returns_only_compliance(test_db: psycopg.Connection) -> None:
    seed_demo(TEST_DATABASE_URL)

    results = hybrid_search(
        test_db,
        embedder=DemoEmbedder(),
        query=HERO_QUERY,
        limit=10,
        tag="compliance",
        fts_only=True,
    )

    assert results, "expected at least one compliance-tagged hero doc"
    assert all("compliance" in r.tags for r in results)


@pytest.mark.fresh_schema
def test_person_filter_returns_only_priya_docs(test_db: psycopg.Connection) -> None:
    seed_demo(TEST_DATABASE_URL)

    results = hybrid_search(
        test_db,
        embedder=DemoEmbedder(),
        query=HERO_QUERY,
        limit=10,
        person_keys=["priya okafor"],
        fts_only=True,
    )

    assert results, "expected at least one Priya Okafor hero doc"
    for r in results:
        row = test_db.execute(
            "SELECT participants FROM documents WHERE id = %s", (r.document_id,)
        ).fetchone()
        assert row is not None
        participants = [p.lower() for p in (row[0] or [])]
        assert "priya okafor" in participants


@pytest.mark.fresh_schema
def test_reseed_is_idempotent(test_db: psycopg.Connection) -> None:
    first = seed_demo(TEST_DATABASE_URL)
    assert first.ingested == 22

    second = seed_demo(TEST_DATABASE_URL)
    assert second.ingested == 0
    assert second.skipped == 22

    row = test_db.execute("SELECT count(*) FROM documents").fetchone()
    assert row is not None and row[0] == 22


@pytest.mark.fresh_schema
def test_seed_with_embeddings_finalizes_column(test_db: psycopg.Connection) -> None:
    """``with_embeddings=True`` leaves every chunk embedding non-NULL + indexed."""
    seed_demo(TEST_DATABASE_URL, with_embeddings=True)

    null_row = test_db.execute(
        "SELECT count(*) FROM chunks WHERE embedding IS NULL"
    ).fetchone()
    assert null_row is not None and null_row[0] == 0
