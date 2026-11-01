"""The assembled MCP HTTP app, in process, around a throwaway SDK server (RM-46).

The real SDK (``streamable_http_app``, its Host/Origin check, its session
manager's lifespan) runs here through ``starlette.testclient.TestClient`` used
as a context manager, so the lifespan starts. The tools are throwaway ones on a
throwaway server: nothing here touches the real registry or a database.
Every "refuses" test has a "keeps ordinary behaviour" twin.
"""
from __future__ import annotations

import json
import socket
import time
from collections.abc import Iterator
from dataclasses import replace
from pathlib import Path
from typing import Any

import anyio
import pytest
from starlette.testclient import TestClient

from brain import mcp_server
from brain.config import Config
from brain.errors import McpTransportError
from brain.mcp_auth import TokenInfo, fingerprint
from brain.mcp_compat import make_server
from brain.mcp_http import (
    HTTP_EXTRA_HINT,
    MAX_RESPONSE_BYTES,
    BindPlan,
    HttpServeOptions,
    banner_lines,
    build_http_app,
    distribution_version,
    readiness_probe,
    serve_http,
    uvicorn_config_kwargs,
    validate_options,
    with_statement_timeout,
)
from brain.mcp_http_conn import MAX_CONNECTIONS
from brain.mcp_http_guard import CONFIDENTIAL_REFUSAL
from brain.mcp_tool_classes import registered_tool_names
from tests.conftest import isolate_config_dotenv

pytestmark = pytest.mark.nodb

SECRET = "A" * 43
TOKEN = TokenInfo(
    path=Path("/synthetic/brain_dir/mcp_token"),
    secret=SECRET,
    mode_octal="0600",
    mode_ok=True,
    fingerprint=fingerprint(SECRET),
)
AUTH = {"Authorization": f"Bearer {SECRET}"}
MCP_HEADERS = {
    "Accept": "application/json, text/event-stream",
    "Content-Type": "application/json",
    "Host": "127.0.0.1:8787",
}
#: An address nothing listens on: the readiness probe must report it, not hang.
UNREACHABLE_DB = "postgresql://synthetic@127.0.0.1:1/none"


