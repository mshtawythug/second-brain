"""Where an upload waits between its request and its job (Ingest spec §7.3, §7.4, RM-13).

**It writes only under its own private temp root**, and deletes what it wrote. That is
not corpus data and not the vault (§6 classes a staged file the same way), so the
mutation-confinement rule (§1.3: only ``notes_service.py`` and ``ingest_service.py``
mutate the corpus or the vault) is unchanged by this module.

- **The root.** ``mkdtemp(prefix="brain-ui-ingest-<pid>-")``, mode ``0700``, once per
  server, created on the first upload (a server nobody uploads to leaves nothing in
  the temp directory). One ``0700`` directory per upload, named by a server-generated uuid4 hex;
  nothing the client sends becomes a directory name. The file inside is the
  sanitized ``<stem><ext>``, so an extractor's ``Path.stem`` title (C6) is the user's
  filename, opened ``O_CREAT | O_EXCL | O_NOFOLLOW`` with mode ``0600``.
- **Caps on the stream.** An honest ``Content-Length`` over the file's cap, or over
  the remaining budget, is refused before a byte is read; a lying or absent one is
  refused by the counter. At most one transport chunk is held in memory. Every
  refusal, and any other exception mid-stream (a client disconnect, a cancelled
  task), deletes the partial file. At most :data:`MAX_STAGED_UPLOADS` uploads are
  held at once, and each is charged in whole filesystem blocks
  (:data:`STAGING_BLOCK_BYTES`), so many tiny files cannot exhaust inodes.
- **Off the event loop.** Directory creation, every write and the sniff run in a
  worker thread (``starlette.concurrency.run_in_threadpool``, as ``routes_meta``
  does; it waits for the thread rather than abandoning it on cancellation): a
  25 MiB write or a 5 MiB UTF-8 check must not stall every other request, the job
  polls included.
- **A filesystem failure is a fixed error.** Any ``OSError`` while staging (a full
  disk, a name the filesystem refuses) is 503 ``staging_failed`` with a fixed
  message, logged by class only. Its text would name the staged path, and with it
  the user's filename, in the server's unhandled-error log.
- **Lifetime.** An upload is *unclaimed* until ``POST /api/ingest/file`` claims it for
  a job, and is then deleted by :meth:`UploadStaging.release` when the job settles
  (the registry's ``on_settled``, which covers a queued job that never ran). An
  unclaimed upload expires after :data:`UPLOAD_TTL`, checked on each stage and claim.
  Claimed uploads still count against :data:`MAX_STAGED_BYTES` and never expire.
  :meth:`close` removes the whole root at shutdown, and :meth:`sweep` (the app
  lifespan's startup) removes a dead process's root, chosen by pid, never by age,
  and only one this user owns. The sweep never fails startup: anything it raises
  is logged by class and the sweep is skipped.

**Threads.** Staging runs on the event loop; release runs on the job worker. One lock
guards the tables; file deletion happens outside it.

**Logging** carries the upload id and size only, never the filename (§7.5).
"""
from __future__ import annotations

import logging
import os
import re
import shutil
import stat
import tempfile
import threading
import uuid
from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import BinaryIO

from starlette.concurrency import run_in_threadpool

from ..errors import describe_failure
from .errors import UiNotFound, UiPayloadTooLarge, UiTooManyRequests, UiUnavailable
from .ingest_limits import (
    MAX_STAGED_BYTES,
    MAX_STAGED_UPLOADS,
    STAGING_BLOCK_BYTES,
    UPLOAD_TTL,
)
from .upload_checks import SafeName, sniff, upload_cap, upload_kind

logger = logging.getLogger(__name__)

