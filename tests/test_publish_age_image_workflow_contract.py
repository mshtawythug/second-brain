"""Contract: publish-age-image.yml build-checks the AGE Dockerfile on PRs, never pushing.

Every AGE test workflow (ci, eval, benchmark) pulls the published image first, so
none of them builds the Dockerfile while the pull succeeds. A Dockerfile edit
that keeps the same tag (a new apt package, a build flag) would then never be
built on its own PR: CI would pull the previously published image and go green.
The PR leg of this workflow closes that gap. On a pull request that touches the
Dockerfile directory or this workflow, it builds ``linux/amd64`` only, with no
QEMU, no registry login and no push.

The master leg must stay as it was: multi-arch, pushed, gha cache, push paths
unchanged. A PR run must not cancel an in-flight master publish, so the
concurrency group cannot be static.

Pure reads of a tracked file: no database, no network.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
import yaml

pytestmark = pytest.mark.nodb

_REPO_ROOT = Path(__file__).resolve().parents[1]
_WORKFLOW = _REPO_ROOT / ".github" / "workflows" / "publish-age-image.yml"
_DOCKERFILE_PATHS = "src/brain/templates/docker/age/**"
_WORKFLOW_PATH = ".github/workflows/publish-age-image.yml"
_NOT_PR = "github.event_name != 'pull_request'"
_CONCURRENCY_GROUP = (
    "publish-age-image-${{ github.event_name == 'pull_request' && github.ref || 'publish' }}"
)
_JOB_NAME = (
    "${{ github.event_name == 'pull_request' && 'Build-check AGE image (amd64, no push)' "
    "|| 'Build + push multi-arch AGE image' }}"
)


def _workflow() -> dict[Any, Any]:
    loaded = yaml.safe_load(_WORKFLOW.read_text(encoding="utf-8"))
    assert isinstance(loaded, dict)
    return loaded


def _triggers() -> dict[str, Any]:
    workflow = _workflow()
    # PyYAML (YAML 1.1) parses the bare key `on` as the boolean True.
    triggers = workflow.get("on", workflow.get(True))
    assert isinstance(triggers, dict), "publish-age-image.yml has no `on:` mapping"
    return triggers


def _steps() -> list[dict[str, Any]]:
    steps = _workflow()["jobs"]["publish"]["steps"]
    assert isinstance(steps, list)
    return steps


def _step(uses_prefix: str) -> dict[str, Any]:
    matches = [s for s in _steps() if str(s.get("uses", "")).startswith(uses_prefix)]
    assert len(matches) == 1, f"expected exactly one `{uses_prefix}` step, found {len(matches)}"
    return matches[0]


def test_pull_request_trigger_covers_the_dockerfile_and_this_workflow() -> None:
    pr = _triggers().get("pull_request")
    assert isinstance(pr, dict), "publish-age-image.yml must run on `pull_request`"
    paths = pr.get("paths", [])
    assert _DOCKERFILE_PATHS in paths, (
        f"the PR leg must trigger on `{_DOCKERFILE_PATHS}`, or a Dockerfile edit that "
        "keeps the image tag is never built on its PR"
    )
    assert _WORKFLOW_PATH in paths, (
        f"the PR leg must trigger on `{_WORKFLOW_PATH}`, so an edit to this workflow "
        "is build-checked on its own PR"
    )


def test_master_push_trigger_is_unchanged() -> None:
    push = _triggers().get("push")
    assert isinstance(push, dict)
    assert push.get("branches") == ["master"]
    assert push.get("tags") == ["age-image-*"]
    assert push.get("paths") == [_DOCKERFILE_PATHS]


def test_concurrency_group_keeps_pr_runs_away_from_the_master_publish() -> None:
    """PRs get one group per ref; EVERY non-PR run shares the single publish group.

    Both halves matter. A static group lets a PR run cancel an in-flight master
    publish. A group keyed on the event or the ref for non-PR runs (for example
    ``${{ github.event_name }}-${{ github.ref }}``) puts a master push and an
    ``age-image-*`` tag push in different groups, so they can race to push the
    same fixed image tag. Substring checks cannot tell these apart, so the
    expression is pinned exactly.
    """
    concurrency = _workflow()["concurrency"]
    assert concurrency["group"] == _CONCURRENCY_GROUP, (
        f"concurrency group must be exactly {_CONCURRENCY_GROUP!r}: per ref for PRs, "
        f"one shared group for every publish. Got {concurrency['group']!r}"
    )
    assert concurrency.get("cancel-in-progress") is True


def test_job_name_says_what_a_pr_run_does() -> None:
    """A PR run only build-checks amd64, so its check must not claim to push."""
    name = _workflow()["jobs"]["publish"]["name"]
    assert name == _JOB_NAME, f"job name must be exactly {_JOB_NAME!r}; got {name!r}"


@pytest.mark.parametrize("uses_prefix", ["docker/setup-qemu-action@", "docker/login-action@"])
def test_qemu_and_registry_login_are_skipped_on_pull_requests(uses_prefix: str) -> None:
    step = _step(uses_prefix)
    assert step.get("if") == _NOT_PR, (
        f"`{uses_prefix}` must carry `if: {_NOT_PR}`. A PR build is amd64-only and "
        "never pushes, so it needs neither emulation nor credentials."
    )


def test_build_is_amd64_only_and_unpushed_on_pull_requests() -> None:
    build = _step("docker/build-push-action@").get("with", {})
    assert build.get("push") == f"${{{{ {_NOT_PR} }}}}", (
        f"build-push `push` must be `${{{{ {_NOT_PR} }}}}`; got {build.get('push')!r}"
    )
    platforms = str(build.get("platforms", ""))
    assert platforms == (
        "${{ github.event_name == 'pull_request' && 'linux/amd64' || 'linux/amd64,linux/arm64' }}"
    ), f"unexpected platforms expression {platforms!r}"


def test_master_build_keeps_its_cache_and_settings() -> None:
    build = _step("docker/build-push-action@").get("with", {})
    assert build.get("context") == "src/brain/templates/docker/age"
    assert build.get("cache-from") == "type=gha"
    assert build.get("cache-to") == "type=gha,mode=max"
    assert build.get("provenance") is False
    assert build.get("tags") == "${{ steps.meta.outputs.tags }}"
    assert build.get("labels") == "${{ steps.meta.outputs.labels }}"
