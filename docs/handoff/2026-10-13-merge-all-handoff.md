# Session handoff — "merge all, keep going until all is done, down to 0 issues" (updated 2026-10-16; closed out 2026-10-21)

## Closed out 2026-10-21 (at `second-brain/master` `c9f6408`)

Every claim in this section is dated 2026-10-21 or bound to a SHA. Re-derive before acting on it.
The 2026-10-16 sections below are kept as history and are annotated where this section supersedes
them.

### Merged since the 2026-10-16 update

| PR | What | Merge |
|---|---|---|
| #35 | build-watcher `_wait_for` backstop moved to `tests/thread_wait.py` | `210d288` |
| #37 | `brain eval` degrades transient embed failures on every backend, aborts on permanent ones | `a967685` |
| #38 | test env isolation (`tests/env_isolation.py`) | `1cd136a` |
| #39 | MCP log level read after the dotenv chain; `brain setup` re-runs read `$BRAIN_HOME/.env` | `1481e1e` |
| #40 | `brain-install-launchd` resolves the vault through `brain.install_config` | `6948c6f` |
| #41 | the two pinned recency rows re-judged; `ci.json` re-recorded | `eec002f` |
| #42 | `python -m brain.install_config BRAIN_HOME`; `brain-status`, `brain-monitor`, `_brain-watcher-fg` resolve the vault through it | `6ba3084` |
| #43 | **phase 9 (R3): the Quartz wiki is deleted** | `cf0d209` |
| #44 | concept extraction: split, then salvage, a reply truncated at `num_predict` | `c9f6408` |

Re-derive: `git log --first-parent --format='%h %ad %s' --date=short 69a780d..second-brain/master`.

### Done on 2026-10-21 (machine state, not in git)

1. **`OLLAMA_CONTEXT_LENGTH` was lowered from 65536 to 8192** in
   `~/Library/LaunchAgents/homebrew.mxcl.ollama.plist`. The backup taken first is
   `~/.brain/backups/launchagents_20261021T164217Z/`. **Trap:** the plist is now hand-edited and also
   carries custom `OLLAMA_KEEP_ALIVE=30m` and `OLLAMA_DEBUG_LOG_REQUESTS`. A `brew services restart`
   or a brew upgrade of ollama regenerates the plist and reverts all of it, so restart Ollama with
   `launchctl bootout` + `bootstrap` instead. Re-check with
   `plutil -extract EnvironmentVariables json -o - ~/Library/LaunchAgents/homebrew.mxcl.ollama.plist`.
2. **`brain graphrag communities refresh` ran on prod, after a backup:** 136 → 202 communities, all 202
   summarized. The backups are `~/.brain/backups/pre-communities-refresh-second_brain-20261021T164327Z.dump`
   and `~/.brain/backups/pre-communities-fill-second_brain-20261021T170526Z.dump`. Re-derive the count
   read-only with `brain graphrag communities list`.
3. **The stale `brain-build.pid` was deleted.** It was a leftover of the retired build daemon.
4. **The main checkout was fast-forwarded twice:** from `6a82db2` to `6948c6f`, then to `c9f6408`. The UNTRACKED copy of this file that 2026-10-16
   item 7 warned about now sits at
   `~/.brain/backups/main-checkout-untracked-merge-all-handoff-20261021T164549Z.md`.
