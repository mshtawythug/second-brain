"""`brain doctor`'s ``reranker`` row (roadmap ticket RM-14b): is the local reranker up?

Only ``BRAIN_RERANKER=llamacpp`` has a local runtime to probe: ``GET {BRAIN_RERANK_URL}/health`` on
``llama-server``, with a 2 s timeout. ``none`` reports "not configured", and ``voyage`` is hosted,
so there is nothing local to check. A reranker that is down is a WARN at worst, never a FAIL:
search skips it and returns hybrid order (RM-14a), so it never fails doctor either.

A module of its own, as :mod:`brain.doctor_delivery` is, so the probe does not spend
:mod:`brain.doctor_runtime`'s line budget (roadmap spec §2.2); ``runtime_doctor_checks`` composes
it in. It never imports ``brain.cli`` at module scope; ``_check`` defers that.
"""

from __future__ import annotations

import os
from collections.abc import Mapping
from typing import TYPE_CHECKING

import httpx

from .config import ConfigError
from .doctor_runtime import _check
from .settings.rerank import BACKEND_LLAMACPP, BACKEND_NONE, load_rerank_settings

if TYPE_CHECKING:  # pragma: no cover — typing only
    from .cli import _DoctorCheck

#: The ticket's bound on the probe; ``/health`` answers at once when the server is up.
HEALTH_TIMEOUT_S = 2.0

_NAME = "reranker"
_REMEDY = (
    "brain-install-launchd --with-reranker  # or start llama-server by hand; "
    "see docs/guides/local-reranker.md"
)


def reranker_doctor_check(
    *,
    env: Mapping[str, str] | None = None,
    transport: httpx.BaseTransport | None = None,
) -> list[_DoctorCheck]:
    """The ``reranker`` row. ``transport`` is a test seam (an ``httpx.MockTransport``).

    ``env`` defaults to ``os.environ``, which ``Config.load()`` has already filled from the
    dotenv chain. A ``ConfigError``'s message names variables and never a value (the settings
    leaf's rule), so it is shown. The URL in the detail has passed the loopback check, which
    refuses credentials, so it carries none.
    """
    try:
        settings = load_rerank_settings(os.environ if env is None else env)
    except ConfigError as exc:
        return [_check(_NAME, "warn", str(exc), remedy="fix it in $BRAIN_HOME/.env")]
    if settings.backend == BACKEND_NONE:
        return [_check(_NAME, "ok", "not configured")]
    if settings.backend != BACKEND_LLAMACPP:
        return [_check(_NAME, "ok", f"{settings.backend} (hosted), no local runtime to check")]

    try:
        with httpx.Client(timeout=HEALTH_TIMEOUT_S, transport=transport) as client:
            response = client.get(f"{settings.url}/health")
    except httpx.HTTPError as exc:
        return [
            _check(
                _NAME,
                "warn",
                f"not running at {settings.url} ({type(exc).__name__}); searches stay "
                f"in hybrid order",
                remedy=_REMEDY,
            )
        ]
    if response.status_code != 200:
        # llama-server answers 503 while it is still loading the model.
        return [
            _check(
                _NAME,
                "warn",
                f"not ready (HTTP {response.status_code}) at {settings.url}",
                remedy=_REMEDY,
            )
        ]
    return [_check(_NAME, "ok", f"llama-server healthy at {settings.url}")]
