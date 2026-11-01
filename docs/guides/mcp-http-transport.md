# Reaching your brain from another machine (`brain mcp serve`)

`brain-mcp` speaks stdio. An MCP client starts it as a subprocess on the machine that
holds the database, so only that machine can use it. `brain mcp serve` serves the same
tools over **streamable HTTP**, so a second device on a private network can call them:
another laptop, or an agent host on your tailnet.

It is off until you run it. Its defaults are:

| Default | Meaning | How to change it |
|---|---|---|
| Loopback | Binds `127.0.0.1:8787`. | `--host`, which also needs `--allow-remote` and a token |
| Token | Every request needs `Authorization: Bearer <token>`. | `--no-auth`, loopback only |
| Read-only | Serves the 28 read tools. The 23 write tools are removed from the server. | `--allow-writes` |
| Confidential excluded | A call that asks for `include_confidential` is refused. | `--include-confidential` |

It needs the mcp 2.x SDK. If `brain mcp serve` says `needs mcp 2.x`, run
`pip install 'secondbrain-py[http]'`. stdio keeps working on either major.

## Quick start (two machines on a tailnet)

On the machine that holds the brain:

```bash
brain mcp token --rotate          # writes <brain_home>/mcp_token, mode 0600
brain mcp token --show            # copy the token into the other machine's client config
brain mcp serve --host 192.0.2.10 --allow-remote --allowed-host brain.tailnet.example.com
```

Bind the machine's **tailnet address**, not `0.0.0.0`. On the other machine, point an MCP
client that supports streamable HTTP at `http://brain.tailnet.example.com:8787/mcp`, with
the header `Authorization: Bearer <token>`.

**Never port-forward this to the internet.** The token is the only thing between the
port and your corpus. A private network (Tailscale, WireGuard) adds encryption and
device identity. The server has no TLS of its own; if you need TLS, put a reverse proxy
in front of it on the private network.

## What the startup banner tells you

```
brain mcp serve — streamable HTTP
  endpoint      http://127.0.0.1:8787/mcp
  health        http://127.0.0.1:8787/healthz
  mode          READ-ONLY (28 of 51 tools; 23 write tools withheld)
  confidential  EXCLUDED (pass --include-confidential to serve)
  auth          bearer token from /Users/you/.brain/mcp_token (fp 4f9a2c71)
  allow-host    127.0.0.1, ::1, [::1], localhost
  embedder      arctic
```

`fp` is the first 8 hex digits of the token's SHA-256. It tells you which token a machine
uses without showing the token. The banner never prints the token itself, and neither does
any log line.

## Why read-only and confidential-excluded are the defaults

A network listener is attacked through a leaked token or a compromised device. Exposing
the read tools to an attacker loses privacy. Exposing the write tools could destroy the
corpus, and `brain_rm`, `brain_edit` and `brain_graphrag_build` have no undo. So the write
tools are **removed from the server**, not merely refused: `tools/list` does not show them,
and calling one answers "Unknown tool". The list of read tools is an allowlist
(`src/brain/mcp_tool_classes.py`). A tool added later is withheld until someone
classifies it.

"Read" means the tool creates, changes or deletes nothing you see as knowledge. Some read
tools still log telemetry (`search_queries`, citation `interactions`). `brain_gaps` is a
write tool here, because `push=true` writes to the elicitation queue.

Confidential documents follow the same rule as `brain ui` on a network bind: they stay on
the machine that owns them unless you start the server with `--include-confidential`.
Several tools say "re-call with include_confidential=true" in their errors, so a model on
the remote device would follow that advice. Without the flag the server refuses such a
call.

Without the flag, confidential METADATA stays home too:

