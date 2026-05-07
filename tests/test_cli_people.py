"""CLI integration tests for ``brain people`` (Phase C, 2026-05-07 plan).

The command surfaces the People Hub aggregation in the terminal — it
reuses :func:`brain.wiki.build_people.aggregate_people` directly, so
the data layer is already covered by ``test_build_people.py``. The
tests below pin the CLI surface: roster vs detail view, ``--json``
output shape, threshold filtering, owner filtering, name disambiguation,
and exit codes.

Mirrors ``test_cli_directory.py`` / ``test_cli_relink_derived.py``:
real Postgres test DB, deterministic fake embedder, autouse fixtures
that isolate ``BRAIN_VAULT_PATH`` to ``tmp_path`` and widen Rich's
captured-stdout column width so substring assertions don't flap.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import psycopg
import pytest
from typer.testing import CliRunner

from brain.cli import app
from brain.ingest import ExtractedDoc, ingest_document


@pytest.fixture(autouse=True)
def _isolate_vault_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Point ``BRAIN_VAULT_PATH`` at a per-test tmp dir.

    Without this, ``Config.load()`` would read whatever ``_people.yml``
    happens to live in ``~/brain-vault``, contaminating the curated-
    badge assertions. We don't write a ``_people.yml`` in this fixture
    — tests that need one create it explicitly.
    """
    monkeypatch.setenv("BRAIN_VAULT_PATH", str(tmp_path))


@pytest.fixture(autouse=True)
def _wide_console(monkeypatch: pytest.MonkeyPatch) -> None:
    """Widen Rich's captured-stdout terminal so cells aren't truncated.

    Same pattern as ``test_cli_directory.py``: without ``COLUMNS``,
    Rich treats CliRunner output as 80 cols and emails get clipped to
    e.g. ``ali@exampl…``, making substring assertions on email
    addresses flap.
    """
    monkeypatch.setenv("COLUMNS", "240")


@pytest.fixture(autouse=True)
def _scrub_owner_participants(monkeypatch: pytest.MonkeyPatch) -> None:
    """Force ``BRAIN_OWNER_PARTICIPANTS`` to empty.

    The test author's local ``.env`` likely has owner identifiers set;
    without this override, ``Config.load()`` would pick them up via
    python-dotenv's cwd walk and silently filter out the seeded "Ali"
    rows. ``setenv`` (rather than ``delenv``) wins because dotenv loads
    with ``override=False`` — a set-but-empty shell var beats the file.
    Owner-filter tests opt in by overriding this with their own setenv.
    """
    monkeypatch.setenv("BRAIN_OWNER_PARTICIPANTS", "")


@pytest.fixture(autouse=True)
def _scrub_people_min_docs(monkeypatch: pytest.MonkeyPatch) -> None:
    """Default the People Hub threshold to 0 so seeded 1-doc rows render.

    The production default is 3; our test seeds usually create 1-2 docs
    per person, which would otherwise be filtered. Tests that exercise
    the threshold opt in by setting ``BRAIN_PEOPLE_HUB_MIN_DOCS``
    explicitly.
    """
    monkeypatch.setenv("BRAIN_PEOPLE_HUB_MIN_DOCS", "0")


def _gmail_doc(
    *,
    body: str = "hello",
    title: str = "Hello",
    from_addr: str = "Ali Sarkis <redacted@example.com>",
    to: str = "person-x last-a <person-a@example.com>",
    message_id: str = "m1",
    thread_id: str | None = None,
) -> ExtractedDoc:
    """Build a Gmail-shaped ``ExtractedDoc`` for ingest pipeline seeding."""
    return ExtractedDoc(
        title=title,
        content=body,
        content_type="email",
        source_path=None,
        metadata={
            "from": from_addr,
            "to": to,
            "date": "Wed, 15 Apr 2026 12:00:00 -0700",
            "message_id": message_id,
            "thread_id": thread_id if thread_id is not None else f"t{message_id}",
            "label_ids": ["INBOX"],
        },
    )


def _seed_gmail(
    conn: psycopg.Connection[Any],
    embedder: Any,
    *,
    external_id: str,
    title: str = "Hello",
    body: str = "hello",
    from_addr: str = "Ali Sarkis <redacted@example.com>",
    to: str = "person-x last-a <person-a@example.com>",
) -> str:
    """Ingest a Gmail document; the post-ingest hook seeds directory_entries."""
    result = ingest_document(
        conn,
        embedder=embedder,
        doc=_gmail_doc(
            title=title,
            body=body,
            from_addr=from_addr,
            to=to,
            message_id=external_id,
        ),
        source_kind="gmail",
        source_external_id=external_id,
    )
    assert result.document_id is not None
    return result.document_id


# ---------------------------------------------------------------------------
# Roster view (no name argument).
# ---------------------------------------------------------------------------


