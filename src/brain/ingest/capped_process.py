"""Run one local CLI with a timeout and a stdout cap, for the shell-out ingest sources.

Lifted out of :func:`brain.ingest.github.gh_runner` (RM-31) when RM-36's
``osascript`` reader needed the same runner, so the two cannot drift. Callers
own the binary lookup and the error wording; this module only runs the process
and reports what happened, and raises nothing but ``OSError`` (the process
could not start).

The process gets no stdin and its stderr is discarded unread, because a CLI's
stderr can quote an account, a token or a server message. It runs in its own
process group, and the whole group is killed (SIGKILL) at ``timeout`` or as soon
as stdout passes ``max_stdout_bytes``, because a child it started (an
extension, a credential helper) would otherwise keep stdout open and the read
would wait for it. stdout is read in chunks, so an oversized answer is refused
before it is held.

**Any exception kills the group too** (rev-107 F1). A ``KeyboardInterrupt`` or a
``SystemExit`` raised while the child runs SIGKILLs its group before it propagates;
otherwise ``Popen.__exit__`` would wait for the child (forever, on ``SystemExit``) and an
interrupted run would leave it running. :func:`sigterm_as_system_exit` turns SIGTERM (a
``launchctl bootout``) into that ``SystemExit``, so a caller's ``finally`` blocks and
temporary directories are cleaned up instead of the process dying where it stands.

**Two opt-in options**, both added for voice memos (RM-38) and passed to ``Popen`` only
when set, so the other runners' calls are unchanged: ``pass_fds`` lets the child read a
file the caller has already opened (as ``/dev/fd/N``), and ``max_file_bytes`` sets the
child's ``RLIMIT_FSIZE``, so no file it writes can grow past that size. What the tool
then does varies: killed by SIGXFSZ, an error exit, or a quiet exit 0 with the file cut
at the limit. ``afconvert`` exits 0 with the file truncated, or is killed by SIGXFSZ,
depending on the build and how it is started; a caller handles both, judging the signal
and the file rather than exit 0.
The limit is set by a ``preexec_fn`` (:func:`limit_file_size`) on a value computed before
the fork. It also sets SIGXFSZ to its default action, so a tool that does not handle the
signal itself dies of it at the limit, ``returncode == -SIGXFSZ``. (``Popen``'s default
``restore_signals`` already does this; the hook does not rely on it.)
"""
from __future__ import annotations

import contextlib
import functools
import os
import resource
import signal
import subprocess
import threading
from collections.abc import Callable, Iterator, Mapping, Sequence
from dataclasses import dataclass
from types import FrameType
from typing import Any

_READ_CHUNK_BYTES = 65536

#: ``subprocess.Popen``'s shape, as the test seam sees it.
Popen = Callable[..., subprocess.Popen[bytes]]


@dataclass(frozen=True)
class CappedRun:
    """What one run did. ``stdout`` is empty when the run timed out or was oversized."""

    stdout: bytes
    returncode: int
    timed_out: bool
    oversized: bool


def run_capped(
    argv: list[str],
    *,
    timeout: float,
    max_stdout_bytes: int,
    env: Mapping[str, str],
    popen: Popen = subprocess.Popen,
    pass_fds: Sequence[int] = (),
    max_file_bytes: int | None = None,
) -> CappedRun:
    """Run ``argv`` (``argv[0]`` an absolute binary path) under the timeout and the cap.

    ``pass_fds`` and ``max_file_bytes`` are the opt-in options of the module docstring.
    """
    timed_out = threading.Event()
    oversized = False
    chunks: list[bytes] = []
    options: dict[str, Any] = {}
    if pass_fds:
        options["pass_fds"] = tuple(pass_fds)
    if max_file_bytes is not None:
        limit = file_size_limit(max_file_bytes)
        options["preexec_fn"] = functools.partial(limit_file_size, limit)
    with popen(
        argv,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        env=dict(env),
        start_new_session=True,
        **options,
    ) as proc:
        def _kill() -> None:
            timed_out.set()
            kill_group(proc)

        timer = threading.Timer(timeout, _kill)
        timer.start()
        try:
            size = 0
            assert proc.stdout is not None  # stdout=PIPE
            while chunk := proc.stdout.read(_READ_CHUNK_BYTES):
                size += len(chunk)
                if size > max_stdout_bytes:
                    oversized = True
                    kill_group(proc)
                    break
                chunks.append(chunk)
            returncode = proc.wait()
        except BaseException:
            kill_group(proc)  # an interrupt or SIGTERM must not leave the child running
            raise
        finally:
            timer.cancel()
    if oversized or timed_out.is_set():
        chunks = []
    return CappedRun(b"".join(chunks), returncode, timed_out.is_set(), oversized)


def kill_group(proc: subprocess.Popen[bytes]) -> None:
    """SIGKILL ``proc``'s process group (it leads one: ``start_new_session``).

    A group that is already gone is quiet. So is one whose members have all exited
    but are not yet reaped: macOS answers ``killpg`` on such a group with ``EPERM``,
    not ``ESRCH`` (RM-43), and an oversized answer from a child that had finished
    writing was reported as the binary failing to start.
    """
    with contextlib.suppress(ProcessLookupError, PermissionError):  # gone, or only zombies
        os.killpg(proc.pid, signal.SIGKILL)


def limit_file_size(limit: int) -> None:  # pragma: no cover — runs in the forked child
    """The ``preexec_fn``: SIGXFSZ to its default action, then ``RLIMIT_FSIZE`` to ``limit``.

    It changes the process it runs in, so only a child about to ``exec`` may call it; it is
    tested in a fresh interpreter (``tests/test_capped_process.py``), which coverage does
    not trace.
    """
    signal.signal(signal.SIGXFSZ, signal.SIG_DFL)
    resource.setrlimit(resource.RLIMIT_FSIZE, (limit, limit))


def file_size_limit(requested: int) -> int:
    """``requested``, lowered to the current hard ``RLIMIT_FSIZE`` (a child cannot raise it)."""
    _, hard = resource.getrlimit(resource.RLIMIT_FSIZE)
    return requested if hard == resource.RLIM_INFINITY else min(requested, hard)


@contextlib.contextmanager
def sigterm_as_system_exit(*, also: tuple[int, ...] = ()) -> Iterator[None]:
    """While inside, SIGTERM (and each signal in ``also``) raises ``SystemExit(128 + n)``.

    The exception unwinds through :func:`run_capped` (which kills its child) and every
    ``finally`` and ``with`` above it. Outside the main thread, where Python cannot set a
    handler, nothing changes. The previous handlers are restored on the way out; one set
    outside Python reads as ``None`` and is restored as the default. ``also`` lets a
    caller holding a sensitive temporary copy add ``SIGHUP`` (a closed terminal tab;
    ``brain ingest-web``, sec-114 F10).
    """
    if threading.current_thread() is not threading.main_thread():
        yield
        return
    previous = {
        signum: signal.signal(signum, _raise_system_exit) for signum in (signal.SIGTERM, *also)
    }
    try:
        yield
    finally:
        for signum, handler in previous.items():
            signal.signal(signum, handler or signal.SIG_DFL)


def _raise_system_exit(signum: int, frame: FrameType | None) -> None:
    raise SystemExit(128 + signum)
