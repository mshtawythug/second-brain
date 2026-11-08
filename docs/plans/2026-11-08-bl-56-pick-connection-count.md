# BL-56: the `--pick` connection-count test waits for the count to settle

> **For agentic workers:** REQUIRED SUB-SKILL: Use team-driven-development to implement this plan task-by-task.

**Goal:** stop `tests/test_cli_search_pick.py::test_no_connection_from_the_search_is_held_while_a_human_chooses`
from failing at random (`assert [3] == [2]`, twice in about six CI runs, including master's push CI at
`11c12333`), without weakening what it proves: no connection from the search is held while fzf waits
for a human. Test-only; no `src/` change.

## Cause

`brain search --pick` closes its search connection (the `with connect(...)` block opened at
`cli_search.py:401`) just before `run_pick` starts fzf (`:541-544`). A client-side `close()` returns
before the server backend leaves `pg_stat_activity`, and the test counted once, from inside the fzf
stub, so it could see `before + 1`.

**Measured** (2026-11-08, a sibling test database on 5434, 200 iterations): opening a connection,
running one query, closing it with `with psycopg.connect(...)`, then taking one `pg_stat_activity`
snapshot from an observer showed the closed connection still listed in **12 of 200** snapshots,
every time as `('client backend', 'idle')`. So filtering to client backends does not fix it alone;
waiting does. An autovacuum worker on the test database would be a second, rarer +1, which the filter
does remove.

## Change

- `count_sql` counts `backend_type = 'client backend'` only.
- The fzf stub re-counts through `tests.thread_wait.wait_for` until the count equals `before`.
  `wait_for` is the repo's one polling helper (10 ms polls, a 10 s backstop that
  `tests/test_thread_wait.py` forbids lowering per call site), so no private loop and no per-site
  timeout. A passing run pays one or two polls.
- When the backstop expires, `wait_for` raises `AssertionError`; the stub suppresses exactly that type
  and records the last count, so the CLI still exits 0 and the test's own `during == [before]` fails
  with the real number rather than an exit-code mismatch.
- No CHANGELOG line: test-only fixes carry none here (`77c4fc2a`, `6f3855c6`, `7ce45a27`).

## Evidence

- Mutation proof, in a git-archive copy deleted afterwards (run on 2026-11-08 at `6d4307af`):
  `cli_search.py` wraps `run_pick` in `with connect(cfg.database_url) as _held:` plus one
  `SELECT 1`, so a second connection is really open across the pick. The fixed test goes **red**
  with `assert [3] == [2]` after the 10 s backstop (13.5 s run); the old test is red on it too; with
  the mutation removed the fixed test is **green** (1.5 s). A first attempt that called `connect()`
  without `with` opened nothing (`db.connect` is a `@contextmanager`) and so survived; it was
  discarded as a broken mutation, not evidence.
- The 20-run before/after loops were skipped: free disk was 6 GiB, under the 7 GiB set for them.
- Targeted tests: `test_cli_search_pick`, `test_thread_wait`, `test_log_privacy_rules`,
  `test_log_privacy`. CI is the full-suite evidence.
