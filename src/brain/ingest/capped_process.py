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
"""
from __future__ import annotations

import contextlib
import os
import signal
import subprocess
import threading
from collections.abc import Callable, Mapping
from dataclasses import dataclass

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
) -> CappedRun:
    """Run ``argv`` (``argv[0]`` an absolute binary path) under the timeout and the cap."""
    timed_out = threading.Event()
    oversized = False
    chunks: list[bytes] = []
    with popen(
        argv,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        env=dict(env),
        start_new_session=True,
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
        finally:
            timer.cancel()
    if oversized or timed_out.is_set():
        chunks = []
    return CappedRun(b"".join(chunks), returncode, timed_out.is_set(), oversized)


def kill_group(proc: subprocess.Popen[bytes]) -> None:
    """SIGKILL ``proc``'s process group (it leads one: ``start_new_session``)."""
    with contextlib.suppress(ProcessLookupError):  # already gone
        os.killpg(proc.pid, signal.SIGKILL)
