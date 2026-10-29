"""Request validation for the Ingest tab (Ingest spec §5.1).

A leaf beside :mod:`brain.ui.schemas` rather than inside it, so that module keeps its
headroom under the file-size ceiling. Validation happens **at submit, before
queueing**, so a bad request is a 400 and never a failed job.
"""
from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from ..errors import SensitivityError
from ..sensitivity import SensitivityLevel, normalize_level
from ..tags import normalize_tags
from .errors import UiBadRequest, UiPayloadTooLarge
from .ingest_limits import (
    DEFAULT_PASTE_CONTENT_TYPE,
    MAX_PASTE_BODY_BYTES,
    PASTE_CONTENT_TYPES,
)
from .schemas import MAX_TITLE_CHARS

#: The fields a paste request may carry. Anything else is a 400, so a client
#: that believes it set an option (``force``, ``allow_secrets``) is told it did not.
_PASTE_FIELDS = frozenset({"title", "body", "tags", "content_type", "sensitivity", "enrich"})


@dataclass(frozen=True)
class PasteSpec:
    """A validated paste: what the runner ingests, and nothing else."""

    title: str
    body: str
    tags: tuple[str, ...]
    content_type: str
    sensitivity: SensitivityLevel
    enrich: bool


def parse_paste(payload: Mapping[str, Any]) -> PasteSpec:
    """Validate a paste request body. Raises 400 or 413 (``body_too_large``)."""
    unknown = sorted(set(payload) - _PASTE_FIELDS)
    if unknown:
        raise UiBadRequest(f"unknown field(s): {', '.join(unknown)}", code="unknown_field")
    return PasteSpec(
        title=_title(payload.get("title")),
        body=_body(payload.get("body")),
        tags=_tags(payload.get("tags", [])),
        content_type=_content_type(payload.get("content_type", DEFAULT_PASTE_CONTENT_TYPE)),
        sensitivity=_sensitivity(payload.get("sensitivity")),
        enrich=_enrich(payload.get("enrich", True)),
    )


def _encodes(value: str) -> bool:
    """Strict UTF-8: a lone surrogate (valid JSON, ``"\\ud800"``) is refused here.

    Accepted, it would fail inside the job at the database and report the
    "may already have been saved" message for a document that never was.
    """
    try:
        value.encode("utf-8")
    except UnicodeEncodeError:
        return False
    return True


def _title(value: Any) -> str:
    if not isinstance(value, str) or not value.strip() or not _encodes(value):
        raise UiBadRequest("title must be non-empty UTF-8 text", code="invalid_title")
    title = value.strip()
    if len(title) > MAX_TITLE_CHARS:
        raise UiBadRequest(
            f"title must be at most {MAX_TITLE_CHARS} characters", code="invalid_title"
        )
    return title


def _body(value: Any) -> str:
    if not isinstance(value, str) or not value.strip() or not _encodes(value):
        raise UiBadRequest("body must be non-empty UTF-8 text", code="invalid_body")
    if len(value.encode("utf-8")) > MAX_PASTE_BODY_BYTES:
        raise UiPayloadTooLarge(
            f"body must be at most {MAX_PASTE_BODY_BYTES} bytes", code="body_too_large"
        )
    return value


def _tags(value: Any) -> tuple[str, ...]:
    if not isinstance(value, list) or not all(isinstance(t, str) and _encodes(t) for t in value):
        raise UiBadRequest("tags must be a list of strings", code="invalid_tags")
    return tuple(normalize_tags(value))


def _content_type(value: Any) -> str:
    if value not in PASTE_CONTENT_TYPES:
        raise UiBadRequest(
            f"content_type must be one of: {', '.join(PASTE_CONTENT_TYPES)}",
            code="invalid_content_type",
        )
    return str(value)


def _sensitivity(value: Any) -> SensitivityLevel:
    """Strict: a typo is a 400, never a silently unprotected document."""
    if value is not None and not isinstance(value, str):
        raise UiBadRequest("sensitivity must be a string", code="invalid_sensitivity")
    try:
        return normalize_level(value, strict=True)
    except SensitivityError as exc:
        raise UiBadRequest(
            "sensitivity must be 'normal' or 'confidential'", code="invalid_sensitivity"
        ) from exc


def _enrich(value: Any) -> bool:
    if not isinstance(value, bool):
        raise UiBadRequest("enrich must be true or false", code="invalid_enrich")
    return value