#: Every staging root's name starts with this, then ``<pid>-``.
ROOT_PREFIX = "brain-ui-ingest-"
#: Seven digits: past every real pid_max (macOS 99,998; Linux at most 2**22), and
#: far short of anything ``os.kill`` cannot take (a huge pid raised OverflowError
#: and stopped ``brain ui`` from starting). Range-checked below as well.
_ROOT_RE = re.compile(rf"^{re.escape(ROOT_PREFIX)}(\d{{1,7}})-")
_MAX_PID = 2**22
_UPLOAD_ID_RE = re.compile(r"^[0-9a-f]{32}$")


@dataclass(frozen=True)
class StagedUpload:
    """One file on disk, waiting for (or claimed by) a job."""

    upload_id: str
    path: Path
    name: SafeName
    size: int
    kind: str
    staged_at: datetime


def _unknown() -> UiNotFound:
    return UiNotFound(
        "no such upload; staged files are kept for 15 minutes. Choose the file again",
        code="upload_unknown",
    )


def _too_large(cap: int) -> UiPayloadTooLarge:
    return UiPayloadTooLarge(f"this file type may be at most {cap} bytes", code="upload_too_large")


def _too_many() -> UiTooManyRequests:
    return UiTooManyRequests(
        f"at most {MAX_STAGED_UPLOADS} files may wait to be ingested; "
        "remove one or retry when a job finishes",
        code="too_many_uploads",
    )


def _staging_failed() -> UiUnavailable:
    return UiUnavailable(
        "the file could not be stored for ingest (the server's disk refused it)",
        code="staging_failed",
    )


