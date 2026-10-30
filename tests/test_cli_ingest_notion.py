"""`brain ingest-notion` end to end (RM-35), through the CLI and the 5434 database.

Every export is synthetic (``tests/notion_fixtures.py``), built in ``tmp_path``
as a folder or a zip. No Notion account or API is involved.
"""
from __future__ import annotations

import zipfile
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import psycopg
import pytest
from typer.testing import CliRunner, Result

from brain.cli import app
from brain.ingest import notion_archive as na
from tests import notion_fixtures as fx
from tests.conftest import CountingEmbedder
from tests.secret_fixtures import SYNTHETIC_AWS_KEY

ESC_TITLE = "Evil \x1b[31mred\x1b[0m title"


@pytest.fixture
def embedder(counting_embedder: CountingEmbedder, patch_embedder: Any) -> CountingEmbedder:
    patch_embedder(counting_embedder)
    return counting_embedder


def _run(*args: str | Path) -> Result:
    return CliRunner().invoke(app, ["ingest-notion", *map(str, args)])


def _docs(conn: psycopg.Connection[Any]) -> dict[str, dict[str, Any]]:
    rows = conn.execute(
        """
        SELECT s.external_id, d.id::text, d.title, d.content, d.content_type, d.source_path,
               d.sensitivity, d.sent_at, d.metadata, d.tags
        FROM documents d JOIN sources s ON s.id = d.source_id
        WHERE s.kind = 'notion'
        """
    ).fetchall()
    keys = ("id", "title", "content", "content_type", "source_path", "sensitivity", "sent_at",
            "metadata", "tags")
    return {str(r[0]): dict(zip(keys, r[1:], strict=True)) for r in rows}


def _tree(tmp_path: Path, name: str = "export-1", **edits: str | bytes) -> Path:
    files = fx.standard_export()
    files.update(edits)
    return fx.write_tree(tmp_path / name, files)


# --- refusals ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("make", "message"),
    [
        (lambda p: p / "missing", "must be a folder or a .zip"),
        (lambda p: (p / "notes.txt").write_text("x") and p / "notes.txt",
         "must be a folder or a .zip"),
    ],
)
def test_a_path_that_is_not_an_export_is_refused(
    tmp_path: Path, make: Any, message: str, test_db: psycopg.Connection
) -> None:
    result = _run(make(tmp_path))

    assert result.exit_code == 2
    assert message in result.output
    assert _docs(test_db) == {}


def test_a_malformed_since_is_refused(tmp_path: Path) -> None:
    result = _run(_tree(tmp_path), "--since", "2026/10/01")

    assert result.exit_code == 2
    assert "--since must be YYYY-MM-DD" in result.output


def test_a_zero_max_is_refused(tmp_path: Path) -> None:
    assert _run(_tree(tmp_path), "--max", "0").exit_code == 2


def test_an_unknown_sensitivity_is_refused(tmp_path: Path, test_db: psycopg.Connection) -> None:
    result = _run(_tree(tmp_path), "--sensitivity", "secretish")

    assert result.exit_code == 1
    assert _docs(test_db) == {}


def test_a_crafted_zip_is_refused_and_writes_nothing(
    tmp_path: Path, embedder: CountingEmbedder, test_db: psycopg.Connection
) -> None:
    archive = fx.write_zip(tmp_path / "e.zip", {fx.HANDBOOK_PATH: fx.HANDBOOK_MD})
    with zipfile.ZipFile(archive, "a") as zf:
        zf.writestr("../escape.md", "crafted")

    result = _run(archive)

    assert result.exit_code == 1
    assert "absolute or leaves the archive" in result.output
    assert "escape" not in result.output
    assert _docs(test_db) == {}


# --- ingest (AC1, AC3, AC4) --------------------------------------------------------------


def test_a_folder_export_ingests_pages_by_id_and_counts_what_it_skips(
    tmp_path: Path, embedder: CountingEmbedder, test_db: psycopg.Connection
) -> None:
    result = _run(_tree(tmp_path))

    assert result.exit_code == 0, result.output
    docs = _docs(test_db)
    assert set(docs) == {fx.HANDBOOK_ID, fx.ONBOARDING_ID}
    handbook = docs[fx.HANDBOOK_ID]
    assert handbook["title"] == fx.HANDBOOK_TITLE
    assert handbook["content"] == fx.HANDBOOK_MD.strip()
    assert (handbook["content_type"], handbook["source_path"]) == ("notion_page", None)
    assert handbook["sensitivity"] == "normal"
    assert docs[fx.ONBOARDING_ID]["metadata"]["notion_parent_id"] == fx.HANDBOOK_ID
    assert "found 2 page(s)" in result.output
    assert "1 attachment(s) and image(s) skipped" in result.output
    assert "1 file(s) without a Notion page id skipped (never guessed)" in result.output
    assert "2 database row page(s) skipped (pass --with-databases)" in result.output
    assert "1 database table(s) (CSV) not stored" in result.output
    assert "2 ingested, 0 updated, 0 unchanged, 0 failed" in result.output


