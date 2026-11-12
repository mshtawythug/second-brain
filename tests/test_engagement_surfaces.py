"""The engagement stage on every search surface, configured the way a user configures it.

RM-22b. Each test sets ``BRAIN_ENGAGEMENT_ALPHA`` in the process environment, so
the real path runs: ``build_stages(os.environ, ...)``, the settings leaf, the
real :class:`~brain.search_engagement.EngagementStage`, ``run_search`` and each
surface's rendering. The corpus is RM-20's (:mod:`tests.doc_leg_corpus`), where
plain search ranks the "fit" document last; it has been opened twenty times, so
the stage lifts it to the top.

Covered: ``brain search``, ``brain explain``, MCP ``brain_search`` and
``brain_explain``, ``GET /api/search``, ``brain recall`` and ``ask``'s
retrieval; ``--fts-only`` (the stage still runs: it needs no query vector); and
nothing at all while the stage is off (AC1). ``saved_search_check`` passes
``stages=()`` by design (a watch is a match set, not a ranking), so it is not a
surface here.

Real Postgres (the 5434 test database). All data is synthetic.
"""
from __future__ import annotations

import contextlib
import json
import os
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import psycopg
import pytest
from starlette.testclient import TestClient
from typer.testing import CliRunner

from brain import mcp_server
from brain.ask import _retrieve_hybrid
from brain.cli import app
from brain.config import Config
from brain.mcp_tools import related as related_tool
from brain.recall import recall
from brain.search import hybrid_search
from brain.ui.app import create_app
from brain.ui.context import UiContext
from brain.vault import init_vault
from tests.doc_leg_corpus import OTHERS, QUERY, reconcile, seed
from tests.saved_search_fakes import WordEmbedder

pytestmark = pytest.mark.filterwarnings("ignore::DeprecationWarning")

TEST_DATABASE_URL = os.environ.get(
    "TEST_DATABASE_URL", "postgresql://brain:brain@localhost:5434/second_brain_test"
)
SEEDED = OTHERS + 1
ORIGIN = "http://127.0.0.1:8765"
SEARCH_JSON = ["search", QUERY, "--json", "--no-meta", "--limit", str(SEEDED)]
OPENS = 20


@pytest.fixture
def word(test_db: psycopg.Connection[Any]) -> WordEmbedder:
    embedder = WordEmbedder()
    reconcile(test_db, embedder)
    return embedder


@pytest.fixture
def corpus(test_db: psycopg.Connection[Any], word: WordEmbedder) -> dict[str, str]:
    """The seeded corpus with the fit document opened :data:`OPENS` times; returns its ids."""
    ids = seed(test_db, word)
    for _ in range(OPENS):
        test_db.execute(
            "INSERT INTO interactions (document_id, action, source) VALUES (%s, 'opened', 'cli')",
            (ids["fit"],),
        )
    return ids


class Stage:
    """Turns the engagement stage on for the rest of one test."""

    def __init__(self, monkeypatch: pytest.MonkeyPatch) -> None:
        self._monkeypatch = monkeypatch

    def on(self) -> None:
        self._monkeypatch.setenv("BRAIN_ENGAGEMENT_ALPHA", "1")


@pytest.fixture
def stage(monkeypatch: pytest.MonkeyPatch) -> Stage:
    return Stage(monkeypatch)


@pytest.fixture
def cli_env(patch_embedder: Any, word: WordEmbedder) -> None:
    patch_embedder(word)


def _cli(args: list[str]) -> Any:
    result = CliRunner().invoke(app, args)
    assert result.exit_code == 0, result.output
    return result


def _cli_ids(args: list[str]) -> list[str]:
    return [row["id"] for row in json.loads(_cli(args).stdout)]


def _lifted(plain: list[str], staged: list[str], fit: str) -> None:
    assert plain[-1] == fit
    assert staged[0] == fit
    assert sorted(staged) == sorted(plain)


@pytest.fixture
def mcp_state(
    monkeypatch: pytest.MonkeyPatch,
    test_db: psycopg.Connection[Any],  # noqa: ARG001 — keeps the schema fresh
    word: WordEmbedder,
    tmp_path: Path,
) -> Iterator[mcp_server._State]:
    monkeypatch.setenv("DATABASE_URL", TEST_DATABASE_URL)
    state = mcp_server._State(
        cfg=Config(database_url=TEST_DATABASE_URL, vault_path=tmp_path), embedder=word
    )
    monkeypatch.setattr(mcp_server, "_state", state)
    yield state


def _ui_client(conn: psycopg.Connection[Any], tmp_path: Path, embedder: Any) -> TestClient:
    vault = tmp_path / "vault"
    vault.mkdir()
    init_vault(vault)
    cfg = Config(
        database_url="postgresql://unused/in/these/tests", vault_path=vault, embedder="none"
    )

    @contextlib.contextmanager
    def conn_factory() -> Any:
        yield conn

    context = UiContext(
        cfg=cfg,
        conn_factory=conn_factory,
        embedder=embedder,
        search_fn=hybrid_search,
        allowed_origin=ORIGIN,
        logging_enabled=False,
        serve_confidential_bodies=True,
    )
    return TestClient(create_app(context), base_url=ORIGIN)


# --- every surface ------------------------------------------------------------------