def _charge(size: int) -> int:
    """Bytes ``size`` costs against the budget: whole blocks, at least one."""
    return max(1, -(-size // STAGING_BLOCK_BYTES)) * STAGING_BLOCK_BYTES


def _full() -> UiTooManyRequests:
    return UiTooManyRequests(
        f"at most {MAX_STAGED_BYTES} bytes may wait to be ingested; retry when a job finishes",
        code="staging_full",
    )


def _remove(directory: Path) -> None:
    """Delete one upload's directory; a failure is logged by class and never raised."""
    try:
        shutil.rmtree(directory)
    except FileNotFoundError:
        return
    except OSError as exc:
        logger.warning("ui upload: could not remove a staged file: %s", describe_failure(exc))


class UploadStaging:
    """The staging tables and their root. See the module docstring."""

    def __init__(
        self,
        *,
        clock: Callable[[], datetime],
        parent: Path | None = None,
        ttl: timedelta = UPLOAD_TTL,
        budget: int = MAX_STAGED_BYTES,
        max_uploads: int = MAX_STAGED_UPLOADS,
        sweeper: Callable[..., list[Path]] | None = None,
        opener: Callable[[Path, Path], BinaryIO] | None = None,
        sniffer: Callable[[Path, str], None] | None = None,
    ) -> None:
        self._clock = clock
        self._ttl = ttl
        self._budget = budget
        self._max_uploads = max_uploads
        # Injected so a test can prove a failing sweep never blocks startup.
        self._sweeper = sweeper or sweep_stale_roots
        # Injected so a test can prove the blocking calls run off the event loop.
        self._opener = opener or _open_staging_file
        self._sniffer = sniffer or sniff
        self._parent = Path(parent) if parent is not None else Path(tempfile.gettempdir())
        self._root: Path | None = None
        self._lock = threading.Lock()
        self._unclaimed: dict[str, StagedUpload] = {}
        self._claimed: dict[str, StagedUpload] = {}
        self._inflight: dict[str, int] = {}

    # ------------------------------------------------------------ public --

    @property
    def root(self) -> Path:
        """This server's staging root, created (mode ``0700``) on first use."""
        with self._lock:
            if self._root is None:
                # mkdtemp creates the directory with mode 0700.
                self._root = Path(
                    tempfile.mkdtemp(prefix=f"{ROOT_PREFIX}{os.getpid()}-", dir=self._parent)
                )
            return self._root

    def sweep(self) -> list[Path]:
        """Remove dead processes' roots beside this one (startup; §7.4 "Lifetime").

        Never raises: a startup sweep is housekeeping, and a failure in it must not
        stop ``brain ui`` from starting. It is logged by class and skipped.
        """
        try:
            return self._sweeper(self._parent, own_pid=os.getpid(), own_uid=os.getuid())
        except Exception as exc:
            logger.warning("ui upload: startup sweep skipped: %s", describe_failure(exc))
            return []

    def used_bytes(self) -> int:
        """Bytes staged, claimed or still streaming."""
        with self._lock:
            return self._used_locked()

    async def stage(
        self, name: SafeName, declared: int | None, chunks: AsyncIterator[bytes]
    ) -> StagedUpload:
        """Stream ``chunks`` to a new staging file, enforcing every cap; then sniff it.

        ``declared`` is the request's ``Content-Length`` (``None`` when absent).
        """
        self.expire()
        cap = upload_cap(name.ext)
        if declared is not None and declared > cap:
            raise _too_large(cap)
        upload_id = uuid.uuid4().hex
        with self._lock:
            if self._count_locked() >= self._max_uploads:
                raise _too_many()
            if declared is not None and self._used_locked() + _charge(declared) > self._budget:
                raise _full()
            self._inflight[upload_id] = 0
        directory = self.root / upload_id
        path = directory / name.basename
        try:
            try:
                size = await self._stage_file(upload_id, directory, path, name, cap, chunks)
            except OSError as exc:
                # Class only: the message would carry the staged path, which
                # holds the user's filename.
                logger.warning(
                    "ui upload %s could not be staged: %s", upload_id, describe_failure(exc)
                )
                raise _staging_failed() from None
        except BaseException:
            with self._lock:
                self._inflight.pop(upload_id, None)
            # Synchronous on purpose: on a cancelled task (a client disconnect) an
            # `await` here would itself be cancelled and the partial file would stay.
            _remove(directory)
            raise
        upload = StagedUpload(
            upload_id=upload_id,
            path=path,
            name=name,
            size=size,
            kind=upload_kind(name.ext),
            staged_at=self._clock(),
        )
        with self._lock:
            self._inflight.pop(upload_id, None)
            self._unclaimed[upload_id] = upload
        logger.info("ui upload %s staged (%d bytes)", upload_id, size)
        return upload

    def claim(self, upload_id: str) -> StagedUpload:
        """Hand an unclaimed upload to a job. 404 ``upload_unknown`` otherwise."""
        self.expire()
        with self._lock:
            upload = self._unclaimed.pop(self._valid(upload_id), None)
            if upload is None:
                raise _unknown()
            self._claimed[upload_id] = upload
            return upload

    def unclaim(self, upload_id: str) -> None:
        """Undo :meth:`claim` when the job could not be queued."""
        with self._lock:
            upload = self._claimed.pop(upload_id, None)
            if upload is not None:
                self._unclaimed[upload_id] = upload

    def release(self, upload_id: str) -> None:
        """Delete a claimed upload once its job has settled. Idempotent."""
        with self._lock:
            upload = self._claimed.pop(upload_id, None)
        if upload is not None:
            _remove(upload.path.parent)

    def discard(self, upload_id: str) -> None:
        """``DELETE /api/ingest/uploads/{id}``: drop an unclaimed upload."""
        with self._lock:
            upload = self._unclaimed.pop(self._valid(upload_id), None)
        if upload is None:
            raise _unknown()
        _remove(upload.path.parent)

    def expire(self) -> None:
        """Delete every unclaimed upload older than the TTL."""
        cutoff = self._clock() - self._ttl
        with self._lock:
            stale = [u for u in self._unclaimed.values() if u.staged_at < cutoff]
            for upload in stale:
                del self._unclaimed[upload.upload_id]
        for upload in stale:
            _remove(upload.path.parent)

    def close(self) -> None:
        """Remove the whole root (server shutdown). Safe to call when none exists."""
        with self._lock:
            self._unclaimed.clear()
            self._claimed.clear()
            root, self._root = self._root, None
        if root is not None:
            _remove(root)

    # ---------------------------------------------------------- internals --

    @staticmethod
    def _valid(upload_id: str) -> str:
        if not _UPLOAD_ID_RE.fullmatch(upload_id):
            raise _unknown()
        return upload_id

    def _used_locked(self) -> int:
        return (
            sum(_charge(u.size) for u in self._unclaimed.values())
            + sum(_charge(u.size) for u in self._claimed.values())
            + sum(_charge(n) for n in self._inflight.values())
        )

    def _count_locked(self) -> int:
        return len(self._unclaimed) + len(self._claimed) + len(self._inflight)

    async def _stage_file(
        self,
        upload_id: str,
        directory: Path,
        path: Path,
        name: SafeName,
        cap: int,
        chunks: AsyncIterator[bytes],
    ) -> int:
        """Create, fill and sniff one staging file; every blocking call off the loop."""
        handle = await run_in_threadpool(self._opener, directory, path)
        try:
            size = 0
            async for piece in chunks:
                size += len(piece)
                if size > cap:
                    raise _too_large(cap)
                with self._lock:
                    self._inflight[upload_id] = size
                    if self._used_locked() > self._budget:
                        raise _full()
                await run_in_threadpool(handle.write, piece)
        finally:
            handle.close()  # synchronous for the same reason as stage()'s cleanup
        await run_in_threadpool(self._sniffer, path, name.ext)
        return size


def _open_staging_file(directory: Path, path: Path) -> BinaryIO:
    """The upload's own ``0700`` directory and its ``0600`` file, created exclusively."""
    directory.mkdir(mode=0o700)
    # The sanitizer already removed every separator; this is the backstop.
    if path.resolve().parent != directory.resolve():
        raise _unknown()  # pragma: no cover — unreachable after sanitize_filename
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
    return os.fdopen(os.open(path, flags, 0o600), "wb")


def pid_alive(pid: int) -> bool:
    """True unless ``pid`` is certainly dead (``ProcessLookupError``).

    Every other answer counts as alive, the failure direction that keeps a root:
    ``PermissionError`` (it exists; it is someone else's), and ``OverflowError`` /
    ``ValueError`` / ``OSError`` for a pid the OS will not even look up.
    """
    if pid <= 0:
        return True  # 0 and -1 address process GROUPS / every process, not one pid
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except (OverflowError, ValueError, OSError):
        return True
    return True


def sweep_stale_roots(
    parent: Path,
    *,
    own_pid: int,
    own_uid: int | None = None,
    is_alive: Callable[[int], bool] = pid_alive,
) -> list[Path]:
    """Remove the staging roots of dead ``brain ui`` processes; return what went.

    Chosen by pid, never by age: a live second instance keeps its root however old
    its uploads are. Always left alone: a name with no parseable, in-range pid; a
    symlink (judged by ``lstat``, never followed); anything that is not a directory;
    anything this user does not own (``own_uid``, defaulting to ``os.getuid()``:
    the temp directory can be shared, and a matching name is no proof of whose it
    is); and this process's own root. If the OS reused a dead instance's pid, its
    root survives until that pid dies too: leftovers stay, live uploads never go.
    """
    uid = os.getuid() if own_uid is None else own_uid
    removed: list[Path] = []
    try:
        entries = sorted(parent.iterdir())
    except OSError as exc:
        logger.warning("ui upload: could not list the temp directory: %s", describe_failure(exc))
        return removed
    for entry in entries:
        match = _ROOT_RE.match(entry.name)
        if match is None:
            continue
        pid = int(match.group(1))
        if not 0 < pid <= _MAX_PID or pid == own_pid:
            continue
        try:
            info = entry.lstat()
        except OSError:
            continue
        if not stat.S_ISDIR(info.st_mode) or info.st_uid != uid:
            continue
        if is_alive(pid):
            continue
        _remove(entry)
        removed.append(entry)
    return removed
