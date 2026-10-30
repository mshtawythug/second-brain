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


# --- review round 1 (PR #107): an exception kills the group; file-size limit; fds ----------


class _RaisingReader:
    """Stands in for the child's stdout: the first read raises ``exc``, as a signal would."""

    def __init__(self, exc: BaseException) -> None:
        self._exc = exc

    def read(self, n: int) -> bytes:
        raise self._exc

    def close(self) -> None:
        return None


def _wait_for_pid(pid_file: Path) -> None:
    """Wait until the script has started its background child and written its pid."""
    deadline = time.monotonic() + 5
    while not (pid_file.exists() and pid_file.read_text().strip()):
        assert time.monotonic() < deadline, "the script never wrote its child's pid"
        time.sleep(0.02)


def _popen_raising(exc: BaseException, procs: list[subprocess.Popen[bytes]]) -> Any:
    def popen(argv: list[str], **kwargs: Any) -> subprocess.Popen[bytes]:
        proc = subprocess.Popen(argv, **kwargs)
        _wait_for_pid(Path(argv[1]))
        assert proc.stdout is not None
        proc.stdout.close()
        proc.stdout = _RaisingReader(exc)  # type: ignore[assignment]
        procs.append(proc)
        return proc

    return popen


@pytest.mark.parametrize("exc", [KeyboardInterrupt(), SystemExit(143)])
def test_an_exception_mid_read_kills_the_whole_group(
    tmp_path: Path, exc: BaseException
) -> None:
    """Without the kill, ``Popen.__exit__`` waits out the ``sleep`` (rev-107 F1)."""
    tool = _script(tmp_path, "sleep 30 &\necho $! > \"$1\"\nsleep 30")
    child_pid_file = tmp_path / "child.pid"
    procs: list[subprocess.Popen[bytes]] = []
    started = time.monotonic()

    with pytest.raises(type(exc)):
        _run([tool, str(child_pid_file)], popen=_popen_raising(exc, procs))

    (proc,) = procs
    assert proc.wait(timeout=5) == -9
    assert time.monotonic() - started < 10
    _assert_gone(child_pid_file)


def _assert_gone(pid_file: Path) -> None:
    """The background child the script started is dead (its group was killed)."""
    deadline = time.monotonic() + 5
    pid = int(pid_file.read_text())
    while time.monotonic() < deadline:
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return
        time.sleep(0.05)  # pragma: no cover — a SIGKILLed child is reaped by init quickly
    raise AssertionError("the background child outlived the group kill")  # pragma: no cover


def test_max_file_bytes_caps_what_the_child_can_write(tmp_path: Path) -> None:
    out = tmp_path / "out.bin"
    tool = _script(tmp_path, 'head -c 100000 /dev/zero > "$1"')

    _run([tool, str(out)], max_file_bytes=1000)

    assert out.stat().st_size <= 1000


def test_without_max_file_bytes_the_child_writes_freely(tmp_path: Path) -> None:
    out = tmp_path / "out.bin"
    tool = _script(tmp_path, 'head -c 100000 /dev/zero > "$1"')

    _run([tool, str(out)])

    assert out.stat().st_size == 100000


def test_the_limit_never_exceeds_the_hard_limit(monkeypatch: pytest.MonkeyPatch) -> None:
    import resource

    from brain.ingest.capped_process import file_size_limit

    monkeypatch.setattr(resource, "getrlimit", lambda which: (500, 700))
    assert file_size_limit(1000) == 700
    assert file_size_limit(600) == 600
    monkeypatch.setattr(
        resource, "getrlimit", lambda which: (resource.RLIM_INFINITY, resource.RLIM_INFINITY)
    )
    assert file_size_limit(1000) == 1000


def test_pass_fds_lets_the_child_read_the_parent_s_fd(tmp_path: Path) -> None:
    data = tmp_path / "data.txt"
    data.write_bytes(b"only-through-the-fd")
    tool = _script(tmp_path, 'cat "/dev/fd/$1"')
    fd = os.open(data, os.O_RDONLY)
    try:
        run = _run([tool, str(fd)], pass_fds=(fd,))
    finally:
        os.close(fd)

    assert run.stdout == b"only-through-the-fd"


def test_the_new_options_are_not_passed_when_unused(tmp_path: Path) -> None:
    """The GitHub and Apple Notes runners keep exactly the Popen call they had."""
    tool = _script(tmp_path, "printf done")
    seen: dict[str, Any] = {}

    def popen(argv: list[str], **kwargs: Any) -> Any:
        seen.update(kwargs)
        return subprocess.Popen(argv, **kwargs)

    _run([tool], popen=popen)

    assert "pass_fds" not in seen and "preexec_fn" not in seen


