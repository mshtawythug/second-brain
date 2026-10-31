"""Read an X data archive's likes and bookmarks as untrusted input (RM-44).

The archive is someone else's file tree, and its files are JavaScript. Nothing
here trusts a name, a link, a size or the code:

- **Never executed.** A file is ``window.YTD.<collection>.part<N> = [ … ]``.
  The prefix is matched by an anchored pattern that must name the expected
  collection and part; the rest is read as ONE JSON array, one element at a
  time (``json.JSONDecoder.raw_decode``), and only whitespace and one ``;`` may
  follow it. ``NaN`` and ``Infinity`` are refused. There is no JavaScript engine
  and no ``eval`` (``tests/test_social_archive.py`` scans the module's AST).
- **Fixed names, no walk.** Only ``data/like.js``, ``data/like-part<N>.js``,
  ``data/bookmark.js`` and ``data/bookmark-part<N>.js`` are opened, probed by
  name from part 0 until the first missing part, relative to directory
  descriptors. No name from the archive is ever joined to a path. Files are
  opened ``O_NOCTTY`` too, so a device node cannot become a controlling
  terminal before ``fstat`` refuses it (sec-116 F5).
- **No links.** ``data`` is opened ``O_DIRECTORY | O_NOFOLLOW`` under the
  root's descriptor; each file ``O_NOFOLLOW | O_NONBLOCK`` under ``data``'s.
  The open descriptor must be a regular file with one link (``fstat``), so a
  symlink, FIFO, device, folder or hard link is refused, with no window between
  the check and the read. The root folder itself is the owner's argument and
  may be a link.
- **Bounded.** :class:`ArchiveLimits`: a per-file size (checked on the open
  descriptor before reading; the read stops one byte past that size, so a file
  that grows while it is read is refused, sec-116 F4), a run-wide read total, a
  part count per collection, an entry count, and a count of JSON objects and
  arrays per file (sec-116 F2). JSON nesting deep enough to exhaust the parser's
  recursion, an over-long number and invalid UTF-8 refuse the file.
- **Memory follows the kept text, not the JSON** (sec-116 F2, rev-116).
  Decoding one element can allocate many times its own text (a flat array of
  short strings is about 17x), so no element is ever decoded past
  :data:`MAX_ELEMENT_CHARS`: each is decoded from a slice of the text that
  starts small and doubles up to that cap, and an element that is still
  incomplete at the cap refuses the file. The transient cost of one element is
  therefore bounded whatever it holds (containers or scalars). The ``{``/``[``
  count is also checked before anything is parsed. Each entry is then cut
  down, as it is parsed, to the three string fields
  :mod:`brain.ingest.social_posts` reads (``tweetId``, ``fullText``,
  ``expandedUrl``), so nothing else is retained. An entry with any of those
  over :data:`MAX_FIELD_CHARS`, or a ``tweetId`` or ``fullText`` that is
  present but not a string, keeps nothing at all, so its post is skipped as
  malformed and no single long string is held until the whole archive is read
  (sec-116 R1). A JSON ``null`` counts as absent.
- **Loud on an unknown shape** (the ticket's risk line). A wrong prefix, a
  payload that is not one array, or an element that is not
  ``{"<collection>": {…}}`` refuses the whole run. Field-level problems inside
  an entry are :mod:`brain.ingest.social_posts`' to count and skip.

Every error is a :class:`SocialArchiveError` with a fixed message naming the
archive file (a name this module chose), never the file's content.
"""
from __future__ import annotations

import errno
import json
import os
import re
import stat
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any, NoReturn

from ..errors import BrainError

__all__ = [
    "ArchiveEntries",
    "ArchiveLimits",
    "SocialArchiveError",
    "parse_payload",
    "read_archive",
]

