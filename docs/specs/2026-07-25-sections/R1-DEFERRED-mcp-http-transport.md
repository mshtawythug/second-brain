# R1-DEFERRED — HTTP transport for the MCP server (evaluated, deferred)

> Design section of `docs/specs/2026-07-25-agent-memory-safety-ui-design.md`.
> Global constraints (PII, production safety, quality gates, style) are inherited from
> section 4 of that document and are not restated here.

## Optional HTTP transport for the MCP server — reach your brain from another machine

### 1. Goal

Today the second brain is reachable only from the machine that owns the Postgres bind-mount, because `brain-mcp` speaks stdio and is spawned as a subprocess by whichever client is on that same box. This section adds an **opt-in, loopback-by-default, bearer-authenticated, read-only-by-default streamable-HTTP transport** so a second device on a private network (laptop, phone, another agent host over Tailscale) can call `brain_search` / `brain_show` / `brain_graphrag_*` against the one real brain. The stdio path — the one Claude Desktop uses and the one 21 existing tests exercise — is byte-for-byte unchanged. The design deliberately ships *less* than the reference project: no OAuth, no SSE transport, no dependency patching, no multi-tenant tokens.

### 2. Current state

**Transport is hard-coded to stdio.** `src/brain/mcp_server.py:3390` (`main()`) ends with:

```python
logger.info("brain-mcp starting (stdio transport)")
mcp_app.run(transport="stdio")
```

`main()` takes no arguments and never inspects `sys.argv`, so `brain-mcp --http` today silently starts a stdio server — a trap, not an error. The FastMCP instance is a module global created bare at `src/brain/mcp_server.py:308`:

```python
mcp_app: FastMCP = FastMCP(name="brain")
```

— i.e. no `host`, no `port`, no `transport_security`, no `auth`. 37 tools are registered on it via `@mcp_app.tool()` (verified: `grep -c "@mcp_app.tool()" src/brain/mcp_server.py` → 37).

Server state is built inline inside `main()` (`src/brain/mcp_server.py:3366-3392`): `Config.load()`, `make_embedder(cfg)`, `make_enricher(cfg)`, a lazily-imported `make_graph_syncer(cfg)`, `PersistentConnection(cfg.database_url)`, assembled into the `_State` dataclass (`src/brain/mcp_server.py:329`… declared at `src/brain/mcp_server.py:129`-ish, fields at 155-167) and assigned to the module global `_state` (`src/brain/mcp_server.py:168`), then `_warmup_embed(_state.embedder)`.

**Logging** is configured by `_configure_logging()` (`src/brain/mcp_server.py:3345`), which reads `BRAIN_MCP_LOG_LEVEL` **directly from `os.environ`, not through `Config`**. That is the precedent this section follows for its own knobs — no `Config` field is added.

**`server.json:19-21`** declares `"transport": {"type": "stdio"}` for the uvx-launched PyPI package. **`docs/guides/claude-desktop-setup.md:47-63`** documents the local-subprocess model (`"command": "/Users/you/.../.venv/bin/brain-mcp"`).

**What is missing:** any HTTP listener, any authentication, any tool-visibility control, any host-header policy, any health endpoint, and any CLI to start such a thing.

#### SDK capability audit (read from the installed package, not from memory)

Installed in `.venv`: `mcp 1.28.0`, `starlette 1.3.1`, `uvicorn 0.49.0`, `httpx 0.28.1`.

| Capability | Verdict | Evidence |
|---|---|---|
| Streamable-HTTP transport | **Present** | `.venv/.../mcp/server/fastmcp/server.py:281` — `transport: Literal["stdio", "sse", "streamable-http"]`; `:777` `run_streamable_http_async`; `:950` `streamable_http_app() -> Starlette` |
| Explicit DNS-rebinding config (no patching needed) | **Present** | `mcp/server/fastmcp/server.py:175` constructor kwarg `transport_security: TransportSecuritySettings | None = None`, threaded to the session manager at `:962`. Definition in `mcp/server/transport_security.py:12`. |
| The reference project's 421 problem | **Confirmed, and avoidable** | `mcp/server/fastmcp/server.py:178-183` auto-enables protection **only when `transport_security is None` *and* `host in ("127.0.0.1","localhost","::1")`**. Passing an explicit `TransportSecuritySettings` bypasses the auto-branch entirely. `transport_security.py:118` returns `Response("Invalid Host header", status_code=421)` — exactly the failure they patched around. **We never patch: we always pass the object.** |
| Removing tools at runtime (for read-only) | **Present and public** | `mcp/server/fastmcp/server.py:435` `def remove_tool(self, name: str) -> None`, delegating to `ToolManager.remove_tool` (`tools/tool_manager.py:75`). |
| Custom HTTP routes (health) | **Present and public** | `mcp/server/fastmcp/server.py:705` `def custom_route(...)`; docstring at `:720` warns such routes **bypass FastMCP authorization** — our middleware sits outside, so we handle this explicitly. |
| `starlette` / `uvicorn` availability | **Hard deps of `mcp`, not extras** | `importlib.metadata.requires("mcp")` → `starlette>=0.48.0; python_version >= '3.14'`, `uvicorn>=0.31.1; sys_platform != 'emscripten'`. No new package is installed. |
| `httpx.ASGITransport` for in-process tests | **Present** | `httpx 0.28.1`, `hasattr(httpx, "ASGITransport") is True` — middleware tests need no live socket. |
| `mcp.client.streamable_http.streamablehttp_client` | **Present** | imports cleanly — gives us a real end-to-end HTTP protocol test. |

**Honest version caveat:** `pyproject.toml:42` pins `"mcp>=1.0,<2.0"`. `transport_security=` and `FastMCP.remove_tool` are verified present in **1.28.0**; they are *not* guaranteed by a `>=1.0` floor and I did not verify which minor introduced them. This section therefore raises the floor to `mcp>=1.28,<2.0` (the version actually tested) rather than guessing.

