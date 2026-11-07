"""``brain ingest-gmail`` killed by SIGTERM or SIGHUP takes its hung ``gws`` with it (sec-126 F1).

``gws`` runs in its own session (``capped_process.run_capped``), so the terminal's SIGHUP never
reaches it, and the timer that would kill it dies with Python. Only the command's
``sigterm_as_system_exit`` handler turns the signal into the ``SystemExit`` that kills the group.

Each test runs ``brain ingest-gmail`` as a subprocess against a throwaway ``gws`` stub that
records its pid and hangs (``tests/gws_stub_fixtures.py``). The list call hangs before the
command connects (the database URL is unreachable), so the file is ``nodb``. Every wait is
bounded (``tests.thread_wait.wait_for``'s backstop, and the process waits' own timeouts); the
stub's group is SIGKILLed in ``finally`` whatever happens.
"""
from __future__ import annotations

import contextlib
import os
import signal
import subprocess
import sys
from pathlib import Path

import pytest

from tests.gws_stub_fixtures import FAST_SECONDS, HUNG, SYNTH_ADDRESS
from tests.thread_wait import wait_for

pytestmark = pytest.mark.nodb

def _group_has_live_members(pgid: int) -> bool:
    try:
        os.killpg(pgid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:  # macOS: every member has exited, only zombies are left
        return False
    return True


@pytest.mark.parametrize("signum", [signal.SIGTERM, signal.SIGHUP], ids=["SIGTERM", "SIGHUP"])
def test_a_signal_during_a_hung_list_kills_the_gws_group(
    tmp_path: Path, signum: signal.Signals
) -> None:
    pidfile = tmp_path / "gws.pid"
    bindir = tmp_path / "bin"
    bindir.mkdir()
    gws = bindir / "gws"
    gws.write_text(f"#!/bin/sh\necho $$ > {pidfile}\n{HUNG}\n")
    gws.chmod(0o755)
    env = {
        **os.environ,
        "PATH": str(bindir),
        "DATABASE_URL": "postgresql://localhost:1/none",
        "BRAIN_VAULT_PATH": str(tmp_path / "vault"),
    }
    proc = subprocess.Popen(
        [sys.executable, "-c", "from brain.cli import app; app()",
         "ingest-gmail", "--from", SYNTH_ADDRESS],
        env=env, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    pgid = None
    try:
        wait_for(lambda: pidfile.exists() and pidfile.read_text().strip())  # the stub started
        pgid = int(pidfile.read_text().strip())  # its own session: the pid is the group id

        proc.send_signal(signum)
        returncode = proc.wait(timeout=FAST_SECONDS)

        assert returncode == 128 + signum, f"brain died of the signal itself ({returncode})"
        wait_for(lambda: not _group_has_live_members(pgid))  # the gws group was killed
    finally:
        if pgid is not None:
            with contextlib.suppress(ProcessLookupError, PermissionError):
                os.killpg(pgid, signal.SIGKILL)
        if proc.poll() is None:
            proc.kill()
            proc.wait(timeout=FAST_SECONDS)
