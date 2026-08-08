"""Drift tests for ``bin/brain-ci`` — the local reproduction of ``ci.yml``.

``bin/brain-ci`` exists because every serious defect in the v0.3.0 cycle came
from a local run and a CI run disagreeing: a stale ``.venv``, the wrong Python
minor, and — the expensive one — ``GITHUB_ACTIONS`` being set on the runner and
nowhere else, which flipped Typer's colour handling and failed 20 CLI tests only
in CI. A script that reproduces CI is worth exactly as much as its agreement
with ``.github/workflows/ci.yml``, so this module pins that agreement.

The comparison logic lives in small pure functions that take the workflow text
and the script text as arguments. ``test_*_drift_is_detected`` feeds them
MUTATED copies and asserts a complaint comes back — without that half, a
checker that silently found nothing would look identical to a passing gate,
which is the exact failure class this repository keeps rediscovering.
"""
from __future__ import annotations

import os
import re
import shutil
import subprocess
from pathlib import Path
from typing import Any

import pytest
import yaml

REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPT = REPO_ROOT / "bin" / "brain-ci"
CI_WORKFLOW = REPO_ROOT / ".github" / "workflows" / "ci.yml"

# The script's contract block is a run of plain NAME="value" assignments, one
# per line. Deliberately dumb to parse: a checker that needs a shell to read
# the file it is checking has a bootstrap problem.
CONTRACT_RE = re.compile(r'(?m)^(CI_[A-Z0-9_]+)="([^"]*)"$')

# `docker compose -f <file> ...` — the compose file ci.yml drives.
COMPOSE_FILE_RE = re.compile(r"docker compose\s+-f\s+(\S+)")

# `pg_isready -h localhost -p 5434 -U brain -d second_brain_test`
# The value classes are `[\w.-]` rather than `\S` because ci.yml writes the
# probe inside an `if ...; then`, so `\S+` swallowed the trailing semicolon and
# reported a permanent, bogus drift on the database name.
PG_ISREADY_RE = re.compile(
    r"pg_isready\s+-h\s+(?P<host>[\w.-]+)\s+-p\s+(?P<port>\d+)"
    r"\s+-U\s+(?P<user>[\w.-]+)\s+-d\s+(?P<db>[\w.-]+)"
)

# `python -m venv .venv`
VENV_DIR_RE = re.compile(r"python\s+-m\s+venv\s+(\S+)")

# `for i in $(seq 1 60); do` ... `sleep 2`
SEQ_RE = re.compile(r"seq\s+1\s+(\d+)")
SLEEP_RE = re.compile(r"(?m)^\s*sleep\s+(\d+)\s*$")

# Flags that would hollow out the gate if the script quietly added them.
GATE_HOLLOWING_FLAGS = ("--no-cov", "--cov-fail-under", "PYTEST_ADDOPTS")


def _workflow_text() -> str:
    return CI_WORKFLOW.read_text(encoding="utf-8")


def _script_text() -> str:
    return SCRIPT.read_text(encoding="utf-8")


def _contract(script_text: str) -> dict[str, str]:
    """The ``CI_*="..."`` constants ``bin/brain-ci`` declares."""
    return dict(CONTRACT_RE.findall(script_text))


def _env_blocks(workflow: dict[str, Any]) -> list[dict[str, Any]]:
    """Every ``env:`` mapping in the workflow — top level, per job, per step."""
    blocks: list[dict[str, Any]] = []
    if isinstance(workflow.get("env"), dict):
        blocks.append(workflow["env"])
    for job in workflow.get("jobs", {}).values():
        if isinstance(job.get("env"), dict):
            blocks.append(job["env"])
        for step in job.get("steps", []):
            if isinstance(step, dict) and isinstance(step.get("env"), dict):
                blocks.append(step["env"])
    return blocks


