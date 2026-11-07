"""A bounded fake clock for deadline tests: no sleeps, no real time (RM-55)."""
from __future__ import annotations


class SteppedClock:
    """Answers each reading in turn, then repeats the last one, so a loop over it stays bounded."""

    def __init__(self, *readings: float) -> None:
        if not readings:
            raise ValueError("a SteppedClock needs at least one reading")
        self._readings = list(readings)

    def __call__(self) -> float:
        return self._readings.pop(0) if len(self._readings) > 1 else self._readings[0]
