"""Graph-retrieval eval scorers (wave G2-j, GraphRAG; spec §6/§6b).

Scores the two G2 graph-retrieval paths against synthetic golden expectations:

* **Local (entity-centric, spec §6a).** ``graph_rag_search(mode='local')``
  returns a *ranked document list* (``GraphContext.docs`` — reused
  :class:`brain.search.SearchResult`s). A ranked doc list is exactly what the
  existing ranking metrics measure, so :func:`score_local_docs` **reuses**
  :func:`brain.eval.metrics.ndcg_at_k` / :func:`~brain.eval.metrics.mrr` /
  :func:`~brain.eval.metrics.recall_at_k` — no new ranking metric is invented.

* **Themes-with-X (spec §6b — the HEADLINE).** ``graph_rag_search(mode='themes')``
  returns ``ThemeGroup``s — *sets of co-occurring entity clusters*, not a ranked
  document list. nDCG/MRR/Recall over doc IDs cannot express "did the right
  *clusters* surface", so themes need a small graph-appropriate scorer:
  :func:`score_themes` matches each expected theme keyset to a predicted theme
  keyset by best Jaccard overlap (greedy, deterministic) above a threshold, then
  reports set precision / recall / F1 over the matched clusters. This is the
  cluster-grouping analogue of the concept-set F1 in
  :mod:`brain.eval.concept_extraction`.

Both scorers are **pure** (no DB, no Ollama, no I/O) and unit-testable with
hand-constructed inputs. The end-to-end graph-retrieval eval test builds a small
synthetic AGE corpus (via ``reconcile_document`` on the test DB), runs
``graph_rag_search``, and feeds the results here.
"""
from __future__ import annotations

from collections.abc import Iterable, Sequence
from dataclasses import dataclass

from brain.set_similarity import jaccard

from .metrics import mrr as _mrr
from .metrics import ndcg_at_k as _ndcg_at_k
from .metrics import recall_at_k as _recall_at_k

__all__ = [
    "DEFAULT_THEME_JACCARD",
    "LocalRetrievalScore",
    "ThemeRetrievalScore",
    "score_local_docs",
    "score_themes",
]

# Default minimum Jaccard overlap for an expected theme keyset to count as
# "matched" by a predicted theme keyset. 0.5 means the two clusters must share
# the majority of their combined entities — strict enough that a one-entity
# accidental overlap between otherwise-different clusters does not count.
DEFAULT_THEME_JACCARD = 0.5


@dataclass(frozen=True)
class LocalRetrievalScore:
    """nDCG@k / MRR / Recall@k for one local graph-retrieval query.

    A thin bundle of the three reused ranking metrics scored over the local
    path's ranked ``GraphContext.docs`` document IDs against the expected
    document-ID set.
    """

    ndcg_at_k: float
    mrr: float
    recall_at_k: float
    ndcg_k: int
    recall_k: int


@dataclass(frozen=True)
class ThemeRetrievalScore:
    """Set precision / recall / F1 for one themes-with-X query.

    Computed by greedy best-Jaccard matching of predicted theme keysets to
    expected theme keysets above :data:`DEFAULT_THEME_JACCARD`. ``matched`` is the
    number of expected themes matched; ``n_expected`` / ``n_actual`` are the
    cluster counts.
    """

    precision: float
    recall: float
    f1: float
    matched: int
    n_expected: int
    n_actual: int