def _workflow_facts(workflow_text: str) -> dict[str, Any]:
    """The handful of things ``bin/brain-ci`` must keep in step with ci.yml."""
    workflow = yaml.safe_load(workflow_text)
    assert isinstance(workflow, dict), "ci.yml did not parse as a YAML mapping"

    db_urls = {
        str(value)
        for env in _env_blocks(workflow)
        for key, value in env.items()
        if key in {"DATABASE_URL", "TEST_DATABASE_URL"}
    }
    python_versions = {
        str(step["with"]["python-version"])
        for job in workflow.get("jobs", {}).values()
        for step in job.get("steps", [])
        if isinstance(step, dict)
        and str(step.get("uses", "")).startswith("actions/setup-python")
        and isinstance(step.get("with"), dict)
        and "python-version" in step["with"]
    }
    runs = "\n".join(
        str(step["run"])
        for job in workflow.get("jobs", {}).values()
        for step in job.get("steps", [])
        if isinstance(step, dict) and "run" in step
    )
    pg = PG_ISREADY_RE.search(runs)
    seq = SEQ_RE.search(runs)
    sleep = SLEEP_RE.search(runs)
    venv = VENV_DIR_RE.search(runs)
    return {
        "db_urls": db_urls,
        "python_versions": python_versions,
        "compose_files": set(COMPOSE_FILE_RE.findall(runs)),
        "pg_isready": pg.groupdict() if pg else None,
        "wait_tries": seq.group(1) if seq else None,
        "wait_sleep": sleep.group(1) if sleep else None,
        "venv_dir": venv.group(1) if venv else None,
    }


def find_drift(workflow_text: str, script_text: str) -> list[str]:
    """Every disagreement between ci.yml and ``bin/brain-ci``'s contract block.

    Returns human-readable complaints; an empty list means the two agree. Pure
    on purpose — the mutation tests below feed it doctored text.
    """
    facts = _workflow_facts(workflow_text)
    contract = _contract(script_text)
    drift: list[str] = []

    def expect(key: str, actual: object, what: str) -> None:
        if key not in contract:
            drift.append(f"bin/brain-ci declares no {key}")
        elif str(actual) != contract[key]:
            drift.append(f"{what}: ci.yml has {actual!r}, bin/brain-ci has {contract[key]!r}")

    if not facts["db_urls"]:
        drift.append("ci.yml pins no DATABASE_URL/TEST_DATABASE_URL")
    elif len(facts["db_urls"]) > 1:
        drift.append(f"ci.yml uses more than one database URL: {sorted(facts['db_urls'])}")
    else:
        expect("CI_DATABASE_URL", next(iter(facts["db_urls"])), "database URL")

    if not facts["python_versions"]:
        drift.append("ci.yml pins no actions/setup-python python-version")
    elif len(facts["python_versions"]) > 1:
        drift.append(f"ci.yml uses more than one Python: {sorted(facts['python_versions'])}")
    else:
        expect("CI_PYTHON_VERSION", next(iter(facts["python_versions"])), "Python version")

    if not facts["compose_files"]:
        drift.append("ci.yml runs no `docker compose -f <file>`")
    elif len(facts["compose_files"]) > 1:
        drift.append(f"ci.yml drives more than one compose file: {sorted(facts['compose_files'])}")
    else:
        expect("CI_COMPOSE_FILE", next(iter(facts["compose_files"])), "compose file")

    if facts["pg_isready"] is None:
        drift.append("ci.yml has no `pg_isready -h ... -p ... -U ... -d ...` readiness probe")
    else:
        expect("CI_PG_HOST", facts["pg_isready"]["host"], "pg_isready host")
        expect("CI_PG_PORT", facts["pg_isready"]["port"], "pg_isready port")
        expect("CI_PG_USER", facts["pg_isready"]["user"], "pg_isready user")
        expect("CI_PG_DB", facts["pg_isready"]["db"], "pg_isready database")

    if facts["venv_dir"] is None:
        drift.append("ci.yml no longer runs `python -m venv <dir>`")
    else:
        expect("CI_VENV_DIR", facts["venv_dir"], "venv directory")

    if facts["wait_tries"] is None:
        drift.append("ci.yml readiness loop no longer uses `seq 1 <n>`")
    else:
        expect("CI_WAIT_TRIES", facts["wait_tries"], "readiness attempts")

    if facts["wait_sleep"] is None:
        drift.append("ci.yml readiness loop no longer sleeps between attempts")
    else:
        expect("CI_WAIT_SLEEP", facts["wait_sleep"], "readiness sleep")

    return drift


