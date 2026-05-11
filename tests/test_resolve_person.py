"""Tests for :func:`brain.queries.resolve_person_to_keys`.

Covers exact email match, exact display-name match, substring match,
ambiguous + not-found error paths, case-folding, and the keys-expansion
contract that powers the ``documents.participants`` overlap predicate
in ``hybrid_search``.
"""
from __future__ import annotations

from typing import Any

import psycopg
import pytest

from brain.errors import PersonAmbiguous, PersonNotFound
from brain.queries import resolve_person_to_keys
from brain.vault.derived_links.directory import DirectoryStore


def _seed_person(
    conn: psycopg.Connection[Any], *, display_name: str, email: str,
    source: str = "gmail",
) -> None:
    """Upsert one (display_name, email, source) row via the canonical writer."""
    DirectoryStore(conn).upsert_pair(
        display_name=display_name, email=email, source=source
    )


def test_resolve_person_by_exact_email(
    test_db: psycopg.Connection[Any]
) -> None:
    _seed_person(test_db, display_name="Alice Doe", email="alice@x.com")
    match = resolve_person_to_keys(test_db, "alice@x.com")
    assert match.display_name == "Alice Doe"
    # Keys must include the email AND the display name (both lowered) +
    # the "Display <email>" combination form expected by Gmail metadata.
    assert "alice@x.com" in match.keys
    assert "alice doe" in match.keys
    assert "alice doe <alice@x.com>" in match.keys


def test_resolve_person_by_exact_display_name(
    test_db: psycopg.Connection[Any]
) -> None:
    _seed_person(test_db, display_name="Alice Doe", email="alice@x.com")
    match = resolve_person_to_keys(test_db, "alice doe")
    assert match.display_name == "Alice Doe"
    assert "alice@x.com" in match.keys


def test_resolve_person_is_case_folded(
    test_db: psycopg.Connection[Any]
) -> None:
    _seed_person(test_db, display_name="Alice Doe", email="alice@x.com")
    match = resolve_person_to_keys(test_db, "ALICE")
    assert match.display_name == "Alice Doe"


def test_resolve_person_substring_single_match(
    test_db: psycopg.Connection[Any]
) -> None:
    _seed_person(test_db, display_name="Alice Xanthus", email="ax@y.com")
    match = resolve_person_to_keys(test_db, "xan")
    assert match.display_name == "Alice Xanthus"


def test_resolve_person_substring_ambiguous_raises(
    test_db: psycopg.Connection[Any]
) -> None:
    _seed_person(test_db, display_name="Alice Doe", email="alice@x.com")
    _seed_person(test_db, display_name="Alice Xanthus", email="ax@y.com")
    with pytest.raises(PersonAmbiguous) as exc_info:
        resolve_person_to_keys(test_db, "alice")
    assert exc_info.value.query == "alice"
    # The error message must surface the candidates so the CLI / MCP layer
    # can pass it straight to the user.
    msg = str(exc_info.value)
    assert "Alice Doe" in msg
    assert "Alice Xanthus" in msg


def test_resolve_person_not_found_raises(
    test_db: psycopg.Connection[Any]
) -> None:
    _seed_person(test_db, display_name="Alice Doe", email="alice@x.com")
    with pytest.raises(PersonNotFound):
        resolve_person_to_keys(test_db, "unknown@y.com")


def test_resolve_person_empty_string_raises_not_found(
    test_db: psycopg.Connection[Any]
) -> None:
    """An empty or whitespace-only argument is a config-shape failure —
    treat it as "no person matched" rather than raising something the
    Typer layer hasn't seen before."""
    _seed_person(test_db, display_name="Alice Doe", email="alice@x.com")
    with pytest.raises(PersonNotFound):
        resolve_person_to_keys(test_db, "")
    with pytest.raises(PersonNotFound):
        resolve_person_to_keys(test_db, "   ")


def test_resolve_person_exact_email_wins_over_substring(
    test_db: psycopg.Connection[Any]
) -> None:
    """Step 1 (exact email) short-circuits before step 3 (substring) — so
    a search for ``alice@x.com`` returns Alice Doe even if "alice" would
    otherwise be ambiguous against multiple people."""
    _seed_person(test_db, display_name="Alice Doe", email="alice@x.com")
    _seed_person(test_db, display_name="Alice Xanthus", email="ax@y.com")
    match = resolve_person_to_keys(test_db, "alice@x.com")
    assert match.display_name == "Alice Doe"


def test_resolve_person_keys_dedup_and_sorted(
    test_db: psycopg.Connection[Any]
) -> None:
    """Keys list is deduped + sorted for byte-stable explain payloads."""
    _seed_person(test_db, display_name="Alice Doe", email="alice@x.com")
    match = resolve_person_to_keys(test_db, "alice@x.com")
    # Sorted ASCII order
    assert match.keys == sorted(match.keys)
    # No duplicates
    assert len(match.keys) == len(set(match.keys))


def test_resolve_person_keys_include_display_email_form_for_every_email(
    test_db: psycopg.Connection[Any]
) -> None:
    """A person with multiple emails gets a "Display <email>" combination
    form for each one — Gmail might have recorded them under any."""
    DirectoryStore(test_db).upsert_pair(
        display_name="Alice Doe", email="alice@x.com", source="gmail"
    )
    DirectoryStore(test_db).upsert_pair(
        display_name="Alice Doe", email="alice@y.com", source="gmail"
    )
    match = resolve_person_to_keys(test_db, "alice doe")
    assert "alice doe <alice@x.com>" in match.keys
    assert "alice doe <alice@y.com>" in match.keys
