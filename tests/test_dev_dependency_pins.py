"""Static checks for dev dependency pins that protect local verification."""

import tomllib
from pathlib import Path

import pytest
from packaging.requirements import Requirement

REPO_ROOT = Path(__file__).resolve().parent.parent
PYPROJECT = REPO_ROOT / "pyproject.toml"

# The newest `coverage` release measured green against this tree — 7.15.4 is
# current, so the cap is the next unreleased minor. Bumping this constant
# without re-running the measurement recorded beside the pin in pyproject.toml
# is exactly the mistake the evidence block there exists to prevent.
MEASURED_COVERAGE_CEILING = "7.16"

# The next unreleased MINOR above the newest `ruff` release measured green on
# this tree. 0.16.4 was newest on 2026-08-20 and is verified green, so the cap is
# 0.17. For a 0.x project the minor IS the breaking-change unit — ruff lands new
# rules and rule-behaviour changes there — so this is the meaningful granularity,
# not a patch cap.
MEASURED_RUFF_CEILING = "0.17"

# Same rule for `mypy`: 2.3.1 was newest on 2026-08-20 and is verified green, so
# the cap is the next unreleased minor. NOTE the floor is `>=1.13`, two majors
# below what actually resolves — see the evidence block in pyproject.toml, which
# records that as an UNMEASURED gap rather than quietly raising it.
MEASURED_MYPY_CEILING = "2.4"

# The gate tools, and why they are tested together: both are what
# `scripts/hooks/pre-commit` and ci.yml's `lint` job run, and both were unbounded
# until 2026-08-20. Parameterising over this keeps the two assertions from
# drifting apart the way a copy-pasted pair does.
GATE_TOOL_CEILINGS = {
    "ruff": MEASURED_RUFF_CEILING,
    "mypy": MEASURED_MYPY_CEILING,
}


def _dev_requirement(name: str) -> Requirement:
    """Return the parsed `dev` extra requirement for ``name``.

    Parses the specifier rather than substring-matching the raw string, so the
    assertions below test the BOUND and not the formatting.
    """
    data = tomllib.loads(PYPROJECT.read_text(encoding="utf-8"))
    dev_deps: list[str] = data["project"]["optional-dependencies"]["dev"]
    for raw in dev_deps:
        requirement = Requirement(raw)
        if requirement.name == name:
            return requirement
    raise AssertionError(
        f"no `{name}` requirement in [project.optional-dependencies].dev: {dev_deps}"
    )


def test_coverage_is_capped_at_the_measured_ceiling() -> None:
    """`coverage` must stay capped at the newest release measured on this tree.

    RENAMED from ``test_coverage_is_pinned_below_pgvector_breaking_range``,
    which was a misnomer inherited from the neighbouring `pgvector` pin. There
    is no pgvector interaction: the 2026-05-05 handoff note blamed one
    ("numpy + coverage 7.13 + pgvector C-ext"), and a 2026-08-08 re-measurement
    across 7.12.0 / 7.13.0 / 7.13.5 / 7.14.3 / 7.15.4 could not reproduce it on
    either Python 3.11 or 3.14 — collection, execution and the reported
    percentage were bit-identical. See the evidence block beside the pin.

    What the cap actually protects is the coverage GATE, not pgvector.
    ``addopts`` carries ``--cov-fail-under=85`` and
    ``tests/test_ci_workflow.py::test_ci_pytest_does_not_disable_coverage``
    forbids CI from passing ``--no-cov``, so there is no escape hatch: a
    coverage release that changes WHAT gets measured erodes the 85% floor
    quietly instead of failing loudly. The bound is therefore not a claim that
    the next minor breaks — it is a requirement that it be measured before
    users get it.
    """
    requirement = _dev_requirement("coverage")
    upper_bounds = {
        (spec.operator, spec.version)
        for spec in requirement.specifier
        if spec.operator in {"<", "<="}
    }

    assert upper_bounds, (
        "`coverage` must carry an UPPER bound. Unbounded, a future release is "
        "free to change what is measured under `--cov-fail-under=85` — and "
        "because CI may not pass `--no-cov`, that shows up as the 85% floor "
        f"drifting rather than as a red test. Found: {requirement}"
    )
    assert upper_bounds == {("<", MEASURED_COVERAGE_CEILING)}, (
        f"`coverage` is capped at {sorted(upper_bounds)} but the measured "
        f"ceiling is <{MEASURED_COVERAGE_CEILING}. To RAISE the cap: install "
        "the new version in a scratch venv, run a coverage-COLLECTING slice "
        "(never `--no-cov` — that is precisely what hides this class of "
        "regression), confirm the reported percentage is unchanged, then update "
        "both this constant and the evidence block beside the pin in "
        "pyproject.toml. To LOWER it, record which version regressed and how."
    )


