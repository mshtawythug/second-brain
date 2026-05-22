"""Pure unit tests for brain.rank_fusion.rrf_contribution + a fused-ordering lock."""
from __future__ import annotations

from brain.rank_fusion import rrf_contribution


def test_rrf_contribution_rank_zero_default_k() -> None:
    # 0-indexed rank 0 → 1 / (60 + 0 + 1) = 1/61.
    assert rrf_contribution(0) == 1.0 / 61


def test_rrf_contribution_rank_one_default_k() -> None:
    assert rrf_contribution(1) == 1.0 / 62


def test_rrf_contribution_arbitrary_rank() -> None:
    assert rrf_contribution(9) == 1.0 / 70


def test_rrf_contribution_custom_k() -> None:
    assert rrf_contribution(0, k=10) == 1.0 / 11
    assert rrf_contribution(5, k=10) == 1.0 / 16


def test_rrf_contribution_monotonic_decreasing_in_rank() -> None:
    values = [rrf_contribution(r) for r in range(20)]
    assert all(
        earlier > later for earlier, later in zip(values, values[1:], strict=False)
    )


def test_rrf_contribution_matches_legacy_inline_formula() -> None:
    # Behavior-preservation lock: the helper must equal search.py's prior
    # inline expression ``1.0 / (RRF_K + rank + 1)`` for every rank/k.
    for k in (1, 60, 100):
        for rank in range(50):
            assert rrf_contribution(rank, k=k) == 1.0 / (k + rank + 1)


def _fused_scores(
    fts_order: list[str], vec_order: list[str], *, k: int = 60
) -> dict[str, float]:
    """Replicate search.py's per-chunk RRF accumulation (pure, no DB)."""
    rrf: dict[str, float] = {}
    for rank, cid in enumerate(fts_order):
        rrf[cid] = rrf.get(cid, 0.0) + rrf_contribution(rank, k=k)
    for rank, cid in enumerate(vec_order):
        rrf[cid] = rrf.get(cid, 0.0) + rrf_contribution(rank, k=k)
    return rrf


def test_fused_ordering_matches_inline_accumulation() -> None:
    # Hand-built FTS + vector rank lists over chunk ids. The extraction must
    # reproduce the legacy inline accumulation byte-for-byte, and a chunk that
    # ranks high in BOTH legs must outrank single-leg chunks.
    fts_order = ["c1", "c2", "c3"]
    vec_order = ["c2", "c4", "c1"]

    fused = _fused_scores(fts_order, vec_order)

    # Recompute with the legacy inline formula directly to prove equivalence.
    legacy: dict[str, float] = {}
    for rank, cid in enumerate(fts_order):
        legacy[cid] = legacy.get(cid, 0.0) + 1.0 / (60 + rank + 1)
    for rank, cid in enumerate(vec_order):
        legacy[cid] = legacy.get(cid, 0.0) + 1.0 / (60 + rank + 1)

    assert fused == legacy

    # c2 (fts rank 1 + vec rank 0) is the only chunk in both top slots → top.
    order = sorted(fused, key=lambda c: fused[c], reverse=True)
    assert order[0] == "c2"
