"""The drop-folder runner: ingest the files saved into a configured folder (RM-33).

``brain ingest-folder`` (``cli_ingest_folder.py``) calls :func:`run` once per folder;
the opt-in ``com.brain.dropfolders`` LaunchAgent runs that command every 10 minutes.
RM-38, RM-39 and RM-40 reuse the runner. This module touches only the filesystem: the
ingest step is a caller-supplied :class:`IngestFn`, so it imports no DB or extractor.

**What is picked up** (:func:`scan`): regular files directly in the folder, by
``lstat`` (a symlink is never followed or returned), with one of the folder's
extensions, modified at or after ``since``. Skipped: sub-directories (``imported/``
among them), names starting with ``.`` (iCloud's ``.<name>.icloud`` placeholders on
macOS before 14, and macOS's in-progress ``.Screenshot …`` file), and files marked
*dataless* (``st_flags & SF_DATALESS``: since macOS 14 an iCloud-evicted file keeps
its name, and reading it blocks or fails). :func:`run` also leaves a file modified in
the last :data:`SETTLE_S` seconds for the next run, as it may still be being written.

**Move first, then ingest** (RM-33.D2). Each file is moved to
``<folder>/imported/<stem>-<sha8><ext>`` (``sha8``: the first 8 hex digits of its
SHA-256) and ingested from there, so the stored ``source_path`` is its permanent,
content-addressed home, and two different files that arrive with one name stay two
documents. The title is the original stem. Three rules keep the owner's files safe:

1. **A move never overwrites.** ``os.rename`` would replace an existing destination
   silently, so a move is a hard link (atomic; ``FileExistsError`` when the name is
   taken) and then an unlink. On a volume without hard links it is an ``O_EXCL`` copy
   instead. A move back uses the same primitive.
2. **A destination that already exists is the same file dropped twice** only when its
   full SHA-256 matches; then the drop copy is deleted and nothing is ingested (the
   earlier import already holds that ``source_path``). A different file at that name
   (``sha8`` is 32 bits), or anything but a regular file there, is a failure that
   touches neither.
3. **Only a move this run made is ever moved back.** When the ingest fails, the file
   returns to the folder, unless a new file took its name meanwhile; then it stays in
   ``imported/`` and the failure says so. ``imported/`` itself must be a real
   directory; a symlink there refuses the whole folder.

An ingest that reads no text (the ``"empty"`` outcome) leaves the file in
``imported/``: it was processed, and retrying it would read nothing again.

**Bounds.** At most ``max_files`` files and ``deadline_s`` seconds a run; files past
either are left exactly where they are, counted as ``deferred``. The ingest step gets
what is left of the deadline as its timeout.

**Known residual.** A crash between the move and the ingest leaves a file in
``imported/`` with no document, and a later identical drop is then a no-op;
``brain ingest <that path>`` recovers it.
"""
from __future__ import annotations

import contextlib
import errno
import hashlib
import os
import stat
import time
from collections import Counter
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal, Protocol

from .errors import BrainError, describe_failure

if TYPE_CHECKING:
    import psycopg

#: ``stat.SF_DATALESS`` exists only from Python 3.13.
SF_DATALESS = 0x40000000
IMPORTED_DIR = "imported"
#: A file modified this recently may still be being written; the next run takes it.
SETTLE_S = 5.0
#: Under the LaunchAgent's 600 s interval, so one run ends before the next is due.
RUN_DEADLINE_S = 480.0
MAX_FILES_PER_RUN = 200
#: The stem is cut to this many UTF-8 bytes, so ``-<sha8><ext>`` fits a 255-byte name.
MAX_STEM_BYTES = 200
#: ``os.link`` errors that mean "no hard links here", not "this file cannot move".
_NO_HARD_LINKS = frozenset(
    {errno.EPERM, errno.ENOTSUP, errno.EOPNOTSUPP, errno.EXDEV, errno.EMLINK, errno.ENOSYS}
)
_CHUNK = 1 << 20

Lstat = Callable[[Path], Any]
Link = Callable[[Path, Path], None]
Write = Callable[[int, bytes], int]
Clock = Callable[[], float]


class DropFolderError(BrainError):
    """The folder cannot be used: an unsafe ``imported/``, or a mode not built yet."""


