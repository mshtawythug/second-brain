"""Static checks for dev dependency pins that protect local verification."""

import tomllib
from pathlib import Path

import pytest
from packaging.requirements import Requirement
from packaging.utils import canonicalize_name
from packaging.version import Version

REPO_ROOT = Path(__file__).resolve().parent.parent
PYPROJECT = REPO_ROOT / "pyproject.toml"

# The newest `coverage` release measured green against this tree — 7.16.2 on
# 2026-09-29 (re-measured for Dependabot #12, landed in #15) — and the cap
# derived from it, the next unreleased minor. Bumping either without re-running
# the measurement recorded beside the pin in pyproject.toml is exactly the
# mistake the evidence block there exists to prevent; the next-minor test below
# fails if one moves without the other.
MEASURED_COVERAGE_VERSION = "7.16.2"
MEASURED_COVERAGE_CEILING = "7.17"

# The newest `ruff` release measured green on this tree, and the cap derived
# from it: the next unreleased MINOR above it. 0.17.0 was newest on 2026-10-13
# and is verified green (byte-identical to 0.16.10 on the same tree, same files,
# same enabled rules). For a 0.x project the minor IS the breaking-change unit —
# ruff lands new rules and rule-behaviour changes there — so this is the
# meaningful granularity, not a patch cap. The two move TOGETHER;
# `test_ceiling_is_the_next_minor_above_the_measured_version` fails if one
# moves without the other.
MEASURED_RUFF_VERSION = "0.17.0"
MEASURED_RUFF_CEILING = "0.18"

# Same rule for `mypy`: 2.4.0 was newest on 2026-10-12 and is verified green
# (byte-identical to 2.3.1 on the same tree). NOTE the floor is `>=1.13`, two
# majors below what actually resolves — see the evidence block in
# pyproject.toml, which records that as an UNMEASURED gap rather than quietly
# raising it.
MEASURED_MYPY_VERSION = "2.4.0"
MEASURED_MYPY_CEILING = "2.5"

# `pytest-timeout`, added 2026-10-12 so a hung browser test FAILS the run instead
# of stalling it (tests/browser_timeout.py). 2.4.0 is the newest release and the
# one measured (evidence block beside the pin in pyproject.toml), and the cap is
# the next unreleased minor above it, by the same rule as ruff and mypy.
MEASURED_PYTEST_TIMEOUT_VERSION = "2.4.0"
MEASURED_PYTEST_TIMEOUT_CEILING = "2.5"

# The gate tools, and why they are tested together: both are what
# `scripts/hooks/pre-commit` and ci.yml's `lint` job run, and both were unbounded
# until 2026-08-20. Parameterising over this keeps the two assertions from
# drifting apart the way a copy-pasted pair does.
GATE_TOOL_CEILINGS = {
    "ruff": MEASURED_RUFF_CEILING,
    "mypy": MEASURED_MYPY_CEILING,
}

# Every measured pin — the gate tools plus `coverage` — as (measured version,
# ceiling). The admits and next-minor checks run over ALL of them: coverage was
# left out at first, and `coverage<7.16` with a ceiling of "7.16" then passed
# every test while excluding the 7.16.2 the evidence block records. Which names
# belong here is not a choice: `test_every_capped_dev_pin_is_measured` derives
# it from pyproject.toml.
MEASURED_PINS = {
    "coverage": (MEASURED_COVERAGE_VERSION, MEASURED_COVERAGE_CEILING),
    "ruff": (MEASURED_RUFF_VERSION, MEASURED_RUFF_CEILING),
    "mypy": (MEASURED_MYPY_VERSION, MEASURED_MYPY_CEILING),
    "pytest-timeout": (MEASURED_PYTEST_TIMEOUT_VERSION, MEASURED_PYTEST_TIMEOUT_CEILING),
}


def _dev_requirements() -> list[Requirement]:
    """Return every requirement in the `dev` extra, parsed."""
    data = tomllib.loads(PYPROJECT.read_text(encoding="utf-8"))
    dev_deps: list[str] = data["project"]["optional-dependencies"]["dev"]
    return [Requirement(raw) for raw in dev_deps]


def _dev_requirement(name: str) -> Requirement:
    """Return the parsed `dev` extra requirement for ``name``.

    Parses the specifier rather than substring-matching the raw string, so the
    assertions below test the BOUND and not the formatting.
    """
    requirements = _dev_requirements()
    for requirement in requirements:
        if requirement.name == name:
            return requirement
    raise AssertionError(
        f"no `{name}` requirement in [project.optional-dependencies].dev: "
        f"{[str(r) for r in requirements]}"
    )


