"""Read a Notion export, a folder or a ``.zip``, as untrusted input (RM-35, ruling R5).

The export is someone else's file tree, so this module lists and reads it
without ever trusting a name, a link or a size:

- **Nothing is extracted to disk.** A zip member is decompressed in memory, one
  at a time, and only ``.md`` and ``.csv`` members are ever decompressed, so an
  attachment cannot be a bomb.
- **Bounded.** At most :data:`MAX_EXPORT_FILES` entries, at most
  :data:`MAX_FILE_BYTES` per page or table (a larger one is skipped and
  counted), and at most :data:`MAX_READ_BYTES` read in one run. The declared
  sizes are summed before anything is read, and a read never returns more than
  a member's declared size: ``zipfile`` stops there. A header that understates
  a member is refused as corrupt when the CRC of the shortened content does not
  match, which is the case for any real content.
- **No traversal, no links.** A zip is refused whole when a member name is
  absolute or drive-absolute, or has an empty, ``.`` or ``..`` component
  (``/`` or ``\\`` separated); when a member is a symbolic link, encrypted, or shares its
  name with another; or when ``zipfile`` reports corruption. A folder is walked
  without following links: symlinked files and folders, anything that is not a
  regular file, files with more than one hard link, and entries that vanish
  while being listed are skipped and counted. Each listed file's
  ``(st_dev, st_ino)`` is recorded. A read re-checks that the path still
  resolves inside the root, opens it with ``O_NOFOLLOW | O_NONBLOCK`` (a FIFO
  swapped in cannot block the run), and requires the open file to be a regular
  file with the recorded identity, so a file, a parent folder or a hard link
  swapped in between listing and reading is refused rather than read.
- **The central directory is sized before it is parsed.** ``zipfile`` parses
  the whole central directory when it opens an archive, so the end record (and
  its ZIP64 form) is read from the file's tail first, and an archive declaring
  more than :data:`MAX_EXPORT_FILES` entries, or a central directory larger
  than that many entries can honestly need, is refused before ``zipfile``
  sees it.

Every :class:`NotionExportError` message is a fixed sentence: no member name,
path or ``zipfile`` text is echoed, since all of them come from the export.
"""
from __future__ import annotations

import lzma
import os
import re
import stat
import struct
import zipfile
import zlib
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Protocol

from ..errors import BrainError
from .notion_export import PAGE_SUFFIX, TABLE_SUFFIX, ExportFile

MAX_EXPORT_FILES = 100_000
MAX_FILE_BYTES = 8 * 1024 * 1024
MAX_READ_BYTES = 512 * 1024 * 1024

#: Only these are ever read (or, in a zip, decompressed).
READ_SUFFIXES = frozenset({PAGE_SUFFIX, TABLE_SUFFIX})

SKIP_LINK = "link"
SKIP_TOO_LARGE = "too_large"
SKIP_UNREADABLE = "unreadable"

_ZIP_SUFFIX = ".zip"
_SYMLINK_MODE = stat.S_IFLNK
_ENCRYPTED_FLAG = 0x1
_DRIVE_RE = re.compile(r"^[A-Za-z]:[/\\]")
_OPEN_FLAGS = os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK

#: The end-of-central-directory record, the ZIP64 locator and the ZIP64 record.
_EOCD = struct.Struct("<4s4H2LH")
_EOCD_SIG = b"PK\x05\x06"
_ZIP64_LOCATOR = struct.Struct("<4sLQL")
_ZIP64_LOCATOR_SIG = b"PK\x06\x07"
_ZIP64_EOCD = struct.Struct("<4sQ2H2L4Q")
_ZIP64_EOCD_SIG = b"PK\x06\x06"
_MAX_ZIP_COMMENT = 0xFFFF
#: A central-directory entry is 46 fixed bytes plus its name, extra field and
#: comment; about 1 KiB of those per entry is more than an honest export needs.
_CD_BYTES_PER_ENTRY = 46 + 1024

_NOT_AN_EXPORT = "the export path is neither a folder nor a .zip archive"
_TOO_MANY = "the export holds more files than the cap; export a smaller part"
_TOO_BIG = "the export's pages and tables pass the read cap; export a smaller part"
_UNSAFE_NAME = "the archive has a member whose path is absolute or leaves the archive"
_LINK_MEMBER = "the archive has a symbolic-link member"
_DUPLICATE_MEMBER = "the archive has two members with the same name"
_ENCRYPTED = "the archive has an encrypted member"
_CORRUPT = "the archive is not a readable zip, or a member is corrupt"
_ESCAPED = "an export file now resolves outside the export folder"
_UNREADABLE = "an export file could not be read as the file that was listed (it was replaced)"
_GREW = "an export file grew past the size cap after it was listed"


class NotionExportError(BrainError):
    """The export is refused, or could not be read; the message names no file."""


@dataclass(frozen=True)
class ExportLimits:
    """The caps one read of an export runs under (tests pass small ones)."""

    max_files: int = MAX_EXPORT_FILES
    max_file_bytes: int = MAX_FILE_BYTES
    max_read_bytes: int = MAX_READ_BYTES


