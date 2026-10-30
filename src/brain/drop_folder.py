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
   full SHA-256 matches; then it is ingested again (``unchanged`` when its document
   exists, reported ``duplicate``) and the drop copy is deleted. A different file at that name
   (``sha8`` is 32 bits), or anything but a regular file there, is a failure that
   touches neither.
3. **Only a move this run made is ever moved back**, and only after a *transient*
   failure (a database or embedder error): the file returns to the folder, unless a
   new file took its name meanwhile; then it stays in ``imported/`` and the failure
   says so. A failure that would repeat on every run (an unreadable or oversized
   image, an OCR timeout, the secret guard) is :class:`RejectedFileError`: the file stays
   in ``imported/``, reported ``rejected``, so it is not retried every 10 minutes.
   ``imported/`` itself must be a real directory; a symlink there refuses the folder.

**Files are pinned by identity, not by name** (the #105 security review). Both
directories are held open as ``O_DIRECTORY | O_NOFOLLOW`` fds and every step names files
relative to them, so swapping ``imported/`` for a symlink later moves nothing outside
it. A file is hashed from one fd opened ``O_NOFOLLOW | O_NONBLOCK`` and checked regular
with ``fstat`` (a symlink or a FIFO swapped in after the scan is refused, and a FIFO
never blocks). After the link or copy, the new name must be a regular file with that
fd's ``(st_dev, st_ino)`` (or the copy's own), else it is removed; and the old name is
unlinked only while it is still that file.

An ingest that reads no text (the ``"empty"`` outcome) leaves the file in
``imported/``: it was processed, and retrying it would read nothing again. A title is
the original stem on one line (``one_line``: no control or format characters),
capped at :data:`MAX_TITLE_CHARS`, else the ``imported/`` stem.

**Bounds.** At most ``max_files`` files and ``deadline_s`` seconds a run; files past
either are left exactly where they are, counted as ``deferred``. The ingest step gets
what is left of the deadline as its timeout.

**A crash between the move and the ingest** leaves a file in ``imported/`` with no
document. The same file dropped again ingests it (a duplicate is re-ingested, which is a
no-op when its document exists), and ``brain ingest <that path>`` recovers it too.
"""
from __future__ import annotations

import contextlib
import errno
import hashlib
import os
import stat
import time
from collections import Counter
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal, Protocol

from .errors import BrainError, describe_failure
from .ingest.text_safety import one_line

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
#: A title longer than this is cut (Notion's export ingest caps titles the same way).
MAX_TITLE_CHARS = 300
_DIR_FLAGS = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
#: ``os.link`` errors that mean "no hard links here", not "this file cannot move".
_NO_HARD_LINKS = frozenset(
    {errno.EPERM, errno.ENOTSUP, errno.EOPNOTSUPP, errno.EXDEV, errno.EMLINK, errno.ENOSYS}
)
_CHUNK = 1 << 20

Lstat = Callable[[Path], Any]
#: ``(from_name, to_name, from_dir_fd, to_dir_fd)``; ``FileExistsError`` when taken.
Link = Callable[[str, str, int, int], None]
Write = Callable[[int, bytes], int]
#: ``(name, dir_fd)``: ``os.unlink(name, dir_fd=dir_fd)``. A seam: a locked file
#: (``chflags uchg``) raises ``EPERM`` here, and Linux CI has no ``chflags``.
Unlink = Callable[[str, int], None]
Clock = Callable[[], float]


class DropFolderError(BrainError):
    """The folder cannot be used: an unsafe ``imported/``, or a mode not built yet."""


class MoveRaceError(BrainError):
    """A file changed identity or type between being hashed and being moved."""


class RejectedFileError(BrainError):
    """Raised by an :class:`IngestFn` for a failure that would repeat on every run.

    The cause (``raise RejectedFileError(...) from exc``) is what the report shows, by class.
    The file stays in ``imported/`` instead of being moved back and retried.
    """


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
    (no text: the file stays in ``imported/``). :class:`RejectedFileError` means it failed
    for good (the file stays in ``imported/``); any other exception means it failed for
    now, and the runner moves the file back.
    """

    def __call__(
        self, conn: psycopg.Connection[Any], path: Path, *, title: str, timeout: float
    ) -> str: ...


