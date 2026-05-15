"""CLI tests for ``brain vault directory refresh`` and ``... directory show`` (Task B.7).

Both commands speak to the live ``directory_entries`` table; ``refresh``
also exercises the gws subprocess path. We neutralise gws the same way
``test_cli_relink_derived.py`` does — patch ``shutil.which`` to ``None``
so ``real_gws_runner`` raises ``DirectoryRefreshError`` on every call,
which the soft-fail in ``refresh_calendar`` / ``refresh_contacts``
already handles gracefully.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any

import psycopg
import pytest
from pytest_mock import MockerFixture
from typer.testing import CliRunner

from brain.cli import app
from brain.ingest import ExtractedDoc, ingest_document


def _gmail_doc(
    *,
    body: str = "hello",
    title: str = "Hi",
    from_addr: str | None = "Ali Sarkis <redacted@example.com>",
    to: str | None = "person-x last-a <person-a@example.com>",
    message_id: str = "m1",
) -> ExtractedDoc:
    """Minimal Gmail-shaped ``ExtractedDoc`` for ingest pipeline seeding."""
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
            "thread_id": f"t{message_id}",
            "label_ids": ["INBOX"],
        },
    )


@pytest.fixture(autouse=True)
def _stub_gws(mocker: MockerFixture) -> None:
    """Force ``real_gws_runner`` to fail-fast for every test in this module.

    ``directory refresh`` hard-codes ``real_gws_runner`` for Calendar /
    Contacts. Patching ``shutil.which`` short-circuits the PATH check
    inside ``brain.vault.derived_links.gws.real_gws_runner`` so it raises
    ``DirectoryRefreshError`` — exactly the "gws unavailable" path we
    want to exercise across the test matrix without relying on real
    Google Workspace auth.
    """
    mocker.patch("brain.vault.derived_links.gws.shutil.which", return_value=None)


@pytest.fixture(autouse=True)
def _isolate_vault_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Point ``BRAIN_VAULT_PATH`` at a per-test tmp dir.

    ``vault_directory_refresh`` now reads ``<vault>/_people.yml`` (Task
    B.7). Without this, every test would inherit whatever the user's
    real ``~/brain-vault/_people.yml`` happens to contain, making the
    "empty corpus" assertions flake.
    """
    monkeypatch.setenv("BRAIN_VAULT_PATH", str(tmp_path))


@pytest.fixture(autouse=True)
def _wide_console(monkeypatch: pytest.MonkeyPatch) -> None:
    """Widen Rich's auto-detected terminal so table cells aren't truncated.

    Rich consults ``COLUMNS`` first and falls back to TTY size. Without
    this, ``CliRunner``'s captured stdout is treated as an 80-column
    terminal and email/timestamp columns get clipped (e.g. ``ali@exampl…``)
    — which makes ``"ali@example.com" in result.stdout`` flap.
    """
    monkeypatch.setenv("COLUMNS", "240")


def _seed_gmail(
    conn: psycopg.Connection[Any],
    embedder: Any,
    *,
    external_id: str,
    body: str = "hello",
    from_addr: str = "Ali Sarkis <redacted@example.com>",
    to: str = "person-x last-a <person-a@example.com>",
) -> str:
    """Ingest a Gmail document; the post-ingest hook seeds directory_entries."""
    result = ingest_document(
        conn,
        embedder=embedder,
        doc=_gmail_doc(
            body=body,
            message_id=external_id,
            from_addr=from_addr,
            to=to,
        ),
        source_kind="gmail",
        source_external_id=external_id,
    )
    assert result.document_id is not None
    return result.document_id


def _insert_entry(
    conn: psycopg.Connection[Any],
    *,
    display_name: str,
    email: str,
    source: str,
    occurrence_count: int = 1,
) -> None:
    """Direct INSERT into directory_entries — bypasses the upsert path.

    Used to set up rows for ``directory show`` tests independently of
    the ingest pipeline so we can exercise sources (calendar, contacts,
    people_yml) that have no ingest-time hook.
    """
    conn.execute(
        """
        INSERT INTO directory_entries
            (display_name, email, source, occurrence_count)
        VALUES (%s, %s, %s, %s)
        """,
        (display_name, email, source, occurrence_count),
    )


def test_directory_refresh_runs_clean(
    test_db: psycopg.Connection,
    fake_embedder: Any,
    patch_embedder: Any,
) -> None:
    """Empty corpus + no gws → command exits 0 with the friendly empty message."""
    patch_embedder(fake_embedder)

    result = CliRunner().invoke(app, ["vault", "directory", "refresh"])
    assert result.exit_code == 0, result.stdout
    out = result.stdout.lower()
    # Either the empty-directory message or the "Done in" footer appears
    # — both are user signals that the command completed cleanly.
    assert "directory is empty" in out or "no entries" in out
    assert "done in" in out


