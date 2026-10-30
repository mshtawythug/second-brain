"""Canonical closed set of ingest source kinds (``sources.kind``) + its guard.

``sources.kind`` is bare ``TEXT NOT NULL`` with no CHECK constraint
(``001_init.sql:6``), so the database accepts any string. Every read surface,
though, treats the column as a closed enum: the facet panel buckets by it,
``brain.ui.schemas`` offers exactly those values in its dropdown,
and ``brain.vault.links`` resolves ``[[<kind>:<external-id>]]`` against the same
set. The enum is therefore real, but it was enforced nowhere on the way in.

This module is that enforcement point, and it exists as a module (rather than as
a constant inside one of the callers) because the two ingest entry points that
accept a caller-supplied kind live in different files —
``brain.cli_ingest.ingest_stdin`` and ``brain.mcp_server.brain_ingest_stdin`` —
and ``cli_ingest`` cannot import from ``cli`` where the set previously lived:
``cli`` imports ``cli_ingest``, so the dependency only runs one way.

``brain.ui.schemas.VALID_SOURCE_KINDS`` **re-exports the set defined here** — it
is the same object, not a copy of it. The import cost that once justified a
second literal was the Typer CLI, and this module is not it: importing
``brain.source_kinds`` does not load ``typer``. ``tests/test_ui_schemas.py``
asserts **identity** (``is``), not equality: once the UI became a re-export an
equality guard could not fail, and the failure mode that remains — someone
restating the literal there — yields an equal-but-separate object that ``==``
passes and ``is`` catches.
"""

from __future__ import annotations

from .errors import BrainError


class InvalidSourceKind(BrainError):
    """An ingest entry point was handed a ``source`` outside the closed set."""


#: The kinds the ingest paths may write to ``sources.kind``. **This is the
#: canonical definition** — :data:`brain.ui.schemas.VALID_SOURCE_KINDS`,
#: :data:`brain.cli._VALID_SOURCE_KINDS` and :data:`brain.vault.links._SOURCE_KINDS`
#: are re-exports *of this object*, so the mirroring runs toward them, not from
#: them. (The direction was stated backwards here until 2026-08-21, which matters:
#: a reader treating this as the mirror edits one of the re-exports and expects the
#: enum to change.) A new kind is added here and nowhere else in Python (RM-07).
#: The front end keeps its own per-kind glyph tables (``SOURCE_ICONS`` in
#: ``ui/static/js/results.js`` and ``discovery.js``), which a new kind also needs.
#: ``calendar`` (RM-30): Google Calendar events, from ``brain ingest-calendar``.
#: ``jira`` (RM-34) has no ingest code: the ``ingest-brain`` skill pipes issues
#: from the Atlassian MCP connector into ``brain ingest-stdin --source jira``.
#: ``github`` (RM-31): ``brain ingest-github`` (``gh``), or the ``ingest-brain``
#: skill's recipe through the GitHub MCP connector into ``brain ingest-stdin``.
#: ``applenotes`` (RM-36): ``brain ingest-apple-notes`` (``osascript``, macOS).
#: ``notion`` (RM-35): pages from a Notion export, ``brain ingest-notion``.
VALID_SOURCE_KINDS: frozenset[str] = frozenset(
    {"manual", "krisp", "gmail", "slack", "calendar", "jira", "github", "applenotes", "notion"}
)


def source_kinds_hint() -> str:
    """The kinds, sorted and ``|``-joined (``github|gmail|…``), for messages and help."""
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
