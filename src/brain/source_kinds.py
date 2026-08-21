"""Canonical closed set of ingest source kinds (``sources.kind``) + its guard.

``sources.kind`` is bare ``TEXT NOT NULL`` with no CHECK constraint
(``001_init.sql:6``), so the database accepts any string. Every read surface,
though, treats the column as a closed enum of four values: the facet panel
buckets by it, ``brain.ui.schemas`` offers exactly those four in its dropdown,
and ``brain.vault.links`` resolves ``[[<kind>:<external-id>]]`` against the same
set. The enum is therefore real, but it was enforced nowhere on the way in.

This module is that enforcement point, and it exists as a module (rather than as
a constant inside one of the callers) because the two ingest entry points that
accept a caller-supplied kind live in different files —
``brain.cli_ingest.ingest_stdin`` and ``brain.mcp_server.brain_ingest_stdin`` —
and ``cli_ingest`` cannot import from ``cli`` where the set previously lived:
``cli`` imports ``cli_ingest``, so the dependency only runs one way.

``brain.ui.schemas.VALID_SOURCE_KINDS`` deliberately keeps its own copy; its
docstring explains why (importing the Typer CLI into every HTTP handler), and
``tests/test_ui_schemas.py`` asserts the two are equal, so that copy is guarded
rather than merely duplicated.
"""

from __future__ import annotations

from .errors import BrainError


class InvalidSourceKind(BrainError):
    """An ingest entry point was handed a ``source`` outside the closed set."""


#: The four kinds the ingest paths may write to ``sources.kind``.
#: Mirrors :data:`brain.vault.links._SOURCE_KINDS` and
#: :data:`brain.ui.schemas.VALID_SOURCE_KINDS`.
VALID_SOURCE_KINDS: frozenset[str] = frozenset({"manual", "krisp", "gmail", "slack"})


def source_kinds_hint() -> str:
    """``gmail|krisp|manual|slack`` — sorted, for error messages."""
    return "|".join(sorted(VALID_SOURCE_KINDS))


def validate_source_kind(source: str) -> str:
    """Return ``source`` unchanged, or raise :class:`InvalidSourceKind`.

    Callers map the exception onto their own surface's error type: the CLI to
    ``typer.BadParameter`` (exit 2), the MCP server to ``INVALID_PARAMS``. The
    message is built here so both surfaces name the same accepted set.
    """
    if source in VALID_SOURCE_KINDS:
        return source
    raise InvalidSourceKind(
        f"unknown source {source!r} (expected: {source_kinds_hint()})"
    )
