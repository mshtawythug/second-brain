"""CLI integration tests for ``brain vault relink-derived`` (Task B.6).

The command is a one-shot maintenance pass: rescan every Gmail document
into ``directory_entries``, backfill ``metadata._participant_keys`` for
every Krisp doc, refresh Calendar/Contacts via the ``gws`` CLI, and
rebuild every ``derived_links`` edge across the Gmail+Krisp corpus.

These tests run through Typer's ``CliRunner`` against the real Postgres
test DB (per ``conftest.test_db``). The ``gws`` shell-out is neutralized
in two complementary ways:

* ``mocker.patch("brain.vault.derived_links.gws.shutil.which", …)`` makes
  the production ``real_gws_runner`` raise ``DirectoryRefreshError`` on
  every call — ``refresh_calendar`` / ``refresh_contacts`` already catch
  that and downgrade it to a soft warning, so the directory rescan and
  linker pass still run cleanly.
* ``patch_embedder`` from ``conftest`` swaps in the deterministic fake
  embedder so we don't depend on Ollama.
"""
from __future__ import annotations

import json
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
    body: str = "Hello world",
    title: str = "Hi",
    from_addr: str | None = "Ali Sarkis <redacted@example.com>",
    to: str | None = "person-x last-a <person-a@example.com>",
    message_id: str = "m1",
    thread_id: str | None = None,
) -> ExtractedDoc:
    """Build a minimal ``ExtractedDoc`` shaped like ``gmail.to_extracted_doc``.

    Mirrors the helper in ``test_cli_ingest_gmail.py`` but inlined so this
    module stays self-contained. ``thread_id`` defaults to ``f"t{message_id}"``
    when omitted, matching the production extractor's threading shape.
    """
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


def _seed_krisp_without_participant_keys(
    conn: psycopg.Connection[Any],
    embedder: Any,
    *,
    external_id: str,
    body: str,
    title: str = "Krisp call",
    date: str = "2026-04-29",
) -> str:
    """Seed a Krisp doc whose ``metadata`` has NO ``_participant_keys``.

    Mimics docs ingested before B.3's pre-insert hook landed: ingest
    normally (which DOES populate the field via the hook), then strip the
    key back out via a direct UPDATE so the test starts in the pre-B.3
    state. Returns the document id.
    """
    result = ingest_document(
        conn,
        embedder=embedder,
        doc=ExtractedDoc(
            title=title,
            content=body,
            content_type="transcript",
            source_path=None,
            metadata={"date": date},
        ),
        source_kind="krisp",
        source_external_id=external_id,
    )
    assert result.document_id is not None
    # Strip _participant_keys to simulate a pre-B.3 ingest. Reading the
    # stored metadata first preserves any other fields the pipeline added
    # (e.g. ``date``) instead of zeroing them.
    row = conn.execute(
        "SELECT metadata FROM documents WHERE id = %s",
        (result.document_id,),
    ).fetchone()
    assert row is not None
    metadata = dict(row[0] or {})
    metadata.pop("_participant_keys", None)
    conn.execute(
        "UPDATE documents SET metadata = %s::jsonb WHERE id = %s",
        (json.dumps(metadata), result.document_id),
    )
    return result.document_id


def _seed_gmail(
    conn: psycopg.Connection[Any],
    embedder: Any,
    *,
    external_id: str,
    body: str,
    from_addr: str = "Ali Sarkis <redacted@example.com>",
    to: str = "person-x last-a <person-a@example.com>",
    thread_id: str | None = None,
) -> str:
    """Ingest a Gmail document via the real pipeline and return its id.

    Routes through ``ingest_document`` so the ``sources`` and ``documents``
    rows are wired the same way production wires them — the ``relink-derived``
    command's ``SELECT d.id FROM documents d JOIN sources s …`` join only
    finds the docs if they're stitched correctly. The Gmail post-ingest hook
    will already populate some ``directory_entries`` rows; that's intentional
    — the rescan must be idempotent over those.
    """
    result = ingest_document(
        conn,
        embedder=embedder,
        doc=_gmail_doc(
            body=body,
            message_id=external_id,
            from_addr=from_addr,
            to=to,
            thread_id=thread_id,
        ),
        source_kind="gmail",
        source_external_id=external_id,
    )
    assert result.document_id is not None
    return result.document_id


