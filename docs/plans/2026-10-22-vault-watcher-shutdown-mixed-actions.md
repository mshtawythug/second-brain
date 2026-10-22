# Vault watcher: no event after shutdown, no lost move: plan and proof

> **For agentic workers:** REQUIRED SUB-SKILL: Use team-driven-development to implement this plan task-by-task.

Branch `fix/vault-watcher-shutdown-and-mixed-actions`, base `second-brain/master` @ `2ae819c`
(PR #49 merged). This closes the two vault-watcher follow-ups that
`docs/handoff/2026-10-13-merge-all-handoff.md` still listed as open (items 2 and 3
under "Found while fixing the `_fire` race"). The previous plan,
`docs/plans/2026-10-22-vault-watcher-fire-race.md`, holds the closed remover list
this one extends.

## (A) An event after the drain enqueues behind the sentinel

`run_watcher` gives `observer.join` 5s. If the observer thread outlives that, an
event it delivers AFTER `_drain_pending_timers` arms a timer that nobody drains.
When the timer fires, `_fire` finds its own entry and enqueues behind the `None`
sentinel, where no worker reads it. The start-failure fallback and the overflow
flush would enqueue behind it directly.

**Fix:**

- `_WatcherState.closed: bool`, set by `_drain_pending_timers` under
  `state.lock`, in the same critical section that clears `pending_timers`.
- `_schedule_debounced` checks `closed` first, under the lock. A closed watcher
  builds no timer and enqueues nothing. It counts the drop (`dropped_after_close`)
  and logs once, on the first drop, without paths or content.
- `_fire` needs no separate check, and gets none, because it would be dead code.
  The invariant is: after `closed`, `pending_timers` is empty and stays empty.
  The drain cleared it, and the only writer refuses once closed. So every
  surviving `_fire` fails its identity check and returns. Test A3 pins this.

**What happens to a dropped event: verified by reading the startup path.**
`run_watcher` runs `sync_vault(... prune=config.prune ...)` before it starts
the observer. `sync_vault` reconciles every `.md` file by frontmatter `id`
(`src/brain/vault/sync.py`, module docstring and steps 1-3). On the next start:

- an **upsert** is re-read;
- a **move** is re-found by its `id` at its new path;
- a **delete** is pruned only with `prune=True`. Otherwise `sync_vault` warns
  about the vault row whose path it did not see, and that row survives until a
  pruning sync.

So "the next startup sync covers it" is true for upserts and moves, and true
for deletes only under `--prune`. That is the same coverage as any event that
arrives while the watcher is down, which is the case the startup sync exists
for.

## (B) Last event wins across actions, so a pending move is lost

Debouncing is keyed by path, and a move is keyed on its src. Any later event on
the same src superseded the pending job: last-wins. For a move, that loses the
in-place rename, meaning the document id and every incoming backlink. Example:
a move from P, then P recreated within `debounce_ms`, became an upsert of P. The
destination was not synced by that run, and the next startup sync walked it as
a new file.

### The action-pair table

Pending job at P, then a new event at P. The new job is always the one that
stays pending, and the debounce restarts. "Flush" means the pending job is
enqueued FIRST, under the lock, before the new one is registered, so both are
delivered in order.

| Pending → new | Behaviour | Why |
|---|---|---|
| upsert → upsert | coalesce | The debounce burst. Unchanged. |
| upsert → delete | coalesce (the delete wins) | The file is gone. Delivering the upsert first would only be a stale upsert the worker skips. |
| upsert → move | coalesce (the move wins) | `_handle_move` syncs the destination, so it carries the edit's content: nothing is lost when P still holds the same document. **Known residue, not introduced or widened here:** if P was re-populated by a DIFFERENT document (another frontmatter id, B) and then renamed P → Q, `_handle_move` renames the previous document's row (A) to Q. Syncing Q then either hits `documents_vault_path_idx` (Q carries id B) or adopts A's id (Q is id-less). Coalescing is still the right default, because it keeps A's backlinks in the common same-id case. The case predates this PR via upsert → move, and is an open handoff follow-up. |
| delete → delete | coalesce | A same-action duplicate. |
| delete → upsert | coalesce (the upsert wins) | **The atomic-save case** (an editor's delete+create, `os.replace`). The file exists, so the upsert reflects disk. Flushing the delete would `DELETE` the row, which cascades away its incoming backlinks, before re-inserting. The worker's stale-delete re-stat makes even a spurious delete safe, so keeping the last-wins collapse is both correct and the existing behaviour. |
| delete → move | coalesce (the move wins) | The same principle as delete → upsert: a later event proves the path was re-populated. In the common case (an atomic save that keeps the frontmatter id), the move then renames that document's row in place. **Known residue, not introduced or widened here:** if P was re-populated by a DIFFERENT document (another frontmatter id, B) and then renamed P → Q, `_handle_move` renames the previous document's row (A) to Q. Syncing Q then either hits `documents_vault_path_idx` (Q carries id B) or adopts A's id (Q is id-less). Coalescing is still the right default, because it keeps A's backlinks in the common same-id case. The case predates this PR via upsert → move, and is an open handoff follow-up. |
| move → move, same destination | coalesce | A duplicate report of one rename. |
| move → move, different destination | **flush** the first move | A second move from P can only follow if P was re-populated in between. Two different files were renamed, and both renames must be applied, in order. |
| move → upsert | **flush** the move | The bug. P was recreated after the rename. The move must update `vault_path` before P's new file is upserted. |
| move → delete | **flush** the move | The move is applied first (the row is renamed to the destination). The delete of P then finds no row at P and is a no-op, instead of deleting the moved document's row. |

**The rule, in one condition:** a pending `move` superseded by a different job
(`existing_job != job`, the dataclass compares action, path and dest) is
enqueued first. A pending `delete` or `upsert` is always coalesced, as before.

**Where it applies.** Every supersession pop goes through one helper,
`_supersede_pending` (called with `state.lock` held):

- the normal path;
- the start-failure fallback, where the order is the pending move first, then
  the new job;
- the overflow branch. That pop is unreachable there: `len(pending_timers)`
  never exceeds `_MAX_PENDING`, so an event on P can only overflow when P has no
  entry. The helper still keeps it correct.

## Closed remover list (extends the fire-race plan)

Re-derived with `grep -n "pending_timers" src/brain/vault/watch.py`.

- **One writer:** the store in `_schedule_debounced`, now unreachable once
  `closed`.
- **Removers:**

| # | Remover | Proof that the entry's job is not lost | Pinned by |
|---|---|---|---|
| 1 | `_fire`: `del`, identity holds | Enqueues its own job in the same critical section | fire-race A, C |
| 2 | supersession: `_supersede_pending` (normal path) | A pending move that differs from the new job is enqueued first. Every other pair is coalesced per the table, where the new job reflects the path's current state | B1-B6 |
| 2b | supersession in the `start()` fallback: also `_supersede_pending` | The same rule as row 2, then the new job is enqueued directly | fire-race Ma, Mb; B7 |
| 3 | overflow `.clear()` | Unchanged from PR #49 | fire-race D, H-K2 |
| 4 | drain `.clear()` | Enqueues each entry's job before the sentinel, and sets `closed` in the same critical section, so no entry can ever exist again | fire-race B, E-G; A1-A3 |

## Tests (TDD, manual-timer rig, RED first at `2ae819c`)

All in `tests/test_vault_watch_fire_race.py`.

- **A1** `test_event_after_shutdown_arms_no_timer_and_enqueues_nothing`: after
  the drain, a new event builds no timer, leaves nothing pending, and puts
  nothing behind the sentinel.
- **A2** `test_events_after_shutdown_are_logged_once`: two drops give one log
  record.
- **A3** `test_timer_fired_after_shutdown_enqueues_nothing`: a timer from before
  the drain fires after it and adds nothing behind the sentinel.
- **B1** move → upsert, **B2** move → delete, **B3** move → move with a different
  dest: the pending move is enqueued at once, and the new job stays pending.
  Each then fires its successor and asserts that the NEW job is the one
  delivered: `[upsert src]`, `[delete src]`, and `[move src→second]`.
- **B4** move → move, same dest: coalesced to a single move.
- **B5** the coalescing pairs, parametrized: upsert → upsert / delete / move, and
  delete → delete / upsert / move. Nothing is enqueued at supersession, and only
  the new job is delivered.
- **B6** delete → upsert, the atomic save: pinned separately, so that a mutant
  flushing deletes reds a test named for the case it breaks.
- **B7** a pending move, then a `start()` failure on an upsert of P: the queue is
  `[move, upsert P]`, in order.

## Ceiling

`watch.py` is in CLAUDE.md's over-800 table. Its row's last hop is descriptive
("the PR #49 exception-safety commit"). That commit is `76c9c2c`, the last to
touch the file, at 1,132 lines. `572d50c` did not touch it. This change
promotes that hop to "1,132 (`76c9c2c`)", adds a descriptive hop for itself,
and records the growth inline, in the module docstring, and in the row.

## Risks

- **Changed semantics for move → * pairs.** Only flushes are added. A flushed
  move is a job the worker already handles (`_handle_move`, including a
  vanished destination). No coalescing pair changes.
- **A flushed move is delivered ahead of other paths' still-pending jobs.** It
  is enqueued at the supersession, while other paths' timers are still waiting
  out their debounce. That is acceptable, for three reasons:
  - The watcher never promised cross-path ordering. Every path debounces
    independently, so jobs for different paths already land in the order their
    timers happen to fire.
  - The move is a fact that has already happened on disk. Delivering it early
    only shortens its debounce: it is not coalesced with anything, because the
    next job on that path is a different one.
  - `_handle_move` reads the destination's current state when it runs, so an
    earlier start cannot observe a stale file.
- **`closed` read under the lock only,** by `_schedule_debounced`. Its sole
  writer is the drain, inside the same lock.
- **Out of scope, recorded.** If the observer outlives the join, `observer.schedule`
  calls from `_maybe_watch_new_directory` can still add watches after shutdown.
  Their events are dropped by `closed`, which is harmless.

## Proof

### TDD: RED at `2ae819c`, then GREEN

The new tests were written first and run against the unmodified `2ae819c` code.
Seven went RED, each at its targeted assertion:

- **A1:** "a closed watcher armed a debounce timer"
- **A2:** "expected one drop log, got 0"
- **A3:** "a timer fired after shutdown enqueued behind the sentinel"
  - The first draft of A3 passed, because it never fired the timer armed after
    shutdown. It now fires every timer the rig ever built.
- **B1:** "...dropped when an upsert of its src superseded it"
- **B2:** "...dropped when a delete of its src superseded it"
- **B3:** "the first move was dropped..."
- **B7:** "the fallback dropped the pending move, or delivered it out of order"

The coalescing tests (B4, the six B5 cases, B6) passed at `2ae819c`, as
intended. They pin behaviour that must NOT change, and the mutants below show
that each of them can fail.

With `closed` and `_supersede_pending`, all 32 race and timer tests pass. They
passed 5 runs in a row (32 passed each).

### Mutation matrix (five clauses), in a `git archive` copy only

The live worktree was never mutated. The snapshot of the staged tree came from
`git stash create` (`5f9eedc`), archived and extracted. Per mutant, the harness
does the following:

- **(b) It applies each edit** with its `str.count` asserted to the declared 1.
- **It runs both test files once,** reading per-test failures from JUnit XML.
- **(c) and (d) It passes the mutant only if** the set of failing tests EQUALS
  the predicted set, and every failure message contains its predicted text.
- **(a) A control run** (no mutation) has 0 failures.
- **The checker can fail.** Two negative controls are judged against a real run
  (MV's failures): a wrong expected text, and a wrong expected set. Both are
  rejected.
- **The archive** was restored byte-identical.

**The run of record: 21 mutants, all OK.** A first run judged MD not OK. The
right test (L) did fail, and at its targeted statement
(`assert rig.queued() == [_Job(action="delete", ...), None]`). But the
predicted text had the wrong format for a bare assert. The prediction was
corrected to the JUnit diff text (`At index 0 diff: _Job(action='upsert'`),
and the whole matrix was re-run.

| Mutant | Edit | Failing tests (exactly) |
|---|---|---|
| MQ | `if state.closed:` changed to `if False:` | A1, A2, A3 |
| MR | the drain does not set `closed` | A1, A2, A3 |
| MS | log every post-shutdown drop | A2 ("got 2") |
| MT | no move flush (`if False:`) | B1, B2, B3, B7 |
| MU | flush pending deletes too | B5[delete-upsert], B5[delete-move], B6 (atomic save) |
| MV | flush a pending move even when identical | B4 |
| MW | the start() fallback pops silently instead of `_supersede_pending` | B7, fire-race Ma (upsert timer left armed) |
| M0 | `a6a76fe`'s `_fire` | fire-race A, B, C, D, G, I, plus A3 |
| MA | no identity check | fire-race A, B, D, G, I, plus A3 |
| MB | enqueue outside the lock | fire-race C |
| MC | the losing fire still enqueues | fire-race A, B, D, G, I, plus A3 |
| MD | the drain enqueues an upsert | fire-race E, F, G, L |
| MF | overflow flush reverted | fire-race H, I, J, K, K2 |
| MG | the flush runs after the full sync | fire-race H, I, J, K, K2 |
| MH | the overflowing call's job is dropped | fire-race K |
| MI | a factory failure pops the entry | fire-race L |
| MN | no start() fallback | fire-race Ma, Mb, plus B7 |
| MP | the fallback drops the job | fire-race Ma, Mb, plus B7 |
| MJ | no scheduling guard | fire-race N |
| OVFSTART | the overflowing call builds a timer | fire-race K, K2 |
| MX | the move is flushed, but the OLD move is stored (and fired) as the successor's job | B1, B2, B3 (the NEW job not delivered) |
| ME | no daemon flag | timer T |

**Each new claim has its own killing mutant (clause e):**

- "a closed watcher arms nothing": MQ / MR → A1;
- "the drop is logged once": MS → A2;
- "no surviving timer enqueues after shutdown": MQ / MR → A3;
- "a superseded move is delivered first": MT → B1, B2, B3;
- "even through the start() fallback": MT / MW → B7;
- "deletes still coalesce, the atomic save included": MU → B5, B6;
- "identical moves still coalesce": MV → B4;
- "the successor delivers the NEW job": MX → B1, B2, B3.

**MX was added in review (of `c00bba0`).** B2 and B3 originally asserted only
the flush. Run against an archive of `c00bba0`, with its own tests, MX was
killed only by B1, and B2 and B3 survived it. With the successor assertions
added, MX reds all three. The run of record is now 22 mutants, all OK. It was
re-run on a `git stash create` snapshot (`eefb528`) of this round's tree, with a
clean control, both negative controls rejected, and the archive restored
byte-identical.

**Correction, recorded later (PR #52 audit F3).** This matrix's clause (d) check matched each prediction against the JUnit failure message AND its traceback text. The traceback also lists the test's earlier source lines, so a prediction naming an assertion that had passed could still match. In PR #52 the judge was narrowed to the failure message only, and every mutant above was re-run under it (`docs/plans/2026-10-22-vault-watcher-move-identity.md`, Proof). Every prediction in this table still holds, so no claim here was false; it was only checked loosely.

**Every earlier claim still holds against the new code:** M0 to ME re-ran with
their earlier kills intact. The A3 additions under M0, MA and MC are expected:
those mutants let a stale `_fire` enqueue, so a late fire after shutdown now
lands behind the sentinel. The B7 additions under MN and MP are the
start-failure path with a pending move.
