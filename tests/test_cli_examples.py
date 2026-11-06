"""RM-53: the `brain examples` command."""
from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path
from unittest import mock

import pytest
from typer.testing import CliRunner

from brain.cli import app
from brain.examples import ExamplesResourceError, ExampleTopic, load_examples

pytestmark = pytest.mark.nodb

_RUNNER = CliRunner()
_SLUGS = [topic.slug for topic in load_examples()]


@pytest.mark.parametrize("slug", _SLUGS)
def test_each_topic_prints_its_own_invocations_and_no_others(slug: str) -> None:
    # Act
    result = _RUNNER.invoke(app, ["examples", slug])

    # Assert
    assert result.exit_code == 0, result.output
    topics = {topic.slug: topic for topic in load_examples()}
    printed = result.stdout.splitlines()
    assert all(line in printed for line in topics[slug].invocations)
    others = {
        line
        for other, topic in topics.items()
        if other != slug
        for line in topic.invocations
    }
    assert not others & set(printed)


def test_bare_examples_prints_every_topic_in_order() -> None:
    result = _RUNNER.invoke(app, ["examples"])
    assert result.exit_code == 0, result.output
    printed = result.stdout.splitlines()
    topics = load_examples()
    assert all(line in printed for topic in topics for line in topic.invocations)
    positions = [printed.index(f"{topic.slug}: {topic.title}") for topic in topics]
    assert positions == sorted(positions)


def test_an_unknown_topic_lists_the_valid_ones_and_exits_2() -> None:
    result = _RUNNER.invoke(app, ["examples", "nosuchtopic"])
    assert result.exit_code == 2
    assert result.stdout == ""
    assert all(slug in result.stderr for slug in _SLUGS)


@pytest.mark.parametrize("hostile", ["\x1b]0;owned\x07", "../../etc/passwd", ""])
def test_an_unknown_topic_is_not_echoed_or_used_as_a_path(hostile: str) -> None:
    result = _RUNNER.invoke(app, ["examples", hostile])
    assert result.exit_code == 2
    assert "\x1b" not in result.output
    assert "owned" not in result.output
    assert "passwd" not in result.output


def test_a_broken_resource_is_one_fixed_line_and_exit_1() -> None:
    with mock.patch(
        "brain.cli_examples.load_examples",
        side_effect=ExamplesResourceError("line 3: /private/path/examples.md"),
    ):
        result = _RUNNER.invoke(app, ["examples"])
    assert result.exit_code == 1
    assert result.stdout == ""
    assert "reinstall" in result.stderr
    assert "/private/path" not in result.stderr


def test_output_is_plain_so_a_long_line_stays_whole_and_brackets_survive() -> None:
    # Arrange
    line = 'brain search "' + ("long " * 30) + '[not markup]"'
    topic = ExampleTopic(slug="a", title="A", lines=(line,))

    # Act
    with mock.patch("brain.cli_examples.load_examples", return_value=(topic,)):
        result = _RUNNER.invoke(app, ["examples", "a"], env={"COLUMNS": "40"})

    # Assert
    assert line in result.stdout.splitlines()


def _run_real(tmp_path: Path, *args: str) -> subprocess.CompletedProcess[str]:
    """The real CLI, with a config that cannot load and a database that cannot answer."""
    env = {
        **os.environ,
        "BRAIN_SECRET_GUARD": "block",
        "DATABASE_URL": "postgresql://localhost:1/none",
        "HOME": str(tmp_path),
        "BRAIN_HOME": str(tmp_path / "brain-home"),
    }
    return subprocess.run(
        [sys.executable, "-m", "brain", *args],
        capture_output=True,
        text=True,
        env=env,
        cwd=tmp_path,
        timeout=120,
        check=False,
    )


def test_the_real_command_needs_no_config_and_no_database(tmp_path: Path) -> None:
    result = _run_real(tmp_path, "examples", "search")
    assert result.returncode == 0, result.stderr
    assert any(line.startswith("brain search ") for line in result.stdout.splitlines())


def test_the_bad_variable_in_that_test_does_stop_a_command_that_loads_config(
    tmp_path: Path,
) -> None:
    """The control: without it the test above could pass because the variable did nothing."""
    result = _run_real(tmp_path, "status")
    assert result.returncode == 2
    assert "BRAIN_SECRET_GUARD" in result.stdout + result.stderr
