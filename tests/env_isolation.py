"""Restore ``os.environ`` after every test, so no test's env changes reach the next one.

Loaded from ``tests/conftest.py`` (``pytest_plugins``). It is a separate module
because conftest is already far past the 800-line ceiling.

**Why ``monkeypatch`` is not enough.** ``Config.load()`` publishes every key
from the dotenv chain into the process env with ``os.environ.setdefault``. That
is intended production behaviour: code outside ``brain.config`` and child
processes (``brain-rebuild``'s stage subprocesses; before R3, also the Quartz
wiki build) read those knobs from ``os.environ``.
``monkeypatch`` undoes only the names it was handed, and a
``delenv(..., raising=False)`` of an absent name records nothing. So a test
that loads config from a ``.env`` it wrote leaks whatever keys that file held.
On 2026-10-16, ``BRAIN_EMBEDDER=none`` leaked from
``test_config_dotenv_chain.py`` into seven ``test_cli_doctor.py`` tests
whenever the chain module ran first. See
``docs/specs/2026-10-16-dotenv-isolation-leak-design.md``.

**Why every test, not only config tests.** Any ``Config.load`` can publish a
key, including the one behind every ``CliRunner`` invocation, and ``brain setup``
assigns ``BRAIN_HOME`` / ``BRAIN_VAULT_PATH`` outright. The leaked names
cannot be known in advance, so the guard snapshots the whole mapping.

Values installed by session- and module-scoped fixtures are part of the
snapshot: pytest sets up higher-scoped fixtures before this function-scoped
one, so they survive.
"""
from __future__ import annotations

import os
from collections.abc import Iterator
from contextlib import contextmanager

import pytest


@contextmanager
def restored_environ() -> Iterator[None]:
    """Snapshot ``os.environ``; on exit, put back exactly the keys that differ.

    Added keys are deleted, and changed or deleted keys get their old values
    back. Only differing keys are touched, never ``os.environ.clear()``, so a
    thread still running during teardown never sees an empty environment.
    """
    snapshot = dict(os.environ)
    try:
        yield
    finally:
        for key in [k for k in os.environ if k not in snapshot]:
            del os.environ[key]
        for key, value in snapshot.items():
            if os.environ.get(key) != value:
                os.environ[key] = value


@pytest.fixture(autouse=True)
def _restore_os_environ() -> Iterator[None]:
    """Run every test inside :func:`restored_environ`."""
    with restored_environ():
        yield