MAX_FILE_BYTES = 64 * 1024 * 1024
MAX_READ_BYTES = 256 * 1024 * 1024
MAX_PARTS = 64
MAX_ENTRIES = 500_000
#: A real entry is two containers (``{"like": {…}}``); text braces add a few.
MAX_CONTAINERS = 2 * MAX_ENTRIES + 1_000_000
#: The only entry fields kept, and only when they are strings.
KEPT_FIELDS = ("tweetId", "fullText", "expandedUrl")
#: A kept field longer than this drops its entry; a post tops out near 25 000 characters.
MAX_FIELD_CHARS = 65_536
#: No element is decoded past this many characters of its JSON text (rev-116). A real
#: entry is well under it even with every character of its text escaped.
MAX_ELEMENT_CHARS = 1_048_576
_FIRST_SLICE_CHARS = 4096
#: Kept fields whose non-string value marks the entry malformed (``expandedUrl`` is optional).
_REQUIRED_STRINGS = frozenset({"tweetId", "fullText"})

_DIR_FLAGS = os.O_RDONLY | os.O_DIRECTORY
_DATA_FLAGS = _DIR_FLAGS | os.O_NOFOLLOW
_FILE_FLAGS = os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_NOCTTY
_CHUNK = 1024 * 1024
_PREFIX = re.compile(r"\A﻿?[ \t\r\n]*window\.YTD\.([A-Za-z_]+)\.part([0-9]{1,4})[ \t]*=[ \t\r\n]*")
_WHITESPACE = re.compile(r"[ \t\r\n]*")

_NOT_A_FOLDER = "the archive path is not a folder; pass the archive's top folder (it holds data/)"
_ROOT_UNOPENED = "the archive folder could not be opened"
_NO_DATA = "the archive has no data/ folder; pass its top folder"
_DATA_NOT_PLAIN = "data/ is not a plain folder (it is a link or a file)"
_DATA_UNOPENED = "data/ could not be opened"
_NO_LIKES = "the archive has no likes file (data/like.js)"
_READ_CAP = "the archive files together are more than the read cap ({cap} bytes)"
_ENTRY_CAP = "the archive has more entries than the cap ({cap})"
_CONTAINER_CAP = "the file has too many JSON objects and arrays (cap {cap})"
_BAD_PREFIX = "the prefix is not window.YTD.{collection}.part{part} ="
_NOT_ONE_ARRAY = "the payload is not one JSON array"
_NOT_JSON = "an entry is not valid JSON"
_TOO_LONG = "an entry is not valid JSON, or is longer than {cap} characters"
_UNKNOWN_SHAPE = "an entry has an unknown shape"
_TRAILING = "something follows the array"


class SocialArchiveError(BrainError):
    """The archive is refused, or could not be read; the message names no content."""


@dataclass(frozen=True)
class ArchiveLimits:
    """The caps one read of an archive runs under (tests pass small ones)."""

    max_file_bytes: int = MAX_FILE_BYTES
    max_read_bytes: int = MAX_READ_BYTES
    max_parts: int = MAX_PARTS
    max_entries: int = MAX_ENTRIES
    max_containers: int = MAX_CONTAINERS


@dataclass(frozen=True)
class ArchiveEntries:
    """The kept fields of every entry, per file kind, in file order."""

    likes: tuple[dict[str, str], ...]
    bookmarks: tuple[dict[str, str], ...]
    bookmarks_present: bool


#: ``os.read``'s shape; a test injects a reader that returns more than ``fstat`` said.
ReadFn = Callable[[int, int], bytes]


@dataclass
class _Budget:
    limits: ArchiveLimits
    read_bytes: int = 0
    entries: int = 0

    def charge_bytes(self, size: int) -> None:
        self.read_bytes += size
        if self.read_bytes > self.limits.max_read_bytes:
            _refuse(_READ_CAP.format(cap=self.limits.max_read_bytes))

    @property
    def entries_left(self) -> int:
        return self.limits.max_entries - self.entries