5. **Clean-up:** 18 merged worktrees and 20 merged local branches were removed. The remote
   `chore/eval-ci-baseline` was deleted; its tip was `a6c4acf` (PR #11, superseded by #13). Re-check with
   `git worktree list` and `git ls-remote --heads second-brain`.
6. **A user-level brew Caddy LaunchAgent was stopped.** It had been crash-looping, with 61,603 runs
   counted by ops on 2026-10-21. Its plist backup is
   `~/.brain/backups/homebrew.mxcl.caddy.user-plist.20261021-151507.plist`. The ROOT Caddy is a different
   job; see "Remains the user's".
7. **Disk caches were cleaned:** `brew cleanup`, the uv, pip and npm caches, and an orphaned go-build
   cache. Free space on the data volume went from 16 to 22 GiB, as measured by ops before swap grew
   again. Re-check with `df -h /System/Volumes/Data`.
8. **`ollama ps` was checked.** It showed `llama3.1:8b` at CONTEXT 8192 and `snowflake-arctic-embed2` at
   2048. That is PR #32 and PR #36 live on the daemons.
9. **The uv tool `secondbrain-py` was reinstalled from master `c9f6408` on Python 3.12.** The source
   was the `.claude/worktrees/daemon-src` worktree, detached at `c9f6408`. **Trap:** the first attempt
   shipped the deleted `brain/wiki` package. The cause was a gitignored setuptools `build/lib` and a
   `src/secondbrain_py.egg-info` left in `daemon-src` by an earlier build: setuptools copies `build/lib`
   into the wheel. They were deleted before the second, clean install.

   **Every install re-creates both directories.** On 2026-10-21, after the clean install, they were
   back in `daemon-src`. So the delete is a standing step, not a one-off fix: before every
   `uv tool install --force <daemon-src>`, delete `daemon-src/build` and
   `daemon-src/src/secondbrain_py.egg-info`.

   Verify by the installed file list, not by the version string, which still reads 0.3.0. The `brain/`
   entries of the dist-info `RECORD` are a subset of `git ls-files src/brain`, with exactly two expected
   omissions: tracked files that are not package data, `brain/graph_rag/aliases/default.yml.example` and
   `brain/migrations/README.md`. This check prints nothing on a clean install. It was run on 2026-10-21
   at `c9f6408`, from the repo root:

   ```bash
   R=$(ls -d ~/.local/share/uv/tools/secondbrain-py/lib/python3.12/site-packages/secondbrain_py-*.dist-info)/RECORD
   diff <(cut -d, -f1 "$R" | grep '^brain/' | grep -v '__pycache__' | sort) \
        <(git -C .claude/worktrees/daemon-src ls-files src/brain | sed 's|^src/||' \
          | grep -vx 'brain/graph_rag/aliases/default.yml.example\|brain/migrations/README.md' | sort)
   ```

   After the reinstall, on 2026-10-21:
   - the check above printed nothing;
   - launchd held only `com.brain.watcher` and `com.brain.brief`;
   - the baked vault path was unchanged;
   - the watcher was running clean;
   - `brain doctor` reported 18/18 OK.

   Re-check with `launchctl list | grep com.brain` and `brain doctor`.
10. **The skills were re-synced** with `bin/brain-skills-sync`. PR #43 had changed 4 of them; afterwards
   all 11 installed copies matched master. Re-check with
   `cmp skills/<s>/SKILL.md ~/.claude/skills/<s>/SKILL.md`.
11. **Claude Desktop was relaunched.** Its `brain` MCP config points at `~/.local/bin/brain-mcp`, which is
    the reinstalled code. Desktop starts local MCP servers lazily, and had started none since 2026-09-23,
    so a missing `brain-mcp` process right after a relaunch is not a failure by itself.
12. **A DB-only backup was taken:** `~/.brain/backups/brain-backup-20261021-192017-db-only.tar.gz`
    (138.7 MiB, sha256 verified). A full backup was skipped on purpose. `brain backup` tars the vault
    unfiltered, so it would have included `<vault>/.quartz`, which held about 11 GB of dead build output
    on 2026-10-21. See "Remains the user's" item 2 and "Follow-ups still open".

### The 2026-10-16 "Still needs the user" list, item by item

| # | Item | Status on 2026-10-21 |
|---|---|---|
| 1 | Phase 9 (delete the wiki) | **DONE:** PR #43, merge `cf0d209`. The user gave the word on 2026-10-21, and RA-8 was rejected (plan `docs/plans/2026-10-21-wiki-to-ui-phase9.md` §0). |
| 2 | Golden corpus re-judge | **DONE:** PR #41 (`eec002f`) re-judged the two pinned recency rows and re-pinned the planning row to `after: 2026-07-24` / `before: 2026-09-02`. The means at `eec002f` were 0.3253/0.4310/0.7581 (nDCG@5/MRR/Recall@20; re-derive from `tests/eval/baselines/ci.json` at that commit). **The other 29 rows: DONE 2026-10-22** by PR #50. See "Remains the user's" item 4. |
| 3 | Lower `OLLAMA_CONTEXT_LENGTH` | **DONE:** "Done on 2026-10-21" item 1. |
| 4 | Restart Claude Desktop | **DONE:** "Done on 2026-10-21" item 11, after the `c9f6408` reinstall (item 9). |
| 5 | Check `ollama ps` after an ingest | **DONE:** "Done on 2026-10-21" item 8. |
| 6 | Host disk | **Remains the user's.** On 2026-10-21, `df -h /System/Volumes/Data` showed 94% used. See "Remains the user's". |
| 7 | Main checkout behind master; untracked copy of this file | **DONE:** "Done on 2026-10-21" item 4. |

### Remains the user's (as of 2026-10-21)

1. **Stop the ROOT Caddy that still serves the retired wiki on :8080, then delete its config.** It is a
   system LaunchDaemon reading `/opt/homebrew/etc/Caddyfile`. First run
   `sudo brew services stop caddy`, then delete `/opt/homebrew/etc/Caddyfile`. Re-check with
   `curl -sI http://localhost:8080`; `lsof` without sudo cannot see root's listener.
2. **Delete `<vault>/.quartz` by hand** (about 11 GB on 2026-10-21; re-derive with
   `du -sh <vault>/.quartz`), then **run a full `brain backup`**. Only a DB-only backup exists from
   2026-10-21 ("Done on 2026-10-21" item 12). R3 deliberately touched nothing on the machine (plan §6).
   `docs/vault.md` lists the leftovers. Vault sync and watch keep skipping `.quartz/` until it is gone
   (ruling R9-8).
3. **Delete or keep two stale recent-rail leftovers** (2026-10-21): `<vault>/_partials/recent.md`, and
   the `<!-- BRAIN_RECENT_START -->` … `<!-- BRAIN_RECENT_END -->` fence in `<vault>/index.md`. They
   were written by `brain.wiki.build_homepage`, which was deleted at R3 (`885b5a6`; read it with
   `git show 885b5a6^:src/brain/wiki/build_homepage.py`). Nothing refreshes them now, so they are frozen
   at the last wiki build. Delete them, or leave them; either is safe.
4. ~~**Re-judge the other 29 golden rows.** Only the two recency rows were re-judged (PR #41). The
   re-judging procedure and the read-only range check are in `tests/eval/baselines/README.md`.~~
   **Done 2026-10-22** by PR #50. 15 rows gained judgments (+67), 14 are unchanged and none was
   removed. `ci.json` was re-recorded against 2,068 documents, and the means went
   0.3253/0.4310/0.7581 → 0.3191/0.4645/0.7176 (nDCG@5/MRR/Recall@20). See README (1),
   "Re-judged 2026-10-22", and `docs/plans/2026-10-22-golden-corpus-rejudge.md`.
5. **Optional: reboot to release swap.** macOS swap grew to roughly 30 GB under parallel-agent memory
   pressure on 2026-10-21. Re-check with `sysctl vm.swapusage`.
6. **Review disk held by other projects:** about 7 GB of reclaimable Docker volumes and 54 GB of Ollama
   models, both measured on 2026-10-21. Re-derive with `docker system df -v` and `ollama list`.
7. **The July `.claude/worktrees/agent-*` worktrees.** On 2026-10-21 there were four, and three held
   unmerged work: `d0377a2`, `4a507c4` and `36c8a58`, which are 1, 5 and 3 commits outside `c9f6408`
   respectively. The fourth, `e813140`, is merged. Re-derive with `git worktree list` and
   `git rev-list --count c9f6408..<sha>`.
8. **Keep `.claude/worktrees/daemon-src`.** After the `c9f6408` reinstall it is still the uv receipt's
   source ("Done on 2026-10-21" item 9; check
   `~/.local/share/uv/tools/secondbrain-py/uv-receipt.toml`). Delete it only after the tool has been
   reinstalled from somewhere else.

### Follow-ups still open (as of 2026-10-21)

- ~~`brain backup`'s `tar_vault` (`src/brain/backup/create.py`) archives the whole vault with no skip
  list, unlike vault sync and watch, which skip `.quartz/` (R9-8). Until `<vault>/.quartz` is deleted, a
  full backup carries about 11 GB of dead build output (measured 2026-10-21).~~ **Fixed 2026-10-22**
  by PR #47 (`fix/backup-skip-quartz`). `tar_vault` now prunes the top-level
  `<vault>/.quartz/` and nothing else, and reports the skip. Rulings B1–B10 are in
  `docs/plans/2026-10-22-backup-skip-quartz.md`. `<vault>/.quartz` itself is still on disk until the
  user deletes it.
- `graph_runner` has no `degraded` tolerance. This is deliberate, and the reason is in its module
  docstring (PR #37 left it out on purpose).
- ~~Watcher burst/stale-event tests that `sleep` and then assert ABSENCE can false-PASS under load.~~
  *[Tests FIXED 2026-10-22, on branch `test/vault-watcher-absence-sync`, in the commit that wrote
  this note. The status covers the tests only; the production follow-up is the next bullet.]* The vault-watcher half had seven such tests in `tests/test_vault_watcher.py`. Each now
  asserts absence only on proof the watcher got past the point in question. The four
  debounce/filter tests use a one-hour debounce, so `pending_timers` is exact right after
  `inject()`, and the burst tests join every superseded timer. The three stale-event tests drain
  `_worker_loop` synchronously. `src/brain/vault/watch.py` is unchanged. Inventory, mutation
  proofs and the old tests' demonstrated false passes are in
  `docs/plans/2026-10-22-vault-watcher-absence-tests.md`. The build-watcher half is moot, because
  `tests/wiki/test_build_watcher.py` was deleted with the wiki at R3 (`885b5a6`).
- *[Both FIXED 2026-10-22 by PR #49 (`fix/vault-watcher-fire-race`). `_fire` now acts only
  on its own entry and enqueues under the lock, and the comment is corrected. Plan and mutation
  proofs: `docs/plans/2026-10-22-vault-watcher-fire-race.md`.]* Two production items in
  `src/brain/vault/watch.py`, found while reviewing those tests. Both were for the follow-up watcher
  PR:
  1. **A race in `_fire`** (lines 636-638 at `58e27c6`). It pops `state.pending_timers[path]` by
     path, with no identity check. A superseded timer whose `_fire` has already started can pop its
     successor's entry. That yields two jobs, and the orphaned successor can enqueue after the
     shutdown sentinel. The fixed tests use a one-hour debounce, so they never reach that window.
  2. **A known-false comment** at lines 800-803. It says an unguarded `sync_one_file` raises
     `FileNotFoundError` and bumps `state.errors`. In fact it reports "file does not exist" and
     returns, so `processed` is what advances. It was left unchanged so the test PR stays test-only.
- Found while fixing the `_fire` race:
  1. ~~**The shutdown drain enqueued every pending path as an `upsert`.**~~ *[FIXED by PR #49, in its
     review-fix commit: each `pending_timers` entry is now `(timer, job)`, and the drain enqueues
     the real job.]* `pending_timers` used to keep the timer, not the job.
     - A `delete` still pending at shutdown became a stale upsert. That was skipped, so the row
       survived until a pruning sync.
     - A pending `move` upserted its vanished source, so the destination was not synced by that run.
       The next startup sync treated the destination as a new file. The in-place move was lost (the
       document id and its incoming backlinks), and the source row survived until a pruning sync.
     - The PR #49 audit found that the identity check would have made the `delete` case a
       regression for a delete whose `_fire` lost the lock to the drain.
     - **The overflow flush had the same defect, also FIXED by PR #49** (its overflow-flush commit,
       from the review of `a2b8af7`). The flush enqueued only a full-vault upsert, and that runs
       with `prune=False`, so flushed deletes and moves were dropped. A delete whose `_fire` lost
       to the flush regressed the same way. The flush now enqueues every flushed delete/move job,
       plus the overflowing call's own, BEFORE the full sync.
     - The plan (`docs/plans/2026-10-22-vault-watcher-fire-race.md`) now lists all four removers
       of `pending_timers` entries as a closed list, each with its proof. The one known gap was
       item 3, now closed by PR #51; `docs/plans/2026-10-22-vault-watcher-shutdown-mixed-actions.md`
       carries the current list.
  2. ~~**A job can land after the sentinel through the observer.**~~ *[FIXED by PR #51
     (`fix/vault-watcher-shutdown-and-mixed-actions`).]* `run_watcher` gives `observer.join` 5s. If
     the observer thread outlived that, an event it delivered after the drain armed a timer that
     nobody drained. The drain now sets `_WatcherState.closed` under the lock. A closed watcher arms
     no timer and enqueues nothing, and logs the drop once. The next startup `sync_vault`
     reconciles the path, though a delete is pruned only with prune on.
  3. ~~**Debouncing is keyed by path, and the last event wins across actions.**~~ *[FIXED by PR
     #51.]* Example: a `move` keyed on its src, then the src recreated within `debounce_ms`,
     became an upsert of src, and the destination was not synced. A pending `move` superseded by a
     DIFFERENT job is now enqueued first, under the lock. Every other action pair still coalesces,
     the atomic-save delete+create collapse included. The pair table is in
     `docs/plans/2026-10-22-vault-watcher-shutdown-mixed-actions.md`.
  4. ~~**A coalesced move can carry the wrong document's row.**~~ *[FIXED by PR #52
     (`fix/vault-watcher-move-identity`).]* A pending `upsert` or `delete` of P is coalesced into
     a later `move` P → Q. If P had been re-populated by a DIFFERENT document (another frontmatter
     id, B) before the rename, `_handle_move` renamed the previous document's row (A) to Q. Syncing
     Q then hit `documents_vault_path_idx`.
     - **The fix:** `_handle_move` now compares Q's frontmatter id with the id of the vault row at
       P, as UUIDs (`brain/vault/move_identity.py`). If they differ, A's row is deleted and Q
       syncs by its own id. The same-id rename is unchanged.
     - **Ruling:** an id-less Q keeps the in-place rename, because `_sync_one`'s recovery branch
       already identifies an id-less file by its path (`docs/plans/2026-10-22-vault-watcher-move-identity.md`).
     - **Residue, still open and pre-existing:** a brand-new, never-synced, id-less file that
       replaces P and is renamed within one debounce window still adopts A's id. Nothing
       distinguishes it from A's own file after a failed id write-back.
  5. ~~**The vault watcher was blind for hours after a restart (production, 2026-10-22).**~~
     *[FIXED by PR #54 (`fix/vault-watcher-startup-blind`).]* The startup `sync_vault`'s graph pass
     (step 7) ran Ollama concept extraction for each new, un-watermarked Krisp document, at about
     8-10 min each. The observer and worker start only after the startup sync, so edits went
     unwatched for about 6h.
     - **The fix:** every graph reconcile and remove the watcher issues goes through one
       `GraphLane` thread with its own connection (`brain/vault/graph_lane.py`). The startup pass
       only enqueues, and the watcher is live as soon as the file reconcile is done.
     - **Unchanged:** there is still one graph writer per process, and `sync_vault` and the
       `brain vault sync` CLI are still synchronous.
     - **Plan:** `docs/plans/2026-10-22-vault-watcher-startup-blind.md`.
     - **Side effect:** the graph now lags while a large startup backlog drains. It is
       best-effort and recomputable with `brain graphrag build`.

## Status on 2026-10-16: DONE except items that need the user

*(History. Superseded by "Closed out 2026-10-21" above.)*

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
   *(2026-10-21: the build daemon's code was deleted at R3 (`9657733`). The stale `brain-build.pid` was
   deleted the same day.)*
   **Trap:** running `brain-up` or `brain-install-launchd` from a STALE install re-creates
   `com.brain.build`. On 2026-10-16 the main checkout's editable `.venv` was at `6a82db2` (whose
   `pyproject.toml` already says 0.3.0), and the pre-upgrade uv tool (v0.2.1) was staler still.
2. **The daemons and the uv tool `secondbrain-py` were upgraded from v0.2.1 to 0.3.0.** The install came
   from the detached worktree `.claude/worktrees/daemon-src` at `8900725`. **Do not delete that worktree**
   until the tool is reinstalled from somewhere else. On 2026-10-16 the uv receipt's source `directory`
   pointed at it; check `~/.local/share/uv/tools/secondbrain-py/uv-receipt.toml`.
   *(2026-10-21: reinstalled from it at `c9f6408`, so it is still the receipt's source. See "Done on 2026-10-21" item 9.)*
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
   *(2026-10-21: deleted; its tip was `a6c4acf`.)*
8. **Claude Desktop's `brain` MCP server was repointed on 2026-10-16 to launch the uv tool.** In
   `~/Library/Application Support/Claude/claude_desktop_config.json`, the `brain` server's `command` was
   repointed from the main checkout's `.venv/bin/brain-mcp`, which is editable, was at `6a82db2` and
   bypassed the Homebrew shims, to `~/.local/bin/brain-mcp`. On 2026-10-16 this was the only brain MCP
   config on the machine, and no Claude Code config launched `brain-mcp`. **To revert,** restore
   `claude_desktop_config.json.bak-20261016T161548` from the same directory.

## Still needs the user

*(History. The status of each item on 2026-10-21 is in the table in "Closed out 2026-10-21" above.)*

1. *[DONE 2026-10-21: PR #43, `cf0d209`.]* **Phase 9 (delete the wiki).** Gated on the user's word (spec Q12) and RA-8.
2. *[DONE: the two recency rows were re-judged 2026-10-21 in PR #41 (`eec002f`), and the other 29 on 2026-10-22 in PR #50.]* **Golden corpus.** As measured in `tests/eval/baselines/README.md` (`3612d0f`, 2026-10-15), the two
   pinned recency rows can reach only 10 of 28 and 0 of 5 of their judged docs. Re-judge them by hand,
   and re-derive the counts with the read-only SQL check in that README.
3. *[DONE 2026-10-21: lowered to 8192; backup `~/.brain/backups/launchagents_20261021T164217Z/`.]* **Optional: lower `OLLAMA_CONTEXT_LENGTH`** in `~/Library/LaunchAgents/homebrew.mxcl.ollama.plist`, so
   other clients stop forcing 65536-context reloads. Brew may regenerate that plist.
4. *[DONE 2026-10-21: relaunched after the `c9f6408` reinstall.]* **Restart Claude Desktop,** so its `brain-mcp` respawns on the upgraded install (see Done #8). Restart
   it again after the coordinator's planned reinstall (see "In flight").
5. *[DONE 2026-10-21: `ollama ps` showed 8192 and 2048.]* **After the next ingest,** `ollama ps` should show the chat model (`llama3.1:8b`) at CONTEXT 8192
   and, once the reinstall picks up PR #36, the embed model at 2048.
6. *[Remains the user's on 2026-10-21. See "Remains the user's" items 2, 5 and 6.]* **Host disk:** on 2026-10-16 the data volume was nearly full; check with
   `df -h /System/Volumes/Data`.
7. *[DONE 2026-10-21: fast-forwarded to master, and the untracked copy moved to `~/.brain/backups/`.]* **The main checkout:** on 2026-10-16 it was far behind master (re-derive with
   `git -C <main> rev-list --count HEAD..second-brain/master`), belonged to another session, and held an
   UNTRACKED copy of this file. Once the PR that commits this file merges, that copy will block a
   `git pull`, so delete or move it first.

## In flight on 2026-10-16

*(History. Nothing below is in flight on 2026-10-21.)*

- *[DONE: reinstalled from `6948c6f` on 2026-10-16, and again from `c9f6408` (Python 3.12) on 2026-10-21, after deleting the stale `build/` and egg-info. See "Done on 2026-10-21" item 9. `daemon-src` is still the receipt's source, so do not delete it yet.]* **Planned: the coordinator reinstalls the uv tool from the final master after the open PRs merge,
  and then `daemon-src` can be deleted.** As of 2026-10-16, the install that the daemons and Claude
  Desktop run was built at `8900725`, so PR #36 and later merges were not live in either. The
  reinstall moves the uv receipt's source off `daemon-src`, which is what makes that worktree safe to
  delete. Restarting Claude Desktop afterwards stays a user item (see above).
- *[MERGED 2026-10-16: `a967685`.]* PR #37 (open on 2026-10-16, branch `fix/eval-degraded-all-embedders`): `brain eval` degrades
  transient embed failures on every backend and aborts on permanent ones. It covers the Ollama/Voyage
  clauses of the eval-degraded follow-up below, not the `graph_runner` one.

## Recorded follow-ups

*(History. What is still open on 2026-10-21 is under "Follow-ups still open" above.)*

As of 2026-10-16, not started, except: the first item (fixed by this PR); the `qwen3` item (addressed
by PR #36); and the Ollama/Voyage eval-degraded item (in flight as PR #37). The `graph_runner` item is
left out of #37 on purpose.

- *[MERGED as PR #35, `210d288`. `tests/wiki/` was then deleted at R3 (`885b5a6`).]* **Fixed by the PR that commits this doc** (branch `test/build-watcher-wait-backstop`, plan
  `docs/plans/2026-10-16-build-watcher-wait-backstop.md`). `tests/wiki/test_build_watcher.py` had its own
  `_wait_for(timeout=2.0)` and 9 short-timeout call sites, the same latency-budget flake class that PR #31
  fixed for the vault watcher. This PR moves the helper to `tests/thread_wait.py` (a 10 s backstop), and one
  guard in `tests/test_thread_wait.py` covers every importer. That guard also found a site PR #31's
  per-file guard could not see: `tests/test_cli_vault_watch.py` imported the helper through the test
  module and passed `timeout=5.0`.
- *[Vault-watcher half FIXED 2026-10-22 on `test/vault-watcher-absence-sync`: see "Follow-ups
  still open" above and `docs/plans/2026-10-22-vault-watcher-absence-tests.md`. The build-watcher
  half is moot after R3.]* Watcher burst/stale-event tests that `sleep` then assert ABSENCE can false-PASS under load. The
  build watcher's `test_ignores_*` and coalescing tests are in the same class (listed in the
  2026-10-16 plan).
- ~~`qwen3` (non-default embedder) still inherits the server's 65536 context for embeddings.~~ Addressed
  by PR #36 (`69a780d`), which makes every Ollama embed request send its own `num_ctx`.
- *[DONE: PR #37, `a967685`.]* At `69a780d`, only `OllamaEmbedError` marks an eval row degraded, and Voyage errors propagate. On
  2026-10-16 these two clauses were in flight as PR #37.
- *[Still open on 2026-10-21, by design.]* At `69a780d`, `graph_runner` has no equivalent `degraded` tolerance. This is deliberately out of scope of PR #37, and
  the reason is in `graph_runner`'s module docstring on PR #37's branch.
- *[Remains the user's on 2026-10-21: 3 of 4 unmerged. See "Remains the user's" item 7.]* On 2026-10-16, old `.claude/worktrees/agent-*` worktrees (from July) held unmerged work. They were
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
