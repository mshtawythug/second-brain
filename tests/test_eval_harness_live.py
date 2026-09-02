"""Live golden-corpus eval harness — an INTEGRATION SMOKE TEST, not a quality gate.

Marked ``@pytest.mark.eval`` so it is excluded from the default test
selection (``addopts = "-m 'not eval'"``).  Run explicitly with::

    pytest -m eval tests/test_eval_harness_live.py -v

What this test asserts: that the harness runs end to end against a live brain —
the corpus parses, every ``expected_doc_id`` resolves, ``hybrid_search`` answers,
and the metrics compute — and that each skip path fires when its precondition is
absent.  It turns red when the *harness* breaks.  That is exactly the value
``.github/workflows/eval.yml`` claims for the ``pytest -m eval`` job.

**Do not re-add per-query score floors here.**  This module used to assert
``nDCG@5 >= 0.5`` / ``Recall@20 >= 0.5`` per category.  Three things were wrong
with that, and they will be wrong again if the floors come back:

1. *A fixed floor cannot detect a regression.*  A query at 0.95 can rot to 0.51
   and stay green.  ``brain eval --baseline ci --diff --fail-below`` trips on a
   mean delta past ``-1e-4`` — roughly 300x more sensitive, and with a reference
   point.  Regression detection belongs there, in one place, not here.
2. *The only way to make a floor green is the anti-pattern.*  Against honestly
   judged relevance the floors failed 11 of 31 queries, and the sole remedy
   available to a corpus author is to drop hard queries or pad
   ``expected_doc_ids`` until the ranker looks good — i.e. record current
   behaviour as ground truth, which is the one thing a golden corpus must never
   do.
3. *They gated a configuration nobody runs.*  ``run_eval``'s retrieval knobs
   default to ``recency_halflife_days=None`` / ``vector_sim_floor=0.0`` /
   ``snippet_context_tokens=0``, and this call site used to accept those
   defaults while ``brain eval`` passes ``cfg.*`` (180 / 0.25 / 200).  The two
   instruments disagreed about which queries were bad.  The call below now
   threads ``Config`` through so it measures what production measures; keep it
   that way.

A fourth, quieter problem is worth remembering as a category: ``INTERVIEW_NDCG_MIN``
was defined alongside the other floors and never referenced by anything.  Editing
it would have changed no behaviour whatsoever.

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
@pytest.mark.live_ollama
@pytest.mark.parametrize("query_obj", _LIVE_QUERIES, ids=[q.query for q in _LIVE_QUERIES])
def test_live_harness_runs_against_brain(query_obj, live_db, live_embedder) -> None:
    """Run one golden-corpus query end to end against the live brain.

    Asserts that the harness *works*, not that the ranker scores well: the query
    reaches ``hybrid_search``, one result row comes back, and the three metrics
    are computable numbers in ``[0, 1]``.  Score floors deliberately live nowhere
    in this module — see the module docstring for why, and use
    ``brain eval --baseline ci --diff --fail-below`` for regression detection.

    Retrieval knobs are threaded from :class:`~brain.config.Config` so this scores
    the same configuration ``brain eval`` scores.  ``run_eval``'s defaults are NOT
    production defaults; accepting them here is what made the two instruments
    disagree.

    Skips the test (not fails) when the query's expected_doc_ids have no live
    counterparts — that means the corpus entry hasn't been curated yet.
    """
    from brain.config import Config
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

    cfg = Config.load()
    report = run_eval(
        live_db,
        embedder=live_embedder,
        queries=[query_obj],
        recency_halflife_days=cfg.recency_halflife_days,
        snippet_context_tokens=cfg.snippet_context_tokens,
        vector_sim_floor=cfg.vector_sim_floor,
        embedder_name=cfg.embedder,
    )

    assert len(report.results) == 1
    r = report.results[0]

    # Identity round-trip: the harness scored the query it was handed.
    assert r.query == query_obj.query
    assert r.category == query_obj.category

    # Every id we resolved above reached the scorer — this catches a corpus/DB
    # drift that would otherwise silently score 0.0.  Superset, not equality:
    # ``resolved`` has nil-UUID sentinels stripped and ``run_eval`` does not, so
    # a bootstrap corpus carrying sentinels would fail an equality check for a
    # reason that has nothing to do with drift.
    assert set(resolved) <= set(r.expected_doc_ids)

    # Metrics are computable numbers in range. NOT floors: a legitimately hard
    # query may score 0.0 here and that is a finding, not a test failure.
    for name, value in (
        ("ndcg_at_5", r.ndcg_at_5),
        ("mrr", r.mrr),
        ("recall_at_20", r.recall_at_20),
    ):
        assert isinstance(value, float), f"{name} is {type(value).__name__}, not float"
        assert 0.0 <= value <= 1.0, (
            f"{name}={value!r} outside [0, 1] for query {r.query!r} "
            f"(category={r.category})"
        )

    # The config actually reached the runner. Guards the regression this module's
    # docstring describes: accepting run_eval's non-production defaults.
    assert report.config_signature["recency_halflife_days"] == cfg.recency_halflife_days
    assert report.config_signature["vector_sim_floor"] == cfg.vector_sim_floor
    assert report.config_signature["snippet_context_tokens"] == cfg.snippet_context_tokens
    assert report.config_signature["embedder"] == cfg.embedder
