"""Live golden-corpus eval harness.

Marked ``@pytest.mark.eval`` so it is excluded from the default test
selection (``addopts = "-m 'not eval'"``).  Run explicitly with::

    pytest -m eval tests/test_eval_harness_live.py -v

Skips cleanly when:
- The live Postgres database is unreachable.
- The embedder fails to initialise (Ollama not running, config error).
- The query's expected_doc_ids are all nil-UUID placeholders (uncurated
  corpus entry — the sentinel ``00000000-0000-0000-0000-000000000000``
  is shipped in the bootstrap corpus for queries that haven't been curated
  against the live brain yet).
- The query's expected_doc_ids don't resolve to any document in the live DB.
"""

from __future__ import annotations

import os
from typing import Any

import pytest

# Per-category nDCG@5 / Recall@20 thresholds.  Conservative by design:
# goal is regression detection, not score-chasing.
SEMANTIC_NDCG_MIN: float = 0.5
INTERVIEW_NDCG_MIN: float = 0.5
BROAD_RECALL_MIN: float = 0.5  # people / meeting / email / source-specific / recency

_NDCG_CATEGORIES = frozenset({"semantic", "interview"})
_RECALL_CATEGORIES = frozenset({"people", "meeting", "email", "source-specific", "recency"})

LIVE_DATABASE_URL = os.environ.get(
    "DATABASE_URL",
    "postgresql://brain:brain@localhost:5433/second_brain",
)

# Sentinel used in the bootstrap corpus for queries not yet curated against the live brain.
# _normalize_ids passes full UUIDs through without a DB existence check, so we must
# strip nil UUIDs explicitly before the "is anything live?" guard.
_NIL_UUID = "00000000-0000-0000-0000-000000000000"


def _load_live_queries() -> list[Any]:
    """Return EvalQuery list from the default corpus, or [] on any load failure."""
    try:
        from brain.eval.corpus import load_corpus

        return list(load_corpus())
    except Exception:
        return []


@pytest.fixture(scope="module")
def live_db():
    """Connect to the live second_brain database; skip if unreachable."""
    psycopg = pytest.importorskip("psycopg")
    try:
        conn = psycopg.connect(LIVE_DATABASE_URL, connect_timeout=5)
    except Exception as exc:
        pytest.skip(f"live DB unreachable: {exc}")
    yield conn
    conn.close()


@pytest.fixture(scope="module")
def live_embedder():
    """Construct the production embedder; skip if Ollama/config is unavailable."""
    try:
        from brain.config import Config
        from brain.embeddings import make_embedder

        cfg = Config.load()
        emb = make_embedder(cfg)
        # Smoke-test: embed a trivial string to verify the backend responds.
        emb.embed(["ping"], input_type="query")
        return emb
    except Exception as exc:
        pytest.skip(f"embedder unavailable: {exc}")


def _corpus_has_curated_ids(live_db_conn, queries) -> bool:
    """Return True if at least one expected_doc_id in the corpus resolves to a live doc."""
    from brain.eval.runner import _normalize_ids

    for q in queries:
        try:
            resolved = _normalize_ids(live_db_conn, list(q.expected_doc_ids))
            resolved = [r for r in resolved if r != _NIL_UUID]
            if resolved:
                return True
        except Exception:
            continue
    return False


_LIVE_QUERIES = _load_live_queries()


@pytest.mark.eval
@pytest.mark.parametrize("query_obj", _LIVE_QUERIES, ids=[q.query for q in _LIVE_QUERIES])
def test_live_harness_runs_against_brain(query_obj, live_db, live_embedder) -> None:
    """Run one query from the golden corpus and assert per-category thresholds.

    Skips the test (not fails) when the query's expected_doc_ids have no live
    counterparts — that means the corpus entry hasn't been curated yet.
    """
    from brain.eval.runner import _normalize_ids, run_eval

    # Skip if this query's expected IDs don't resolve to anything live.
    # Strip nil UUID placeholders first — _normalize_ids passes full UUIDs through without
    # a DB existence check, so the nil sentinel would otherwise defeat this guard.
    resolved = _normalize_ids(live_db, list(query_obj.expected_doc_ids))
    resolved = [r for r in resolved if r != _NIL_UUID]
    if not resolved:
        pytest.skip(
            f"No live documents match expected_doc_ids for query {query_obj.query!r} "
            "— corpus entry needs curation"
        )

    report = run_eval(live_db, embedder=live_embedder, queries=[query_obj])
    assert len(report.results) == 1
    r = report.results[0]

    if query_obj.category in _NDCG_CATEGORIES:
        assert r.ndcg_at_5 >= SEMANTIC_NDCG_MIN, (
            f"nDCG@5={r.ndcg_at_5:.4f} below threshold {SEMANTIC_NDCG_MIN} "
            f"for query {r.query!r} (category={r.category})"
        )
    elif query_obj.category in _RECALL_CATEGORIES:
        assert r.recall_at_20 >= BROAD_RECALL_MIN, (
            f"Recall@20={r.recall_at_20:.4f} below threshold {BROAD_RECALL_MIN} "
            f"for query {r.query!r} (category={r.category})"
        )
