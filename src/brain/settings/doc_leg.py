"""The document-leg stage's knobs (roadmap ticket RM-20).

``BRAIN_DOC_LEG`` turns the stage on (it ships off); ``BRAIN_DOC_LEG_WEIGHT``
scales its reciprocal-rank contribution against the pipeline's own rank. Both are
validated even while the leg is off, as the reranker's knobs are: a typo fails the
first time a search builds its stages, not the day someone turns the leg on.

The weight's range is closed: ``0`` is a usable control (the stage runs and
leaves the order as it was), and the upper bound catches a typo such as ``100``.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass

from brain.settings._env import env_bool, env_float

DEFAULT_WEIGHT = 1.0
MIN_WEIGHT = 0.0
MAX_WEIGHT = 10.0


@dataclass(frozen=True)
class DocLegSettings:
    """Whether the document leg runs, and how much its rank counts."""

    enabled: bool
    weight: float


def load_doc_leg_settings(env: Mapping[str, str]) -> DocLegSettings:
    """Read ``BRAIN_DOC_LEG`` and ``BRAIN_DOC_LEG_WEIGHT`` from ``env``."""
    return DocLegSettings(
        enabled=env_bool(env, "BRAIN_DOC_LEG", False),
        weight=env_float(
            env,
            "BRAIN_DOC_LEG_WEIGHT",
            DEFAULT_WEIGHT,
            minimum=MIN_WEIGHT,
            maximum=MAX_WEIGHT,
        ),
    )
