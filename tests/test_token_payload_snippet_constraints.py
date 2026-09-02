"""Wave 4 — ``scripts/token_payload_report.py --snippet-constraints``.

This mode is what decided Wave 4, so the arithmetic behind its numbers is
tested rather than trusted. The script lives outside BOTH gates
``bin/brain-ci`` runs over the package (``--cov=brain`` and ``mypy src/``),
which is exactly why its logic needs tests of its own — the same argument
``tests/test_token_payload_report.py`` makes for the payload arms.

It began as ``--adaptive-stats``, the engagement measurement for an Otsu cut
that was subsequently removed (see ``brain.snippet_context``'s module
docstring). What survives measures the two constraints that were found to
actually bind: the walk budget and the character cap.

Separate file from ``test_token_payload_report.py`` on purpose: that file is
the Wave-0 harness's own test and is being touched by other work.
"""
from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from types import ModuleType
from typing import Any

import psycopg
import pytest

SCRIPTS_DIR = Path(__file__).resolve().parent.parent / "scripts"
SCRIPT_PATH = SCRIPTS_DIR / "token_payload_report.py"

# The script imports its sibling ``scripts/query_files``; loading it via
# ``importlib`` does not put ``scripts/`` on ``sys.path`` the way running it
# directly would.
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))


@pytest.fixture
def report() -> ModuleType:
    """Load ``scripts/token_payload_report.py`` as an importable module."""
    spec = importlib.util.spec_from_file_location("token_payload_report", SCRIPT_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _row(report: ModuleType, **overrides: Any) -> Any:
    """A ``SnippetConstraint`` with every field defaulted, then overridden."""
    fields: dict[str, Any] = {
        "query": "q",
        "document_id": "doc",
        "neighbors_differ": True,
        "neighbors_admitted": 1,
        "neighbors_available": 2,
        "tokens": 400,
        "uncapped_tokens": 900,
        "matched_only_tokens": 400,
        "pinned_at_cap": True,
        "matched_chunk_fills_cap": True,
    }
    fields.update(overrides)
    return report.SnippetConstraint(**fields)


def test_engagement_rate_is_the_fraction_of_all_results_not_of_engaged_ones(
    report: ModuleType,
) -> None:
    """The denominator is ALL results.

    Dividing by engaged rows would make any non-empty run report 100% — the
    shape of a metric that cannot fail. This number read 74.5% on the live
    corpus while the mechanism it gated changed nothing, so it is kept
    precisely as evidence that a high value here implies nothing about payload
    size.
    """
    rows = [
        _row(report, neighbors_differ=True),
        _row(report, neighbors_differ=True),
        _row(report, neighbors_differ=False),
        _row(report, neighbors_differ=False),
    ]

    totals = report.constraint_totals(rows)

    assert totals["results"] == 4
    assert totals["neighbors_differ"] == 2
    assert totals["engagement_rate"] == pytest.approx(0.5)


def test_empty_run_reports_zeros_not_a_zero_division(report: ModuleType) -> None:
    """A query set that returns nothing must report 0%, not crash the harness."""
    totals = report.constraint_totals([])

    assert totals["results"] == 0
    assert totals["engagement_rate"] == 0.0
    assert totals["pinned_at_cap_rate"] == 0.0
    assert totals["results_with_any_neighbor_rate"] == 0.0


def test_counts_results_that_admitted_no_neighbour_at_all(report: ModuleType) -> None:
    """The WALK BUDGET binding. Measured 3 of 55 on the live corpus.

    If the expansion admits nothing, anything that tunes neighbour SELECTION is
    tuning a code path that does not run. Without this counter a 74.5%
    engagement rate reads as success.
    """
    rows = [
        _row(report, neighbors_admitted=0),
        _row(report, neighbors_admitted=0),
        _row(report, neighbors_admitted=2),
    ]

    totals = report.constraint_totals(rows)

    assert totals["results_with_any_neighbor"] == 1
    assert totals["results_with_any_neighbor_rate"] == pytest.approx(1 / 3)


def test_counts_results_whose_matched_chunk_alone_fills_the_cap(
    report: ModuleType,
) -> None:
    """The CHAR CAP binding. Measured 47 of 55 (85.5%) on the live corpus.

    Where the matched chunk alone reaches the cap, neighbour admission cannot
    change the delivered payload by construction — which is what distinguishes
    "the selection is badly tuned" from "the selection is irrelevant here".
    """
    rows = [
        _row(report, matched_chunk_fills_cap=True),
        _row(report, matched_chunk_fills_cap=False),
    ]

    totals = report.constraint_totals(rows)

    assert totals["results_matched_chunk_fills_cap"] == 1


def test_tokens_discarded_by_the_cap_is_produced_minus_delivered(
    report: ModuleType,
) -> None:
    """The headroom a content-aware truncation would be working with.

    Reported separately from the delivered total because the two answer
    different questions: what the agent pays, versus how much the cap is
    throwing away unread. Conflating them is what let the original wave
    conclude a cut had worked when the cap had discarded its effect.

    **The fixture numbers are chosen, not arbitrary.** The first version of
    this test used ``(400, 900)`` and ``(100, 100)``, where the discarded total
    (500) happens to equal the delivered total (500) — so a mutation aliasing
    ``tokens_discarded_by_cap`` to ``sum(tokens)`` left it GREEN. The three
    totals below are now pairwise distinct, which is what makes the assertions
    able to tell them apart.
    """
    rows = [
        _row(report, tokens=400, uncapped_tokens=900),
        _row(report, tokens=100, uncapped_tokens=250),
    ]

    totals = report.constraint_totals(rows)

    assert totals["delivered_tokens"] == 500
    assert totals["uncapped_tokens"] == 1150
    assert totals["tokens_discarded_by_cap"] == 650
    assert len({500, 1150, 650}) == 3, (
        "the three totals must be pairwise distinct or an aliasing mutation "
        "cannot be detected by the assertions above"
    )


def test_measure_snippet_constraints_runs_against_a_real_db(
    report: ModuleType,
    test_db: psycopg.Connection,
    fake_embedder: Any,
) -> None:
    """End-to-end: one row per result, with both size figures populated.

    Not compared to a golden string — the point is that the mode yields
    coherent rows whose uncapped size is never below the delivered size, which
    is what makes the totals meaningful.
    """
    from brain.config import Config
    from brain.ingest import ExtractedDoc, ingest_document

    ingest_document(
        test_db,
        embedder=fake_embedder,
        doc=ExtractedDoc(
            title="Snippet Constraints Doc",
            content="\n\n".join(
                [
                    "Provisioning ridge notes for the northern cluster.",
                    "Beacon signal calibration entry seventeen.",
                    "Gardening implements appendix, unrelated.",
                ]
            ),
            content_type="note",
            source_path=None,
            metadata={},
        ),
        source_kind="manual",
        source_external_id="snippet-constraints-test:1",
    )
    cfg = Config(database_url="", snippet_context_tokens=200)

    rows = report.measure_snippet_constraints(
        test_db,
        cfg,
        embedder=fake_embedder,
        query="provisioning",
        limit=5,
        sensitivity=None,
    )

    assert rows, "expected at least one measured result"
    for row in rows:
        assert row.tokens > 0
        assert row.uncapped_tokens >= row.tokens, (
            "the uncapped size can never be smaller than the delivered size — "
            "the delivered string is a prefix of it"
        )
        assert row.neighbors_admitted <= row.neighbors_available
