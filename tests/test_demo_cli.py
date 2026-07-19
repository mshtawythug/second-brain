"""CLI tests for the `brain demo` sub-app.

Search-path tests run against the real test Postgres via ``--database-url`` (no
Docker in CI); status/teardown mock the single subprocess boundary
(:func:`brain.demo._run` and its helpers) with ``pytest-mock`` — a standard
test double, not monkey-patching production code.
"""
import subprocess
from pathlib import Path

import psycopg
import pytest
from pytest_mock import MockerFixture
from typer.testing import CliRunner

from brain.cli_demo import _DOCKER_MISSING_MSG, demo_app
from brain.demo import seed_demo
from tests.conftest import TEST_DATABASE_URL

runner = CliRunner()

HERO_QUERY = "compliance horror stories"
HERO_DOC_TITLE = "Compliance Horror Stories — collected war stories"


@pytest.mark.fresh_schema
def test_default_flow_seeds_and_runs_hero_query(test_db: psycopg.Connection) -> None:
    """`brain demo --database-url X` seeds + prints ranked hero results."""
    result = runner.invoke(demo_app, ["--database-url", TEST_DATABASE_URL])

    assert result.exit_code == 0, result.output
    assert "Seeded 22 new doc(s)" in result.output
    assert "Try these next" in result.output
    # A follow-up suggestion + the rendered results table both appear. (The
    # title text itself is asserted in the --json query test below — Rich wraps
    # table cells at the CliRunner's 80-col width, so it isn't a stable check.)
    assert "brain demo query" in result.output
    assert "Snippet" in result.output


@pytest.mark.fresh_schema
def test_default_flow_is_idempotent(test_db: psycopg.Connection) -> None:
    """Re-running the default flow re-seeds nothing (dedup)."""
    first = runner.invoke(demo_app, ["--database-url", TEST_DATABASE_URL])
    assert first.exit_code == 0, first.output

    second = runner.invoke(demo_app, ["--database-url", TEST_DATABASE_URL])
    assert second.exit_code == 0, second.output
    assert "Seeded 0 new doc(s)" in second.output


@pytest.mark.fresh_schema
def test_query_command_via_database_url(test_db: psycopg.Connection) -> None:
    seed_demo(TEST_DATABASE_URL)

    result = runner.invoke(
        demo_app,
        ["query", HERO_QUERY, "--database-url", TEST_DATABASE_URL, "--json"],
    )

    assert result.exit_code == 0, result.output
    assert HERO_DOC_TITLE in result.output


@pytest.mark.fresh_schema
def test_query_source_filter_via_database_url(test_db: psycopg.Connection) -> None:
    seed_demo(TEST_DATABASE_URL)

    result = runner.invoke(
        demo_app,
        [
            "query", HERO_QUERY,
            "--source", "slack",
            "--database-url", TEST_DATABASE_URL,
            "--json",
        ],
    )

    assert result.exit_code == 0, result.output
    # JSON payload present and only slack docs came back.
    assert '"source_kind": "slack"' in result.output
    assert '"source_kind": "gmail"' not in result.output


@pytest.mark.fresh_schema
def test_query_no_results_is_clean(test_db: psycopg.Connection) -> None:
    seed_demo(TEST_DATABASE_URL)

    result = runner.invoke(
        demo_app,
        ["query", "zzznonexistentqueryzzz", "--database-url", TEST_DATABASE_URL],
    )

    assert result.exit_code == 0, result.output
    assert "(no results)" in result.output


def test_default_flow_without_docker_exits_one(mocker: MockerFixture) -> None:
    """No --database-url + no Docker → exit 1 with actionable guidance."""
    mocker.patch("brain.cli_demo.shutil.which", return_value=None)

    result = runner.invoke(demo_app, [])

    assert result.exit_code == 1
    assert _DOCKER_MISSING_MSG in result.output


def test_status_not_running(mocker: MockerFixture) -> None:
    # docker ps returns no matching container → not running (no DB connect).
    mocker.patch(
        "brain.demo._run",
        return_value=subprocess.CompletedProcess(args=[], returncode=0, stdout="", stderr=""),
    )

    result = runner.invoke(demo_app, ["status"])

    assert result.exit_code == 0, result.output
    assert "not running" in result.output


