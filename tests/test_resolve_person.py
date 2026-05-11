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


# ---------------------------------------------------------------------------
# Canonical-name dedup: same person stored under multiple formattings
# (e.g. "person-x person-j" vs "person-x.person-j") should merge into one match.
# ---------------------------------------------------------------------------


def test_resolve_person_merges_dot_separated_variant_of_same_name(
    test_db: psycopg.Connection[Any],
) -> None:
    """Gmail headers sometimes record a name as ``person-x.person-j`` while
    Krisp records the same person as ``person-x person-j``. The resolver must
    treat them as ONE person (same canonical form) and merge their keys
    rather than raising PersonAmbiguous.
    """
    DirectoryStore(test_db).upsert_pair(
        display_name="person-x person-j", email="person-j@example.com", source="contacts"
    )
    DirectoryStore(test_db).upsert_pair(
        display_name="person-x.person-j", email="person-j.d@example.com", source="gmail"
    )
    # `person-j` is a substring of both display names — pre-fix this raised
    # PersonAmbiguous with two candidates.
    match = resolve_person_to_keys(test_db, "person-j")
    # Both emails must be threaded into the merged key set so the SQL
    # overlap predicate catches docs ingested under EITHER formatting.
    assert "person-j@example.com" in match.keys
    assert "person-j.d@example.com" in match.keys


def test_resolve_person_merges_underscore_and_space_variants(
    test_db: psycopg.Connection[Any],
) -> None:
    """Underscore-separated and hyphen-separated variants canonicalize to
    the same person too."""
    DirectoryStore(test_db).upsert_pair(
        display_name="Alice Wonder", email="a@x.com", source="contacts"
    )
    DirectoryStore(test_db).upsert_pair(
        display_name="alice_wonder", email="alice@y.com", source="gmail"
    )
    DirectoryStore(test_db).upsert_pair(
        display_name="Alice-Wonder", email="aw@example.com", source="gmail"
    )
    match = resolve_person_to_keys(test_db, "alice wonder")
    # All three emails merged into one key set.
    assert "a@x.com" in match.keys
    assert "alice@y.com" in match.keys
    assert "aw@example.com" in match.keys


def test_resolve_person_still_raises_when_canonical_forms_differ(
    test_db: psycopg.Connection[Any],
) -> None:
    """The dedup pass MUST NOT merge two genuinely-different people who
    happen to share a substring. ``"John Smith"`` and ``"John Smith Jr"``
    canonicalize differently (``"john smith"`` vs ``"john smith jr"``),
    so they stay ambiguous on a substring query like ``"smith"`` that
    hits step-3 with both records.

    (A query of exactly ``"john smith"`` would resolve cleanly via the
    step-2 strict-identity tier — that's the desired behaviour, not a
    bug. This test exercises the substring path.)
    """
    DirectoryStore(test_db).upsert_pair(
        display_name="John Smith", email="js@x.com", source="contacts"
    )
    DirectoryStore(test_db).upsert_pair(
        display_name="John Smith Jr", email="jsjr@x.com", source="contacts"
    )
    with pytest.raises(PersonAmbiguous) as exc_info:
        resolve_person_to_keys(test_db, "smith")
    msg = str(exc_info.value)
    assert "John Smith" in msg
    assert "John Smith Jr" in msg
