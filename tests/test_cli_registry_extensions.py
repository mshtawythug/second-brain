"""RM-06: the ``cli_registry`` extension seam for new CLI commands.

A later command module adds one import and one ``EXTENSION_REGISTRARS`` entry in
``cli_registry.py``; ``cli.py`` calls :func:`~brain.cli_registry.register_extensions` once,
after its last registration. Typer lists every ``@app.command`` before every ``add_typer``
group (registration order within each kind), so "last" means: an extension command lands after
every built-in command, and an extension sub-app after every built-in sub-app.
"""

from __future__ import annotations

import json
import subprocess
import sys
import textwrap

import pytest
import typer
import typer.main
from typer.testing import CliRunner

from brain import cli_registry


def _builtin_app() -> typer.Typer:
    """A throwaway app with built-in commands and a sub-app, in mixed order."""
    app = typer.Typer()

    @app.command("mmm-first")
    def first() -> None:
        """A built-in command."""

    group = typer.Typer()

    @group.command("inner")
    def inner() -> None:
        """A built-in sub-app's command."""

    app.add_typer(group, name="nnn-group")

    @app.command("ooo-last")
    def last() -> None:
        """Another built-in command."""

    return app


def _extension(app: typer.Typer) -> None:
    """A test-only extension: one command and one sub-app, named to sort FIRST."""

    @app.command("aaa-ext")
    def ext() -> None:
        """An extension command."""

    group = typer.Typer()

    @group.command("inner")
    def inner() -> None:
        """An extension sub-app's command."""

    app.add_typer(group, name="aaa-ext-group")


def _help_command_names(app: typer.Typer) -> list[str]:
    """Command names in the order ``--help`` renders them."""
    result = CliRunner().invoke(app, ["--help"], env={"COLUMNS": "120", "NO_COLOR": "1"})
    assert result.exit_code == 0, result.output
    names: list[str] = []
    in_commands = False
    for line in result.output.splitlines():
        if "Commands" in line and line.lstrip().startswith("╭"):
            in_commands = True
            continue
        if in_commands and line.lstrip().startswith("╰"):
            break
        if in_commands:
            cells = line.strip().strip("│").split()
            if cells:
                names.append(cells[0])
    return names


def test_extension_command_lists_after_every_builtin_command(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Arrange
    app = _builtin_app()
    monkeypatch.setattr(cli_registry, "EXTENSION_REGISTRARS", (("probe", _extension),))

    # Act
    cli_registry.register_extensions(app)

    # Assert
    names = _help_command_names(app)
    assert names == ["mmm-first", "ooo-last", "aaa-ext", "nnn-group", "aaa-ext-group"]


def test_shipped_extension_tuple_registers_exactly_the_shipped_sub_apps() -> None:
    """The shipped tuple: RM-15's ``watch``, RM-19's ``dedup``, RM-21's ``tags`` and RM-31's
    ``ingest-github``.

    RM-06 shipped the tuple empty and this test pinned that; RM-15 is the first module behind
    the seam, RM-19 the second, RM-21 the third, RM-31 the fourth. A new extension module
    updates the expected names here. ``ingest-github`` is a command, not a sub-app, so it
    lists after the built-in commands and before every sub-app.
    """
    # Arrange
    app = _builtin_app()
    before = _help_command_names(app)

    # Act
    cli_registry.register_extensions(app)

    # Assert
    assert [name for name, _ in cli_registry.EXTENSION_REGISTRARS] == [
        "watch",
        "dedup",
        "tags",
        "ingest-github",
    ]
    assert before == ["mmm-first", "ooo-last", "nnn-group"]
    assert _help_command_names(app) == [
        "mmm-first",
        "ooo-last",
        "ingest-github",
        "nnn-group",
        "watch",
        "dedup",
        "tags",
    ]


def test_brain_cli_calls_the_seam_after_its_last_registration() -> None:
    """The real ``brain`` app runs the extension registrars, and lists them last.

    A clean interpreter, because ``brain.cli`` registers at import time; ``mock.patch`` (a
    standard test double that restores itself) supplies an extension before that import.
    """
    script = textwrap.dedent(
        """
        import json
        from unittest import mock

        import typer
        import typer.main

        import brain.cli_registry as registry


        def extension(app):
            @app.command("aaa-rm06-probe")
            def probe():
                pass


        with mock.patch.object(registry, "EXTENSION_REGISTRARS", (("probe", extension),)):
            import brain.cli

        root = typer.main.get_command(brain.cli.app)
        rows = [
            (name, hasattr(root.get_command(None, name), "list_commands"))
            for name in root.list_commands(None)
        ]
        print(json.dumps(rows))
        """
    )

    # Act
    completed = subprocess.run(
        [sys.executable, "-c", script],
        capture_output=True,
        text=True,
        timeout=60,
        check=True,
    )

    # Assert
    rows = json.loads(completed.stdout)
    commands = [name for name, is_group in rows if not is_group]
    assert commands[-1] == "aaa-rm06-probe", commands
    assert len(commands) > 1
