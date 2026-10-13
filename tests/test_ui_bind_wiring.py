"""``build_context`` derives the Host allowlist and the Origin from the bind.

Rulings W1/W2 (phase 7, ``docs/plans/2026-10-12-wiki-to-ui-phase7.md`` §10.2):
the security tests in ``tests/test_ui_security.py`` prove the helpers; this
proves ``build_context`` USES them, which no DB-free test can — it probes
Postgres before it returns. Uses the test database, read-only.
"""
from __future__ import annotations

import pytest

from brain.config import Config
from brain.ui.context import LOOPBACK_HOSTS
from brain.ui.server import build_context

from .conftest import TEST_DATABASE_URL


def _build(host: str) -> object:
    return build_context(
        Config(database_url=TEST_DATABASE_URL, embedder="none"),
        host=host,
        port=8765,
        read_only=True,
        token="t",
        include_confidential=False,
        embedder=None,
    )


@pytest.mark.parametrize(
    ("bind", "origin", "hosts"),
    [
        ("0.0.0.0", "http://127.0.0.1:8765", LOOPBACK_HOSTS),
        ("::", "http://[::1]:8765", LOOPBACK_HOSTS),
        ("127.0.0.1", "http://127.0.0.1:8765", LOOPBACK_HOSTS),
        ("192.0.2.10", "http://192.0.2.10:8765", frozenset({"192.0.2.10"})),
    ],
)
def test_build_context_derives_origin_and_hosts_from_the_bind(
    bind: str, origin: str, hosts: frozenset[str]
) -> None:
    context = _build(bind)
    assert context.allowed_origin == origin  # type: ignore[attr-defined]
    assert context.allowed_hosts == hosts  # type: ignore[attr-defined]
