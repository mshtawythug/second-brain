"""``scripts/verify_release.py`` check 3 follows the launchd label split (phase 8 T2).

Tier A renders each plist ``brain-install-launchd`` writes, in a subprocess under
the repo venv. It imported ``brain.bin.launchd._LABELS``, a name the T2 split
removed, so after T2 the release harness would report every plist as defective
from an ImportError. Tier B kickstarts the live agents, so its label list must
be the installed set: the retired ``com.brain.build`` no longer exists to
restart. Neither test touches launchd: tier A only renders, and tier B is
compared as data, never run.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from types import ModuleType

import pytest

from brain.bin.launchd import _INSTALLED_LABELS

_SCRIPT = Path(__file__).resolve().parent.parent / "scripts" / "verify_release.py"


def _load(monkeypatch: pytest.MonkeyPatch) -> ModuleType:
    spec = importlib.util.spec_from_file_location("verify_release", _SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    monkeypatch.setitem(sys.modules, "verify_release", module)
    spec.loader.exec_module(module)
    return module


def test_tier_a_renders_every_installed_plist_without_defects(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Tier A runs under the repo venv's python; here, the interpreter running the tests.

    ``REPO_ROOT/.venv`` need not exist (CI, a worktree, an archive copy), and the
    question is whether the plists the CURRENT code renders are sound, which
    ``sys.executable`` answers.
    """
    vr = _load(monkeypatch)
    monkeypatch.setattr(vr, "REPO_PY", Path(sys.executable))

    result = vr.check_3("A", allow_restart=False)

    assert result.status == vr.PASS, f"{result.detail} {result.evidence}"


def test_tier_b_restarts_only_the_installed_agents(monkeypatch: pytest.MonkeyPatch) -> None:
    vr = _load(monkeypatch)

    assert vr.LAUNCHD_LABELS == _INSTALLED_LABELS