def test_coverage_has_no_redundant_floor() -> None:
    """No explicit `coverage` floor: `pytest-cov` already imposes one.

    `pytest-cov>=5.0` resolves to 7.1.0, which requires `coverage[toml]>=7.10.6`
    — so coverage is ALREADY bounded below for every install, and a second floor
    here would be a duplicate to maintain in two places when pytest-cov moves.
    Recorded as a test so the omission reads as a decision rather than a gap
    (same reasoning the `httpx` and `click` notes in pyproject.toml spell out).
    """
    requirement = _dev_requirement("coverage")
    lower_bounds = {
        (spec.operator, spec.version)
        for spec in requirement.specifier
        if spec.operator in {">", ">=", "==", "~="}
    }

    assert not lower_bounds, (
        f"`coverage` declares a lower bound {sorted(lower_bounds)}, but "
        "`pytest-cov` already constrains coverage from below transitively. If "
        "this floor is deliberate, record WHICH coverage version is too old "
        "and what it breaks — otherwise drop it and keep one source of truth."
    )
    _dev_requirement("pytest-cov")  # the pin above leans on this being present


@pytest.mark.parametrize("tool", sorted(GATE_TOOL_CEILINGS))
def test_gate_tool_is_capped_at_the_measured_ceiling(tool: str) -> None:
    """`ruff` and `mypy` must stay capped at the newest release measured green.

    WHY THIS EXISTS. Both were unbounded (`ruff>=0.7`, `mypy>=1.13`) and both are
    what the local gate runs. On 2026-08-20 a fresh `pip install -e ".[dev]"` —
    the command ci.yml uses — resolved ruff 0.16.4 / mypy 2.3.1 while the repo
    `.venv` held 0.16.3 / 2.3.0. Unbounded, the tool that gates a commit is
    whatever the resolver happened to fetch, and nothing announces a change.

    The bound is NOT a claim that the next minor breaks. It is a requirement that
    it be MEASURED before it silently becomes the gate — the same convention as
    `coverage` above, and as `typer` / `pgvector` in pyproject.toml.
    """
    requirement = _dev_requirement(tool)
    upper_bounds = {
        (spec.operator, spec.version)
        for spec in requirement.specifier
        if spec.operator in {"<", "<="}
    }

    assert upper_bounds, (
        f"`{tool}` must carry an UPPER bound. Unbounded, the version that gates "
        "every local commit is whatever the resolver last fetched, which is how "
        "the pre-commit type gate came to run a different ruff than this repo "
        f"pins. Found: {requirement}"
    )
    assert upper_bounds == {("<", GATE_TOOL_CEILINGS[tool])}, (
        f"`{tool}` is capped at {sorted(upper_bounds)} but the measured ceiling "
        f"is <{GATE_TOOL_CEILINGS[tool]}. To RAISE the cap: install the new "
        "version in a SCRATCH venv (never the repo .venv — other agents share "
        "it), run the gate it belongs to against this tree, diff its output "
        "against the currently-pinned version on the SAME tree so the comparison "
        "is controlled, then update this constant AND the evidence block beside "
        "the pin in pyproject.toml. To LOWER it, record which version regressed "
        "and on what."
    )


@pytest.mark.parametrize("tool", sorted(GATE_TOOL_CEILINGS))
def test_gate_tool_ceiling_admits_the_version_that_was_measured(tool: str) -> None:
    """The cap must not exclude the very version the evidence block measured.

    A ceiling that forbids the release it was derived from is self-refuting, and
    it fails at `pip install` time rather than here — which is worse, because the
    bound then looks deliberate. This is the counterfactual half of the pin: the
    test above proves the bound EXISTS, this one proves it ADMITS its own
    evidence.
    """
    measured = {"ruff": "0.16.4", "mypy": "2.3.1"}[tool]
    requirement = _dev_requirement(tool)

    assert requirement.specifier.contains(measured), (
        f"`{requirement}` excludes {tool} {measured}, which is the version the "
        "evidence block in pyproject.toml records as measured green on this "
        "tree. Either the cap is wrong or the evidence block is stale — fix "
        "whichever, but they cannot disagree."
    )
