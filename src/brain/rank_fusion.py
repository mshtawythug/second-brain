"""Pure Reciprocal Rank Fusion (RRF) scoring helper shared across rankers."""

__all__ = ["rrf_contribution"]


def rrf_contribution(rank: int, *, k: int = 60) -> float:
    """RRF contribution ``1 / (k + rank + 1)`` for a 0-indexed ``rank`` (default ``k=60``)."""
    return 1.0 / (k + rank + 1)
