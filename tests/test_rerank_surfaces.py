"""The reranker on every search surface, configured the way a user configures it (RM-14a).

Each test sets ``BRAIN_RERANKER=llamacpp`` in the process environment and
swaps only the BACKEND (``rerank_backends.make_reranker``) for a fake, so the
real path runs everywhere else: ``build_stages(os.environ)``, the settings
leaf, the real :class:`~brain.rerank.RerankStage`, ``run_search`` and each
surface's own rendering. The fake prefers the hybrid order reversed, and the
depth is 2 of 4 rows, so the head flips and the tail stays.

Covered: ``brain search``, ``brain explain``, MCP ``brain_search`` and
``brain_explain``, ``GET /api/search``, ``brain recall`` and ``ask``'s
retrieval (AC2); a failing backend (AC4); the one-run flags; the footer and
the UI's timing key; and nothing at all while the setting is ``none`` (AC1).

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

from brain import mcp_server, rerank_backends
from brain.ask import _retrieve_hybrid
from brain.cli import app
from brain.config import Config
from brain.ingest import ExtractedDoc, ingest_document
from brain.mcp_tools import related as related_tool
from brain.recall import recall
from brain.rerank import RERANK_SKIPPED_NOTICE
from brain.search import hybrid_search
from brain.ui.app import create_app
from brain.ui.context import UiContext
from brain.vault import init_vault
from tests.rerank_fakes import FakeReranker, RaisingReranker, title_scorer

pytestmark = pytest.mark.filterwarnings("ignore::DeprecationWarning")

TEST_DATABASE_URL = os.environ.get(
    "TEST_DATABASE_URL", "postgresql://brain:brain@localhost:5434/second_brain_test"
)
#: A nonsense term so nothing here depends on real content.
TERM = "gorsemantle"
SEEDED = 4
DEPTH = 2
ORIGIN = "http://127.0.0.1:8765"


@pytest.fixture
def titles(test_db: psycopg.Connection[Any], fake_embedder: Any) -> dict[str, str]:
    """Four notes that all match :data:`TERM`; returns id → title."""
    out: dict[str, str] = {}
    for i in range(SEEDED):
        title = f"Gorse note {i}"
        result = ingest_document(
            test_db,
            embedder=fake_embedder,
            doc=ExtractedDoc(
                title=title,
                content=f"The {TERM} ledger names shelf {i} and {'x' * i}.",
                content_type="note",
                source_path=None,
                metadata={},
            ),
            source_kind="manual",
            source_external_id=f"manual:gorse-{i}",
        )
        out[str(result.document_id)] = title
    return out


@pytest.fixture
def cli_env(monkeypatch: pytest.MonkeyPatch, patch_embedder: Any, fake_embedder: Any) -> None:
    patch_embedder(fake_embedder)


class Backend:
    """Installs a fake backend behind ``BRAIN_RERANKER=llamacpp``."""

    def __init__(self, monkeypatch: pytest.MonkeyPatch) -> None:
        self._monkeypatch = monkeypatch
        self.reranker: FakeReranker | None = None

    def install(self, reranker: FakeReranker) -> FakeReranker:
        self.reranker = reranker
        self._monkeypatch.setenv("BRAIN_RERANKER", "llamacpp")
        self._monkeypatch.setenv("BRAIN_RERANK_DEPTH", str(DEPTH))
        self._monkeypatch.setattr(rerank_backends, "make_reranker", lambda _settings: reranker)
        return reranker

    def reversing(self, titles: dict[str, str], plain_ids: list[str]) -> FakeReranker:
        """A reranker that prefers ``plain_ids`` reversed."""
        return self.install(FakeReranker(title_scorer([titles[i] for i in reversed(plain_ids)])))


@pytest.fixture
def backend(monkeypatch: pytest.MonkeyPatch) -> Backend:
    monkeypatch.delenv("BRAIN_RERANKER", raising=False)
    monkeypatch.delenv("BRAIN_RERANK_DEPTH", raising=False)
    return Backend(monkeypatch)


def _flipped(plain: list[str]) -> list[str]:
    """The head of ``DEPTH`` reversed, the tail kept."""
    return list(reversed(plain[:DEPTH])) + plain[DEPTH:]


def _cli(args: list[str]) -> Any:
    result = CliRunner().invoke(app, args)
    assert result.exit_code == 0, result.output
    return result


def _cli_ids(args: list[str]) -> list[str]:
    return [row["id"] for row in json.loads(_cli(args).stdout)]


@pytest.fixture
def mcp_state(
    monkeypatch: pytest.MonkeyPatch,
    test_db: psycopg.Connection[Any],  # noqa: ARG001 — keeps the schema fresh
    fake_embedder: Any,
    tmp_path: Path,
) -> Iterator[mcp_server._State]:
    monkeypatch.setenv("DATABASE_URL", TEST_DATABASE_URL)
    state = mcp_server._State(
        cfg=Config(database_url=TEST_DATABASE_URL, vault_path=tmp_path), embedder=fake_embedder
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


# --- AC2: every surface ----------------------------------------------------------


SEARCH_JSON = ["search", TERM, "--json", "--no-meta", "--limit", str(SEEDED)]


@pytest.mark.usefixtures("cli_env")
def test_cli_search_reranks_the_head_and_keeps_the_tail(
    backend: Backend, titles: dict[str, str]
) -> None:
    plain = _cli_ids(SEARCH_JSON)
    reranker = backend.reversing(titles, plain)

    staged = _cli_ids(SEARCH_JSON)

    assert len(plain) == SEEDED
    assert staged == _flipped(plain)
    assert len(reranker.passages_sent) == DEPTH


@pytest.mark.usefixtures("cli_env")
def test_cli_explain_reranks_and_reports_the_reranker_score(
    backend: Backend, titles: dict[str, str]
) -> None:
    args = ["explain", TERM, "--json", "--limit", str(SEEDED)]
    plain = [row["id"] for row in json.loads(_cli(args).stdout)]
    backend.reversing(titles, plain)

    rows = json.loads(_cli(args).stdout)

    assert [row["id"] for row in rows] == _flipped(plain)
    scores = [row["explain"]["reranker_score"] for row in rows]
    assert all(isinstance(s, float) for s in scores[:DEPTH])
    assert scores[DEPTH:] == [None] * (SEEDED - DEPTH)


@pytest.mark.usefixtures("titles")
def test_mcp_brain_search_reranks_and_reports_the_stage(
    backend: Backend, titles: dict[str, str], mcp_state: mcp_server._State  # noqa: ARG002
) -> None:
    plain = [r["id"] for r in mcp_server.brain_search(query=TERM, limit=SEEDED)["results"]]
    backend.reversing(titles, plain)

    payload = mcp_server.brain_search(query=TERM, limit=SEEDED)

    assert [r["id"] for r in payload["results"]] == _flipped(plain)
    (stage,) = payload["stages"]
    assert (stage["name"], stage["applied"], stage["skipped_reason"]) == ("rerank", True, None)


def test_mcp_brain_explain_reranks_and_reports_the_reranker_score(
    backend: Backend, titles: dict[str, str], mcp_state: mcp_server._State  # noqa: ARG001
) -> None:
    plain = [r["id"] for r in related_tool.brain_explain(query=TERM, limit=SEEDED)["results"]]
    backend.reversing(titles, plain)

    rows = related_tool.brain_explain(query=TERM, limit=SEEDED)["results"]

    assert [r["id"] for r in rows] == _flipped(plain)
    assert rows[0]["explain"]["reranker_score"] is not None


def test_ui_search_reranks_and_shows_the_rerank_timing(
    backend: Backend,
    titles: dict[str, str],
    test_db: psycopg.Connection[Any],
    fake_embedder: Any,
    tmp_path: Path,
) -> None:
    client = _ui_client(test_db, tmp_path, fake_embedder)
    params = {"q": TERM, "limit": str(SEEDED)}
    plain_payload = client.get("/api/search", params=params).json()
    plain = [r["id"] for r in plain_payload["results"]]
    backend.reversing(titles, plain)

    response = client.get("/api/search", params=params)

    assert response.status_code == 200, response.text
    payload = response.json()
    assert [r["id"] for r in payload["results"]] == _flipped(plain)
    assert isinstance(payload["timing_ms"]["rerank"], float)
    # Off, the key does not exist at all: the response is what it always was.
    assert "rerank" not in plain_payload["timing_ms"]
    assert set(payload["timing_ms"]) == set(plain_payload["timing_ms"]) | {"rerank"}


def test_recall_reranks(
    backend: Backend,
    titles: dict[str, str],
    test_db: psycopg.Connection[Any],
    fake_embedder: Any,
) -> None:
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
    backend.reversing(titles, plain)

    assert _recall_ids() == _flipped(plain)


def test_ask_retrieval_reranks(
    backend: Backend,
    titles: dict[str, str],
    test_db: psycopg.Connection[Any],
    fake_embedder: Any,
) -> None:
    cfg = Config(database_url=TEST_DATABASE_URL)

    def _ask_ids() -> list[str]:
        searched = _retrieve_hybrid(
            test_db, cfg, embedder=fake_embedder, query=TERM, limit=SEEDED, fallback=True
        )
        return [r.document_id for r in searched.results]

    plain = _ask_ids()
    backend.reversing(titles, plain)

    assert _ask_ids() == _flipped(plain)


# --- AC4: a failing backend ------------------------------------------------------


@pytest.mark.usefixtures("cli_env")
def test_a_failing_backend_keeps_hybrid_order_and_says_so_once(
    backend: Backend, titles: dict[str, str]  # noqa: ARG001
) -> None:
    plain = _cli_ids(SEARCH_JSON)
    backend.install(RaisingReranker())

    result = _cli(SEARCH_JSON)

    assert [row["id"] for row in json.loads(result.stdout)] == plain
    assert result.stderr.count(RERANK_SKIPPED_NOTICE) == 1


@pytest.mark.usefixtures("cli_env", "titles")
def test_an_unreachable_llama_server_is_a_skip_not_an_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """No fake at all: the real backend, pointed at a closed loopback port."""
    monkeypatch.setenv("BRAIN_RERANKER", "llamacpp")
    monkeypatch.setenv("BRAIN_RERANK_URL", "http://127.0.0.1:9")
    monkeypatch.setenv("BRAIN_RERANK_TIMEOUT_S", "1")

    result = _cli(["search", TERM, "--json", "--meta"])

    envelope = json.loads(result.stdout)
    (stage,) = envelope["stages"]
    assert stage["applied"] is False
    assert stage["skipped_reason"] == "RerankError from ConnectError"
    assert len(envelope["results"]) > 0
    assert RERANK_SKIPPED_NOTICE in result.stderr


# --- the one-run flags -------------------------------------------------------------


@pytest.mark.usefixtures("cli_env")
def test_no_rerank_turns_a_configured_reranker_off_for_one_run(
    backend: Backend, titles: dict[str, str]
) -> None:
    explain_args = ["explain", TERM, "--json", "--limit", str(SEEDED)]
    plain = _cli_ids(SEARCH_JSON)
    plain_explain = _cli_ids(explain_args)
    reranker = backend.reversing(titles, plain)

    assert _cli_ids([*SEARCH_JSON, "--no-rerank"]) == plain
    assert _cli_ids([*explain_args, "--no-rerank"]) == plain_explain
    assert reranker.calls == []
    assert _cli_ids([*SEARCH_JSON, "--rerank"]) == _flipped(plain)


@pytest.mark.usefixtures("cli_env", "titles")
@pytest.mark.parametrize("command", ["search", "explain"])
def test_rerank_without_a_backend_is_a_usage_error(backend: Backend, command: str) -> None:
    del backend  # the fixture clears BRAIN_RERANKER
    result = CliRunner().invoke(app, [command, TERM, "--json", "--rerank"])

    assert result.exit_code == 2
    assert "BRAIN_RERANKER" in result.output


# --- the footer --------------------------------------------------------------------


@pytest.mark.usefixtures("cli_env")
def test_the_footer_shows_the_rerank_time(backend: Backend, titles: dict[str, str]) -> None:
    plain_footer = _cli(["search", TERM]).stderr
    backend.install(FakeReranker(title_scorer(list(titles.values()))))

    footer = _cli(["search", TERM]).stderr

    assert " · rerank " not in plain_footer
    assert " · rerank " in footer
    assert footer.index(" · rerank ") < footer.index(" · total ")


# --- AC1: off changes nothing -------------------------------------------------------


@pytest.mark.usefixtures("cli_env", "titles")
def test_off_means_no_stage_on_any_envelope(
    backend: Backend, mcp_state: mcp_server._State  # noqa: ARG001
) -> None:
    del backend  # BRAIN_RERANKER cleared

    envelope = json.loads(_cli(["search", TERM, "--json", "--meta"]).stdout)

    assert envelope["stages"] == []
    assert mcp_server.brain_search(query=TERM)["stages"] == []
