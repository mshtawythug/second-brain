"""The pure half of RM-35: names to page ids, the export plan, and page text to a document."""
from __future__ import annotations

from datetime import datetime
from pathlib import PurePosixPath

import pytest

from brain.ingest import notion_export as ne
from brain.ingest.notion_export import ExportFile
from tests import notion_fixtures as fx

pytestmark = pytest.mark.nodb


def _files(export: fx.Export) -> list[ExportFile]:
    return [ExportFile(PurePosixPath(rel), len(fx._bytes(c))) for rel, c in export.items()]


def _plan(export: fx.Export, *, with_databases: bool = False) -> ne.ExportPlan:
    return ne.plan_export(_files(export), with_databases=with_databases)


def _ref(plan: ne.ExportPlan, page_id: str) -> ne.PageRef:
    return next(p for p in plan.pages if p.page_id == page_id)


# --- names --------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("stem", "expected"),
    [
        (f"Team Handbook {fx.HANDBOOK_ID}", ("Team Handbook", fx.HANDBOOK_ID)),
        (f"Untitled {fx.HANDBOOK_ID.upper()}", ("Untitled", fx.HANDBOOK_ID)),
        (fx.HANDBOOK_ID, ("", fx.HANDBOOK_ID)),
        (f"A  spaced   title {fx.HANDBOOK_ID}", ("A  spaced   title", fx.HANDBOOK_ID)),
    ],
)
def test_split_name_takes_the_trailing_32_hex_id(stem: str, expected: tuple[str, str]) -> None:
    assert ne.split_name(stem) == expected


@pytest.mark.parametrize(
    "stem",
    [
        "Loose Note",
        f"Title {fx.HANDBOOK_ID[:-1]}",  # 31 hex
        f"Title {fx.HANDBOOK_ID}0",  # 33 hex
        f"Title{fx.HANDBOOK_ID}",  # no separator
        f"Title {fx.HANDBOOK_ID[:-1]}g",  # not hex
        f"Title {fx.HANDBOOK_ID} copy",  # id not at the end
    ],
)
def test_split_name_never_guesses_an_id(stem: str) -> None:
    assert ne.split_name(stem) is None


# --- the plan -------------------------------------------------------------------------


def test_plan_holds_pages_and_counts_everything_it_skips() -> None:
    plan = _plan(fx.standard_export())

    assert [p.page_id for p in plan.pages] == [fx.HANDBOOK_ID, fx.ONBOARDING_ID]  # parents first
    assert plan.skipped == {
        ne.SKIP_ATTACHMENT: 1,
        ne.SKIP_NO_ID: 1,
        ne.SKIP_DATABASE_ROW: 2,
    }
    assert [d.database_id for d in plan.databases] == [fx.READING_DB_ID]
    assert plan.databases[0].row_pages == 2
    assert plan.databases[0].table.path == PurePosixPath(fx.READING_ALL_CSV_PATH)


def test_plan_with_databases_includes_the_row_pages() -> None:
    plan = _plan(fx.standard_export(), with_databases=True)

    assert {p.page_id for p in plan.pages} == {
        fx.HANDBOOK_ID, fx.ONBOARDING_ID, fx.BOOK_ONE_ID, fx.BOOK_TWO_ID,
    }
    assert ne.SKIP_DATABASE_ROW not in plan.skipped
    assert _ref(plan, fx.BOOK_ONE_ID).database_id == fx.READING_DB_ID
    assert _ref(plan, fx.BOOK_ONE_ID).parent_id == fx.READING_DB_ID
    assert _ref(plan, fx.HANDBOOK_ID).database_id is None


def test_parent_is_the_nearest_folder_with_an_id() -> None:
    export = {
        **fx.standard_export(),
        f"Plain/{fx.HANDBOOK_ID}/Deep {fx.LEDGER_ID}.md": "x",
        f"Team Handbook {fx.HANDBOOK_ID}/Plain folder/Deeper {fx.BOOK_TWO_ID}.md": "x",
    }
    del export[fx.HANDBOOK_PATH]  # keep the handbook id unique
    del export[fx.BOOK_TWO_PATH]

    plan = _plan(export)

    assert _ref(plan, fx.ONBOARDING_ID).parent_id == fx.HANDBOOK_ID
    assert _ref(plan, fx.LEDGER_ID).parent_id == fx.HANDBOOK_ID
    assert _ref(plan, fx.LEDGER_ID).name_title == "Deep"
    assert _ref(plan, fx.BOOK_TWO_ID).parent_id == fx.HANDBOOK_ID  # past a folder with no id


def test_a_root_page_has_no_parent() -> None:
    assert _ref(_plan(fx.standard_export()), fx.HANDBOOK_ID).parent_id is None


