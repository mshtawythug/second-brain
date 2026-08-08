"""Regression guard for the GitHub-Actions ANSI-colour bug (v0.3.0 release blocker).

Typer force-enables colour when ``GITHUB_ACTIONS`` / ``FORCE_COLOR`` /
``PY_COLORS`` is set, binding ``typer.rich_utils.FORCE_TERMINAL`` at import
time. That wrapped every ``--help`` screen in escape codes, so 20 plain
``assert "--flag" in result.output`` checks failed in CI while passing on every
developer machine. ``tests/__init__.py`` neutralises it using Typer's own
``_TYPER_FORCE_DISABLE_TERMINAL`` hatch, before anything imports Typer.

Why these tests exist rather than trusting the one-liner: that hatch is a
*private* Typer API and the dependency pin is a range (``typer>=0.26,<0.28``).
If an upgrade renames or drops it, the hatch would silently stop working and
those 20 tests would fail again with the same opaque
``assert '--port' in '\\x1b[1m...'`` message that cost a release. These two
tests convert that into a single obvious failure that names the cause.
"""

from __future__ import annotations

import typer
from typer.testing import CliRunner

from brain.cli_ui import register_ui_commands

runner = CliRunner()

# Every ANSI escape sequence starts with this; Typer exposes it as
# ``rich_utils.ANSI_PREFIX`` for the same purpose.
ANSI_PREFIX = "\033["


def test_typer_colour_forcing_is_disabled_for_the_test_session() -> None:
    """The escape hatch in ``tests/__init__.py`` is still honoured by Typer."""
    from typer import rich_utils

    assert rich_utils.FORCE_TERMINAL is False, (
        "Typer is force-enabling ANSI colour for this test session. "
        "tests/__init__.py sets _TYPER_FORCE_DISABLE_TERMINAL=1 to prevent "
        "this, so the hatch has most likely been renamed or removed by a Typer "
        "upgrade. Without it, every substring assertion against --help output "
        "fails under GITHUB_ACTIONS/FORCE_COLOR/PY_COLORS."
    )


def test_help_output_carries_no_ansi_escapes() -> None:
    """End-to-end: real help text is plain, and still genuinely carries its flags.

    The second half matters as much as the first — a fix that produced empty or
    stripped-to-nothing output would satisfy an ANSI check while destroying the
    coverage these help tests exist to provide.
    """
    app = typer.Typer()
    register_ui_commands(app)

    result = runner.invoke(app, ["ui", "--help"])

    assert result.exit_code == 0
    assert ANSI_PREFIX not in result.output, "help output contains ANSI escape codes"
    assert "--port" in result.output
