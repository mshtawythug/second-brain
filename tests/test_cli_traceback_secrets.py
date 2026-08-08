"""Tracebacks escaping the `brain` CLI must never render frame locals.

Almost every command body binds ``cfg = Config.load()``. ``Config`` is a
dataclass, so its repr spells out ``voyage_api_key=...`` and a ``database_url``
containing the Postgres password. Typer renders escaping exceptions through
Rich, and Rich's ``show_locals`` prints exactly those reprs to stderr.

These tests are behavioural, not configuration assertions: each one renders a
REAL traceback from a REAL crashing invocation of the real ``brain.cli.app``
through Typer's own ``except_hook``, then greps the resulting text.

``test_config_secrets_are_rendered_when_locals_are_shown`` is the positive
control. Without it the other tests could pass against a CLI that simply never
puts a secret in a rendered frame, and the guard would be protecting nothing.

No database is touched: the DSN points at 127.0.0.1 port 1, which is refused
immediately by the kernel. Both marker values are synthetic.
"""
from __future__ import annotations

import io
import os
import subprocess
import sys
import textwrap
from collections.abc import Callable

import pytest
from typer.main import _typer_developer_exception_attr_name as TYPER_EXC_ATTR
from typer.main import except_hook
from typer.models import DeveloperExceptionConfig

from brain.cli import app

#: Synthetic stand-ins for the two live credentials a real user's Config holds.
#: Deliberately distinctive so a substring search cannot match by accident, and
#: deliberately fake so nothing real is ever written to a captured buffer.
VOYAGE_KEY_MARKER = "voyage-key-marker-d0d0cafe"
DB_PASSWORD_MARKER = "db-password-marker-d0d0cafe"

#: Port 1 is reserved and never listening, so `psycopg.connect` fails with
#: OperationalError in microseconds. NOT 55432 (production) and NOT 5434 (test).
UNREACHABLE_DSN = (
    f"postgresql://brain:{DB_PASSWORD_MARKER}@127.0.0.1:1/brain_no_such_database"
)

#: Commands whose bodies reach a DB connection with `cfg` still live in the
#: frame. `status` is defined on the root app; `graphrag stats` is defined on a
#: sub-app registered via `add_typer`, which is the case that would be missed by
#: a fix that only reasoned about the root.
ROOT_COMMAND = ["status"]
SUBAPP_COMMAND = ["graphrag", "stats"]


@pytest.fixture
def crash(monkeypatch: pytest.MonkeyPatch) -> Callable[[list[str]], BaseException]:
    """Return a callable that runs `argv` through the real app and returns the crash.

    The returned exception carries whatever ``DeveloperExceptionConfig`` the app
    itself attached, so downstream assertions read production configuration
    rather than a hand-built copy of it.
    """
    monkeypatch.setenv("DATABASE_URL", UNREACHABLE_DSN)
    monkeypatch.setenv("VOYAGE_API_KEY", VOYAGE_KEY_MARKER)
    monkeypatch.setenv("BRAIN_EMBEDDER", "voyage")
    # Keep the dotenv chain from pulling a developer's real .env in behind the
    # markers, and give Rich a wide console so a rendered value cannot be
    # hidden from the assertion by line wrapping rather than by the guard.
    monkeypatch.setenv("BRAIN_IGNORE_CWD_DOTENV", "1")
    monkeypatch.setenv("COLUMNS", "200")

    def _crash(argv: list[str]) -> BaseException:
        try:
            app(argv, standalone_mode=True)
        except SystemExit as exc:  # pragma: no cover - guards a silent no-op test
            pytest.fail(
                f"`brain {' '.join(argv)}` exited cleanly (code {exc.code}) instead "
                "of raising; this test needs a real escaping exception."
            )
        except Exception as exc:
            return exc
        pytest.fail(  # pragma: no cover - same guard, no-exception branch
            f"`brain {' '.join(argv)}` did not raise; this test needs a real crash."
        )

    return _crash


