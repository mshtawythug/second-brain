# Serving the tools to another machine — `brain mcp serve`

Part of the [`brain-maintenance`](../SKILL.md) skill. Its safety rules apply here too.

`brain-mcp` speaks stdio, for MCP clients on this machine. To reach the brain from another
device on a private network (a laptop, an agent host on a tailnet), the **user** runs
`brain mcp serve`. It is a daemon they start themselves; nothing listens until they do.

```bash
brain mcp token --rotate                  # mint <brain_home>/mcp_token (mode 0600)
brain mcp token --show                    # print it, to paste into the other client
brain mcp serve                           # http://127.0.0.1:8787/mcp, read-only
brain mcp serve --host <tailnet address> --allow-remote --allowed-host <tailnet name>
```

## The safe defaults, and what each flag gives up

| Default | Flag that changes it | Say this before suggesting it |
|---|---|---|
| Loopback only | `--host <address> --allow-remote` (needs the token) | Only a private-network address. Never `0.0.0.0` on an open network, never port-forwarding. |
| Bearer token required | `--no-auth` | Loopback only; it is refused anywhere else. |
| Read-only (write tools removed) | `--allow-writes` | A leaked token could then edit, delete or rebuild. |
| Confidential documents refused | `--include-confidential` | Confidential bodies and titles would leave the machine. |
| Graph tools (and `brain_review_weekly`, `brain_gaps`) withheld, `brain_ask` hybrid only | `--include-confidential` | The entity graph and the search log are built from every document, so their names, links, themes, communities and search terms carry confidential content. |

## The token

- The server reads its token **once, at start**. After `brain mcp token --rotate`, the
  user must **restart `brain mcp serve`**; until then the old token still works.
- Precedence: `--token-file`, then `BRAIN_MCP_TOKEN`, then `BRAIN_MCP_TOKEN_PATH`, then
  `<brain_home>/mcp_token`. With `BRAIN_MCP_TOKEN` set, rotating the file changes nothing;
  `brain mcp token` says so.
- Never paste the token into a chat, a file in the repo, or a command the user did not ask
  for. `brain mcp token` without `--show` prints only a fingerprint.

## Reading a refusal

- Exit 2 means a refused option or an unsafe token file; the message names the fix (for a
  wildcard bind, `--allowed-host <name>`, or bind the private address).
- On mcp 1.x the command exits 2 and names `pip install 'secondbrain-py[http]'`.
- A client sees 401 (token), 400 (Host not allowed: add `--allowed-host`), 404 (wrong
  path), 413 (body too big), 503 (busy), 504 (call too slow) or
  `tool <name> failed (<Class>)`, whose detail is in the server's own log.

The full guide is `docs/guides/mcp-http-transport.md` in the repository.