@dataclass(frozen=True)
class FileOutcome:
    """What happened to one dropped file. ``detail`` is class-only, never a message."""

    name: str
    status: str  # an IngestFn outcome, or "duplicate", "vanished", "rejected", "failed"
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
        return any(o.status in ("failed", "rejected") for o in self.outcomes)


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
    unlink: Unlink | None = None,
) -> DropReport:
    """Move each new file into ``imported/`` and ingest it there (module docstring).

    Raises :class:`DropFolderError` before touching any file when the folder cannot be
    used. ``now`` (wall-clock seconds), ``clock``, ``lstat``, ``link``, ``write`` and
    ``unlink`` are test seams. One file never aborts the folder: an unexpected
    ``OSError`` from a file is that file's ``failed`` outcome (rev-105 N1).
    """
    if folder.mode != "ingest":
        raise DropFolderError(f"mode {folder.mode!r} is not available until RM-39")
    if folder.kind is None:
        raise DropFolderError("an ingest folder needs a source kind")
    wall = now if now is not None else time.time()
    until = datetime.fromtimestamp(wall - SETTLE_S, UTC)
    with _open_dirs(folder.path) as dirs:
        files = scan(folder, since=since, until=until, lstat=lstat)
        mover = _Mover(dirs, link=link or _hard_link, write=write, unlink=unlink or _unlink)
        start = clock()
        outcomes: list[FileOutcome] = []
        for index, src in enumerate(files):
            elapsed = clock() - start
            if index >= max_files or elapsed >= deadline_s:
                return DropReport(folder.path, tuple(outcomes), deferred=len(files) - index)
            try:
                outcome = _one(
                    conn, src.name, ingest=ingest, timeout=deadline_s - elapsed, mover=mover
                )
            except OSError as exc:  # one file never aborts the folder (rev-105 N1)
                outcome = FileOutcome(src.name, "failed", describe_failure(exc))
            outcomes.append(outcome)
    return DropReport(folder.path, tuple(outcomes), deferred=0)


# --- One named file (RM-40) ---------------------------------------------------------


def run_one(
    conn: psycopg.Connection[Any],
    folder: DropFolder,
    name: str,
    *,
    ingest: IngestFn,
    timeout: float = RUN_DEADLINE_S,
    lstat: Lstat = os.lstat,
    link: Link | None = None,
    write: Write = os.write,
    unlink: Unlink | None = None,
) -> FileOutcome:
    """Move the one file ``name`` into ``imported/`` and ingest it there (RM-40's inbox).

    The same move-first step as :func:`run`, for a file the owner picked by name, so
    there is no ``since`` and no settle window. ``name`` must be a bare file name this
    folder takes (not empty, no leading ``.``, no ``/`` or NUL, one of its extensions),
    else ``ValueError`` before anything is opened. A dataless file is refused unread. The
    open is ``O_NOFOLLOW`` relative to the folder's fd and must be a regular file, so a
    name swapped for a symlink, a FIFO or a directory since the caller checked it moves
    nothing. Raises :class:`DropFolderError` as :func:`run` does; any other ``OSError``
    is the file's ``failed`` outcome, by class.
    """
    if folder.mode != "ingest":
        raise DropFolderError(f"mode {folder.mode!r} is not available until RM-39")
    if folder.kind is None:
        raise DropFolderError("an ingest folder needs a source kind")
    if not _is_bare_name(name) or Path(name).suffix.lower() not in folder.extensions:
        raise ValueError("not a file name this folder takes")
    with _open_dirs(folder.path) as dirs:
        mover = _Mover(dirs, link=link or _hard_link, write=write, unlink=unlink or _unlink)
        try:
            if getattr(lstat(folder.path / name), "st_flags", 0) & SF_DATALESS:
                return FileOutcome(name, "failed", "not on disk (dataless)")
            return _one(conn, name, ingest=ingest, timeout=timeout, mover=mover)
        except FileNotFoundError:
            return FileOutcome(name, "vanished")
        except OSError as exc:
            return FileOutcome(name, "failed", describe_failure(exc))