def _render(exc: BaseException, config: DeveloperExceptionConfig) -> str:
    """Render `exc` through Typer's real except_hook and return the stderr text."""
    setattr(exc, TYPER_EXC_ATTR, config)
    buffer = io.StringIO()
    original_stderr = sys.stderr
    sys.stderr = buffer
    try:
        except_hook(type(exc), exc, exc.__traceback__)
    finally:
        sys.stderr = original_stderr
    return buffer.getvalue()


def _attached_config(exc: BaseException) -> DeveloperExceptionConfig:
    """The rendering config the app attached to `exc`, i.e. production settings."""
    config = getattr(exc, TYPER_EXC_ATTR, None)
    assert config is not None, (
        "Typer.__call__ did not attach a DeveloperExceptionConfig; the CLI is no "
        "longer invoked through the Typer object and this guard is moot."
    )
    return config


def _leaks(rendered: str) -> tuple[bool, bool]:
    """(voyage key present, db password present) — booleans only.

    Returned as flags, never as the rendered text, so a failing assertion
    reports WHICH secret leaked without pytest dumping a traceback that may
    also contain unrelated values from the developer's environment.
    """
    return VOYAGE_KEY_MARKER in rendered, DB_PASSWORD_MARKER in rendered


def test_config_secrets_are_rendered_when_locals_are_shown(
    crash: Callable[[list[str]], BaseException],
) -> None:
    """Positive control: with locals on, both credentials DO reach stderr.

    This is what the app looked like under typer <= 0.22, whose
    `pretty_exceptions_show_locals` defaulted to True. It proves the guarded
    tests below are guarding a real exposure rather than an empty frame.
    """
    exc = crash(ROOT_COMMAND)
    rendered = _render(
        exc,
        DeveloperExceptionConfig(
            pretty_exceptions_enable=True,
            pretty_exceptions_show_locals=True,
            pretty_exceptions_short=app.pretty_exceptions_short,
        ),
    )

    voyage_leaked, password_leaked = _leaks(rendered)

    assert voyage_leaked, (
        "Positive control failed: VOYAGE_API_KEY did not appear even with "
        "show_locals=True, so the tests below prove nothing. Check that the "
        "invoked command still binds a Config in its own frame."
    )
    assert password_leaked, (
        "Positive control failed: the DATABASE_URL password did not appear even "
        "with show_locals=True. See above."
    )


def test_root_command_traceback_hides_config_secrets(
    crash: Callable[[list[str]], BaseException],
) -> None:
    """A crash in a root-app command renders no credentials."""
    exc = crash(ROOT_COMMAND)

    rendered = _render(exc, _attached_config(exc))
    voyage_leaked, password_leaked = _leaks(rendered)

    assert not voyage_leaked, "VOYAGE_API_KEY was rendered into a `brain` traceback."
    assert not password_leaked, (
        "The DATABASE_URL password was rendered into a `brain` traceback."
    )


def test_subapp_command_traceback_hides_config_secrets(
    crash: Callable[[list[str]], BaseException],
) -> None:
    """A crash in an `add_typer` sub-app command renders no credentials either.

    `graphrag stats` lives on `graphrag_app`, which never sets
    `pretty_exceptions_show_locals` itself. If the setting did not reach
    sub-apps, this is the test that would fail while the root-app test passed.
    """
    exc = crash(SUBAPP_COMMAND)

    rendered = _render(exc, _attached_config(exc))
    voyage_leaked, password_leaked = _leaks(rendered)

    assert not voyage_leaked, (
        "VOYAGE_API_KEY was rendered into a `brain graphrag` traceback — the "
        "root app's setting is not reaching sub-apps."
    )
    assert not password_leaked, (
        "The DATABASE_URL password was rendered into a `brain graphrag` "
        "traceback — the root app's setting is not reaching sub-apps."
    )


def test_subapp_inherits_the_root_apps_rendering_config(
    crash: Callable[[list[str]], BaseException],
) -> None:
    """Sub-app crashes are rendered with the ROOT app's settings, not their own.

    Pins the fact that makes a root-only fix complete: Typer attaches the
    rendering config in `Typer.__call__`, which runs on the console-script
    object alone. Were this to change — a sub-app promoted to its own entry
    point, say — the root-only fix would silently stop covering it, and this
    test is what would say so.
    """
    root_config = _attached_config(crash(ROOT_COMMAND))
    subapp_config = _attached_config(crash(SUBAPP_COMMAND))

    # Compared field-by-field: DeveloperExceptionConfig is a plain class with no
    # __eq__, so `==` on the instances would only ever compare identities.
    fields = (
        "pretty_exceptions_enable",
        "pretty_exceptions_show_locals",
        "pretty_exceptions_short",
    )
    assert {name: getattr(subapp_config, name) for name in fields} == {
        name: getattr(root_config, name) for name in fields
    }
    assert subapp_config.pretty_exceptions_show_locals is False