@pytest.fixture(autouse=True)
def _isolate_vault_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Point ``BRAIN_VAULT_PATH`` at a per-test tmp dir.

    ``vault_relink_derived`` now reads ``<vault>/_people.yml`` (Task B.7).
    Without this, every test would inherit whatever the user's real
    ``~/brain-vault/_people.yml`` happens to contain, making the
    "empty corpus" assertions flake.
    """
    monkeypatch.setenv("BRAIN_VAULT_PATH", str(tmp_path))


@pytest.fixture(autouse=True)
def _stub_gws(mocker: MockerFixture) -> None:
    """Force ``real_gws_runner`` to fail fast for every test.

    The CLI hard-codes ``real_gws_runner`` for Calendar/Contacts refresh.
    Patching ``shutil.which`` inside the ``gws`` module short-circuits the
    PATH check and raises ``DirectoryRefreshError`` — ``refresh_calendar``
    and ``refresh_contacts`` catch it and return 0, which is exactly the
    "gws unavailable" path we want to exercise across all CLI tests.

    This is NOT monkey-patching internals: ``shutil.which`` is stdlib and
    ``mocker.patch`` is a managed test double with auto-cleanup (CLAUDE.md
    rule 13 explicitly allows this pattern).
    """
    mocker.patch("brain.vault.derived_links.gws.shutil.which", return_value=None)


def test_relink_derived_runs_on_empty_corpus(
    test_db: psycopg.Connection,
    fake_embedder: Any,
    patch_embedder: Any,
) -> None:
    """Empty DB → command exits 0 with a "no linkable documents" line."""
    patch_embedder(fake_embedder)

    result = CliRunner().invoke(app, ["vault", "relink-derived"])
    assert result.exit_code == 0, result.stdout
    # Empty corpus message is the user's signal that nothing matched the
    # Gmail/Krisp filter — keep the assertion case-insensitive so we can
    # tweak the exact wording without churning tests.
    combined = result.stdout.lower()
    assert "no linkable documents" in combined
    # Done line still fires so the user knows the command finished.
    assert "done" in combined

    # No directory_entries either, because the Gmail rescan walked zero docs.
    cnt = test_db.execute("SELECT count(*) FROM directory_entries").fetchone()
    assert cnt is not None
    assert cnt[0] == 0


def test_relink_derived_populates_directory_from_gmail(
    test_db: psycopg.Connection,
    fake_embedder: Any,
    patch_embedder: Any,
) -> None:
    """Two seeded Gmail docs → directory_entries has rows tagged ``source='gmail'``."""
    patch_embedder(fake_embedder)
    _seed_gmail(test_db, fake_embedder, external_id="m1", body="first body")
    _seed_gmail(
        test_db,
        fake_embedder,
        external_id="m2",
        body="second body",
        from_addr="person-x last-a <person-a@example.com>",
        to="Ali Sarkis <redacted@example.com>",
    )

    result = CliRunner().invoke(app, ["vault", "relink-derived"])
    assert result.exit_code == 0, result.stdout

    # Both addresses are present, both with source='gmail'. The Gmail
    # post-ingest hook also writes these, so the count is whatever the hook
    # plus the rescan produced — we only care that the rows EXIST and are
    # tagged correctly.
    rows = test_db.execute(
        "SELECT email, source FROM directory_entries "
        "WHERE source = 'gmail' ORDER BY email"
    ).fetchall()
    emails = {r[0] for r in rows}
    assert emails == {"redacted@example.com", "person-a@example.com"}
    # All rows tagged gmail (sanity).
    assert all(r[1] == "gmail" for r in rows)


def test_relink_derived_creates_derived_edges(
    test_db: psycopg.Connection,
    fake_embedder: Any,
    patch_embedder: Any,
) -> None:
    """Two Gmail docs in the same thread → at least one ``shared_thread`` edge."""
    patch_embedder(fake_embedder)
    _seed_gmail(
        test_db,
        fake_embedder,
        external_id="m1",
        body="first message in thread",
        thread_id="thread-shared",
    )
    _seed_gmail(
        test_db,
        fake_embedder,
        external_id="m2",
        body="reply in thread",
        thread_id="thread-shared",
    )

    result = CliRunner().invoke(app, ["vault", "relink-derived"])
    assert result.exit_code == 0, result.stdout

    rows = test_db.execute(
        "SELECT rule FROM derived_links WHERE rule = 'shared_thread'"
    ).fetchall()
    assert len(rows) >= 1


def test_relink_derived_idempotent(
    test_db: psycopg.Connection,
    fake_embedder: Any,
    patch_embedder: Any,
) -> None:
    """Running twice produces the same final edge set (no duplicates)."""
    patch_embedder(fake_embedder)
    _seed_gmail(
        test_db,
        fake_embedder,
        external_id="m1",
        body="first message",
        thread_id="t-shared",
    )
    _seed_gmail(
        test_db,
        fake_embedder,
        external_id="m2",
        body="second message",
        thread_id="t-shared",
    )

    runner = CliRunner()
    first = runner.invoke(app, ["vault", "relink-derived"])
    assert first.exit_code == 0, first.stdout
    first_edges = set(
        test_db.execute(
            "SELECT src_document_id::text, dst_document_id::text, rule "
            "FROM derived_links"
        ).fetchall()
    )
    first_count = len(first_edges)
    assert first_count >= 1

    second = runner.invoke(app, ["vault", "relink-derived"])
    assert second.exit_code == 0, second.stdout
    second_edges = set(
        test_db.execute(
            "SELECT src_document_id::text, dst_document_id::text, rule "
            "FROM derived_links"
        ).fetchall()
    )
    # The DELETE+INSERT inside ``rebuild_derived_for`` regenerates new row
    # ids on each run; the (src, dst, rule) signatures should be stable.
    assert second_edges == first_edges
    # Sanity: count matches too — same rows, no UNIQUE-constraint violations.
    assert len(second_edges) == first_count


def test_relink_derived_continues_when_gws_missing(
    test_db: psycopg.Connection,
    fake_embedder: Any,
    patch_embedder: Any,
) -> None:
    """Missing ``gws`` binary → command still exits 0 and Gmail rescan runs.

    The autouse ``_stub_gws`` fixture forces ``shutil.which`` to return
    None, simulating a host without ``gws``. ``refresh_calendar`` and
    ``refresh_contacts`` log a warning and return 0, but the Gmail rescan
    doesn't depend on ``gws`` at all — it walks the local DB only.
    """
    patch_embedder(fake_embedder)
    _seed_gmail(test_db, fake_embedder, external_id="m1", body="body 1")

    result = CliRunner().invoke(app, ["vault", "relink-derived"])
    assert result.exit_code == 0, result.stdout

    rows = test_db.execute(
        "SELECT count(*) FROM directory_entries WHERE source = 'gmail'"
    ).fetchone()
    assert rows is not None
    # The Gmail rescan ran independently of gws; the directory has at least
    # the two addresses (from + to) from the seeded message.
    assert rows[0] >= 2


def test_relink_derived_summary_includes_counts(
    test_db: psycopg.Connection,
    fake_embedder: Any,
    patch_embedder: Any,
) -> None:
    """CLI stdout contains the directory + derived_links summary keywords."""
    patch_embedder(fake_embedder)
    _seed_gmail(
        test_db,
        fake_embedder,
        external_id="m1",
        body="hello there",
        thread_id="t-shared",
    )
    _seed_gmail(
        test_db,
        fake_embedder,
        external_id="m2",
        body="hi back",
        thread_id="t-shared",
    )

    result = CliRunner().invoke(app, ["vault", "relink-derived"])
    assert result.exit_code == 0, result.stdout

    out = result.stdout.lower()
    # The user-visible summary mentions both halves (directory + derived
    # links) and the elapsed time, plus at least one numeric count somewhere
    # so the report is more than just labels.
    assert "directory" in out
    # The Rich table title uses "derived links by rule"; tolerate both
    # singular and plural by matching the prefix.
    assert "derived" in out
    assert "done in" in out
    # Edge count line appears whenever the corpus is non-empty.
    assert "edges" in out


def test_relink_derived_backfills_krisp_participant_keys(
    test_db: psycopg.Connection,
    fake_embedder: Any,
    patch_embedder: Any,
) -> None:
    """A pre-B.3 Krisp doc's ``_participant_keys`` is restored from its body.

    Seeds a Krisp doc whose stored body has two speaker labels but whose
    metadata lacks ``_participant_keys`` (simulating a doc ingested before
    the B.3 pre-insert hook landed). After ``relink-derived`` runs, the
    metadata MUST have ``_participant_keys`` populated with the sorted,
    normalized speaker keys parsed from the body.
    """
    patch_embedder(fake_embedder)
    body = "**Ali Sarkis | 0:01**\nHey.\n\n**person-x | 0:02**\nHi back.\n"
    doc_id = _seed_krisp_without_participant_keys(
        test_db, fake_embedder, external_id="meeting-backfill", body=body
    )
    # Sanity: the seeding helper actually stripped the field.
    pre = test_db.execute(
        "SELECT metadata FROM documents WHERE id = %s", (doc_id,)
    ).fetchone()
    assert pre is not None
    assert "_participant_keys" not in (pre[0] or {})

    result = CliRunner().invoke(app, ["vault", "relink-derived"])
    assert result.exit_code == 0, result.stdout

    post = test_db.execute(
        "SELECT metadata FROM documents WHERE id = %s", (doc_id,)
    ).fetchone()
    assert post is not None
    keys = (post[0] or {}).get("_participant_keys")
    # ``extract_krisp_speakers`` returns normalized lowercase names; the
    # backfill stores the sorted list. Both labels in the body resolve, so
    # we expect both keys, sorted alphabetically.
    assert keys == ["ali sarkis", "person-a"]
    # The user-facing summary surfaces the backfill count.
    assert "backfilled" in result.stdout.lower()


def test_relink_derived_overwrites_stale_krisp_keys(
    test_db: psycopg.Connection,
    fake_embedder: Any,
    patch_embedder: Any,
) -> None:
    """Stale ``_participant_keys`` get overwritten to match the current body.

    Body parsing is the source of truth; the backfill ignores whatever
    happens to live in metadata at run time. A doc whose stored keys are
    out of sync with its body (e.g., from an older body that was later
    edited via ``brain edit``) snaps back into alignment.
    """
    patch_embedder(fake_embedder)
    body = "**Ali Sarkis | 0:01**\nHey.\n\n**person-x | 0:02**\nHi back.\n"
    # Ingest normally (the pre-insert hook will write keys based on body),
    # then OVERWRITE the keys with garbage so the test verifies the
    # backfill replaces, not just fills-when-missing.
    doc_id = _seed_krisp_without_participant_keys(
        test_db, fake_embedder, external_id="meeting-stale", body=body
    )
    test_db.execute(
        "UPDATE documents SET metadata = "
        "jsonb_set(metadata, '{_participant_keys}', %s::jsonb) "
        "WHERE id = %s",
        (json.dumps(["someone-who-doesnt-exist"]), doc_id),
    )

    result = CliRunner().invoke(app, ["vault", "relink-derived"])
    assert result.exit_code == 0, result.stdout

    post = test_db.execute(
        "SELECT metadata FROM documents WHERE id = %s", (doc_id,)
    ).fetchone()
    assert post is not None
    keys = (post[0] or {}).get("_participant_keys")
    # Stale value gone; current-body-derived value present.
    assert keys == ["ali sarkis", "person-a"]
    assert "someone-who-doesnt-exist" not in (keys or [])