def _is_bare_name(name: str) -> bool:
    """A name directly in the folder: no path separator, no NUL, not hidden, ``.`` or ``..``."""
    return bool(name) and not name.startswith(".") and "/" not in name and "\0" not in name


@dataclass(frozen=True)
class _Dirs:
    """The folder and its ``imported/``, held open so a later swap of either path is moot."""

    folder: int
    imported: int
    imported_path: Path


@contextlib.contextmanager
def _open_dirs(folder: Path) -> Iterator[_Dirs]:
    """Open the folder and ``<folder>/imported`` (created if missing) as directory fds.

    ``O_NOFOLLOW | O_DIRECTORY``: a symlink or a file at ``imported`` refuses the folder.
    Every later step names files relative to these fds, so ``imported/`` swapped for a
    symlink after this point still receives nothing.
    """
    folder_fd = os.open(folder, _DIR_FLAGS)
    try:
        with contextlib.suppress(FileExistsError):
            os.mkdir(IMPORTED_DIR, dir_fd=folder_fd)
        try:
            imported_fd = os.open(IMPORTED_DIR, _DIR_FLAGS, dir_fd=folder_fd)
        except OSError as exc:
            if exc.errno in (errno.ELOOP, errno.ENOTDIR):
                raise DropFolderError(
                    f"{IMPORTED_DIR}/ is not a real directory (a symlink or a file)"
                ) from None
            raise
        try:
            yield _Dirs(folder_fd, imported_fd, folder / IMPORTED_DIR)
        finally:
            os.close(imported_fd)
    finally:
        os.close(folder_fd)


def _unlink(name: str, dir_fd: int) -> None:
    os.unlink(name, dir_fd=dir_fd)


def _hard_link(src: str, dst: str, src_dir: int, dst_dir: int) -> None:
    os.link(src, dst, src_dir_fd=src_dir, dst_dir_fd=dst_dir, follow_symlinks=False)


def _imported_name(name: str, digest: str) -> str:
    stem, suffix = Path(name).stem, Path(name).suffix.lower()
    while len(stem.encode("utf-8", "surrogateescape")) > MAX_STEM_BYTES:
        stem = stem[:-1]
    return f"{stem}-{digest[:8]}{suffix}"


def _title(name: str, dst_name: str) -> str:
    """The document's title: the original stem on one line, capped; else the imported stem."""
    title = one_line(Path(name).stem)[:MAX_TITLE_CHARS].strip()
    return title or Path(dst_name).stem


def _open_regular(name: str, dir_fd: int) -> tuple[int, os.stat_result]:
    """Open ``name`` without following a symlink or blocking on a FIFO; regular files only."""
    fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=dir_fd)
    st = os.fstat(fd)
    if not stat.S_ISREG(st.st_mode):
        os.close(fd)
        raise MoveRaceError("not a regular file")
    return fd, st


def _sha256_fd(fd: int) -> str:
    digest = hashlib.sha256()
    os.lseek(fd, 0, os.SEEK_SET)
    while chunk := os.read(fd, _CHUNK):
        digest.update(chunk)
    return digest.hexdigest()


def _identity(st: os.stat_result) -> tuple[int, int]:
    return st.st_dev, st.st_ino


def _path_identity(path: Path) -> tuple[int, int] | None:
    try:
        st = os.stat(path)
    except OSError:
        return None
    return _identity(st) if stat.S_ISREG(st.st_mode) else None


def _same_file(name: str, dir_fd: int, identity: tuple[int, int]) -> bool:
    """Whether ``name`` is still a regular file with ``identity`` (``lstat``, no following)."""
    try:
        st = os.stat(name, dir_fd=dir_fd, follow_symlinks=False)
    except FileNotFoundError:
        return False
    return stat.S_ISREG(st.st_mode) and _identity(st) == identity