@pytest.mark.usefixtures("cli_env")
def test_cli_search_lifts_the_opened_document(stage: Stage, corpus: dict[str, str]) -> None:
    plain = _cli_ids(SEARCH_JSON)
    stage.on()

    _lifted(plain, _cli_ids(SEARCH_JSON), corpus["fit"])


@pytest.mark.usefixtures("cli_env")
def test_cli_explain_lifts_the_opened_document(stage: Stage, corpus: dict[str, str]) -> None:
    args = ["explain", QUERY, "--json", "--limit", str(SEEDED)]
    plain = _cli_ids(args)
    stage.on()

    _lifted(plain, _cli_ids(args), corpus["fit"])


@pytest.mark.usefixtures("cli_env")
def test_cli_search_meta_reports_the_stage_and_the_footer_its_time(
    stage: Stage, corpus: dict[str, str]  # noqa: ARG001
) -> None:
    stage.on()

    envelope = json.loads(_cli(["search", QUERY, "--json", "--meta"]).stdout)
    footer = _cli(["search", QUERY]).stderr

    (report,) = envelope["stages"]
    assert (report["name"], report["applied"], report["skipped_reason"]) == (
        "engagement",
        True,
        None,
    )
    assert " · engagement " in footer


@pytest.mark.usefixtures("cli_env")
def test_fts_only_still_applies_the_stage(stage: Stage, corpus: dict[str, str]) -> None:
    # The stage reads interactions, not the query vector, so a text-only search keeps it.
    stage.on()

    envelope = json.loads(
        _cli(["search", "lighthouse", "--json", "--meta", "--fts-only"]).stdout
    )

    (report,) = envelope["stages"]
    assert (report["applied"], report["skipped_reason"]) == (True, None)
    assert envelope["results"][0]["id"] == corpus["fit"]


def test_mcp_brain_search_lifts_and_reports_the_stage(
    stage: Stage, corpus: dict[str, str], mcp_state: mcp_server._State  # noqa: ARG001
) -> None:
    plain = [r["id"] for r in mcp_server.brain_search(query=QUERY, limit=SEEDED)["results"]]
    stage.on()

    payload = mcp_server.brain_search(query=QUERY, limit=SEEDED)

    _lifted(plain, [r["id"] for r in payload["results"]], corpus["fit"])
    (report,) = payload["stages"]
    assert (report["name"], report["applied"]) == ("engagement", True)


def test_mcp_brain_explain_lifts(
    stage: Stage, corpus: dict[str, str], mcp_state: mcp_server._State  # noqa: ARG001
) -> None:
    plain = [r["id"] for r in related_tool.brain_explain(query=QUERY, limit=SEEDED)["results"]]
    stage.on()

    rows = related_tool.brain_explain(query=QUERY, limit=SEEDED)["results"]

    _lifted(plain, [r["id"] for r in rows], corpus["fit"])


def test_ui_search_lifts_and_shows_the_stage_time(
    stage: Stage,
    corpus: dict[str, str],
    test_db: psycopg.Connection[Any],
    word: WordEmbedder,
    tmp_path: Path,
) -> None:
    client = _ui_client(test_db, tmp_path, word)
    params = {"q": QUERY, "limit": str(SEEDED)}
    plain_payload = client.get("/api/search", params=params).json()
    stage.on()

    response = client.get("/api/search", params=params)

    assert response.status_code == 200, response.text
    payload = response.json()
    _lifted(
        [r["id"] for r in plain_payload["results"]],
        [r["id"] for r in payload["results"]],
        corpus["fit"],
    )
    assert isinstance(payload["timing_ms"]["engagement"], float)
    assert "engagement" not in plain_payload["timing_ms"]


def test_recall_lifts(
    stage: Stage, corpus: dict[str, str], test_db: psycopg.Connection[Any], word: WordEmbedder
) -> None:
    def _recall_ids() -> list[str]:
        result = recall(
            test_db,
            Config(database_url=TEST_DATABASE_URL),
            embedder=word,
            query=QUERY,
            budget_tokens=4000,
            max_candidates=25,
        )
        return [p.document_id for p in result.passages]

    plain = _recall_ids()
    stage.on()

    _lifted(plain, _recall_ids(), corpus["fit"])


def test_ask_retrieval_lifts(
    stage: Stage, corpus: dict[str, str], test_db: psycopg.Connection[Any], word: WordEmbedder
) -> None:
    cfg = Config(database_url=TEST_DATABASE_URL)

    def _ask_ids() -> list[str]:
        searched = _retrieve_hybrid(
            test_db, cfg, embedder=word, query=QUERY, limit=SEEDED, fallback=True
        )
        return [r.document_id for r in searched.results]

    plain = _ask_ids()
    stage.on()

    _lifted(plain, _ask_ids(), corpus["fit"])


# --- AC1: off changes nothing -----------------------------------------------------------


@pytest.mark.usefixtures("cli_env")
def test_off_means_no_stage_and_the_same_rows(
    corpus: dict[str, str], mcp_state: mcp_server._State  # noqa: ARG001
) -> None:
    # The fit document has twenty opens, and still sits where plain search puts it.
    envelope = json.loads(_cli(["search", QUERY, "--json", "--meta"]).stdout)

    assert envelope["stages"] == []
    assert envelope["results"][-1]["id"] == corpus["fit"]
    assert mcp_server.brain_search(query=QUERY)["stages"] == []