def test_people_roster_lists_seeded_correspondents(
    test_db: psycopg.Connection,
    fake_embedder: Any,
    patch_embedder: Any,
) -> None:
    """``brain people`` lists every person from a seeded Gmail doc."""
    patch_embedder(fake_embedder)
    _seed_gmail(test_db, fake_embedder, external_id="m1")

    result = CliRunner().invoke(app, ["people"])
    assert result.exit_code == 0, result.stdout
    out = result.stdout
    # Both seeded names show up (title-cased) — the table headers + rows.
    assert "Display name" in out
    assert "person-x last-a" in out
    assert "Ali Sarkis" in out
    # Primary email surfaces too.
    assert "person-a@example.com" in out


def test_people_roster_handles_empty_db(
    test_db: psycopg.Connection,
    fake_embedder: Any,
    patch_embedder: Any,
) -> None:
    """Empty corpus → friendly "no people in scope" line, exit 0."""
    patch_embedder(fake_embedder)

    result = CliRunner().invoke(app, ["people"])
    assert result.exit_code == 0, result.stdout
    assert "no people in scope" in result.stdout.lower()


def test_people_roster_curated_badge_for_people_yml(
    test_db: psycopg.Connection,
    fake_embedder: Any,
    patch_embedder: Any,
    tmp_path: Path,
) -> None:
    """``_people.yml`` entries surface with the ✅ curated badge.

    Seeds a ``_people.yml`` with one entry, runs ``vault relink-derived``
    so the directory_entries row is created, then invokes ``brain people``
    and asserts the badge column carries the curated marker for that row.
    """
    patch_embedder(fake_embedder)
    _seed_gmail(test_db, fake_embedder, external_id="m1")

    # Add a curated entry. ``_people.yml`` schema is a flat top-level
    # mapping of ``Display Name: canonical@example.com`` lines (see
    # :func:`brain.vault.derived_links.directory.load_people_yml`).
    yml = tmp_path / "_people.yml"
    yml.write_text(
        "person-x last-a: person-a@example.com\n",
        encoding="utf-8",
    )
    # Trigger people_yml directory load (relink-derived also rebuilds the
    # directory from scratch, which is what we want for the curated badge).
    CliRunner().invoke(app, ["vault", "relink-derived"])

    result = CliRunner().invoke(app, ["people"])
    assert result.exit_code == 0, result.stdout
    # The person-x row should carry the curated checkmark; the Ali row should not.
    assert "person-x last-a" in result.stdout
    assert "✅" in result.stdout


def test_people_roster_json_output_shape(
    test_db: psycopg.Connection,
    fake_embedder: Any,
    patch_embedder: Any,
) -> None:
    """``brain people --json`` emits a list with the documented per-person shape."""
    patch_embedder(fake_embedder)
    _seed_gmail(test_db, fake_embedder, external_id="m1")

    result = CliRunner().invoke(app, ["people", "--json"])
    assert result.exit_code == 0, result.stdout
    payload = json.loads(result.stdout)
    assert isinstance(payload, list)
    assert payload, "expected at least one person in JSON output"
    for entry in payload:
        # Every documented field is present.
        for key in (
            "slug",
            "display_name",
            "primary_email",
            "all_emails",
            "doc_count",
            "in_people_yml",
            "docs",
        ):
            assert key in entry, f"missing key {key!r} in {entry!r}"
        # ``docs`` is a list and each row carries the documented fields.
        assert isinstance(entry["docs"], list)
        for doc in entry["docs"]:
            for key in ("id", "title", "source_kind", "date", "vault_target"):
                assert key in doc


