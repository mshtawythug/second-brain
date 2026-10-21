"""Phase 8 T5 (E9): ``brain-status`` with the wiki dormant (R2).

``brain-status`` labels a stopped build watcher dormant. (This file's
``brain-up`` tests were deleted with the Quartz wiki at R3, phase 9; brain-up's
R3 behaviour is pinned in ``tests/test_bin_scripts.py``.) Every run is
hermetic: stubbed ``curl`` and tmp pid files. The helpers and fixtures are
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
    _seed_healthy_current,
    _write_stub,
)

# test_bin_scripts.py's fixtures, bound by name so pytest resolves them here too.
stub_dir = bin_scripts.stub_dir
vault_dir = bin_scripts.vault_dir
hermetic_pids = bin_scripts.hermetic_pids

_REBUILD = "brain-rebuild --only quartz"


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