@pytest.fixture
def cfg(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Config:
    isolate_config_dotenv(monkeypatch, tmp_path)
    monkeypatch.setenv("BRAIN_EMBEDDER", "none")
    monkeypatch.setenv("DATABASE_URL", UNREACHABLE_DB)
    return Config.load()


def _server(*, slow: float = 0.0, big: bool = False) -> Any:
    """A throwaway server: one read tool, one write tool, one unclassified."""
    server = make_server("throwaway")

    async def brain_search(query: str, include_confidential: bool = False) -> dict[str, Any]:
        """A stand-in search."""
        if slow:
            await anyio.sleep(slow)
        payload = "x" * (MAX_RESPONSE_BYTES + 1) if big else query
        return {"echo": payload, "confidential": include_confidential}

    def brain_edit(id: str) -> dict[str, Any]:
        """A stand-in write tool."""
        return {"edited": id}

    def brain_brand_new() -> dict[str, Any]:
        """A tool nobody classified."""
        return {}

    server.tool()(brain_search)
    server.tool()(brain_edit)
    server.tool()(brain_brand_new)
    return server


async def _not_probed() -> dict[str, Any]:
    return {"status": "ok"}


def _client(opts: HttpServeOptions, server: Any = None) -> TestClient:
    plan = validate_options(opts)
    app = build_http_app(server or _server(), opts, plan, probe=_not_probed)
    return TestClient(app, base_url="http://127.0.0.1:8787")


@pytest.fixture
def client() -> Iterator[TestClient]:
    with _client(HttpServeOptions(token=TOKEN)) as test_client:
        yield test_client


def _rpc(method: str, params: dict[str, Any] | None = None, rid: int = 1) -> bytes:
    body: dict[str, Any] = {"jsonrpc": "2.0", "id": rid, "method": method}
    if params is not None:
        body["params"] = params
    return json.dumps(body).encode()


def _post(client: TestClient, body: bytes, **headers: str) -> Any:
    return client.post("/mcp", content=body, headers={**MCP_HEADERS, **AUTH, **headers})


def _call(client: TestClient, arguments: dict[str, Any], **headers: str) -> Any:
    return _post(
        client, _rpc("tools/call", {"name": "brain_search", "arguments": arguments}), **headers
    )


# ------------------------------------------------------------------ ordinary use


def test_tools_list_answers_over_http(client: TestClient) -> None:
    response = _post(client, _rpc("tools/list"))

    assert response.status_code == 200
    names = {tool["name"] for tool in response.json()["result"]["tools"]}
    assert names == {"brain_search", "brain_edit", "brain_brand_new"}


def test_a_tool_call_answers_over_http(client: TestClient) -> None:
    response = _call(client, {"query": "synthetic"})

    assert response.status_code == 200
    content = response.json()["result"]["structuredContent"]
    assert content == {"echo": "synthetic", "confidential": False}


# ----------------------------------------------------------------------- auth


def test_a_request_without_the_token_is_401(client: TestClient) -> None:
    response = client.post("/mcp", content=_rpc("tools/list"), headers=MCP_HEADERS)

    assert response.status_code == 401
    assert response.json() == {"error": "unauthorized"}


def test_no_auth_serves_without_a_header() -> None:
    with _client(HttpServeOptions(no_auth=True)) as test_client:
        response = test_client.post("/mcp", content=_rpc("tools/list"), headers=MCP_HEADERS)

    assert response.status_code == 200


# ------------------------------------------------------- DNS rebinding and CORS


@pytest.mark.parametrize("path", ["/mcp", "/healthz", "/readyz"])
def test_an_unlisted_host_is_refused_on_every_path(client: TestClient, path: str) -> None:
    response = client.get(path, headers={**AUTH, "Host": "evil.example.com"})

    assert response.status_code == 400


@pytest.mark.parametrize("host", ["127.0.0.1:8787", "localhost:8787", "[::1]:8787", "127.0.0.1"])
def test_a_loopback_host_is_accepted(client: TestClient, host: str) -> None:
    assert client.get("/healthz", headers={"Host": host}).status_code == 200


def test_an_allowed_host_is_accepted_bare_and_with_a_port() -> None:
    opts = HttpServeOptions(token=TOKEN, allowed_hosts=("brain.tailnet.example.com",))
    with _client(opts) as test_client:
        bare = _post(test_client, _rpc("tools/list"), Host="brain.tailnet.example.com")
        ported = _post(test_client, _rpc("tools/list"), Host="brain.tailnet.example.com:8787")

    assert bare.status_code == 200
    assert ported.status_code == 200


def test_a_browser_origin_is_refused(client: TestClient) -> None:
    response = _post(client, _rpc("tools/list"), Origin="https://attacker.example.com")

    assert response.status_code == 403


def test_no_response_grants_cross_origin_access(client: TestClient) -> None:
    preflight = client.options(
        "/mcp",
        headers={
            **AUTH,
            "Origin": "https://attacker.example.com",
            "Access-Control-Request-Method": "POST",
        },
    )
    ordinary = _post(client, _rpc("tools/list"))

    for response in (preflight, ordinary):
        assert not any(name.lower().startswith("access-control-") for name in response.headers)
    assert ordinary.status_code == 200


# ----------------------------------------------------------- confidential egress


def test_include_confidential_is_refused_by_default(client: TestClient) -> None:
    response = _call(client, {"query": "q", "include_confidential": True})

    assert response.status_code == 200
    assert response.json()["error"]["message"] == CONFIDENTIAL_REFUSAL


def test_include_confidential_reaches_the_tool_when_the_server_opts_in() -> None:
    opts = HttpServeOptions(token=TOKEN, include_confidential=True)
    with _client(opts) as test_client:
        response = _call(test_client, {"query": "q", "include_confidential": True})

    content = response.json()["result"]["structuredContent"]
    assert content == {"echo": "q", "confidential": True}


# ------------------------------------------------------------- size and time


def test_an_oversized_request_is_413(client: TestClient) -> None:
    response = _call(client, {"query": "q" * (2 * 1024 * 1024)})

    assert response.status_code == 413


def test_an_oversized_request_is_413_without_the_gate_too() -> None:
    # With --include-confidential the gate is absent; the SDK's own cap holds.
    opts = HttpServeOptions(token=TOKEN, include_confidential=True)
    with _client(opts) as test_client:
        response = _call(test_client, {"query": "q" * (2 * 1024 * 1024)})

    assert response.status_code == 413


def test_a_slow_tool_call_is_504() -> None:
    opts = HttpServeOptions(token=TOKEN, call_timeout_s=1.0)
    with _client(opts, _server(slow=5.0)) as test_client:
        response = _call(test_client, {"query": "q"})

    assert response.status_code == 504


def test_an_oversized_result_is_502() -> None:
    with _client(HttpServeOptions(token=TOKEN), _server(big=True)) as test_client:
        response = _call(test_client, {"query": "q"})

    assert response.status_code == 502


def test_no_refusal_names_a_filesystem_path(client: TestClient) -> None:
    refusals = [
        client.post("/mcp", content=b"{}", headers=MCP_HEADERS),
        client.get("/mcp", headers={"Host": "evil.example.com"}),
        _post(client, b"not json"),
        _post(client, _rpc("tools/list"), Origin="https://attacker.example.com"),
        _call(client, {"query": "q" * (2 * 1024 * 1024)}),
        _call(client, {"query": "q", "include_confidential": True}),
    ]
    for response in refusals:
        assert "/synthetic/brain_dir" not in response.text
        assert "/Users/" not in response.text
        assert "Traceback" not in response.text


# --------------------------------------------------------------------- readiness


def test_readyz_needs_the_token(client: TestClient) -> None:
    assert client.get("/readyz").status_code == 401


def test_readyz_reports_a_degraded_database_without_hanging(cfg: Config) -> None:
    probe = readiness_probe(cfg, read_only=True, tools=29)
    opts = HttpServeOptions(token=TOKEN)
    app = build_http_app(_server(), opts, validate_options(opts), probe=probe)
    with TestClient(app, base_url="http://127.0.0.1:8787") as test_client:
        response = test_client.get("/readyz", headers=AUTH)

    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "degraded"
    assert body["database"] == "unavailable"
    assert body["read_only"] is True
    assert body["tools"] == 29
    assert set(body) == {"status", "database", "embedder", "read_only", "tools", "version"}


def test_readyz_reports_read_write_with_allow_writes(cfg: Config) -> None:
    probe = readiness_probe(cfg, read_only=False, tools=51)

    body = anyio.run(probe)

    assert body["read_only"] is False
    assert body["tools"] == 51


# ------------------------------------------------------------- banner and uvicorn


def _plan(opts: HttpServeOptions) -> BindPlan:
    return validate_options(opts)


def test_banner_names_read_only_by_default() -> None:
    opts = HttpServeOptions(token=TOKEN)

    lines = banner_lines(opts, _plan(opts), 8787, served=29, total=51, embedder="none")

    text = "\n".join(lines)
    assert "READ-ONLY (29 of 51 tools; 22 write tools withheld)" in text
    assert "EXCLUDED (pass --include-confidential to serve)" in text
    assert f"fp {TOKEN.fingerprint}" in text
    assert SECRET not in text
    assert "http://127.0.0.1:8787/mcp" in text
    assert "Bind is loopback-only" in text


def test_banner_names_allow_writes() -> None:
    opts = HttpServeOptions(token=TOKEN, allow_writes=True, include_confidential=True)

    lines = banner_lines(opts, _plan(opts), 8787, served=51, total=51, embedder="none")

    text = "\n".join(lines)
    assert "READ-WRITE (51 of 51 tools; --allow-writes: write tools exposed)" in text
    assert "SERVED (--include-confidential)" in text


def test_banner_names_the_auth_source_and_a_network_bind() -> None:
    env_token = replace(TOKEN, path=None)
    remote = HttpServeOptions(host="192.0.2.10", allow_remote=True, token=env_token)
    local = HttpServeOptions(no_auth=True)

    remote_text = "\n".join(
        banner_lines(remote, _plan(remote), 8787, served=29, total=51, embedder="none")
    )
    local_text = "\n".join(
        banner_lines(local, _plan(local), 8787, served=29, total=51, embedder="none")
    )

    assert "bearer token from BRAIN_MCP_TOKEN" in remote_text
    assert "Bind is NOT loopback" in remote_text
    assert "NONE (--no-auth; loopback only)" in local_text


def test_uvicorn_is_hardened() -> None:
    opts = HttpServeOptions(token=TOKEN)

    kwargs = uvicorn_config_kwargs(_plan(opts), 8787, "INFO")
    protocol = kwargs.pop("http")

    assert protocol.__name__ == "IdleDeadlineProtocol"
    assert kwargs == {
        "host": "127.0.0.1",
        "port": 8787,
        "log_level": "info",
        "access_log": False,
        "proxy_headers": False,
        "server_header": False,
        "limit_concurrency": MAX_CONNECTIONS,
        "timeout_keep_alive": 5,
        "timeout_graceful_shutdown": 10,
        "lifespan": "on",
    }


# --------------------------------------------------------------------- serve_http


def _free_port() -> int:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return int(probe.getsockname()[1])


class _Recorder:
    def __init__(self) -> None:
        self.calls: list[tuple[Any, list[socket.socket], dict[str, Any]]] = []
        self.lines: list[str] = []

    def run(self, app: Any, sockets: list[socket.socket], kwargs: dict[str, Any]) -> None:
        self.calls.append((app, sockets, kwargs))

    def say(self, line: str) -> None:
        self.lines.append(line)


def test_serve_on_mcp_1x_names_the_http_extra(cfg: Config) -> None:
    with pytest.raises(McpTransportError) as excinfo:
        serve_http(HttpServeOptions(token=TOKEN), cfg, mcp_major=1)

    assert "needs mcp 2.x" in str(excinfo.value)
    assert HTTP_EXTRA_HINT in str(excinfo.value)
    assert HTTP_EXTRA_HINT == "pip install 'secondbrain-py[http]'"


def test_serve_withholds_write_tools_by_default(
    cfg: Config, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(mcp_server, "_state", None)
    server = _server()
    recorder = _Recorder()
    opts = HttpServeOptions(token=TOKEN, port=_free_port())

    serve_http(opts, cfg, server=server, run=recorder.run, announce=recorder.say)

    assert anyio.run(registered_tool_names, server) == ("brain_search",)
    (_, sockets, kwargs), = recorder.calls
    assert kwargs["port"] == opts.port
    assert all(sock.fileno() == -1 for sock in sockets)  # closed after serving
    expected = "READ-ONLY (1 of 3 tools; 2 write tools withheld)"
    assert any(expected in line for line in recorder.lines)
    assert mcp_server._get_state().db_conn is None  # a connection per call (D5)


def test_serve_with_allow_writes_keeps_every_tool(
    cfg: Config, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(mcp_server, "_state", None)
    server = _server()
    recorder = _Recorder()
    opts = HttpServeOptions(token=TOKEN, port=_free_port(), allow_writes=True)

    serve_http(opts, cfg, server=server, run=recorder.run, announce=recorder.say)

    assert len(anyio.run(registered_tool_names, server)) == 3
    assert any("READ-WRITE (3 of 3 tools" in line for line in recorder.lines)


def test_serve_refuses_a_port_in_use(cfg: Config, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(mcp_server, "_state", None)
    with socket.socket() as holder:
        holder.bind(("127.0.0.1", 0))
        holder.listen(1)
        port = holder.getsockname()[1]
        with pytest.raises(McpTransportError, match="already in use"):
            serve_http(HttpServeOptions(token=TOKEN, port=port), cfg, server=_server())


def test_serve_refuses_a_bad_bind_before_building_anything(
    cfg: Config, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(mcp_server, "_state", None)
    recorder = _Recorder()

    with pytest.raises(McpTransportError, match="--allow-remote"):
        serve_http(HttpServeOptions(host="0.0.0.0", token=TOKEN), cfg, run=recorder.run)

    assert recorder.calls == []
    assert mcp_server._state is None


def test_an_absent_distribution_reports_unknown() -> None:
    assert distribution_version("synthetic-not-installed-dist") == "unknown"
    assert distribution_version("mcp") != "unknown"


# ------------------------------------------------- sec-118 #1: path variants


@pytest.mark.parametrize("path", ["/mcp%0A", "/mcp%0a", "/mcp%0D", "/mcp%09", "//mcp", "/mcp/"])
def test_a_path_variant_never_reaches_the_sdk(client: TestClient, path: str) -> None:
    body = _rpc(
        "tools/call",
        {"name": "brain_search", "arguments": {"query": "q", "include_confidential": True}},
    )

    response = client.post(path, content=body, headers={**MCP_HEADERS, **AUTH})

    assert response.status_code == 404
    assert response.json() == {"error": "not found"}
    assert "structuredContent" not in response.text


def test_the_exact_paths_still_answer(client: TestClient) -> None:
    assert _call(client, {"query": "q"}).status_code == 200
    assert client.get("/healthz").json() == {"status": "ok"}
    assert client.get("/readyz", headers=AUTH).status_code == 200


# ------------------------------------------------------ sec-118 #5: error text


def _failing_server() -> Any:
    server = _server()

    def brain_show(id_prefix: str) -> dict[str, Any]:
        """A stand-in read tool that fails like an unreadable vault file."""
        raise PermissionError(13, "Permission denied", "/synthetic/vault/private-note.md")

    server.tool()(brain_show)
    return server


def test_serve_wraps_every_served_tool(cfg: Config, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(mcp_server, "_state", None)
    server = _failing_server()
    recorder = _Recorder()
    opts = HttpServeOptions(token=TOKEN, port=_free_port())

    serve_http(opts, cfg, server=server, run=recorder.run, announce=recorder.say)
    (app, _, _), = recorder.calls
    with TestClient(app, base_url="http://127.0.0.1:8787") as test_client:
        failed = _post(
            test_client,
            _rpc("tools/call", {"name": "brain_show", "arguments": {"id_prefix": "abcdef"}}),
        )
        ordinary = _call(test_client, {"query": "synthetic"})

    assert "/synthetic" not in failed.text
    assert "brain_show failed (PermissionError)" in failed.text
    assert ordinary.json()["result"]["structuredContent"]["echo"] == "synthetic"


# ------------------------------------------- sec-118 #4: orphaned work and statements


def test_a_call_behind_an_orphaned_worker_is_503_until_it_returns() -> None:
    server = _server()

    def brain_show(id_prefix: str) -> dict[str, Any]:
        """A stand-in read tool that blocks its worker thread."""
        time.sleep(2.0)
        return {"id": id_prefix}

    server.tool()(brain_show)
    opts = HttpServeOptions(token=TOKEN, call_timeout_s=1.0)
    app = build_http_app(server, opts, validate_options(opts), probe=_not_probed, max_calls=1)
    show = _rpc("tools/call", {"name": "brain_show", "arguments": {"id_prefix": "abcdef"}})
    with TestClient(app, base_url="http://127.0.0.1:8787") as test_client:
        timed_out = _post(test_client, show)
        behind_orphan = _call(test_client, {"query": "q"})
        time.sleep(2.0)
        after = _call(test_client, {"query": "q"})

    assert timed_out.status_code == 504
    assert behind_orphan.status_code == 503
    assert after.status_code == 200


@pytest.mark.parametrize(
    ("url", "expected"),
    [
        (
            "postgresql://u:p@db.example.com:5434/brain",
            "postgresql://u:p@db.example.com:5434/brain?options=-c%20statement_timeout%3D1500",
        ),
        (
            "postgresql://u@db.example.com/brain?sslmode=disable&options=-c%20search_path%3Dx",
            "postgresql://u@db.example.com/brain?sslmode=disable"
            "&options=-c%20search_path%3Dx%20-c%20statement_timeout%3D1500",
        ),
    ],
)
def test_statement_timeout_is_added_to_a_uri(url: str, expected: str) -> None:
    assert with_statement_timeout(url, 1500) == expected


def test_statement_timeout_is_added_to_a_key_value_string() -> None:
    from psycopg.conninfo import conninfo_to_dict

    plain = with_statement_timeout("host=db.example.com dbname=brain", 1500)
    merged = with_statement_timeout(
        "host=db.example.com dbname=brain options='-c search_path=x'", 1500
    )

    assert conninfo_to_dict(plain)["options"] == "-c statement_timeout=1500"
    assert conninfo_to_dict(merged)["options"] == "-c search_path=x -c statement_timeout=1500"
    assert conninfo_to_dict(merged)["dbname"] == "brain"


def test_serve_bounds_every_statement_by_the_call_deadline(
    cfg: Config, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(mcp_server, "_state", None)
    recorder = _Recorder()
    opts = HttpServeOptions(token=TOKEN, port=_free_port(), call_timeout_s=30.0)

    serve_http(opts, cfg, server=_server(), run=recorder.run, announce=recorder.say)

    assert "statement_timeout%3D30000" in mcp_server._get_state().cfg.database_url


@pytest.mark.parametrize(("include", "hidden"), [(False, True), (True, False)])
def test_serve_hides_confidential_metadata_unless_opted_in(
    cfg: Config, monkeypatch: pytest.MonkeyPatch, include: bool, hidden: bool
) -> None:
    monkeypatch.setattr(mcp_server, "_state", None)
    recorder = _Recorder()
    opts = HttpServeOptions(token=TOKEN, port=_free_port(), include_confidential=include)

    serve_http(opts, cfg, server=_server(), run=recorder.run, announce=recorder.say)

    assert mcp_server._get_state().hide_confidential is hidden