def test_a_title_falls_back_to_the_file_name_without_its_id(
    tmp_path: Path, embedder: CountingEmbedder, test_db: psycopg.Connection
) -> None:
    root = fx.write_tree(tmp_path / "e", {f"Quarter Plan {fx.LEDGER_ID}.md": "No heading here."})

    assert _run(root).exit_code == 0

    assert _docs(test_db)[fx.LEDGER_ID]["title"] == "Quarter Plan"


def test_titles_never_reach_the_terminal(
    tmp_path: Path, embedder: CountingEmbedder, test_db: psycopg.Connection
) -> None:
    root = fx.write_tree(tmp_path / "e", {f"Evil {fx.LEDGER_ID}.md": f"# {ESC_TITLE}\n\nbody"})

    result = _run(root)

    assert "\x1b[31m" not in result.output
    assert f"ingested {fx.LEDGER_ID}" in result.output


def test_with_databases_ingests_each_row_page_with_its_date(
    tmp_path: Path, embedder: CountingEmbedder, test_db: psycopg.Connection
) -> None:
    result = _run(_tree(tmp_path), "--with-databases")

    assert result.exit_code == 0, result.output
    docs = _docs(test_db)
    assert set(docs) == {fx.HANDBOOK_ID, fx.ONBOARDING_ID, fx.BOOK_ONE_ID, fx.BOOK_TWO_ID}
    book = docs[fx.BOOK_ONE_ID]
    assert book["metadata"]["notion_database_id"] == fx.READING_DB_ID
    assert book["sent_at"] == datetime(2026, 10, 20, 16, 12, tzinfo=UTC)
    assert "Status: Reading" in book["content"]
    assert "database row page(s) skipped" not in result.output


def test_with_databases_warns_when_a_table_has_rows_but_no_row_pages(
    tmp_path: Path, embedder: CountingEmbedder, test_db: psycopg.Connection
) -> None:
    files = fx.standard_export()
    del files[fx.BOOK_ONE_PATH]
    del files[fx.BOOK_TWO_PATH]
    files[f"Empty {fx.LEDGER_ID}.csv"] = "Name\n"  # no rows: no warning

    result = _run(fx.write_tree(tmp_path / "e", files), "--with-databases")

    assert result.exit_code == 0, result.output
    assert (
        f"database {fx.READING_DB_ID}: its table lists 2 row(s) but the export has no row pages"
        in result.output
    )
    assert f"database {fx.LEDGER_ID}" not in result.output


def test_link_and_oversize_skips_are_reported(
    tmp_path: Path, embedder: CountingEmbedder, monkeypatch: pytest.MonkeyPatch,
    test_db: psycopg.Connection,
) -> None:
    root = _tree(tmp_path, **{f"Huge {fx.LEDGER_ID}.md": "x" * 2_000, "Part-1.zip": b"PK"})
    (root / f"Linked {fx.BOOK_ONE_ID}.md").symlink_to(root / fx.HANDBOOK_PATH)
    monkeypatch.setattr(
        "brain.cli_ingest_notion.notion_export_limits", na.ExportLimits(max_file_bytes=1_000)
    )

    result = _run(root)

    assert result.exit_code == 0, result.output
    assert "1 link(s), hard link(s) or special file(s) skipped" in result.output
    assert "1 file(s) over the size cap skipped" in result.output
    assert "1 nested archive(s) skipped (unzip it and pass the inner archive)" in result.output
    assert fx.LEDGER_ID not in _docs(test_db)


# --- re-export (AC2) ---------------------------------------------------------------------


def test_a_re_export_to_a_new_folder_updates_in_place(
    tmp_path: Path, embedder: CountingEmbedder, test_db: psycopg.Connection
) -> None:
    assert _run(_tree(tmp_path, "export-1")).exit_code == 0
    before = _docs(test_db)
    edited = fx.HANDBOOK_MD + "\nA paragraph added in the second export.\n"

    result = _run(_tree(tmp_path, "export-2", **{fx.HANDBOOK_PATH: edited}))

    assert result.exit_code == 0, result.output
    after = _docs(test_db)
    assert set(after) == set(before)
    assert after[fx.HANDBOOK_ID]["id"] == before[fx.HANDBOOK_ID]["id"]
    assert after[fx.HANDBOOK_ID]["content"] == edited.strip()
    assert after[fx.ONBOARDING_ID] == before[fx.ONBOARDING_ID]
    assert f"updated {fx.HANDBOOK_ID}" in result.output
    assert "0 ingested, 1 updated, 1 unchanged, 0 failed" in result.output