def test_directory_refresh_populates_from_gmail_docs(
    test_db: psycopg.Connection,
    fake_embedder: Any,
    patch_embedder: Any,
) -> None:
    """Seeded Gmail docs → directory_entries has gmail rows after refresh."""
    patch_embedder(fake_embedder)
    _seed_gmail(test_db, fake_embedder, external_id="m1", body="first")
    _seed_gmail(
        test_db,
        fake_embedder,
        external_id="m2",
        body="second",
        from_addr="person-x last-a <person-a@example.com>",
        to="Ali Sarkis <redacted@example.com>",
    )

    result = CliRunner().invoke(app, ["vault", "directory", "refresh"])
    assert result.exit_code == 0, result.stdout

    rows = test_db.execute(
        "SELECT email FROM directory_entries WHERE source = 'gmail' "
        "ORDER BY email"
    ).fetchall()
    emails = {r[0] for r in rows}
    assert emails == {"redacted@example.com", "person-a@example.com"}


def test_directory_refresh_loads_people_yml(
    test_db: psycopg.Connection,
    fake_embedder: Any,
    patch_embedder: Any,
    tmp_path: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Regression for Task B.7: ``refresh`` loads ``<vault>/_people.yml``.

    Before this fix, the loader existed but no production caller wired
    it into the directory refresh pipeline, so ``_people.yml`` entries
    never reached ``directory_entries`` and the linker's name→email
    bridge silently lost the YAML override path.
    """
    patch_embedder(fake_embedder)
    monkeypatch.setenv("BRAIN_VAULT_PATH", str(tmp_path))
    (tmp_path / "_people.yml").write_text(
        "person-person-luke: person-person-luke@example.com\n"
        "person-person-marc: person-person-marc@example.com\n"
    )

    result = CliRunner().invoke(app, ["vault", "directory", "refresh"])
    assert result.exit_code == 0, result.stdout
    assert "_people.yml: 2 entries" in result.stdout

    rows = test_db.execute(
        "SELECT display_name, email FROM directory_entries "
        "WHERE source = 'people_yml' ORDER BY display_name"
    ).fetchall()
    assert rows == [
        ("person-person-luke", "person-person-luke@example.com"),
        ("person-person-marc", "person-person-marc@example.com"),
    ]


def test_directory_refresh_continues_when_gws_missing(
    test_db: psycopg.Connection,
    fake_embedder: Any,
    patch_embedder: Any,
) -> None:
    """Missing gws binary → command still exits 0 and the Gmail rescan runs.

    The ``_stub_gws`` autouse fixture forces ``shutil.which`` to ``None``,
    so every gws invocation raises and ``refresh_calendar`` /
    ``refresh_contacts`` log warnings + return 0. The Gmail rescan,
    which is pure SQL, is unaffected.
    """
    patch_embedder(fake_embedder)
    _seed_gmail(test_db, fake_embedder, external_id="m1", body="body")

    result = CliRunner().invoke(app, ["vault", "directory", "refresh"])
    assert result.exit_code == 0, result.stdout

    cnt = test_db.execute(
        "SELECT count(*) FROM directory_entries WHERE source = 'gmail'"
    ).fetchone()
    assert cnt is not None
    assert cnt[0] >= 2


def test_directory_show_lists_all_entries(
    test_db: psycopg.Connection,
    patch_embedder: Any,
    fake_embedder: Any,
) -> None:
    """Seeded entries from multiple sources all show up in stdout."""
    patch_embedder(fake_embedder)
    _insert_entry(
        test_db,
        display_name="ali sarkis",
        email="ali@example.com",
        source="gmail",
    )
    _insert_entry(
        test_db,
        display_name="person-a last-a",
        email="person-a@example.com",
        source="contacts",
    )
    _insert_entry(
        test_db,
        display_name="alice",
        email="alice@example.com",
        source="calendar",
    )

    result = CliRunner().invoke(app, ["vault", "directory", "show"])
    assert result.exit_code == 0, result.stdout
    out = result.stdout
    # All three emails appear.
    assert "ali@example.com" in out
    assert "person-a@example.com" in out
    assert "alice@example.com" in out
    # Source labels appear.
    assert "gmail" in out
    assert "contacts" in out
    assert "calendar" in out


def test_directory_show_filters_by_source(
    test_db: psycopg.Connection,
    patch_embedder: Any,
    fake_embedder: Any,
) -> None:
    """``--source gmail`` shows only gmail rows; other sources are filtered out."""
    patch_embedder(fake_embedder)
    _insert_entry(
        test_db,
        display_name="ali sarkis",
        email="ali@example.com",
        source="gmail",
    )
    _insert_entry(
        test_db,
        display_name="person-a last-a",
        email="person-a@example.com",
        source="contacts",
    )

    result = CliRunner().invoke(
        app, ["vault", "directory", "show", "--source", "gmail"]
    )
    assert result.exit_code == 0, result.stdout
    out = result.stdout
    assert "ali@example.com" in out
    # contacts row not shown — both the email and the source label tied
    # to the contacts row are absent from the rendered table.
    assert "person-a@example.com" not in out


def test_directory_show_invalid_source_errors(
    test_db: psycopg.Connection,
    patch_embedder: Any,
    fake_embedder: Any,
) -> None:
    """``--source bogus`` exits non-zero and the error names the valid options."""
    patch_embedder(fake_embedder)

    result = CliRunner().invoke(
        app, ["vault", "directory", "show", "--source", "bogus"]
    )
    assert result.exit_code != 0
    # The error message lists the valid source values so the user can
    # self-correct without consulting the schema.
    combined = (result.stdout + (result.stderr or "")).lower()
    assert "invalid" in combined
    assert "gmail" in combined
    assert "calendar" in combined
    assert "contacts" in combined
    assert "people_yml" in combined
