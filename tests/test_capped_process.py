"""The shared capped runner (``brain.ingest.capped_process``): timeout, stdout cap, process group.

Every process here is a throwaway ``/bin/sh`` script in ``tmp_path``; no installed
CLI is ever run.
"""
from __future__ import annotations

import os
import subprocess
import time
from pathlib import Path
from typing import Any

import pytest

from brain.ingest.capped_process import CappedRun, kill_group, run_capped

ENV = {"PATH": os.environ.get("PATH", "/usr/bin:/bin"), "PROBE": "on"}


def _script(tmp_path: Path, body: str) -> str:
    script = tmp_path / "tool"
    script.write_text(f"#!/bin/sh\n{body}\n")
    script.chmod(0o755)
    return str(script)


def _run(argv: list[str], **kwargs: Any) -> CappedRun:
    return run_capped(argv, **{"timeout": 10.0, "max_stdout_bytes": 1000, "env": ENV, **kwargs})


def test_a_run_returns_stdout_exit_code_and_the_given_environment(tmp_path: Path) -> None:
    tool = _script(tmp_path, 'printf \'%s\' "$PROBE:$*"')

    run = _run([tool, "a", "b"])

    assert run == CappedRun(b"on:a b", 0, timed_out=False, oversized=False)


def test_a_failing_run_keeps_its_exit_code_and_never_reads_stderr(tmp_path: Path) -> None:
    tool = _script(tmp_path, "echo secret-on-stderr >&2\nprintf partial\nexit 3")

    run = _run([tool])

    assert run.returncode == 3
    assert run.stdout == b"partial"
    assert not run.timed_out and not run.oversized


def test_the_process_gets_no_stdin_a_discarded_stderr_and_its_own_session(
    tmp_path: Path,
) -> None:
    tool = _script(tmp_path, "printf done")
    seen: dict[str, Any] = {}

    def popen(argv: list[str], **kwargs: Any) -> Any:
        seen.update(kwargs)
        return subprocess.Popen(argv, **kwargs)

    run = _run([tool], popen=popen)

    assert run.stdout == b"done"
    assert seen["stdin"] is subprocess.DEVNULL
    assert seen["stderr"] is subprocess.DEVNULL
    assert seen["stdout"] is subprocess.PIPE
    assert seen["start_new_session"] is True
    assert seen["env"] == ENV


def test_a_hung_process_and_its_children_are_killed_at_the_timeout(tmp_path: Path) -> None:
    """The child ``sleep`` holds stdout open; only a group kill ends the read."""
    tool = _script(tmp_path, "printf early\nsleep 30 &\nsleep 30")
    started = time.monotonic()

    run = _run([tool], timeout=0.3)

    assert run.timed_out and not run.oversized
    assert run.stdout == b""  # nothing from a killed run is handed back
    assert time.monotonic() - started < 10


def test_output_over_the_cap_is_refused_while_reading(tmp_path: Path) -> None:
    tool = _script(tmp_path, "head -c 300000 /dev/zero\nsleep 30")
    started = time.monotonic()

    run = _run([tool], max_stdout_bytes=1000)

    assert run.oversized and not run.timed_out
    assert run.stdout == b""
    assert time.monotonic() - started < 10


def test_output_at_the_cap_is_returned(tmp_path: Path) -> None:
    tool = _script(tmp_path, "printf 12345")

    run = _run([tool], max_stdout_bytes=5)

    assert run.stdout == b"12345" and not run.oversized


def test_a_process_that_cannot_start_raises_os_error(tmp_path: Path) -> None:
    with pytest.raises(OSError):
        _run([str(tmp_path / "missing")])


def test_killing_a_group_that_is_already_gone_is_quiet() -> None:
    proc = subprocess.Popen(["/bin/sh", "-c", "exit 0"], start_new_session=True)
    proc.wait()

    kill_group(proc)  # no ProcessLookupError escapes
