"""Static contract tests for the CI quality gate (.github/workflows/ci.yml).

Every assertion here is a pure read of a tracked repository file — no database,
no network, no fixtures. The module exists because a workflow that silently
stops running ``ruff``/``mypy``/``pytest``, or that quietly grows a ``--no-cov``
flag, still shows a green badge; only a test can notice.
"""
from __future__ import annotations

import re
from pathlib import Path
from typing import Any

import pytest
import yaml

REPO_ROOT = Path(__file__).parent.parent
WORKFLOWS = REPO_ROOT / ".github" / "workflows"
CI_WORKFLOW = WORKFLOWS / "ci.yml"
README = REPO_ROOT / "README.md"
TESTS_DIR = REPO_ROOT / "tests"

# GitHub renders a workflow badge from this path shape; the README may carry
# several, and every one of them must name a workflow that exists on disk.
BADGE_URL_RE = re.compile(r"actions/workflows/(?P<workflow>[A-Za-z0-9_.-]+)/badge\.svg")

# Any test module reaching a live Postgres + Ollama carries this marker.
# Deselected from the default suite since 2026-08-07 (C7) — but these modules
# must STILL degrade to a skip rather than a failure when the services are
# absent, because `pytest -m live_db` runs them deliberately on machines that
# may not have a corpus. The deselection is the gate policy; the skip is the
# module's own contract, and this test still pins the latter.
LIVE_DB_MARKER_RE = re.compile(r"pytest\.mark\.live_db")
GATED_MARKER_RE = re.compile(r"@pytest\.mark\.(eval|benchmark)\b")


def load_workflow(name: str) -> dict[str, object]:
    """Load .github/workflows/<name> as parsed YAML."""
    parsed = yaml.safe_load((WORKFLOWS / name).read_text(encoding="utf-8"))
    assert isinstance(parsed, dict), f"{name} did not parse as a YAML mapping"
    return parsed


def _triggers(workflow: dict[str, object]) -> dict[str, Any]:
    """Return the workflow's ``on:`` block.

    PyYAML follows YAML 1.1, where the bare key ``on`` is the boolean ``True``.
    Both spellings are accepted so the test survives a quoting change.
    """
    raw = workflow.get("on", workflow.get(True))
    assert isinstance(raw, dict), "ci.yml has no `on:` mapping"
    return raw


def _jobs(workflow: dict[str, object]) -> dict[str, Any]:
    jobs = workflow.get("jobs")
    assert isinstance(jobs, dict), "ci.yml has no `jobs:` mapping"
    return jobs


def _steps(workflow: dict[str, object]) -> list[dict[str, Any]]:
    """Every step of every job, flattened."""
    return [
        step
        for job in _jobs(workflow).values()
        for step in job.get("steps", [])
        if isinstance(step, dict)
    ]


def _run_commands(workflow: dict[str, object]) -> list[str]:
    """Every ``run:`` script in the workflow, flattened."""
    return [str(step["run"]) for step in _steps(workflow) if "run" in step]


# ---------------------------------------------------------------------------
# The gate exists and runs all three checks
# ---------------------------------------------------------------------------


def test_ci_workflow_file_exists() -> None:
    assert CI_WORKFLOW.is_file(), (
        "No .github/workflows/ci.yml — the repository has no quality gate, so a "
        "PR can break ruff, mypy, or the coverage floor and still show green."
    )


def test_ci_runs_ruff_mypy_and_pytest() -> None:
    runs = "\n".join(_run_commands(load_workflow("ci.yml")))

    assert "ruff check" in runs, "ci.yml never runs `ruff check`"
    assert "mypy src/" in runs, "ci.yml never runs `mypy src/`"
    assert re.search(r"(?m)^\s*pytest\s*$", runs), "ci.yml never runs a bare `pytest`"


def test_ci_pytest_does_not_disable_coverage() -> None:
    """CI must inherit pyproject.toml's coverage floor, never override it."""
    for command in _run_commands(load_workflow("ci.yml")):
        assert "--no-cov" not in command, f"ci.yml disables coverage: {command!r}"
        assert (
            "--cov-fail-under" not in command
        ), f"ci.yml overrides the coverage floor instead of inheriting it: {command!r}"


