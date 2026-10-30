"""Saved-search ("watch") knobs (roadmap ticket RM-15).

``BRAIN_WATCH_MIN_COSINE`` is the cosine a SEMANTIC watch's match must reach to count as a hit:
a semantic check keeps a result only when its best chunk's ``vector_cosine`` is at least this.
Lexical watches (the default) ignore it.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass

from brain.settings._env import env_float

#: The ticket's value. Above ``BRAIN_VECTOR_SIM_FLOOR``'s 0.25, which only keeps a chunk in the
#: vector leg; a watch notifies, so it asks for a closer match than a search shows.
DEFAULT_MIN_COSINE = 0.45


@dataclass(frozen=True)
class WatchSettings:
    """How close a semantic watch's match must be."""

    min_cosine: float


def load_watch_settings(env: Mapping[str, str]) -> WatchSettings:
    """Read the watch knobs from ``env`` (``os.environ`` after ``Config.load()``)."""
    return WatchSettings(
        min_cosine=env_float(
            env, "BRAIN_WATCH_MIN_COSINE", DEFAULT_MIN_COSINE, minimum=0.0, maximum=1.0
        ),
    )
