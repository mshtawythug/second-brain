"""Static checks for dev dependency pins that protect local verification."""
from __future__ import annotations

import tomllib
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
PYPROJECT = REPO_ROOT / "pyproject.toml"


def test_coverage_is_pinned_below_pgvector_breaking_range() -> None:
    """Keep pytest's default coverage path away from coverage 7.13+."""
    data = tomllib.loads(PYPROJECT.read_text(encoding="utf-8"))
    dev_deps = data["project"]["optional-dependencies"]["dev"]

    assert "coverage<7.13" in dev_deps