@dataclass(frozen=True)
class _Mover:
    """The no-overwrite move between the two directory fds.

    A hard link (atomic; ``FileExistsError`` when the name is taken), or an ``O_EXCL``
    copy from the already-open source fd. Either way the new name must then be a regular
    file with the expected identity, or it is removed and :class:`MoveRaceError` raised;
    and the old name is unlinked only while it is still the file that was moved.
    """

    dirs: _Dirs
    link: Link
    write: Write
    unlink: Unlink = _unlink

    def move(
        self, name: str, to_name: str, *, fd: int, identity: tuple[int, int], digest: str,
        back: bool = False,
    ) -> tuple[int, int]:
        """Move ``name`` to ``to_name``; return the identity of the file now at ``to_name``."""
        dirs = self.dirs
        from_dir, to_dir = (dirs.imported, dirs.folder) if back else (dirs.folder, dirs.imported)
        try:
            self.link(name, to_name, from_dir, to_dir)
            expected = identity
        except OSError as exc:
            if exc.errno not in _NO_HARD_LINKS:
                raise
            expected = self._copy_exclusive(fd, to_name, to_dir, digest)
        if not _same_file(to_name, to_dir, expected):
            # This run created ``to_name``; what it names is not the file that was hashed
            # (a symlink or another file was swapped in), so it goes and nothing moves.
            with contextlib.suppress(FileNotFoundError):
                self.unlink(to_name, to_dir)
            raise MoveRaceError("the file changed during the move")
        try:
            _unlink_if_same(name, from_dir, identity, self.unlink)
        except OSError:
            # The old name cannot go (a locked file: EPERM). Remove the name this run
            # created, so a failed move leaves no stray import behind (rev-105 N1).
            with contextlib.suppress(OSError):
                _unlink_if_same(to_name, to_dir, expected, self.unlink)
            raise
        return expected

    def _copy_exclusive(self, fd: int, to_name: str, to_dir: int, digest: str) -> tuple[int, int]:
        """Copy the open ``fd`` to a new ``to_name``; the bytes must hash to ``digest``."""
        st = os.fstat(fd)
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW
        out = os.open(to_name, flags, stat.S_IMODE(st.st_mode), dir_fd=to_dir)
        try:
            copied = hashlib.sha256()
            os.lseek(fd, 0, os.SEEK_SET)
            while chunk := os.read(fd, _CHUNK):
                copied.update(chunk)
                view = memoryview(chunk)
                while view:
                    view = view[self.write(out, bytes(view)) :]
            if copied.hexdigest() != digest:
                raise MoveRaceError("the file changed during the copy")
            # Some volumes refuse to set times; the bytes are what count (rev-105 F4).
            with contextlib.suppress(OSError):
                os.utime(out, ns=(st.st_atime_ns, st.st_mtime_ns))
            identity = _identity(os.fstat(out))
        except BaseException:
            os.close(out)
            os.unlink(to_name, dir_fd=to_dir)  # O_EXCL: this run created it
            raise
        os.close(out)
        return identity


def _one(
    conn: psycopg.Connection[Any],
    name: str,
    *,
    ingest: IngestFn,
    timeout: float,
    mover: _Mover,
) -> FileOutcome:
    """Move, ingest and (on a transient failure) move back one file."""
    try:
        fd, st = _open_regular(name, mover.dirs.folder)
    except FileNotFoundError:
        return FileOutcome(name, "vanished")
    except (OSError, MoveRaceError) as exc:  # ELOOP / FIFO: swapped since the scan
        return FileOutcome(name, "failed", describe_failure(exc))
    try:
        digest = _sha256_fd(fd)
        identity = _identity(st)
        dst_name = _imported_name(name, digest)
        step = _Step(conn, ingest, timeout, name, dst_name, mover.dirs, mover.unlink)
        try:
            moved = mover.move(name, dst_name, fd=fd, identity=identity, digest=digest)
        except FileExistsError:
            return _duplicate(step, identity, digest)
        except FileNotFoundError:
            return FileOutcome(name, "vanished")
        except (OSError, MoveRaceError) as exc:
            return FileOutcome(name, "failed", describe_failure(exc))
        try:
            status = step.ingest_moved(moved)
        except RejectedFileError as exc:
            return FileOutcome(name, "rejected", _cause(exc))
        except Exception as exc:  # noqa: BLE001 - any other failure moves the file back
            detail = describe_failure(exc)
            return FileOutcome(name, "failed", _move_back(dst_name, name, mover, detail, digest))
        return FileOutcome(name, status)
    finally:
        os.close(fd)