class ExportReader(Protocol):
    """An opened export: its files, what the listing skipped, and a reader."""

    files: tuple[ExportFile, ...]
    skipped: dict[str, int]

    def read_text(self, file: ExportFile) -> str:
        """``file`` decoded as UTF-8, a leading BOM dropped (invalid bytes replaced)."""


def is_readable(file: ExportFile) -> bool:
    """Whether ``file`` is a page or a table, the only kinds ever read."""
    return file.path.suffix.lower() in READ_SUFFIXES


class _Budget:
    """The listing caps and the running count of bytes read."""

    def __init__(self, limits: ExportLimits) -> None:
        self.limits = limits
        self.read = 0
        self.skipped: dict[str, int] = {}

    def skip(self, reason: str) -> None:
        self.skipped[reason] = self.skipped.get(reason, 0) + 1

    def admit(self, files: list[ExportFile]) -> tuple[ExportFile, ...]:
        """Drop oversized pages and tables (counted); refuse an export over the read cap."""
        kept: list[ExportFile] = []
        for file in files:
            if is_readable(file) and file.size > self.limits.max_file_bytes:
                self.skip(SKIP_TOO_LARGE)
            else:
                kept.append(file)
        if sum(f.size for f in kept if is_readable(f)) > self.limits.max_read_bytes:
            raise NotionExportError(_TOO_BIG)
        return tuple(kept)

    def charge(self, data: bytes) -> str:
        if len(data) > self.limits.max_file_bytes:
            raise NotionExportError(_GREW)
        self.read += len(data)
        if self.read > self.limits.max_read_bytes:
            raise NotionExportError(_TOO_BIG)
        # ``utf-8-sig``: a byte-order mark would otherwise lead the title line.
        return data.decode("utf-8-sig", errors="replace")


def _require_readable(file: ExportFile) -> None:
    if not is_readable(file):
        raise ValueError("only pages and tables are read")


class FolderExport:
    """An export unpacked into a folder, walked without following links."""

    def __init__(self, root: Path, limits: ExportLimits) -> None:
        self._root = root.resolve()
        self._budget = _Budget(limits)
        self._identity: dict[PurePosixPath, tuple[int, int]] = {}
        listed: list[ExportFile] = []
        for dirpath, dirnames, filenames in os.walk(self._root, followlinks=False):
            here = Path(dirpath)
            for name in dirnames:
                if (here / name).is_symlink():  # followlinks=False: walk never enters it
                    self._budget.skip(SKIP_LINK)
            for name in filenames:
                try:
                    info = os.lstat(here / name)
                except OSError:  # vanished since the walk listed it; its path is not echoed
                    self._budget.skip(SKIP_UNREADABLE)
                    continue
                if not stat.S_ISREG(info.st_mode) or info.st_nlink > 1:
                    self._budget.skip(SKIP_LINK)  # a hard link may be a file outside the root
                    continue
                rel = PurePosixPath((here / name).relative_to(self._root).as_posix())
                self._identity[rel] = (info.st_dev, info.st_ino)
                listed.append(ExportFile(rel, info.st_size))
                if len(listed) > limits.max_files:
                    raise NotionExportError(_TOO_MANY)
        self.files = self._budget.admit(listed)
        self.skipped = self._budget.skipped

    def read_text(self, file: ExportFile) -> str:
        _require_readable(file)
        full = self._root.joinpath(*file.path.parts)
        if not full.resolve().is_relative_to(self._root):
            raise NotionExportError(_ESCAPED)
        try:
            fd = os.open(full, _OPEN_FLAGS)
        except OSError as exc:
            raise NotionExportError(_UNREADABLE) from exc
        try:
            info = os.fstat(fd)
            # The identity alone refuses every swap (a link, FIFO or folder is another
            # inode); ``O_NOFOLLOW`` and ``S_ISREG`` are kept as defence in depth.
            if (
                not stat.S_ISREG(info.st_mode)
                or (info.st_dev, info.st_ino) != self._identity[file.path]
            ):
                raise NotionExportError(_UNREADABLE)
            with os.fdopen(fd, "rb", closefd=False) as fh:
                data = fh.read(self._budget.limits.max_file_bytes + 1)
        except OSError as exc:
            raise NotionExportError(_UNREADABLE) from exc
        finally:
            os.close(fd)
        return self._budget.charge(data)


def _member_path(name: str) -> PurePosixPath:
    """The member's path inside the archive; refuses anything that could leave it.

    Names are only ever used as logical paths (nothing is extracted), but a name
    that would leave the root on any platform marks a crafted archive: an
    absolute or drive-absolute name, or a ``.``/``..``/empty component, with
    ``\\`` read as a separator too (a Windows-made traversal).
    """
    trimmed = name[:-1] if name.endswith("/") else name
    parts = trimmed.split("/")
    segments = [seg for part in parts for seg in part.split("\\")]
    if _DRIVE_RE.match(trimmed) or any(seg in ("", ".", "..") for seg in segments):
        raise NotionExportError(_UNSAFE_NAME)
    return PurePosixPath(*parts)