#: Runs in a subprocess so the default-swap below cannot escape into the parent
#: test session, and so `brain.cli` is imported fresh AFTER the swap (the app
#: object is built at import time, so patching later would change nothing).
#:
#: The swapped value is Typer's OWN keyword default, not any brain module — this
#: reconstructs a different released Typer, it does not reopen production code.
_HOSTILE_DEFAULT_PROBE = """
import io, sys, typer
# typer <= 0.22 shipped this default as True; 0.23 flipped it to False. Restore
# the old value before importing brain.cli, which builds `app` at import time.
typer.Typer.__init__.__kwdefaults__["pretty_exceptions_show_locals"] = True

from typer.main import except_hook, _typer_developer_exception_attr_name as ATTR
from brain.cli import app

try:
    app(["status"], standalone_mode=True)
except SystemExit:
    print("CLEAN_EXIT")
    raise SystemExit(0)
except Exception as exc:
    buffer = io.StringIO()
    stderr = sys.stderr
    sys.stderr = buffer
    try:
        except_hook(type(exc), exc, exc.__traceback__)
    finally:
        sys.stderr = stderr
    rendered = buffer.getvalue()
    # Booleans only. The rendered traceback is never printed: it can contain
    # unrelated values from whatever environment the suite is running in.
    print("VOYAGE_LEAKED=%s" % ({voyage!r} in rendered))
    print("PASSWORD_LEAKED=%s" % ({password!r} in rendered))
else:
    print("NO_EXCEPTION")
"""


def test_secrets_stay_hidden_even_if_typers_default_flips_back() -> None:
    """The guard must come from THIS app, not from Typer's current default.

    Typer's `pretty_exceptions_show_locals` default was `True` through 0.22 and
    only became `False` in 0.23. `[project.dependencies]` pins `typer>=0.26`
    today, so the range in force happens to be safe — which means a test run
    against the pinned range cannot distinguish "we set this" from "we got
    lucky". This test removes the luck by reconstructing the hostile default and
    asserting the app still renders no credentials.

    It is the test that fails if the explicit kwarg is ever dropped from
    `brain.cli.app`.
    """
    probe = textwrap.dedent(_HOSTILE_DEFAULT_PROBE).format(
        voyage=VOYAGE_KEY_MARKER, password=DB_PASSWORD_MARKER
    )
    env = {
        **os.environ,
        "DATABASE_URL": UNREACHABLE_DSN,
        "VOYAGE_API_KEY": VOYAGE_KEY_MARKER,
        "BRAIN_EMBEDDER": "voyage",
        "BRAIN_IGNORE_CWD_DOTENV": "1",
        "COLUMNS": "200",
    }
    result = subprocess.run(
        [sys.executable, "-c", probe],
        capture_output=True,
        text=True,
        env=env,
        timeout=120,
    )

    assert result.returncode == 0, (
        f"hostile-default probe exited {result.returncode}; it should have "
        f"rendered a traceback and exited 0. stderr tail: {result.stderr[-400:]!r}"
    )
    lines = result.stdout.split()
    assert "VOYAGE_LEAKED=False" in lines, (
        "Under a Typer whose show_locals default is True, VOYAGE_API_KEY was "
        "rendered into the traceback. `brain.cli.app` must pass "
        "pretty_exceptions_show_locals=False explicitly rather than relying on "
        f"the installed Typer's default. Probe output: {lines}"
    )
    assert "PASSWORD_LEAKED=False" in lines, (
        "Under a Typer whose show_locals default is True, the DATABASE_URL "
        "password was rendered into the traceback. See above. "
        f"Probe output: {lines}"
    )
