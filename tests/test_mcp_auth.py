"""The MCP HTTP bearer token: its file, its settings, and the ASGI gate (RM-46).

Every path is under ``tmp_path``; nothing reads or writes the owner's real
``$BRAIN_HOME``. Tokens are minted here with :func:`generate_token` or are
obvious synthetic strings.
"""
from __future__ import annotations

import hmac
import logging
import os
import stat
from pathlib import Path
from typing import Any

import pytest
from pytest_mock import MockerFixture
from starlette.testclient import TestClient

from brain.config import ConfigError
from brain.errors import McpTokenError
from brain.mcp_auth import (
    MAX_TOKEN_FILE_BYTES,
    BearerAuthMiddleware,
    TokenInfo,
    default_token_path,
    fingerprint,
    generate_token,
    read_token,
    resolve_token,
    write_token,
)
from brain.settings.mcp_http import McpHttpSettings, load_mcp_http_settings

pytestmark = pytest.mark.nodb

#: A synthetic token in the token alphabet, the length ``token_urlsafe(32)`` has.
SYNTHETIC = "A" * 43


# --------------------------------------------------------------------------- files


def test_generate_token_is_256_bits_urlsafe() -> None:
    first, second = generate_token(), generate_token()
    assert first != second
    assert len(first) == 43
    assert set(first) <= set("ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-_")


def test_default_token_path_is_under_brain_home(tmp_path: Path) -> None:
    assert default_token_path(tmp_path) == tmp_path / "mcp_token"


def test_write_then_read_round_trips_at_mode_0600(tmp_path: Path) -> None:
    path = tmp_path / "home" / "mcp_token"
    token = generate_token()

    write_token(path, token)
    info = read_token(path)

    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert info.secret == token
    assert info.path == path
    assert info.mode_octal == "0600"
    assert info.mode_ok is True
    assert info.fingerprint == fingerprint(token)
    assert len(info.fingerprint) == 8


def test_rotation_replaces_the_token_and_leaves_no_temp_sibling(tmp_path: Path) -> None:
    path = tmp_path / "mcp_token"
    write_token(path, generate_token())
    replacement = generate_token()

    write_token(path, replacement)

    assert read_token(path).secret == replacement
    assert sorted(p.name for p in tmp_path.iterdir()) == ["mcp_token"]


def test_write_refuses_a_value_outside_the_token_alphabet(tmp_path: Path) -> None:
    with pytest.raises(McpTokenError):
        write_token(tmp_path / "mcp_token", "short")
    assert not (tmp_path / "mcp_token").exists()


def test_write_failure_leaves_no_temp_sibling(tmp_path: Path) -> None:
    target = tmp_path / "mcp_token"
    target.mkdir()  # os.replace onto a directory fails

    with pytest.raises(McpTokenError):
        write_token(target, generate_token())

    assert sorted(p.name for p in tmp_path.iterdir()) == ["mcp_token"]


def test_a_missing_token_file_names_the_fix(tmp_path: Path) -> None:
    with pytest.raises(McpTokenError, match="brain mcp token --rotate"):
        read_token(tmp_path / "absent")


def test_a_loose_mode_is_reported_not_trusted(tmp_path: Path) -> None:
    path = tmp_path / "mcp_token"
    write_token(path, generate_token())
    path.chmod(0o644)

    info = read_token(path)

    assert info.mode_ok is False
    assert info.mode_octal == "0644"


def test_a_read_only_owner_mode_is_accepted(tmp_path: Path) -> None:
    path = tmp_path / "mcp_token"
    write_token(path, generate_token())
    path.chmod(0o400)

    assert read_token(path).mode_ok is True


def test_a_symlink_is_refused(tmp_path: Path) -> None:
    real = tmp_path / "real_token"
    write_token(real, generate_token())
    link = tmp_path / "mcp_token"
    link.symlink_to(real)

    with pytest.raises(McpTokenError, match="symbolic link"):
        read_token(link)


def test_a_fifo_is_refused_without_blocking(tmp_path: Path) -> None:
    fifo = tmp_path / "mcp_token"
    os.mkfifo(fifo, 0o600)

    with pytest.raises(McpTokenError, match="regular file"):
        read_token(fifo)


def test_a_file_owned_by_someone_else_is_refused(tmp_path: Path) -> None:
    path = tmp_path / "mcp_token"
    write_token(path, generate_token())

    with pytest.raises(McpTokenError, match="owned"):
        read_token(path, expected_uid=os.getuid() + 1)


def test_an_oversized_token_file_is_refused(tmp_path: Path) -> None:
    path = tmp_path / "mcp_token"
    path.write_bytes(b"A" * (MAX_TOKEN_FILE_BYTES + 1))
    path.chmod(0o600)

    with pytest.raises(McpTokenError, match="too large"):
        read_token(path)


