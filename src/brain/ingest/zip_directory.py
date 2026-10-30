"""Find and bound a zip's central directory before ``zipfile`` parses it (RM-35, sec-104 L3).

``zipfile.ZipFile`` reads the whole central directory into memory and builds a
``ZipInfo`` per entry when it opens an archive, trusting the end record's
declared size and ignoring its declared count. An untrusted archive therefore
has to be bounded first, and by the same directory ``zipfile`` will read:

- :func:`locate` picks the end record exactly as ``zipfile._EndRecData`` and
  ``_EndRecData64`` do (copied, not called: they are private). A ZIP64 locator
  is followed whenever its signature precedes the end record, and its record is
  read at the locator's offset first, then just before the locator (prepended
  data). With no locator, the plain record's values stand, even when they are
  ``0xFFFF`` or ``0xFFFFFFFF`` (a plain archive with exactly 65,535 entries).
  The directory ends where that record starts, as in ``zipfile``, so a
  declared size larger than what precedes it (it would start before the file)
  is refused.
- :func:`count_entries` walks the directory from its start for its declared
  size, one 46-byte header at a time (constant memory), requiring each
  header's signature and skipping its name, extra field and comment. It stops
  at ``limit + 1`` headers, so the declared count is never trusted.

Errors carry a reason (:data:`CORRUPT` or :data:`TOO_MANY`), never the
archive's bytes; the caller maps them to its own fixed sentences.
"""
from __future__ import annotations

import struct
from dataclasses import dataclass
from typing import BinaryIO

from ..errors import BrainError

CORRUPT = "corrupt"
TOO_MANY = "too_many"

_EOCD = struct.Struct("<4s4H2LH")
_EOCD_SIG = b"PK\x05\x06"
_LOCATOR = struct.Struct("<4sLQL")
_LOCATOR_SIG = b"PK\x06\x07"
_ZIP64 = struct.Struct("<4sQ2H2L4Q")
_ZIP64_SIG = b"PK\x06\x06"
_HEADER = struct.Struct("<4s4B4HL2L5H2L")
_HEADER_SIG = b"PK\x01\x02"
_NAME_LEN, _EXTRA_LEN, _COMMENT_LEN = 12, 13, 14
_MAX_COMMENT = 1 << 16


class ZipDirectoryError(BrainError):
    """The directory cannot be bounded; ``reason`` is :data:`CORRUPT` or :data:`TOO_MANY`."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


@dataclass(frozen=True)
class Directory:
    """Where ``zipfile`` will read the central directory, and what the end record claims."""

    start: int
    size: int
    declared_entries: int


@dataclass(frozen=True)
class _EndRecord:
    entries: int
    size: int
    offset: int
    location: int


def _end_record(fh: BinaryIO, filesize: int) -> _EndRecord:
    """``zipfile._EndRecData`` without the ZIP64 step: the exact tail, else the last match."""
    if filesize >= _EOCD.size:
        fh.seek(filesize - _EOCD.size)
        data = fh.read(_EOCD.size)
        if data[:4] == _EOCD_SIG and data[-2:] == b"\0\0":
            return _record(data, filesize - _EOCD.size)
    start_at = max(filesize - _MAX_COMMENT - _EOCD.size, 0)
    fh.seek(start_at)
    data = fh.read()
    start = data.rfind(_EOCD_SIG)
    if start < 0 or len(data) - start < _EOCD.size:
        raise ZipDirectoryError(CORRUPT)
    return _record(data[start : start + _EOCD.size], start_at + start)


def _record(data: bytes, location: int) -> _EndRecord:
    fields = _EOCD.unpack(data)
    return _EndRecord(fields[4], fields[5], fields[6], location)


def _zip64(fh: BinaryIO, record: _EndRecord) -> _EndRecord:
    """``zipfile._EndRecData64``: follow a locator whenever one precedes the end record."""
    offset = record.location - _LOCATOR.size
    if offset < 0:
        return record
    fh.seek(offset)
    sig, disk, reloff, disks = _LOCATOR.unpack(fh.read(_LOCATOR.size))
    if sig != _LOCATOR_SIG:
        return record
    offset -= _ZIP64.size
    if disk != 0 or disks > 1 or reloff > offset:
        raise ZipDirectoryError(CORRUPT)
    fh.seek(reloff)
    extra = offset - reloff
    data = fh.read(_ZIP64.size)
    if not data.startswith(_ZIP64_SIG) and reloff != offset:
        fh.seek(offset)  # prepended data: the record sits just before the locator
        extra = 0
        data = fh.read(_ZIP64.size)
    if not data.startswith(_ZIP64_SIG):
        raise ZipDirectoryError(CORRUPT)
    _sig, size, *_versions_disks, _here, entries, dir_size, dir_offset = _ZIP64.unpack(data)
    if dir_offset + dir_size != reloff or size + 12 != _ZIP64.size + extra:
        raise ZipDirectoryError(CORRUPT)
    return _EndRecord(entries, dir_size, dir_offset, offset - extra)


def locate(fh: BinaryIO) -> Directory:
    """The central directory ``zipfile`` will read from ``fh``; :data:`CORRUPT` if none."""
    filesize = fh.seek(0, 2)
    record = _zip64(fh, _end_record(fh, filesize))
    # zipfile's ``offset + concat``: the directory ends where the end record (or
    # the ZIP64 record) starts, so a declared size larger than what precedes it
    # is the only way it can leave the file. The declared offset is not used.
    start = record.location - record.size
    if start < 0:
        raise ZipDirectoryError(CORRUPT)
    return Directory(start, record.size, record.entries)


def count_entries(fh: BinaryIO, directory: Directory, limit: int) -> int:
    """Count the headers in ``directory``; :data:`TOO_MANY` at ``limit + 1``."""
    fh.seek(directory.start)
    walked = count = 0
    while walked < directory.size:
        header = fh.read(_HEADER.size)
        if len(header) != _HEADER.size or header[:4] != _HEADER_SIG:
            raise ZipDirectoryError(CORRUPT)
        count += 1
        if count > limit:
            raise ZipDirectoryError(TOO_MANY)
        fields = _HEADER.unpack(header)
        skip = fields[_NAME_LEN] + fields[_EXTRA_LEN] + fields[_COMMENT_LEN]
        fh.seek(skip, 1)
        walked += _HEADER.size + skip
    return count
