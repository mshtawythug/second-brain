"""Posts from an X data archive as documents (RM-44): ids, dates, handles, selection, the body.

Pure logic; :mod:`brain.ingest.social_archive` reads the files and hands this
module the inner object of each entry (``{"tweetId": …, "fullText": …,
"expandedUrl": …}``). Everything in an entry is someone else's text, so:

- **The id** must be a canonical decimal string (no leading zero) of at most
  19 ASCII digits below ``2**63``, checked before ``int()``, so one post has
  exactly one ``x-<id>`` key (sec-116 F3). Anything else skips the entry as
  ``malformed``.
- **The date** comes from the id, because an archive's like carries none: the
  platform's snowflake ids encode ``(id >> 22) + 1288834974657`` milliseconds
  since the Unix epoch. An id from before snowflakes (its offset under one
  minute) is undated; an id dated more than a day ahead is forged and skipped.
- **The URL is constructed, never copied** (plan D3):
  ``https://x.com/<handle>/status/<id>``, or ``https://x.com/i/web/status/<id>``
  without a handle. The handle comes from ``expandedUrl`` only when
  :func:`brain.ingest.web_url.normalize_url` (which drops the query, fragment,
  userinfo and ``;`` parameters) yields a platform host and exactly the path
  ``/<handle>/status/<this id>``. No query string, and no link the archive
  planted, can be stored.
- **The text** loses every control and format character except newline and tab
  (a NUL cannot be stored in Postgres; an ESC must not reach a terminal), and a
  text over :data:`MAX_TEXT_CHARS` is ``malformed``. A post with no text (deleted
  or private) is skipped as ``no_text`` (plan D4).
- **The title** is the start of the text, so it is built from the text after
  :func:`brain.ingest.guard.redact_secrets`, before it is cut, whatever the
  guard mode (sec-116 F1). The ingest guard redacts the body but never the
  title, and a title never needs the secret.

One post liked and bookmarked is one document. Its ``collections`` metadata is
volatile: a re-run unions it with the stored list, so a later archive without
the bookmarks file never removes ``bookmark``, and an identical re-run changes
nothing.
"""
from __future__ import annotations

import re
import unicodedata
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any, Final, Literal
from urllib.parse import urlsplit

from . import ExtractedDoc
from .guard import redact_secrets
from .text_safety import one_line
from .web_url import normalize_url

__all__ = [
    "COLLECTIONS",
    "CONTENT_TYPE",
    "MAX_TEXT_CHARS",
    "PLATFORM",
    "SKIP_MALFORMED",
    "SKIP_NO_TEXT",
    "SOURCE_KIND",
    "TITLE_CHARS",
    "TITLE_CHARS_WITH_HANDLE",
    "VOLATILE_KEYS",
    "Collected",
    "PostBatch",
    "SocialPost",
    "build_document",
    "collect_posts",
    "handle_from_url",
    "merge_posts",
    "post_from_entry",
    "select_posts",
    "snowflake_time",
    "volatile_metadata",
]

SOURCE_KIND: Final = "social"
CONTENT_TYPE: Final = "social_post"
PLATFORM: Final = "x"
#: The archive files read, by the key that wraps each of their entries.
COLLECTIONS: Final = ("like", "bookmark")
VOLATILE_KEYS: Final = ("collections",)

#: The platform's longest post is 25 000 characters; more is not a post.
MAX_TEXT_CHARS = 30_000
TITLE_CHARS = 80
TITLE_CHARS_WITH_HANDLE = 60

SKIP_NO_TEXT: Final = "no_text"
SKIP_MALFORMED: Final = "malformed"
Skip = Literal["no_text", "malformed"]