@dataclass(frozen=True)
class DropFolder:
    """One drop folder, as :mod:`brain.settings.drop_folders` reads it from its TOML file."""

    path: Path  # resolved; must exist
    mode: Literal["ingest", "capture"]  # "capture" → capture-inbox notes (RM-39)
    kind: str | None  # a VALID_SOURCE_KINDS member when mode == "ingest"
    extensions: frozenset[str]
    sensitivity: str
    auto: bool = True  # False: only an explicit run uses it (RM-40)


class IngestFn(Protocol):
    """Ingests the moved file at ``path`` titled ``title``; returns its outcome.

    The outcome is ``"ingested"``, ``"updated"``, ``"unchanged"`` or ``"empty"``
    (no text: the file stays in ``imported/``). Raising means it failed, and the
    runner moves the file back.
    """

    def __call__(
        self, conn: psycopg.Connection[Any], path: Path, *, title: str, timeout: float
    ) -> str: ...


@dataclass(frozen=True)
class FileOutcome:
    """What happened to one dropped file. ``detail`` is class-only, never a message."""

    name: str
    status: str  # an IngestFn outcome, or "duplicate", "vanished", "failed"
    detail: str = ""


@dataclass(frozen=True)
class DropReport:
    """One folder's run: each file's outcome, and how many were left for the next run."""

    folder: Path
    outcomes: tuple[FileOutcome, ...]
    deferred: int

    def counts(self) -> dict[str, int]:
        return dict(Counter(o.status for o in self.outcomes))

    @property
    def failed(self) -> bool:
        return any(o.status == "failed" for o in self.outcomes)


def scan(
    folder: DropFolder,
    *,
    since: datetime,
    until: datetime | None = None,
    lstat: Lstat = os.lstat,
) -> list[Path]:
    """The files :func:`run` would take, oldest first (module docstring). ``lstat`` is a seam."""
    lower = since.timestamp()
    upper = until.timestamp() if until is not None else None
    found: list[tuple[float, str, Path]] = []
    with os.scandir(folder.path) as entries:
        for entry in entries:
            name = entry.name
            if name.startswith(".") or Path(name).suffix.lower() not in folder.extensions:
                continue
            path = folder.path / name
            try:
                st = lstat(path)
            except FileNotFoundError:
                continue
            if not stat.S_ISREG(st.st_mode) or getattr(st, "st_flags", 0) & SF_DATALESS:
                continue
            if st.st_mtime < lower or (upper is not None and st.st_mtime > upper):
                continue
            found.append((st.st_mtime, name, path))
    return [path for _, _, path in sorted(found)]


def run(
    conn: psycopg.Connection[Any],
    folder: DropFolder,
    *,
    since: datetime,
    ingest: IngestFn,
    now: float | None = None,
    clock: Clock = time.monotonic,
    deadline_s: float = RUN_DEADLINE_S,
    max_files: int = MAX_FILES_PER_RUN,
    lstat: Lstat = os.lstat,
    link: Link | None = None,
    write: Write = os.write,
) -> DropReport:
    """Move each new file into ``imported/`` and ingest it there (module docstring).

    Raises :class:`DropFolderError` before touching any file when the folder cannot be
    used. ``now`` (wall-clock seconds), ``clock``, ``lstat``, ``link`` and ``write`` are
    test seams.
    """
    if folder.mode != "ingest":
        raise DropFolderError(f"mode {folder.mode!r} is not available until RM-39")
    if folder.kind is None:
        raise DropFolderError("an ingest folder needs a source kind")
    imported = _imported_dir(folder.path)
    wall = now if now is not None else time.time()
    until = datetime.fromtimestamp(wall - SETTLE_S, UTC)
    files = scan(folder, since=since, until=until, lstat=lstat)
    mover = _Mover(link=link or _hard_link, write=write)
    start = clock()
    outcomes: list[FileOutcome] = []
    for index, src in enumerate(files):
        elapsed = clock() - start
        if index >= max_files or elapsed >= deadline_s:
            return DropReport(folder.path, tuple(outcomes), deferred=len(files) - index)
        outcomes.append(
            _one(conn, src, imported, ingest=ingest, timeout=deadline_s - elapsed, mover=mover)
        )
    return DropReport(folder.path, tuple(outcomes), deferred=0)


def _imported_dir(folder: Path) -> Path:
    """``<folder>/imported``, created if missing; refuses anything but a real directory."""
    imported = folder / IMPORTED_DIR
    try:
        st = os.lstat(imported)
    except FileNotFoundError:
        imported.mkdir()
        st = os.lstat(imported)
    if not stat.S_ISDIR(st.st_mode):
        raise DropFolderError(f"{IMPORTED_DIR}/ is not a real directory (a symlink or a file)")
    return imported