# ---------------------------------------------------------------------------
# The script exists, and agrees with the workflow it claims to reproduce.
# ---------------------------------------------------------------------------


def test_brain_ci_script_exists_and_is_executable() -> None:
    assert SCRIPT.is_file(), (
        "bin/brain-ci is gone — there is no faithful local reproduction of the "
        "CI gate, and the local-vs-CI divergence it was written to kill comes back."
    )
    assert os.access(SCRIPT, os.X_OK), f"{SCRIPT} is not executable"


def test_brain_ci_matches_the_ci_workflow() -> None:
    """The whole point: ci.yml and bin/brain-ci must not drift apart."""
    drift = find_drift(_workflow_text(), _script_text())

    assert not drift, "bin/brain-ci has drifted from .github/workflows/ci.yml:\n  " + "\n  ".join(
        drift
    )


# The DSN ci.yml pins, read OUT of ci.yml rather than repeated here. Two
# reasons: a second copy of the string would be one more thing to drift, and
# tests/test_database_url_isolation.py forbids a test module from pinning a
# `second_brain*test*` DSN literal at all (it would ignore a TEST_DATABASE_URL
# override). Nothing in this module ever connects — the URL is only ever
# compared as text.
CI_DB_URL = next(iter(_workflow_facts(_workflow_text())["db_urls"]), "")


@pytest.mark.parametrize(
    ("old", "new", "expected_fragment"),
    [
        # The DB the gate talks to (port swapped; still a *_test database, so the
        # complaint is about drift, not about safety).
        (CI_DB_URL, CI_DB_URL.replace("5434", "5439"), "database URL"),
        # The Python minor. 3.11 vs the checkout's 3.14 was one of the real bugs.
        ('python-version: "3.11"', 'python-version: "3.12"', "Python version"),
        # The compose file that brings up the AGE test instance.
        ("docker-compose.age-test.yml", "docker-compose.other.yml", "compose file"),
        # The readiness probe's target.
        ("-p 5434 -U brain", "-p 5999 -U brain", "pg_isready port"),
        # The venv layout the bin/ wrappers depend on.
        ("python -m venv .venv", "python -m venv .venv-ci", "venv directory"),
    ],
)
def test_workflow_drift_is_detected(old: str, new: str, expected_fragment: str) -> None:
    """Mutate ci.yml, and the checker must complain.

    Without this, ``find_drift`` could return ``[]`` for every input — a green
    test proving nothing. Each case also asserts the mutation actually applied,
    so a silently-missed substring cannot masquerade as a passing check.
    """
    workflow_text = _workflow_text()
    assert old in workflow_text, f"fixture is stale: ci.yml no longer contains {old!r}"
    mutated = workflow_text.replace(old, new)
    assert mutated != workflow_text, "mutation did not apply"

    drift = find_drift(mutated, _script_text())

    assert any(expected_fragment in complaint for complaint in drift), (
        f"mutating ci.yml ({old!r} -> {new!r}) produced no {expected_fragment!r} "
        f"complaint; got {drift}"
    )


def test_script_drift_is_detected() -> None:
    """Mutating the SCRIPT is caught too — drift has two directions."""
    mutated = _script_text().replace(
        'CI_PG_PORT="5434"', 'CI_PG_PORT="55432"'
    )
    assert mutated != _script_text(), "fixture is stale: no CI_PG_PORT constant in bin/brain-ci"

    drift = find_drift(_workflow_text(), mutated)

    assert any("pg_isready port" in complaint for complaint in drift), drift


