"""Test helper: drive ``brain.vault.watch._worker_loop`` synchronously on one connection."""

from __future__ import annotations

from typing import Any

import psycopg

from brain.vault.db_reconnect import ReconnectingConnection


def worker_link(conn: psycopg.Connection[Any]) -> ReconnectingConnection:
    """A worker link over the test's own ``conn``.

    The tests that use it never lose their connection, so it never reconnects.
    It is never closed either: ``conn`` belongs to the caller (usually the
    ``test_db`` fixture). The reconnect itself is tested in
    ``tests/test_vault_watch_db_reconnect.py``.
    """
    return ReconnectingConnection(
        lambda: conn, label="worker", sleep=lambda _delay: None, stopping=lambda: True, conn=conn
    )
