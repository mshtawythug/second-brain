"""Every user-facing search surface goes through ``brain.search_pipeline`` (RM-09).

One test per surface (acceptance criterion 2): a reversing stage installed
through :func:`brain.search_pipeline.build_stages` (the one place a stage is
wired) reorders ``brain search``, ``brain explain``, MCP ``brain_search``,
``GET /api/search``, ``brain recall`` and the retrieval of ``brain ask``. Plus
criterion 3 end to end on the CLI envelope, criterion 5 on the UI route, and the
``stages`` key: present in the ``--json --meta`` envelope and MCP, absent
everywhere else.

Real Postgres (the 5434 test database) and the real ``hybrid_search``. All data
is synthetic.
"""
from __future__ import annotations

import contextlib
import json
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import psycopg
import pytest
from starlette.testclient import TestClient
from typer.testing import CliRunner

from brain import mcp_server, search_pipeline
from brain.ask import _retrieve_hybrid
from brain.cli import app
from brain.config import Config
from brain.ingest import ExtractedDoc, ingest_document
from brain.recall import recall
from brain.search import hybrid_search
from brain.ui.app import create_app
from brain.ui.context import UiContext
from brain.vault import init_vault
from tests.conftest import TEST_DATABASE_URL
from tests.search_stages import STAGE_SECRET, RaisingStage, ReverseStage

pytestmark = pytest.mark.filterwarnings("ignore::DeprecationWarning")

#: A nonsense term so nothing here depends on real content.
TERM = "lanternwick"
SEEDED = 4
ORIGIN = "http://127.0.0.1:8765"


@pytest.fixture
def seeded(test_db: psycopg.Connection[Any], fake_embedder: Any) -> None:
    """Ingest notes that all match :data:`TERM`, each with a distinct body."""
    for i in range(SEEDED):
        ingest_document(
            test_db,
            embedder=fake_embedder,
            doc=ExtractedDoc(
                title=f"Lantern note {i}",
                content=f"The {TERM} inventory counted {i} spare wicks on shelf {i}.",
                content_type="note",
                source_path=None,
                metadata={},
            ),
            source_kind="manual",
            source_external_id=f"manual:lantern-{i}",
        )


@pytest.fixture
def cli_embedder(patch_embedder: Any, fake_embedder: Any) -> None:
    """Point the CLI at the test database and the fake embedder."""
    patch_embedder(fake_embedder)


def _install(monkeypatch: pytest.MonkeyPatch, *stages: Any) -> None:
    """Configure ``stages`` the way a shipped stage will be: in ``build_stages``."""
    monkeypatch.setattr(search_pipeline, "build_stages", lambda env: tuple(stages))


@pytest.fixture
def mcp_state(
    monkeypatch: pytest.MonkeyPatch,
    test_db: psycopg.Connection[Any],  # noqa: ARG001 — keeps the schema fresh
    fake_embedder: Any,
) -> Iterator[mcp_server._State]:
    state = mcp_server._State(
        cfg=Config(database_url=TEST_DATABASE_URL), embedder=fake_embedder
    )
    monkeypatch.setattr(mcp_server, "_state", state)
    yield state


class RecordingSearch:
    """``hybrid_search``, with every call's kwargs kept for the assertions."""

    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    def __call__(self, conn: psycopg.Connection[Any], **kwargs: Any) -> Any:
        self.calls.append(kwargs)
        return hybrid_search(conn, **kwargs)


def _ui_client(
    conn: psycopg.Connection[Any], tmp_path: Path, embedder: Any, search: Any
) -> TestClient:
    vault = tmp_path / "vault"
    vault.mkdir()
    init_vault(vault)
    cfg = Config(
        database_url="postgresql://unused/in/these/tests",
        vault_path=vault,
        embedder="none",
    )

    @contextlib.contextmanager
    def conn_factory() -> Any:
        yield conn

    context = UiContext(
        cfg=cfg,
        conn_factory=conn_factory,
        embedder=embedder,
        search_fn=search,
        allowed_origin=ORIGIN,
        logging_enabled=False,
        serve_confidential_bodies=True,
    )
    return TestClient(create_app(context), base_url=ORIGIN)