# ---------------------------------------------------------------------------
# The gate the script runs must be the gate CI runs.
# ---------------------------------------------------------------------------


def _pytest_invocations(script_text: str) -> list[str]:
    """Lines that RUN pytest — not comments, and not `[[ -x ... ]]` guards.

    The first version of this helper matched any line containing
    ``/bin/pytest``, which swept in the ``[[ -x "$VENV/bin/pytest" ]]``
    precondition and then flagged its ``-x`` and ``||`` as a smuggled pytest
    flag and a pipe. A test expression is not an invocation.
    """
    invocations = []
    for raw in script_text.splitlines():
        line = raw.strip()
        if "bin/pytest" not in line or line.startswith(("#", "[[")):
            continue
        invocations.append(line)
    return invocations


def _code_lines(script_text: str) -> list[str]:
    """Non-comment, non-blank lines. Comments may discuss what code may not do."""
    return [
        line.strip()
        for line in script_text.splitlines()
        if line.strip() and not line.strip().startswith("#")
    ]


def test_script_runs_an_unflagged_pytest() -> None:
    """pyproject.toml's ``addopts`` owns the marker filter and coverage floor.

    ci.yml runs a bare ``pytest`` deliberately (see
    tests/test_ci_workflow.py). A local reproduction that quietly adds ``-x``,
    ``-m``, or ``--no-cov`` would report a pass the real gate would not.
    """
    invocations = _pytest_invocations(_script_text())

    assert invocations, "bin/brain-ci never invokes pytest"
    for line in invocations:
        for flag in ("--no-cov", "--cov-fail-under", " -m ", " -k ", " -x ", " -p "):
            assert flag not in line, f"bin/brain-ci adds {flag!r} to its pytest run: {line!r}"


def test_script_never_hollows_out_the_coverage_gate() -> None:
    """Not one mention anywhere — including in a helper or an exported variable.

    ``PYTEST_ADDOPTS`` is APPENDED to ``addopts`` by pytest itself, so an export
    is a complete bypass of the floor without touching the pytest command line.
    The script may only ever ``unset`` it, never assign it.
    """
    text = _script_text()
    for flag in GATE_HOLLOWING_FLAGS:
        for line in text.splitlines():
            stripped = line.strip()
            if flag not in stripped:
                continue
            # A comment explaining the rule, or the `unset` that enforces it,
            # are the only legitimate mentions.
            assert stripped.startswith("#") or stripped.startswith("unset "), (
                f"bin/brain-ci sets {flag} (or passes it through): {stripped!r}"
            )


def test_script_never_pipes_pytest() -> None:
    """A pipeline reports the LAST command's status, not pytest's.

    A wrapper that ended in ``| tail`` reported tail's success three times, once
    while a release was being cut. The script must run pytest bare and re-export
    its raw status.
    """
    for line in _pytest_invocations(_script_text()):
        assert "|" not in line, (
            f"bin/brain-ci pipes its pytest run, so the exit status reported is the "
            f"pipeline's, not pytest's: {line!r}"
        )


def test_script_exits_with_pytests_own_status() -> None:
    """The captured status has to actually reach ``exit``."""
    text = _script_text()

    assert "PYTEST_STATUS=$?" in text, "bin/brain-ci never captures pytest's exit status"
    assert "EXIT_STATUS=$PYTEST_STATUS" in text, (
        "bin/brain-ci captures pytest's status but never propagates it to its own exit"
    )
    assert 'exit "$EXIT_STATUS"' in text, "bin/brain-ci does not exit with the computed status"


# ---------------------------------------------------------------------------
# The CI *environment* — the divergence a version-matched venv still missed.
# ---------------------------------------------------------------------------


