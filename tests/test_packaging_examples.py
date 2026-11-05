"""RM-53: the `brain examples` resource's package-data declaration.

These read the source tree. The check that opens a real wheel is
``scripts/check_wheel_static.py``, in CI's ``lint`` job and in ``bin/brain-ci``.
"""
from __future__ import annotations

import importlib.resources
import tomllib
from pathlib import Path

import pytest

pytestmark = pytest.mark.nodb

REPO_ROOT = Path(__file__).resolve().parent.parent


def _package_data() -> dict[str, dict[str, list[str]]]:
    with (REPO_ROOT / "pyproject.toml").open("rb") as handle:
        return tomllib.load(handle)["tool"]["setuptools"]


def test_the_resource_has_its_own_narrow_glob() -> None:
    assert _package_data()["package-data"]["brain.resources"] == ["*.md"]


def test_the_resource_package_keeps_bytecode_out_of_the_wheel() -> None:
    assert _package_data()["exclude-package-data"]["brain.resources"] == [
        "__pycache__/*",
        "*.pyc",
    ]


def test_the_resource_resolves_through_importlib_resources() -> None:
    resource = importlib.resources.files("brain.resources") / "examples.md"
    assert resource.read_text(encoding="utf-8").startswith("# brain examples\n")
