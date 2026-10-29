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
  task), deletes the partial file.
- **Lifetime.** An upload is *unclaimed* until ``POST /api/ingest/file`` claims it for
  a job, and is then deleted by :meth:`UploadStaging.release` when the job settles
  (the registry's ``on_settled``, which covers a queued job that never ran). An
  unclaimed upload expires after :data:`UPLOAD_TTL`, checked on each stage and claim.
  Claimed uploads still count against :data:`MAX_STAGED_BYTES` and never expire.
  :meth:`close` removes the whole root at shutdown, and :meth:`sweep` (the app
  lifespan's startup) removes a dead process's root, chosen by pid, never by age.

**Threads.** Staging runs on the event loop; release runs on the job worker. One lock
guards the tables; file deletion happens outside it.

**Logging** carries the upload id and size only, never the filename (§7.5).
"""
from __future__ import annotations

import logging
import os
import re
import shutil
import tempfile
import threading
import uuid
from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path

from ..errors import describe_failure
from .errors import UiNotFound, UiPayloadTooLarge, UiTooManyRequests
from .ingest_limits import MAX_STAGED_BYTES, UPLOAD_TTL
from .upload_checks import SafeName, sniff, upload_cap, upload_kind

logger = logging.getLogger(__name__)

#: Every staging root's name starts with this, then ``<pid>-``.
ROOT_PREFIX = "brain-ui-ingest-"
_ROOT_RE = re.compile(rf"^{re.escape(ROOT_PREFIX)}(\d{{1,10}})-")
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
    ) -> None:
        self._clock = clock
        self._ttl = ttl
        self._budget = budget
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
        """Remove dead processes' roots beside this one (startup; §7.4 "Lifetime")."""
        return sweep_stale_roots(self._parent, own_pid=os.getpid())

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
            if declared is not None and self._used_locked() + declared > self._budget:
                raise _full()
            self._inflight[upload_id] = 0
        directory = self.root / upload_id
        path = directory / name.basename
        try:
            directory.mkdir(mode=0o700)
            # The sanitizer already removed every separator; this is the backstop.
            if path.resolve().parent != directory.resolve():
                raise _unknown()  # pragma: no cover — unreachable after sanitize_filename
            size = await self._write(upload_id, path, cap, chunks)
            sniff(path, name.ext)
        except BaseException:
            with self._lock:
                self._inflight.pop(upload_id, None)
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
            sum(u.size for u in self._unclaimed.values())
            + sum(u.size for u in self._claimed.values())
            + sum(self._inflight.values())
        )

    async def _write(
        self, upload_id: str, path: Path, cap: int, chunks: AsyncIterator[bytes]
    ) -> int:
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
        fd = os.open(path, flags, 0o600)
        size = 0
        with os.fdopen(fd, "wb") as handle:
            async for piece in chunks:
                size += len(piece)
                if size > cap:
                    raise _too_large(cap)
                with self._lock:
                    self._inflight[upload_id] = size
                    if self._used_locked() > self._budget:
                        raise _full()
                handle.write(piece)
        return size


def pid_alive(pid: int) -> bool:
    """True unless ``pid`` is certainly dead (``ProcessLookupError``)."""
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True  # it exists; it is someone else's
    return True


def sweep_stale_roots(
    parent: Path, *, own_pid: int, is_alive: Callable[[int], bool] = pid_alive
) -> list[Path]:
    """Remove the staging roots of dead ``brain ui`` processes; return what went.

    Chosen by pid, never by age: a live second instance keeps its root however old
    its uploads are. A root whose name carries no parseable pid, a symlink, and this
    process's own root are always left alone. If the OS reused a dead instance's pid,
    its root survives until that pid dies too: leftovers stay, live uploads never go.
    """
    removed: list[Path] = []
    try:
        entries = sorted(parent.iterdir())
    except OSError as exc:
        logger.warning("ui upload: could not list the temp directory: %s", describe_failure(exc))
        return removed
    for entry in entries:
        match = _ROOT_RE.match(entry.name)
        if match is None or entry.is_symlink() or not entry.is_dir():
            continue
        pid = int(match.group(1))
        if pid == own_pid or is_alive(pid):
            continue
        _remove(entry)
        removed.append(entry)
    return removed
