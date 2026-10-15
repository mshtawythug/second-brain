"""`GET /api/watcher` — A9's UI half, proven against a real ``create_app``.

The route answers one question — is the vault-sync watcher running — as one
enum. Four properties, each its own test because each fails on its own:

* **reachable** from an app built the normal way, for every state;
* **minimal**: exactly ``{"state": …}``. The process snapshot carries every
  command line on the machine, and the watcher's own names the vault path, so
  nothing from it may ride along. Proven with a sentinel planted in the fake
  snapshot and run through the REAL ``watcher_state``;
* **behind the same guards as every /api/ route** — the token on a protected
  server, ``no-store``, GET only;
* **database-free**, like ``/api/health``: the watcher question does not depend
  on Postgres, so a dead database must not take the line down with it.

No monkey-patching: ``UiContext.watcher_probe`` is the seam.
No PII: the snapshot is synthetic paths.
"""
from __future__ import annotations

import contextlib
from pathlib import Path
from typing import Any

import pytest
from starlette.testclient import TestClient

from brain.doctor_watcher import WATCHER_STATES, WatcherState, watcher_state
from brain.ui.app import create_app
from brain.ui.context import UiContext
from brain.ui.security import TOKEN_HEADER
from brain.ui.watcher_probe import WatcherProbe

pytestmark = [
    pytest.mark.nodb,
    pytest.mark.filterwarnings("ignore::DeprecationWarning"),
]

ORIGIN = "http://127.0.0.1:8765"
SENTINEL = "/synthetic/sentinel-vault-path"


class _FakeConn:
    def execute(self, *args: Any, **kwargs: Any) -> Any:  # pragma: no cover
        raise AssertionError("/api/watcher must not reach the database")


def _context(tmp_path: Path, probe: WatcherProbe, **overrides: Any) -> UiContext:
    class _Cfg:
        vault_path = tmp_path
        database_url = "postgresql://user:pw@localhost:5432/nowhere"
        embedder = "none"
        user_email: str | None = None

    @contextlib.contextmanager
    def conn_factory() -> Any:
        yield _FakeConn()

    fields: dict[str, Any] = {
        "cfg": _Cfg(),
        "conn_factory": conn_factory,
        "embedder": object(),
        "search_fn": lambda *a, **k: [],
        "allowed_origin": ORIGIN,
        "watcher_probe": probe,
        **overrides,
    }
    return UiContext(**fields)


def _fixed(state: WatcherState) -> WatcherProbe:
    return WatcherProbe(detect=lambda: state)


@pytest.mark.parametrize("state", WATCHER_STATES)
def test_each_state_round_trips_as_one_enum(tmp_path: Path, state: WatcherState) -> None:
    with TestClient(create_app(_context(tmp_path, _fixed(state))), base_url=ORIGIN) as c:
        response = c.get("/api/watcher")

    assert response.status_code == 200
    assert response.json() == {"state": state}


@pytest.mark.parametrize(
    "snapshot",
    [
        [f"/x/bin/brain vault sync --watch --vault {SENTINEL}", "/bin/zsh"],
        [f"/x/bin/brain ui --vault {SENTINEL}", "/bin/zsh"],
    ],
    ids=["running", "not_running"],
)
def test_no_command_line_leaks_into_the_body(tmp_path: Path, snapshot: list[str]) -> None:
    probe = WatcherProbe(detect=lambda: watcher_state(snapshot))

    with TestClient(create_app(_context(tmp_path, probe)), base_url=ORIGIN) as c:
        response = c.get("/api/watcher")

    assert set(response.json()) == {"state"}
    assert SENTINEL not in response.text
    assert "brain" not in response.text


def test_a_token_protected_server_refuses_it_without_the_token(tmp_path: Path) -> None:
    context = _context(tmp_path, _fixed("running"), token="synthetic-token")

    with TestClient(create_app(context), base_url=ORIGIN) as c:
        refused = c.get("/api/watcher")
        served = c.get("/api/watcher", headers={TOKEN_HEADER: "synthetic-token"})

    assert refused.status_code == 403
    assert refused.json()["error"]["code"] == "invalid_token"
    assert served.json() == {"state": "running"}


def test_it_is_not_cached_by_the_browser_and_is_get_only(tmp_path: Path) -> None:
    with TestClient(create_app(_context(tmp_path, _fixed("running"))), base_url=ORIGIN) as c:
        got = c.get("/api/watcher")
        posted = c.post(
            "/api/watcher", json={}, headers={"Origin": ORIGIN}
        )

    assert got.headers["cache-control"] == "no-store"
    assert posted.status_code == 405


def test_the_default_context_probes_with_the_doctors_rule(tmp_path: Path) -> None:
    """A context built without a probe still gets one, over ``watcher_state``."""
    context = _context(tmp_path, _fixed("running"))
    fields = {k: v for k, v in vars(context).items() if k != "watcher_probe"}

    default = UiContext(**fields)

    assert isinstance(default.watcher_probe, WatcherProbe)
    assert default.watcher_probe.detect is watcher_state