def test_every_capped_dev_pin_is_measured() -> None:
    """Every `dev` requirement with an upper bound must have a MEASURED_PINS entry.

    The cap convention is "the next unreleased minor above the newest MEASURED
    release", so a cap with no measurement behind it is a guess. The admits and
    next-minor tests below only cover names that are already in MEASURED_PINS,
    so membership has to come from pyproject.toml rather than from this file:
    otherwise dropping `coverage` from the map (or adding a new capped dev dep
    and forgetting the map) leaves every test green.

    TO ADD A CAPPED DEV DEP — e.g. `pytest-timeout>=2.4.0,<2.5` — measure the
    newest release on this tree, write the evidence block beside the pin in
    pyproject.toml, then add both constants here and the entry, e.g.
    `MEASURED_PYTEST_TIMEOUT_VERSION = "2.4.0"`,
    `MEASURED_PYTEST_TIMEOUT_CEILING = "2.5"` and
    `"pytest-timeout": (MEASURED_PYTEST_TIMEOUT_VERSION,
    MEASURED_PYTEST_TIMEOUT_CEILING)`. An uncapped dep (`>=` only) needs nothing.
    """
    capped = {
        canonicalize_name(requirement.name)
        for requirement in _dev_requirements()
        if any(spec.operator in {"<", "<="} for spec in requirement.specifier)
    }
    measured = {canonicalize_name(name) for name in MEASURED_PINS}

    assert measured == capped, (
        f"capped dev pins without a MEASURED_PINS entry: {sorted(capped - measured)}; "
        f"MEASURED_PINS entries with no capped dev pin: {sorted(measured - capped)}. "
        "Every upper bound in [project.optional-dependencies].dev must be "
        "measured: add MEASURED_<NAME>_VERSION and MEASURED_<NAME>_CEILING (the "
        "next minor above the version) and a MEASURED_PINS entry for it, plus "
        "the evidence block in pyproject.toml — see this test's docstring. If "
        "the cap was dropped, drop the entry too."
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


def test_pytest_timeout_is_capped_at_the_measured_ceiling() -> None:
    """`pytest-timeout` must stay capped at the next minor above the release
    measured. (That the cap admits that release, and is its next minor, is
    checked for every MEASURED_PINS entry below.)

    WHY CAPPED. The plugin is the only thing that turns a hung browser test into
    a failure: `page.evaluate` has no timeout of its own. A release that changed
    how the `timeout` marker is read, or how the `thread` method ends a run,
    would put the stall back silently, with the suite still green on every
    healthy run. So the next minor is measured before it becomes the guard. WHY
    IN `dev`: `--strict-markers` makes `pytest.mark.timeout` a hard error
    wherever the plugin is missing, and every CI job installs `dev`.
    """
    requirement = _dev_requirement("pytest-timeout")
    upper_bounds = {
        (spec.operator, spec.version)
        for spec in requirement.specifier
        if spec.operator in {"<", "<="}
    }

    assert upper_bounds == {("<", MEASURED_PYTEST_TIMEOUT_CEILING)}, (
        f"`pytest-timeout` is capped at {sorted(upper_bounds)} but the measured "
        f"ceiling is <{MEASURED_PYTEST_TIMEOUT_CEILING}. To RAISE it: install the "
        "new version in a scratch venv, run the CI browser command clean (all "
        "green) and under the fitText hang mutant (`else hi = mid;`; it must "
        "FAIL, not stall), then move MEASURED_PYTEST_TIMEOUT_VERSION, this "
        "ceiling AND the evidence block beside the pin in pyproject.toml."
    )


@pytest.mark.parametrize("name", sorted(MEASURED_PINS))
def test_ceiling_admits_the_version_that_was_measured(name: str) -> None:
    """The cap must not exclude the very version the evidence block measured.

    A ceiling that forbids the release it was derived from is self-refuting, and
    it fails at `pip install` time rather than here — which is worse, because the
    bound then looks deliberate. This is the counterfactual half of the pin: the
    test above proves the bound EXISTS, this one proves it ADMITS its own
    evidence. It checks the specifier AS WRITTEN, so it holds for `coverage`'s
    upper-bound-only `<7.17` as much as for `ruff`'s `>=0.7,<0.18`.
    """
    measured, _ceiling = MEASURED_PINS[name]
    requirement = _dev_requirement(name)

    assert requirement.specifier.contains(measured), (
        f"`{requirement}` excludes {name} {measured}, which is the version the "
        "evidence block in pyproject.toml records as measured green on this "
        "tree. Either the cap is wrong or the evidence block is stale — fix "
        "whichever, but they cannot disagree."
    )


@pytest.mark.parametrize("name", sorted(MEASURED_PINS))
def test_ceiling_is_the_next_minor_above_the_measured_version(name: str) -> None:
    """Each ceiling constant must be derived from its measured-version constant.

    The two tests above compare pyproject.toml against the constants, so they
    cannot notice the constants disagreeing with EACH OTHER. Before the
    measured versions were named constants, the admits-test carried a literal
    map (`0.16.4` / `2.3.1`) that two cap raises left behind, and a bound of
    `<0.17` with a ceiling of "0.17" passed every test while excluding the
    0.17.0 just measured. This pins the rule the evidence block states — cap =
    next unreleased minor above the newest MEASURED release — so moving either
    constant alone goes red here. (Which names are in MEASURED_PINS is
    enforced separately, by `test_every_capped_dev_pin_is_measured`.)
    """
    raw_measured, ceiling = MEASURED_PINS[name]
    measured = Version(raw_measured)
    expected = f"{measured.major}.{measured.minor + 1}"

    assert ceiling == expected, (
        f"`{name}` ceiling constant is <{ceiling} but the measured "
        f"version constant is {measured}, whose next unreleased minor is "
        f"<{expected}. Move the measured version, the ceiling and the evidence "
        "block in pyproject.toml together."
    )
