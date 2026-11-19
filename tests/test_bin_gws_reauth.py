"""F20: ``bin/gws-reauth`` handles argv before it deletes the gws token cache.

Its first action is ``rm -f ~/.config/gws/token_cache.json``, so ``--help`` (or
any token it does not understand) used to delete the cache before anything
looked at the arguments. Every run here uses ``HOME=tmp_path`` with a DECOY
cache and a fake ``gws`` on a tmp PATH that records its argv; the real ``gws``
is never reachable (PATH is the stub dir plus ``/usr/bin:/bin`` only).
"""

from __future__ import annotations

import subprocess
from pathlib import Path
from typing import NamedTuple

import pytest

pytestmark = pytest.mark.nodb

SCRIPT = Path(__file__).resolve().parents[1] / "bin" / "gws-reauth"


class Reauth(NamedTuple):
    """The env for one run, the decoy cache it must protect, and the gws call log."""

    env: dict[str, str]
    cache: Path
    gws_log: Path


@pytest.fixture
def reauth(tmp_path: Path) -> Reauth:
    cache = tmp_path / ".config" / "gws" / "token_cache.json"
    cache.parent.mkdir(parents=True)
    cache.write_text('{"synthetic": "decoy"}\n')
    stubs = tmp_path / "stubs"
    stubs.mkdir()
    gws_log = stubs / "gws.calls"
    gws = stubs / "gws"
    # One argv token per line, so an empty or spaced token stays visible.
    gws.write_text(f'#!/bin/sh\nfor a in "$@"; do echo "$a"; done > "{gws_log}"\nexit 0\n')
    gws.chmod(0o755)
    env = {"PATH": f"{stubs}:/usr/bin:/bin", "HOME": str(tmp_path)}
    return Reauth(env=env, cache=cache, gws_log=gws_log)


def _run(reauth: Reauth, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(  # noqa: S603 — list-form, no shell
        [str(SCRIPT), *args],
        env=reauth.env,
        capture_output=True,
        text=True,
        timeout=15,
        check=False,
    )


@pytest.mark.parametrize("flag", ["-h", "--help"])
def test_help_prints_usage_and_deletes_nothing(reauth: Reauth, flag: str) -> None:
    result = _run(reauth, flag)

    assert result.returncode == 0, result.stderr
    assert "usage: gws-reauth" in result.stdout.lower()
    assert reauth.cache.exists(), "--help deleted the gws token cache"
    assert not reauth.gws_log.exists(), "--help ran gws"


@pytest.mark.parametrize(
    "args", [["--bogus"], ["help"], ["", "x"]], ids=["flag", "word", "empty-then-arg"]
)
def test_unknown_arguments_exit_2_and_delete_nothing(reauth: Reauth, args: list[str]) -> None:
    result = _run(reauth, *args)

    assert result.returncode == 2, (result.stdout, result.stderr)
    assert "error: unknown argument" in result.stderr
    assert reauth.cache.exists(), "an unknown argument deleted the gws token cache"
    assert not reauth.gws_log.exists(), "an unknown argument ran gws"


def _gws_argv(reauth: Reauth) -> list[str]:
    return reauth.gws_log.read_text().splitlines()


def test_no_arguments_clears_the_cache_and_logs_in(reauth: Reauth) -> None:
    result = _run(reauth)

    assert result.returncode == 0, result.stderr
    assert not reauth.cache.exists()
    argv = _gws_argv(reauth)
    assert argv[:3] == ["auth", "login", "--scopes"]
    assert len(argv) == 4
    assert "https://www.googleapis.com/auth/contacts.other.readonly" in argv[3].split(",")


def test_arguments_after_double_dash_go_to_gws_auth_login(reauth: Reauth) -> None:
    result = _run(reauth, "--", "--account", "someone@example.test")

    assert result.returncode == 0, result.stderr
    assert not reauth.cache.exists()
    argv = _gws_argv(reauth)
    assert argv[:3] == ["auth", "login", "--scopes"]
    assert argv[4:] == ["--account", "someone@example.test"], argv
