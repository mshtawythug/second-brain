"""Browser history and bookmarks as `web` documents for `brain ingest-web` (RM-41).

:mod:`brain.ingest.browser_store` reads the browser's rows; this module decides
which become pages and what a page's document holds. :mod:`brain.cli_ingest_web`
writes them.

**Selection** (:func:`select_pages`). Each URL is normalized first
(:func:`brain.ingest.web_url.normalize_url`: query string, fragment and userinfo
dropped), and an entry whose host is not allowlisted is discarded **before**
anything of it is kept: no visit count, no title. The surviving entries merge by
normalized URL, so the query-string variants of one page are one page and their
visits add up. A page qualifies when it is bookmarked (RM-41.D5: on any date) or
had at least ``min_visits`` visits inside the ``--since`` window (RM-41.D13).
Pages are ordered newest visit first (bookmark-only pages last, by URL), and at
most ``limit`` are returned; the rest are counted.

**The document** (:func:`build_document`). One per normalized URL (ticket D1),
keyed ``(web, <sha256 of the url>)`` (:func:`page_external_id`; the URL itself is
``metadata.url``). The key is a hash, not the URL, because the vault mirror names a
file after the first characters of ``sources.external_id``, and a URL's ``https://``
would make that name a directory (RM-41.D17). The body is the title, the URL and the host, plus the
page text under ``--fetch``; it holds no visit counts or times, because a body
change re-chunks and re-embeds. Those live in ``metadata`` under per-browser
keys (:func:`volatile_keys`, RM-41.D4), so Chrome and Safari can both describe
one page without overwriting each other. ``metadata.date`` (promoted to
``sent_at``) is the first visit seen, set when the page is created.
"""
from __future__ import annotations

import dataclasses
import hashlib
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Protocol

from . import ExtractedDoc
from .browser_store import BROWSERS, Browser, BrowserRows
from .text_safety import one_line
from .web_url import host_of, normalize_url

__all__ = [
    "ALL_VOLATILE_KEYS",
    "CONTENT_TYPE",
    "DEFAULT_MIN_VISITS",
    "MAX_TITLE_CHARS",
    "SOURCE_KIND",
    "Page",
    "PageBatch",
    "build_document",
    "page_external_id",
    "select_pages",
    "volatile_keys",
    "volatile_metadata",
    "with_stored_keys",
]

SOURCE_KIND = "web"
CONTENT_TYPE = "web_page"
#: The ticket's default for "revisited": three visits inside the window.
DEFAULT_MIN_VISITS = 3
#: The longest title kept; a page title is a line, not a paragraph.
MAX_TITLE_CHARS = 300


def page_external_id(url: str) -> str:
    """``sources.external_id`` for the page at the normalized ``url``: its SHA-256, in hex."""
    return hashlib.sha256(url.encode("utf-8")).hexdigest()


def volatile_keys(browser: Browser) -> tuple[str, ...]:
    """The metadata keys one browser's run refreshes without touching the body."""
    return (f"{browser}_visits", f"{browser}_last_visit", f"{browser}_bookmarked")


#: Every browser's volatile keys, for carrying the other browser's keys across a rebuild.
ALL_VOLATILE_KEYS: tuple[str, ...] = tuple(key for b in BROWSERS for key in volatile_keys(b))


@dataclass(frozen=True)
class Page:
    """One normalized URL on an allowlisted host, and what the browser says about it."""

    url: str
    host: str
    title: str
    visits: int
    first_seen: datetime | None
    last_visit: datetime | None
    bookmarked: bool

    @property
    def external_id(self) -> str:
        return page_external_id(self.url)


@dataclass(frozen=True)
class PageBatch:
    """The selected pages, and what was left out and why."""

    pages: tuple[Page, ...]
    remainder: int = 0
    off_allowlist: int = 0
    below_threshold: int = 0
    unusable: int = 0


@dataclass
class _Merged:
    host: str
    visits: int = 0
    first_seen: datetime | None = None
    last_visit: datetime | None = None
    history_title: str = ""
    history_title_at: datetime | None = None
    bookmark_title: str = ""
    bookmarked: bool = False


class _Allowlist(Protocol):
    """The one method :func:`select_pages` needs (``WebAllowlist`` satisfies it)."""

    def allows(self, host: str) -> bool: ...