**Cut from scope, deliberately:** SSE transport (legacy, second attack surface, zero benefit), and OAuth 2.1 / dynamic client registration. The SDK's `AuthSettings` + `TokenVerifier` path (`server.py:174`, `:1004`) exists, but wiring an authorization server into a single-user local product is a large, fragile surface for no gain. **Consequence to state plainly in the docs:** a static bearer token works with any client that lets you set an `Authorization` header, but it will **not** satisfy the claude.ai "custom connector" flow, which expects OAuth. If that flow is required later it is a separate spec, not a bolt-on.

### 3. User-visible surface

#### Decision: a new `brain mcp` sub-app, **not** a flag on `brain-mcp`

`brain-mcp` is spawned by MCP clients with **no arguments** and its stdout is the JSON-RPC channel. Starting an HTTP daemon is an *operator* action — it wants `--help`, argument validation, Rich error text, and a refusal path, all of which already exist on the `brain` Typer app. Overloading the stdio entry point risks a mis-parsed flag corrupting the JSON-RPC stream. So:

- `brain-mcp` stays stdio-only and argument-free.
- **One behavior change to `brain-mcp`:** if `sys.argv[1:]` is non-empty, print to **stderr** and exit `2` instead of silently starting stdio:
  ```
  brain-mcp takes no arguments (stdio transport only).
  HTTP transport lives on the human CLI: brain mcp serve --help
  ```
  This is strictly better than today's silent misbehavior. `python -m brain.mcp_server` (used by `tests/test_mcp_server_protocol.py:37-39`) passes no extra argv and is unaffected.

#### `brain mcp serve`

| Flag | Type | Default | Help text |
|---|---|---|---|
| `--host` | `str` | `"127.0.0.1"` | Interface to bind. Non-loopback requires --allow-remote AND a token. |
| `--port` | `int` | `8787` | TCP port to listen on. |
| `--path` | `str` | `"/mcp"` | URL path for the streamable-HTTP endpoint. |
| `--allow-remote / --no-allow-remote` | `bool` | `False` | Acknowledge that a non-loopback bind exposes the brain to the network. |
| `--allow-writes / --no-allow-writes` | `bool` | `False` | Expose the 16 write tools too. Off by default: HTTP serves the 21 read tools only. |
| `--allowed-host` | `list[str]` | `[]` | Extra Host header value to accept (repeatable), e.g. brain.tailnet.ts.net. Loopback is always allowed. |
| `--token-file` | `Path \| None` | `None` | Path to the bearer-token file. Default: $BRAIN_MCP_TOKEN_PATH or <brain_home>/mcp_token. |
| `--no-auth` | `bool` | `False` | Serve without a bearer token. Only permitted on a loopback bind. |
| `--max-body-bytes` | `int` | `1048576` | Reject POSTs whose Content-Length exceeds this (0 disables the check). |
| `--log-level` | `str` | `"INFO"` | DEBUG/INFO/WARNING/ERROR/CRITICAL. Overrides BRAIN_MCP_LOG_LEVEL. |

Startup banner (stderr; the process then blocks):

```
brain mcp serve — streamable HTTP
  endpoint    http://127.0.0.1:8787/mcp
  health      http://127.0.0.1:8787/healthz
  mode        READ-ONLY (21 of 37 tools; 16 write tools withheld)
  auth        bearer token from /Users/you/.brain/mcp_token (fp 4f9a2c71)
  allow-host  127.0.0.1:*, localhost:*, [::1]:*
  embedder    arctic (1024d)
Bind is loopback-only. To reach this from another device, put BOTH machines on
a private network (Tailscale) — never port-forward this to the internet.
Ctrl-C to stop.
```

