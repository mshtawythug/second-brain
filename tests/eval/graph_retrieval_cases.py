"""Synthetic golden fixtures for the GraphRAG graph-retrieval eval (wave G2-j).

SHIPPED in-repo — every person / org / topic / project here is SYNTHETIC (no
PII). This module is **pure data** (corpus spec + query cases + thresholds); the
end-to-end eval test (``tests/test_graphrag_retrieval_eval.py``) builds the
corpus on the AGE test DB via ``reconcile_document`` and scores the results with
the pure scorers in :mod:`brain.eval.graph_retrieval`.

Corpus shape: a synthetic person **Dana Lee** (with an owner co-participant)
appears across six documents spanning two disjoint topic clusters —
``{pricing, billing, acmepay}`` and ``{roadmap, analytics}``. Concepts come from
a deterministic fake extractor keyed on a per-document marker substring (no
Ollama). This is the same construction the headline themes integration test
uses, generalized into a reusable fixture.
"""
from __future__ import annotations

from dataclasses import dataclass

TENANT = "default"

# Synthetic participants (no PII). The owner is co-mentioned with Dana so the
# themes-path owner exclusion is genuinely exercised.
DANA: tuple[str, str] = ("dana lee", "dana@example.com")
OWNER: tuple[str, str] = ("owner one", "owner@example.com")

# Per-marker concept entities the fake extractor emits: marker -> list of
# (entity_type, canonical_key, display_name). Concepts in the same list are
# placed at adjacent word positions so they co-occur within the default window
# (forming a cluster).
CONCEPT_MARKERS: dict[str, list[tuple[str, str, str]]] = {
    "PRICING": [
        ("topic", "pricing", "Pricing"),
        ("topic", "billing", "Billing"),
        ("org", "acmepay", "Acmepay"),
    ],
    "ROADMAP": [
        ("topic", "roadmap", "Roadmap"),
        ("topic", "analytics", "Analytics"),
    ],
}


@dataclass(frozen=True)
class CorpusRow:
    """One synthetic document in the graph-retrieval eval corpus.

    ``external_id`` keys the source row; ``marker`` selects which concept cluster
    the fake extractor emits; ``body`` contains the marker substring.
    """

    external_id: str
    marker: str
    body: str


# Six Dana documents: three PRICING-cluster, three ROADMAP-cluster.
CORPUS_ROWS: tuple[CorpusRow, ...] = (
    CorpusRow("g-eval-p1", "PRICING", "PRICING enterprise tiers and billing cycles"),
    CorpusRow("g-eval-p2", "PRICING", "PRICING strategy notes and billing renewals"),
    CorpusRow("g-eval-p3", "PRICING", "PRICING review covering billing and discounts"),
    CorpusRow("g-eval-r1", "ROADMAP", "ROADMAP planning and analytics dashboards"),
    CorpusRow("g-eval-r2", "ROADMAP", "ROADMAP milestones and analytics review"),
    CorpusRow("g-eval-r3", "ROADMAP", "ROADMAP themes and analytics deep dive"),
)


@dataclass(frozen=True)
class GraphLocalCase:
    """A local (entity-centric) graph-retrieval eval case.

    ``query`` resolves to a seed entity; ``expected_doc_external_ids`` are the
    corpus rows whose documents should rank for that seed + its co-occurring
    neighbours. ``min_recall`` / ``min_ndcg`` are the pass thresholds.
    """

    query: str
    expected_doc_external_ids: tuple[str, ...]
    min_recall: float = 1.0
    min_ndcg: float = 0.99


@dataclass(frozen=True)
class GraphThemesCase:
    """A themes-with-X graph-retrieval eval case.

    ``person`` is scoped; ``expected_theme_keysets`` are the entity-key clusters
    that should surface as themes (seed + owner excluded). ``min_f1`` is the pass
    threshold for the Jaccard-matched theme set F1.
    """

    person: str
    expected_theme_keysets: tuple[frozenset[str], ...]
    min_f1: float = 1.0


# Local queries: "pricing" seeds the pricing topic and (via CO_OCCURS) reaches
# billing + acmepay, so the three PRICING-cluster docs should rank.
LOCAL_CASES: tuple[GraphLocalCase, ...] = (
    GraphLocalCase(
        query="pricing",
        expected_doc_external_ids=("g-eval-p1", "g-eval-p2", "g-eval-p3"),
    ),
    GraphLocalCase(
        query="roadmap",
        expected_doc_external_ids=("g-eval-r1", "g-eval-r2", "g-eval-r3"),
    ),
)

# Themes query: Dana's two disjoint topic clusters should surface as themes.
THEMES_CASES: tuple[GraphThemesCase, ...] = (
    GraphThemesCase(
        person="dana lee",
        expected_theme_keysets=(
            frozenset({"pricing", "billing", "acmepay"}),
            frozenset({"roadmap", "analytics"}),
        ),
    ),
)

# Aggregate eval pass thresholds (mean across cases) — kept conservative;
# the goal is regression detection on a deterministic synthetic corpus.
MEAN_LOCAL_RECALL_MIN: float = 1.0
MEAN_THEMES_F1_MIN: float = 1.0

# Fuse mode (wave G4-c; spec §17d Q1) reuses the LOCAL_CASES queries + expected
# docs — fuse is a ranked-doc mode (local doc-leg ⊕ hybrid doc-leg via RRF). Its
# hybrid leg can interleave non-seed docs by vector similarity, so the BLOCKING
# fuse gate is on recall only (fuse must never LOSE a relevant doc the graph leg
# found) — a robust, non-flaky regression signal; nDCG is recorded but not gated.
MEAN_FUSE_RECALL_MIN: float = 1.0

__all__ = [
    "CONCEPT_MARKERS",
    "CORPUS_ROWS",
    "DANA",
    "LOCAL_CASES",
    "MEAN_FUSE_RECALL_MIN",
    "MEAN_LOCAL_RECALL_MIN",
    "MEAN_THEMES_F1_MIN",
    "OWNER",
    "TENANT",
    "THEMES_CASES",
    "CorpusRow",
    "GraphLocalCase",
    "GraphThemesCase",
]