def test_script_exports_the_github_actions_environment() -> None:
    """``GITHUB_ACTIONS`` is not a package, and that is exactly why it bit us.

    Typer binds ``rich_utils.FORCE_TERMINAL`` at import time from
    ``GITHUB_ACTIONS``/``FORCE_COLOR``/``PY_COLORS``. A CI-repro venv that
    matched every pinned version still could not reproduce the 20 failures,
    because the trigger was an environment variable.

    That bug is now fixed at the source — ``tests/__init__.py`` sets Typer's
    private ``_TYPER_FORCE_DISABLE_TERMINAL`` before anything imports Typer — so
    these exports are not what makes help text render identically. They are
    pinned because the neutraliser rides a PRIVATE API behind a range pin
    (``typer>=0.26,<0.28``): when an upgrade breaks it, the breakage appears
    only where ``GITHUB_ACTIONS`` is set. Drop these and that failure goes back
    to being CI-only, which is the whole disease.
    """
    text = _script_text()

    assert "export GITHUB_ACTIONS=true" in text, (
        "bin/brain-ci does not set GITHUB_ACTIONS. Reproducing CI's dependencies "
        "without CI's environment is what let 20 CLI tests fail only in CI for a "
        "whole release cycle."
    )
    assert "export CI=true" in text, "bin/brain-ci does not set CI=true"


def test_script_scrubs_local_only_colour_and_width_overrides() -> None:
    """A developer shell carries knobs a runner does not; leaving them masks bugs."""
    text = _script_text()
    unset_lines = " ".join(line for line in text.splitlines() if line.strip().startswith("unset "))

    for name in ("FORCE_COLOR", "PY_COLORS", "COLUMNS", "PYTEST_ADDOPTS"):
        assert name in unset_lines, f"bin/brain-ci does not unset {name}"


def test_script_installs_into_the_repo_local_venv() -> None:
    """``.venv`` at the repo root is load-bearing, not incidental.

    ``bin/brain-{up,down,status,rebuild}`` exec ``<repo>/.venv/bin/<script>``
    and tests/test_bin_scripts.py asserts on it, so a run against a venv
    anywhere else exercises a layout CI does not have.
    """
    contract = _contract(_script_text())

    assert contract.get("CI_VENV_DIR") == ".venv"
    assert 'VENV="$REPO_ROOT/$CI_VENV_DIR"' in _script_text()


def test_interpreter_lookup_never_resolves_into_the_venv_it_deletes() -> None:
    """Regression: `uv python find` searches virtual environments FIRST.

    Found on the second consecutive full run. Once ``bin/brain-ci`` had built a
    3.11 ``.venv``, a bare ``uv python find 3.11`` resolved to
    ``<repo>/.venv/bin/python3`` — the venv the very next step deletes — and the
    run died with ``python -m venv .venv failed`` / "No such file or
    directory". The first run passed only because the pre-existing venv was on
    3.14 and therefore did not match the request, which is precisely the kind of
    state-dependent pass this script exists to stop trusting.

    Two things keep it fixed: ``--system --no-project`` on every lookup, and a
    defensive rejection of any interpreter path inside ``$VENV``.
    """
    text = _script_text()
    lookups = [line.strip() for line in text.splitlines() if "uv python find" in line]

    assert lookups, "bin/brain-ci no longer resolves the CI interpreter with uv"
    for line in lookups:
        if line.startswith("#"):
            continue
        assert "--system" in line and "--no-project" in line, (
            f"`uv python find` without --system --no-project resolves to the .venv "
            f"this script is about to delete: {line!r}"
        )
    assert '"$found" == "$VENV/"*' in text, (
        "bin/brain-ci lost the guard rejecting an interpreter inside the venv it deletes"
    )


def test_script_rebuilds_the_venv_by_default() -> None:
    """A stale resolve is the failure mode; reuse must be opt-in and announced."""
    text = _script_text()

    assert "REUSE_VENV=0" in text, "bin/brain-ci reuses the existing .venv by default"
    assert "--reuse-venv" in text, "bin/brain-ci offers no fast-path flag"
    assert "mark_not_the_gate" in text, (
        "bin/brain-ci has no mechanism for announcing that a run is not the gate"
    )


