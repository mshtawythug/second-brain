"""Persistent name↔email directory + refresh logic for the metadata linker."""
from pathlib import Path
from typing import Any, Protocol

import psycopg


class GwsRunner(Protocol):
    """Subprocess shell-out for the `gws` CLI used to mine Calendar / Contacts.

    Production runner shells out to `gws` (must be on PATH); tests pass a
    fake. See `src/brain/ingest/gmail.py:179` for the parallel pattern.
    """

    def __call__(self, args: list[str]) -> str: ...


class DirectoryStore:
    """Read/write interface over `directory_entries` + `directory_refresh_state`.

    Bridges Krisp's name-only speaker labels with Gmail's email-only headers
    by remembering `(display_name, email)` co-occurrences mined from Gmail
    metadata, Calendar invites, Contacts, and `_people.yml`.
    """

    def __init__(self, conn: psycopg.Connection[Any]) -> None:
        self._conn = conn

    def upsert_pair(
        self,
        *,
        display_name: str | None,
        email: str,
        source: str,
    ) -> None:
        """Insert or bump occurrence_count for (display_name, email, source).

        `display_name` may be None (bare-email Gmail headers); the row is
        still recorded with display_name='' for indexability — resolution
        helpers ignore empty-name rows.
        """
        raise NotImplementedError("Implemented in Task B.1")

    def resolve_name_to_email(self, name: str) -> str | None:
        """Return canonical email for `name`, or None if zero / multiple matches.

        `_people.yml` overrides win; otherwise prefer entries where exactly
        one (display_name, email) pair has highest occurrence_count across
        sources. Returns None on ambiguity.
        """
        raise NotImplementedError("Implemented in Task B.1")

    def all_emails(self) -> set[str]:
        """Return the set of every email seen in any directory row.

        Used by the linker pass for fast membership checks.
        """
        raise NotImplementedError("Implemented in Task B.1")


def load_people_yml(vault_path: Path) -> dict[str, str]:
    """Parse `<vault_path>/_people.yml` if present; return {} otherwise.

    Schema: `Display Name: canonical@example.com` per line. Returns a mapping
    from normalized lowercase name to lowercase email. Missing file is not
    an error.
    """
    raise NotImplementedError("Implemented in Task B.1")
