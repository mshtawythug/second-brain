# Session handoff — "merge all, keep going until all is done, down to 0 issues" (updated 2026-10-15)

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

## Needs the user

1. **S4 — live launchd migration.** The user's next `brain-up` runs `brain-install-launchd`, which boots
   out and DELETES `com.brain.build`. Nothing has touched launchd on this machine.
2. **Phase 9 (delete the wiki)** — gated on the user's word (spec Q12) + RA-8.
3. **Ollama RAM.** `.env` now has `BRAIN_OLLAMA_KEEP_ALIVE=30m` (was `-1`, never unload). The vault
   watcher must be restarted to pick it up (launchd — the user's action). PR #32 makes brain chat calls
   request an 8192 context (KV ~544 MiB instead of ~4352 MiB at 65536); the first call after the restart
   reloads the model once. Optional: lower `OLLAMA_CONTEXT_LENGTH` in
   `~/Library/LaunchAgents/homebrew.mxcl.ollama.plist` so other clients stop forcing 65536 reloads.
4. **Golden corpus:** the two pinned recency rows reach only 10/28 and 0/5 judged docs — re-judge by hand.
5. **Remote branch cleanup:** merged branches on `second-brain` (`git branch -r --merged second-brain/master`).
6. **Leftover sibling test DBs on 5434** created by earlier agents (never dropped by the coordinator —
   `DROP DATABASE` is the user's call): list with
   `psql -h localhost -p 5434 -U brain -lqt | cut -d'|' -f1 | grep -v '^ *second_brain_test *$'`.
   The 5434 data disk is nearly full and logged an fsync I/O error on 2026-10-13.

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
- `qwen3` (non-default embedder) still inherits the server's 65536 context for embeddings.
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
