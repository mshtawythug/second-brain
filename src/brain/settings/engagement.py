"""The engagement stage's knobs (roadmap ticket RM-22b).

``BRAIN_ENGAGEMENT_ALPHA`` weighs decayed opens and ``BRAIN_ENGAGEMENT_BETA`` weighs
net ratings (``useful − irrelevant``) in the stage's multiplier
(:mod:`brain.search_engagement`). Both default to ``0``, and the stage is on when
**either** is above zero (plan D1): a ratings-only stage is coherent, and with both
at their defaults it is off, as the spec's "α = 0 means off" requires.

Both are validated even while the stage is off, as the reranker's and the document
leg's knobs are, so a typo fails the first time a search builds its stages. The
range is ``[0, 1]`` (plan D7): a negative weight would demote what the owner
reads, and ``1`` already reaches the 1.25 clamp at about 0.3 decayed opens, so a
larger value is a typo.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass

from brain.settings._env import env_float

DEFAULT_WEIGHT = 0.0
MIN_WEIGHT = 0.0
MAX_WEIGHT = 1.0


@dataclass(frozen=True)
class EngagementSettings:
    """How much decayed opens (``alpha``) and net ratings (``beta``) move a result."""

    alpha: float
    beta: float

    @property
    def enabled(self) -> bool:
        """On when either weight is above zero (plan D1)."""
        return self.alpha > 0 or self.beta > 0


def _weight(env: Mapping[str, str], name: str) -> float:
    return env_float(env, name, DEFAULT_WEIGHT, minimum=MIN_WEIGHT, maximum=MAX_WEIGHT)


def load_engagement_settings(env: Mapping[str, str]) -> EngagementSettings:
    """Read ``BRAIN_ENGAGEMENT_ALPHA`` and ``BRAIN_ENGAGEMENT_BETA`` from ``env``."""
    return EngagementSettings(
        alpha=_weight(env, "BRAIN_ENGAGEMENT_ALPHA"),
        beta=_weight(env, "BRAIN_ENGAGEMENT_BETA"),
    )
