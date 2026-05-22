"""Pure set-similarity helpers shared across eval and graph modules."""
from collections.abc import Set as AbstractSet
from typing import TypeVar

__all__ = ["jaccard"]

_T = TypeVar("_T")


def jaccard(a: AbstractSet[_T], b: AbstractSet[_T]) -> float:
    """Jaccard overlap ``|a ∩ b| / |a ∪ b|`` (``0.0`` when both sets are empty)."""
    union = a | b
    if not union:
        return 0.0
    return len(a & b) / len(union)