_TWEPOCH_MS = 1288834974657
_UNIX_EPOCH = datetime(1970, 1, 1, tzinfo=UTC)
_PRE_SNOWFLAKE_MS = 60_000
_FUTURE_SLACK = timedelta(days=1)
_MAX_ID = 2**63 - 1
_ID_RE = re.compile(r"[1-9][0-9]{0,18}")
_HANDLE_RE = re.compile(r"[A-Za-z0-9_]{1,15}")
_POST_HOSTS = frozenset(
    {"x.com", "www.x.com", "twitter.com", "www.twitter.com", "mobile.twitter.com"}
)
_KEPT_CONTROLS = frozenset("\n\t")
_ELLIPSIS = "…"


@dataclass(frozen=True)
class SocialPost:
    """One liked or bookmarked post, validated."""

    post_id: str
    text: str
    handle: str | None
    written_at: datetime | None
    collections: frozenset[str]

    @property
    def url(self) -> str:
        """The post's link, built from the id and handle (module docstring)."""
        author = self.handle if self.handle is not None else "i/web"
        return f"https://x.com/{author}/status/{self.post_id}"

    @property
    def external_id(self) -> str:
        """``x-<id>``: the dedup key under the ``social`` kind (plan D2)."""
        return f"{PLATFORM}-{self.post_id}"


def snowflake_time(post_id: int, *, now: datetime) -> datetime | None:
    """When ``post_id`` was written; ``None`` before snowflakes; ``ValueError`` if future."""
    offset = post_id >> 22
    if offset < _PRE_SNOWFLAKE_MS:
        return None
    written = _UNIX_EPOCH + timedelta(milliseconds=offset + _TWEPOCH_MS)
    if written > now + _FUTURE_SLACK:
        raise ValueError("the id is dated in the future")
    return written


def handle_from_url(raw: object, post_id: str) -> str | None:
    """The author handle in ``raw``, a status URL for ``post_id`` on the platform, else ``None``."""
    if not isinstance(raw, str):
        return None
    url = normalize_url(raw)
    if url is None:
        return None
    parts = urlsplit(url)
    if parts.scheme != "https" or parts.netloc not in _POST_HOSTS:
        return None
    segments = parts.path.split("/")
    if len(segments) != 4 or segments[0] or segments[2] != "status" or segments[3] != post_id:
        return None
    handle = segments[1]
    if _HANDLE_RE.fullmatch(handle) is None or handle.lower() == "i":
        return None
    return handle


def post_from_entry(
    entry: Mapping[str, Any], collection: str, *, now: datetime
) -> SocialPost | Skip:
    """``entry`` as a post, or the reason it is skipped (module docstring)."""
    if collection not in COLLECTIONS:
        raise ValueError(f"unknown collection {collection!r}")
    post_id = entry.get("tweetId")
    if not isinstance(post_id, str) or _ID_RE.fullmatch(post_id) is None:
        return SKIP_MALFORMED
    number = int(post_id)
    if number > _MAX_ID:
        return SKIP_MALFORMED
    try:
        written = snowflake_time(number, now=now)
    except ValueError:
        return SKIP_MALFORMED
    raw_text = entry.get("fullText")
    if raw_text is None:
        return SKIP_NO_TEXT
    if not isinstance(raw_text, str) or len(raw_text) > MAX_TEXT_CHARS:
        return SKIP_MALFORMED
    text = _clean_text(raw_text)
    if not text:
        return SKIP_NO_TEXT
    return SocialPost(
        post_id=post_id,
        text=text,
        handle=handle_from_url(entry.get("expandedUrl"), post_id),
        written_at=written,
        collections=frozenset({collection}),
    )


def _clean_text(raw: str) -> str:
    kept = (
        ch for ch in raw
        if ch in _KEPT_CONTROLS or not unicodedata.category(ch).startswith("C")
    )
    return "".join(kept).strip()


def merge_posts(posts: Iterable[SocialPost]) -> list[SocialPost]:
    """One post per id, in first-seen order: collections unioned, the first text and handle."""
    merged: dict[str, SocialPost] = {}
    for post in posts:
        seen = merged.get(post.post_id)
        if seen is None:
            merged[post.post_id] = post
            continue
        merged[post.post_id] = SocialPost(
            post_id=seen.post_id,
            text=seen.text,
            handle=seen.handle if seen.handle is not None else post.handle,
            written_at=seen.written_at,
            collections=seen.collections | post.collections,
        )
    return list(merged.values())