The refusal, when someone binds wide open (exit code **2**, matching the repo's `2 = Typer BadParameter` convention recorded in CLAUDE.md):

```
Error: refusing to bind 0.0.0.0 — a non-loopback bind requires BOTH an explicit
--allow-remote flag AND a bearer token.

  1. brain mcp token --rotate          # writes ~/.brain/mcp_token (mode 0600)
  2. brain mcp serve --host 0.0.0.0 --allow-remote

Never expose this port to the public internet. Put both machines on a private
network (e.g. Tailscale) and bind the tailnet address instead of 0.0.0.0.
See docs/guides/mcp-http-transport.md.
```

`--no-auth` with a non-loopback `--host` produces the same class of refusal with the first line `Error: refusing to bind 0.0.0.0 with --no-auth — a token is mandatory off loopback.`

#### `brain mcp token`

| Flag | Type | Default | Help text |
|---|---|---|---|
| `--rotate` | `bool` | `False` | Generate a new 256-bit token, replacing any existing one. |
| `--show` | `bool` | `False` | Print the token itself to stdout (for pasting into a client config). |
| `--path` | `Path \| None` | `None` | Token file to operate on. Default: $BRAIN_MCP_TOKEN_PATH or <brain_home>/mcp_token. |
| `--json` | `bool` | `False` | Emit machine-readable JSON instead of the table. |

Human output (no `--show`; the token is never printed unless asked):

```
token file   /Users/you/.brain/mcp_token
mode         0600  OK
fingerprint  4f9a2c71
Use --show to print the token. Treat it like a password.
```

JSON shape (`--json`, `--show` omitted → `token` is `null`):

```json
{
  "path": "/Users/you/.brain/mcp_token",
  "exists": true,
  "mode": "0600",
  "mode_ok": true,
  "fingerprint": "4f9a2c71",
  "token": null
}
```

#### HTTP endpoints

| Path | Method | Auth | Body |
|---|---|---|---|
| `/mcp` (configurable) | POST/GET/DELETE | Bearer | MCP streamable-HTTP |
| `/healthz` | GET | **none** | `{"status": "ok"}` — static liveness only |
| `/readyz` | GET | Bearer (when auth is on) | readiness, below |

`/readyz` body — deliberately free of corpus statistics:

```json
{
  "status": "ok",
  "database": "ok",
  "embedder": "arctic",
  "read_only": true,
  "tools": 21,
  "version": "0.2.1"
}
```

`"status"` is `"degraded"` (HTTP 200) when `database` is `"unavailable"`; `/healthz` still returns `ok` so a supervisor distinguishes "process alive" from "brain usable".

401 shape (identical for missing, malformed, and wrong tokens — no oracle):

```
HTTP/1.1 401 Unauthorized
WWW-Authenticate: Bearer
Content-Type: application/json

{"error": "unauthorized"}
```

413 shape: `{"error": "request too large"}`. Host-header rejection is the SDK's own `421 Invalid Host header` (`mcp/server/transport_security.py:118`) — we do not reimplement it.

#### Backward-compatibility risk and mitigation

| Risk | Mitigation |
|---|---|
| Stdio JSON-RPC framing broken by new logging/imports | Nothing in the HTTP path is imported unless `brain mcp serve` runs (`mcp_http` is imported lazily inside the command body, mirroring the existing lazy `from .graph_rag.sync import make_graph_syncer` at `src/brain/mcp_server.py:3383`). `main()`'s stdio branch is untouched apart from the argv guard. |
| Tool list changes for existing stdio clients | Read-only filtering happens **only** inside `build_http_app()`. `main()` never calls `apply_read_only`. A regression test asserts stdio still advertises all 37. |
| `server.json` consumers | **Unchanged.** It keeps `"transport": {"type": "stdio"}` — it describes the uvx-spawned package, which is still stdio. HTTP is an operator-run daemon, not a registry transport. |
| `brain-mcp` invoked with stray args | Previously silent stdio start; now a clear exit 2. Documented as an intentional fix. |
| `Config` schema | **No new fields.** All knobs read `os.environ` directly, matching `BRAIN_MCP_LOG_LEVEL` (`src/brain/mcp_server.py:3352`). |

### 4. Module layout

| Path | New/changed | Purpose | Est. lines |
|---|---|---|---|
| `src/brain/mcp_auth.py` | **new** | Token file resolution, 0600 permission check, generation/rotation, constant-time comparison, fingerprinting, the pure-ASGI `BearerAuthMiddleware`. Imports **nothing** from `mcp_server`, so `brain mcp token` stays fast. | ~150 |
| `src/brain/mcp_http.py` | **new** | `HttpServeOptions` value object, bind/auth validation, read-only tool allow-list + `apply_read_only`, `build_transport_security`, `RequestLogMiddleware`, `/healthz` + `/readyz` handlers, `build_http_app`, `serve_http`. | ~280 |
| `src/brain/cli_mcp.py` | **new** | Typer sub-app: `brain mcp serve`, `brain mcp token`. Thin — maps flags to `HttpServeOptions`, maps `McpTransportError` to `typer.BadParameter`/`typer.Exit`, Rich output. Mirrors `src/brain/cli_connect.py:1-8`'s "thin orchestration" docstring contract. | ~170 |
| `src/brain/mcp_server.py` | changed | Extract `build_state(cfg) -> _State` and `install_state(state) -> None` out of `main()` (lines 3366-3392) so `serve_http` reuses them verbatim instead of duplicating startup. Add the argv guard. Net ≈ +25/−18. | 3405 → ~3412 |
| `src/brain/errors.py` | changed | `+ class McpTransportError(BrainError)` and `+ class McpTokenError(McpTransportError)`. | +8 |
| `src/brain/cli.py` | changed | Two lines: `from .cli_mcp import mcp_cli_app` and `app.add_typer(mcp_cli_app, name="mcp")`, alongside the existing `add_typer` block at `src/brain/cli.py:294-328`. | +3 |
| `pyproject.toml` | changed | Raise `mcp>=1.0,<2.0` → `mcp>=1.28,<2.0`; add explicit `starlette>=0.48` and `uvicorn>=0.31` (we import them directly; do not rely on a transitive). | +2/−1 |
| `docs/guides/mcp-http-transport.md` | **new** | Exposure guidance, Tailscale walkthrough, token rotation, read-only rationale. | ~180 |
| `docs/configuration.md`, `docs/guides/claude-desktop-setup.md`, `README.md` | changed | Env-knob table rows + a pointer to the new guide. | +30 |

`src/brain/cli.py` is already 9760 lines; no command body goes into it. Every new file is comfortably under 400.

**No migration.** This section is transport-only: no schema change, no new table, no new column. `024_agent_attribution.sql` and `025_document_sensitivity.sql` belong to other sections and are not touched.

### 5. Design detail

#### Value objects

```python
@dataclass(frozen=True)
class TokenInfo:
    """Resolved bearer token plus the provenance needed for doctor/CLI output."""
    path: Path | None          # None when sourced from BRAIN_MCP_TOKEN
    secret: str                # never logged, never rendered by __repr__ callers
    mode_octal: str            # "0600" when path is not None, "" otherwise
    mode_ok: bool
    fingerprint: str           # sha256(secret.encode()).hexdigest()[:8]


@dataclass(frozen=True)
class HttpServeOptions:
    """Validated options for one `brain mcp serve` invocation."""
    host: str = "127.0.0.1"
    port: int = 8787
    path: str = "/mcp"
    allow_remote: bool = False
    allow_writes: bool = False
    allowed_hosts: tuple[str, ...] = ()
    token: TokenInfo | None = None
    max_body_bytes: int = 1_048_576
    log_level: str = "INFO"
```

Both frozen; `validate_options` returns a **new** `HttpServeOptions` (via `dataclasses.replace`) rather than mutating — consistent with the `replace` import already at `src/brain/mcp_server.py:10`.

#### Function signatures

```python
# mcp_auth.py
def default_token_path(brain_home: Path) -> Path: ...
def generate_token() -> str: ...                     # secrets.token_urlsafe(32)
def write_token(path: Path, token: str) -> None: ...  # O_CREAT|O_EXCL|O_WRONLY, 0o600
def read_token(path: Path) -> TokenInfo: ...          # raises McpTokenError
def resolve_token(brain_home: Path, explicit: Path | None) -> TokenInfo | None: ...
def token_matches(expected: TokenInfo, presented: str) -> bool: ...

class BearerAuthMiddleware:
    def __init__(self, app: ASGIApp, *, token: TokenInfo,
                 exempt_paths: frozenset[str]) -> None: ...
    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None: ...

# mcp_http.py
READ_ONLY_TOOLS: frozenset[str]
def write_tool_names(registered: Iterable[str]) -> tuple[str, ...]: ...
def apply_read_only(app: FastMCP) -> tuple[str, ...]: ...
def build_transport_security(opts: HttpServeOptions) -> TransportSecuritySettings: ...
def validate_options(opts: HttpServeOptions) -> HttpServeOptions: ...
def build_http_app(opts: HttpServeOptions, cfg: Config) -> ASGIApp: ...
def serve_http(opts: HttpServeOptions) -> None: ...
```

#### Startup data flow (`serve_http`)

1. `validate_options(opts)` — the **only** place bind policy lives (below). Raises `McpTransportError`; `cli_mcp` converts to `typer.BadParameter` (exit 2).
2. `cfg = Config.load()` → `state = mcp_server.build_state(cfg)` → `mcp_server.install_state(state)`. Identical code path to stdio, so tool behavior cannot drift.
3. If `not opts.allow_writes`: `withheld = apply_read_only(mcp_server.mcp_app)`.
4. `mcp_app.settings.host/port/streamable_http_path` are set from `opts`, and `mcp_app.settings.transport_security = build_transport_security(opts)`. (Assigning the pydantic `Settings` fields post-construction is supported — `streamable_http_app()` reads `self.settings.transport_security` at build time, `server.py:962`. This avoids reconstructing `FastMCP`, which would discard all 37 registered tools.)
5. Register `/healthz` and `/readyz` via `mcp_app.custom_route(...)` — at serve time only, so stdio never gains routes.
6. `inner = mcp_app.streamable_http_app()`; wrap outside-in: `RequestLogMiddleware(BearerAuthMiddleware(inner, ...))`. Auth is *inside* logging so a 401 is still logged.
7. `uvicorn.Config(app, host=..., port=..., log_level=..., access_log=False)` → `uvicorn.Server(...).run()`. We build our own config (rather than `run_streamable_http_async`, `server.py:777`) precisely because that helper allows neither middleware nor `access_log=False`.

#### Bind policy (the one function that matters)

```python
_LOOPBACK_HOSTS = frozenset({"127.0.0.1", "localhost", "::1", "[::1]"})

def validate_options(opts: HttpServeOptions) -> HttpServeOptions:
    is_loopback = opts.host in _LOOPBACK_HOSTS
    if not is_loopback:
        if not opts.allow_remote:
            raise McpTransportError(_REFUSE_NO_ALLOW_REMOTE.format(host=opts.host))
        if opts.token is None:
            raise McpTransportError(_REFUSE_NO_TOKEN.format(host=opts.host))
    if opts.token is not None and not opts.token.mode_ok:
        raise McpTransportError(
            f"token file {opts.token.path} is mode {opts.token.mode_octal}; "
            f"expected 0600. Fix with: chmod 600 {opts.token.path}"
        )
    if not 1 <= opts.port <= 65535:
        raise McpTransportError(f"--port must be 1-65535 (got {opts.port})")
    return opts
```

Both conditions are required off loopback — `--allow-remote` alone is not enough, and a token alone is not enough. The refusal strings are module constants so tests assert on the exact text.

#### Token: file, not env (recommendation)

**Recommend the file.** `BRAIN_MCP_TOKEN` is honored as an override (for containers), but the documented path is `<brain_home>/mcp_token`, because:

- process environment is visible to other processes and is copied into every child (`brain mcp serve` shells out to nothing, but `brain_ask` invokes Ollama and the enricher);
- env vars land in shell history, `launchctl print`, and crash dumps;
- a file can be permission-*verified* (`mode_ok`) and atomically rotated; an env var cannot.

Generation: `secrets.token_urlsafe(32)` (256 bits). Write via `os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)` so the file is never briefly world-readable; rotation writes a temp sibling then `Path.replace()`. Permission check: `(path.stat().st_mode & 0o077) == 0` — reject group/other bits, tolerate `0o400`.

Comparison is `hmac.compare_digest(expected.secret, presented)` — constant time, and the header is parsed before comparison so a length difference in the *scheme* prefix cannot be timed.

**The token is never logged.** `BearerAuthMiddleware` reads `scope["headers"]`, never copies the value anywhere, and logs only `"auth rejected: %s"` with one of the fixed reasons `missing-header` / `bad-scheme` / `mismatch`. `TokenInfo.secret` is only ever passed to `compare_digest` and (on explicit `--show`) to stdout. The startup banner prints the 8-hex **fingerprint**, which is a truncated SHA-256 of a 256-bit random value — not reversible, and useful for "which token is this box using". A lint-enforced rule: `grep -n "token" src/brain/mcp_http.py src/brain/mcp_auth.py` must show no `logger.*` call whose format args include `.secret`.

#### DNS-rebinding / Host-header protection

We **always** pass an explicit `TransportSecuritySettings`, so the SDK's auto-enable branch (`server.py:178-183`) never fires and we never patch it out:

```python
def build_transport_security(opts: HttpServeOptions) -> TransportSecuritySettings:
    hosts: list[str] = ["127.0.0.1:*", "localhost:*", "[::1]:*",
                        "127.0.0.1", "localhost", "[::1]"]
    for extra in opts.allowed_hosts:
        hosts.append(extra)          # exact, e.g. "brain.tailnet.ts.net:8787"
        if ":" not in extra:
            hosts.append(f"{extra}:*")   # any-port form
    return TransportSecuritySettings(
        enable_dns_rebinding_protection=True,
        allowed_hosts=hosts,
        allowed_origins=[],           # see note
    )
```

Two details drawn from reading `mcp/server/transport_security.py`:

- `_validate_host` (`:44`) supports exact match plus a `base:*` suffix wildcard **only**. A Host header carrying no port (`brain.tailnet.ts.net`, when served behind a reverse proxy on 443) will *not* match `brain.tailnet.ts.net:*`. Hence we add both forms. This is exactly the trap the reference project fell into.
- `_validate_origin` (`:67`) returns `True` when `Origin` is **absent** — which is the case for every non-browser MCP client. We therefore leave `allowed_origins` **empty**: any browser-originated cross-site request is rejected with 403, while normal clients are unaffected. That is the correct default for a private brain, and it is a supported configuration, not a workaround.

If a user hits a 421, the fix is `--allowed-host <their-hostname>` — documented in the guide with the literal error text so it is searchable.

#### Read-only enforcement

Classification is an **explicit allow-list that fails closed**: any tool name not in `READ_ONLY_TOOLS` is a write tool and is removed.

```python
READ_ONLY_TOOLS: frozenset[str] = frozenset({
    "brain_search", "brain_gaps", "brain_show", "brain_list", "brain_resurface",
    "brain_status", "brain_backlinks", "brain_links", "brain_orphans",
    "brain_review_findings_list", "brain_brief", "brain_ask",
    "brain_link_proposal", "brain_graphrag_search", "brain_graphrag_themes",
    "brain_graphrag_entity", "brain_timeline", "brain_graphrag_communities",
    "brain_graphrag_entities", "brain_graphrag_stats", "brain_connect_list",
})
```

21 read / 16 write of the 37 registered. Notable calls, each justified against the actual docstring:

- `brain_link_proposal` is READ — its docstring literally says *"Propose adding a `[[link]]` … Writes nothing."* (`src/brain/mcp_server.py:1803`).
- `brain_review_weekly` is WRITE — *"When `emit` is true (default) the dated page is written to `<vault>/reviews/<week>.md`"* (`src/brain/mcp_server.py:1398`).
- `brain_search` / `brain_show` / `brain_ask` are READ even though they insert **telemetry** rows (search-failure logging at `src/brain/mcp_server.py:439`; interaction logging at `:613`). "Read" here means *does not create, modify, or delete user-visible knowledge* — documents, tags, vault files, graph entities. Telemetry is explicitly in-scope for read mode; withholding `brain_search` would defeat the feature.
- `brain_rate` is WRITE despite being harmless, because the allow-list is an allow-list. Promoting it is a one-line change if the user asks.
- All four `brain_graphrag_*_build` / `*_refresh` and `brain_graphrag_aliases_apply` are WRITE — they are the admin/destructive rebuilds the brief calls out.

Enforcement uses the SDK's **public** `FastMCP.remove_tool` (`server.py:435`):

```python
def apply_read_only(app: FastMCP) -> tuple[str, ...]:
    """Remove every non-allow-listed tool. Returns the withheld names, sorted."""
    registered = [tool.name for tool in app.list_tools_sync_names()]  # see note
    withheld = tuple(sorted(n for n in registered if n not in READ_ONLY_TOOLS))
    for name in withheld:
        app.remove_tool(name)
    return withheld
```

(`FastMCP.list_tools` is `async`; the implementation enumerates names by awaiting it once inside `serve_http`'s async setup, or reads them from the sync `ToolManager` via the same public `add_tool`/`remove_tool` surface. Either way no private attribute is written.)

Removal is the right primitive because the tool then **does not exist**: `ToolManager.call_tool` raises `Unknown tool` for it, so there is no separate call-path to bypass — enforcement is not a check that can be forgotten. It also means `tools/list` over HTTP is honest, which matters: an LLM client that cannot see `brain_edit` will not try to use it.

**Recommendation: read-only is the DEFAULT for HTTP.** Rationale: the threat model for a network listener is "a token leaked, or a compromised device on the tailnet". Read exposure is a privacy loss; write exposure is *corpus destruction* — `brain_graphrag_build` and `brain_edit` are irreversible against a knowledge base with no undo. Defaulting to read-only makes the dangerous case an explicit `--allow-writes` typed by a human.

#### Request logging

```python
class RequestLogMiddleware:
    """Log method/path/status/duration. Never logs bodies, headers, or tokens."""
```

One line per request at INFO:

```
INFO brain.mcp.http 100.64.0.7 POST /mcp 200 41ms
WARN brain.mcp.http 100.64.0.9 POST /mcp 401 0ms auth rejected: mismatch
```

The middleware inspects only `scope["method"]`, `scope["path"]`, `scope["client"]`, and the outbound `http.response.start` status. It **never wraps `receive`**, so request bodies are structurally unreachable from it — the guarantee is enforced by construction, not by discipline. Response bodies are likewise untouched (`send` is passed through except for reading `status`). Query strings are not logged (the streamable-HTTP endpoint has none, but this is belt-and-braces if `--path` ever gains one). `uvicorn` is configured with `access_log=False` so uvicorn cannot emit a second, less careful line.

Document bodies can only surface via tool results, which never reach the logger. This preserves the existing project rule (CLAUDE.md security standards) that full document content is never logged at INFO.

#### Error handling

| Condition | Exception | Surface |
|---|---|---|
| Bad bind / missing token / bad file mode / bad port | `McpTransportError(BrainError)` | `typer.BadParameter` → exit 2 |
| Token file unreadable / empty / rotation collision | `McpTokenError(McpTransportError)` | exit 2 with `chmod`/`--rotate` remedy |
| `Config.load()` failure | existing `ConfigError` | exit 1, unchanged text |
| Port already bound | `OSError` from uvicorn — caught, re-raised as `McpTransportError(f"port {port} is already in use…")` | exit 2 |
| DB down at startup | **not fatal.** `build_state` opens a `PersistentConnection` lazily (`src/brain/mcp_server.py:3388`); the server starts, `/healthz` is `ok`, `/readyz` reports `"database": "unavailable"`, and individual tools return the existing `INTERNAL_ERROR` via `_wrap_db_error` (`src/brain/mcp_server.py:183`) | HTTP 200 with degraded body |
| Ollama down at startup | already non-fatal — `_warmup_embed` logs a warning; unchanged | banner shows `embedder arctic (warmup failed)` |

No bare `except:` anywhere; the middleware catches nothing (exceptions propagate to Starlette's handler, which already returns 500 without a body leak in non-debug mode — `debug=self.settings.debug`, default `False`).

### 6. Edge cases and failure modes

1. **`--host 0.0.0.0` with no token, no `--allow-remote`.** Refuse at `validate_options`, exit 2, print the two-step remedy. The socket is never opened. Covered by a dedicated test asserting the literal message.
2. **`--host 0.0.0.0 --allow-remote` but no token file exists.** Refuse with the token-specific message. `--allow-remote` alone must never be sufficient — this is the single most likely user mistake.
3. **Token file exists but is `0644`.** Refuse before binding: `token file … is mode 0644; expected 0600. Fix with: chmod 600 …`. A world-readable token on a shared machine is equivalent to no token.
4. **Client sends `Host: evil.example.com` after a DNS rebind.** SDK middleware returns `421 Invalid Host header` (`transport_security.py:118`) before any tool dispatch. The user's remedy is `--allowed-host`, not patching. A test asserts 421 for an unlisted host **and** 200 for one added via `--allowed-host`.
5. **Client sends `Host: brain.tailnet.ts.net` with no port** (reverse proxy on 443). Handled because `build_transport_security` adds both the exact and the `:*` forms for every `--allowed-host` value. Without this the user sees a 421 that looks like the flag is broken.
6. **Read-only mode, client calls `brain_edit` anyway.** The tool is not registered; `ToolManager.call_tool` raises `Unknown tool: brain_edit`, surfaced as a normal MCP tool error. `tools/list` also omits it, so a well-behaved client never attempts it.
7. **A new tool is added in a later release and nobody updates the allow-list.** It is treated as a **write** tool and withheld over HTTP. Fail-closed. A unit test asserts `READ_ONLY_TOOLS <= set(registered_names)` so a *renamed* read tool fails CI loudly rather than silently vanishing from HTTP.
8. **Two `brain mcp serve` processes on the same port.** The second gets `OSError: [Errno 48]` from uvicorn, translated to `port 8787 is already in use — stop the other server or pass --port`. Exit 2, no partial state.
9. **Postgres restarts while the HTTP server is up.** `PersistentConnection` reconnect semantics are unchanged from stdio; a request during the outage returns the existing `INTERNAL_ERROR` mapping and `/readyz` flips to `degraded`. The listener stays up — a transport that dies on a DB blip is worse than one that reports it.
10. **A 500 MB POST.** Rejected with 413 when `Content-Length` exceeds `--max-body-bytes`. **Known gap, stated honestly:** a chunked request with no `Content-Length` bypasses this check; we accept that (it is the same risk class as any long-lived connection, and the endpoint is token-gated on a private network). Documented in the guide rather than papered over.
11. **`brain-mcp --http` typed by a user who read the wrong doc.** Exit 2 with a pointer to `brain mcp serve --help`, instead of today's silent stdio start on a terminal that never speaks JSON-RPC.
12. **Ctrl-C during an in-flight tool call.** uvicorn's graceful shutdown drains connections; `PersistentConnection` is closed in a `finally` in `serve_http`. Second Ctrl-C forces exit.

### 7. Security and safety

| Risk | Guard |
|---|---|
| Brain exposed to the LAN/internet by accident | Loopback default; non-loopback needs `--allow-remote` **and** a token; the refusal text names Tailscale and forbids port-forwarding. |
| Token leaked via logs | Middleware never reads the header value into a log call; only an 8-hex SHA-256 fingerprint is ever emitted; `uvicorn` access log disabled. |
| Token leaked via filesystem | `O_EXCL | 0o600` on create; `mode_ok` verified before every bind; refusal (not a warning) on a loose mode. |
| Token brute-forced / timing-attacked | 256-bit `secrets.token_urlsafe(32)`; `hmac.compare_digest`; identical 401 body for missing/malformed/wrong. |
| DNS-rebinding from the user's own browser | Explicit `TransportSecuritySettings` with a curated `allowed_hosts` and an **empty** `allowed_origins` (any browser `Origin` → 403). No dependency patching. |
| Corpus destroyed by a remote/compromised client | Read-only default; the 16 write tools — including `brain_edit`, `brain_graphrag_build`, `brain_graphrag_aliases_apply` — are *removed from the registry*, not merely gated. |
| Document bodies written to disk logs | `RequestLogMiddleware` structurally cannot see bodies (it never wraps `receive`); pre-existing rule that content is never logged at INFO is untouched. |
| Health endpoint used for reconnaissance | `/healthz` is a static `{"status":"ok"}` with no counts, paths, or version; the informative `/readyz` is token-gated and still omits corpus statistics. |
| Resource exhaustion via giant payloads | `Content-Length` cap at 1 MiB (configurable); documented chunked-encoding gap. |
| Production DB harmed by this feature | It performs **no** DDL, no migration, no destructive statement. All SQL is the existing tool code, already parameterized. Tests run against port 5434 / `second_brain_test` only. |
| Supply chain | Zero new packages. `starlette` and `uvicorn` are already hard requirements of `mcp` (verified via `importlib.metadata.requires`); we merely declare them explicitly. |

### 8. Test plan

All HTTP tests use `httpx.ASGITransport` (verified present in httpx 0.28.1) against the app object — no real socket, no flakiness — except the one end-to-end test that deliberately binds an ephemeral loopback port.

**Red-first test that proves the gap** — `tests/test_mcp_http_protocol.py::test_streamable_http_serves_tools_list`. Spawns `python -m brain.cli mcp serve --host 127.0.0.1 --port 0 --no-auth` as a subprocess, connects with `mcp.client.streamable_http.streamablehttp_client`, runs `initialize` + `tools/list`, and asserts `brain_search` is advertised. **Today this fails at collection with `ModuleNotFoundError: brain.mcp_http`** — the module does not exist and there is no HTTP listener at all. Write it first, watch it fail, then build.

| File | Test | Asserts |
|---|---|---|
| `tests/test_mcp_http_bind_guard.py` | `test_refuses_non_loopback_without_allow_remote` | `validate_options(host="0.0.0.0")` raises `McpTransportError`; message contains `refusing to bind 0.0.0.0`, `--allow-remote`, and `brain mcp token --rotate`. |
| | `test_refuses_non_loopback_with_allow_remote_but_no_token` | `--allow-remote` alone is insufficient; message names the token. |
| | `test_accepts_non_loopback_with_allow_remote_and_token` | Returns options unchanged (no exception). |
| | `test_defaults_are_loopback_and_read_only` | `HttpServeOptions()` → `host == "127.0.0.1"`, `allow_writes is False`. |
| | `test_refuses_token_file_with_loose_permissions` | `tmp_path` token at `0o644` → `McpTransportError` naming `chmod 600`. |
| | `test_cli_serve_exits_2_on_bad_bind` | `CliRunner` on `brain mcp serve --host 0.0.0.0` → `exit_code == 2`. |
| `tests/test_mcp_http_auth.py` | `test_missing_authorization_is_401` | `{"error": "unauthorized"}`, `WWW-Authenticate: Bearer`. |
| | `test_wrong_token_is_401_with_identical_body` | Byte-identical to the missing-header response (no oracle). |
| | `test_malformed_scheme_is_401` | `Authorization: Token abc` → 401. |
| | `test_correct_token_passes_through` | Sentinel downstream ASGI app is reached exactly once. |
| | `test_healthz_is_exempt_from_auth` | `GET /healthz` → 200 with no header. |
| | `test_readyz_requires_auth_when_token_set` | `GET /readyz` without header → 401. |
| | `test_token_never_appears_in_log_records` | `caplog` over a 401 and a 200: no record's `getMessage()` contains the secret; the `mismatch` reason is present. |
| | `test_token_comparison_uses_compare_digest` | `mocker.patch("hmac.compare_digest", wraps=hmac.compare_digest)` — stdlib boundary, allowed; asserts it is called. |
| | `test_oversized_body_is_413` | `Content-Length` above cap → 413 `{"error": "request too large"}`. |
| `tests/test_mcp_http_readonly.py` | `test_read_only_allowlist_is_subset_of_registered_tools` | Every name in `READ_ONLY_TOOLS` exists on the real `mcp_app` — catches renames. |
| | `test_all_37_tools_are_classified` | `len(READ_ONLY_TOOLS) + len(write_tool_names(registered)) == 37`. |
| | `test_unknown_tool_classified_as_write` | `write_tool_names(["brain_brand_new"])` includes it (fail-closed). |
| | `test_apply_read_only_removes_write_tools` | On a **throwaway** `FastMCP` seeded with two fake tools — no mutation of the module global. Asserts `brain_edit`-like name gone, read name retained, and the returned tuple is sorted. |
| | `test_known_write_tools_are_withheld` | `brain_edit`, `brain_ingest_stdin`, `brain_graphrag_build`, `brain_graphrag_aliases_apply`, `brain_note_new` all classified write. |
| | `test_link_proposal_is_read` / `test_review_weekly_is_write` | Pins the two judgement calls to their docstrings. |
| `tests/test_mcp_http_host_header.py` | `test_unlisted_host_is_421` | Real `build_http_app` + `ASGITransport`, `Host: evil.example.com` → 421 `Invalid Host header`. |
| | `test_loopback_host_is_allowed` | `Host: 127.0.0.1:8787` → not 421. |
| | `test_allowed_host_flag_accepts_bare_and_ported_forms` | `--allowed-host brain.tailnet.example.com` → both `…example.com` and `…example.com:8787` accepted. |
| | `test_browser_origin_is_403` | `Origin: https://attacker.example.com` → 403. |
| | `test_absent_origin_is_allowed` | Non-browser client (no `Origin`) → not 403. |
| `tests/test_mcp_http_health.py` | `test_healthz_static_body` | Exactly `{"status": "ok"}` — no counts, no paths, no version. |
| | `test_readyz_reports_degraded_without_db` | `database == "unavailable"`, HTTP 200, `status == "degraded"`. |
| | `test_readyz_reports_read_only_and_tool_count` | `read_only is True`, `tools == 21`. |
| `tests/test_mcp_token_cli.py` | `test_rotate_creates_file_mode_0600` | `stat().st_mode & 0o777 == 0o600`. |
| | `test_rotate_replaces_existing_atomically` | Old secret gone, new secret readable, no `.tmp` sibling left behind. |
| | `test_token_command_hides_secret_by_default` | Secret not in stdout; fingerprint is. |
| | `test_token_show_prints_secret` | With `--show`, secret is in stdout. |
| | `test_token_json_shape` | Keys exactly `{path, exists, mode, mode_ok, fingerprint, token}`; `token is None` without `--show`. |
| `tests/test_mcp_http_protocol.py` | `test_streamable_http_serves_tools_list` (**the red-first test**) | Real subprocess, real HTTP, `initialize` + `tools/list`, `brain_search` present. |
| | `test_http_read_only_hides_write_tools_end_to_end` | Same round-trip: `brain_edit` absent, `brain_search` present. |
| | `test_http_with_allow_writes_exposes_all_37` | `--allow-writes --no-auth` on loopback → 37 tools. |
| `tests/test_mcp_server_protocol.py` (**regression, existing file**) | `test_stdio_still_advertises_all_tools` | New assertion alongside the existing `EXPECTED_TOOLS` check (`tests/test_mcp_server_protocol.py:20-29`): stdio `tools/list` returns **37** tools, proving read-only filtering never leaks into stdio. |
| | `test_bare_brain_mcp_main_unchanged` | `python -m brain.mcp_server` with no argv still completes an `initialize` handshake. |
| | `test_brain_mcp_rejects_arguments` | `python -m brain.mcp_server --http` → exit 2, stderr names `brain mcp serve`. |

Coverage: `mcp_auth.py` and the classification/validation logic in `mcp_http.py` are pure logic → **95%** target. `cli_mcp.py` is CLI orchestration → **85%**. No monkey-patching of production modules; the only `mocker.patch` targets are stdlib (`hmac.compare_digest`) and the uvicorn boundary in `serve_http`. Fakes (`_State` built directly, throwaway `FastMCP`) follow the pattern the existing MCP tests already use (`src/brain/mcp_server.py:133-137` documents it). All fixture hostnames are `*.example.com`; the tailnet examples in docs use `brain.tailnet.example.com`. No PII.

### 9. Open questions — with the decision taken

1. **Flag on `brain-mcp`, env var, or a new `brain mcp serve`?** → **`brain mcp serve`.** stdio clients spawn the binary with zero args and own its stdout; adding a parser there risks corrupting JSON-RPC. Starting a daemon is an operator action and belongs on the human CLI with `--help` and Rich errors. `brain-mcp` additionally gains a hard "no arguments" guard so the wrong invocation fails loudly.
2. **Token in env or in a file?** → **File** (`<brain_home>/mcp_token`, mode-checked 0600), with `BRAIN_MCP_TOKEN` honored as an override for container users. Env vars leak into child processes, `ps`, shell history, and crash dumps, and cannot be permission-verified.
3. **Should read-only be the HTTP default?** → **Yes.** Read exposure is a privacy loss; write exposure is corpus destruction with no undo (`brain_graphrag_build`, `brain_edit`). `--allow-writes` makes the dangerous case an explicit human decision.
4. **Is `brain_search` "read" even though it INSERTs telemetry?** → **Yes.** "Read" means *does not touch user-visible knowledge*. Telemetry rows (`search_queries`, `interactions`) are in-scope for read mode; excluding `brain_search` would make the feature pointless.
5. **`brain_rate` — read or write?** → **Write**, because the list is an allow-list and rating mutates `interactions` in a way that steers future ranking. Trivially promotable if the user wants remote thumbs-up.
6. **Also ship the SSE transport?** → **No.** Legacy, a second attack surface, zero incremental capability over streamable HTTP.
7. **Ship OAuth 2.1 / DCR so claude.ai's custom-connector flow works?** → **No, and say so plainly in the docs.** The SDK supports it (`AuthSettings`, `TokenVerifier`), but standing up an authorization server for a single-user local product is a large fragile surface. Static bearer covers every client that permits a custom `Authorization` header. If the claude.ai connector flow becomes a requirement, it is its own spec.
8. **Patch the SDK's DNS-rebinding behavior like the reference project did?** → **Absolutely not, and it is unnecessary.** `FastMCP(transport_security=…)` (`server.py:175`) is the supported configuration path; the auto-enable branch at `server.py:178` only fires when the kwarg is `None`. We always pass it. Any user-facing 421 is fixed with `--allowed-host`, not with a vendored patch.
9. **Default port?** → **8787.** Away from 8000 (uvicorn/FastMCP default, commonly occupied) and the project's 55432/55433/5434 Postgres block.
10. **Raise the `mcp` floor?** → **Yes: `mcp>=1.28,<2.0`.** `transport_security=` and `remove_tool` are verified on 1.28.0; I did not verify which earlier minor introduced them, and shipping a floor I have not tested would be guessing.
11. **Add fields to `Config`?** → **No.** `BRAIN_MCP_TOKEN` / `BRAIN_MCP_TOKEN_PATH` are read from `os.environ` at the point of use, matching how `BRAIN_MCP_LOG_LEVEL` is already handled (`src/brain/mcp_server.py:3352`). Zero risk to the frozen `Config` dataclass and its ~60 existing fields.
12. **Change `server.json`?** → **No.** It describes the uvx-spawned PyPI package, which remains stdio. Advertising an HTTP transport there would imply the registry can launch a network listener, which it cannot and should not.
13. **Should `brain doctor` report HTTP status?** → **Not in this release.** `doctor` (`src/brain/cli.py:1502`) probes things the CLI needs; a daemon the user may deliberately not be running would produce a permanent yellow line. `brain mcp token` already reports the only persistent state (the token file). Revisit if users start filing "is my HTTP server up?" questions.