@pytest.mark.parametrize(
    "content",
    [b"", b"\n", b"A" * 42, b"A" * 43 + b"\x00", b"A" * 43 + b"\r\n", "é".encode() * 43,
     b"A" * 43 + b"\n\n", b" " + b"A" * 43],
)
def test_malformed_token_content_is_refused(tmp_path: Path, content: bytes) -> None:
    path = tmp_path / "mcp_token"
    path.write_bytes(content)
    path.chmod(0o600)

    with pytest.raises(McpTokenError):
        read_token(path)


def test_one_trailing_newline_is_tolerated(tmp_path: Path) -> None:
    path = tmp_path / "mcp_token"
    path.write_bytes(SYNTHETIC.encode() + b"\n")
    path.chmod(0o600)

    assert read_token(path).secret == SYNTHETIC


def test_token_info_repr_hides_the_secret() -> None:
    info = TokenInfo(
        path=None, secret=SYNTHETIC, mode_octal="", mode_ok=True, fingerprint="0" * 8
    )
    assert SYNTHETIC not in repr(info)


# ------------------------------------------------------------------------ settings


def test_settings_default_to_nothing() -> None:
    assert load_mcp_http_settings({}) == McpHttpSettings(token=None, token_path=None)


def test_settings_read_a_valid_env_token_and_path(tmp_path: Path) -> None:
    settings = load_mcp_http_settings(
        {"BRAIN_MCP_TOKEN": SYNTHETIC, "BRAIN_MCP_TOKEN_PATH": str(tmp_path / "t")}
    )
    assert settings.token == SYNTHETIC
    assert settings.token_path == tmp_path / "t"
    assert SYNTHETIC not in repr(settings)


def test_a_weak_env_token_is_refused_without_echoing_it() -> None:
    weak = "hunter2-synthetic"
    with pytest.raises(ConfigError, match="BRAIN_MCP_TOKEN") as excinfo:
        load_mcp_http_settings({"BRAIN_MCP_TOKEN": weak})
    assert weak not in str(excinfo.value)


# ------------------------------------------------------------------------- resolve


def test_resolve_prefers_an_explicit_file(tmp_path: Path) -> None:
    explicit = tmp_path / "explicit"
    write_token(explicit, SYNTHETIC)
    settings = McpHttpSettings(token="B" * 43, token_path=None)

    info = resolve_token(settings, explicit=explicit, brain_home=tmp_path)

    assert info is not None and info.secret == SYNTHETIC and info.path == explicit


def test_resolve_takes_the_env_token_over_any_file(tmp_path: Path) -> None:
    write_token(default_token_path(tmp_path), SYNTHETIC)
    settings = McpHttpSettings(token="B" * 43, token_path=None)

    info = resolve_token(settings, explicit=None, brain_home=tmp_path)

    assert info is not None and info.secret == "B" * 43
    assert info.path is None and info.mode_ok is True


def test_resolve_reads_the_env_path_then_the_default(tmp_path: Path) -> None:
    env_path = tmp_path / "elsewhere"
    write_token(env_path, SYNTHETIC)
    write_token(default_token_path(tmp_path), "C" * 43)

    from_env = resolve_token(
        McpHttpSettings(token=None, token_path=env_path), explicit=None, brain_home=tmp_path
    )
    from_default = resolve_token(McpHttpSettings(), explicit=None, brain_home=tmp_path)

    assert from_env is not None and from_env.secret == SYNTHETIC
    assert from_default is not None and from_default.secret == "C" * 43


def test_resolve_returns_none_when_no_token_exists(tmp_path: Path) -> None:
    assert resolve_token(McpHttpSettings(), explicit=None, brain_home=tmp_path) is None


def test_resolve_refuses_a_missing_explicit_file(tmp_path: Path) -> None:
    with pytest.raises(McpTokenError):
        resolve_token(McpHttpSettings(), explicit=tmp_path / "absent", brain_home=tmp_path)


# --------------------------------------------------------------------- middleware


class _Sentinel:
    """The downstream app: counts calls and answers 200."""

    def __init__(self) -> None:
        self.calls = 0

    async def __call__(self, scope: dict[str, Any], receive: Any, send: Any) -> None:
        self.calls += 1
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": b"ok"})


def _info(secret: str = SYNTHETIC) -> TokenInfo:
    return TokenInfo(
        path=None, secret=secret, mode_octal="", mode_ok=True, fingerprint=fingerprint(secret)
    )


def _client(sentinel: _Sentinel) -> TestClient:
    app = BearerAuthMiddleware(sentinel, token=_info(), exempt_paths=frozenset({"/healthz"}))
    return TestClient(app, raise_server_exceptions=True)


def test_missing_authorization_is_401() -> None:
    sentinel = _Sentinel()
    response = _client(sentinel).post("/mcp")

    assert response.status_code == 401
    assert response.json() == {"error": "unauthorized"}
    assert response.headers["www-authenticate"] == "Bearer"
    assert sentinel.calls == 0