def test_people_roster_min_docs_threshold_filters(
    test_db: psycopg.Connection,
    fake_embedder: Any,
    patch_embedder: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A high threshold filters out seeded 1-doc people (regression: env honored)."""
    patch_embedder(fake_embedder)
    _seed_gmail(test_db, fake_embedder, external_id="m1")
    # Override the autouse default of 0 so non-curated 1-doc people are filtered.
    monkeypatch.setenv("BRAIN_PEOPLE_HUB_MIN_DOCS", "10")

    result = CliRunner().invoke(app, ["people"])
    assert result.exit_code == 0, result.stdout
    # No curated entries exist; threshold strips everyone.
    assert "no people in scope" in result.stdout.lower()


def test_people_roster_owner_filter_strips_owner(
    test_db: psycopg.Connection,
    fake_embedder: Any,
    patch_embedder: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``BRAIN_OWNER_PARTICIPANTS`` strips the owner from the roster."""
    patch_embedder(fake_embedder)
    _seed_gmail(test_db, fake_embedder, external_id="m1")
    # Owner = the "Ali Sarkis" identity; should drop from the roster.
    monkeypatch.setenv(
        "BRAIN_OWNER_PARTICIPANTS",
        "ali sarkis,redacted@example.com",
    )

    result = CliRunner().invoke(app, ["people"])
    assert result.exit_code == 0, result.stdout
    # Counterparty stays.
    assert "person-x last-a" in result.stdout
    # Owner gone.
    assert "Ali Sarkis" not in result.stdout


# ---------------------------------------------------------------------------
# Detail view (with name argument).
# ---------------------------------------------------------------------------


def test_people_detail_shows_one_record_with_docs(
    test_db: psycopg.Connection,
    fake_embedder: Any,
    patch_embedder: Any,
) -> None:
    """``brain people <name>`` shows a per-doc table for that person."""
    patch_embedder(fake_embedder)
    _seed_gmail(
        test_db, fake_embedder, external_id="m1", title="Hello person-x"
    )

    result = CliRunner().invoke(app, ["people", "person-x"])
    assert result.exit_code == 0, result.stdout
    out = result.stdout
    # Title line carries the title-cased name + doc count.
    assert "person-x last-a" in out
    assert "1 doc" in out  # "1 doc(s)" with optional plural
    # Doc title row appears.
    assert "Hello person-x" in out
    # Source column carries the kind.
    assert "gmail" in out


def test_people_detail_case_insensitive_match(
    test_db: psycopg.Connection,
    fake_embedder: Any,
    patch_embedder: Any,
) -> None:
    """Lowercased input still resolves the title-cased display name."""
    patch_embedder(fake_embedder)
    _seed_gmail(test_db, fake_embedder, external_id="m1")

    result = CliRunner().invoke(app, ["people", "person-a"])
    assert result.exit_code == 0, result.stdout
    assert "person-x last-a" in result.stdout


def test_people_detail_substring_match(
    test_db: psycopg.Connection,
    fake_embedder: Any,
    patch_embedder: Any,
) -> None:
    """Partial substring (``"meh"`` → "person-x last-a") resolves cleanly."""
    patch_embedder(fake_embedder)
    _seed_gmail(test_db, fake_embedder, external_id="m1")

    result = CliRunner().invoke(app, ["people", "meh"])
    assert result.exit_code == 0, result.stdout
    assert "person-x last-a" in result.stdout


def test_people_detail_no_match_exits_nonzero(
    test_db: psycopg.Connection,
    fake_embedder: Any,
    patch_embedder: Any,
) -> None:
    """A name with no matches exits 1 with a clear stderr line."""
    patch_embedder(fake_embedder)
    _seed_gmail(test_db, fake_embedder, external_id="m1")

    result = CliRunner().invoke(app, ["people", "Nobody"])
    assert result.exit_code == 1
    # Typer routes ``err=True`` to stderr; CliRunner mixes both into ``output``
    # when ``mix_stderr`` is True (the default), so we look there.
    assert "no person matched" in result.output.lower()


def test_people_detail_ambiguous_match_warns_and_picks_first(
    test_db: psycopg.Connection,
    fake_embedder: Any,
    patch_embedder: Any,
) -> None:
    """Multiple matches → warn on stderr, pick the alpha-first match.

    Two distinct people whose names both contain ``"a"`` — the
    aggregator returns them sorted; the CLI's substring lookup picks
    the first and surfaces the count of remaining matches as a yellow
    warning so the user can disambiguate.
    """
    patch_embedder(fake_embedder)
    _seed_gmail(
        test_db,
        fake_embedder,
        external_id="m1",
        from_addr="Ali Sarkis <redacted@example.com>",
        to="person-x last-a <person-a@example.com>",
    )
    _seed_gmail(
        test_db,
        fake_embedder,
        external_id="m2",
        from_addr="Ali Sarkis <redacted@example.com>",
        to="Anna Lee <anna@example.com>",
    )

    # Substring "a" is in "ali sarkis", "anna lee", AND "person-a last-a" (the 'a'
    # in "last-a"). All three persons match.
    result = CliRunner().invoke(app, ["people", "a"])
    assert result.exit_code == 0, result.output
    out_lower = result.output.lower()
    assert "matches" in out_lower
    # The picked match (alpha-first) is Ali Sarkis.
    assert "Ali Sarkis" in result.output


def test_people_detail_json_emits_single_object(
    test_db: psycopg.Connection,
    fake_embedder: Any,
    patch_embedder: Any,
) -> None:
    """``brain people <name> --json`` emits a single dict, not a list."""
    patch_embedder(fake_embedder)
    _seed_gmail(test_db, fake_embedder, external_id="m1")

    result = CliRunner().invoke(app, ["people", "person-x", "--json"])
    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert isinstance(payload, dict)
    assert payload["display_name"] == "person-x last-a"
    assert payload["doc_count"] == 1
    assert isinstance(payload["docs"], list)
