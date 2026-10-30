"""The cross-encoder reranker's knobs (roadmap ticket RM-14a, decision G7).

``BRAIN_RERANKER`` picks the backend: ``none`` (the default, so the stage is
off), ``llamacpp`` (a local ``llama-server`` over loopback HTTP) or ``voyage``
(hosted, through the already-declared ``voyageai``). The rest size and bound
the stage: how many head rows it scores, how long it may take, and how much of
each passage it sends.

Every knob is validated even while the backend is ``none``, as ``config.py``'s
knobs are: a typo fails loudly the first time a search builds its stages,
rather than the day someone turns the reranker on.

``BRAIN_RERANK_URL`` must be loopback. The passages are document text, so a
reranker on another host would be an egress path nobody chose (spec §2.5).
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from urllib.parse import urlsplit

from brain.config import ConfigError
from brain.settings._env import env_choice, env_float, env_int, env_str

#: The model ``llama-server`` serves for reranking (spec G7).
LLAMACPP_RERANK_DEFAULT_MODEL = "bge-reranker-v2-m3"
#: The reranker Voyage's docs recommend. Re-checked 2026-10-29 against the
#: Python client reference (docs.voyageai.com): ``rerank-3``, with
#: ``rerank-3-lite`` for latency-sensitive use.
VOYAGE_RERANK_DEFAULT_MODEL = "rerank-3"
#: Where RM-14b's launchd job binds ``llama-server``.
DEFAULT_RERANK_URL = "http://127.0.0.1:8081"
#: The GGUF the launchd job serves when ``BRAIN_RERANK_GGUF`` is unset, under
#: ``$BRAIN_HOME/models/`` (RM-14b; ``brain.bin.launchd.resolve_rerank_model``).
#: The file ``docs/guides/local-reranker.md`` downloads and measured.
DEFAULT_RERANK_GGUF_NAME = "bge-reranker-v2-m3-Q8_0.gguf"

BACKEND_NONE = "none"
BACKEND_LLAMACPP = "llamacpp"
BACKEND_VOYAGE = "voyage"
BACKENDS = frozenset({BACKEND_NONE, BACKEND_LLAMACPP, BACKEND_VOYAGE})

DEFAULT_DEPTH = 20
#: ``search.CANDIDATE_LIMIT``: each ranking leg sees at most 50 rows, so a
#: deeper rerank would only ever score the same rows.
MAX_DEPTH = 50
DEFAULT_TIMEOUT_S = 5.0
DEFAULT_MAX_CHARS = 1500

_LOOPBACK_HOSTS = frozenset({"127.0.0.1", "::1", "localhost"})
_URL_SCHEMES = frozenset({"http", "https"})


@dataclass(frozen=True)
class RerankSettings:
    """The reranker's configuration. ``backend == "none"`` means the stage is off."""

    backend: str
    depth: int
    timeout_s: float
    url: str
    model: str
    max_chars: int
    voyage_api_key: str | None = field(default=None, repr=False)

    @property
    def enabled(self) -> bool:
        return self.backend != BACKEND_NONE


def _loopback_url(env: Mapping[str, str]) -> str:
    """``BRAIN_RERANK_URL``, refused unless it is plain http(s) to a loopback host.

    The message names the variable and never the value: a URL can carry a
    credential in its userinfo, and this module never echoes a value.
    """
    raw = env_str(env, "BRAIN_RERANK_URL", DEFAULT_RERANK_URL)
    message = (
        "BRAIN_RERANK_URL must be an http(s) URL to a loopback host "
        "(127.0.0.1, ::1 or localhost), with no credentials"
    )
    try:
        parts = urlsplit(raw)
        _ = parts.port  # raises ValueError for a malformed port
    except ValueError:
        raise ConfigError(message) from None
    if (
        parts.scheme not in _URL_SCHEMES
        or parts.hostname not in _LOOPBACK_HOSTS
        or parts.username is not None
        or parts.password is not None
    ):
        raise ConfigError(message)
    return raw.rstrip("/")


def load_rerank_settings(env: Mapping[str, str]) -> RerankSettings:
    """Read the reranker's knobs from ``env`` (``os.environ`` after ``Config.load()``)."""
    backend = env_choice(env, "BRAIN_RERANKER", BACKENDS, BACKEND_NONE)
    default_model = (
        VOYAGE_RERANK_DEFAULT_MODEL
        if backend == BACKEND_VOYAGE
        else LLAMACPP_RERANK_DEFAULT_MODEL
    )
    voyage_api_key: str | None = None
    if backend == BACKEND_VOYAGE:
        # The variable ``Config`` already uses for the Voyage embedder.
        voyage_api_key = env_str(env, "VOYAGE_API_KEY", None)
        if voyage_api_key is None:
            raise ConfigError("BRAIN_RERANKER=voyage needs VOYAGE_API_KEY to be set")
    return RerankSettings(
        backend=backend,
        depth=env_int(env, "BRAIN_RERANK_DEPTH", DEFAULT_DEPTH, minimum=2, maximum=MAX_DEPTH),
        timeout_s=env_float(
            env, "BRAIN_RERANK_TIMEOUT_S", DEFAULT_TIMEOUT_S, minimum=0.1, maximum=120.0
        ),
        url=_loopback_url(env),
        model=env_str(env, "BRAIN_RERANK_MODEL", default_model),
        max_chars=env_int(
            env, "BRAIN_RERANK_MAX_CHARS", DEFAULT_MAX_CHARS, minimum=100, maximum=20_000
        ),
        voyage_api_key=voyage_api_key,
    )
