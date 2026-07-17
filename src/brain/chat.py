"""Public JSON-mode Ollama chat helper shared across feature modules.

Extracted from :meth:`brain.enrichment.OllamaEnricher._chat_with_retry` so
feature modules (Plan 01 ``brief``, Plan 04 audio, Plan 06 ``ask``) can issue
JSON-mode ``/api/chat`` round-trips WITHOUT importing ``enrichment`` — which
would create import cycles (``enrichment`` → feature → ``enrichment``).
``chat`` is the neutral home: it depends only on :class:`~brain.config.Config`
and the project error types.

Single implementation of the round-trip. :func:`chat_json` is the public
``Config``-based convenience entry; :func:`chat_json_with_client` is the
lower-level core (caller supplies the long-lived ``httpx.Client`` and an
explicit message list). ``OllamaEnricher`` delegates its transport + retry to
:func:`chat_json_with_client` so the enricher keeps its persistent injected
client and exact two-message (system + user) wire shape, with zero duplicated
retry logic.
"""
from __future__ import annotations

import json
import logging
from typing import Any

import httpx

from .config import Config, keep_alive_wire_value
from .errors import EnrichmentError, OllamaUnavailable

_logger = logging.getLogger(__name__)

# Default completion-length cap. Matches the historical enricher default so the
# delegated summary/tag calls keep their exact prior budget.
DEFAULT_NUM_PREDICT = 256

# One chat message: ``{"role": ..., "content": ...}``.
ChatMessage = dict[str, str]

# Stringified boolean tokens local models commonly emit instead of a JSON bool.
# Shared by :func:`coerce_bool`; kept in lockstep with the (raise-on-unknown)
# variant in :meth:`brain.enrichment.OllamaEnricher._coerce_contradicts`.
_TRUE_TOKENS = frozenset({"true", "yes", "1"})
_FALSE_TOKENS = frozenset({"false", "no", "0"})


def coerce_bool(value: Any, *, default: bool = False) -> bool:
    """Coerce a model's JSON boolean-ish field into a real ``bool``.

    Local models frequently emit a *stringified* boolean (``"true"`` /
    ``"false"``) or an int (``1`` / ``0``) where a JSON boolean was asked for.
    Passing such a value straight through ``bool(...)`` is a trap: ``bool("false")``
    is ``True`` (any non-empty string is truthy), so a control-flow flag like
    ``ask``'s ``sufficient`` verdict silently flips.

    Normalisation:

    * a real ``bool`` returns as-is;
    * an ``int`` (``1`` → ``True``, ``0`` → ``False``; any other int → ``default``);
    * a ``str`` matched case-insensitively / trimmed against ``"true"/"yes"/"1"``
      (→ ``True``) and ``"false"/"no"/"0"`` (→ ``False``);
    * anything else (an unrecognised string, a float, a dict, ``None``) falls
      back to ``default``.

    Unlike :meth:`brain.enrichment.OllamaEnricher._coerce_contradicts` this never
    raises: it is meant for control-flow flags where a safe default beats an
    abort. Callers pick the conservative fallback (``ask`` passes ``default=False``
    so an unparseable sufficiency verdict keeps the retrieval loop going).
    """
    if isinstance(value, bool):
        return value
    if isinstance(value, int):  # bool already handled above; plain 1/0 only.
        if value == 1:
            return True
        if value == 0:
            return False
        return default
    if isinstance(value, str):
        token = value.strip().lower()
        if token in _TRUE_TOKENS:
            return True
        if token in _FALSE_TOKENS:
            return False
    return default


def _build_client(host: str, timeout: float) -> httpx.Client:
    """Construct the Ollama HTTP client (single seam tests patch).

    Kept as a tiny factory so unit tests can swap in an ``httpx.Client`` backed
    by a :class:`httpx.MockTransport` without monkey-patching production
    transport internals.
    """
    return httpx.Client(base_url=host, timeout=httpx.Timeout(timeout))


def chat_json(
    prompt: str,
    *,
    schema: dict[str, Any],
    cfg: Config,
    model: str | None = None,
    num_predict: int | None = None,
    timeout: float | None = None,
) -> dict[str, Any]:
    """Issue a JSON-mode ``/api/chat`` call and return the parsed object.

    Canonical signature shared across Plans 01 / 04 / 06 (locked in the
    roadmap README cross-plan decisions).

    Args:
        prompt: The full user-turn instruction. Callers fold any system
            framing directly into this string — ``chat_json`` sends a single
            ``user`` message so feature modules own their whole prompt.
        schema: A mapping whose KEYS are the required top-level keys of the
            returned JSON object. Every key must be present in the model's
            response or the call retries (and ultimately raises). Values are
            ignored here (they exist for the caller's own documentation of the
            expected shape).
        cfg: Supplies the Ollama host, default model (``cfg.enrich_model``),
            ``keep_alive``, and default timeout (``cfg.enrich_timeout_seconds``).
        model: Per-call model override; defaults to ``cfg.enrich_model``.
        num_predict: Per-call completion-length cap; defaults to
            :data:`DEFAULT_NUM_PREDICT`.
        timeout: Per-call HTTP timeout (seconds); defaults to
            ``cfg.enrich_timeout_seconds``.

    Returns:
        The parsed JSON object (a ``dict``) with every key in ``schema`` present.

    Raises:
        OllamaUnavailable: Transport failure / 5xx (no retry — fail fast).
        EnrichmentError: Two consecutive malformed-JSON / schema-violating
            responses, or a 4xx permanent error.
    """
    resolved_model = model or cfg.enrich_model
    resolved_timeout = (
        timeout if timeout is not None else cfg.enrich_timeout_seconds
    )
    resolved_num_predict = (
        num_predict if num_predict is not None else DEFAULT_NUM_PREDICT
    )
    required_keys = tuple(schema.keys())
    messages: list[ChatMessage] = [{"role": "user", "content": prompt}]
    with _build_client(cfg.ollama_host, resolved_timeout) as client:
        return chat_json_with_client(
            client,
            model=resolved_model,
            messages=messages,
            required_keys=required_keys,
            keep_alive=cfg.ollama_keep_alive,
            num_predict=resolved_num_predict,
        )