def test_a_zip_export_then_a_folder_export_share_one_document_per_page(
    tmp_path: Path, embedder: CountingEmbedder, test_db: psycopg.Connection
) -> None:
    archive = fx.write_zip(tmp_path / "Export-1.zip", fx.standard_export(), prefix="Export-1/")

    first = _run(archive)
    second = _run(_tree(tmp_path, "Export-2"))

    assert first.exit_code == 0, first.output
    assert "2 ingested" in first.output
    assert "0 ingested, 0 updated, 2 unchanged, 0 failed" in second.output
    assert set(_docs(test_db)) == {fx.HANDBOOK_ID, fx.ONBOARDING_ID}


def test_a_corrupt_member_stops_the_run_after_reporting_what_was_written(
    tmp_path: Path, embedder: CountingEmbedder, test_db: psycopg.Connection
) -> None:
    files = {fx.HANDBOOK_PATH: fx.HANDBOOK_MD, f"Zed {fx.LEDGER_ID}.md": "# Zed\n" * 400}
    archive = fx.write_zip(tmp_path / "e.zip", files)
    data = bytearray(archive.read_bytes())
    # The name's first occurrence is its local header; its deflated data follows the name.
    at = data.find(f"Zed {fx.LEDGER_ID}.md".encode()) + len(f"Zed {fx.LEDGER_ID}.md") + 4
    data[at : at + 16] = bytes(b ^ 0xFF for b in data[at : at + 16])
    archive.write_bytes(bytes(data))

    result = _run(archive)

    assert result.exit_code == 1
    assert "1 ingested, 0 updated, 0 unchanged, 0 failed" in result.output
    assert "member is corrupt" in result.output
    assert set(_docs(test_db)) == {fx.HANDBOOK_ID}


# --- --since, --max (AC5), sensitivity, tags ----------------------------------------------


def test_since_keeps_recent_and_undated_pages_and_skips_older_ones(
    tmp_path: Path, embedder: CountingEmbedder, test_db: psycopg.Connection
) -> None:
    result = _run(_tree(tmp_path), "--with-databases", "--since", "2026-10-01")

    assert result.exit_code == 0, result.output
    assert set(_docs(test_db)) == {fx.HANDBOOK_ID, fx.ONBOARDING_ID, fx.BOOK_ONE_ID}
    assert "1 page(s) last edited before --since skipped" in result.output
    assert "2 page(s) without a readable last-edited date kept" in result.output


def test_max_stops_the_run_reports_the_remainder_and_the_next_run_continues(
    tmp_path: Path, embedder: CountingEmbedder, test_db: psycopg.Connection
) -> None:
    root = _tree(tmp_path)

    first = _run(root, "--with-databases", "--max", "1")
    second = _run(root, "--with-databases", "--max", "2")
    third = _run(root, "--with-databases", "--max", "2")

    assert "1 ingested" in first.output
    assert "stopped at --max 1: 3 page(s) remain; run again to continue" in first.output
    assert "2 ingested, 0 updated, 1 unchanged" in second.output
    assert "stopped at --max 2: 1 page(s) remain" in second.output
    assert "1 ingested, 0 updated, 3 unchanged" in third.output
    assert "remain" not in third.output
    assert len(_docs(test_db)) == 4


def test_pages_past_the_cap_that_are_unchanged_are_not_counted_as_remaining(
    tmp_path: Path, embedder: CountingEmbedder, test_db: psycopg.Connection
) -> None:
    assert _run(_tree(tmp_path, "export-1")).exit_code == 0
    edited = {
        fx.HANDBOOK_PATH: fx.HANDBOOK_MD + "\nedit\n",
        fx.ONBOARDING_PATH: fx.ONBOARDING_MD + "\nedit\n",
    }

    result = _run(_tree(tmp_path, "export-2", **edited), "--max", "1")

    assert "0 ingested, 1 updated, 0 unchanged" in result.output
    assert "stopped at --max 1: 1 page(s) remain" in result.output


