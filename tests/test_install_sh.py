"""Tests for install.sh (T4.1).

All tests that invoke the script pass a sandbox PATH (/usr/bin:/bin) so no
real pipx, brew, or python3 binary on the developer's machine is invoked
accidentally.  The BRAIN_INSTALL_SH_DRY_RUN=1 env var causes the script to
short-circuit all real subprocess calls and exit 0.
"""
from __future__ import annotations

import os
import stat
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).parent.parent
INSTALL_SH = REPO_ROOT / "install.sh"

# Minimal PATH that provides bash built-ins + uname/sed/wc but NOT pipx/brew.
SANDBOX_PATH = "/usr/bin:/bin"


def _run(
    env_overrides: dict[str, str] | None = None,
    *,
    extra_args: list[str] | None = None,
) -> subprocess.CompletedProcess[str]:
    """Run install.sh with a sandboxed PATH and capture stdout+stderr."""
    env = {
        "PATH": SANDBOX_PATH,
        "HOME": os.environ.get("HOME", "/tmp"),
        # Prevent the script from trying to reach the network.
        "BRAIN_INSTALL_REF": "v0.2.0",
        **(env_overrides or {}),
    }
    cmd = ["bash", str(INSTALL_SH)] + (extra_args or [])
    return subprocess.run(
        cmd,
        env=env,
        capture_output=True,
        text=True,
    )


# ---------------------------------------------------------------------------
# Test 1 — file exists and is executable
# ---------------------------------------------------------------------------


def test_install_sh_exists_and_executable() -> None:
    assert INSTALL_SH.exists(), f"install.sh not found at {INSTALL_SH}"
    mode = INSTALL_SH.stat().st_mode
    assert mode & stat.S_IXUSR, "install.sh must be user-executable (+x)"


# ---------------------------------------------------------------------------
# Test 2 — refuses Windows (OSTYPE=cygwin / msys / mingw)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("ostype", ["cygwin", "msys", "mingw32"])
def test_install_sh_refuses_windows(ostype: str) -> None:
    result = _run({"OSTYPE": ostype})
    combined = result.stdout + result.stderr
    assert result.returncode != 0, (
        f"install.sh must exit non-zero for OSTYPE={ostype!r}, got 0"
    )
    assert "Windows" in combined, (
        f"stderr must mention 'Windows' for OSTYPE={ostype!r}:\n{combined}"
    )
    assert "WSL" in combined, (
        f"stderr must mention 'WSL' for OSTYPE={ostype!r}:\n{combined}"
    )


# ---------------------------------------------------------------------------
# Test 3 — refuses branch ref without BRAIN_INSECURE=1
# ---------------------------------------------------------------------------


def test_install_sh_refuses_branch_without_insecure() -> None:
    result = _run(
        {
            "BRAIN_INSTALL_REF": "feat/test",
            # Ensure BRAIN_INSECURE is absent / not set to 1.
            "BRAIN_INSECURE": "0",
            "OSTYPE": "darwin21.0",
            "BRAIN_INSTALL_SH_DRY_RUN": "1",
        }
    )
    assert result.returncode != 0, (
        "install.sh must exit non-zero for a non-tag ref without BRAIN_INSECURE=1"
    )
    combined = result.stdout + result.stderr
    assert "BRAIN_INSECURE" in combined, (
        f"output must mention BRAIN_INSECURE:\n{combined}"
    )


# ---------------------------------------------------------------------------
# Test 4 — accepts tag ref with dry-run (short-circuit)
# ---------------------------------------------------------------------------


def test_install_sh_accepts_tag_ref() -> None:
    result = _run(
        {
            "BRAIN_INSTALL_REF": "v0.2.0",
            "OSTYPE": "darwin21.0",
            "BRAIN_INSTALL_SH_DRY_RUN": "1",
        }
    )
    combined = result.stdout + result.stderr
    assert result.returncode == 0, (
        f"install.sh must exit 0 for a tag ref in dry-run mode:\n{combined}"
    )


# ---------------------------------------------------------------------------
# Test 5 — script is under 100 lines (readability constraint)
# ---------------------------------------------------------------------------


def test_install_sh_under_100_lines() -> None:
    lines = INSTALL_SH.read_text().splitlines()
    assert len(lines) < 100, (
        f"install.sh must be under 100 lines (currently {len(lines)})"
    )
