"""Tests for the ``brain doctor`` Quartz/npx soft check.

The npx check is informational — it tells the user whether
``brain vault render`` will work without actually running any Quartz
machinery. We never invoke real ``npx`` from a test (Quartz is a heavy
Node dependency that the user opts into; CI shouldn't depend on it).
Every test patches ``shutil.which`` and ``subprocess.run`` so the
brain-side logic is exercised in isolation.

The hard rule for this check: doctor exit code stays 0 regardless of
what we tell it about npx. Quartz is optional; only Postgres + the
embedder backend can fail doctor.
"""
from __future__ import annotations

import contextlib
import os
import subprocess
from collections.abc import Iterator
from typing import Any
from unittest.mock import patch

import httpx
import pytest
from typer.testing import CliRunner

from brain.cli import app

TEST_DATABASE_URL = os.environ.get(
    "TEST_DATABASE_URL",
    "postgresql://brain:brain@localhost:5433/second_brain_test",
)


def _ok_ollama_transport() -> httpx.MockTransport:
    """Mock transport returning the default-backend model loaded.

    Reused from ``test_cli_doctor.py`` shape so this file stays
    self-contained — we don't import private helpers across test
    modules.
    """

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "models": [
                    {"name": "snowflake-arctic-embed2"},
                    {"name": "qwen3-embedding:8b"},
                ]
            },
        )

    return httpx.MockTransport(handler)


@contextlib.contextmanager
def _patch_httpx_client(transport: httpx.MockTransport) -> Iterator[None]:
    real_client = httpx.Client

    def factory(*args: Any, **kwargs: Any) -> httpx.Client:
        kwargs["transport"] = transport
        return real_client(*args, **kwargs)

    with patch("brain.cli.httpx.Client", side_effect=factory):
        yield


def _completed(
    returncode: int = 0, stdout: str = "10.2.0\n"
) -> subprocess.CompletedProcess[str]:
    return subprocess.CompletedProcess(
        args=["npx", "--version"],
        returncode=returncode,
        stdout=stdout,
        stderr="",
    )


def _which_factory(npx_path: str | None, gws_path: str | None = "/usr/local/bin/gws") -> Any:
    """Build a ``shutil.which`` replacement.

    Doctor calls ``shutil.which`` for both ``gws`` (for Gmail ingestion)
    and ``npx`` (for Quartz). We want to control them independently in
    each test, so the returned callable dispatches on the requested
    binary name.
    """

    def _which(binary: str) -> str | None:
        if binary == "npx":
            return npx_path
        if binary == "gws":
            return gws_path
        return None

    return _which


# ---------------------------------------------------------------------------
# npx present
# ---------------------------------------------------------------------------


def test_doctor_reports_npx_ok(
    monkeypatch: pytest.MonkeyPatch, mocker: Any
) -> None:
    """``shutil.which("npx")`` returns a path + ``npx --version`` succeeds.

    The line should mention OK, the version, the path, and that
    ``brain vault render`` is available.
    """
    monkeypatch.setenv("DATABASE_URL", TEST_DATABASE_URL)
    mocker.patch(
        "brain.cli.shutil.which",
        side_effect=_which_factory(npx_path="/usr/local/bin/npx"),
    )
    mocker.patch(
        "brain.cli.subprocess.run",
        return_value=_completed(stdout="10.2.0\n"),
    )

    with _patch_httpx_client(_ok_ollama_transport()):
        result = CliRunner().invoke(app, ["doctor"])
    assert result.exit_code == 0, result.output
    assert "quartz/npx" in result.output
    assert "OK" in result.output
    assert "10.2.0" in result.output
    assert "/usr/local/bin/npx" in result.output
    assert "brain vault render" in result.output


# ---------------------------------------------------------------------------
# npx absent (the soft-warn path)
# ---------------------------------------------------------------------------


def test_doctor_reports_npx_not_installed(
    monkeypatch: pytest.MonkeyPatch, mocker: Any
) -> None:
    """``shutil.which("npx")`` returns None → warning, exit 0 (soft check)."""
    monkeypatch.setenv("DATABASE_URL", TEST_DATABASE_URL)
    mocker.patch(
        "brain.cli.shutil.which", side_effect=_which_factory(npx_path=None)
    )
    # subprocess.run should NEVER be called when which returns None.
    run_mock = mocker.patch("brain.cli.subprocess.run")

    with _patch_httpx_client(_ok_ollama_transport()):
        result = CliRunner().invoke(app, ["doctor"])
    assert result.exit_code == 0, result.output
    assert "quartz/npx" in result.output
    assert "not installed" in result.output
    assert "Node.js" in result.output
    assert run_mock.call_count == 0


