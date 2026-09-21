"""``brain ui``'s boot sequence: port resolution, preflight, and notices.

Kept separate from ``test_ui_telemetry_persists.py`` — that module is about
whether a telemetry row SURVIVES, this one is about whether the server starts
and what it tells the user when it starts degraded.

Nothing here binds a listening socket or starts uvicorn. The two branches that
genuinely require a real bind — ``serve``'s startup hook and its ``EADDRINUSE``
handler — are deliberately out of scope; they are covered by running the thing,
not by a unit test that would have to fake the operating system to reach them.
"""
from __future__ import annotations

import socket
from contextlib import closing
from typing import Any

import pytest

from brain.config import Config
from brain.errors import BrainError
from brain.ui.context import UiContext
from brain.ui.server import (
    TELEMETRY_DISABLED_NOTICE,
    preflight,
    resolve_port,
    with_notice,
)

# Refused immediately rather than after a connect timeout: port 1 on loopback
# has nothing listening, so this stays a fast unit test.
UNREACHABLE_DB = "postgresql://brain:brain@127.0.0.1:1/nowhere"


@pytest.fixture
def occupied_port() -> Any:
    """A port genuinely held by a listening socket for the test's duration.

    A real bind, not a patched ``_port_is_free``: the function under test exists
    precisely because probing ports is fiddly, so faking the probe would test
    the fake.
    """
    with closing(socket.socket(socket.AF_INET, socket.SOCK_STREAM)) as sock:
        sock.bind(("127.0.0.1", 0))
        sock.listen(1)
        yield sock.getsockname()[1]


# ------------------------------------------------------------ port resolution --


def test_an_occupied_port_is_stepped_over(occupied_port: int) -> None:
    """The default: take the next free port rather than making the user care."""
    resolved = resolve_port(occupied_port, host="127.0.0.1", attempts=20, auto=True)
    assert resolved != occupied_port
    assert resolved > occupied_port


def test_giving_up_after_the_attempt_window_names_the_range(
    occupied_port: int,
) -> None:
    """Exhausting the window must fail with a remediation, not an IndexError.

    ``attempts=1`` makes the candidate range empty, which is the boundary case:
    the loop body never runs and control falls through to the raise. An
    off-by-one that made the range non-empty would silently return a port that
    was never probed.
    """
    with pytest.raises(BrainError) as excinfo:
        resolve_port(occupied_port, host="127.0.0.1", attempts=1, auto=True)

    message = str(excinfo.value)
    assert "no free port found" in message
    assert str(occupied_port) in message, (
        "the error must name the range it searched, or the user cannot tell "
        "which ports to free"
    )
    assert "--port" in message, "an error with no remediation is just a stack trace"


def test_auto_port_can_be_refused(occupied_port: int) -> None:
    """``--no-auto-port`` means the user chose the port deliberately."""
    with pytest.raises(BrainError) as excinfo:
        resolve_port(occupied_port, host="127.0.0.1", attempts=20, auto=False)

    assert "already in use" in str(excinfo.value)


# ------------------------------------------------------------------ preflight --


def test_an_unreachable_database_fails_before_the_port_is_bound() -> None:
    """A dead database must fail EARLY, and say how to diagnose it.

    The ordering is the point, and it is a usability property rather than a
    correctness one: preflight runs before uvicorn binds, so a user with
    Postgres down gets an actionable message instead of a server that appears
    to start and then 500s on first use — and no orphaned listener to hunt down
    and kill.
    """
    cfg = Config(database_url=UNREACHABLE_DB, embedder="none")

    with pytest.raises(BrainError) as excinfo:
        preflight(cfg)

    message = str(excinfo.value)
    assert "cannot reach Postgres" in message
    assert "brain doctor" in message, (
        "the error must hand the user the next command; this is the first thing "
        "a new install hits"
    )


def test_a_healthy_database_reports_no_notices(test_db: Any) -> None:
    """The success path — without it the notice test below proves nothing.

    A ``preflight`` that returned a notice unconditionally would satisfy the
    next test on its own.
    """
    from .conftest import TEST_DATABASE_URL

    cfg = Config(database_url=TEST_DATABASE_URL, embedder="none")
    logging_enabled, notices = preflight(cfg)

    assert logging_enabled is True, (
        "this test database has migration 024 applied, so 'ui' is admitted"
    )
    assert notices == ()


def test_a_database_that_cannot_log_boots_anyway_and_says_so(
    test_db: Any, mocker: Any
) -> None:
    """Degraded, not broken — and the user is told which.

    ``'ui'`` joined the ``search_queries`` CHECK only in migration 024. On an
    older database the UI must still run; it simply records nothing. Silence
    here would be the worse failure: this module's headline defect was ~215
    writes discarded while the app reported itself healthy, so a UI that cannot
    log and does not SAY it cannot log is the exact shape that hid it.

    The probe is doubled rather than the database downgraded: dropping and
    re-adding a live CHECK constraint to reach one branch is a destructive
    schema edit on a shared instance, and the unit under test is preflight's
    notice assembly, not the constraint itself.
    """
    from .conftest import TEST_DATABASE_URL

    mocker.patch("brain.ui.server.ui_source_supported", return_value=False)
    cfg = Config(database_url=TEST_DATABASE_URL, embedder="none")

    logging_enabled, notices = preflight(cfg)

    assert logging_enabled is False
    assert TELEMETRY_DISABLED_NOTICE in notices, (
        "the UI degraded to recording nothing and said nothing about it"
    )
    assert "migration 024" in TELEMETRY_DISABLED_NOTICE, (
        "the notice must name the fix, not merely report the symptom"
    )


# -------------------------------------------------------------------- notices --


def _bare_context(**overrides: Any) -> UiContext:
    return UiContext(
        cfg=Config(database_url=UNREACHABLE_DB, embedder="none"),
        conn_factory=lambda: None,
        embedder=None,
        search_fn=lambda *a, **k: [],
        allowed_origin="http://127.0.0.1:8765",
        **overrides,
    )


def test_with_notice_appends_without_mutating_the_original() -> None:
    """``UiContext`` is frozen and shared across every request handler.

    So the assertion that matters is not that the new context has the notice —
    it is that the ORIGINAL does not. A ``with_notice`` implemented by appending
    to ``context.notices`` in place would satisfy a "the notice is there" check
    and silently leak startup warnings into a long-lived shared object.
    """
    original = _bare_context(notices=("first",))

    updated = with_notice(original, "second")

    assert updated.notices == ("first", "second")
    assert original.notices == ("first",), (
        "with_notice mutated the context in place; UiContext is shared by every "
        "request and must not accumulate state"
    )
    assert updated is not original


def test_with_notice_works_from_an_empty_start() -> None:
    """The common case: the first notice on a context that has none."""
    assert with_notice(_bare_context(), "only").notices == ("only",)