def _hard_link(src: Path, dst: Path) -> None:
    os.link(src, dst, follow_symlinks=False)


def _imported_name(src: Path, digest: str) -> str:
    stem = src.stem
    while len(stem.encode("utf-8", "surrogateescape")) > MAX_STEM_BYTES:
        stem = stem[:-1]
    return f"{stem}-{digest[:8]}{src.suffix.lower()}"


def _sha256(path: Path) -> str:
    """The file's SHA-256, opened without following a symlink (``ELOOP`` if it is one)."""
    digest = hashlib.sha256()
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    try:
        while chunk := os.read(fd, _CHUNK):
            digest.update(chunk)
    finally:
        os.close(fd)
    return digest.hexdigest()


@dataclass(frozen=True)
class _Mover:
    """The no-overwrite move: hard link then unlink, or an ``O_EXCL`` copy then unlink."""

    link: Link
    write: Write

    def move(self, src: Path, dst: Path) -> None:
        """Move ``src`` to ``dst``; ``FileExistsError`` when ``dst`` exists (nothing moved)."""
        try:
            self.link(src, dst)
        except OSError as exc:
            if exc.errno not in _NO_HARD_LINKS:
                raise
            self._copy_exclusive(src, dst)
        # A concurrent run that found ``dst`` taken may have deleted ``src`` as its
        # duplicate; the file is safe at ``dst`` either way, and this run ingests it.
        with contextlib.suppress(FileNotFoundError):
            os.unlink(src)

    def _copy_exclusive(self, src: Path, dst: Path) -> None:
        fd_in = os.open(src, os.O_RDONLY | os.O_NOFOLLOW)
        try:
            st = os.fstat(fd_in)
            flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW
            fd_out = os.open(dst, flags, stat.S_IMODE(st.st_mode))
            try:
                while chunk := os.read(fd_in, _CHUNK):
                    view = memoryview(chunk)
                    while view:
                        view = view[self.write(fd_out, bytes(view)) :]
            except BaseException:
                os.close(fd_out)
                os.unlink(dst)  # O_EXCL: this run created it
                raise
            os.close(fd_out)
        finally:
            os.close(fd_in)
        os.utime(dst, ns=(st.st_atime_ns, st.st_mtime_ns))


def _one(
    conn: psycopg.Connection[Any],
    src: Path,
    imported: Path,
    *,
    ingest: IngestFn,
    timeout: float,
    mover: _Mover,
) -> FileOutcome:
    """Move, ingest and (on failure) move back one file."""
    name = src.name
    try:
        digest = _sha256(src)
    except FileNotFoundError:
        return FileOutcome(name, "vanished")
    except OSError as exc:  # ELOOP: swapped for a symlink since the scan
        return FileOutcome(name, "failed", describe_failure(exc))
    dst = imported / _imported_name(src, digest)
    try:
        mover.move(src, dst)
    except FileExistsError:
        return _duplicate(src, dst, digest)
    except FileNotFoundError:
        return FileOutcome(name, "vanished")
    except OSError as exc:
        return FileOutcome(name, "failed", describe_failure(exc))
    try:
        status = ingest(conn, dst, title=src.stem, timeout=timeout)
    except Exception as exc:  # noqa: BLE001 - any failure moves the file back
        return FileOutcome(name, "failed", _move_back(dst, src, mover, describe_failure(exc)))
    return FileOutcome(name, status)


def _duplicate(src: Path, dst: Path, digest: str) -> FileOutcome:
    """``dst`` existed before this run: delete the drop copy only if the bytes match."""
    try:
        if not stat.S_ISREG(os.lstat(dst).st_mode) or _sha256(dst) != digest:
            return FileOutcome(src.name, "failed", f"a different file holds {dst.name}")
        os.unlink(src)
    except FileNotFoundError:
        return FileOutcome(src.name, "vanished")
    except OSError as exc:
        return FileOutcome(src.name, "failed", describe_failure(exc))
    return FileOutcome(src.name, "duplicate")


def _move_back(dst: Path, src: Path, mover: _Mover, detail: str) -> str:
    """Return the file this run moved; never over a file that took its name meanwhile."""
    try:
        mover.move(dst, src)
    except OSError as exc:
        return f"{detail}; left in {IMPORTED_DIR}/{dst.name} ({describe_failure(exc)})"
    return detail
