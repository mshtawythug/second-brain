"""RM-23 — ``abs_path`` in ``GET /api/notes/{id}``: loopback only, and inside the lens.

The "Copy path" button copies ``abs_path`` when the payload carries one and
``vault_path`` otherwise. ``abs_path`` is an absolute filesystem path, which a
LAN reader does not need, so the server sends it only when
``UiContext.serve_abs_path`` is set. ``server.build_context`` sets that flag from
the same bind classification (``ui.context.address_class``) that D8's
``ingest_enabled`` uses.

It must also never be the one field that escapes the confidential lens: a note
whose body this server withholds carries no ``abs_path`` either, whatever the
flag says.

Real Postgres (the suite's ``test_db``) and a ``tmp_path`` vault, as in
``tests/test_ui_routes.py``. Every value is synthetic.
"""
from __future__ import annotations

import contextlib
import ipaddress
from pathlib import Path
from typing import Any

import psycopg
import pytest
from starlette.testclient import TestClient

from brain.config import Config
from brain.search import hybrid_search
from brain.sensitivity import CONFIDENTIAL
from brain.ui.app import create_app
from brain.ui.context import UiContext
from brain.ui.server import build_context
from brain.vault import init_vault
from brain.vault.note_builder import create_vault_note

from .conftest import TEST_DATABASE_URL

pytestmark = pytest.mark.filterwarnings("ignore::DeprecationWarning")

ORIGIN = "http://127.0.0.1:8765"
NOTE_TITLE = "Synthetic Copy Path Note"
NOTE_FOLDER = "projects"


@pytest.fixture
def ui_cfg(tmp_path: Path) -> Config:
    vault = tmp_path / "vault"
    vault.mkdir()
    init_vault(vault)
    return Config(
        database_url="postgresql://unused/in/these/tests",
        vault_path=vault,
        embedder="none",
    )


@pytest.fixture
def note_id(test_db: psycopg.Connection, ui_cfg: Config, fake_embedder: Any) -> str:
    return create_vault_note(
        test_db,
        cfg=ui_cfg,
        vault_path=ui_cfg.vault_path,
        title=NOTE_TITLE,
        body="A synthetic body with nothing in it.\n",
        tags=["synthetic"],
        template="note",
        folder=NOTE_FOLDER,
        embedder=fake_embedder,
    )


def _client(test_db: psycopg.Connection, cfg: Config, embedder: Any, **flags: Any) -> TestClient:
    @contextlib.contextmanager
    def conn_factory() -> Any:
        yield test_db

    context = UiContext(
        cfg=cfg,
        conn_factory=conn_factory,
        embedder=embedder,
        search_fn=hybrid_search,
        allowed_origin=ORIGIN,
        logging_enabled=False,
        **flags,
    )
    return TestClient(create_app(context), base_url=ORIGIN)


def _note(client: TestClient, note_id: str) -> dict[str, Any]:
    response = client.get(f"/api/notes/{note_id}")
    assert response.status_code == 200, response.text
    payload: dict[str, Any] = response.json()
    return payload


def _make_confidential(conn: psycopg.Connection, note_id: str) -> None:
    conn.execute(
        "UPDATE documents SET sensitivity = %s WHERE id = %s::uuid",
        (CONFIDENTIAL, note_id),
    )


# -------------------------------------------------------------- AC 1 / AC 2 --


def test_loopback_payload_carries_the_absolute_path(
    test_db: psycopg.Connection, ui_cfg: Config, fake_embedder: Any, note_id: str
) -> None:
    """AC 1, server half: the path is absolute, resolved, and names the real file."""
    client = _client(test_db, ui_cfg, fake_embedder, serve_abs_path=True)

    payload = _note(client, note_id)

    abs_path = Path(payload["abs_path"])
    assert abs_path.is_absolute()
    assert abs_path == (ui_cfg.vault_path / payload["vault_path"]).resolve()
    assert abs_path.is_file()


