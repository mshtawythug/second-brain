"""Phase 8 T5 (E9, R8-6): ``brain-up`` and ``brain-status`` with the wiki dormant (R2).

``brain-up`` runs the Quartz overlay and the cold-start build only when
``BRAIN_WIKI_BUILD=1``; its banner says the wiki is dormant and names
``brain-rebuild --only quartz``. ``brain-status`` labels a stopped build watcher
dormant. Every run is hermetic: ``BRAIN_NO_BUILD_WATCHER=1`` (no launchd), stubbed
``brain`` / ``python`` / ``curl``, and tmp pid files. The helpers and fixtures are
``tests/test_bin_scripts.py``'s, so the launchctl stand-in its ``stub_dir``
seeds applies here too.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import tests.test_bin_scripts as bin_scripts
from tests.test_bin_scripts import (
    BIN,
    HermeticPids,
    _make_env,
    _read_log,
    _seed_healthy_current,
    _write_stub,
)

# test_bin_scripts.py's fixtures, bound by name so pytest resolves them here too.
stub_dir = bin_scripts.stub_dir
vault_dir = bin_scripts.vault_dir
hermetic_pids = bin_scripts.hermetic_pids

_REBUILD = "brain-rebuild --only quartz"


def _brain_up(
    stub_dir: Path, vault_dir: Path, pids: HermeticPids, *, wiki_build: str | None
) -> subprocess.CompletedProcess[str]:
    """``brain-up`` with the overlay ENABLED (no ``BRAIN_NO_OVERLAY``) and no ``current/``."""
    for name in ("brain", "python", "curl"):
        _write_stub(stub_dir, name)
    env = _make_env(
        stub_dir=stub_dir,
        vault_dir=vault_dir,
        extras={**pids.env, "BRAIN_NO_BUILD_WATCHER": "1"},
    )
    if wiki_build is not None:
        env["BRAIN_WIKI_BUILD"] = wiki_build
    return subprocess.run(  # noqa: S603 — list-form, no shell
        [str(BIN / "brain-up")],
        env=env,
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )


def test_brain_up_runs_no_overlay_and_no_quartz_build_by_default(
    stub_dir: Path, vault_dir: Path, hermetic_pids: HermeticPids
) -> None:
    """E9: with ``current/`` missing, the old cold start would build. At R2 it does not."""
    result = _brain_up(stub_dir, vault_dir, hermetic_pids, wiki_build=None)

    assert result.returncode == 0, result.stderr
    brain_calls = _read_log(stub_dir / "brain.calls")
    python_calls = _read_log(stub_dir / "python.calls")
    assert not [c for c in brain_calls if "render" in c], brain_calls
    assert not [c for c in python_calls if "build_swap" in c], python_calls
    assert "dormant" in result.stdout and _REBUILD in result.stdout, result.stdout


def test_brain_wiki_build_1_restores_the_overlay_and_the_cold_start(
    stub_dir: Path, vault_dir: Path, hermetic_pids: HermeticPids
) -> None:
    """E9 / R8-6: the build is reversible on request, not deleted."""
    result = _brain_up(stub_dir, vault_dir, hermetic_pids, wiki_build="1")

    brain_calls = _read_log(stub_dir / "brain.calls")
    python_calls = _read_log(stub_dir / "python.calls")
    assert any("vault render --overlay --no-build" in c for c in brain_calls), brain_calls
    assert len([c for c in python_calls if "brain.wiki.build_swap" in c]) == 1, python_calls
    assert "running first-time build" in result.stdout, result.stdout


def test_brain_up_banner_says_the_wiki_is_dormant(
    stub_dir: Path, vault_dir: Path, hermetic_pids: HermeticPids
) -> None:
    """E9: no banner line claims the wiki is built or re-rendered any more."""
    _seed_healthy_current(vault_dir)

    result = _brain_up(stub_dir, vault_dir, hermetic_pids, wiki_build=None)

    lines = {
        line.strip().split(":", 1)[0]: line
        for line in result.stdout.splitlines()
        if line.startswith("   ") and ":" in line
    }
    assert {"wiki", "build"} <= lines.keys(), result.stdout
    assert "dormant" in lines["wiki"] and "legacy" in lines["wiki"], result.stdout
    assert "dormant" in lines["build"] and _REBUILD in lines["build"], result.stdout
    assert "re-rendering" not in result.stdout, result.stdout
    assert "still built" not in result.stdout, result.stdout


def test_brain_status_labels_a_stopped_build_watcher_dormant(
    stub_dir: Path, vault_dir: Path, hermetic_pids: HermeticPids
) -> None:
    """E9: "stopped" read as an outage; at R2 it is the expected state."""
    _seed_healthy_current(vault_dir)
    _write_stub(stub_dir, "curl")
    env = {**os.environ, **hermetic_pids.env, "BRAIN_VAULT_PATH": str(vault_dir)}
    env["PATH"] = f"{stub_dir}:{env['PATH']}"

    result = subprocess.run(  # noqa: S603 — list-form, no shell
        [str(BIN / "brain-status")],
        env=env,
        capture_output=True,
        text=True,
        timeout=10,
        check=False,
    )

    (build,) = [line for line in result.stdout.splitlines() if line.strip().startswith("build:")]
    assert "dormant" in build and _REBUILD in build, result.stdout
    assert "stopped" not in build, result.stdout