def chat_json_with_client(
    client: httpx.Client,
    *,
    model: str,
    messages: list[ChatMessage],
    required_keys: tuple[str, ...],
    keep_alive: str,
    num_predict: int = DEFAULT_NUM_PREDICT,
) -> dict[str, Any]:
    """Issue an ``/api/chat`` JSON-mode call with one retry on parse failure.

    The shared retry core. The caller supplies the (possibly long-lived)
    ``client`` and an explicit ``messages`` list so both the ``Config``-based
    :func:`chat_json` (single user message, per-call client) and
    :class:`brain.enrichment.OllamaEnricher` (system + user messages, persistent
    injected client) route through one implementation.

    Returns the parsed JSON object on success. Raises
    :class:`OllamaUnavailable` on transient transport errors (no retry — fail
    fast so callers can skip). Raises :class:`EnrichmentError` when two
    consecutive attempts fail JSON parsing / schema validation.
    """
    last_error: Exception | None = None
    current_num_predict = num_predict
    for attempt in (1, 2):
        try:
            response_text = _chat_once(
                client,
                model=model,
                messages=messages,
                keep_alive=keep_alive,
                num_predict=current_num_predict,
            )
        except OllamaUnavailable:
            # Transient — propagate immediately, no retry.
            raise
        try:
            parsed = json.loads(response_text)
        except json.JSONDecodeError as exc:
            last_error = exc
            # Truncation: the model exhausted ``num_predict`` before closing the
            # JSON. Two signals, both meaning "the response was cut off, not
            # malformed from the start": an unterminated string (cut mid-value),
            # OR a decode failure that lands at/after the end of the received text
            # (cut mid-object — e.g. "Expecting ',' delimiter" / "Expecting
            # property name …" at EOF). Either way the SAME budget would
            # re-truncate deterministically at temperature 0.0, so double
            # ``num_predict`` for the retry to give the next call room to finish.
            # A genuinely malformed response (garbage decoded well before EOF, e.g.
            # "Expecting value" at position 0) is NOT truncation and doesn't bump —
            # the retry there is a deliberate best-effort second try. Schema /
            # non-dict failures don't reach this branch.
            if "Unterminated" in exc.msg or exc.pos >= len(response_text):
                current_num_predict *= 2
            _logger.debug(
                "chat attempt %d: JSON decode failed (%s); response=%r",
                attempt,
                exc,
                response_text[:200],
            )
            continue
        if not isinstance(parsed, dict):
            last_error = EnrichmentError(
                f"chat returned non-object JSON: {parsed!r}"
            )
            continue
        missing = [k for k in required_keys if k not in parsed]
        if missing:
            last_error = EnrichmentError(
                f"chat response missing required keys {missing}: {parsed!r}"
            )
            continue
        return parsed
    # Both attempts failed.
    assert last_error is not None
    raise EnrichmentError(
        f"chat failed after 2 attempts: {last_error}"
    ) from last_error


def _chat_once(
    client: httpx.Client,
    *,
    model: str,
    messages: list[ChatMessage],
    keep_alive: str,
    num_predict: int = DEFAULT_NUM_PREDICT,
) -> str:
    """Single ``/api/chat`` round-trip returning the inner content string.

    Returns the ``message.content`` field verbatim so the caller can
    ``json.loads`` it. Maps transport-layer failures to
    :class:`OllamaUnavailable` (CLAUDE.md "no bare except" — only
    ``httpx.HTTPError`` and ``ValueError`` are caught here, matching the
    embedder's pattern). ``num_predict`` caps the completion length.
    """
    request_body = {
        "model": model,
        "stream": False,
        "format": "json",
        "keep_alive": keep_alive_wire_value(keep_alive),
        "messages": messages,
        "options": {"temperature": 0.0, "num_predict": num_predict},
    }
    try:
        response = client.post("/api/chat", json=request_body)
        response.raise_for_status()
        payload = response.json()
    except httpx.HTTPStatusError as exc:
        status = exc.response.status_code if exc.response is not None else 0
        body_preview = (
            exc.response.text[:200] if exc.response is not None else "<no body>"
        )
        if status >= 500:
            raise OllamaUnavailable(
                f"Ollama returned HTTP {status}: {body_preview}"
            ) from exc
        # 4xx — permanent (bad model name, malformed request).
        raise EnrichmentError(
            f"Ollama returned HTTP {status}: {body_preview}"
        ) from exc
    except (httpx.ConnectError, httpx.ConnectTimeout, httpx.ReadTimeout) as exc:
        raise OllamaUnavailable(f"Ollama unreachable: {exc}") from exc
    except httpx.HTTPError as exc:
        raise OllamaUnavailable(f"Ollama transport error: {exc}") from exc
    except ValueError as exc:
        # json.JSONDecodeError is a ValueError — a 200 OK with non-JSON body
        # leaks as a raw decode error otherwise.
        raise EnrichmentError(
            f"Ollama returned non-JSON envelope: {exc}"
        ) from exc
    message = payload.get("message") if isinstance(payload, dict) else None
    if not isinstance(message, dict):
        raise EnrichmentError(f"Ollama response missing 'message': {payload!r}")
    content = message.get("content")
    if not isinstance(content, str):
        raise EnrichmentError(
            f"Ollama message.content is not a string: {message!r}"
        )
    return content
