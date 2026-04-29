"""Tests for the production ``gws`` subprocess runner.

Covers the GwsRunner-Protocol contract: stdout pass-through on success,
``DirectoryRefreshError`` translation on every documented subprocess
failure (missing binary, non-zero exit, timeout, generic OSError), and
arg pass-through to ``subprocess.run``.

Uses ``mocker.patch`` (pytest-mock) for ``shutil.which`` /
``subprocess.run`` — these are standard test doubles with auto-cleanup,
not banned monkey-patching of production internals.
"""
import subprocess

import pytest
from pytest_mock import MockerFixture

from brain.errors import DirectoryRefreshError
from brain.vault.derived_links.gws import real_gws_runner


def _completed(stdout: str = "", stderr: str = "") -> subprocess.CompletedProcess[str]:
    """Build a ``CompletedProcess`` with the given stdout/stderr."""
    return subprocess.CompletedProcess(
        args=["gws"], returncode=0, stdout=stdout, stderr=stderr
    )


def test_real_gws_runner_returns_stdout(mocker: MockerFixture) -> None:
    """Happy path: ``subprocess.run`` succeeds → stdout returned verbatim."""
    mocker.patch(
        "brain.vault.derived_links.gws.shutil.which", return_value="/usr/bin/gws"
    )
    mocker.patch(
        "brain.vault.derived_links.gws.subprocess.run",
        return_value=_completed(stdout='[{"id": "evt1"}]'),
    )
    out = real_gws_runner(["gws", "calendar", "list-events"])
    assert out == '[{"id": "evt1"}]'


def test_real_gws_runner_raises_on_missing_binary(mocker: MockerFixture) -> None:
    """``shutil.which`` returning ``None`` → ``DirectoryRefreshError``."""
    mocker.patch(
        "brain.vault.derived_links.gws.shutil.which", return_value=None
    )
    with pytest.raises(DirectoryRefreshError, match="not found on PATH"):
        real_gws_runner(["gws", "calendar", "list-events"])


def test_real_gws_runner_raises_on_empty_args() -> None:
    """Defensive: empty args list raises before any subprocess call."""
    with pytest.raises(DirectoryRefreshError, match="empty args"):
        real_gws_runner([])


def test_real_gws_runner_translates_called_process_error(
    mocker: MockerFixture,
) -> None:
    """Non-zero exit → ``DirectoryRefreshError`` with exit code + stderr snippet."""
    mocker.patch(
        "brain.vault.derived_links.gws.shutil.which", return_value="/usr/bin/gws"
    )
    mocker.patch(
        "brain.vault.derived_links.gws.subprocess.run",
        side_effect=subprocess.CalledProcessError(
            returncode=42,
            cmd=["gws", "calendar", "list-events"],
            output="",
            stderr="permission denied: token expired",
        ),
    )
    with pytest.raises(DirectoryRefreshError) as exc:
        real_gws_runner(["gws", "calendar", "list-events"])
    assert "exit 42" in str(exc.value)
    assert "permission denied" in str(exc.value)


def test_real_gws_runner_translates_timeout(mocker: MockerFixture) -> None:
    """Timeout → ``DirectoryRefreshError`` mentioning the timeout duration."""
    mocker.patch(
        "brain.vault.derived_links.gws.shutil.which", return_value="/usr/bin/gws"
    )
    mocker.patch(
        "brain.vault.derived_links.gws.subprocess.run",
        side_effect=subprocess.TimeoutExpired(
            cmd=["gws", "calendar", "list-events"], timeout=30
        ),
    )
    with pytest.raises(DirectoryRefreshError) as exc:
        real_gws_runner(["gws", "calendar", "list-events"])
    assert "timed out" in str(exc.value)
    assert "30s" in str(exc.value)


def test_real_gws_runner_translates_oserror(mocker: MockerFixture) -> None:
    """Generic ``OSError`` (e.g. EMFILE) → ``DirectoryRefreshError``."""
    mocker.patch(
        "brain.vault.derived_links.gws.shutil.which", return_value="/usr/bin/gws"
    )
    mocker.patch(
        "brain.vault.derived_links.gws.subprocess.run",
        side_effect=OSError("Too many open files"),
    )
    with pytest.raises(DirectoryRefreshError, match="Too many open files"):
        real_gws_runner(["gws", "calendar", "list-events"])


def test_real_gws_runner_includes_stderr_snippet(mocker: MockerFixture) -> None:
    """Long stderr is truncated to ~200 chars in the translated error message."""
    mocker.patch(
        "brain.vault.derived_links.gws.shutil.which", return_value="/usr/bin/gws"
    )
    long_err = "x" * 5000
    mocker.patch(
        "brain.vault.derived_links.gws.subprocess.run",
        side_effect=subprocess.CalledProcessError(
            returncode=1, cmd=["gws"], output="", stderr=long_err
        ),
    )
    with pytest.raises(DirectoryRefreshError) as exc:
        real_gws_runner(["gws", "calendar", "list-events"])
    msg = str(exc.value)
    # Truncated — message stays well under the raw stderr length, but
    # still contains the truncated payload.
    assert len(msg) < 5000
    assert "xxx" in msg


def test_real_gws_runner_passes_args_through(mocker: MockerFixture) -> None:
    """Args list is forwarded to ``subprocess.run`` exactly as supplied."""
    mocker.patch(
        "brain.vault.derived_links.gws.shutil.which", return_value="/usr/bin/gws"
    )
    run_mock = mocker.patch(
        "brain.vault.derived_links.gws.subprocess.run",
        return_value=_completed(stdout="[]"),
    )
    args = [
        "gws",
        "calendar",
        "list-events",
        "--time-min",
        "2026-01-01T00:00:00+00:00",
        "--time-max",
        "2026-04-29T12:00:00+00:00",
        "--format",
        "json",
    ]
    real_gws_runner(args)
    assert run_mock.call_count == 1
    # First positional arg to subprocess.run is the cmd list.
    called_args = run_mock.call_args
    assert called_args.args[0] == args
    # Required safety knobs from CLAUDE.md: explicit timeout, capture, text.
    assert called_args.kwargs["capture_output"] is True
    assert called_args.kwargs["text"] is True
    assert called_args.kwargs["check"] is True
    assert called_args.kwargs["timeout"] == 30


def test_real_gws_runner_handles_empty_stderr_on_failure(
    mocker: MockerFixture,
) -> None:
    """``CalledProcessError`` with no stderr produces a clean message (no None)."""
    mocker.patch(
        "brain.vault.derived_links.gws.shutil.which", return_value="/usr/bin/gws"
    )
    mocker.patch(
        "brain.vault.derived_links.gws.subprocess.run",
        side_effect=subprocess.CalledProcessError(
            returncode=2, cmd=["gws"], output=None, stderr=None
        ),
    )
    with pytest.raises(DirectoryRefreshError) as exc:
        real_gws_runner(["gws", "calendar", "list-events"])
    # Doesn't contain the literal word "None" — empty stderr collapses cleanly.
    assert "None" not in str(exc.value)
    assert "exit 2" in str(exc.value)
