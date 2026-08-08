"""Contract tests for the release workflow's CI gate (.github/workflows/release.yml).

``release.yml`` publishes to **PyPI** via Trusted Publishing. That action is
irreversible: a published version can be yanked but never replaced, and it stays
resolved in every lockfile that already saw it. Everything else in this repository
is recoverable; this one job is not.

Before the gate existed, ``pypi-publish`` had nothing in front of it — the ``pypi``
deployment environment carried no protection rules, and the job did not depend on
the ``ci`` workflow. Because ``ci.yml`` triggers on ``pull_request`` and
``push: branches: [main, master]`` but **not** on tags, pushing a ``v*`` tag
started the PyPI publish *concurrently with* CI rather than after it. A red suite
and a published wheel were entirely compatible states.

Every assertion here is a pure read of a tracked repository file — no database,
no network, no fixtures. A workflow that quietly loses its gate still shows a
green badge; only a test can notice.
"""
from __future__ import annotations

import re
from pathlib import Path
from typing import Any

import yaml

REPO_ROOT = Path(__file__).resolve().parents[1]
RELEASE_WORKFLOW = REPO_ROOT / ".github" / "workflows" / "release.yml"

# The job that must not run until `ci` is proven green, and the job that proves it.
PUBLISH_JOB = "pypi-publish"
GATE_JOB = "ci-gate"

# The gate's whole job is to ask about ONE commit's `ci` runs. Both halves must be
# present: the workflow-scoped runs endpoint, and the head_sha filter that pins it
# to the commit being released rather than "the latest run on the branch".
CI_RUNS_ENDPOINT = "actions/workflows/ci.yml/runs"
HEAD_SHA_FILTER = "head_sha"

# `if:` tokens that make a job run even when a `needs:` dependency FAILED. Any one
# of them on the publish job turns the gate into decoration.
NEEDS_BYPASS_TOKENS = ("always()", "cancelled()", "failure()")

# Four distinct ambiguity classes must each end in a non-zero exit: the API query
# erroring, no `ci` run existing for the commit, a run concluding anything other
# than success, and the bounded wait expiring. "Not finished yet" must never be
# read as success.
MIN_FAIL_CLOSED_EXITS = 4


def _workflow() -> dict[str, Any]:
    parsed = yaml.safe_load(RELEASE_WORKFLOW.read_text(encoding="utf-8"))
    assert isinstance(parsed, dict), "release.yml did not parse as a YAML mapping"
    return parsed


def _jobs() -> dict[str, Any]:
    jobs = _workflow().get("jobs")
    assert isinstance(jobs, dict), "release.yml has no `jobs:` mapping"
    return jobs


def _job(name: str) -> dict[str, Any]:
    jobs = _jobs()
    assert name in jobs, f"release.yml has no `{name}` job (found: {sorted(jobs)})"
    job = jobs[name]
    assert isinstance(job, dict), f"release.yml's `{name}` job is not a mapping"
    return job


def _needs(job: dict[str, Any]) -> list[str]:
    """A job's ``needs:`` normalised to a list (the key accepts a scalar or a list)."""
    raw = job.get("needs", [])
    if isinstance(raw, str):
        return [raw]
    assert isinstance(raw, list), "`needs:` must be a string or a list"
    return [str(item) for item in raw]


def _gate_scripts() -> list[str]:
    """Every ``run:`` script in the gate job."""
    steps = _job(GATE_JOB).get("steps", [])
    assert isinstance(steps, list), f"`{GATE_JOB}` has no `steps:` list"
    return [str(step["run"]) for step in steps if isinstance(step, dict) and "run" in step]


# ---------------------------------------------------------------------------
# The gate exists, and the irreversible job actually depends on it
# ---------------------------------------------------------------------------


def test_release_workflow_parses() -> None:
    """A syntactically broken release.yml is not a release process."""
    assert _jobs(), "release.yml declares no jobs"


def test_a_ci_gate_job_exists() -> None:
    """There must be a job whose purpose is to prove `ci` succeeded for this commit."""
    scripts = "\n".join(_gate_scripts())

    assert CI_RUNS_ENDPOINT in scripts, (
        f"`{GATE_JOB}` never queries `{CI_RUNS_ENDPOINT}` — it cannot know whether "
        "the quality gate passed for the commit being released"
    )
    assert HEAD_SHA_FILTER in scripts, (
        f"`{GATE_JOB}` queries the `ci` runs without a `{HEAD_SHA_FILTER}` filter. "
        "Any green run on any commit would satisfy it; the gate must be pinned to "
        "the exact commit the tag points at."
    )


def test_pypi_publish_depends_on_the_ci_gate() -> None:
    """The gate must BLOCK, which in GitHub Actions means `needs:`.

    A gate job that merely runs alongside the publish (or that logs a warning and
    exits 0) is worse than no gate: it reads as protection while the irreversible
    action proceeds regardless.
    """
    needs = _needs(_job(PUBLISH_JOB))

    assert GATE_JOB in needs, (
        f"`{PUBLISH_JOB}` does not declare `needs: {GATE_JOB}` (needs={needs}). "
        "Without it the PyPI publish starts concurrently with — not after — the "
        "proof that CI is green, and a published version cannot be replaced."
    )


