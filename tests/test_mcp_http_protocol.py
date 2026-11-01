"""``brain mcp serve`` end to end: a real server process, a real MCP client (RM-46).

Each test starts ``python -m brain mcp serve`` on a free loopback port against
the TEST database, with ``BRAIN_HOME`` in ``tmp_path`` and the FTS-only
embedder, then talks to it with the SDK's streamable-HTTP client. The server is
stopped by its own PID in a ``finally``, and the test asserts it exited. All
documents are synthetic.
"""
from __future__ import annotations

import asyncio
import importlib
import importlib.util
import json
import os
import socket
import subprocess
import sys
import time
from collections.abc import Awaitable, Callable, Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

import httpx
import psycopg
import pytest

from brain.mcp_auth import generate_token, write_token
from brain.mcp_compat import MCP_MAJOR
from brain.mcp_http import database_reachable

pytestmark = pytest.mark.skipif(MCP_MAJOR < 2, reason="the HTTP transport needs mcp 2.x")

TEST_DATABASE_URL = os.environ.get(
    "TEST_DATABASE_URL", "postgresql://brain:brain@localhost:5434/second_brain_test"
)
#: The registry at RM-46 and its read-only slice.
ALL_TOOLS = 51
READ_TOOLS = 28
#: Round 5: without --include-confidential the graph family (6 read tools; 12
#: prefixed plus brain_review_weekly, sec-118e) is withheld, so the default serves
#: 22 and --allow-writes serves 38.
GRAPH_READ_TOOLS = 6
GRAPH_TOOLS = 13
STARTUP_DEADLINE_S = 60.0


def _free_port() -> int:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return int(probe.getsockname()[1])


@contextmanager
def _serve(tmp_path: Path, *args: str) -> Iterator[str]:
    """Run ``brain mcp serve`` until the block ends; yield its endpoint URL."""
    port = _free_port()
    home = tmp_path / "home"
    log = tmp_path / f"serve-{port}.log"
    env = {
        **os.environ,
        "DATABASE_URL": TEST_DATABASE_URL,
        "BRAIN_HOME": str(home),
        "BRAIN_EMBEDDER": "none",
        "BRAIN_IGNORE_CWD_DOTENV": "1",
        "OLLAMA_HOST": "http://127.0.0.1:1",
    }
    env.pop("BRAIN_MCP_TOKEN", None)
    env.pop("BRAIN_MCP_TOKEN_PATH", None)
    with log.open("wb") as sink:
        proc = subprocess.Popen(
            [sys.executable, "-m", "brain", "mcp", "serve", "--port", str(port), *args],
            env=env,
            cwd=tmp_path,
            stdout=sink,
            stderr=sink,
            stdin=subprocess.DEVNULL,
        )
        try:
            _wait_until_healthy(proc, port, log)
            yield f"http://127.0.0.1:{port}/mcp"
        finally:
            proc.terminate()
            try:
                proc.wait(timeout=15)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait(timeout=15)
            assert proc.poll() is not None


def _wait_until_healthy(proc: subprocess.Popen[bytes], port: int, log: Path) -> None:
    deadline = time.monotonic() + STARTUP_DEADLINE_S
    while time.monotonic() < deadline:
        if proc.poll() is not None:
            raise AssertionError(f"server exited early:\n{log.read_text(errors='replace')}")
        try:
            if httpx.get(f"http://127.0.0.1:{port}/healthz", timeout=1).status_code == 200:
                return
        except httpx.HTTPError:
            pass
        time.sleep(0.2)
    raise AssertionError(f"server never became healthy:\n{log.read_text(errors='replace')}")


def _http_client(token: str | None) -> Any:
    """The client library the installed SDK's transport takes (httpx2 from mcp 2.3)."""
    name = "httpx2" if importlib.util.find_spec("httpx2") else "httpx"
    module = importlib.import_module(name)
    headers = {"Authorization": f"Bearer {token}"} if token else {}
    return module.AsyncClient(headers=headers, timeout=60)


