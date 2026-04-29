"""Persistent name↔email directory + refresh logic for the metadata linker."""
import json
import logging
from datetime import datetime
from pathlib import Path
from typing import Any, Protocol

import psycopg
import yaml

from brain.errors import DirectoryRefreshError
from brain.vault.derived_links.participants import (
    is_email_like,
    normalize_participant,
)

_logger = logging.getLogger(__name__)

# Sources accepted by ``directory_entries.source`` (mirrors the CHECK
# constraint in migration 005). Kept in sync with the migration.
_VALID_SOURCES: frozenset[str] = frozenset(
    {"gmail", "calendar", "contacts", "people_yml"}
)


class GwsRunner(Protocol):
    """Subprocess shell-out for the `gws` CLI used to mine Calendar / Contacts.

    Production runner shells out to `gws` (must be on PATH); tests pass a
    fake. See `src/brain/ingest/gmail.py:179` for the parallel pattern,
    but note: that pattern raises ``GmailError`` — the refresh helpers
    here only catch ``(OSError, DirectoryRefreshError, RuntimeError)``.

    **Implementations must raise ``DirectoryRefreshError``, ``OSError``,
    or ``RuntimeError`` on failure.** ``subprocess.CalledProcessError``
    must be translated by the runner — it is NOT caught by the refresh
    helpers and will propagate.

    Implementations should return the raw stdout of the gws command
    (typically JSON). The refresh helpers parse JSON shape per command:

    - ``gws calendar list-events …`` →
      ``[{"summary": ..., "attendees": [{"email": ..., "displayName": ...}], ...}, ...]``
    - ``gws people list …`` →
      ``[{"names": [{"displayName": ...}], "emailAddresses": [{"value": ...}, ...]}, ...]``
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
        if source not in _VALID_SOURCES:
            raise ValueError(f"invalid source: {source!r}")

        normalized_name: str | None = (
            None if display_name is None else normalize_participant(display_name)
        )
        # Empty-name rows are still indexable but will be skipped by name
        # resolution; canonical "no display name" stored as ''.
        name_value = normalized_name if normalized_name else ""

        normalized_email = email.strip().lower()
        if not normalized_email:
            raise ValueError("email cannot be empty")

        self._conn.execute(
            """
            INSERT INTO directory_entries (display_name, email, source)
            VALUES (%s, %s, %s)
            ON CONFLICT (display_name, email, source) DO UPDATE SET
                occurrence_count = directory_entries.occurrence_count + 1,
                last_seen_at = NOW()
            """,
            (name_value, normalized_email, source),
        )

    def resolve_name_to_email(self, name: str) -> str | None:
        """Return canonical email for `name`, or None if zero / multiple matches.

        `_people.yml` overrides win; otherwise prefer entries where exactly
        one (display_name, email) pair has highest occurrence_count across
        sources. Returns None on ambiguity.
        """
        normalized = normalize_participant(name)
        if not normalized:
            return None

        rows = self._conn.execute(
            """
            SELECT email,
                   SUM(occurrence_count) AS total,
                   BOOL_OR(source = 'people_yml') AS has_people_yml
            FROM directory_entries
            WHERE display_name = %s AND display_name <> ''
            GROUP BY email
            ORDER BY total DESC
            """,
            (normalized,),
        ).fetchall()

        if not rows:
            return None

        # ``people_yml`` wins absolutely. If multiple ``people_yml`` rows
        # exist for this name (caller mistake — _people.yml should map one
        # name to one email) we still treat the first as the answer.
        for email, _total, has_people_yml in rows:
            if has_people_yml:
                return str(email)

        # Skip-ambiguous policy: if the top-summed row ties with another,
        # there's no winner.
        top_total = rows[0][1]
        top_rows = [r for r in rows if r[1] == top_total]
        if len(top_rows) > 1:
            return None
        return str(top_rows[0][0])

    def all_emails(self) -> set[str]:
        """Return the set of every email seen in any directory row.

        Used by the linker pass for fast membership checks.
        """
        rows = self._conn.execute(
            "SELECT DISTINCT email FROM directory_entries"
        ).fetchall()
        return {str(r[0]) for r in rows}


def load_people_yml(vault_path: Path) -> dict[str, str]:
    """Parse `<vault_path>/_people.yml` if present; return {} otherwise.

    Schema: `Display Name: canonical@example.com` per line. Returns a mapping
    from normalized lowercase name to lowercase email. Missing file is not
    an error.
    """
    yml_path = vault_path / "_people.yml"
    if not yml_path.exists():
        return {}

    try:
        with yml_path.open("r", encoding="utf-8") as fh:
            data = yaml.safe_load(fh)
    except yaml.YAMLError as exc:
        _logger.warning("malformed _people.yml at %s: %s", yml_path, exc)
        return {}
    except OSError as exc:
        _logger.warning("could not read _people.yml at %s: %s", yml_path, exc)
        return {}

    if data is None:
        # Empty file — yaml.safe_load returns None. Treat as empty mapping.
        return {}

    if not isinstance(data, dict):
        _logger.warning(
            "_people.yml at %s: expected top-level mapping, got %s",
            yml_path,
            type(data).__name__,
        )
        return {}

    result: dict[str, str] = {}
    for raw_name, raw_email in data.items():
        if not isinstance(raw_name, str) or not isinstance(raw_email, str):
            _logger.warning(
                "_people.yml: skipping non-string entry %r -> %r",
                raw_name,
                raw_email,
            )
            continue
        normalized_name = normalize_participant(raw_name)
        if not normalized_name:
            _logger.warning("_people.yml: skipping unnormalizable name %r", raw_name)
            continue
        normalized_email = raw_email.strip().lower()
        if not is_email_like(normalized_email):
            _logger.warning(
                "_people.yml: skipping invalid email for %r: %r",
                raw_name,
                raw_email,
            )
            continue
        result[normalized_name] = normalized_email
    return result


_REFRESH_STATE_SOURCES: frozenset[str] = frozenset({"gmail", "calendar", "contacts"})


def _update_refresh_state(
    conn: psycopg.Connection[Any], *, source: str, records_seen: int
) -> None:
    """Insert / update the high-water mark for ``source``.

    On conflict the running ``records_seen`` counter is incremented so each
    refresh contributes to the lifetime tally; ``last_refreshed_at`` is
    always set to ``NOW()``.

    Mirrors the CHECK constraint on ``directory_refresh_state.source`` from
    migration 005:55 — ``people_yml`` is rejected here (it has no refresh
    cadence). Raising early keeps the failure local instead of surfacing
    as a Postgres ``IntegrityError`` from the INSERT.
    """
    if source not in _REFRESH_STATE_SOURCES:
        raise ValueError(
            f"invalid refresh-state source {source!r}; "
            "see migration 005's CHECK constraint"
        )
    conn.execute(
        """
        INSERT INTO directory_refresh_state (source, last_refreshed_at, records_seen)
        VALUES (%s, NOW(), %s)
        ON CONFLICT (source) DO UPDATE SET
            last_refreshed_at = NOW(),
            records_seen = directory_refresh_state.records_seen + EXCLUDED.records_seen
        """,
        (source, records_seen),
    )


def refresh_calendar(
    conn: psycopg.Connection[Any],
    *,
    since: datetime,
    until: datetime,
    runner: GwsRunner,
) -> int:
    """Mine name↔email pairs from Google Calendar via the `gws` CLI.

    On any subprocess / JSON failure the error is logged at WARNING level
    and the function returns 0 — the linker keeps running on a stale
    directory rather than failing the whole sync. Successful empty
    refreshes still bump ``last_refreshed_at`` so the high-water mark
    advances even when no events match.

    NOTE: The exact ``gws calendar`` subcommand is a best-guess
    (``list-events --time-min --time-max --format json``); production code
    can adapt to the real ``gws`` flag set without touching this function
    because the Protocol-based design keeps it shell-agnostic.
    """
    store = DirectoryStore(conn)
    try:
        raw = runner(
            [
                "gws",
                "calendar",
                "list-events",
                "--time-min",
                since.isoformat(),
                "--time-max",
                until.isoformat(),
                "--format",
                "json",
            ]
        )
    except (OSError, DirectoryRefreshError, RuntimeError) as exc:
        _logger.warning(
            "gws calendar refresh failed [%s..%s]: %s",
            since.isoformat(),
            until.isoformat(),
            exc,
        )
        return 0

    try:
        events = json.loads(raw) if raw else []
    except json.JSONDecodeError as exc:
        _logger.warning(
            "gws calendar JSON parse failed [%s..%s]: %s",
            since.isoformat(),
            until.isoformat(),
            exc,
        )
        return 0

    if not isinstance(events, list):
        _logger.warning(
            "gws calendar: expected JSON list, got %s", type(events).__name__
        )
        return 0

    events_seen = 0
    for event in events:
        if not isinstance(event, dict):
            continue
        events_seen += 1
        attendees = event.get("attendees") or []
        if not isinstance(attendees, list):
            continue
        for attendee in attendees:
            if not isinstance(attendee, dict):
                continue
            email = attendee.get("email")
            if not isinstance(email, str) or not email.strip():
                # Without an email there's nothing to upsert.
                continue
            display = attendee.get("displayName")
            display_name = display if isinstance(display, str) else None
            try:
                store.upsert_pair(
                    display_name=display_name,
                    email=email,
                    source="calendar",
                )
            except ValueError:
                # Bad email shape — skip, keep going. (Don't let one bad
                # row poison the whole refresh.) Log the rejected email at
                # DEBUG so operators can correlate; emails are IDs (not
                # payload bodies) so this respects the CLAUDE.md log rule.
                _logger.debug(
                    "calendar refresh: skipping attendee with invalid email: %s",
                    email or "<missing>",
                )
                continue

    _update_refresh_state(conn, source="calendar", records_seen=events_seen)
    return events_seen


def refresh_contacts(
    conn: psycopg.Connection[Any],
    *,
    runner: GwsRunner,
) -> int:
    """Mine name↔email pairs from Google Contacts via the `gws` CLI.

    Same error-handling contract as :func:`refresh_calendar`: any failure
    logs a warning and returns 0 without updating the high-water mark.

    NOTE: Best-guess subcommand ``gws people list --format json``; see the
    note on :func:`refresh_calendar`.
    """
    store = DirectoryStore(conn)
    try:
        raw = runner(["gws", "people", "list", "--format", "json"])
    except (OSError, DirectoryRefreshError, RuntimeError) as exc:
        _logger.warning(
            "gws people refresh failed (cmd=%s): %s",
            "gws people list --format json",
            exc,
        )
        return 0

    try:
        contacts = json.loads(raw) if raw else []
    except json.JSONDecodeError as exc:
        _logger.warning(
            "gws people JSON parse failed (cmd=%s): %s",
            "gws people list --format json",
            exc,
        )
        return 0

    if not isinstance(contacts, list):
        _logger.warning(
            "gws people: expected JSON list, got %s", type(contacts).__name__
        )
        return 0

    contacts_seen = 0
    for contact in contacts:
        if not isinstance(contact, dict):
            continue
        names = contact.get("names") or []
        emails = contact.get("emailAddresses") or []
        if not isinstance(names, list) or not isinstance(emails, list):
            continue

        # Pick the first display name; Google People returns names ordered
        # by primacy.
        display: str | None = None
        for name_obj in names:
            if isinstance(name_obj, dict):
                candidate = name_obj.get("displayName")
                if isinstance(candidate, str) and candidate.strip():
                    display = candidate
                    break

        # A contact with no usable email contributes nothing.
        upserted = False
        for email_obj in emails:
            if not isinstance(email_obj, dict):
                continue
            value = email_obj.get("value")
            if not isinstance(value, str) or not value.strip():
                continue
            try:
                store.upsert_pair(
                    display_name=display,
                    email=value,
                    source="contacts",
                )
            except ValueError:
                # Same belt-and-suspenders catch as refresh_calendar — see
                # the comment there for rationale. DEBUG-log the rejected
                # email value (it's an ID, not a payload body).
                _logger.debug(
                    "contacts refresh: skipping email with invalid shape: %s",
                    value or "<missing>",
                )
                continue
            upserted = True

        if upserted:
            contacts_seen += 1

    _update_refresh_state(conn, source="contacts", records_seen=contacts_seen)
    return contacts_seen