def test_sensitivity_and_tags_apply_to_every_page(
    tmp_path: Path, embedder: CountingEmbedder, test_db: psycopg.Connection
) -> None:
    result = _run(
        _tree(tmp_path), "--sensitivity", "confidential", "--tag", "Team Wiki", "--no-enrich"
    )

    assert result.exit_code == 0, result.output
    docs = _docs(test_db).values()
    assert {d["sensitivity"] for d in docs} == {"confidential"}
    assert all(d["tags"] == ["team-wiki"] for d in docs)


# --- per-page failures --------------------------------------------------------------------


HOSTILE_TITLE = "\x1b]8;;https://example.invalid\x07Pay me\x1b]8;;\x07 \x1b[2J\x1b[31mRed"


def _hostile_export(tmp_path: Path) -> Path:
    body = f"# {HOSTILE_TITLE}\n\nThe key is {SYNTHETIC_AWS_KEY}.\n"
    return fx.write_tree(tmp_path / "e", {f"Hostile {fx.LEDGER_ID}.md": body})


def _assert_no_title_or_escape(output: str) -> None:
    assert "\x1b" not in output and "\x07" not in output
    assert "Pay me" not in output and "Hostile" not in output
    assert SYNTHETIC_AWS_KEY not in output


def test_a_rejected_page_prints_its_id_and_count_never_its_title(
    tmp_path: Path, embedder: CountingEmbedder, test_db: psycopg.Connection,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("BRAIN_SECRET_GUARD", "reject")

    result = _run(_hostile_export(tmp_path))

    assert f"refused {fx.LEDGER_ID}: 1 finding(s)" in result.output
    _assert_no_title_or_escape(result.output)
    assert _docs(test_db) == {}


@pytest.mark.parametrize(
    ("mode", "args", "marker"),
    [("warn", (), "stored UNCHANGED"), ("redact", (), "REDACTED"),
     ("reject", ("--allow-secrets",), "guard bypassed")],
)
def test_guard_notices_are_labelled_with_the_page_id(
    tmp_path: Path, embedder: CountingEmbedder, test_db: psycopg.Connection,
    monkeypatch: pytest.MonkeyPatch, mode: str, args: tuple[str, ...], marker: str,
) -> None:
    monkeypatch.setenv("BRAIN_SECRET_GUARD", mode)

    result = _run(_hostile_export(tmp_path), *args)

    assert result.exit_code == 0, result.output
    assert f'1 finding(s) in "{fx.LEDGER_ID}"' in result.output
    assert marker in result.output
    _assert_no_title_or_escape(result.output)
    stored = _docs(test_db)[fx.LEDGER_ID]["title"]
    assert "\x1b" not in stored and stored.endswith("Red")


def test_the_secret_guard_refuses_one_page_and_the_run_goes_on(
    tmp_path: Path, embedder: CountingEmbedder, test_db: psycopg.Connection,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("BRAIN_SECRET_GUARD", "reject")
    leaky = fx.ONBOARDING_MD + f"\nThe key is {SYNTHETIC_AWS_KEY}.\n"

    result = _run(_tree(tmp_path, **{fx.ONBOARDING_PATH: leaky}))

    assert f"refused {fx.ONBOARDING_ID}: 1 finding(s)" in result.output
    assert SYNTHETIC_AWS_KEY not in result.output
    assert set(_docs(test_db)) == {fx.HANDBOOK_ID}
    assert "1 ingested, 0 updated, 0 unchanged, 1 failed" in result.output


def test_a_write_failure_is_printed_by_class_and_counted(
    tmp_path: Path, embedder: CountingEmbedder, test_db: psycopg.Connection,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from brain import cli_ingest_notion

    real = cli_ingest_notion.ingest_document

    def failing(conn: Any, **kwargs: Any) -> Any:
        if kwargs["source_external_id"] == fx.ONBOARDING_ID:
            raise ValueError(f"row text {fx.ONBOARDING_TITLE}")
        return real(conn, **kwargs)

    monkeypatch.setattr(cli_ingest_notion, "ingest_document", failing)

    result = _run(_tree(tmp_path))

    assert f"failed {fx.ONBOARDING_ID}: ValueError" in result.output
    assert fx.ONBOARDING_TITLE not in result.output
    assert "1 ingested, 0 updated, 0 unchanged, 1 failed" in result.output


# --- pages that are not stored (rev-104 HIGH, aud-104 F1) ------------------------------

SHARED_BODY = "# Meeting notes\n\nAgenda: the synthetic quarterly sync.\n"


def _twins_and_an_empty(tmp_path: Path, name: str) -> Path:
    return fx.write_tree(
        tmp_path / name,
        {
            f"Meeting notes {fx.BOOK_ONE_ID}.md": SHARED_BODY,
            f"Meeting notes {fx.BOOK_TWO_ID}.md": SHARED_BODY,
            f"Untitled {fx.LEDGER_ID}.md": "  \n\n ",
        },
    )


def test_same_text_and_empty_pages_are_reported_by_id_never_as_unchanged(
    tmp_path: Path, embedder: CountingEmbedder, test_db: psycopg.Connection
) -> None:
    root = _twins_and_an_empty(tmp_path, "export-1")

    first = _run(root)
    second = _run(_twins_and_an_empty(tmp_path, "export-2"))

    for result in (first, second):
        assert result.exit_code == 0, result.output
        assert f"skipped {fx.BOOK_TWO_ID}: same text as an existing document" in result.output
        assert f"skipped {fx.LEDGER_ID}: empty" in result.output
        assert "1 page(s) not stored: same text as an existing document" in result.output
        assert "1 page(s) not stored: empty" in result.output
    assert "1 ingested, 0 updated, 0 unchanged, 0 failed" in first.output
    assert "0 ingested, 0 updated, 1 unchanged, 0 failed" in second.output
    assert set(_docs(test_db)) == {fx.BOOK_ONE_ID}


def test_same_text_and_empty_pages_never_count_as_remaining(
    tmp_path: Path, embedder: CountingEmbedder, test_db: psycopg.Connection
) -> None:
    files = {
        f"Meeting notes {fx.BOOK_ONE_ID}.md": SHARED_BODY,
        f"Meeting notes {fx.BOOK_TWO_ID}.md": SHARED_BODY,
        f"Untitled {fx.LEDGER_ID}.md": "",
        f"Zeta {fx.ONBOARDING_ID}.md": "# Zeta\n\nA page of its own.\n",
    }
    root = fx.write_tree(tmp_path / "e", files)

    first = _run(root, "--max", "1")
    second = _run(root, "--max", "1")
    third = _run(root, "--max", "1")

    assert "stopped at --max 1: 1 page(s) remain" in first.output
    for past_the_cap in (f"skipped {fx.BOOK_TWO_ID}: same text", f"skipped {fx.LEDGER_ID}: empty"):
        assert past_the_cap in first.output
    assert "1 page(s) not stored: empty" in first.output
    assert "1 ingested" in second.output and "remain" not in second.output
    assert "0 ingested, 0 updated, 2 unchanged" in third.output
    assert "remain" not in third.output
    assert set(_docs(test_db)) == {fx.BOOK_ONE_ID, fx.ONBOARDING_ID}


def test_a_corrupt_lzma_member_exits_1_through_the_cli(
    tmp_path: Path, embedder: CountingEmbedder, test_db: psycopg.Connection
) -> None:
    archive = tmp_path / "e.zip"
    name = f"Ledger {fx.LEDGER_ID}.md"
    with zipfile.ZipFile(archive, "w", compression=zipfile.ZIP_LZMA) as zf:
        zf.writestr(name, "# Ledger\n\n" + "synthetic ledger line\n" * 60)
    data = bytearray(archive.read_bytes())
    at = data.find(name.encode()) + len(name) + 9
    data[at : at + 24] = bytes(b ^ 0xFF for b in data[at : at + 24])
    archive.write_bytes(bytes(data))

    result = _run(archive)

    assert result.exit_code == 1
    assert "ingest-notion: the archive is not a readable zip, or a member is corrupt" in (
        result.output
    )
    assert _docs(test_db) == {}


def test_a_byte_order_mark_does_not_reach_the_title(
    tmp_path: Path, embedder: CountingEmbedder, test_db: psycopg.Connection
) -> None:
    root = fx.write_tree(tmp_path / "e", {f"Bom {fx.LEDGER_ID}.md": "﻿# Bom page\n\nbody\n"})

    assert _run(root).exit_code == 0

    stored = _docs(test_db)[fx.LEDGER_ID]
    assert stored["title"] == "Bom page"
    assert not stored["content"].startswith("﻿")


def test_an_unchanged_page_past_the_cap_is_not_remaining(
    tmp_path: Path, embedder: CountingEmbedder, test_db: psycopg.Connection
) -> None:
    assert _run(_tree(tmp_path, "export-1")).exit_code == 0
    edited = {fx.HANDBOOK_PATH: fx.HANDBOOK_MD + "\nedit\n"}  # sorts first; onboarding is unchanged

    result = _run(_tree(tmp_path, "export-2", **edited), "--max", "1")

    assert "0 ingested, 1 updated, 1 unchanged" in result.output  # counted, though past the cap
    assert "remain" not in result.output
