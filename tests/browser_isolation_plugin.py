"""Keep every test away from the owner's real browser data and the network (RM-41).

Loaded from ``tests/conftest.py`` (``pytest_plugins``), following
``tests/voice_isolation_plugin.py``: conftest is past the 800-line ceiling.

**Why.** ``brain ingest-web`` without ``--profile`` resolves the browser's default
profile under :data:`brain.cli_ingest_web.home_dir` (``~/Library/Safari``,
``~/Library/Application Support/Google/Chrome/Default``). A test that forgot
``--profile`` would read the owner's real history. ``--fetch`` resolves hosts through
:data:`brain.cli_ingest_web.fetch_resolver`; a test that forgot to inject one would
reach the network.

**How.** For every test, ``home_dir`` returns a path under the session's temporary
root that is never created, so such a run fails with "no browser history found";
and ``fetch_resolver`` resolves nothing, so such a fetch fails with "did not
resolve" before any connection. A test that needs either passes ``--profile`` or
sets the attribute itself with ``monkeypatch``. Proven by
``tests/test_browser_isolation.py``.
"""
from __future__ import annotations

from collections.abc import Iterator, Sequence
from pathlib import Path

import pytest

#: The fake home's name under the session temp root; nothing ever creates it.
NOWHERE = "no-real-browser-home"


def _resolve_nothing(host: str, port: int) -> Sequence[str]:
    return []


@pytest.fixture(autouse=True)
def _no_real_browser_data(
    monkeypatch: pytest.MonkeyPatch, tmp_path_factory: pytest.TempPathFactory
) -> Iterator[None]:
    """A home with no browser profile and a resolver with no answers, for one test."""
    nowhere = Path(tmp_path_factory.getbasetemp()) / NOWHERE
    monkeypatch.setattr("brain.cli_ingest_web.home_dir", lambda: nowhere)
    monkeypatch.setattr("brain.cli_ingest_web.fetch_resolver", _resolve_nothing)
    yield