# ---------------------------------------------------------------------------
# Database safety — port 55432 is PRODUCTION.
# ---------------------------------------------------------------------------


def test_script_targets_only_the_test_database() -> None:
    contract = _contract(_script_text())
    url = contract["CI_DATABASE_URL"]

    assert ":5434/" in url, f"bin/brain-ci is not on the test port 5434: {url}"
    assert url.endswith("_test"), f"bin/brain-ci does not name a *_test database: {url}"
    assert "55432" not in url, f"bin/brain-ci targets the production port: {url}"


def test_script_never_touches_the_production_bind_mount() -> None:
    """The prod corpus lives in ./data/postgres, a host bind-mount."""
    code = _code_lines(_script_text())

    for line in code:
        assert "data/postgres" not in line, (
            f"bin/brain-ci touches the production data directory: {line!r}"
        )
        if "docker compose" in line:
            # Every real docker invocation goes through the `compose()` wrapper,
            # which pins `-f "$CI_COMPOSE_FILE"` — the TEST project, with its own
            # named volume. An unpinned `docker compose down -v` in this repo
            # would target docker-compose.yml, i.e. production.
            assert "CI_COMPOSE_FILE" in line, (
                f"bin/brain-ci runs docker compose without pinning the test "
                f"compose file: {line!r}"
            )
        if "down -v" in line:
            assert "compose down -v" in line, (
                f"bin/brain-ci runs `down -v` outside the test compose project: {line!r}"
            )


def test_script_refuses_a_production_database_url(tmp_path: Path) -> None:
    """Mutation proof: point the contract at prod and the script must refuse.

    Run as a real subprocess against a COPY, so the guard is exercised rather
    than merely read. The copy dies at the safety check before it touches the
    filesystem, Docker, or any database.
    """
    # Both DSNs are derived from ci.yml's, never spelled out: the prod one so it
    # cannot rot, the test one for the reason given at CI_DB_URL above.
    prod_url = CI_DB_URL.replace("5434", "55432").replace("second_brain_test", "second_brain")
    copy = tmp_path / "brain-ci"
    copy.write_text(
        _script_text().replace(
            f'CI_DATABASE_URL="{CI_DB_URL}"',
            f'CI_DATABASE_URL="{prod_url}"',
        ),
        encoding="utf-8",
    )
    assert prod_url in copy.read_text(encoding="utf-8"), "mutation did not apply"
    copy.chmod(0o755)

    result = subprocess.run(  # noqa: S603 — list-form, no shell
        [str(copy), "--lint-only"],
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )

    assert result.returncode == 70, (
        f"a production database URL did not stop the run "
        f"(exit {result.returncode})\nstdout:\n{result.stdout}\nstderr:\n{result.stderr}"
    )
    assert "PRODUCTION" in result.stderr


# ---------------------------------------------------------------------------
# Behaviour that needs no venv, no Docker, and no database.
# ---------------------------------------------------------------------------


def test_script_parses_as_bash() -> None:
    bash = shutil.which("bash") or "/bin/bash"
    result = subprocess.run(  # noqa: S603 — list-form, no shell
        [bash, "-n", str(SCRIPT)],
        capture_output=True,
        text=True,
        timeout=10,
        check=False,
    )

    assert result.returncode == 0, result.stderr


def test_unknown_flag_is_a_usage_error() -> None:
    result = subprocess.run(  # noqa: S603 — list-form, no shell
        [str(SCRIPT), "--definitely-not-a-flag"],
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )

    assert result.returncode == 64, result.stderr
    assert "unknown option" in result.stderr


def test_help_documents_the_default_as_the_faithful_run() -> None:
    result = subprocess.run(  # noqa: S603 — list-form, no shell
        [str(SCRIPT), "--help"],
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    assert "NOT the gate" in result.stdout, "--help does not warn which modes are not the gate"
    assert "5434" in result.stdout