- A confidential document answers every lookup by id prefix (`brain_show`,
  `brain_backlinks`, `brain_links`, `brain_related`, `brain_link_proposal`'s source)
  exactly as an unknown prefix does: `document not found: <prefix>`. So its title, tags
  and source path never leave the machine.
- `brain_link_proposal` never resolves a confidential destination. This one applies on
  stdio too, unless the caller passes `include_confidential`.
- Graph tools name an entity (a person, project, organisation or topic) only if at least
  one non-confidential document mentions it. The same goes for text written from names:
  a synthesized theme summary that drew on a withheld name is dropped, and a theme with no
  public entity is not returned. `brain_ask`'s graph modes write their answer from the
  same filtered labels.
- Global graph search never returns a community any of whose members a confidential
  document mentions, so its stored summary (written from member names and titles) stays
  home. This holds over stdio too, matching `brain_graphrag_communities`.

Three small things remain visible, and are accepted:

- `brain_status` and `brain_graphrag_stats` counts cover every document and entity,
  confidential ones included. These are numbers only: `brain_graphrag_stats`' top-10 entity
  NAMES are filtered like the entity listing.
- An id prefix shared by a normal and a confidential document answers "ambiguous".
- Guessing an exact entity name for `brain_search`'s `entity` filter confirms that some
  document mentions it, but returns no content. (`brain_timeline` matches names by
  substring, so over HTTP it returns only entities a non-confidential document mentions.)
- Some counts include confidential documents: a graph explanation's `seed_count` and a
  theme's `scope_doc_count`.

People follow the same rule. A `person` argument (`brain_search`, `brain_recall`,
`brain_timeline`, graph themes) resolves only people at least one non-confidential document
names. Anyone else "matched no one", and an ambiguous match lists public people only. A
person filter's `person_keys` (their email addresses) are dropped from every result.

"Read-only" means no tool that changes your knowledge is served. It does not mean the
server never writes to the database:

- Read tools still log telemetry (`search_queries`, citation `interactions`).
- `brain_graphrag_search`, `brain_graphrag_themes`, `brain_graphrag_entity` and
  `brain_ask`'s graph modes run the graph backend's bootstrap on each call. That is idempotent DDL: it creates the AGE graph, its labels and
  their indexes only if they are missing, exactly as over stdio. On a built graph it
  changes nothing.

## Requests the server refuses

| Response | When | What to do |
|---|---|---|
| `401 {"error": "unauthorized"}` | No token, a malformed header, or a wrong token. All three get the same response. | Check the `Authorization: Bearer` header. Compare `fp` with `brain mcp token`. |
| `400 Invalid host header` | The request's `Host` is not one this server answers to. This is the DNS-rebinding defence. | Pass `--allowed-host <the name the client uses>`. It covers the bare name and any port. |
| `403 Invalid Origin header` | The request came from a browser page. | None: MCP clients send no `Origin`. |
| `413 {"error": "request too large"}` | The body is over `--max-body-bytes` (default 1 MiB, streamed bodies included). | Raise the cap (16 MiB at most) or send less. |
| `415` | The body is compressed (`Content-Encoding`). | Send it uncompressed. |
| JSON-RPC error naming `--include-confidential` | The call asked for `include_confidential`. | Restart with `--include-confidential` if you mean it. |
| `502 {"error": "result too large"}` | A tool's result was over 8 MiB. | Narrow the call (smaller `limit`, `brain_show` caps). |
| `504 {"error": "timed out"}` | The call took longer than `--call-timeout` (default 300 s). | Raise it, or ask for less. |
| `405 {"error": "method not allowed"}` | Any method but POST on the endpoint. The server is stateless and never streams. | Use POST. |
| `404 {"error": "not found"}` | Any path but the endpoint, `/healthz` and `/readyz`, compared exactly (`/mcp/`, `//mcp` and `/mcp%0A` included). | Use the endpoint URL from the banner. |
| `503 {"error": "busy"}` | 16 calls are already in flight, or every worker thread is still finishing a call that already timed out. | Retry shortly. |
| `503` (uvicorn) | 256 open connections. | Retry. Idle connections are closed after 10 s. |
| JSON-RPC error `tool <name> failed (<Class>)` | A tool failed unexpectedly. The class is logged on the server; the message never leaves it. | Read the server log. |

A connection with no request in flight is closed after 10 seconds, whether or not bytes
arrive, and every refusal closes its connection. That stops a few silent clients from
holding the server. It does **not** stop a peer that keeps opening new connections from
filling the 256-connection cap; on a private network that peer is one of your own devices.
There is no per-address cap, because on the default loopback bind every client is
127.0.0.1.

A call that times out gets its 504 at the deadline, but the tool still finishes, its answer
is discarded, and it keeps its slot until then. Its database statements are cancelled at the
same deadline (`statement_timeout`). New calls get 503 while all 16 slots are held, rather
than waiting behind it. The endpoint answers POST only: a `GET` (an event stream) or any
other method gets 405.

`GET /healthz` answers `{"status": "ok"}` without a token, for a process supervisor.
`GET /readyz` needs the token. It reports whether the database answers, plus the embedder,
the mode and the tool count, and nothing about the corpus.

## Refusals at startup (exit 2)

- `refusing to bind 0.0.0.0 — a non-loopback bind requires BOTH an explicit --allow-remote
  flag AND a bearer token`. `--allow-remote` alone is not enough, and neither is a token
  alone. A wildcard bind also accepts only loopback `Host` values unless you add
  `--allowed-host`.
- `no bearer token`. Run `brain mcp token --rotate`, or pass `--no-auth` on loopback.
- `--no-auth cannot be combined with --allow-remote or --allowed-host`. Both widen who can
  reach the server, so a token is required.
- `token file … is mode 0644; expected 0600`. Run `chmod 600` on the file. The server also
  refuses a token file that is a symbolic link, is not a regular file, belongs to another
  user, or does not hold a token.
- `port 8787 is already in use`. Another server is running; stop it or pass `--port`.

`--host` is judged by the address it resolves to, once, exactly as `brain ui` does it.
`localhost` counts as loopback only when it resolves to loopback. Any other name that
resolves to `127.0.0.1` is treated as a network bind, because whoever controls that name's
DNS could re-point it.

## The token

- `brain mcp token --rotate` writes a new 256-bit token. It is written at mode 0600 and
  replaces the old file atomically. **A running `brain mcp serve` keeps the token it read
  at start: restart it to apply the new one.** After the restart, clients holding the old
  token get 401. The command says so when it rotates.
- `brain mcp token` describes the file: its path, mode and fingerprint. Add `--show` to
  print the token and `--json` for machine-readable output.
- `brain mcp serve` takes its token from, in order: `--token-file`, `BRAIN_MCP_TOKEN`,
  `BRAIN_MCP_TOKEN_PATH`, and `<brain_home>/mcp_token`. **With `BRAIN_MCP_TOKEN` set,
  rotating the file changes nothing until you unset it or pass `--token-file`.**
  `brain mcp token` says so when the variable is set.
- `BRAIN_MCP_TOKEN` supplies the token from the environment, for containers. It must look
  like a minted token (43 to 256 URL-safe characters). The file is preferred, because an
  environment variable is copied into every child process and its permissions cannot be
  checked.

## What it does not do

- **OAuth.** A static bearer token works with any client that lets you set an
  `Authorization` header. It does **not** satisfy claude.ai's custom-connector flow, which
  expects OAuth.
- **SSE transport.** It serves streamable HTTP only, with stateless JSON responses.
- **Per-client tokens.** There is one token per server.
- **A rate limit** beyond the concurrency cap.

`server.json` still declares stdio, because it describes the package `uvx` launches.
`brain mcp serve` is a daemon you start yourself.
