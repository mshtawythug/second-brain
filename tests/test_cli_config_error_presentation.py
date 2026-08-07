"""A bad ``BRAIN_*`` env var reads like a bad flag, not like a crash (#24).

`Config.load()` always produced a good *message*; nothing caught it, so the
user got a Rich traceback and exit 1. Meanwhile a typo in a flag —
``brain list --sensitivity confidentail`` — printed a tidy boxed usage error
and exited 2. Same class of mistake, two very different experiences, and the
env-var path is about to get much more traffic now that
``docs/configuration.md`` teaches these variables by name.

:class:`brain.cli_errors.BrainGroup` fixes it in one place rather than in ~50
per-command ``except ConfigError`` blocks — N call sites drifting apart is
precisely how the two presentations diverged.

The last test here is the important one: a **genuine bug must still
traceback**. A handler that swallowed everything would trade one bad
experience for a much worse one.

All fixture data is synthetic.
"""
from __future__ import annotations

import typer
from typer.testing import CliRunner

from brain.cli_errors import BrainGroup
from brain.config import Config, ConfigError

#: A DSN that parses but is never connected to — these tests never touch a DB.
_UNUSED_DSN = "postgresql://brain:brain@localhost:5434/unused"


def _app() -> typer.Typer:
    """A two-command app wired with the group under test.

    Two commands, not one: Typer collapses a single-command app into a bare
    command, which changes the usage line and masks the error message. The
    real ``brain`` CLI has ~50, so two is the shape that actually matches it.
    """
    app = typer.Typer(cls=BrainGroup)

    @app.command()
    def probe() -> None:
        Config.load()
        typer.echo("config loaded")

    @app.command()
    def other() -> None:
        typer.echo("other")

    return app


def _run(args: list[str], env: dict[str, str]) -> object:
    return CliRunner(env=env).invoke(_app(), args)


def test_a_valid_environment_still_works() -> None:
    """The guard must not intercept the happy path."""
    result = _run(["probe"], {"DATABASE_URL": _UNUSED_DSN})

    assert result.exit_code == 0, result.output
    assert "config loaded" in result.stdout


def test_a_bad_env_var_exits_2_like_a_bad_flag() -> None:
    """Exit 2 is Typer's "you invoked this wrong" code, and this is that."""
    result = _run(
        ["probe"],
        {"DATABASE_URL": _UNUSED_DSN, "BRAIN_SECRET_GUARD": "block"},
    )

    assert result.exit_code == 2


def test_the_message_survives_intact() -> None:
    """The diagnosis was never the problem — only the presentation was.

    Naming the variable, the allowed values AND the offending input is what
    makes it fixable without reading the source.
    """
    result = _run(
        ["probe"],
        {"DATABASE_URL": _UNUSED_DSN, "BRAIN_SECRET_GUARD": "block"},
    )

    assert "BRAIN_SECRET_GUARD" in result.output
    assert "warn/redact/reject/off" in result.output
    assert "block" in result.output


def test_no_traceback_is_shown() -> None:
    """The whole point: a typo is not a crash."""
    result = _run(
        ["probe"],
        {"DATABASE_URL": _UNUSED_DSN, "BRAIN_SECRET_GUARD": "block"},
    )

    assert "Traceback" not in result.output
    assert "ConfigError" not in result.output


def test_it_generalizes_beyond_one_variable() -> None:
    """#24 is general — it reproduces on ``BRAIN_AGENT_ID`` too.

    A per-variable fix would have left every other ``BRAIN_*`` still crashing,
    which is why this lives at the group rather than at one parse site.
    """
    result = _run(
        ["probe"],
        {"DATABASE_URL": _UNUSED_DSN, "BRAIN_AGENT_ID": "-leading-hyphen"},
    )

    assert result.exit_code == 2
    assert "BRAIN_AGENT_ID" in result.output
    assert "Traceback" not in result.output


def test_sub_app_commands_are_covered_too() -> None:
    """Sub-group invocation nests inside the parent's, so one handler suffices.

    ``brain`` has several sub-apps (``vault``, ``note``, ``graphrag``); if the
    handler only covered top-level commands the fix would be half a fix.
    """
    app = typer.Typer(cls=BrainGroup)
    sub = typer.Typer()

    @app.command()
    def top() -> None:
        typer.echo("top")

    @sub.command("inner")
    def inner() -> None:
        Config.load()

    app.add_typer(sub, name="sub")

    result = CliRunner(
        env={"DATABASE_URL": _UNUSED_DSN, "BRAIN_SECRET_GUARD": "block"}
    ).invoke(app, ["sub", "inner"])

    assert result.exit_code == 2
    assert "BRAIN_SECRET_GUARD" in result.output
    assert "Traceback" not in result.output


def test_a_genuine_bug_still_raises() -> None:
    """Load-bearing: the handler must catch ``ConfigError`` and nothing else.

    Swallowing every exception would turn real defects into tidy usage errors
    — a far worse outcome than the traceback this ticket set out to remove.
    """
    app = typer.Typer(cls=BrainGroup)

    @app.command()
    def boom() -> None:
        raise RuntimeError("a real bug")

    @app.command()
    def other() -> None:
        typer.echo("other")

    result = CliRunner(env={"DATABASE_URL": _UNUSED_DSN}).invoke(app, ["boom"])

    assert result.exit_code == 1
    assert isinstance(result.exception, RuntimeError)


def test_config_error_is_not_a_click_exception() -> None:
    """The library stays framework-free; only the CLI layer knows about Click.

    Making ``ConfigError`` inherit ``click.ClickException`` would also fix the
    presentation — and would couple ``config.py`` to a CLI framework that the
    MCP server and ``brain ui`` do not use.
    """
    import click

    assert not issubclass(ConfigError, click.ClickException)
