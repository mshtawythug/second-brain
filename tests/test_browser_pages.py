"""Which browser entries become `web` documents, and what each document holds (RM-41).

Pure: rows in (:class:`~brain.ingest.browser_store.BrowserRows`), pages and
documents out. The allowlist is checked on the normalized host before anything
else is kept, so an entry off the list never reaches a page, a count of visits,
or a title.
"""
from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from brain.ingest import browser as br
from brain.ingest.browser_store import BookmarkRow, BrowserRows, HistoryRow
from brain.settings.web_allowlist import WebAllowlist

ALLOW = WebAllowlist(exact=frozenset({"example.com"}), suffixes=frozenset({"example.org"}))
T0 = datetime(2026, 10, 20, 9, 0, tzinfo=UTC)
HOUR = timedelta(hours=1)


def _row(url: str, title: str = "T", visits: int = 3, *, first: datetime = T0,
         last: datetime | None = None) -> HistoryRow:
    return HistoryRow(url=url, title=title, visits=visits, first_visit=first,
                      last_visit=last or first + HOUR)


def _select(history: tuple[HistoryRow, ...] = (), bookmarks: tuple[BookmarkRow, ...] = (),
            *, min_visits: int = 3, limit: int = 500) -> br.PageBatch:
    return br.select_pages(
        BrowserRows(history=history, bookmarks=bookmarks), ALLOW,
        min_visits=min_visits, limit=limit,
    )


def test_constants() -> None:
    assert br.SOURCE_KIND == "web"
    assert br.CONTENT_TYPE == "web_page"
    assert br.DEFAULT_MIN_VISITS == 3
    assert br.volatile_keys("chrome") == ("chrome_visits", "chrome_last_visit",
                                          "chrome_bookmarked")
    assert (*br.volatile_keys("chrome"), *br.volatile_keys("safari")) == br.ALL_VOLATILE_KEYS


def test_an_often_visited_allowlisted_page_is_selected_with_its_query_stripped() -> None:
    batch = _select((_row("https://example.com/doc?session=synthetic-token#top", "Doc", 4),))

    (page,) = batch.pages
    assert page.url == "https://example.com/doc"
    assert page.host == "example.com"
    assert page.title == "Doc"
    assert page.visits == 4
    assert page.bookmarked is False


def test_entries_off_the_allowlist_never_become_pages() -> None:
    batch = _select(
        (_row("https://example.net/x", visits=100), _row("https://example.org/apex", visits=9)),
        (BookmarkRow("https://www.example.com/sub", "Bookmarked but off-list"),),
    )

    assert batch.pages == ()
    assert batch.off_allowlist == 3


def test_a_page_below_the_threshold_is_not_selected() -> None:
    batch = _select((_row("https://example.com/rare", visits=2),))

    assert batch.pages == ()
    assert batch.below_threshold == 1


def test_a_bookmark_qualifies_without_visits_and_names_the_page() -> None:
    batch = _select(
        (_row("https://docs.example.org/guide?ref=x", "History title", 1),),
        (BookmarkRow("https://docs.example.org/guide", "  Bookmark\x1b[31m name  "),),
    )

    (page,) = batch.pages
    assert page.bookmarked is True
    assert page.title == "Bookmark[31m name"
    assert page.visits == 1


def test_a_bookmark_only_page_has_no_visit_times() -> None:
    (page,) = _select((), (BookmarkRow("https://example.com/b", ""),)).pages

    assert page.visits == 0
    assert page.first_seen is None
    assert page.last_visit is None
    assert page.title == "https://example.com/b"


def test_query_variants_of_one_page_merge_and_sum_their_visits() -> None:
    batch = _select((
        _row("https://example.com/p?a=1", "Older", 2, first=T0, last=T0 + HOUR),
        _row("https://example.com/p?a=2", "Newer", 2, first=T0 + HOUR, last=T0 + 5 * HOUR),
        _row("https://example.com/p#frag", "", 1, first=T0 - HOUR, last=T0 + 9 * HOUR),
    ))

    (page,) = batch.pages
    assert page.visits == 5
    assert page.first_seen == T0 - HOUR
    assert page.last_visit == T0 + 9 * HOUR
    assert page.title == "Newer"  # the newest non-empty history title


def test_unusable_urls_are_counted_not_kept() -> None:
    batch = _select(
        (_row("javascript:alert(1)"), _row("file:///etc/hosts")),
        (BookmarkRow("chrome://settings", "x"),),
    )

    assert batch.pages == ()
    assert batch.unusable == 3


