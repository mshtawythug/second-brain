# Session handoff — wiki→`brain ui` phase 5 (sidebar graph + related panel) (2026-10-09)

Path convention: `<repo>` = the main checkout (where `core.hooksPath` points). Remote is
**`second-brain`**, not `origin`. Every count is SHA-bound or replaced by the command that
derives it.

> **RESOLVED 2026-10-09 (later the same day).** Disk freed (22 GB free) and Docker Desktop
> restarted via `docker desktop restart` (`osascript quit app "Docker Desktop"` silently does
> nothing — the app is named "Docker"). The rule-14 loop then EXITED on `f611e88`: code review
> APPROVED and completion audit AUDIT PASSED (full suite 8847 passed / 2 skipped, 94.36%
> coverage; CI browser command 167 passed; 15 mutation proofs). The branch was pushed and a PR
> opened on the user's go. §0 and §2 below are kept as the record of how it got there.

---

## 0. READ THIS FIRST — the machine is not healthy

**The host disk is full.** At the time of writing `df -h /System/Volumes/Data` showed 100% used
with **2.5 GB free and falling** (306 MB at the worst point, 3.5 GB after cleanup, 2.5 GB a day
later). The Docker Desktop VM hit I/O errors when the disk filled and **has not recovered**:

| | State |
|---|---|
| Production Postgres `:55432` | still accepts connections and answers queries, but its container health check fails with `exec /bin/sh: input/output error` |
| AGE test Postgres `:5434` | **unreachable**; `docker compose -f docker-compose.age-test.yml up -d` fails reading a cached image layer (`input/output error`) |
| `docker ps` | still claims the test container is "Up (healthy)" — it is wrong; `docker exec` says it is not running |

**The cure is the user's to do:** free real disk space, then restart Docker Desktop. Do NOT
restart Docker yourself — it restarts production and affects other sessions on this machine.
Production's data is a host bind mount (`./data/postgres`), so it survives the restart.
Re-check with:

    df -h /System/Volumes/Data | tail -1
    pg_isready -h localhost -p 5434; pg_isready -h localhost -p 55432
    docker inspect second-brain-postgres --format '{{.State.Health.Status}}'

What this session freed: the `.venv` of the four phase-5 teammate worktrees (~1.9 GB). Docker's
disk image (`~/Library/Containers/com.docker.docker/Data/vms/0/data/Docker.raw`) holds ~71 GB in
use and is the largest single item found; what to delete is the user's decision.

---

## 1. STATE — described at `f611e88`

    git -C <repo> log --oneline -1                      # anything newer invalidates this section

