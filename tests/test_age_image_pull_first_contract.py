"""Contract: CI workflows pull the AGE test image first and build it only as a fallback.

``docker-compose.age-test.yml`` has both ``image:`` (the prebuilt multi-arch image
on GHCR) and ``build:`` (the canonical Dockerfile). A workflow that runs
``up -d --build`` compiles Apache AGE from C source on every run and never uses
the published image. A workflow that pulls without ``|| true`` fails the job
when the registry misses, even though the ``build:`` fallback would have worked.
This module pins the shape ``ci.yml`` already uses, for every workflow that
names the compose file:

1. a ``docker compose -f docker-compose.age-test.yml pull ... || true`` step;
2. then an ``up -d`` step with no ``--build``. With no local image, Compose's
   default ``pull_policy`` (``missing``) builds from ``build:``, which is the
   fallback.

The pulled ref and the built ref are the same string because both come from
the compose ``image:`` line. That only holds while no workflow names the image
ref itself, so that is pinned too. Whether the tag matches the Dockerfile pins
is ``test_packaging_templates.py::test_age_image_version_pins_agree_everywhere``.

Pure reads of tracked files: no database, no network.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
import yaml

pytestmark = pytest.mark.nodb

_REPO_ROOT = Path(__file__).resolve().parents[1]
_WORKFLOWS = _REPO_ROOT / ".github" / "workflows"
_COMPOSE_NAME = "docker-compose.age-test.yml"
_COMPOSE = _REPO_ROOT / _COMPOSE_NAME
_COMPOSE_CMD = f"docker compose -f {_COMPOSE_NAME}"
_AGE_SERVICE = "postgres-age-test"
_IMAGE_REPO = "ghcr.io/mshtawythug/second-brain-age"

# Discovery below finds every workflow that names the compose file. This floor
# keeps discovery from passing on an empty set: if one of these stops matching,
# the test fails instead of iterating over nothing.
_EXPECTED_AT_LEAST = frozenset({"ci.yml", "eval.yml", "benchmark.yml"})


def _age_workflows() -> list[Path]:
    """Every workflow file whose text names the AGE test compose file."""
    return sorted(
        path
        for path in _WORKFLOWS.glob("*.y*ml")
        if _COMPOSE_NAME in path.read_text(encoding="utf-8")
    )


def _steps(path: Path) -> list[dict[str, Any]]:
    """Every step of every job in ``path``, in file order."""
    workflow = yaml.safe_load(path.read_text(encoding="utf-8"))
    steps: list[dict[str, Any]] = []
    for job in workflow["jobs"].values():
        steps.extend(job.get("steps", []))
    return steps


def _run(step: dict[str, Any]) -> str:
    return str(step.get("run", ""))


def test_discovery_covers_every_known_age_workflow() -> None:
    found = {path.name for path in _age_workflows()}
    missing = _EXPECTED_AT_LEAST - found
    assert not missing, f"workflows no longer naming {_COMPOSE_NAME}: {sorted(missing)}"


@pytest.mark.parametrize("workflow", _age_workflows(), ids=lambda p: p.name)
def test_age_db_is_pulled_first_with_a_build_fallback(workflow: Path) -> None:
    """A tolerant ``pull`` comes first, then a plain ``up -d``. ``--build`` never appears."""
    steps = _steps(workflow)
    compose_runs = [(i, _run(s)) for i, s in enumerate(steps) if _COMPOSE_CMD in _run(s)]

    pulls = [(i, run) for i, run in compose_runs if f"{_COMPOSE_CMD} pull" in run]
    assert len(pulls) == 1, (
        f"{workflow.name}: expected exactly one `{_COMPOSE_CMD} pull` step, found {len(pulls)}"
    )
    pull_index, pull_run = pulls[0]
    assert pull_run.strip().endswith("|| true"), (
        f"{workflow.name}: the pull step must end in `|| true`, or a registry miss fails "
        f"the job instead of falling back to `build:`. Got: {pull_run.strip()!r}"
    )

    ups = [(i, run) for i, run in compose_runs if f"{_COMPOSE_CMD} up -d" in run]
    assert len(ups) == 1, (
        f"{workflow.name}: expected exactly one `{_COMPOSE_CMD} up -d` step, found {len(ups)}"
    )
    up_index, _ = ups[0]
    assert pull_index < up_index, f"{workflow.name}: the pull step must come before `up -d`"

    building = [run for _, run in compose_runs if "--build" in run]
    assert not building, (
        f"{workflow.name}: `--build` rebuilds AGE over the pulled image, which undoes the "
        f"pull. Without a local image, `up -d` already builds from `build:`. "
        f"Got: {building!r}"
    )


@pytest.mark.parametrize("workflow", _age_workflows(), ids=lambda p: p.name)
def test_workflow_takes_the_image_ref_only_from_compose(workflow: Path) -> None:
    """No step names the image itself, so the pulled tag is always the one compose would build."""
    hardcoded = [run for run in map(_run, _steps(workflow)) if _IMAGE_REPO in run]
    assert not hardcoded, (
        f"{workflow.name} names {_IMAGE_REPO} directly. The ref must come only from "
        f"{_COMPOSE_NAME}'s `image:` line, or the pull and the build can drift apart. "
        f"Got: {hardcoded!r}"
    )


@pytest.mark.parametrize("workflow", _age_workflows(), ids=lambda p: p.name)
def test_pull_first_todo_is_gone(workflow: Path) -> None:
    text = workflow.read_text(encoding="utf-8")
    assert "TODO flip to pull-first" not in text, (
        f"{workflow.name} still carries the pull-first TODO. The image is public now."
    )


def test_compose_service_keeps_both_image_and_build() -> None:
    """The fallback requires both: ``image:`` for the pull and ``build:`` for the fallback."""
    service = yaml.safe_load(_COMPOSE.read_text(encoding="utf-8"))["services"][_AGE_SERVICE]
    assert str(service.get("image", "")).startswith(f"{_IMAGE_REPO}:"), (
        f"{_COMPOSE_NAME} `{_AGE_SERVICE}` must pull from {_IMAGE_REPO}"
    )
    assert service.get("build"), (
        f"{_COMPOSE_NAME} `{_AGE_SERVICE}` lost its `build:` stanza, so a registry miss "
        "has nothing to fall back to"
    )