def test_a_plain_csv_alone_marks_a_database() -> None:
    export = fx.standard_export()
    del export[fx.READING_ALL_CSV_PATH]

    plan = _plan(export)

    assert plan.databases[0].table.path == PurePosixPath(fx.READING_CSV_PATH)
    assert plan.skipped[ne.SKIP_DATABASE_ROW] == 2


def test_a_folder_without_a_matching_csv_is_not_a_database() -> None:
    export = fx.standard_export()
    del export[fx.READING_ALL_CSV_PATH]
    del export[fx.READING_CSV_PATH]

    plan = _plan(export)

    assert plan.databases == ()
    assert ne.SKIP_DATABASE_ROW not in plan.skipped
    assert _ref(plan, fx.BOOK_ONE_ID).database_id is None


def test_a_database_with_no_row_pages_is_still_listed() -> None:
    export = fx.standard_export()
    del export[fx.BOOK_ONE_PATH]
    del export[fx.BOOK_TWO_PATH]

    plan = _plan(export)

    assert [(d.database_id, d.row_pages) for d in plan.databases] == [(fx.READING_DB_ID, 0)]


def test_a_csv_without_an_id_is_counted_as_no_id() -> None:
    plan = _plan({"Orphan table.csv": "a,b\n1,2\n"})

    assert plan.skipped == {ne.SKIP_NO_ID: 1}
    assert plan.databases == ()


def test_every_copy_of_a_repeated_id_is_skipped() -> None:
    export = {
        fx.HANDBOOK_PATH: fx.HANDBOOK_MD,
        f"Copy/Team Handbook {fx.HANDBOOK_ID}.md": fx.HANDBOOK_MD,
        f"Ledger {fx.LEDGER_ID}.md": "# Ledger\n",
    }

    plan = _plan(export)

    assert [p.page_id for p in plan.pages] == [fx.LEDGER_ID]
    assert plan.skipped == {ne.SKIP_DUPLICATE_ID: 2}


def test_nested_archives_and_attachments_are_counted_apart() -> None:
    plan = _plan({"Part-1.zip": b"PK", "a.PNG": b"x", "b.pdf": b"y", "Note.MD": "# n"})

    assert plan.skipped == {ne.SKIP_ARCHIVE: 1, ne.SKIP_ATTACHMENT: 2, ne.SKIP_NO_ID: 1}


def test_macos_metadata_is_an_attachment_not_a_duplicate_page() -> None:
    export = {
        fx.HANDBOOK_PATH: fx.HANDBOOK_MD,
        f"__MACOSX/._{fx.HANDBOOK_PATH}": b"\x00\x05\x16\x07",
        f"Sub/._Ledger {fx.LEDGER_ID}.md": b"\x00\x05\x16\x07",
    }

    plan = _plan(export)

    assert [p.page_id for p in plan.pages] == [fx.HANDBOOK_ID]
    assert plan.skipped == {ne.SKIP_ATTACHMENT: 2}


def test_page_suffix_match_ignores_case() -> None:
    plan = _plan({f"Shout {fx.LEDGER_ID}.MD": "# Shout\n"})

    assert [p.page_id for p in plan.pages] == [fx.LEDGER_ID]


# --- one page -------------------------------------------------------------------------


def test_build_page_uses_the_heading_title_and_keeps_the_body() -> None:
    ref = _ref(_plan(fx.standard_export()), fx.ONBOARDING_ID)

    page = ne.build_page(ref, fx.ONBOARDING_MD)

    assert page.doc.title == fx.ONBOARDING_TITLE
    assert page.doc.content == fx.ONBOARDING_MD.strip()
    assert page.doc.content_type == ne.CONTENT_TYPE == "notion_page"
    assert page.doc.source_path is None
    assert page.doc.metadata == {
        "notion_page_id": fx.ONBOARDING_ID,
        "notion_parent_id": fx.HANDBOOK_ID,
    }
    assert page.page_id == fx.ONBOARDING_ID
    assert page.last_edited is None and page.has_last_edited is False


def test_build_page_falls_back_to_the_name_without_its_id() -> None:
    ref = _ref(_plan({f"Quarter Plan {fx.LEDGER_ID}.md": "x"}), fx.LEDGER_ID)

    assert ne.build_page(ref, "Body without a heading.").doc.title == "Quarter Plan"


@pytest.mark.parametrize("text", ["#   \n\nbody", "", "## Second level\n"])
def test_build_page_falls_back_when_the_heading_is_missing_or_empty(text: str) -> None:
    ref = _ref(_plan({f"{fx.LEDGER_ID}.md": "x"}), fx.LEDGER_ID)

    assert ne.build_page(ref, text).doc.title == ne.UNTITLED