@dataclass(frozen=True)
class Collected:
    """Every usable post of a run, merged, and what was skipped on the way."""

    posts: tuple[SocialPost, ...]
    liked: int
    bookmarked: int
    no_text: int
    malformed: int


def collect_posts(
    likes: Sequence[Mapping[str, Any]],
    bookmarks: Sequence[Mapping[str, Any]],
    *,
    now: datetime,
) -> Collected:
    """Validate every entry of both files and merge the posts (module docstring)."""
    posts: list[SocialPost] = []
    skips = {SKIP_NO_TEXT: 0, SKIP_MALFORMED: 0}
    for collection, entries in (("like", likes), ("bookmark", bookmarks)):
        for entry in entries:
            outcome = post_from_entry(entry, collection, now=now)
            if isinstance(outcome, SocialPost):
                posts.append(outcome)
            else:
                skips[outcome] += 1
    merged = merge_posts(posts)
    return Collected(
        posts=tuple(merged),
        liked=sum(1 for post in merged if "like" in post.collections),
        bookmarked=sum(1 for post in merged if "bookmark" in post.collections),
        no_text=skips[SKIP_NO_TEXT],
        malformed=skips[SKIP_MALFORMED],
    )


@dataclass(frozen=True)
class PostBatch:
    """The posts one run writes, newest first, and what the window and the cap left out."""

    posts: tuple[SocialPost, ...]
    before_since: int
    undated: int
    remainder: int


def select_posts(
    posts: Iterable[SocialPost], *, since: datetime | None, limit: int
) -> PostBatch:
    """Posts written at or after ``since``, newest first (undated last), at most ``limit``.

    With ``since`` an undated post is left out, since nothing shows it is recent.
    """
    kept: list[SocialPost] = []
    before = undated = 0
    for post in posts:
        if since is not None and post.written_at is None:
            undated += 1
        elif since is not None and post.written_at is not None and post.written_at < since:
            before += 1
        else:
            kept.append(post)
    kept.sort(key=_newest_first, reverse=True)
    return PostBatch(
        posts=tuple(kept[:limit]),
        before_since=before,
        undated=undated,
        remainder=max(0, len(kept) - limit),
    )


def _newest_first(post: SocialPost) -> tuple[bool, datetime, int]:
    written = post.written_at
    return (written is not None, written or datetime.min.replace(tzinfo=UTC), int(post.post_id))


def build_document(post: SocialPost) -> ExtractedDoc:
    """``post`` as a ``social_post`` document; the body never depends on the collection."""
    line = one_line(redact_secrets(post.text)[0])
    if post.handle is not None:
        title = f"@{post.handle}: {_cut(line, TITLE_CHARS_WITH_HANDLE)}"
        body = f"@{post.handle}\n\n{post.text}\n\n{post.url}"
    else:
        title = _cut(line, TITLE_CHARS)
        body = f"{post.text}\n\n{post.url}"
    metadata: dict[str, Any] = {"platform": PLATFORM, "post_id": post.post_id, "url": post.url}
    if post.handle is not None:
        metadata["handle"] = post.handle
    if post.written_at is not None:
        metadata["date"] = post.written_at.isoformat()
    metadata["collections"] = sorted(post.collections)
    return ExtractedDoc(
        title=title,
        content=body,
        content_type=CONTENT_TYPE,
        source_path=None,
        metadata=metadata,
    )


def _cut(text: str, limit: int) -> str:
    return text if len(text) <= limit else f"{text[:limit]}{_ELLIPSIS}"


def volatile_metadata(post: SocialPost, stored: Mapping[str, Any]) -> dict[str, Any]:
    """``collections``: this run's unioned with the known names in ``stored``."""
    previous = stored.get("collections")
    known: set[str] = set()
    if isinstance(previous, list):
        known = {name for name in previous if name in COLLECTIONS}
    return {"collections": sorted(known | post.collections)}
