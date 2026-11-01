"""The ``http`` extra pins mcp 2.x without moving a plain install (RM-46, C2).

``brain mcp serve`` needs the mcp 2.x server API; stdio still runs on 1.x
(``brain.mcp_compat``). So the base pin stays wide and the extra narrows it.
Checked offline against ``pyproject.toml`` with ``packaging``: no resolver,
no network. CI's ``pip install -e ".[dev,http]"`` is the real resolve.
"""
from __future__ import annotations

import tomllib
from pathlib import Path

import pytest
from packaging.requirements import Requirement
from packaging.specifiers import SpecifierSet

pytestmark = pytest.mark.nodb

_PYPROJECT = Path(__file__).resolve().parent.parent / "pyproject.toml"


def _project() -> dict[str, object]:
    with _PYPROJECT.open("rb") as handle:
        data = tomllib.load(handle)
    project = data["project"]
    assert isinstance(project, dict)
    return project


def _mcp_spec(requirements: list[str]) -> SpecifierSet:
    found = [Requirement(line) for line in requirements if Requirement(line).name == "mcp"]
    assert len(found) == 1, requirements
    return found[0].specifier


def test_the_package_is_published_as_secondbrain_py() -> None:
    # The runtime refusal names `pip install 'secondbrain-py[http]'`.
    assert _project()["name"] == "secondbrain-py"


def test_http_extra_intersects_base_pin() -> None:
    project = _project()
    base = _mcp_spec(list(project["dependencies"]))  # type: ignore[call-overload]
    extras = project["optional-dependencies"]
    assert isinstance(extras, dict)
    http = _mcp_spec(list(extras["http"]))

    combined = base & http
    assert combined.contains("2.0.0")
    assert combined.contains("2.3.0")
    assert not combined.contains("1.29.0")
    assert not combined.contains("3.0.0")


def test_a_plain_install_still_admits_mcp_1x() -> None:
    base = _mcp_spec(list(_project()["dependencies"]))  # type: ignore[call-overload]
    assert base.contains("1.2.0")
    assert base.contains("1.29.0")
    assert base.contains("2.3.0")