def _refuse(message: str) -> NoReturn:
    raise SocialArchiveError(message)


_DEFAULT_LIMITS = ArchiveLimits()


def read_archive(
    root: Path, *, limits: ArchiveLimits = _DEFAULT_LIMITS, read: ReadFn = os.read
) -> ArchiveEntries:
    """Every like and bookmark entry under ``root/data`` (module docstring)."""
    root_fd = _open_root(root)
    try:
        data_fd = _open_data(root_fd)
        try:
            budget = _Budget(limits)
            likes = _read_collection(data_fd, "like", budget, read)
            bookmarks = _read_collection(data_fd, "bookmark", budget, read)
        finally:
            os.close(data_fd)
    finally:
        os.close(root_fd)
    if likes is None:
        _refuse(_NO_LIKES)
    return ArchiveEntries(
        likes=tuple(likes),
        bookmarks=tuple(bookmarks or ()),
        bookmarks_present=bookmarks is not None,
    )


def _open_root(root: Path) -> int:
    try:
        return os.open(root, _DIR_FLAGS)
    except (FileNotFoundError, NotADirectoryError):
        _refuse(_NOT_A_FOLDER)
    except OSError:
        _refuse(_ROOT_UNOPENED)


def _open_data(root_fd: int) -> int:
    try:
        return os.open("data", _DATA_FLAGS, dir_fd=root_fd)
    except FileNotFoundError:
        _refuse(_NO_DATA)
    except OSError as exc:
        if exc.errno in (errno.ELOOP, errno.ENOTDIR):
            _refuse(_DATA_NOT_PLAIN)
        _refuse(_DATA_UNOPENED)


def _file_name(collection: str, part: int) -> str:
    return f"{collection}.js" if part == 0 else f"{collection}-part{part}.js"


def _read_collection(
    data_fd: int, collection: str, budget: _Budget, read: ReadFn
) -> list[dict[str, str]] | None:
    """The entries of every part of ``collection``; ``None`` when part 0 is absent."""
    entries: list[dict[str, str]] = []
    for part in range(budget.limits.max_parts + 1):
        name = _file_name(collection, part)
        text = _read_file(data_fd, name, budget, read)
        if text is None:
            return entries if part else None
        if part == budget.limits.max_parts:
            _refuse(f"the {collection} files have more than {part} parts")
        try:
            for entry in parse_payload(
                text,
                collection=collection,
                part=part,
                max_entries=budget.entries_left,
                max_containers=budget.limits.max_containers,
            ):
                entries.append(entry)
                budget.entries += 1
        except SocialArchiveError as exc:
            _refuse(f"data/{name} is refused: {exc}")
    raise AssertionError("unreachable: the loop returns or refuses")  # pragma: no cover


def _read_file(data_fd: int, name: str, budget: _Budget, read: ReadFn) -> str | None:
    """``data/<name>`` decoded, ``None`` if absent; refused unless one plain, bounded file."""
    label = f"data/{name}"
    try:
        fd = os.open(name, _FILE_FLAGS, dir_fd=data_fd)
    except FileNotFoundError:
        return None
    except OSError as exc:
        if exc.errno == errno.ELOOP:
            _refuse(f"{label} is not a plain file (a link, a folder or a device)")
        _refuse(f"{label} could not be read")
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode):
            _refuse(f"{label} is not a plain file (a link, a folder or a device)")
        if info.st_nlink != 1:
            _refuse(f"{label} has more than one link")
        cap = budget.limits.max_file_bytes
        if info.st_size > cap:
            _refuse(f"{label} is larger than {cap} bytes")
        budget.charge_bytes(info.st_size)
        raw = _read_bounded(fd, info.st_size, read, label)
    finally:
        os.close(fd)
    try:
        return raw.decode("utf-8")
    except UnicodeDecodeError:
        _refuse(f"{label} is not UTF-8")


