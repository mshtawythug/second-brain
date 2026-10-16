# Session handoff — "merge all, keep going until all is done, down to 0 issues" (updated 2026-10-16)

## Status on 2026-10-16: DONE except items that need the user

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

1. **S4: `com.brain.build` was retired on 2026-10-16.** Its plist was deleted and the job booted out,
   by running master's `brain-install-launchd` logic directly, not `brain-up`. On 2026-10-16, `launchctl list`
   showed only `com.brain.watcher` and `com.brain.brief`; re-check with `launchctl list | grep com.brain`.
   **Trap:** running `brain-up` or `brain-install-launchd` from a STALE install re-creates
   `com.brain.build`. On 2026-10-16 the main checkout's editable `.venv` was at `6a82db2` (whose
   `pyproject.toml` already says 0.3.0), and the pre-upgrade uv tool (v0.2.1) was staler still.
2. **The daemons and the uv tool `secondbrain-py` were upgraded from v0.2.1 to 0.3.0.** The install came
   from the detached worktree `.claude/worktrees/daemon-src` at `8900725`. **Do not delete that worktree**
   until the tool is reinstalled from somewhere else. On 2026-10-16 the uv receipt's source `directory`
   pointed at it; check `~/.local/share/uv/tools/secondbrain-py/uv-receipt.toml`.
3. **`com.brain.watcher` was restarted on 0.3.0 on 2026-10-16.** It reads `.env` through the
   `~/.brain/.env` symlink, so from that restart `BRAIN_OLLAMA_KEEP_ALIVE=30m` applied and PR #32's chat
   `num_ctx` (default 8192) took effect.
4. **The Homebrew shims `/opt/homebrew/bin/brain{,-mcp}` were repointed on 2026-10-16 to
   `~/.local/bin/brain{,-mcp}`** (the uv tool). Check with `ls -l /opt/homebrew/bin/brain*`.
   To revert one, run
   `ln -sfn ~/workspace/second-brain/.venv/bin/<name> /opt/homebrew/bin/<name>`.
5. **Prod got migration 028 on 2026-10-16.** It was applied with `brain.db.run_migrations` only, not
   `brain init`. The backup taken first is `~/.brain/backups/pre-028-second_brain-20261016T190652Z.dump`.
6. **The leftover sibling test DBs on 5434 were dropped:** 95 of them, per the ops record. Listing
   whatever siblings exist now is read-only:
   `psql -h localhost -p 5434 -U brain -d postgres -Atc "select datname from pg_database where datname like 'second_brain%' and datname not in ('second_brain_test','second_brain_test_sbx')"`.
   Expect live teammates' DBs in that list while agents are running.