def select_pages(
    rows: BrowserRows, allowlist: _Allowlist, *, min_visits: int, limit: int
) -> PageBatch:
    """The pages to write this run (module docstring)."""
    merged: dict[str, _Merged] = {}
    off_allowlist = unusable = 0
    for row in rows.history:
        entry = _entry(merged, row.url, allowlist)
        if isinstance(entry, str):
            off_allowlist += entry == "off"
            unusable += entry == "unusable"
            continue
        entry.visits += row.visits
        entry.first_seen = _earliest(entry.first_seen, row.first_visit)
        entry.last_visit = _latest(entry.last_visit, row.last_visit)
        title = one_line(row.title)
        if title and (entry.history_title_at is None or row.last_visit > entry.history_title_at):
            entry.history_title, entry.history_title_at = title, row.last_visit
    for bookmark in rows.bookmarks:
        entry = _entry(merged, bookmark.url, allowlist)
        if isinstance(entry, str):
            off_allowlist += entry == "off"
            unusable += entry == "unusable"
            continue
        entry.bookmarked = True
        entry.bookmark_title = entry.bookmark_title or one_line(bookmark.title)
    pages = [_page(url, entry) for url, entry in merged.items()]
    qualified = [p for p in pages if p.bookmarked or p.visits >= min_visits]
    qualified.sort(key=_newest_first)
    return PageBatch(
        pages=tuple(qualified[:limit]),
        remainder=max(0, len(qualified) - limit),
        off_allowlist=off_allowlist,
        below_threshold=len(pages) - len(qualified),
        unusable=unusable,
    )


def _entry(merged: dict[str, _Merged], raw: str, allowlist: _Allowlist) -> _Merged | str:
    """The merge slot for ``raw``, or ``"unusable"`` / ``"off"`` when it is not kept."""
    url = normalize_url(raw)
    if url is None:
        return "unusable"
    host = host_of(url)
    if not allowlist.allows(host):
        return "off"
    return merged.setdefault(url, _Merged(host=host))


def _page(url: str, entry: _Merged) -> Page:
    title = entry.bookmark_title or entry.history_title or url
    return Page(
        url=url,
        host=entry.host,
        title=title[:MAX_TITLE_CHARS],
        visits=entry.visits,
        first_seen=entry.first_seen,
        last_visit=entry.last_visit,
        bookmarked=entry.bookmarked,
    )


def _newest_first(page: Page) -> tuple[bool, float, str]:
    """Newest visit first; bookmark-only pages (no visit) last; then by URL."""
    stamp = page.last_visit.timestamp() if page.last_visit else 0.0
    return page.last_visit is None, -stamp, page.url


def _earliest(current: datetime | None, candidate: datetime) -> datetime:
    return candidate if current is None or candidate < current else current


def _latest(current: datetime | None, candidate: datetime) -> datetime:
    return candidate if current is None or candidate > current else current


def volatile_metadata(page: Page, browser: Browser) -> dict[str, Any]:
    """This browser's volatile keys for ``page``."""
    visits_key, last_key, bookmarked_key = volatile_keys(browser)
    return {
        visits_key: page.visits,
        last_key: page.last_visit.isoformat() if page.last_visit else None,
        bookmarked_key: page.bookmarked,
    }


def build_document(
    page: Page, browser: Browser, *, fetched_text: str | None = None
) -> ExtractedDoc:
    """``page`` as a document (module docstring)."""
    body = f"{page.title}\n\n{page.url}\n\nhost: {page.host}"
    if fetched_text and fetched_text.strip():
        body = f"{body}\n\n{fetched_text.strip()}"
    metadata: dict[str, Any] = {"url": page.url, "host": page.host}
    if page.first_seen is not None:
        metadata["date"] = page.first_seen.isoformat()
    metadata.update(volatile_metadata(page, browser))
    return ExtractedDoc(
        title=page.title,
        content=body,
        content_type=CONTENT_TYPE,
        source_path=None,
        metadata=metadata,
    )


def with_stored_keys(
    doc: ExtractedDoc, stored: Mapping[str, Any], browser: Browser
) -> ExtractedDoc:
    """``doc`` keeping, from ``stored``, the other browsers' volatile keys and ``date``.

    ``ingest_document`` replaces ``metadata`` wholesale when a body changes
    (``--refresh``), which would otherwise drop what another browser's run wrote
    and move ``sent_at`` to this window's first visit. ``date`` is the first visit
    the page was created with, and stays.
    """
    own = set(volatile_keys(browser))
    carried = {
        key: stored[key]
        for key in (*ALL_VOLATILE_KEYS, "date")
        if key not in own and key in stored
    }
    return dataclasses.replace(doc, metadata={**doc.metadata, **carried})
