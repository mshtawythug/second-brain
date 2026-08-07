"""CLI tests for the root ``brain --version`` flag.

``SECURITY.md`` asks vulnerability reporters to include the output of
``brain --version``, and a package published to PyPI needs a way to report the
installed version. Neither worked before this flag existed: ``brain --version``
exited as an unknown-option usage error.
"""
from __future__ import annotations

from importlib.metadata import version as _dist_version

from typer.testing import CliRunner

import brain.cli as cli

runner = CliRunner()

DIST_NAME = "secondbrain-py"


def test_version_flag_exits_zero_and_prints_the_installed_version() -> None:
    """``brain --version`` reports the installed distribution version."""
    # Arrange
    expected = _dist_version(DIST_NAME)

    # Act
    result = runner.invoke(cli.app, ["--version"])

    # Assert
    assert result.exit_code == 0, result.output
    assert expected in result.output


def test_version_flag_is_eager_and_needs_no_subcommand() -> None:
    """The flag short-circuits before Typer demands a subcommand.

    Without ``is_eager=True`` Typer resolves the missing-command error first and
    exits non-zero, which is exactly the pre-fix behavior.
    """
    # Act
    result = runner.invoke(cli.app, ["--version"])

    # Assert
    assert result.exit_code == 0, result.output
    assert "Usage:" not in result.output
    assert "Missing command" not in result.output


def test_short_flag_matches_long_flag() -> None:
    """``-V`` is accepted as the short form and prints the same string."""
    # Act
    long_form = runner.invoke(cli.app, ["--version"])
    short_form = runner.invoke(cli.app, ["-V"])

    # Assert
    assert short_form.exit_code == 0, short_form.output
    assert short_form.output == long_form.output


def test_version_output_is_a_single_line_naming_the_tool() -> None:
    """Output stays greppable: one line, the tool name, then the version."""
    # Act
    result = runner.invoke(cli.app, ["--version"])

    # Assert
    lines = [line for line in result.output.splitlines() if line.strip()]
    assert len(lines) == 1, result.output
    assert lines[0].startswith("brain ")
