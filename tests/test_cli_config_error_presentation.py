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

import importlib.util
from importlib.metadata import version
from typing import Any

import pytest
import typer
import typer.main
from typer.core import TyperGroup
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


def _framework_usage_error() -> type[BaseException]:
    """The ``UsageError`` class the *installed* framework's runner recognises.

    Resolved by asking the framework rather than by importing a class by name,
    which is the whole point: the name moved between Typer releases and the
    test must not hard-code either answer.
    """
    ctx = typer.main.get_command(_app()).make_context("brain", ["probe"])
    try:
        ctx.fail("probe")
    except BaseException as exc:  # noqa: BLE001 — the class is what's under test
        return type(exc)
    raise AssertionError("ctx.fail() returned instead of raising")


def _typer_vendors_click() -> bool:
    """True on Typer >= 0.26, which vendored Click into ``typer._click``.

    That vendoring is the entire reason the bug below exists, so it is also
    the honest predicate for "can this machine observe the bug at all".
    """
    return importlib.util.find_spec("typer._click") is not None


class _LegacyGroup(TyperGroup):
    """The pre-fix ``BrainGroup``: hand-raises the **stock** ``click`` class.

    A deliberate control, kept in the test file rather than in production so
    the regression has something to be measured against. ``BrainGroup`` used
    to be exactly this.
    """

    def invoke(self, ctx: Any) -> Any:
        try:
            return super().invoke(ctx)
        except ConfigError as exc:
            import click

            raise click.UsageError(str(exc), ctx=ctx) from None


