"""The live-Ollama guard must ban the port the application actually dials.

``tests/conftest.py::_forbid_live_ollama`` blocks outbound connections to *one*
port. If that port is not the one the code under test dials, the guard is inert:
it bans an address nothing contacts, the real call goes out (or fails with a
plain connection refusal), and **nothing turns red**. Every eval gate keeps
skipping — for the wrong reason — and the suite still passes.

That was a live trap. ``_ollama_port()`` used to read ``OLLAMA_HOST`` with its
own hardcoded ``"http://localhost:11434"`` fallback, while the application takes
its default from :data:`brain.config.DEFAULT_OLLAMA_HOST`. Two independent
sources of truth for one value, agreeing only by coincidence. Redirecting just
one of them is enough to disarm the guard silently: doing exactly that made a
``live_ollama`` counterfactual appear to refute the marker's entire purpose,
because the application dialled the redirected port while the guard went on
banning the old one.

The fix makes ``conftest`` derive its port from ``brain.config``. These tests
pin that, so the day someone changes the default port the drift is a red test
rather than a guard that quietly stopped guarding.

The expected value is parsed here **independently** of ``conftest``'s own
helper. Asserting ``_ollama_port() == _port_of(DEFAULT_OLLAMA_HOST)`` would
merely restate the implementation and could never fail.
"""
from __future__ import annotations

from urllib.parse import urlparse

import pytest

from brain.config import DEFAULT_OLLAMA_HOST
from tests.conftest import _OLLAMA_DEFAULT_PORT, _ollama_port

#: Independent parse of the application's default host — deliberately NOT
#: ``conftest._port_of``, so this is an oracle rather than an echo.
_EXPECTED_DEFAULT_PORT = urlparse(DEFAULT_OLLAMA_HOST).port


def test_the_app_default_declares_an_explicit_port() -> None:
    """Guard the guard: a portless default would make the oracle below ``None``."""
    assert _EXPECTED_DEFAULT_PORT is not None, (
        f"brain.config.DEFAULT_OLLAMA_HOST={DEFAULT_OLLAMA_HOST!r} has no explicit "
        "port, so this module's oracle is None and the comparisons below stop "
        "meaning anything. Give the default an explicit :port, or rewrite the "
        "oracle to resolve the scheme default."
    )


def test_guard_bans_the_port_the_app_actually_dials(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """With no override, the guard's port IS the application's default port.

    Fails if anyone reintroduces a literal in ``conftest`` that drifts from
    ``brain.config`` — the exact defect this module exists for.
    """
    monkeypatch.delenv("OLLAMA_HOST", raising=False)

    assert _ollama_port() == _EXPECTED_DEFAULT_PORT, (
        f"the live-Ollama guard bans port {_ollama_port()}, but the application "
        f"dials {_EXPECTED_DEFAULT_PORT} (brain.config.DEFAULT_OLLAMA_HOST="
        f"{DEFAULT_OLLAMA_HOST!r}). The guard is banning a port nothing contacts, "
        "so it is inert: live calls escape and every eval gate skips for the "
        "wrong reason, with nothing going red."
    )


def test_guard_default_constant_tracks_the_app_default() -> None:
    """The module-level fallback is derived, not a second hardcoded literal."""
    assert _OLLAMA_DEFAULT_PORT == _EXPECTED_DEFAULT_PORT, (
        f"conftest._OLLAMA_DEFAULT_PORT={_OLLAMA_DEFAULT_PORT} has drifted from "
        f"brain.config.DEFAULT_OLLAMA_HOST's port {_EXPECTED_DEFAULT_PORT}"
    )


@pytest.mark.parametrize(
    ("host", "expected"),
    [
        ("http://127.0.0.1:9", 9),
        ("http://localhost:12345", 12345),
        ("127.0.0.1:9", 9),  # scheme-less, as an operator might export it
        ("https://ollama.example:8443", 8443),
    ],
)
def test_ollama_host_env_override_still_wins(
    monkeypatch: pytest.MonkeyPatch, host: str, expected: int
) -> None:
    """The operator-facing knob must keep working.

    ``OLLAMA_HOST`` is what ``Config.load()`` honours, and pointing it at a
    closed port is how a live-service gate is exercised offline. Deriving the
    *fallback* from ``brain.config`` must not cost us the *override*.
    """
    monkeypatch.setenv("OLLAMA_HOST", host)

    assert _ollama_port() == expected


def test_empty_ollama_host_falls_back_to_the_app_default(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``OLLAMA_HOST=""`` is an unset knob, not a request to dial port 80."""
    monkeypatch.setenv("OLLAMA_HOST", "")

    assert _ollama_port() == _EXPECTED_DEFAULT_PORT
