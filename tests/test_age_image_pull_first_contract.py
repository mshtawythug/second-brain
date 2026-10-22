"""Contract: CI workflows pull the AGE test image first and build it only as a fallback.

``docker-compose.age-test.yml`` has both ``image:`` (the prebuilt multi-arch image
on GHCR) and ``build:`` (the canonical Dockerfile). A workflow that runs
``up -d --build``, or a separate ``build`` step, compiles Apache AGE from C source
on every run and never uses the published image. A workflow that pulls without
``|| true`` fails the job when the registry misses, even though the ``build:``
fallback would have worked. This module pins one shape for every workflow that
names the compose file:

1. a ``docker compose -f docker-compose.age-test.yml pull ... || true`` step;
2. then an ``up -d`` step with no ``--build``. With no ``pull_policy`` set, ``up``
   uses a local image when one exists. When none exists it tries the registry
   and builds from ``build:`` only if that pull fails. That is the fallback.
   (Checked on Compose v5.1.4: an absent pullable image was pulled, and an
   unpullable ref was built.) The explicit pull step is what refreshes a
   mutable tag that is already cached, and it is visible as its own step.

The pulled ref and the built ref are the same string because both come from
the compose ``image:`` line. That only holds while no workflow names the image
ref itself, so that is pinned too. Whether the tag matches the Dockerfile pins
is ``test_packaging_templates.py::test_age_image_version_pins_agree_everywhere``.
The PR-time build of the Dockerfile itself lives in ``publish-age-image.yml``
and is pinned by ``test_publish_age_image_workflow_contract.py``.

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
    """A tolerant ``pull`` comes first, then a plain ``up -d``. Nothing forces a build."""
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

    building = [
        run for _, run in compose_runs if "--build" in run or f"{_COMPOSE_CMD} build" in run
    ]
    assert not building, (
        f"{workflow.name}: `--build` or a `{_COMPOSE_CMD} build` step compiles AGE even "
        f"when the pull succeeded, which undoes the pull. `up -d` already builds from "
        f"`build:` when the pull missed. Got: {building!r}"
    )


@pytest.mark.parametrize("workflow", _age_workflows(), ids=lambda p: p.name)
def test_workflow_takes_the_image_ref_only_from_compose(workflow: Path) -> None:
    """No key in the workflow names the image, so the pulled tag is the one compose builds.

    The whole parsed document is checked (``run:``, ``env:``, ``with:``, anything),
    and comments are not, because a comment does not change what gets pulled.
    """
    parsed = yaml.safe_dump(yaml.safe_load(workflow.read_text(encoding="utf-8")))
    assert _IMAGE_REPO not in parsed, (
        f"{workflow.name} names {_IMAGE_REPO} outside a comment. The ref must come only "
        f"from {_COMPOSE_NAME}'s `image:` line, or the pull and the build can drift apart."
    )


@pytest.mark.parametrize("workflow", _age_workflows(), ids=lambda p: p.name)
def test_no_workflow_text_still_describes_build_first(workflow: Path) -> None:
    """The TODO, and any prose or command still saying ``up -d --build``, are gone.

    Checked against the raw text, comments included: a header comment that says
    the other workflows ``up -d --build`` misleads the next reader just as much.
    """
    text = workflow.read_text(encoding="utf-8")
    assert "TODO flip to pull-first" not in text, (
        f"{workflow.name} still carries the pull-first TODO. The image is public now."
    )
    assert "up -d --build" not in text, (
        f"{workflow.name} still mentions `up -d --build`. Every AGE workflow pulls first."
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