def test_a_lan_context_sends_no_abs_path(
    test_db: psycopg.Connection, ui_cfg: Config, fake_embedder: Any, note_id: str
) -> None:
    """AC 2: without the loopback flag the key is absent, and ``vault_path`` stays."""
    client = _client(test_db, ui_cfg, fake_embedder, serve_abs_path=False)

    payload = _note(client, note_id)

    assert "abs_path" not in payload
    assert payload["vault_path"]


def test_the_default_context_sends_no_abs_path(
    test_db: psycopg.Connection, ui_cfg: Config, fake_embedder: Any, note_id: str
) -> None:
    """The dataclass default is the fail-closed one, like ``ingest_enabled``."""
    client = _client(test_db, ui_cfg, fake_embedder)

    assert "abs_path" not in _note(client, note_id)


def test_no_abs_path_when_the_mirror_file_is_missing(
    test_db: psycopg.Connection, ui_cfg: Config, fake_embedder: Any, note_id: str
) -> None:
    """A path no editor could open is not offered; the button falls back to vault_path."""
    client = _client(test_db, ui_cfg, fake_embedder, serve_abs_path=True)
    vault_path = _note(client, note_id)["vault_path"]
    (ui_cfg.vault_path / vault_path).unlink()

    payload = _note(client, note_id)

    assert "abs_path" not in payload


# -------------------------------------------------------------------- lens --


def test_a_withheld_note_never_carries_abs_path(
    test_db: psycopg.Connection, ui_cfg: Config, fake_embedder: Any, note_id: str
) -> None:
    """Privacy: a confidential note whose body is withheld carries no ``abs_path``.

    The loopback flag is set on purpose. A real loopback build always serves
    confidential bodies, so only a hand-built context reaches this shape, and it
    must fail closed rather than make the path the one field that escapes the lens.
    """
    _make_confidential(test_db, note_id)
    client = _client(
        test_db, ui_cfg, fake_embedder,
        serve_abs_path=True, serve_confidential_bodies=False,
    )

    payload = _note(client, note_id)

    assert payload.get("withheld"), "the lens did not withhold this body"
    assert "abs_path" not in payload


def test_a_served_confidential_note_carries_abs_path(
    test_db: psycopg.Connection, ui_cfg: Config, fake_embedder: Any, note_id: str
) -> None:
    """The other half of the lens: a body this session may read keeps its path."""
    _make_confidential(test_db, note_id)
    client = _client(
        test_db, ui_cfg, fake_embedder,
        serve_abs_path=True, serve_confidential_bodies=True,
    )

    payload = _note(client, note_id)

    assert "withheld" not in payload
    assert Path(payload["abs_path"]).is_file()


# ------------------------------------------------------ the derived boolean --


@pytest.mark.parametrize(
    ("bind", "resolved", "expected"),
    [
        ("127.0.0.1", None, True),
        ("::1", None, True),
        ("localhost", ("127.0.0.1", "::1"), True),
        ("192.0.2.10", None, False),  # a LAN bind (`--lan`)
        ("0.0.0.0", None, False),  # wildcard: every interface  # noqa: S104
        ("::", None, False),
        ("loopback-alias.example", ("127.0.0.1",), False),  # round 16 alias
        ("localhost", ("192.0.2.30",), False),
        # Mixed answer: loopback AND a LAN address is not loopback (round 10).
        ("localhost", ("127.0.0.1", "192.0.2.30"), False),
    ],
)
def test_build_context_derives_serve_abs_path_from_the_bind(
    bind: str, resolved: tuple[str, ...] | None, expected: bool
) -> None:
    """AC 2's wiring: the flag follows ``address_class`` and nothing else.

    Not ``read_only`` (copying a path mutates nothing) and not
    ``--include-confidential``, which widens what a LAN session may READ, not
    what it learns about this machine's filesystem.
    """
    addresses = (
        tuple(ipaddress.ip_address(a) for a in resolved) if resolved is not None else None
    )
    for read_only in (False, True):
        for include_confidential in (False, True):
            context = build_context(
                Config(database_url=TEST_DATABASE_URL, embedder="none"),
                host=bind, port=8765, read_only=read_only, token="t",
                include_confidential=include_confidential, embedder=None,
                resolved=addresses,
            )
            assert context.serve_abs_path is expected, (
                bind, resolved, read_only, include_confidential,
            )
