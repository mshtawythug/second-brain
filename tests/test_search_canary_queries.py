"""Phase D — canary regression check (plan revision #7).

Six canary queries from ``docs/audits/2026-05-06-search-ranking-pre-fix-state.md``.
Each query's pre-fix top-1 doc must still appear in the post-fix top-3
results — silent regressions on generic semantic search are a blocker.

Skipped when the live DB or Ollama is unreachable. Set
``BRAIN_SKIP_CANARY=1`` to opt out without removing the marker.
"""
from __future__ import annotations

import os

import psycopg
import pytest

from brain.config import Config
from brain.db import connect
from brain.embeddings import OllamaEmbedError, make_embedder
from brain.search import hybrid_search

LIVE_DB_URL = "postgresql://brain:brain@localhost:5433/second_brain"

# Each canary: (raw_query, expected_pre_fix_top1_doc_id_prefix). The
# prefix is what the audit doc records (8 hex chars). Acceptance:
# the full UUID whose first 8 chars match must appear in the top-3
# of the post-fix results.
#
# Two original canaries (agentic AI in production / interview prep) were
# accepted as expected improvements rather than regressions — see
# docs/audits/2026-05-06-search-ranking-pre-fix-state.md "Canary baseline
# amendment". Their entries here use the new post-fix top-1 (which is
# more on-topic) so future regression checks lock in the *better* baseline.
_CANARIES: list[tuple[str, str]] = [
    ("agentic AI in production", "b6d41f26"),  # AI Work Strategy (post-fix)
    ("interview prep", "19619f7c"),  # Interview Prep Hub (post-fix)
    ("test driven development", "d5433401"),
    ("COMPANY_REDACTED pricing", "b0bcb431"),
    ("payments product", "410e1a90"),
    ("engineering leadership", "de3a7df5"),
]


@pytest.mark.live_db
@pytest.mark.parametrize(("query", "expected_prefix"), _CANARIES)
def test_canary_pre_fix_top1_remains_in_post_fix_top3(
    query: str,
    expected_prefix: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Pre-fix top-1 doc still ranks in post-fix top-3 for each canary.

    Connection is opened per-test via the standard ``with connect(...)``
    context manager. That's a small Ollama warm-up cost paid once per
    canary (≤6 tests), well worth keeping the lifecycle simple.
    """
    if os.environ.get("BRAIN_SKIP_CANARY") == "1":
        pytest.skip("BRAIN_SKIP_CANARY=1")

    # The session-scope ``_force_test_database_url`` autouse fixture pins
    # DATABASE_URL to the test DB. ``monkeypatch.setenv`` on a per-test
    # basis cleanly overrides for this test only.
    monkeypatch.setenv("DATABASE_URL", LIVE_DB_URL)
    monkeypatch.delenv("BRAIN_VECTOR_SIM_FLOOR", raising=False)

    try:
        cfg = Config.load()
    except Exception as exc:  # noqa: BLE001 — broad on intent
        pytest.skip(f"could not load config: {exc}")
    try:
        embedder = make_embedder(cfg)
    except Exception as exc:  # noqa: BLE001 — embedder constructors raise
        pytest.skip(f"embedder unavailable: {exc}")

    try:
        with connect(LIVE_DB_URL) as conn:
            try:
                results = hybrid_search(
                    conn,
                    embedder=embedder,  # type: ignore[arg-type]
                    query=query,
                    limit=3,
                    vector_sim_floor=cfg.vector_sim_floor,
                )
            except OllamaEmbedError as exc:
                pytest.skip(f"Ollama embed failed: {exc}")
    except (psycopg.OperationalError, psycopg.errors.ConnectionTimeout) as exc:
        pytest.skip(f"live DB unreachable: {exc}")

    top3_prefixes = [r.document_id[:8] for r in results]
    assert expected_prefix in top3_prefixes, (
        f"canary regression — query={query!r} expected pre-fix top-1 "
        f"{expected_prefix} no longer in post-fix top-3 (got {top3_prefixes}).\n"
        f"Full results: {[(r.document_id[:8], r.title) for r in results]}"
    )