7. **The merged remote branches on `second-brain` were deleted:** 24 of them, per the ops record. Of the
   pre-existing branches, only `chore/eval-ci-baseline` was left on 2026-10-16 (re-check with
   `git ls-remote --heads second-brain`). It was unmerged (PR #11 closed and was superseded by #13), so
   deleting it is the user's call.
8. **Claude Desktop's `brain` MCP server was repointed on 2026-10-16 to launch the uv tool.** In
   `~/Library/Application Support/Claude/claude_desktop_config.json`, the `brain` server's `command` was
   repointed from the main checkout's `.venv/bin/brain-mcp`, which is editable, was at `6a82db2` and
   bypassed the Homebrew shims, to `~/.local/bin/brain-mcp`. On 2026-10-16 this was the only brain MCP
   config on the machine, and no Claude Code config launched `brain-mcp`. **To revert,** restore
   `claude_desktop_config.json.bak-20261016T161548` from the same directory.

## Still needs the user

1. **Phase 9 (delete the wiki).** Gated on the user's word (spec Q12) and RA-8.
2. **Golden corpus.** As measured in `tests/eval/baselines/README.md` (`3612d0f`, 2026-10-15), the two
   pinned recency rows can reach only 10 of 28 and 0 of 5 of their judged docs. Re-judge them by hand,
   and re-derive the counts with the read-only SQL check in that README.
3. **Optional: lower `OLLAMA_CONTEXT_LENGTH`** in `~/Library/LaunchAgents/homebrew.mxcl.ollama.plist`, so
   other clients stop forcing 65536-context reloads. Brew may regenerate that plist.
4. **Restart Claude Desktop,** so its `brain-mcp` respawns on the upgraded install (see Done #8). Restart
   it again after the coordinator's planned reinstall (see "In flight").
5. **After the next ingest,** `ollama ps` should show the chat model (`llama3.1:8b`) at CONTEXT 8192
   and, once the reinstall picks up PR #36, the embed model at 2048.
6. **Host disk:** on 2026-10-16 the data volume was nearly full; check with
   `df -h /System/Volumes/Data`.
7. **The main checkout:** on 2026-10-16 it was far behind master (re-derive with
   `git -C <main> rev-list --count HEAD..second-brain/master`), belonged to another session, and held an
   UNTRACKED copy of this file. Once the PR that commits this file merges, that copy will block a
   `git pull`, so delete or move it first.

## In flight on 2026-10-16

- **Planned: the coordinator reinstalls the uv tool from the final master after the open PRs merge,
  and then `daemon-src` can be deleted.** As of 2026-10-16, the install that the daemons and Claude
  Desktop run was built at `8900725`, so PR #36 and later merges were not live in either. The
  reinstall moves the uv receipt's source off `daemon-src`, which is what makes that worktree safe to
  delete. Restarting Claude Desktop afterwards stays a user item (see above).
- PR #37 (open on 2026-10-16, branch `fix/eval-degraded-all-embedders`): `brain eval` degrades
  transient embed failures on every backend and aborts on permanent ones. It covers the Ollama/Voyage
  clauses of the eval-degraded follow-up below, not the `graph_runner` one.

## Recorded follow-ups

As of 2026-10-16, not started, except: the first item (fixed by this PR); the `qwen3` item (addressed
by PR #36); and the Ollama/Voyage eval-degraded item (in flight as PR #37). The `graph_runner` item is
left out of #37 on purpose.

- **Fixed by the PR that commits this doc** (branch `test/build-watcher-wait-backstop`, plan
  `docs/plans/2026-10-16-build-watcher-wait-backstop.md`). `tests/wiki/test_build_watcher.py` had its own
  `_wait_for(timeout=2.0)` and 9 short-timeout call sites, the same latency-budget flake class that PR #31
  fixed for the vault watcher. This PR moves the helper to `tests/thread_wait.py` (a 10 s backstop), and one
  guard in `tests/test_thread_wait.py` covers every importer. That guard also found a site PR #31's
  per-file guard could not see: `tests/test_cli_vault_watch.py` imported the helper through the test
  module and passed `timeout=5.0`.
- Watcher burst/stale-event tests that `sleep` then assert ABSENCE can false-PASS under load. The
  build watcher's `test_ignores_*` and coalescing tests are in the same class (listed in the
  2026-10-16 plan).
- ~~`qwen3` (non-default embedder) still inherits the server's 65536 context for embeddings.~~ Addressed
  by PR #36 (`69a780d`), which makes every Ollama embed request send its own `num_ctx`.
- At `69a780d`, only `OllamaEmbedError` marks an eval row degraded, and Voyage errors propagate. On
  2026-10-16 these two clauses were in flight as PR #37.
- At `69a780d`, `graph_runner` has no equivalent `degraded` tolerance. This is deliberately out of scope of PR #37, and
  the reason is in `graph_runner`'s module docstring on PR #37's branch.
- On 2026-10-16, old `.claude/worktrees/agent-*` worktrees (from July) held unmerged work. They were
  left alone.

## Traps learned (also in memory)

- Teammates read messages only between tasks; messages cross constantly — confirm SHAs.
- Two PRs that both grow an over-ceiling file collide on the same CLAUDE.md row and docstring "growth"
  numbering; the second merges master in (no rebase of a pushed branch) and records sibling trails.
- A promoted trail hop names the commit that WROTE the descriptive hop and grew the file — never a
  merge commit `git log -- <path>` does not list.
- psycopg auto-prepare silently ignores `SET LOCAL enable_indexscan=off` after 5 executions — use
  `prepare_threshold=None` for exact-reference probes.
- Same-size mutations + same-second restore → stale `.pyc` (use `PYTHONDONTWRITEBYTECODE=1`).