def _cli_ids(args: list[str]) -> list[str]:
    result = CliRunner().invoke(app, args)
    assert result.exit_code == 0, result.output
    return [row["id"] for row in json.loads(result.stdout)]


# --- criterion 2: one test per surface ----------------------------------


@pytest.mark.usefixtures("seeded", "cli_embedder")
def test_cli_search_applies_the_configured_stage(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Arrange
    plain = _cli_ids(["search", TERM, "--json", "--no-meta"])
    stage = ReverseStage()
    _install(monkeypatch, stage)

    # Act
    staged = _cli_ids(["search", TERM, "--json", "--no-meta"])

    # Assert
    assert len(plain) == SEEDED
    assert staged == list(reversed(plain))
    assert stage.calls[0]["query"] == TERM
    assert stage.calls[0]["session_id"] is None


@pytest.mark.usefixtures("seeded", "cli_embedder")
def test_cli_explain_applies_the_configured_stage(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Arrange
    plain = _cli_ids(["explain", TERM, "--json"])
    _install(monkeypatch, ReverseStage())

    # Act
    staged = _cli_ids(["explain", TERM, "--json"])

    # Assert
    assert len(plain) == SEEDED
    assert staged == list(reversed(plain))


@pytest.mark.usefixtures("seeded")
def test_mcp_brain_search_applies_the_configured_stage(
    monkeypatch: pytest.MonkeyPatch, mcp_state: mcp_server._State  # noqa: ARG001
) -> None:
    # Arrange
    plain = [r["id"] for r in mcp_server.brain_search(query=TERM)["results"]]
    stage = ReverseStage()
    _install(monkeypatch, stage)

    # Act
    payload = mcp_server.brain_search(query=TERM)

    # Assert
    assert len(plain) == SEEDED
    assert [r["id"] for r in payload["results"]] == list(reversed(plain))
    assert stage.calls[0]["session_id"] == payload["session_id"]
    assert payload["stages"] == [
        {
            "name": "reverse",
            "applied": True,
            "skipped_reason": None,
            "ms": payload["stages"][0]["ms"],
        }
    ]


@pytest.mark.usefixtures("seeded")
def test_ui_search_applies_the_configured_stage(
    monkeypatch: pytest.MonkeyPatch,
    test_db: psycopg.Connection[Any],
    fake_embedder: Any,
    tmp_path: Path,
) -> None:
    # Arrange
    client = _ui_client(test_db, tmp_path, fake_embedder, RecordingSearch())
    plain = [r["id"] for r in client.get("/api/search", params={"q": TERM}).json()["results"]]
    stage = ReverseStage()
    _install(monkeypatch, stage)

    # Act
    response = client.get("/api/search", params={"q": TERM})

    # Assert
    assert response.status_code == 200, response.text
    payload = response.json()
    assert len(plain) == SEEDED
    assert [r["id"] for r in payload["results"]] == list(reversed(plain))
    assert stage.calls[0]["session_id"] == payload["session_id"]


@pytest.mark.usefixtures("seeded")
def test_recall_applies_the_configured_stage(
    monkeypatch: pytest.MonkeyPatch,
    test_db: psycopg.Connection[Any],
    fake_embedder: Any,
) -> None:
    # Arrange
    def _recall_ids() -> list[str]:
        result = recall(
            test_db,
            Config(database_url=TEST_DATABASE_URL),
            embedder=fake_embedder,
            query=TERM,
            budget_tokens=4000,
            max_candidates=25,
        )
        return [p.document_id for p in result.passages]

    plain = _recall_ids()
    _install(monkeypatch, ReverseStage())

    # Act
    staged = _recall_ids()

    # Assert
    assert len(plain) == SEEDED
    assert staged == list(reversed(plain))


@pytest.mark.parametrize("fallback", [True, False])
@pytest.mark.usefixtures("seeded")
def test_ask_retrieval_applies_the_configured_stage(
    monkeypatch: pytest.MonkeyPatch,
    test_db: psycopg.Connection[Any],
    fake_embedder: Any,
    fallback: bool,
) -> None:
    """``fallback=False`` (``brain eval --answer``) gets the stages too."""

    # Arrange
    def _ask_ids() -> list[str]:
        searched = _retrieve_hybrid(
            test_db,
            Config(database_url=TEST_DATABASE_URL),
            embedder=fake_embedder,
            query=TERM,
            limit=10,
            fallback=fallback,
        )
        return [r.document_id for r in searched.results]

    plain = _ask_ids()
    _install(monkeypatch, ReverseStage())

    # Act
    staged = _ask_ids()

    # Assert
    assert len(plain) == SEEDED
    assert staged == list(reversed(plain))


# --- criterion 3, end to end ---------------------------------------------


@pytest.mark.usefixtures("seeded", "cli_embedder")
def test_a_raising_stage_is_reported_and_the_rows_are_unstaged(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Arrange
    args = ["search", TERM, "--json", "--meta"]
    plain = CliRunner().invoke(app, args)
    _install(monkeypatch, RaisingStage())

    # Act
    result = CliRunner().invoke(app, args)

    # Assert
    assert result.exit_code == 0, result.output
    payload = json.loads(result.stdout)
    # Ids, not whole rows: the recency boost reads the clock, so a score can
    # move in its last digits between two runs.
    assert [r["id"] for r in payload["results"]] == [
        r["id"] for r in json.loads(plain.stdout)["results"]
    ]
    assert payload["stages"] == [
        {
            "name": "raising",
            "applied": False,
            "skipped_reason": "StageBackendDown",
            "ms": payload["stages"][0]["ms"],
        }
    ]
    assert STAGE_SECRET not in result.output


# --- criterion 5: the UI route passes ``limit`` once ---------------------


@pytest.mark.usefixtures("seeded")
def test_ui_route_asks_for_fetch_limit_once_and_returns_one_page(
    monkeypatch: pytest.MonkeyPatch,
    test_db: psycopg.Connection[Any],
    fake_embedder: Any,
    tmp_path: Path,
) -> None:
    # Arrange: a stage that wants to see 9 rows.
    search = RecordingSearch()
    client = _ui_client(test_db, tmp_path, fake_embedder, search)
    _install(monkeypatch, ReverseStage(depth=9))

    # Act
    plain_page = client.get("/api/search", params={"q": TERM, "limit": 2, "offset": 1})

    # Assert
    assert plain_page.status_code == 200, plain_page.text
    payload = plain_page.json()
    assert len(payload["results"]) == 2
    assert [call["limit"] for call in search.calls] == [9]


@pytest.mark.usefixtures("seeded")
def test_ui_route_without_stages_asks_for_exactly_the_page_it_serves(
    test_db: psycopg.Connection[Any], fake_embedder: Any, tmp_path: Path
) -> None:
    # Arrange
    search = RecordingSearch()
    client = _ui_client(test_db, tmp_path, fake_embedder, search)

    # Act
    response = client.get("/api/search", params={"q": TERM, "limit": 2, "offset": 1})

    # Assert
    assert response.status_code == 200, response.text
    assert [call["limit"] for call in search.calls] == [3]
    assert "stages" not in response.json()


# --- the ``stages`` key: envelope and MCP only ---------------------------


@pytest.mark.usefixtures("seeded", "cli_embedder")
def test_the_meta_envelope_carries_an_empty_stages_list() -> None:
    # Act
    result = CliRunner().invoke(app, ["search", TERM, "--json", "--meta"])

    # Assert
    assert result.exit_code == 0, result.output
    payload = json.loads(result.stdout)
    assert payload["stages"] == []
    keys = list(payload)
    assert keys.index("stages") == keys.index("fallback_note") + 1


@pytest.mark.usefixtures("seeded")
def test_mcp_brain_search_carries_an_empty_stages_list(
    mcp_state: mcp_server._State,  # noqa: ARG001
) -> None:
    # Act
    payload = mcp_server.brain_search(query=TERM)

    # Assert
    assert payload["stages"] == []
