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
  descriptors. No name from the archive is ever joined to a path.
- **No links.** ``data`` is opened ``O_DIRECTORY | O_NOFOLLOW`` under the
  root's descriptor; each file ``O_NOFOLLOW | O_NONBLOCK`` under ``data``'s.
  The open descriptor must be a regular file with one link (``fstat``), so a
  symlink, FIFO, device, folder or hard link is refused, with no window between
  the check and the read. The root folder itself is the owner's argument and
  may be a link.
- **Bounded.** :class:`ArchiveLimits`: a per-file size (checked on the open
  descriptor before reading, then enforced while reading, so a file that grows
  is refused), a run-wide read total, a part count per collection, and an entry
  count. JSON nesting deep enough to exhaust the parser's recursion, an
  over-long number and invalid UTF-8 refuse the file.
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

_DIR_FLAGS = os.O_RDONLY | os.O_DIRECTORY
_DATA_FLAGS = _DIR_FLAGS | os.O_NOFOLLOW
_FILE_FLAGS = os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK
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
_BAD_PREFIX = "the prefix is not window.YTD.{collection}.part{part} ="
_NOT_ONE_ARRAY = "the payload is not one JSON array"
_NOT_JSON = "an entry is not valid JSON"
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


@dataclass(frozen=True)
class ArchiveEntries:
    """The inner object of every entry, per file kind, in file order."""

    likes: tuple[dict[str, Any], ...]
    bookmarks: tuple[dict[str, Any], ...]
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
) -> list[dict[str, Any]] | None:
    """The entries of every part of ``collection``; ``None`` when part 0 is absent."""
    entries: list[dict[str, Any]] = []
    for part in range(budget.limits.max_parts + 1):
        name = _file_name(collection, part)
        text = _read_file(data_fd, name, budget, read)
        if text is None:
            return entries if part else None
        if part == budget.limits.max_parts:
            _refuse(f"the {collection} files have more than {part} parts")
        try:
            for entry in parse_payload(
                text, collection=collection, part=part, max_entries=budget.entries_left
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
        raw = _read_bounded(fd, cap, read, label)
    finally:
        os.close(fd)
    budget.charge_bytes(len(raw) - info.st_size)
    try:
        return raw.decode("utf-8")
    except UnicodeDecodeError:
        _refuse(f"{label} is not UTF-8")


def _read_bounded(fd: int, cap: int, read: ReadFn, label: str) -> bytes:
    chunks: list[bytes] = []
    total = 0
    while True:
        try:
            chunk = read(fd, min(_CHUNK, cap + 1 - total))
        except OSError:
            _refuse(f"{label} could not be read")
        if not chunk:
            return b"".join(chunks)
        total += len(chunk)
        if total > cap:
            _refuse(f"{label} grew while it was read")
        chunks.append(chunk)


def _reject_constant(name: str) -> NoReturn:
    raise ValueError(f"{name} is not JSON")


_DECODER = json.JSONDecoder(parse_constant=_reject_constant)


def parse_payload(
    text: str, *, collection: str, part: int, max_entries: int = MAX_ENTRIES
) -> Iterator[dict[str, Any]]:
    """The inner object of each ``{"<collection>": {…}}`` element (module docstring)."""
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
    try:
        return _DECODER.raw_decode(text, index)
    except (ValueError, RecursionError):
        _refuse(_NOT_JSON)


def _unwrap(element: Any, collection: str) -> dict[str, Any]:
    if not isinstance(element, dict) or list(element) != [collection]:
        _refuse(_UNKNOWN_SHAPE)
    inner = element[collection]
    if not isinstance(inner, dict):
        _refuse(_UNKNOWN_SHAPE)
    return inner