def test_build_page_strips_control_sequences_from_the_title() -> None:
    ref = _ref(_plan({f"Evil \x1b[31mname {fx.LEDGER_ID}.md": "x"}), fx.LEDGER_ID)
    heading = "# \x1b]8;;https://example.invalid\x07Click\x1b]8;;\x07 \x1b[2Jme\u202e\n\nbody"

    page = ne.build_page(ref, heading)

    assert page.doc.title == "]8;;https://example.invalidClick]8;; [2Jme"
    assert not any(ch in page.doc.title for ch in "\x1b\x07\u202e")
    assert ne.build_page(ref, "no heading").doc.title == "Evil [31mname"


def test_a_heading_of_only_control_characters_falls_back_to_the_name() -> None:
    ref = _ref(_plan({f"Plain {fx.LEDGER_ID}.md": "x"}), fx.LEDGER_ID)

    assert ne.build_page(ref, "# \x1b\x07\x9b\n\nbody").doc.title == "Plain"


def test_the_title_is_capped() -> None:
    ref = _ref(_plan({f"Long {fx.LEDGER_ID}.md": "x"}), fx.LEDGER_ID)

    page = ne.build_page(ref, "# " + "a" * 8_000 + "\n\nbody")

    assert page.doc.title == "a" * ne.MAX_TITLE_CHARS
    assert page.doc.content.startswith("# " + "a" * 8_000)  # the body keeps the heading


def test_build_page_removes_nul_characters() -> None:
    ref = _ref(_plan({f"Nul {fx.LEDGER_ID}.md": "x"}), fx.LEDGER_ID)

    assert "\x00" not in ne.build_page(ref, "# Nul\x00 page\n\nbo\x00dy").doc.content
    assert ne.build_page(ref, "# Nul\x00 page\n\nbody").doc.title == "Nul page"


def test_a_row_page_carries_its_database_and_last_edited_date() -> None:
    ref = _ref(_plan(fx.standard_export(), with_databases=True), fx.BOOK_ONE_ID)

    page = ne.build_page(ref, fx.BOOK_ONE_MD)

    assert page.last_edited == datetime(2026, 10, 20, 16, 12)
    assert page.doc.metadata == {
        "notion_page_id": fx.BOOK_ONE_ID,
        "notion_parent_id": fx.READING_DB_ID,
        "notion_database_id": fx.READING_DB_ID,
        "last_edited": "2026-10-20T16:12:00",
        "date": "2026-10-20T16:12:00",
    }


@pytest.mark.parametrize(
    ("line", "expected"),
    [
        ("Last edited time: October 20, 2026 4:12 PM", datetime(2026, 10, 20, 16, 12)),
        ("Last edited time: October 20, 2026", datetime(2026, 10, 20)),
        ("last edited: 2026-10-20", datetime(2026, 10, 20)),
        ("Last Edited Time: 2026-10-20 08:05", datetime(2026, 10, 20, 8, 5)),
        ("Last edited time: 2026-10-20T08:05:00", datetime(2026, 10, 20, 8, 5)),
        ("Last edited time: 2026-10-20T08:05:00+02:00", datetime(2026, 10, 20, 6, 5)),
    ],
)
def test_last_edited_formats(line: str, expected: datetime) -> None:
    assert ne.last_edited(f"# T\n\nCreated: x\n{line}\n\nbody") == (True, expected)


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("# T\n\nLast edited time: sometime soon\n", (True, None)),
        ("# T\n\nStatus: Done\n", (False, None)),
        ("# T\n\nBody first.\nLast edited time: October 20, 2026\n", (False, None)),
        ("# T\n\nStatus: Done\n\nLast edited time: October 20, 2026\n", (False, None)),
        ("No heading\nLast edited time: October 20, 2026\n", (False, None)),
        ("# T", (False, None)),
    ],
)
def test_last_edited_reads_only_the_property_block(
    text: str, expected: tuple[bool, datetime | None]
) -> None:
    assert ne.last_edited(text) == expected


def test_an_unparseable_last_edited_is_present_but_undated() -> None:
    ref = _ref(_plan({f"Row {fx.LEDGER_ID}.md": "x"}), fx.LEDGER_ID)

    page = ne.build_page(ref, "# Row\n\nLast edited time: soon\n")

    assert (page.has_last_edited, page.last_edited) == (True, None)
    assert "date" not in page.doc.metadata


# --- the CSV row count ------------------------------------------------------------------


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        (fx.READING_CSV, 2),
        ("Name,Status\n", 0),
        ("", 0),
        ("Name\n\n,\nOne\n", 1),
        ('Name\n"multi\nline",x\n', 1),
    ],
)
def test_csv_row_count(text: str, expected: int) -> None:
    assert ne.csv_row_count(text) == expected


def test_csv_row_count_is_none_for_a_malformed_table() -> None:
    assert ne.csv_row_count('Name\n"never closed\n') is None