async def _with_session(
    url: str, token: str | None, use: Callable[[Any], Awaitable[Any]]
) -> Any:
    from mcp import ClientSession
    from mcp.client.streamable_http import streamable_http_client

    async with (
        _http_client(token) as http,
        streamable_http_client(url, http_client=http) as (read, write),
        ClientSession(read, write) as session,
    ):
        await session.initialize()
        return await use(session)


async def _tool_names(session: Any) -> set[str]:
    return {tool.name for tool in (await session.list_tools()).tools}


def _payload(result: Any) -> Any:
    dumped = result.model_dump(by_alias=True)
    assert not dumped["isError"], dumped
    return json.loads(dumped["content"][0]["text"])


def _search(query: str, *, include_confidential: bool) -> Callable[[Any], Awaitable[Any]]:
    async def use(session: Any) -> Any:
        return _payload(
            await session.call_tool(
                "brain_search",
                {"query": query, "fts_only": True, "include_confidential": include_confidential},
            )
        )

    return use


def _leaf_messages(exc: BaseException) -> list[str]:
    """Every message in ``exc``, through the client's task-group exception groups."""
    if isinstance(exc, BaseExceptionGroup):
        return [message for inner in exc.exceptions for message in _leaf_messages(inner)]
    return [f"{type(exc).__name__}: {exc}"]


def _titles(payload: Any) -> set[str]:
    return {row["title"] for row in payload["results"]}


@pytest.fixture
def corpus(test_db: psycopg.Connection[Any], seed_doc: Callable[..., str]) -> None:
    seed_doc(title="Example Project Alpha", content="Alpha launch plan for Example Co.")
    secret_id = seed_doc(
        title="Example Project Bravo", content="Bravo launch plan, confidential tier."
    )
    test_db.execute(
        "UPDATE documents SET sensitivity = 'confidential' WHERE id = %s", (secret_id,)
    )


def test_streamable_http_serves_the_read_tools(tmp_path: Path) -> None:
    with _serve(tmp_path, "--no-auth") as url:
        names = asyncio.run(_with_session(url, None, _tool_names))

    assert "brain_search" in names
    assert len(names) == READ_TOOLS - GRAPH_READ_TOOLS
    assert not {"brain_edit", "brain_rm", "brain_graphrag_build", "brain_graphrag_search"} & names


def test_allow_writes_serves_every_tool(tmp_path: Path) -> None:
    with _serve(tmp_path, "--no-auth", "--allow-writes") as url:
        names = asyncio.run(_with_session(url, None, _tool_names))

    assert len(names) == ALL_TOOLS - GRAPH_TOOLS
    assert "brain_edit" in names


