"""Latency-measurement helpers for the GraphRAG P95 perf gate (wave G2-k).

The gate (``tests/test_graphrag_benchmark_gate.py``, ``-m benchmark``) measures
steady-state retrieval latency over the full-scale synthetic graph and asserts
two budgets (spec §16/§17b Q5): **P95 local traversal ≤ 750 ms** and **P95
themes-with-X ≤ 2 s**. This module owns the measurement mechanics so the gate
test stays declarative:

* :func:`percentile` — linear-interpolation percentile over a sorted sample.
* :class:`LatencyStats` — P50/P95/P99/max/mean summary (ms) + a human line.
* :func:`measure` — run a warm-up burst (discarded), then time each measured
  call with :func:`time.perf_counter`, returning a :class:`LatencyStats`.

**This module is NOT a test** (its filename does not match ``test_*``) so it is
never collected by the default suite. It holds no DB or domain logic — only
timing + statistics — so it has no live dependency and is import-cheap.
"""
from __future__ import annotations

import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass

__all__ = ["LatencyStats", "measure", "percentile"]


def percentile(values_sorted: Sequence[float], q: float) -> float:
    """Return the ``q``-quantile (0..1) of an already-sorted sample (ms).

    Uses the linear-interpolation-between-closest-ranks method (the NumPy
    default), so P95 of a small sample is a sensible interpolated value rather
    than a coarse nearest-rank pick. An empty sample raises ``ValueError`` (a
    percentile of nothing is a caller bug, never silently 0).
    """
    if not values_sorted:
        raise ValueError("percentile of an empty sample is undefined")
    if not 0.0 <= q <= 1.0:
        raise ValueError(f"q must be in [0, 1], got {q}")
    if len(values_sorted) == 1:
        return float(values_sorted[0])
    rank = q * (len(values_sorted) - 1)
    low = int(rank)
    high = min(low + 1, len(values_sorted) - 1)
    frac = rank - low
    return float(values_sorted[low] + (values_sorted[high] - values_sorted[low]) * frac)


@dataclass(frozen=True)
class LatencyStats:
    """Summary statistics (milliseconds) over a set of measured calls."""

    samples: int
    p50_ms: float
    p95_ms: float
    p99_ms: float
    max_ms: float
    mean_ms: float

    @classmethod
    def from_durations(cls, durations_ms: Sequence[float]) -> LatencyStats:
        """Build a summary from a list of per-call durations (ms)."""
        if not durations_ms:
            raise ValueError("cannot summarize an empty duration list")
        ordered = sorted(durations_ms)
        return cls(
            samples=len(ordered),
            p50_ms=percentile(ordered, 0.50),
            p95_ms=percentile(ordered, 0.95),
            p99_ms=percentile(ordered, 0.99),
            max_ms=float(ordered[-1]),
            mean_ms=sum(ordered) / len(ordered),
        )

    def describe(self, label: str) -> str:
        """One-line human summary for the gate's captured report output."""
        return (
            f"{label}: n={self.samples} "
            f"p50={self.p50_ms:.1f}ms p95={self.p95_ms:.1f}ms "
            f"p99={self.p99_ms:.1f}ms max={self.max_ms:.1f}ms "
            f"mean={self.mean_ms:.1f}ms"
        )


def measure(calls: Sequence[Callable[[], object]], *, warmup: int) -> LatencyStats:
    """Time each callable after a discarded warm-up burst; return the summary.

    ``calls`` is a list of zero-arg closures, each performing one retrieval. The
    first ``warmup`` are executed but NOT timed (they prime caches / plans /
    connection state so the measurement reflects steady state). Every remaining
    call is timed individually with :func:`time.perf_counter` (monotonic), and
    the per-call durations are summarized into a :class:`LatencyStats`.

    Raises ``ValueError`` if no measured calls remain after the warm-up.
    """
    if warmup < 0:
        raise ValueError(f"warmup must be >= 0, got {warmup}")
    if len(calls) <= warmup:
        raise ValueError(
            f"need more than warmup ({warmup}) calls to measure, got {len(calls)}"
        )
    for call in calls[:warmup]:
        call()
    durations_ms: list[float] = []
    for call in calls[warmup:]:
        start = time.perf_counter()
        call()
        durations_ms.append((time.perf_counter() - start) * 1000.0)
    return LatencyStats.from_durations(durations_ms)
