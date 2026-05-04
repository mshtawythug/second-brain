"""Tests for the gzipped-size budget enforcer (``scripts/check_index_size.py``).

The slim transform in P3.1 brings ``contentIndex.json`` under 2 MB
gzipped. The script ``scripts/check_index_size.py`` is the regression
guard: any future change that bloats the index past 2 MB gzipped trips
this check.

These tests drive the script's ``main()`` entrypoint directly (so we
exercise the same arg-parsing + return-code paths the CLI does) against
synthetic fixtures: one tiny payload that should pass, one large
already-compressed-blob payload that should fail. Using direct calls
avoids a subprocess dependency and gives mypy / coverage visibility.
"""
from __future__ import annotations

import gzip
import importlib.util
import json
import os
import sys
from pathlib import Path
from types import ModuleType

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPT_PATH = REPO_ROOT / "scripts" / "check_index_size.py"


def _load_check_script() -> ModuleType:
    """Import ``scripts/check_index_size.py`` as a module.

    ``scripts/`` is not a Python package on the path; load via
    ``importlib`` so the test doesn't depend on the script being on
    ``PYTHONPATH``. Cached in ``sys.modules`` under a stable name so
    repeated calls don't re-import.
    """
    name = "_brain_check_index_size_under_test"
    if name in sys.modules:
        return sys.modules[name]
    spec = importlib.util.spec_from_file_location(name, SCRIPT_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def check_module() -> ModuleType:
    return _load_check_script()


def test_script_file_exists() -> None:
    """Sanity: the script lives where the rest of the project expects."""
    assert SCRIPT_PATH.is_file(), f"missing script at {SCRIPT_PATH}"


def test_default_budget_is_two_mib(check_module: ModuleType) -> None:
    """Budget pinned at 2 MiB matches the plan's wording."""
    assert check_module.DEFAULT_BUDGET_BYTES == 2 * 1024 * 1024


def test_under_budget_returns_zero(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    check_module: ModuleType,
) -> None:
    """A small synthetic JSON file passes the size check."""
    # Setup — a few short strings gzip-compress easily under 2 MB.
    target = tmp_path / "contentIndex.json"
    target.write_text(
        json.dumps({f"slug-{i}": {"title": f"doc {i}"} for i in range(10)}),
        encoding="utf-8",
    )

    # Exercise
    rc = check_module.main([str(target)])

    # Verify
    assert rc == 0
    captured = capsys.readouterr()
    assert "OK" in captured.out
    assert str(target) in captured.out


def test_over_budget_returns_one(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    check_module: ModuleType,
) -> None:
    """A bloated index over the budget exits 1 with a FAIL message.

    Random-bytes are used as the payload because they don't compress —
    so we can hit the budget cap with a deterministic-size raw input.
    Random data of ~3 MB compresses to ~3 MB gzipped (gzip can't shrink
    incompressible data), reliably crossing the 2 MB threshold.
    """
    # Setup
    target = tmp_path / "contentIndex.json"
    payload = os.urandom(3 * 1024 * 1024)  # 3 MB random — incompressible
    target.write_bytes(payload)
    # Sanity — confirm gzipped size really does exceed budget.
    assert len(gzip.compress(payload)) > check_module.DEFAULT_BUDGET_BYTES

    # Exercise
    rc = check_module.main([str(target)])

    # Verify
    assert rc == 1
    captured = capsys.readouterr()
    assert "FAIL" in captured.err
    assert str(target) in captured.err


def test_missing_file_returns_two(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    check_module: ModuleType,
) -> None:
    """A nonexistent path exits 2 with a clear error message."""
    # Setup — point at a file that doesn't exist.
    target = tmp_path / "does-not-exist.json"

    # Exercise
    rc = check_module.main([str(target)])

    # Verify
    assert rc == 2
    captured = capsys.readouterr()
    assert "not found" in captured.err
    assert str(target) in captured.err


def test_custom_budget_flag_tightens_limit(
    tmp_path: Path,
    check_module: ModuleType,
) -> None:
    """``--budget-bytes`` overrides the default budget.

    Drives the same payload through two invocations: one with a tiny
    budget that should fail, one with a generous budget that should
    pass. Confirms the flag is wired and not just decoration.
    """
    # Setup
    target = tmp_path / "contentIndex.json"
    target.write_text("x" * 5_000, encoding="utf-8")

    # Exercise + Verify — tight budget fails.
    rc_tight = check_module.main([str(target), "--budget-bytes", "10"])
    assert rc_tight == 1

    # Exercise + Verify — generous budget passes.
    rc_loose = check_module.main([str(target), "--budget-bytes", str(10 * 1024 * 1024)])
    assert rc_loose == 0


def test_gzipped_size_helper_matches_gzip_module(check_module: ModuleType) -> None:
    """``gzipped_size`` agrees with the stdlib ``gzip.compress`` baseline.

    Cheap parity check — guards against the helper drifting (e.g. someone
    refactoring to a streaming API and accidentally double-counting the
    header).
    """
    payload = b"hello world" * 100
    assert check_module.gzipped_size(payload) == len(gzip.compress(payload))