def test_the_usage_error_is_the_class_this_typer_actually_catches(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The CLI raises whatever class *this* framework treats as a usage error.

    Scope, stated plainly because it is narrower than it looks: this holds on
    every supported Typer, but it only *discriminates* on 0.26+. Below 0.26,
    ``ctx.fail()`` and the pre-fix ``raise click.UsageError(...)`` raise the
    very same class, so this assertion passes against the buggy implementation
    too. Measured, not assumed — see the table in
    ``test_the_pre_fix_group_is_a_real_regression_on_a_vendored_typer``, which
    is where the actual regression gate lives.

    What it still buys everywhere: if ``BrainGroup`` ever stopped raising a
    usage error at all — returning, or letting some unrelated exception out —
    this fails on any Typer. That is worth the three lines; it is just not the
    version-drift guard the name might suggest.
    """
    monkeypatch.setenv("DATABASE_URL", _UNUSED_DSN)
    monkeypatch.setenv("BRAIN_SECRET_GUARD", "block")
    expected = _framework_usage_error()

    # `standalone_mode=False` re-raises instead of converting to an exit code,
    # so the class itself is observable.
    with pytest.raises(expected):
        typer.main.get_command(_app()).main(["probe"], standalone_mode=False)


def test_the_pre_fix_group_is_a_real_regression_on_a_vendored_typer() -> None:
    """The gate: ``ctx.fail()`` and ``raise click.UsageError(...)`` must differ.

    Typer 0.26 vendored the whole of Click into ``typer._click``, so from that
    release on ``click.UsageError`` and the class Typer's runner catches are
    two unrelated classes in disjoint hierarchies. A hand-raised stock
    ``click.UsageError`` therefore stops being a usage error to the framework
    running it: it escapes the runner as an unhandled exception and exits 1
    with a Rich traceback of ``cli_errors.py``, instead of exiting 2 with the
    boxed message — the exact crash this module exists to remove, silently
    restored on any machine whose resolver picked a newer Typer.

    Running ``_LegacyGroup`` (the pre-fix code) alongside ``BrainGroup`` is
    what makes this a gate rather than a restatement: revert ``cli_errors.py``
    and the two collapse onto the same behaviour and this test goes red.

    Measured across the whole pinned range::

        typer    typer._click   BrainGroup   _LegacyGroup
        0.16.0   absent         exit 2       exit 2   <- indistinguishable
        0.25.1   absent         exit 2       exit 2   <- indistinguishable
        0.26.8   present        exit 2       exit 1
        0.27.1   present        exit 2       exit 1

    Hence the skip below rather than a quietly-passing assertion: on the
    pre-vendoring half of the range there is no bug to catch, and a test that
    says so is worth more than one that pretends otherwise.
    """
    if not _typer_vendors_click():
        pytest.skip(
            f"typer {version('typer')} predates the 0.26 Click vendoring, so "
            "ctx.fail() and a hand-raised click.UsageError raise the SAME "
            "class here and the regression is not observable. This gate runs "
            "on typer >= 0.26 (CI resolves the ceiling of the pyproject pin)."
        )

    # Guard the premise: if this ever stops holding while `typer._click` still
    # exists, fail loudly instead of asserting something vacuous below.
    import click

    assert _framework_usage_error() is not click.UsageError, (
        "typer._click exists but ctx.fail() still raises the stock "
        "click.UsageError — the premise of this test no longer holds"
    )

    env = {"DATABASE_URL": _UNUSED_DSN, "BRAIN_SECRET_GUARD": "block"}

    fixed = CliRunner(env=env).invoke(_app(), ["probe"])
    assert fixed.exit_code == 2, fixed.output

    legacy_app = typer.Typer(cls=_LegacyGroup)

    @legacy_app.command()
    def probe() -> None:
        Config.load()

    @legacy_app.command()
    def other() -> None:
        typer.echo("other")

    legacy = CliRunner(env=env).invoke(legacy_app, ["probe"])

    assert legacy.exit_code == 1, (
        "the pre-fix implementation was expected to escape the runner and exit "
        f"1 on this vendored Typer, but exited {legacy.exit_code} — if this is "
        "genuinely no longer a regression, delete this test rather than "
        "loosening it"
    )
    assert legacy.exit_code != fixed.exit_code


def test_a_missing_required_argument_is_a_usage_error_not_a_crash() -> None:
    """The blind spot that hid a live Typer/Click incompatibility.

    Nothing in the suite invoked a command with a required argument omitted, so
    on 2026-08-07 `typer 0.16.0 + click 8.4.2` — a pairing the pin then allowed
    — reported ``834 passed, 0 failed`` while the real CLI answered ``brain
    show`` (no ID) with **exit 1 and a Rich traceback** instead of exit 2 and
    ``Missing argument 'ID'``. It was found by diffing the shipped CLI across
    click versions, not by tests, and it leaked ``Config``'s locals — including
    ``voyage_api_key`` — into that traceback.

    Measured then::

        typer 0.16.0 + click 8.2.1   exit 2, "Missing argument"   OK
        typer 0.16.0 + click 8.3.0   exit 1, traceback            BROKEN
        typer 0.18.0 + click 8.4.2   exit 2, "Missing argument"   OK
        typer 0.26.0 + click 8.1.8   exit 2, "Missing argument"   OK

    The floor moved to ``typer>=0.26`` in response. This test is the guard that
    was missing: it is the same property the rest of this module protects for
    ``BRAIN_*`` env vars — a usage mistake exits 2 and does not traceback —
    applied to the argument parser, which is where the framework pairing
    actually shows.
    """
    app = typer.Typer(cls=BrainGroup)

    @app.command()
    def show(doc_id: str) -> None:  # a REQUIRED positional, deliberately
        typer.echo(f"showing {doc_id}")

    @app.command()
    def other() -> None:
        typer.echo("other")

    result = CliRunner(env={"DATABASE_URL": _UNUSED_DSN}).invoke(app, ["show"])

    assert result.exit_code == 2, (
        "a missing required argument must be a usage error (exit 2), not a "
        f"crash — got exit {result.exit_code}. This is the Typer/Click "
        "pairing regression the pyproject typer floor guards against.\n"
        f"{result.output}"
    )
    assert "Traceback" not in result.output
    assert "Missing argument" in result.output


def test_config_error_is_not_a_click_exception() -> None:
    """The library stays framework-free; only the CLI layer knows about Click.

    Making ``ConfigError`` inherit ``click.ClickException`` would also fix the
    presentation — and would couple ``config.py`` to a CLI framework that the
    MCP server and ``brain ui`` do not use.
    """
    import click

    assert not issubclass(ConfigError, click.ClickException)