class ZipExport:
    """A zipped export, read member by member in memory."""

    def __init__(self, archive: zipfile.ZipFile, limits: ExportLimits) -> None:
        self._zip = archive
        self._budget = _Budget(limits)
        infos = archive.infolist()
        if len(infos) > limits.max_files:
            raise NotionExportError(_TOO_MANY)
        self._members: dict[PurePosixPath, zipfile.ZipInfo] = {}
        listed: list[ExportFile] = []
        for info in infos:
            path = _member_path(info.filename)
            if stat.S_IFMT(info.external_attr >> 16) == _SYMLINK_MODE:
                raise NotionExportError(_LINK_MEMBER)
            if info.flag_bits & _ENCRYPTED_FLAG:
                raise NotionExportError(_ENCRYPTED)
            if path in self._members:
                raise NotionExportError(_DUPLICATE_MEMBER)
            self._members[path] = info
            if not info.is_dir():
                listed.append(ExportFile(path, info.file_size))
        self.files = self._budget.admit(listed)
        self.skipped = self._budget.skipped

    def read_text(self, file: ExportFile) -> str:
        _require_readable(file)
        try:
            with self._zip.open(self._members[file.path]) as fh:
                # ``zipfile`` never returns more than the declared size; a header that
                # understates it fails the CRC check on the shortened content.
                data = fh.read(self._budget.limits.max_file_bytes + 1)
        except (
            zipfile.BadZipFile, zlib.error, lzma.LZMAError, EOFError, OSError, NotImplementedError
        ) as exc:
            raise NotionExportError(_CORRUPT) from exc
        return self._budget.charge(data)


def _central_directory(path: Path) -> tuple[int, int]:
    """``(entries, central-directory bytes)`` from the end records in the file's tail.

    Read the way ``zipfile`` reads them: the last end-of-central-directory
    signature in the final 64 KiB, and for ZIP64 the record just before its
    locator. A missing or truncated record, or a locator whose record is not
    one, is :data:`_CORRUPT`; maximal values with no locator are taken as they
    are.
    """
    with path.open("rb") as fh:
        size = fh.seek(0, os.SEEK_END)
        tail_start = max(0, size - _EOCD.size - _MAX_ZIP_COMMENT)
        fh.seek(tail_start)
        tail = fh.read()
        at = tail.rfind(_EOCD_SIG)
        if at < 0 or len(tail) - at < _EOCD.size:
            raise NotionExportError(_CORRUPT)
        _sig, _disk, _cd_disk, _here, entries, cd_size, cd_offset, _note = _EOCD.unpack_from(
            tail, at
        )
        plain = (entries, cd_size)
        if entries != 0xFFFF and 0xFFFFFFFF not in (cd_size, cd_offset):
            return plain
        # A field at its maximum is a ZIP64 marker only when a locator precedes the
        # record; without one, a plain archive simply has 65,535 entries (or a
        # 4 GiB directory), and its own values stand, as in ``zipfile``.
        locator_at = tail_start + at - _ZIP64_LOCATOR.size
        if locator_at < 0:
            return plain
        fh.seek(locator_at)
        if fh.read(len(_ZIP64_LOCATOR_SIG)) != _ZIP64_LOCATOR_SIG:
            return plain
        record_at = locator_at - _ZIP64_EOCD.size
        if record_at < 0:
            raise NotionExportError(_CORRUPT)
        fh.seek(record_at)
        record = fh.read(_ZIP64_EOCD.size)
    if record[: len(_ZIP64_EOCD_SIG)] != _ZIP64_EOCD_SIG:
        raise NotionExportError(_CORRUPT)  # a locator that points at garbage
    fields = _ZIP64_EOCD.unpack_from(record)
    return fields[7], fields[8]  # total entries, central-directory size


def _check_central_directory(path: Path, limits: ExportLimits) -> None:
    entries, cd_size = _central_directory(path)
    if entries > limits.max_files or cd_size > limits.max_files * _CD_BYTES_PER_ENTRY:
        raise NotionExportError(_TOO_MANY)


@contextmanager
def open_export(path: Path, *, limits: ExportLimits | None = None) -> Iterator[ExportReader]:
    """Open ``path`` (a folder or a ``.zip``) for listing and reading; refuse anything else."""
    caps = limits or ExportLimits()
    if path.is_dir():
        yield FolderExport(path, caps)
        return
    if path.suffix.lower() != _ZIP_SUFFIX or not path.is_file():
        raise NotionExportError(_NOT_AN_EXPORT)
    try:
        _check_central_directory(path, caps)
        archive = zipfile.ZipFile(path)
    except (zipfile.BadZipFile, OSError, EOFError, ValueError) as exc:
        raise NotionExportError(_CORRUPT) from exc
    with archive:
        yield ZipExport(archive, caps)