@pytest.mark.parametrize(
    "header",
    [
        {"Authorization": "Bearer " + "B" * 43},
        {"Authorization": "Token " + SYNTHETIC},
        {"Authorization": SYNTHETIC},
        {"Authorization": "Bearer"},
        {"Authorization": "Bearer " + SYNTHETIC + "x"},
    ],
)
def test_every_refusal_is_byte_identical(header: dict[str, str]) -> None:
    client = _client(_Sentinel())
    baseline = client.post("/mcp")

    response = client.post("/mcp", headers=header)

    assert response.status_code == 401
    assert response.content == baseline.content
    assert dict(response.headers) == dict(baseline.headers)


def test_a_non_ascii_header_is_401_not_a_crash() -> None:
    # ``hmac.compare_digest`` raises TypeError on non-ASCII ``str`` (the
    # `brain ui` review-round-5 bug); the gate compares bytes.
    sentinel = _Sentinel()
    app = BearerAuthMiddleware(sentinel, token=_info(), exempt_paths=frozenset())
    client = TestClient(app)

    response = client.post(
        "/mcp", headers=[(b"authorization", "Bearer é".encode())]  # type: ignore[list-item]
    )

    assert response.status_code == 401
    assert sentinel.calls == 0


def test_two_authorization_headers_are_refused() -> None:
    sentinel = _Sentinel()
    client = _client(sentinel)
    good = ("authorization", f"Bearer {SYNTHETIC}")

    response = client.post("/mcp", headers=[good, good])  # type: ignore[list-item]

    assert response.status_code == 401
    assert sentinel.calls == 0


def test_the_correct_token_passes_through_once() -> None:
    sentinel = _Sentinel()

    response = _client(sentinel).post("/mcp", headers={"Authorization": f"Bearer {SYNTHETIC}"})

    assert response.status_code == 200
    assert sentinel.calls == 1


def test_the_scheme_is_case_insensitive() -> None:
    sentinel = _Sentinel()

    response = _client(sentinel).post("/mcp", headers={"Authorization": f"bearer {SYNTHETIC}"})

    assert response.status_code == 200


def test_an_exempt_path_needs_no_header() -> None:
    sentinel = _Sentinel()

    response = _client(sentinel).get("/healthz")

    assert response.status_code == 200
    assert sentinel.calls == 1


def test_the_comparison_goes_through_compare_digest(mocker: MockerFixture) -> None:
    spy = mocker.patch("brain.mcp_auth.hmac.compare_digest", wraps=hmac.compare_digest)

    _client(_Sentinel()).post("/mcp", headers={"Authorization": f"Bearer {SYNTHETIC}"})

    assert spy.call_count == 1
    presented, expected = spy.call_args.args
    assert isinstance(presented, bytes) and isinstance(expected, bytes)


def test_the_token_never_reaches_a_log_record(caplog: pytest.LogCaptureFixture) -> None:
    client = _client(_Sentinel())
    with caplog.at_level(logging.DEBUG):
        client.post("/mcp", headers={"Authorization": "Bearer " + "B" * 43})
        client.post("/mcp", headers={"Authorization": f"Bearer {SYNTHETIC}"})

    messages = [record.getMessage() for record in caplog.records]
    assert any("mismatch" in message for message in messages)
    assert not any(SYNTHETIC in message or "B" * 43 in message for message in messages)


def test_a_websocket_scope_is_closed_without_reaching_the_app() -> None:
    sentinel = _Sentinel()
    app = BearerAuthMiddleware(sentinel, token=_info(), exempt_paths=frozenset())
    sent: list[dict[str, Any]] = []

    async def receive() -> dict[str, Any]:
        return {"type": "websocket.connect"}

    async def send(message: dict[str, Any]) -> None:
        sent.append(message)

    import anyio

    anyio.run(app, {"type": "websocket", "path": "/mcp", "headers": []}, receive, send)

    assert sent == [{"type": "websocket.close", "code": 1008}]
    assert sentinel.calls == 0


def test_lifespan_passes_straight_through() -> None:
    sentinel_scopes: list[str] = []

    async def inner(scope: dict[str, Any], receive: Any, send: Any) -> None:
        sentinel_scopes.append(scope["type"])

    app = BearerAuthMiddleware(inner, token=_info(), exempt_paths=frozenset())

    async def noop(*_: Any) -> Any:
        return {}

    import anyio

    anyio.run(app, {"type": "lifespan"}, noop, noop)

    assert sentinel_scopes == ["lifespan"]


def test_an_unreadable_token_file_is_refused(tmp_path: Path) -> None:
    path = tmp_path / "mcp_token"
    write_token(path, generate_token())
    path.chmod(0o000)
    try:
        with pytest.raises(McpTokenError, match="cannot open"):
            read_token(path)
    finally:
        path.chmod(0o600)