def _cause(exc: RejectedFileError) -> str:
    return describe_failure(exc.__cause__) if exc.__cause__ is not None else describe_failure(exc)


@dataclass(frozen=True)
class _Step:
    """One file's ingest call: what :func:`_one` and :func:`_duplicate` share."""

    conn: psycopg.Connection[Any]
    ingest: IngestFn
    timeout: float
    name: str
    dst_name: str
    dirs: _Dirs
    unlink: Unlink = _unlink

    def ingest_moved(self, identity: tuple[int, int]) -> str:
        """Ingest ``imported/<dst_name>``, which must still be the file with ``identity``.

        The ingest reads by path. If ``imported/`` was swapped since it was opened, that
        path names some other file: :class:`MoveRaceError`, and ours stays put by fd.
        """
        dst = self.dirs.imported_path / self.dst_name
        if _path_identity(dst) != identity:
            raise MoveRaceError("imported/ changed during the run")
        title = _title(self.name, self.dst_name)
        return self.ingest(self.conn, dst, title=title, timeout=self.timeout)


def _duplicate(step: _Step, identity: tuple[int, int], digest: str) -> FileOutcome:
    """``imported/<dst_name>`` existed before this move: the same file, dropped again?

    Only when its full SHA-256 matches. Then it is ingested again (rev-105 F2, a deviation
    from the spec's "ingests nothing"): ingest is idempotent by ``source_path``, so this is
    ``unchanged`` (reported ``duplicate``) when its document exists, and it ingests a file
    a crash left in ``imported/`` without one. The drop copy is deleted after a success or
    a rejection (the import holds those bytes), and kept for the next run after a
    transient failure. The existing file is never moved or deleted.
    """
    name, dst_name, dirs = step.name, step.dst_name, step.dirs
    different = FileOutcome(name, "failed", f"a different file holds {dst_name}")
    try:
        fd, dst_st = _open_regular(dst_name, dirs.imported)
    except FileNotFoundError:
        return FileOutcome(name, "vanished")
    except MoveRaceError:
        return different
    except OSError as exc:
        return different if exc.errno == errno.ELOOP else FileOutcome(
            name, "failed", describe_failure(exc)
        )
    try:
        if _sha256_fd(fd) != digest:
            return different
    finally:
        os.close(fd)
    try:
        status = step.ingest_moved(_identity(dst_st))
    except RejectedFileError as exc:
        return FileOutcome(name, "rejected", _cause(exc) + _drop_copy_kept(step, identity))
    except Exception as exc:  # noqa: BLE001 - transient: keep the drop copy for the next run
        return FileOutcome(name, "failed", describe_failure(exc))
    status = "duplicate" if status == "unchanged" else status
    return FileOutcome(name, status, _drop_copy_kept(step, identity).removeprefix("; "))


def _drop_copy_kept(step: _Step, identity: tuple[int, int]) -> str:
    """Delete the drop copy; ``""``, or why it was kept. Never raises (rev-105 N1)."""
    try:
        _unlink_if_same(step.name, step.dirs.folder, identity, step.unlink)
    except OSError as exc:  # a locked file: EPERM
        return f"; drop copy kept ({describe_failure(exc)})"
    return ""


def _unlink_if_same(
    name: str, dir_fd: int, identity: tuple[int, int], unlink: Unlink = _unlink
) -> None:
    """Delete ``name`` only while it is still the file with ``identity``."""
    if _same_file(name, dir_fd, identity):
        unlink(name, dir_fd)


def _move_back(dst_name: str, name: str, mover: _Mover, detail: str, digest: str) -> str:
    """Return the file this run moved; never over a file that took its name meanwhile."""
    try:
        fd, st = _open_regular(dst_name, mover.dirs.imported)
        try:
            mover.move(dst_name, name, fd=fd, identity=_identity(st), digest=digest, back=True)
        finally:
            os.close(fd)
    except (OSError, MoveRaceError) as exc:
        return f"{detail}; left in {IMPORTED_DIR}/{dst_name} ({describe_failure(exc)})"
    return detail