def test_sigterm_inside_the_context_is_a_system_exit() -> None:
    import signal

    from brain.ingest.capped_process import sigterm_as_system_exit

    before = signal.getsignal(signal.SIGTERM)
    with pytest.raises(SystemExit) as info, sigterm_as_system_exit():
        signal.raise_signal(signal.SIGTERM)

    assert info.value.code == 128 + signal.SIGTERM
    assert signal.getsignal(signal.SIGTERM) == before


def test_sigterm_mid_run_kills_the_child(tmp_path: Path) -> None:
    """A ``launchctl bootout`` mid-whisper: the child dies and the caller unwinds."""
    import signal

    from brain.ingest.capped_process import sigterm_as_system_exit

    class _SigtermReader(_RaisingReader):
        def read(self, n: int) -> bytes:
            signal.raise_signal(signal.SIGTERM)
            return b""  # pragma: no cover — the handler raised

    tool = _script(tmp_path, "sleep 30 &\necho $! > \"$1\"\nsleep 30")
    procs: list[subprocess.Popen[bytes]] = []

    def popen(argv: list[str], **kwargs: Any) -> subprocess.Popen[bytes]:
        proc = subprocess.Popen(argv, **kwargs)
        _wait_for_pid(Path(argv[1]))
        assert proc.stdout is not None
        proc.stdout.close()
        proc.stdout = _SigtermReader(KeyboardInterrupt())  # type: ignore[assignment]
        procs.append(proc)
        return proc

    with pytest.raises(SystemExit), sigterm_as_system_exit():
        _run([tool, str(tmp_path / "child.pid")], popen=popen)

    assert procs[0].wait(timeout=5) == -9
    _assert_gone(tmp_path / "child.pid")


def test_the_context_restores_a_handler_installed_outside_python() -> None:
    """``signal.signal`` returns ``None`` for a C-installed handler; restore the default."""
    import signal

    from brain.ingest import capped_process

    calls: list[Any] = []

    def fake_signal(signum: int, handler: Any) -> Any:
        calls.append(handler)
        return None

    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(capped_process.signal, "signal", fake_signal)
        with capped_process.sigterm_as_system_exit():
            pass

    assert calls[-1] == signal.SIG_DFL


def test_outside_the_main_thread_the_context_changes_nothing() -> None:
    import signal
    import threading

    from brain.ingest.capped_process import sigterm_as_system_exit

    seen: list[Any] = []

    def body() -> None:
        with sigterm_as_system_exit():
            seen.append(signal.getsignal(signal.SIGTERM))

    before = signal.getsignal(signal.SIGTERM)
    thread = threading.Thread(target=body)
    thread.start()
    thread.join()

    assert seen == [before]


def test_the_limit_hook_restores_sigxfsz_and_sets_the_limit() -> None:
    """Run in a fresh interpreter: the hook changes the process it runs in."""
    import sys

    code = (
        "import resource, signal\n"
        "from brain.ingest.capped_process import limit_file_size\n"
        "limit_file_size(123456)\n"
        "print(signal.getsignal(signal.SIGXFSZ) == signal.SIG_DFL,"
        " resource.getrlimit(resource.RLIMIT_FSIZE))\n"
    )
    out = subprocess.run(
        [sys.executable, "-c", code], capture_output=True, text=True, check=True
    ).stdout

    assert out.strip() == "True (123456, 123456)"


def test_a_writer_that_honours_sigxfsz_is_killed_at_the_limit(tmp_path: Path) -> None:
    """``head`` dies of SIGXFSZ (the shell reports 128 + 25); the file stops at the limit."""
    out = tmp_path / "out.bin"
    tool = _script(tmp_path, 'head -c 100000 /dev/zero > "$1"\nprintf "%s" "$?"')

    run = _run([tool, str(out)], max_file_bytes=1000)

    assert run.stdout == b"153"
    assert out.stat().st_size == 1000


def test_the_hook_does_not_rely_on_popen_restoring_signals(tmp_path: Path) -> None:
    """Python ignores SIGXFSZ; with ``restore_signals=False`` only the hook resets it."""
    import signal

    assert signal.getsignal(signal.SIGXFSZ) == signal.SIG_IGN  # inherited unless reset
    out = tmp_path / "out.bin"
    tool = _script(tmp_path, 'head -c 100000 /dev/zero > "$1" 2>/dev/null\nprintf "%s" "$?"')

    def popen(argv: list[str], **kwargs: Any) -> subprocess.Popen[bytes]:
        return subprocess.Popen(argv, restore_signals=False, **kwargs)

    run = _run([tool, str(out)], max_file_bytes=1000, popen=popen)

    assert run.stdout == b"153"  # killed by SIGXFSZ (128 + 25), not an EFBIG exit 1