def _read_bounded(fd: int, size: int, read: ReadFn, label: str) -> bytes:
    """At most ``size + 1`` bytes; more than ``size`` (what ``fstat`` said) is refused."""
    chunks: list[bytes] = []
    total = 0
    while True:
        try:
            chunk = read(fd, min(_CHUNK, size + 1 - total))
        except OSError:
            _refuse(f"{label} could not be read")
        if not chunk:
            return b"".join(chunks)
        total += len(chunk)
        if total > size:
            _refuse(f"{label} grew while it was read")
        chunks.append(chunk)


def _reject_constant(name: str) -> NoReturn:
    raise ValueError(f"{name} is not JSON")


_DECODER = json.JSONDecoder(parse_constant=_reject_constant)


def parse_payload(
    text: str,
    *,
    collection: str,
    part: int,
    max_entries: int = MAX_ENTRIES,
    max_containers: int = MAX_CONTAINERS,
) -> Iterator[dict[str, str]]:
    """The kept fields of each ``{"<collection>": {…}}`` element (module docstring)."""
    if text.count("{") + text.count("[") > max_containers:
        _refuse(_CONTAINER_CAP.format(cap=max_containers))
    match = _PREFIX.match(text)
    if match is None or match.group(1) != collection or int(match.group(2)) != part:
        _refuse(_BAD_PREFIX.format(collection=collection, part=part))
    index = match.end()
    if not text.startswith("[", index):
        _refuse(_NOT_ONE_ARRAY)
    index = _skip_whitespace(text, index + 1)
    count = 0
    if text.startswith("]", index):
        index += 1
    else:
        while True:
            element, index = _decode(text, index)
            count += 1
            if count > max_entries:
                _refuse(_ENTRY_CAP.format(cap=max_entries))
            yield _unwrap(element, collection)
            index = _skip_whitespace(text, index)
            if text.startswith(",", index):
                index = _skip_whitespace(text, index + 1)
                continue
            if text.startswith("]", index):
                index += 1
                break
            _refuse(_NOT_ONE_ARRAY)
    if text[index:].strip(" \t\r\n") not in ("", ";"):
        _refuse(_TRAILING)


def _skip_whitespace(text: str, index: int) -> int:
    match = _WHITESPACE.match(text, index)
    return match.end() if match is not None else index  # pragma: no branch - always matches


def _decode(text: str, index: int) -> tuple[Any, int]:
    """The element at ``index`` and the index after it, never decoding past the cap.

    Each attempt decodes a slice, so a truncated element fails instead of reading on;
    the slice doubles until the element fits or the cap is reached. Only an object is
    accepted later (``_unwrap``), and no object is complete before its last ``}``.
    """
    size = _FIRST_SLICE_CHARS
    while True:
        window = text[index:index + size]
        try:
            element, end = _DECODER.raw_decode(window)
        except RecursionError:
            _refuse(_NOT_JSON)
        except ValueError:
            if size >= MAX_ELEMENT_CHARS or index + size >= len(text):
                _refuse(_TOO_LONG.format(cap=MAX_ELEMENT_CHARS))
            size = min(size * 2, MAX_ELEMENT_CHARS)
            continue
        return element, index + end


def _unwrap(element: Any, collection: str) -> dict[str, str]:
    """The kept string fields of one entry; everything else is dropped here (sec-116 F2)."""
    if not isinstance(element, dict) or list(element) != [collection]:
        _refuse(_UNKNOWN_SHAPE)
    inner = element[collection]
    if not isinstance(inner, dict):
        _refuse(_UNKNOWN_SHAPE)
    kept: dict[str, str] = {}
    for key in KEPT_FIELDS:
        value = inner.get(key)
        if value is None:
            continue
        if not isinstance(value, str):
            if key in _REQUIRED_STRINGS:
                return {}
            continue
        if len(value) > MAX_FIELD_CHARS:
            return {}
        kept[key] = value
    return kept
