# Hooking the Second Brain into Claude Desktop

> Part of the [Second Brain](../../README.md) docs — see [docs/README.md](../README.md) for the full index.

Step-by-step to get the `brain_*` MCP tools usable from any Claude Desktop chat.

> Paths below use `/Users/you/...` as a placeholder — replace `/Users/you`
> with your own home directory (`echo $HOME`), and swap
> `~/workspace/second-brain` for wherever you cloned the repo.

## What you need before starting

- Postgres + pgvector running on `localhost:55432` (`docker compose up -d` from the repo root). Check with `docker compose ps`.
- The same embedder backend configured for the CLI: Ollama running for `arctic` / `qwen3`, or a Voyage AI API key for `voyage`.
- A vault folder if you want MCP note-authoring tools (`brain vault init` creates the default `~/brain-vault`).
- The `brain-mcp` binary registered in your venv at `~/workspace/second-brain/.venv/bin/brain-mcp`. (Created automatically by `pip install -e ".[dev]"` since `pyproject.toml` declares the console script.)

## Step 1 — Globally callable `brain-mcp` (mirrors `brain`)

Mirror the `brain` setup with a Homebrew-prefix symlink:

```bash
ln -s "$HOME/workspace/second-brain/.venv/bin/brain-mcp" /opt/homebrew/bin/brain-mcp
which brain-mcp     # → /opt/homebrew/bin/brain-mcp
```

Claude Desktop doesn't strictly need this (the config below uses the absolute venv path) but it lets you run `brain-mcp` from any terminal for debugging.

## Step 2 — Sanity-check the server boots

Run the protocol integration test once. It spawns `brain-mcp` over real stdio, sends `tools/list`, and asserts the advertised tool schemas come back:

```bash
cd ~/workspace/second-brain
source .venv/bin/activate
pytest tests/test_mcp_server_protocol.py -v
```

Should print `1 passed`. (You can ignore the `Required test coverage of 85% not reached` line — that's the project-wide gate firing on a single-file run; the test itself passed.)

Optional manual smoke: `brain-mcp` (boots, waits on stdin for JSON-RPC, Ctrl-C to exit).

## Step 3 — Configure Claude Desktop

File: `~/Library/Application Support/Claude/claude_desktop_config.json`

Add a top-level `mcpServers` key as a sibling to whatever's already there:

```json
"mcpServers": {
  "brain": {
    "command": "/Users/you/workspace/second-brain/.venv/bin/brain-mcp",
    "env": {
      "DATABASE_URL": "postgresql://brain:brain@localhost:55432/second_brain",
      "BRAIN_EMBEDDER": "arctic",
      "OLLAMA_HOST": "http://localhost:11434",
      "BRAIN_VAULT_PATH": "/Users/you/brain-vault",
      "BRAIN_MCP_LOG_LEVEL": "INFO"
    }
  }
}
```

For Voyage, replace the local-backend keys with `"BRAIN_EMBEDDER": "voyage"` and `"VOYAGE_API_KEY": "PASTE_YOUR_VOYAGE_KEY_HERE"`. Don't paste the key into chats or commit the config file anywhere.

Key env vars:
- `DATABASE_URL` — Postgres connection string. Same as the CLI uses.
- `BRAIN_EMBEDDER` — `arctic`, `voyage`, or `qwen3`; must match the database's embedding column.
- `OLLAMA_HOST` / `QWEN3_MODEL` — local backend settings for `arctic` / `qwen3`.
- `VOYAGE_API_KEY` — required only for `voyage`.
- `BRAIN_VAULT_PATH` — vault folder for MCP-created notes and daily notes.
- `BRAIN_MCP_LOG_LEVEL` — `INFO` is the default. Set to `DEBUG` while debugging integration; `WARNING`/`ERROR` if logs get noisy. Unknown values silently fall back to `INFO`.

## Step 4 — Restart Claude Desktop

`Cmd-Q` (full quit, not just close window) and reopen from Applications. MCP servers only re-spawn on full restart.

## Step 5 — Smoke-test in a new chat

Open a new conversation and try:

- **"Search my brain for Jane Doe"** → calls `brain_search`
- **"What's the status of my brain?"** → calls `brain_status`
- **"List the 5 most recent documents in my brain"** → calls `brain_list`
- **"Save this conversation to my brain as a meeting note titled X"** → calls `brain_ingest_stdin` (auto-tagged `source-mcp`)
- **"Create a daily note for today with these bullets"** → calls `brain_daily`, then `brain_edit`
- **"What links to this note?"** → calls `brain_backlinks`

Claude Desktop's tools indicator (gear / plug icon) should show the `brain_*` tools. Clicking should expand to show the schemas.

## The tools

| Tool | What it does |
|---|---|
| `brain_search` | Hybrid (FTS + vector) search across the brain. Filters: `source`, `tag`, `since_days`, `fts_only`. |
| `brain_show` | Full document body by id-prefix. |
| `brain_list` | Browse documents. Filters: `source`, `tag`. |
| `brain_status` | Counts + last-ingest timestamp + by-kind breakdown. |
| `brain_ingest_stdin` | Save a snippet. Auto-tags `source-mcp`. Dedupes by `(source, external_id)`. |
| `brain_tag` | Add/remove tags on an existing document. |
| `brain_edit` | Update title / content_type / content / metadata. Body changes re-embed. |
| `brain_backlinks` | List documents that link to a document. |
| `brain_links` | List outgoing links, optionally including unresolved refs. |
| `brain_orphans` | List docs with no incoming or outgoing links. |
| `brain_note_new` | Create a vault note without opening `$EDITOR`. Auto-tags `source-mcp`. |
| `brain_daily` | Resolve or create a daily note for a date. |
| `brain_link_proposal` | Propose a `[[link]]` from one vault note to another without writing files. |

## Troubleshooting

**Tools don't appear in Claude Desktop**
- Check the MCP launch log: `~/Library/Logs/Claude/mcp*.log` (or `mcp-server-brain.log` specifically). Startup errors land there.
- Confirm the absolute path in the config exists: `ls /Users/you/workspace/second-brain/.venv/bin/brain-mcp`.
- Confirm the JSON is valid: `python -m json.tool < ~/Library/Application\ Support/Claude/claude_desktop_config.json`.

**`brain_search` returns "embedding failed"**
- For `arctic` / `qwen3`, confirm Ollama is running and the model is pulled.
- For `voyage`, check the key and rate limits.
- Set `BRAIN_MCP_LOG_LEVEL=DEBUG` and restart Claude Desktop. The full exception lands in stderr (captured by Claude Desktop's MCP log).

**Postgres connection error**
- `docker compose ps` — is the container up?
- Was it stopped/restarted recently? `brain doctor` in the terminal will report the same problem and confirm.

**Cold-start latency**
- First `brain_search` may take ~0.5–1.5s while the Voyage embedder warms up. The server already does a one-shot warmup embed at startup to cut this down; if it still feels slow, it's network round-trip time to Voyage.

**Server logs**
- All logging goes to **stderr** — never stdout (which is the JSON-RPC channel). Claude Desktop captures stderr into `~/Library/Logs/Claude/mcp-server-brain.log`. Tail it while debugging.

## Updating after code changes

After pulling new commits:
```bash
cd ~/workspace/second-brain && source .venv/bin/activate
pip install -e ".[dev]"        # only if pyproject.toml changed
brain init                      # only if src/brain/migrations/ has new files
```
Then **Cmd-Q + reopen Claude Desktop** so the server re-spawns with the updated code.

## Reference

- The "Claude Desktop (MCP server)" section in [docs/configuration.md](../configuration.md): high-level summary + the full env-var table.