def test_doctor_npx_missing_does_not_fail_doctor(
    monkeypatch: pytest.MonkeyPatch, mocker: Any
) -> None:
    """Crucial: missing npx is a soft check, not a failure.

    Even if every other doctor check passes, missing npx must not flip
    doctor's exit code. Quartz is optional.
    """
    monkeypatch.setenv("DATABASE_URL", TEST_DATABASE_URL)
    mocker.patch(
        "brain.cli.shutil.which", side_effect=_which_factory(npx_path=None)
    )
    mocker.patch("brain.cli.subprocess.run")

    with _patch_httpx_client(_ok_ollama_transport()):
        result = CliRunner().invoke(app, ["doctor"])
    assert result.exit_code == 0, result.output


# ---------------------------------------------------------------------------
# npx probe failures (timeout, non-zero exit, OSError)
# ---------------------------------------------------------------------------


def test_doctor_npx_timeout_treated_as_not_installed(
    monkeypatch: pytest.MonkeyPatch, mocker: Any
) -> None:
    """``subprocess.TimeoutExpired`` from ``npx --version`` → soft warn."""
    monkeypatch.setenv("DATABASE_URL", TEST_DATABASE_URL)
    mocker.patch(
        "brain.cli.shutil.which",
        side_effect=_which_factory(npx_path="/usr/local/bin/npx"),
    )
    mocker.patch(
        "brain.cli.subprocess.run",
        side_effect=subprocess.TimeoutExpired(cmd=["npx", "--version"], timeout=5),
    )

    with _patch_httpx_client(_ok_ollama_transport()):
        result = CliRunner().invoke(app, ["doctor"])
    assert result.exit_code == 0, result.output
    assert "quartz/npx" in result.output
    assert "not installed" in result.output


def test_doctor_npx_nonzero_treated_as_not_installed(
    monkeypatch: pytest.MonkeyPatch, mocker: Any
) -> None:
    """A non-zero exit from ``npx --version`` (corrupt install) → soft warn."""
    monkeypatch.setenv("DATABASE_URL", TEST_DATABASE_URL)
    mocker.patch(
        "brain.cli.shutil.which",
        side_effect=_which_factory(npx_path="/usr/local/bin/npx"),
    )
    mocker.patch(
        "brain.cli.subprocess.run",
        return_value=_completed(returncode=1, stdout=""),
    )

    with _patch_httpx_client(_ok_ollama_transport()):
        result = CliRunner().invoke(app, ["doctor"])
    assert result.exit_code == 0, result.output
    assert "quartz/npx" in result.output
    assert "not installed" in result.output


def test_doctor_npx_oserror_treated_as_not_installed(
    monkeypatch: pytest.MonkeyPatch, mocker: Any
) -> None:
    """OSError (permission denied, etc.) from subprocess → soft warn."""
    monkeypatch.setenv("DATABASE_URL", TEST_DATABASE_URL)
    mocker.patch(
        "brain.cli.shutil.which",
        side_effect=_which_factory(npx_path="/usr/local/bin/npx"),
    )
    mocker.patch(
        "brain.cli.subprocess.run",
        side_effect=OSError("permission denied"),
    )

    with _patch_httpx_client(_ok_ollama_transport()):
        result = CliRunner().invoke(app, ["doctor"])
    assert result.exit_code == 0, result.output
    assert "quartz/npx" in result.output
    assert "not installed" in result.output


def test_doctor_npx_empty_version_renders_question_mark(
    monkeypatch: pytest.MonkeyPatch, mocker: Any
) -> None:
    """If ``npx --version`` exits 0 but prints nothing, fall back to ``?``.

    Defensive — every shipped npx prints a version, but a corrupt
    install or a sandboxed environment could swallow stdout. The line
    still says OK (we don't lie about availability), just with a
    placeholder version.
    """
    monkeypatch.setenv("DATABASE_URL", TEST_DATABASE_URL)
    mocker.patch(
        "brain.cli.shutil.which",
        side_effect=_which_factory(npx_path="/usr/local/bin/npx"),
    )
    mocker.patch(
        "brain.cli.subprocess.run",
        return_value=_completed(returncode=0, stdout=""),
    )

    with _patch_httpx_client(_ok_ollama_transport()):
        result = CliRunner().invoke(app, ["doctor"])
    assert result.exit_code == 0, result.output
    assert "quartz/npx" in result.output
    assert "OK" in result.output
    assert "npx ?" in result.output