def score_local_docs(
    actual_doc_ids: Sequence[str],
    expected_doc_ids: Iterable[str],
    *,
    ndcg_k: int = 5,
    recall_k: int = 20,
) -> LocalRetrievalScore:
    """Score a local query's ranked doc IDs with the reused ranking metrics.

    Args:
        actual_doc_ids: Document IDs from ``GraphContext.docs``, in rank order.
        expected_doc_ids: The relevant document IDs (order-independent).
        ndcg_k: Cutoff for nDCG.
        recall_k: Cutoff for recall.

    Returns:
        A :class:`LocalRetrievalScore`.

    Raises:
        EvalMetricError: When ``expected_doc_ids`` is empty (propagated from the
            underlying ranking metrics).
    """
    expected = list(expected_doc_ids)
    return LocalRetrievalScore(
        ndcg_at_k=_ndcg_at_k(actual_doc_ids, expected, k=ndcg_k),
        mrr=_mrr(actual_doc_ids, expected),
        recall_at_k=_recall_at_k(actual_doc_ids, expected, k=recall_k),
        ndcg_k=ndcg_k,
        recall_k=recall_k,
    )


def _to_keysets(themes: Iterable[Iterable[str]]) -> list[frozenset[str]]:
    """Normalize theme entity-key iterables to lower-cased frozensets.

    Empty clusters are dropped (a themeless group cannot be matched). Keys are
    lower-cased + whitespace-collapsed to match the catalog ``canonical_key``.
    """
    out: list[frozenset[str]] = []
    for theme in themes:
        keys = {" ".join(str(k).lower().split()) for k in theme}
        keys.discard("")
        if keys:
            out.append(frozenset(keys))
    return out


def score_themes(
    actual_themes: Iterable[Iterable[str]],
    expected_themes: Iterable[Iterable[str]],
    *,
    jaccard_threshold: float = DEFAULT_THEME_JACCARD,
) -> ThemeRetrievalScore:
    """Greedy best-Jaccard set precision / recall / F1 over theme clusters.

    Each predicted / expected theme is a set of entity ``canonical_key``s. Every
    ``(expected, actual)`` pair with Jaccard ``>= jaccard_threshold`` is a
    candidate match; candidates are consumed greedily highest-Jaccard-first (ties
    broken deterministically by index) so each expected and each actual theme is
    matched at most once. ``recall = matched / n_expected``,
    ``precision = matched / n_actual``, ``f1`` is their harmonic mean.

    Args:
        actual_themes: Predicted theme keysets (e.g. from ``GraphContext.themes``).
        expected_themes: The expected theme keysets.
        jaccard_threshold: Minimum overlap for a match (default
            :data:`DEFAULT_THEME_JACCARD`).

    Returns:
        A :class:`ThemeRetrievalScore`. With no expected and no actual themes,
        precision/recall/F1 are all 1.0 (vacuously correct); when exactly one
        side is empty they are 0.0.
    """
    expected = _to_keysets(expected_themes)
    actual = _to_keysets(actual_themes)
    n_expected = len(expected)
    n_actual = len(actual)

    if n_expected == 0 and n_actual == 0:
        return ThemeRetrievalScore(1.0, 1.0, 1.0, 0, 0, 0)

    candidates: list[tuple[float, int, int]] = []
    for ei, exp in enumerate(expected):
        for ai, act in enumerate(actual):
            score = jaccard(exp, act)
            if score >= jaccard_threshold:
                candidates.append((score, ei, ai))
    # Highest Jaccard first; ties → lowest expected index, then lowest actual
    # index, so the matching is deterministic across runs.
    candidates.sort(key=lambda c: (-c[0], c[1], c[2]))

    used_expected: set[int] = set()
    used_actual: set[int] = set()
    matched = 0
    for _score, ei, ai in candidates:
        if ei in used_expected or ai in used_actual:
            continue
        used_expected.add(ei)
        used_actual.add(ai)
        matched += 1

    recall = matched / n_expected if n_expected > 0 else 0.0
    precision = matched / n_actual if n_actual > 0 else 0.0
    f1 = (
        2 * precision * recall / (precision + recall)
        if (precision + recall) > 0
        else 0.0
    )
    return ThemeRetrievalScore(
        precision=precision,
        recall=recall,
        f1=f1,
        matched=matched,
        n_expected=n_expected,
        n_actual=n_actual,
    )
