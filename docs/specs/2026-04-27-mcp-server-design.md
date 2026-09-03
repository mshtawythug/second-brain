# Second Brain — MCP Server Design Spec

**Date:** 2026-04-27
**Owner:** Pat Morgan
**Status:** Draft — pending approval

## Goal

Make the second brain usable from **Claude Desktop** (and any other MCP-aware client — Cursor, Claude Code, etc.) without leaving the chat. Today the brain is reachable only via the `brain` CLI in a terminal; Desktop has no terminal. The MCP server exposes a narrow set of tools that wrap the existing `brain` library so Claude can search/show/list/save brain entries during a conversation.

## In scope (v1)

- stdio MCP server, single binary `brain-mcp`, launched by Claude Desktop on startup.
- Read tools: `brain_search`, `brain_show`, `brain_list`, `brain_status`.
- Curated write tools: `brain_ingest_stdin` (save a snippet), `brain_tag` (add/remove tags), `brain_edit` (flag-driven update of title/content_type/content/metadata).
- Reuses existing `src/brain/` modules as a library — no subprocess to the CLI, no re-parsing of stdout.
- Single user, local-only. Reads `DATABASE_URL` and `VOYAGE_API_KEY` from env (passed by Claude Desktop's config) or `.env`.

## Non-goals (v1)

- HTTP / SSE transport. stdio only.
- Multi-user, auth, ACLs.
- Destructive ops: `brain rm` stays in the terminal. The LLM should not be able to delete documents.
- File-path ingestion (`brain ingest`, `brain ingest-dir`): tools that take filesystem paths are confusing across Claude Desktop's sandbox boundary; keep them on the CLI.
- Editor mode (`brain edit` no-flags): editor blocking on stdio inside an MCP server doesn't make sense. The MCP `brain_edit` tool is flag-equivalent only — pass the new field values as args. Editor mode stays on the CLI.
- `brain init` / `brain doctor`: admin ops, terminal-only.
- `brain ingest-gmail`: heavy, infrequent, currently terminal-only. Skip for v1.
- Streaming responses. Result sets are small.

## Architecture

```
┌─────────────────────────────────────────────┐
│             Claude Desktop                  │
│                  │ (stdio JSON-RPC)         │
│                  ▼                          │
│        ┌──────────────────┐                 │
│        │  brain-mcp        │ <- new module  │
│        │  (mcp_server.py)  │                │
│        └─────┬─────────────┘                │
│              │                              │
│              ▼                              │
│   ┌──────────────────────────────┐          │
│   │ brain library                │          │
│   │  search.hybrid_search        │          │
│   │  ingest.update_document      │          │
│   │  ingest.ingest_document      │          │
│   │  ingest.stdin.make_doc       │          │
│   │  config.Config / db.connect  │          │
│   └─────┬──────────────────┬─────┘          │
│         │                  │                │
│  ┌──────▼─────┐    ┌──────▼─────┐           │
│  │ Postgres   │    │ Voyage AI  │           │
│  │ + pgvector │    │ embeddings │           │
│  └────────────┘    └────────────┘           │
└─────────────────────────────────────────────┘
```

Single long-running process per Claude Desktop session. No connection pool — psycopg connections opened per tool call (cheap, simple, avoids leaks). One `VoyageEmbedder` instance built at server start and reused (Voyage SDK handles its own rate limiting + retries).

## Tools

| Tool name             | Args                                                                 | Returns                                                                  | Writes? | Why |
|-----------------------|----------------------------------------------------------------------|--------------------------------------------------------------------------|---------|-----|
| `brain_search`        | `query: str`, `limit?: int=5`, `source?: str`, `tag?: str`, `since_days?: int`, `fts_only?: bool` | List of `{id, title, source_kind, snippet, score, content_type, tags}` | no      | The headline tool. Mirrors `brain search --json`. |
| `brain_show`          | `id_prefix: str` (≥6 hex chars)                                      | `{id, title, content, content_type, tags, source_path, ingested_at, source_kind}` | no | Full document body for follow-up after a search hit. |
| `brain_list`          | `source?: str`, `tag?: str`, `limit?: int=20`                        | List of `{id, title, content_type, tags, source_kind, ingested_at}`     | no      | Browse by source/tag. |
| `brain_status`        | (none)                                                               | `{documents, chunks, sources, last_ingest, by_kind: [{kind, count}]}`   | no      | Self-check / "is the brain healthy?" |
| `brain_ingest_stdin`  | `content: str`, `source: str`, `external_id: str`, `title: str`, `content_type?: str="note"`, `tags?: list[str]=[]`, `metadata?: dict={}`, `date?: str` | `{document_id, created}` | yes | "Save this conversation snippet to my brain" — same shape as the existing CLI command. Required `external_id` keeps dedup honest. |
| `brain_tag`           | `id_prefix: str`, `add?: list[str]=[]`, `remove?: list[str]=[]`      | `{document_id, tags}`                                                   | yes     | Cheap curation. Mirrors `brain tag`. |
| `brain_edit`          | `id_prefix: str`, `title?: str`, `content_type?: str`, `content?: str`, `metadata?: dict`, `replace_metadata?: bool=false` | `{document_id, fields_changed: list[str], rechunked: bool}` | yes | Flag-equivalent of `brain edit`. Body changes re-chunk + re-embed; field-only changes are a single UPDATE. **No `tags` arg** — use `brain_tag` for tag mutations (clean separation). At least one mutating arg required, otherwise MCP error. |

**Excluded by design:**
- `brain_ingest` (file path), `brain_ingest_dir` (file path) — terminal-only.
- `brain_rm` — destructive; user runs it from terminal.
- `brain_init`, `brain_doctor` — admin.
- `brain_ingest_gmail` — heavy, terminal-only.

## File layout

```
src/brain/
  mcp_server.py          ← new. Defines the MCP server + 6 tools. ~200 LOC.
  …existing modules…

pyproject.toml           ← add `mcp` dep (>=1.0,<2.0), add console_script `brain-mcp = brain.mcp_server:main`

tests/
  test_mcp_server.py     ← new. Unit tests calling each tool function directly with kwargs.
                           Plus one integration test that spawns brain-mcp via stdio,
                           sends `tools/list` + `tools/call`, verifies the response shape.
                           Reuses test_db, fake_embedder, seed_doc fixtures.

docs/specs/2026-04-27-mcp-server-design.md   ← this file
README.md                ← add a "Use from Claude Desktop" section with the config snippet
auto-memory/cli.md       ← add a "MCP server" section noting brain-mcp is the entry point
```

## Tool implementations (sketch)

Each tool function in `mcp_server.py` follows the same shape:

```python
@mcp_app.tool()
def brain_search(
    query: str,
    limit: int = 5,
    source: str | None = None,
    tag: str | None = None,
    since_days: int | None = None,
    fts_only: bool = False,
) -> list[dict[str, Any]]:
    """Hybrid search across the second brain. Returns up to `limit` matching documents
    ranked by RRF over FTS + vector cosine. Filter by source kind, tag, or recency."""
    with connect(_cfg.database_url) as conn:
        results = hybrid_search(
            conn,
            embedder=_embedder,
            query=query,
            limit=limit,
            source_kind=source,
            tag=tag,
            since_days=since_days,
            fts_only=fts_only,
        )
    return [
        {
            "id": r.document_id,
            "title": r.title,
            "source_kind": r.source_kind,
            "snippet": r.snippet,
            "score": r.score,
            "content_type": r.content_type,
            "tags": r.tags,
        }
        for r in results
    ]
```

Module-level state initialized once in `main()`:
```python
_cfg: Config = Config.load()
_embedder: VoyageEmbedder = VoyageEmbedder(api_key=_cfg.voyage_api_key)
```

`brain_ingest_stdin` reuses `make_doc` from `src/brain/ingest/stdin.py` and `ingest_document` from `src/brain/ingest/__init__.py` — same code path the CLI uses.

`brain_tag` reuses the same SQL the CLI's `tag` command runs. Or: extract the tag mutation into a helper in `src/brain/ingest/__init__.py` (`apply_tags(conn, document_id, *, add, remove) -> list[str]`) and have both the CLI and MCP server call it. Mild DRY win; small refactor.

## Error handling

| Failure mode                          | Surface as                                              |
|---------------------------------------|---------------------------------------------------------|
| `id_prefix` not found / ambiguous     | MCP error: `document not found: <prefix>` / `id prefix ambiguous: <prefix>` |
| Empty stdin content                   | MCP error: `content is empty`                           |
| Postgres unavailable                  | MCP error wrapping `psycopg.OperationalError`           |
| Voyage rate limit / API failure       | MCP error wrapping `voyageai.error.RateLimitError` etc. |
| Bad JSON in `metadata` arg            | MCP error: `metadata must be a JSON object`             |

All errors logged to stderr (Claude Desktop captures and surfaces them). Bodies and PII never logged at INFO — title + id only, same rule as the rest of the project.

## Claude Desktop integration

User adds to `~/Library/Application Support/Claude/claude_desktop_config.json`:

```json
{
  "mcpServers": {
    "brain": {
      "command": "/Users/you/workspace/second-brain/.venv/bin/brain-mcp",
      "env": {
        "DATABASE_URL": "postgresql://brain:brain@localhost:5433/second_brain",
        "VOYAGE_API_KEY": "<paste here>"
      }
    }
  }
}
```

Restart Claude Desktop. Tools appear in any conversation. The user can ask "search my brain for the Q1 review with Jane Doe" and Claude Desktop calls `brain_search`.

README will document this exact snippet under a new **Use from Claude Desktop** section.

## Testing

Per CLAUDE.md targets: 85% overall, 95% on pure logic, 90% on ingest pipeline. The MCP server is a thin orchestration layer — target 95% line coverage since the logic is mostly delegation.

**Unit tests (in `test_mcp_server.py`):**

1. `test_brain_search_returns_expected_shape` — seed two docs, call `brain_search(query=…)`, assert returned dicts have all expected keys and snippets.
2. `test_brain_search_respects_filters` — seed three docs across two source kinds; filter by `source="krisp"`; verify only krisp docs returned.
3. `test_brain_show_full_document` — seed doc, call `brain_show(id_prefix=…)`, verify full content returned.
4. `test_brain_show_unknown_id_errors` — call with random hex; assert MCP error.
5. `test_brain_show_ambiguous_prefix_errors` — seed two docs with shared 6-char prefix; assert error.
6. `test_brain_list_filters_by_source_and_tag` — seed mixed, verify filtering.
7. `test_brain_status_returns_counts` — seed N docs, assert counts match.
8. `test_brain_ingest_stdin_creates_document` — call with content + source + external_id; assert returned `{document_id, created: True}` and the doc shows up in `brain_search`.
9. `test_brain_ingest_stdin_dedup_on_external_id` — call twice with same `(source, external_id)`; second call returns `created: False`.
10. `test_brain_ingest_stdin_empty_content_errors` — assert MCP error.
11. `test_brain_tag_adds_and_removes` — call with `add=["x"]`, then `remove=["x"]`; verify final tags.
12. `test_brain_tag_unknown_id_errors`.
13. `test_brain_edit_title_only_no_voyage_call` — counting embedder asserts no embed call; new title persists.
14. `test_brain_edit_content_rechunks_and_reembeds` — chunk count + content_hash both change.
15. `test_brain_edit_metadata_merge_keeps_other_keys` — start `{a:1,b:2}`, patch `{b:3}` → `{a:1,b:3}`.
16. `test_brain_edit_metadata_replace_swaps_blob` — `replace_metadata=True` overwrites the entire blob.
17. `test_brain_edit_no_args_errors` — call with only `id_prefix`; assert MCP error.
18. `test_brain_ingest_stdin_auto_tags_with_source_mcp` — call without `tags`; verify the stored doc has `["source-mcp"]`.
19. `test_brain_ingest_stdin_user_tags_union_with_source_mcp` — call with `tags=["interview"]`; verify stored doc has `["interview", "source-mcp"]` (order-independent).

**Integration test (just one — the rest at unit level):**

13. `test_mcp_server_responds_to_tools_list` — spawn `brain-mcp` via subprocess on stdio, send a `tools/list` JSON-RPC request, parse the response, assert the 6 tools are advertised with correct schemas. This exercises the actual MCP protocol once; subsequent runs trust the unit tests.

**Reuse:** `test_db`, `fake_embedder`, `seed_doc` fixtures from `tests/conftest.py`. The fake embedder + real test DB pattern keeps tests fast and offline.

## Risks

- **Voyage cold start.** First `brain_search` after server boot takes longer (network round-trip to Voyage). Mitigation: a one-shot warmup embed call in `main()` before serving (ignore failures — search will retry on demand).
- **MCP SDK versioning.** Pin `mcp >=1.0,<2.0` in `pyproject.toml`. Re-evaluate when 2.0 lands.
- **DB connection on every tool call.** Negligible at single-user scale; revisit if we ever batch.
- **Path leakage.** `brain_show` returns `source_path` which is a local filesystem path. Fine for the user's own tool; flag if we ever ship this multi-user.
- **Conversation context bloat.** Returning full document bodies via `brain_show` can be large. Soft cap: tools document that bodies up to ~50KB are fine; users should `brain_search` first to land on the right doc, then `brain_show`.

## Decisions (resolved 2026-04-27)

1. **`brain_edit` included in v1** as a flag-equivalent tool (no `tags` arg — use `brain_tag` for those). Maps directly to `update_document` in `src/brain/ingest/__init__.py`.
2. **`brain_ingest_stdin` auto-adds the `source-mcp` tag** so MCP-saved snippets are distinguishable from CLI-ingested ones. The user can pass additional tags via the `tags` arg; they're unioned with `source-mcp`. Tag dedup matches the existing CLI behavior.
3. **`BRAIN_MCP_LOG_LEVEL` env var** (default `INFO`, accepts standard Python logging levels: `DEBUG|INFO|WARNING|ERROR`). Used by `mcp_server.main()` to configure stderr logging. Misconfigured value falls back to `INFO` with a warning.

## Out of scope, deferred to a later spec

- HTTP / SSE transport so the server can be run on a different machine.
- Reranking before returning search results.
- Long-form `brain_summarize_tag(tag)` tool.
- Per-document permission scoping if this ever leaves single-user.