# ---------------------------------------------------------------------------
# Database safety
# ---------------------------------------------------------------------------


def _env_blocks(workflow: dict[str, object]) -> list[dict[str, Any]]:
    """Every ``env:`` mapping in the workflow — top level, per job, per step."""
    blocks: list[dict[str, Any]] = []
    top_env = workflow.get("env")
    if isinstance(top_env, dict):
        blocks.append(top_env)
    for job in _jobs(workflow).values():
        if isinstance(job.get("env"), dict):
            blocks.append(job["env"])
    for step in _steps(workflow):
        if isinstance(step.get("env"), dict):
            blocks.append(step["env"])
    return blocks


def test_ci_never_targets_the_prod_database() -> None:
    workflow = load_workflow("ci.yml")
    urls = [
        str(value)
        for env in _env_blocks(workflow)
        for key, value in env.items()
        if key in {"DATABASE_URL", "TEST_DATABASE_URL"}
    ]

    assert urls, "ci.yml pins no DATABASE_URL/TEST_DATABASE_URL — the prod fallback could apply"
    for url in urls:
        assert ":5434/" in url, f"CI database URL is not on the test port 5434: {url}"
        assert url.rsplit("/", 1)[-1].endswith(
            "_test"
        ), f"CI database name does not end in `_test`: {url}"

    raw = CI_WORKFLOW.read_text(encoding="utf-8")
    for forbidden in ("55432", ":5433", '/second_brain"'):
        assert forbidden not in raw, f"ci.yml references a prod database token: {forbidden!r}"


def test_ci_tears_down_with_if_always() -> None:
    teardown = [
        step
        for step in _steps(load_workflow("ci.yml"))
        if "docker compose" in str(step.get("run", "")) and " down" in str(step.get("run", ""))
    ]

    assert teardown, "ci.yml never tears the compose stack down"
    for step in teardown:
        assert step.get("if") == "always()", (
            f"compose teardown step {step.get('name')!r} is not `if: always()` — "
            "a failed suite would leak the container"
        )


# ---------------------------------------------------------------------------
# Workflow hygiene
# ---------------------------------------------------------------------------


def test_ci_declares_least_privilege_permissions() -> None:
    assert load_workflow("ci.yml").get("permissions") == {"contents": "read"}


def test_ci_concurrency_cancels_only_on_pull_requests() -> None:
    concurrency = load_workflow("ci.yml").get("concurrency")

    assert isinstance(concurrency, dict), "ci.yml declares no `concurrency:` block"
    assert concurrency.get("cancel-in-progress") == "${{ github.event_name == 'pull_request' }}", (
        "master/main runs must keep a complete audit trail; only PR runs cancel"
    )


def test_ci_triggers_match_the_house_pattern() -> None:
    triggers = _triggers(load_workflow("ci.yml"))

    assert "pull_request" in triggers
    assert "workflow_dispatch" in triggers
    assert triggers["push"]["branches"] == ["main", "master"]


# ---------------------------------------------------------------------------
# The README badge must point at a workflow that exists
# ---------------------------------------------------------------------------


def test_readme_ci_badge_points_at_ci_workflow() -> None:
    ci_badge_lines = [
        line for line in README.read_text(encoding="utf-8").splitlines() if "[![CI]" in line
    ]

    assert ci_badge_lines, "README carries no CI badge"
    for line in ci_badge_lines:
        assert (
            "workflows/ci.yml" in line
        ), f"README CI badge does not point at the quality gate: {line.strip()!r}"


@pytest.mark.parametrize(
    "workflow_file",
    sorted(
        {m.group("workflow") for m in BADGE_URL_RE.finditer(README.read_text(encoding="utf-8"))}
    ),
)
def test_readme_badges_reference_existing_workflows(workflow_file: str) -> None:
    assert (
        WORKFLOWS / workflow_file
    ).is_file(), f"README badges a workflow that does not exist: {workflow_file}"


# ---------------------------------------------------------------------------
# The default suite must stay runnable without Ollama
# ---------------------------------------------------------------------------


def _live_db_modules() -> list[Path]:
    return sorted(
        (
            path
            for path in TESTS_DIR.rglob("test_*.py")
            if LIVE_DB_MARKER_RE.search(path.read_text(encoding="utf-8"))
        ),
        key=lambda path: path.name,
    )