def test_the_bearer_token_guards_the_endpoint(tmp_path: Path) -> None:
    token = generate_token()
    write_token(tmp_path / "home" / "mcp_token", token)

    with _serve(tmp_path) as url:
        refused = httpx.post(url, json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"})
        names = asyncio.run(_with_session(url, token, _tool_names))

    assert refused.status_code == 401
    assert len(names) == READ_TOOLS - GRAPH_READ_TOOLS


def test_include_confidential_serves_the_graph_family_and_it_answers(tmp_path: Path) -> None:
    async def stats(session: Any) -> Any:
        return _payload(await session.call_tool("brain_graphrag_stats", {}))

    with _serve(tmp_path, "--no-auth", "--include-confidential") as url:
        names = asyncio.run(_with_session(url, None, _tool_names))
        payload = asyncio.run(_with_session(url, None, stats))
    with _serve(tmp_path, "--no-auth", "--include-confidential", "--allow-writes") as url:
        every = asyncio.run(_with_session(url, None, _tool_names))

    assert len(names) == READ_TOOLS
    assert "brain_graphrag_search" in names
    assert "total_entities" in payload
    assert len(every) == ALL_TOOLS


@pytest.mark.usefixtures("corpus")
def test_confidential_documents_are_refused_unless_the_server_opts_in(tmp_path: Path) -> None:
    with _serve(tmp_path, "--no-auth") as url:
        ordinary = asyncio.run(
            _with_session(url, None, _search("launch", include_confidential=False))
        )
        with pytest.raises(BaseException) as refused:
            asyncio.run(_with_session(url, None, _search("launch", include_confidential=True)))

    with _serve(tmp_path, "--no-auth", "--include-confidential") as url:
        opted_in = asyncio.run(
            _with_session(url, None, _search("launch", include_confidential=True))
        )

    messages = _leaf_messages(refused.value)
    assert any("--include-confidential" in message for message in messages), messages
    assert _titles(ordinary) == {"Example Project Alpha"}
    assert _titles(opted_in) == {"Example Project Alpha", "Example Project Bravo"}


@pytest.mark.usefixtures("corpus")
def test_overlapping_calls_each_get_their_own_connection(tmp_path: Path) -> None:
    # RM-46.D5: calls run concurrently on worker threads; a shared psycopg
    # connection would interleave them.
    async def overlapping(url: str) -> list[Any]:
        return list(
            await asyncio.gather(
                *(
                    _with_session(url, None, _search("launch", include_confidential=False))
                    for _ in range(4)
                )
            )
        )

    with _serve(tmp_path, "--no-auth") as url:
        payloads = asyncio.run(overlapping(url))

    assert all(_titles(payload) == {"Example Project Alpha"} for payload in payloads)


@pytest.mark.usefixtures("test_db")
def test_the_readiness_check_reaches_the_test_database() -> None:
    assert database_reachable(TEST_DATABASE_URL) is True


@pytest.mark.usefixtures("test_db")
def test_a_statement_cannot_outlive_the_call_deadline(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """sec-118 #4: a 504 must not leave its query running in Postgres."""
    from starlette.testclient import TestClient

    from brain import mcp_server
    from brain.config import Config
    from brain.mcp_auth import TokenInfo, fingerprint
    from brain.mcp_compat import make_server
    from brain.mcp_http import HttpServeOptions, serve_http
    from tests.conftest import isolate_config_dotenv

    isolate_config_dotenv(monkeypatch, tmp_path)
    monkeypatch.setenv("BRAIN_EMBEDDER", "none")
    monkeypatch.setenv("DATABASE_URL", TEST_DATABASE_URL)
    monkeypatch.setattr(mcp_server, "_state", None)
    marker = "rm46-synthetic-sleep"
    server = make_server("throwaway")

    def brain_show(id_prefix: str) -> dict[str, Any]:
        """A stand-in read tool whose query outlives its deadline."""
        url = mcp_server._get_state().cfg.database_url
        with psycopg.connect(url, autocommit=True) as conn:
            conn.execute(f"SELECT pg_sleep(5) /* {marker} */")
        return {"id": id_prefix}

    server.tool()(brain_show)
    secret = "A" * 43
    token = TokenInfo(
        path=None, secret=secret, mode_octal="", mode_ok=True, fingerprint=fingerprint(secret)
    )
    apps: list[Any] = []
    opts = HttpServeOptions(token=token, port=_free_port(), call_timeout_s=1.0)
    serve_http(
        opts, Config.load(), server=server, run=lambda app, *_: apps.append(app),
        announce=lambda _line: None,
    )

    body = {"jsonrpc": "2.0", "id": 1, "method": "tools/call",
            "params": {"name": "brain_show", "arguments": {"id_prefix": "abcdef"}}}
    headers = {"Authorization": f"Bearer {secret}", "Accept": "application/json, text/event-stream",
               "Host": "127.0.0.1:8787"}
    with TestClient(apps[0], base_url="http://127.0.0.1:8787") as client:
        response = client.post("/mcp", json=body, headers=headers)
        time.sleep(1.5)
        with psycopg.connect(TEST_DATABASE_URL, autocommit=True) as watcher:
            running = watcher.execute(
                "SELECT count(*) FROM pg_stat_activity "
                "WHERE query LIKE %s AND pid <> pg_backend_pid()",
                (f"%{marker}%",),
            ).fetchone()

    assert response.status_code == 504
    assert running is not None and running[0] == 0