def test_status_running_reports_doc_count(mocker: MockerFixture) -> None:
    mocker.patch("brain.demo._container_running", return_value=True)
    mocker.patch("brain.demo._count_documents", return_value=22)

    result = runner.invoke(demo_app, ["status"])

    assert result.exit_code == 0, result.output
    assert "running" in result.output
    assert "22 doc(s)" in result.output


def test_teardown_invokes_compose_down_v(mocker: MockerFixture) -> None:
    run_mock = mocker.patch(
        "brain.demo._run",
        return_value=subprocess.CompletedProcess(args=[], returncode=0, stdout="", stderr=""),
    )

    result = runner.invoke(demo_app, ["teardown"])

    assert result.exit_code == 0, result.output
    assert "torn down" in result.output
    called_args = run_mock.call_args.args[0]
    assert called_args[:4] == ["docker", "compose", "-p", "brain-demo"]
    assert called_args[-2:] == ["down", "-v"]


def _assert_clean_failure(result: object) -> None:
    """No raw traceback leaked — a clean Typer/SystemExit exit only."""
    exc = getattr(result, "exception", None)
    assert exc is None or isinstance(exc, SystemExit), f"leaked exception: {exc!r}"
    assert "Traceback" not in getattr(result, "output", "")


# --- M3: --port must be range-validated, not overflow deep in a socket bind ----


def test_port_out_of_range_is_rejected_cleanly() -> None:
    result = runner.invoke(demo_app, ["--port", "99999"])

    assert result.exit_code == 2  # Typer BadParameter
    assert "OverflowError" not in result.output
    _assert_clean_failure(result)


# --- I1: docker provisioning failures must be actionable, not raw tracebacks ---


def _patch_docker_present(mocker: MockerFixture, tmp_path: Path) -> None:
    """Make the default flow attempt provisioning against a mocked `_run`."""
    mocker.patch("brain.cli_demo.shutil.which", return_value="/usr/bin/docker")
    mocker.patch("brain.demo.DEMO_HOME", tmp_path)


def test_provision_docker_daemon_down_exits_clean(
    mocker: MockerFixture, tmp_path: Path
) -> None:
    _patch_docker_present(mocker, tmp_path)
    stderr = "Cannot connect to the Docker daemon at unix:///var/run/docker.sock"
    mocker.patch(
        "brain.demo._run",
        side_effect=subprocess.CalledProcessError(1, ["docker"], stderr=stderr),
    )

    result = runner.invoke(demo_app, [])

    assert result.exit_code == 1
    _assert_clean_failure(result)
    assert "Cannot connect to the Docker daemon" in result.output


def test_provision_docker_binary_missing_exits_clean(
    mocker: MockerFixture, tmp_path: Path
) -> None:
    _patch_docker_present(mocker, tmp_path)
    mocker.patch("brain.demo._run", side_effect=FileNotFoundError("docker"))

    result = runner.invoke(demo_app, [])

    assert result.exit_code == 1
    _assert_clean_failure(result)
    assert "Docker" in result.output


def test_provision_docker_timeout_exits_clean(
    mocker: MockerFixture, tmp_path: Path
) -> None:
    _patch_docker_present(mocker, tmp_path)
    mocker.patch(
        "brain.demo._run",
        side_effect=subprocess.TimeoutExpired(["docker"], 120),
    )

    result = runner.invoke(demo_app, [])

    assert result.exit_code == 1
    _assert_clean_failure(result)
    assert "timed out" in result.output.lower()


# --- M1: teardown must not traceback when docker is unavailable ---------------


def test_teardown_without_docker_reports_gracefully(mocker: MockerFixture) -> None:
    mocker.patch("brain.demo._run", side_effect=FileNotFoundError("docker"))

    result = runner.invoke(demo_app, ["teardown"])

    assert result.exit_code == 1
    _assert_clean_failure(result)
    assert "Docker" in result.output