@pytest.mark.parametrize("module_path", _live_db_modules(), ids=lambda path: path.name)
def test_default_suite_needs_no_ollama(module_path: Path) -> None:
    """A ``live_db`` module runs in CI, where Postgres and Ollama are absent.

    ``live_db`` IS in pyproject.toml's marker exclusion list as of 2026-08-07
    (C7), so these modules do not execute in CI at all. The skip contract below
    still matters: ``pytest -m live_db --no-cov`` runs them deliberately on
    machines that may have neither corpus nor Ollama, and they must degrade to a
    skip rather than a failure there. Formerly this docstring read "deliberately
    NOT in ... the exclusion list",
    so these modules execute on a runner that has neither the prod corpus nor
    Ollama. Each therefore has to reach ``pytest.skip(...)`` on an unreachable
    service; a hard failure there would make the gate red for the wrong reason.
    """
    source = module_path.read_text(encoding="utf-8")

    assert "pytest.skip(" in source or GATED_MARKER_RE.search(source), (
        f"{module_path.name} is marked live_db but never calls pytest.skip() — "
        "it will FAIL rather than skip on a runner without Postgres/Ollama"
    )


# ---------------------------------------------------------------------------
# Gate-policy regression (C7 iteration 3).
#
# Deselecting `live_db` was correct: those tests assert ranking against the
# operator's live PRODUCTION corpus, so they reported machine conditions rather
# than code. Deselecting `live_ollama` alongside it was NOT — that marker does
# double duty, and on tests/test_llm_hermeticity.py it merely lifts the socket
# ban for a test that needs no live service, runs in 0.25 s, and is the ONLY
# proof that `_forbid_live_ollama`'s escape hatch still works.
#
# Removing the proof that a guard works is the same failure class the whole
# review was hunting — a reassuring artifact that does nothing — and it was
# introduced BY a fix aimed at that class. This test is what stops one token
# reintroducing it silently: without it, re-adding `and not live_ollama` leaves
# every test green and the coverage floor intact.
# ---------------------------------------------------------------------------


def _addopts() -> str:
    """The `addopts` string from pyproject's pytest config."""
    import tomllib

    data = tomllib.loads((REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    addopts = data["tool"]["pytest"]["ini_options"]["addopts"]
    assert isinstance(addopts, str)
    return addopts


def test_live_db_is_deselected_from_the_default_suite() -> None:
    """The gate must not depend on a live production corpus."""
    assert "not live_db" in _addopts()


def test_live_ollama_is_NOT_deselected_from_the_default_suite() -> None:
    """...but `live_ollama` must stay IN, or the escape-hatch proof leaves the gate.

    `tests/test_llm_hermeticity.py::test_live_ollama_marker_lifts_the_guard`
    carries `live_ollama` and nothing else. Deselecting the marker removes the
    only test proving the opt-out works, and a later break in it would surface
    as a confusing corpus error under `pytest -m live_db` instead of a red test.

    Both canary modules carry BOTH markers, so `not live_db` alone already keeps
    them out — deselecting `live_ollama` buys nothing and costs the guard.
    """
    assert "not live_ollama" not in _addopts(), (
        "`live_ollama` was re-added to the addopts deselection. That removes "
        "test_live_ollama_marker_lifts_the_guard from the default suite — the "
        "only proof the Ollama-guard escape hatch works. `not live_db` alone "
        "already deselects the canaries; see the comment above this test."
    )


def test_the_hermeticity_escape_hatch_test_carries_only_live_ollama() -> None:
    """Pins the premise the test above depends on.

    If that test ever also gained `live_db`, `not live_db` would deselect it and
    the assertion above would pass while the guard's proof quietly left the gate
    anyway — the same defect through a different door.
    """
    source = (REPO_ROOT / "tests" / "test_llm_hermeticity.py").read_text("utf-8")
    marker_block = source[: source.index("def test_live_ollama_marker_lifts_the_guard")]
    tail = marker_block[marker_block.rindex("\n\n") :]
    assert "live_ollama" in tail
    assert "live_db" not in tail, (
        "test_live_ollama_marker_lifts_the_guard gained a live_db marker, which "
        "the default suite deselects — it would silently leave the gate."
    )
