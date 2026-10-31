"""The capped read of :func:`brain.vault.derived_links.gws.real_gws_runner` (RM-30).

Each test runs a tiny real executable from ``tmp_path`` in place of ``gws``, so
the chunked read, the cap, the timer and the exit-code paths run for real, with
no mock of ``subprocess``. The command is passed by absolute path, which
``shutil.which`` accepts.
"""
from __future__ import annotations

import sys
import time
from pathlib import Path

import pytest

from brain.cli_ingest_calendar import (
    GWS_MAX_STDOUT_BYTES,
    GWS_TIMEOUT_SECONDS,
    calendar_gws_runner,
)
from brain.errors import DirectoryRefreshError, describe_failure
from brain.vault.derived_links import gws


def _script(tmp_path: Path, body: str) -> str:
    path = tmp_path / "fake-gws"
    path.write_text(f"#!{sys.executable}\nimport sys, time\n{body}\n", encoding="utf-8")
    path.chmod(0o755)
    return str(path)


def test_output_under_the_cap_is_returned(tmp_path: Path) -> None:
    exe = _script(tmp_path, "sys.stdout.write('x' * 1000)")

    out = gws.real_gws_runner([exe], max_stdout_bytes=1000)

    assert out == "x" * 1000


def test_output_over_the_cap_is_refused_with_a_clear_error(tmp_path: Path) -> None:
    exe = _script(tmp_path, "sys.stdout.write('x' * 200000)")

    with pytest.raises(DirectoryRefreshError, match="exceeded 1000 bytes") as info:
        gws.real_gws_runner([exe], max_stdout_bytes=1000)

    assert "narrow the --since/--until window" in str(info.value)
    assert describe_failure(info.value) == "DirectoryRefreshError[stdout over 1000 bytes]"


def test_a_capped_read_that_runs_past_the_timeout_is_killed(tmp_path: Path) -> None:
    exe = _script(tmp_path, "time.sleep(30)")

    with pytest.raises(DirectoryRefreshError, match="timed out after 0.5s"):
        gws.real_gws_runner([exe], timeout=0.5, max_stdout_bytes=1000)


def test_a_capped_read_reports_a_failing_exit_by_code(tmp_path: Path) -> None:
    exe = _script(tmp_path, "sys.stderr.write('token=SECRET account data'); sys.exit(3)")

    with pytest.raises(DirectoryRefreshError, match=r"exit 3") as info:
        gws.real_gws_runner([exe], max_stdout_bytes=1000)

    assert describe_failure(info.value) == "DirectoryRefreshError[exit 3]"


def test_a_capped_read_that_cannot_start_is_translated(tmp_path: Path) -> None:
    not_executable = tmp_path / "fake-gws"
    not_executable.write_text("not a program", encoding="utf-8")
    not_executable.chmod(0o755)

    with pytest.raises(DirectoryRefreshError, match="gws command failed"):
        gws.real_gws_runner([str(not_executable)], max_stdout_bytes=1000)


def test_the_calendar_runner_passes_its_timeout_and_cap(tmp_path: Path) -> None:
    """``brain ingest-calendar`` reads with 120 s and a 16 MiB cap, not the 30 s default."""
    assert (GWS_TIMEOUT_SECONDS, GWS_MAX_STDOUT_BYTES) == (120, 16 * 1024 * 1024)
    exe = _script(tmp_path, "sys.stdout.write('ok')")

    assert calendar_gws_runner([exe]) == "ok"


def test_the_calendar_runner_refuses_an_oversized_answer(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("brain.cli_ingest_calendar.GWS_MAX_STDOUT_BYTES", 10)
    exe = _script(tmp_path, "sys.stdout.write('x' * 100)")

    with pytest.raises(DirectoryRefreshError, match="exceeded 10 bytes"):
        calendar_gws_runner([exe])


# --- sec-117 F6: process group, stdin, interrupt ---------------------------------


@pytest.mark.timeout(60)
def test_a_child_holding_stdout_is_killed_with_the_group_at_the_timeout(tmp_path: Path) -> None:
    """``proc.kill()`` reached only ``gws``; a helper it started kept stdout open, so the
    read waited for the helper rather than the timeout."""
    exe = _script(
        tmp_path,
        "import subprocess\n"
        "subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(20)'])\n"
        "time.sleep(20)",
    )
    started = time.monotonic()

    with pytest.raises(DirectoryRefreshError, match="timed out after 0.5s"):
        gws.real_gws_runner([exe], timeout=0.5, max_stdout_bytes=1000)

    assert time.monotonic() - started < 10


def test_the_capped_read_gives_gws_no_stdin(tmp_path: Path) -> None:
    exe = _script(
        tmp_path,
        "import os\n"
        "sys.stdout.write(str(os.path.samestat(os.fstat(0), os.stat(os.devnull))))",
    )

    assert gws.real_gws_runner([exe], max_stdout_bytes=1000) == "True"


def test_a_failing_exit_keeps_no_stderr_in_the_message(tmp_path: Path) -> None:
    exe = _script(tmp_path, "sys.stderr.write('token=SECRET account data'); sys.exit(3)")

    with pytest.raises(DirectoryRefreshError) as info:
        gws.real_gws_runner([exe], max_stdout_bytes=1000)

    assert "SECRET" not in str(info.value)
