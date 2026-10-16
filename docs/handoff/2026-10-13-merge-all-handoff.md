# Session handoff — "merge all, keep going until all is done, down to 0 issues" (updated 2026-10-16)

## Status: DONE except items that need the user

Merged to `second-brain/master` (all through the rule-14 review + audit loop at zero issues, CI green):

| PR | What | Merge |
|---|---|---|
| #17–#21 | phase 5, mypy/ruff caps, gitignore, eval re-record (2026-10-12/13) | — |
| #23 | debounce-flake fix (injected timer) | `2b6f560` |
| #24 | phase-5 follow-ups | `2dd7d61` |
| #22 | search vector leg = scoped iterative HNSW | `c30449e` |
| #25 | `ci.json` re-record (7 vault notes synced 2026-10-14) | `edd8986` |
| #26 | **phase 7** — wiki demoted; `brain ui --host/--token/--lan` | `5b1d285` |
| #27 | **phase 8** — wiki dormant (R2): quartz opt-in, `com.brain.build` retired | `e1fc28e` |
| #28 | eval recency rows pinned to absolute `after:`/`before:` dates; strict `since_days` | `b1451d5` |
| #29 | A9 — `brain ui` watcher status line (shared `doctor_watcher.watcher_state`) | `62f0d63` |
| #30 | `ci.json` re-recorded with the pinned recency rows | `5dfbe46` |
| #31 | vault-watcher racing-edit flake: test made synchronous; `_wait_for` is a 10 s backstop | `f6a588d` |
| #32 | brain sends `options.num_ctx` (`BRAIN_OLLAMA_NUM_CTX`, default 8192) on every chat call | `7545ce1` |
| #33 | `brain eval` refuses to record / gate on a degraded (embed-timeout) run | `293a3b9` |
| #34 | related + connect vector legs on shared design-D `brain.hnsw_scan`; NaN-proof floors | `8900725` |
| #36 | embedders send options.num_ctx (BRAIN_OLLAMA_EMBED_NUM_CTX, default 2048) + truncate:false | `69a780d` |

## Done on 2026-10-16 (machine state; re-checked read-only the same day)

1. **S4: `com.brain.build` is retired.** Its plist is deleted and the job is booted out. This was done by
   running master's `brain-install-launchd` logic directly, not `brain-up`. `launchctl list` now shows
   only `com.brain.watcher` and `com.brain.brief`. **Trap:** running `brain-up` or
   `brain-install-launchd` from a STALE install re-creates `com.brain.build`. The main checkout's editable
   `.venv` is at `6a82db2` (whose `pyproject.toml` already says 0.3.0), and the pre-upgrade uv tool
   (v0.2.1) was staler still.
2. **The daemons and the uv tool `secondbrain-py` were upgraded from v0.2.1 to 0.3.0.** The install came
   from the detached worktree `.claude/worktrees/daemon-src` at `8900725`. **Do not delete that worktree**
   until the tool is reinstalled from somewhere else: the uv receipt's source `directory` points at it.
3. **`com.brain.watcher` was restarted on 0.3.0.** It reads `.env` through the `~/.brain/.env` symlink,
   so `BRAIN_OLLAMA_KEEP_ALIVE=30m` applies, and PR #32's chat `num_ctx` (default 8192) is live.
4. **The Homebrew shims `/opt/homebrew/bin/brain{,-mcp}` now point at `~/.local/bin/brain{,-mcp}`**
   (the uv tool). To revert one, run
   `ln -sfn ~/workspace/second-brain/.venv/bin/<name> /opt/homebrew/bin/<name>`.
5. **Prod has migration 028.** It was applied with `brain.db.run_migrations` only, not `brain init`. The
   backup taken first is `~/.brain/backups/pre-028-second_brain-20261016T190652Z.dump`.
6. **The leftover sibling test DBs on 5434 were dropped:** 95 of them, per the ops record. Listing
   whatever siblings exist now is read-only:
   `psql -h localhost -p 5434 -U brain -d postgres -Atc "select datname from pg_database where datname like 'second_brain%' and datname not in ('second_brain_test','second_brain_test_sbx')"`.
   Expect live teammates' DBs in that list while agents are running.