def test_pypi_publish_cannot_bypass_a_failed_gate() -> None:
    """`if: always()` (or `!cancelled()`) on the publish job would undo `needs:`.

    GitHub only skips a dependent job on a failed `needs:` while its own `if:`
    stays free of the status functions. Adding one is a single-token regression
    that leaves every other assertion in this module passing.
    """
    condition = str(_job(PUBLISH_JOB).get("if", ""))

    offenders = [token for token in NEEDS_BYPASS_TOKENS if token in condition]
    assert not offenders, (
        f"`{PUBLISH_JOB}`'s `if:` contains {offenders} — those functions make the "
        f"job run even when `{GATE_JOB}` FAILED, so the gate stops blocking. "
        f"Current condition: {condition!r}"
    )


def test_the_gate_applies_to_workflow_dispatch_too() -> None:
    """A manual dispatch must not be a way around the gate.

    `workflow_dispatch` exists so a failed publish can be re-driven without
    re-tagging (see the workflow header). Exempting it would leave a
    one-click bypass: dispatch on a `v*` tag whose CI is red and publish anyway.
    Pinning the two conditions to the same expression is what keeps the dispatch
    path usable AND gated — on that path CI has long since concluded, so the gate
    resolves on its first poll.
    """
    gate_if = str(_job(GATE_JOB).get("if", ""))
    publish_if = str(_job(PUBLISH_JOB).get("if", ""))

    assert gate_if, f"`{GATE_JOB}` has no `if:` guard; it must mirror `{PUBLISH_JOB}`'s"
    assert gate_if == publish_if, (
        f"`{GATE_JOB}`'s `if:` ({gate_if!r}) differs from `{PUBLISH_JOB}`'s "
        f"({publish_if!r}). If the gate skips on a ref the publish still runs on, "
        "that ref publishes ungated."
    )


# ---------------------------------------------------------------------------
# The gate fails CLOSED
# ---------------------------------------------------------------------------


def test_nothing_in_the_release_workflow_continues_on_error() -> None:
    """`continue-on-error: true` on the gate would make its failure advisory."""
    raw = RELEASE_WORKFLOW.read_text(encoding="utf-8")

    assert "continue-on-error" not in raw, (
        "release.yml uses `continue-on-error`. On the gate job or any of its steps "
        "that converts a proven-red CI into a warning, and the PyPI publish "
        "proceeds — the exact 'gate that only logs' pattern this file exists to "
        "prevent."
    )


def test_the_gate_script_does_not_swallow_failures() -> None:
    """`set -euo pipefail`, and no `|| true` masking the verdict."""
    for script in _gate_scripts():
        if CI_RUNS_ENDPOINT not in script:
            continue
        assert "set -euo pipefail" in script, (
            "the gate script must run under `set -euo pipefail`; without `-e` an "
            "unexpected command failure mid-script still reaches the final `exit 0`"
        )
        assert "|| true" not in script, (
            "the gate script masks a command failure with `|| true`. Every "
            "ambiguity — API error, missing run, unfinished run — must fail closed."
        )


def test_the_gate_fails_closed_on_every_ambiguity() -> None:
    """Four ambiguity classes, four non-zero exits.

    In-progress, failed, missing, and API-error must each end the gate with a
    non-zero status. A bounded wait is fine; treating its expiry as a pass is not.
    """
    gate_script = next(
        (script for script in _gate_scripts() if CI_RUNS_ENDPOINT in script),
        None,
    )
    assert gate_script is not None, f"`{GATE_JOB}` has no step that queries the `ci` runs"

    fail_closed_exits = len(re.findall(r"(?m)^\s*exit\s+1\b", gate_script))
    assert fail_closed_exits >= MIN_FAIL_CLOSED_EXITS, (
        f"the gate script has {fail_closed_exits} `exit 1` path(s); at least "
        f"{MIN_FAIL_CLOSED_EXITS} are expected, one per ambiguity class (API error, "
        "no `ci` run for the commit, a run that did not conclude `success`, and the "
        "bounded wait expiring). Fewer means one of them falls through to success."
    )


def test_the_gates_wait_is_bounded_by_a_job_timeout() -> None:
    """A polling loop needs a ceiling the runner enforces, not just one it intends.

    The in-script deadline is the primary bound; `timeout-minutes` is the backstop
    for the case where the loop itself wedges (a `gh` call that never returns).
    Both make the expiry a failure — GitHub marks a timed-out job failed, so the
    `needs:` edge still skips the publish.
    """
    timeout = _job(GATE_JOB).get("timeout-minutes")

    assert isinstance(timeout, int), (
        f"`{GATE_JOB}` declares no integer `timeout-minutes`. Its poll loop would "
        "otherwise be capped only by the 6-hour runner default, holding the release "
        "open indefinitely instead of failing."
    )


def test_the_gate_can_read_workflow_runs() -> None:
    """Least privilege, but not so least that the query 404s.

    Listing workflow runs needs `actions: read`. Without it the gate still fails
    closed (the API call errors), so this asserts usability rather than safety —
    a gate that can only ever fail is a gate nobody keeps.
    """
    permissions = _job(GATE_JOB).get("permissions")

    assert isinstance(permissions, dict), (
        f"`{GATE_JOB}` declares no job-level `permissions:` block, so it inherits "
        "the workflow's `contents: write` — more than a read-only gate needs"
    )
    assert permissions.get("actions") == "read", (
        f"`{GATE_JOB}` needs `actions: read` to list the `ci` workflow's runs; "
        f"got permissions={permissions}"
    )
