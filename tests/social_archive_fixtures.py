"""Synthetic X data-archive trees for the `brain ingest-bookmarks` tests (RM-44); never real.

Every builder writes under a test's ``tmp_path`` (or a scratch directory) from the
named constants below. Handles are invented (``example_author``), post ids are
built from fixed instants with :func:`post_id_at`, and the text is made up.

The file shape follows the archive's published description: ``data/like.js`` is
``window.YTD.like.part0 = [ {"like": {"tweetId": …, "fullText": …,
"expandedUrl": …}}, … ]``, and a later part ``N`` is ``data/like-partN.js`` with
``part<N>`` in its prefix. The bookmarks file has the same shape under
``bookmark`` (plan D8).
"""
from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

TWEPOCH_MS = 1288834974657

NOW = datetime(2026, 10, 30, 12, 0, tzinfo=UTC)
DAY = timedelta(days=1)
AUTHOR = "example_author"
OTHER_AUTHOR = "sample_writer"
TEXT = "A synthetic post about retrieval and ranking."
OTHER_TEXT = "Another invented post on chunking strategies."


def post_id_at(instant: datetime, seq: int = 0) -> str:
    """A snowflake id whose encoded timestamp is ``instant`` (millisecond precision)."""
    millis = int(instant.timestamp() * 1000) - TWEPOCH_MS
    return str((millis << 22) | seq)


def status_url(post_id: str, handle: str | None = AUTHOR, *, host: str = "x.com") -> str:
    """The ``expandedUrl`` an archive entry might carry for ``post_id``."""
    if handle is None:
        return f"https://{host}/i/web/status/{post_id}"
    return f"https://{host}/{handle}/status/{post_id}"


def fields(
    post_id: str,
    text: str | None = TEXT,
    expanded_url: str | None = None,
    **extra: Any,
) -> dict[str, Any]:
    """The inner object of one entry; ``None`` leaves a key out."""
    inner: dict[str, Any] = {"tweetId": post_id}
    if text is not None:
        inner["fullText"] = text
    if expanded_url is not None:
        inner["expandedUrl"] = expanded_url
    inner.update(extra)
    return inner


def payload(collection: str, part: int, entries: Sequence[Mapping[str, Any]]) -> str:
    """One archive file's text: the ``window.YTD`` prefix and the wrapped entries."""
    body = json.dumps([{collection: dict(entry)} for entry in entries], indent=2)
    return f"window.YTD.{collection}.part{part} = {body}"


def file_name(collection: str, part: int) -> str:
    return f"{collection}.js" if part == 0 else f"{collection}-part{part}.js"


def write_archive(
    root: Path,
    likes: Sequence[Sequence[Mapping[str, Any]]] | None = (),
    bookmarks: Sequence[Sequence[Mapping[str, Any]]] | None = None,
) -> Path:
    """Write ``root/data/`` with one file per part; ``None`` writes no file for that list.

    ``likes=[[a, b], [c]]`` writes ``like.js`` (part 0) and ``like-part1.js``.
    """
    data = root / "data"
    data.mkdir(parents=True, exist_ok=True)
    for collection, parts in (("like", likes), ("bookmark", bookmarks)):
        if parts is None:
            continue
        for part, entries in enumerate(parts or [[]]):
            (data / file_name(collection, part)).write_text(
                payload(collection, part, entries), encoding="utf-8"
            )
    return root