| | |
|---|---|
| Branch | `feat/wiki-to-ui-phase5`, checked out in `<repo>` |
| Base | `121df23` (= `second-brain/master`) |
| Ahead of master | 28 commits (`git log --oneline second-brain/master..HEAD`) |
| Pushed | **No.** No upstream. Pushing or opening a PR needs the user's explicit go (CLAUDE.md rule 4) |
| Working tree | clean except: ` M docker-compose.yml` (**another session's edit** — a `restart: unless-stopped` policy, written 2026-09-29; never stage, restore or stash it) and ~81 untracked `docs/plans/*.md` (pre-existing local plan docs, un-ignored by PR #9; not ours) |
| Plan + closeout | `docs/plans/2026-10-08-wiki-to-ui-phase5.md` (committed; §7 is the closeout) |
| Spec | `docs/specs/2026-08-10-wiki-to-ui-consolidation-design.md` — §11 phase-5 row marked exited, Q2/Q3 answered, B-4 and B-17 closed |

### What phase 5 shipped

| Task | Final commit | What |
|---|---|---|
| T1 | `df9742f` | `GET /api/notes/{id}/graph` (`ui/routes_graph.py`) + pure layout (`ui/graph_layout.py`): depth-1 neighbourhood, server-side geometry (ring R=110 on 320, ≤24 neighbours from 12 o'clock clockwise), `truncated`, `corpus_linked`; confidential root under strict → 403 `graph_withheld`; root vanished mid-request → 404 `note_not_found` |
| T2 | `cc28ff9` | `GET /api/notes/{id}/related` (`ui/routes_related.py`), first consumer of `compute_related`; floor only from `cfg.vector_sim_floor` (closes spec B-4); strict unless titles AND bodies served; confidential root → 403 `related_withheld` before ranking; snippet capped with `snippet_truncated` |
| T3 | `eff393f` | sidebar local graph client (`static/js/graph.js`, `graph.css`); crowded rings (>4 neighbours) show labels only on hover/focus; tests split into `test_ui_browser_graph.py`, `test_ui_browser_graph_layout.py`, harness `tests/ui_graph_harness.py` |
| Integration | `d0e63d7` | T1–T3 merged; full suite **8848 passed, 94.36% coverage** (last full run with a working DB) |
| T4 | `d04d540` | related rail (`related.js`, `related.css`); one canonical inspector block order (`dom.js` `INSPECTOR_BLOCKS` / `placeInspectorBlock`); refresh after save (per-note revision in `store.js`); shared fetch helper `note_fetch.js` `perNoteFetch` |
| T5 | `0af7353` | docs closeout: spec, plan §7, README "Local web UI", `related.py` docstring + CLAUDE.md trail |
| Fix round 1 | `4b7d039`..`7536c4f` | 8 findings from the phase review + audit; the important one: client now mirrors each route's confidential gate and shows a one-line notice instead of a silent 403 |
| Fix round 2 | `0323298`, `f611e88` | fail-closed tests under degraded `/api/health`; plan R4 Why cell annotated |

---

## 2. THE ONE OPEN STEP — the rule-14 loop has not exited

CLAUDE.md rule 14: the phase is done only when the code reviewer says APPROVED **and** the
completion auditor says AUDIT PASSED, **on the same commit**.

| Gate | Verdict | On |
|---|---|---|
| Phase code review | **ZERO ISSUES FOUND — APPROVED** | `f611e88` |
| Completion audit | **provisional AUDIT FAILED** — static checks only; every static finding is since fixed | `0af7353` (stale) |

**What blocks the audit is only the test database.** Not yet run on this branch:
- the full default suite (`pytest`, ~22 min; coverage floor 85% in `addopts`)
- DB-backed tests in `tests/test_ui_routes_graph.py`, `tests/test_ui_routes_related.py`
- `tests/test_related_compute.py`, `tests/test_ci_workflow.py`
- ~40 DB-marked tests deselected by the `-m nodb` runs

No server *code* changed after `d0e63d7` except docstrings, but the rule requires a green run on
the audited commit.

### Resume procedure, once the user has freed disk and restarted Docker

    cd <repo> && git checkout feat/wiki-to-ui-phase5 && git log --oneline -1   # expect f611e88
    docker compose -f docker-compose.age-test.yml up -d && until pg_isready -h localhost -p 5434 -q; do sleep 2; done
    # then dispatch a FRESH completion auditor on HEAD (the old teammates do not survive a restart)

The auditor brief from this session is reusable: plan §1–§7, checklist (a)–(h) (completeness,
spec match, tests can fail, no regressions incl. the full suite + the exact CI browser command +
`tests/test_ci_workflow.py`, UI reachability, orphans/coverage of the new Python modules,
documentation truth, repo rules). It must report `AUDIT PASSED` / `AUDIT FAILED` with the SHA.
**If the audit finds anything, the fix commit invalidates the reviewer's approval too** — re-run both.

Test DB for the run: sibling DB `second_brain_t_evalb` on 5434, credentials **`brain:brain`**
(not `postgres:postgres`): `TEST_DATABASE_URL=postgresql://brain:brain@localhost:5434/second_brain_t_evalb`.
Never `DROP DATABASE` (crash-recovers the AGE image).

### After the loop exits
1. Ask the user for the go to push `feat/wiki-to-ui-phase5` and open a PR against master.
2. Watch CI (`ci`, `eval`, `benchmark`) on the PR.
3. Clean up the four phase-5 worktrees (their branches are merged into the phase branch; venvs already deleted): `.claude/worktrees/agent-af52e8f1736d6250f`, `agent-a284c388424aa9d03`, `agent-ad9e7b565993e8bbd`, `t4-related-panel` — `git worktree remove <path>` then `git branch -d <branch>`. **Do not touch the other `.claude/worktrees/agent-*`** — they predate this phase and one (`agent-a7e9ea932f5b31352`) has ~19 uncommitted changes.

---

## 3. OTHER OPEN ITEMS (carried, not phase 5)

- **`ci.json` eval baseline is overdue** — its recency window was documented as stable until
  ~2026-10-05. Procedure: `tests/eval/baselines/README.md` and the 2026-09-29 handoff §3.1. Needs prod
  Postgres + Ollama; do it after Docker is healthy.
- **Hermetic eval gate** — four-option decision, 2026-09-29 handoff §3.3.
- **Wiki→UI phases 7→8→9** (demote / dormant / delete) — phase 7 needs spec Q1 (what replaces LAN
  reading from the phone) answered by the user first; phase 9 is gated on the user's word.
- **Phase-5 known limits** (plan §7.5): touch screens can't preview crowded-ring labels; saving X does
  not refresh Y's blocks; move/rename/draft don't bump the revision; superseded cache keys retained;
  rim labels clear the inspector by only 5–6 px at 400 px; graph route reads the whole link corpus
  (measured median 174 ms / p95 269 ms on a synthetic 1,400-doc corpus; revisit if p95 > 500 ms).
- Old worktrees `<repo>-readme` and `<repo>-wiki-ui` (merged long ago) and stale remote branches from
  the 2026-09-29 handoff §3.5.

---

## 4. TRAPS THIS SESSION HIT

1. **Teammates spawned with Agent `isolation: "worktree"` run in-process — no tmux pane.** The user
   wants to watch them in tmux (`teammateMode: "tmux"` is set). Create the worktree yourself
   (`git worktree add .claude/worktrees/<name> -b <branch> <base>`) and spawn the teammate WITHOUT
   `isolation`. Resuming an old agent by message also brings it back in-process.
2. **A stopped teammate can leave a live mutation on disk** (`if False and strict and …` was found
   in a worktree after a restart). After any interruption, check worktrees for mutation markers
   before trusting a green run.
3. **`.github/workflows/ci.yml` lists browser test modules EXPLICITLY, in byte order — no glob.** A
   new `tests/test_ui_browser*.py` must be added there; `tests/test_ci_workflow.py` catches a miss
   (it stayed red for a day in this phase because nobody ran it).
4. **Test stubs must use the server's real geometry.** Rim-label clipping at phone width was
   invisible until the stub used the real ring (R=110, nodes at 3 and 9 o'clock).
5. **Test DB credentials are `brain:brain`.** Sibling DBs (`second_brain_t_evalb`, `_t_evalc`,
   `second_brain_test_co`) let teammates run in parallel; the lock is per database.
6. **Python 3.11 is not on PATH** — the repo venv uses a uv-managed interpreter:
   `~/.local/share/uv/python/cpython-3.11.15-macos-aarch64-none/bin/python3.11`.
7. **Another Claude session restarted Docker Desktop mid-session** (it ran `pkill` on the Docker app).
   If Docker vanishes, check `pgrep -fl Docker` for another session's restart before acting.
8. **`release_held` / held routes in `tests/ui_graph_harness.py`** let browser tests control the
   arrival order of fetches — use them instead of timed waits; this phase caught two flaky tests
   built on "wait for one, then assert all three".

---

## 5. STANDING RULES (unchanged)

No commit or push without the user's go — this file itself is uncommitted. Production Postgres
55432 is read-only for us. Never `DROP DATABASE`; never `pkill` by pattern. No PII in anything
checked in (public repo). Prove every guard can fail. Read exit codes from files, never through a pipe.