7. **The merged remote branches on `second-brain` were deleted:** 24 of them, per the ops record. Of the
   pre-existing branches, only `chore/eval-ci-baseline` remains. It is unmerged (PR #11 closed and was
   superseded by #13), so deleting it is the user's call.
8. **Claude Desktop's `brain` MCP server now launches the uv tool.** In
   `~/Library/Application Support/Claude/claude_desktop_config.json`, the `brain` server's `command` was
   repointed from the main checkout's `.venv/bin/brain-mcp`, which is editable, sits at `6a82db2` and
   bypassed the Homebrew shims, to `~/.local/bin/brain-mcp`. This is the only brain MCP config on the
   machine; no Claude Code config launches `brain-mcp`. **To revert,** restore
   `claude_desktop_config.json.bak-20261016T161548` from the same directory.

## Still needs the user

1. **Phase 9 (delete the wiki).** Gated on the user's word (spec Q12) and RA-8.
2. **Golden corpus.** The two pinned recency rows reach only 10/28 and 0/5 judged docs; re-judge them
   by hand.
3. **Optional: lower `OLLAMA_CONTEXT_LENGTH`** in `~/Library/LaunchAgents/homebrew.mxcl.ollama.plist`, so
   other clients stop forcing 65536-context reloads. Brew may regenerate that plist.
4. **Restart Claude Desktop,** so its `brain-mcp` respawns on the upgraded install (see Done #8).
5. **After the next ingest, confirm that `ollama ps` shows CONTEXT 8192.**
6. **Host disk:** the data volume was 93% full when measured on 2026-10-16.
7. **The main checkout** is 157 commits behind `second-brain/master` (measured 2026-10-16) and belongs to
   another session. It holds an UNTRACKED copy of this file. Once the PR that commits this file merges,
   that copy will block a `git pull`, so delete or move it first.

8. **Reinstall the daemons' uv tool after the remaining PRs merge.** The uv tool install that the
   daemons and Claude Desktop run was built from the `daemon-src` worktree at `8900725`. PR #36 and
   anything merged after it are not live in either until the tool is reinstalled from a newer
   checkout. The coordinator plans to reinstall once the remaining PRs merge. When that happens, the
   uv receipt's source moves off `daemon-src`, and only then can that worktree go.

## In flight when this was written

- A follow-up on how `brain eval` handles degraded runs.

## Recorded follow-ups

Not started, except the first item, which is fixed.

- **Fixed by the PR that commits this doc** (branch `test/build-watcher-wait-backstop`, plan
  `docs/plans/2026-10-16-build-watcher-wait-backstop.md`). `tests/wiki/test_build_watcher.py` had its own
  `_wait_for(timeout=2.0)` and 9 short-timeout call sites, the same latency-budget flake class that PR #31
  fixed for the vault watcher. The helper now lives in `tests/thread_wait.py` (a 10 s backstop), and one
  guard in `tests/test_thread_wait.py` covers every importer. That guard also found a site PR #31's
  per-file guard could not see: `tests/test_cli_vault_watch.py` imported the helper through the test
  module and passed `timeout=5.0`.
- Watcher burst/stale-event tests that `sleep` then assert ABSENCE can false-PASS under load. The
  build watcher's `test_ignores_*` and coalescing tests are in the same class (listed in the 2026-10-16 plan).
- ~~`qwen3` (non-default embedder) still inherits the server's 65536 context for embeddings.~~ Addressed
  by PR #36 (`69a780d`): every Ollama embed request now sends its own `num_ctx`.
- Only `OllamaEmbedError` marks an eval row degraded; Voyage errors still propagate; `graph_runner` has no
  equivalent tolerance.
- Old `.claude/worktrees/agent-*` (July) hold unmerged work — left alone.

## Traps learned (also in memory)

- Teammates read messages only between tasks; messages cross constantly — confirm SHAs.
- Two PRs that both grow an over-ceiling file collide on the same CLAUDE.md row and docstring "growth"
  numbering; the second merges master in (no rebase of a pushed branch) and records sibling trails.
- A promoted trail hop names the commit that WROTE the descriptive hop and grew the file — never a
  merge commit `git log -- <path>` does not list.
- psycopg auto-prepare silently ignores `SET LOCAL enable_indexscan=off` after 5 executions — use
  `prepare_threshold=None` for exact-reference probes.
- Same-size mutations + same-second restore → stale `.pyc` (use `PYTHONDONTWRITEBYTECODE=1`).