def test_pages_are_newest_first_and_capped_with_a_remainder() -> None:
    rows = tuple(
        _row(f"https://example.com/{n}", str(n), 3, first=T0, last=T0 + n * HOUR)
        for n in range(4)
    )
    bookmark_only = (BookmarkRow("https://example.com/zz", "Saved"),)

    batch = _select(rows, bookmark_only, limit=3)

    assert [page.url for page in batch.pages] == [
        "https://example.com/3", "https://example.com/2", "https://example.com/1"
    ]
    assert batch.remainder == 2


def test_bookmark_only_pages_sort_after_visited_ones_by_url() -> None:
    batch = _select((), (BookmarkRow("https://example.com/b", "B"),
                         BookmarkRow("https://example.com/a", "A")))

    assert [page.url for page in batch.pages] == ["https://example.com/a",
                                                   "https://example.com/b"]


def test_a_long_title_is_cut() -> None:
    (page,) = _select((_row("https://example.com/l", "x" * 1000),)).pages

    assert len(page.title) == br.MAX_TITLE_CHARS


def test_min_visits_one_selects_every_visited_allowlisted_page() -> None:
    assert len(_select((_row("https://example.com/once", visits=1),), min_visits=1).pages) == 1


# --- the document ------------------------------------------------------------------------------


def _page(**overrides: object) -> br.Page:
    values: dict[str, object] = {
        "url": "https://example.com/doc", "host": "example.com", "title": "Doc",
        "visits": 4, "first_seen": T0, "last_visit": T0 + HOUR, "bookmarked": False,
    }
    values.update(overrides)
    return br.Page(**values)  # type: ignore[arg-type]


def test_the_body_is_title_url_and_host() -> None:
    doc = br.build_document(_page(), "chrome")

    assert doc.title == "Doc"
    assert doc.content == "Doc\n\nhttps://example.com/doc\n\nhost: example.com"
    assert doc.content_type == "web_page"
    assert doc.source_path is None


def test_fetched_text_is_appended_to_the_body() -> None:
    doc = br.build_document(_page(), "safari", fetched_text="Page text.")

    assert doc.content.endswith("host: example.com\n\nPage text.")


def test_empty_fetched_text_adds_nothing() -> None:
    assert br.build_document(_page(), "chrome", fetched_text="  ").content.endswith(
        "host: example.com"
    )


def test_metadata_carries_the_volatile_keys_of_one_browser_only() -> None:
    doc = br.build_document(_page(bookmarked=True), "safari")

    assert doc.metadata == {
        "url": "https://example.com/doc",
        "host": "example.com",
        "date": T0.isoformat(),
        "safari_visits": 4,
        "safari_last_visit": (T0 + HOUR).isoformat(),
        "safari_bookmarked": True,
    }
    assert br.volatile_metadata(_page(bookmarked=True), "safari") == {
        "safari_visits": 4,
        "safari_last_visit": (T0 + HOUR).isoformat(),
        "safari_bookmarked": True,
    }


def test_a_bookmark_only_page_has_no_date() -> None:
    doc = br.build_document(_page(first_seen=None, last_visit=None, visits=0), "chrome")

    assert "date" not in doc.metadata
    assert doc.metadata["chrome_last_visit"] is None


def test_the_body_holds_no_visit_counts_or_times() -> None:
    doc = br.build_document(_page(visits=12345), "chrome")

    assert "12345" not in doc.content
    assert T0.isoformat()[:10] not in doc.content


@pytest.mark.parametrize("browser", ["chrome", "safari"])
def test_with_stored_keys_keeps_the_other_browsers_keys_and_the_first_date(
    browser: str,
) -> None:
    other = "safari" if browser == "chrome" else "chrome"
    stored = {
        f"{other}_visits": 7, f"{other}_last_visit": "x", f"{other}_bookmarked": True,
        f"{browser}_visits": 1, "url": "stale", "summary_hint": "kept out",
        "date": "2026-01-01T00:00:00+00:00",
    }
    doc = br.build_document(_page(), browser)  # type: ignore[arg-type]

    merged = br.with_stored_keys(doc, stored, browser)  # type: ignore[arg-type]

    assert merged.metadata[f"{other}_visits"] == 7
    assert merged.metadata[f"{other}_bookmarked"] is True
    assert merged.metadata[f"{browser}_visits"] == 4
    assert merged.metadata["url"] == "https://example.com/doc"
    assert merged.metadata["date"] == "2026-01-01T00:00:00+00:00"
    assert "summary_hint" not in merged.metadata
    assert f"{other}_visits" not in doc.metadata  # the input document is not mutated
    assert doc.metadata["date"] == T0.isoformat()


def test_with_stored_keys_without_a_stored_date_keeps_the_new_one() -> None:
    doc = br.build_document(_page(), "chrome")

    assert br.with_stored_keys(doc, {}, "chrome").metadata["date"] == T0.isoformat()
