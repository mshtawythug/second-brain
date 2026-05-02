"""Local conftest for the wiki tests — keeps them DB-free.

The repo-wide :func:`tests.conftest._ensure_test_db_initialized` is an
``autouse`` session fixture that opens a Postgres connection and runs
migrations. Wiki tests don't touch the DB at all (they exercise pure
filesystem + subprocess + threading paths), so paying that startup
cost is wasteful — and worse, it couples wiki test runs to Docker
health. We override the session fixture here with a no-op so wiki
tests run cleanly even when Postgres is down.

This is a fixture override, not monkey-patching: pytest resolves
fixtures by name and the closest-scoped definition wins, so the
no-op below shadows the parent's connect-and-migrate version for
any test under ``tests/wiki/``.
"""
from __future__ import annotations

from collections.abc import Iterator

import pytest


@pytest.fixture(autouse=True, scope="session")
def _ensure_test_db_initialized() -> Iterator[None]:
    """No-op override — wiki tests don't need a DB."""
    yield
