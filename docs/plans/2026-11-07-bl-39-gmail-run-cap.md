# BL-39: a timeout and a stdout cap on `ingest/gmail._run`

> **For agentic workers:** REQUIRED SUB-SKILL: Use team-driven-development to implement this plan task-by-task.

Backlog row: BL-39 in `docs/plans/2026-10-23-backlog-reconciliation.md` (MEDIUM; found in the
RM-45 plan, D2, PR #117). Base: `second-brain/master` at `862c5cdc`. Branch
`fix/bl-39-gmail-run-cap`.

## Rulings (team-lead, 2026-11-07): approved, with changes

These override the decisions below where they differ.

- **D1:** reuse `real_gws_runner`, but an ingest module must not import `vault`. The runner lived
  in `vault/derived_links/gws.py`, so it moved to the neutral leaf `src/brain/gws.py` (`git mv`),
  and the old module re-exports it. `DirectoryRefreshError` already lived in the neutral
  `brain/errors.py`. Tests that patched `brain.vault.derived_links.gws.shutil.which` /
  `.subprocess.run` now patch `brain.gws.…`, the module the code lives in. A re-export module
  importing `shutil` just to keep a patch string resolving would be dead code.
- **D3:** 48 MiB, with the 25 MB × 4/3 evidence beside the constant.
- **D5:** per-thread failure stays, plus a breaker: three CONSECUTIVE timeouts (timeouts only)
  stop the run with one class-only line and exit 1, after the counts so far. To tell a timeout
  apart without matching message text, `real_gws_runner` raises `brain.errors.GwsTimeoutError`
  (a `DirectoryRefreshError` subclass, so every existing catch holds), and `gmail._run`
  translates it to `GmailTimeoutError` (a `GmailError` subclass). The rule is the leaf
  `src/brain/ingest/gmail_timeouts.py` (`ConsecutiveTimeouts`); a read that returned, or another
  failure, resets the count. The dry-run report's per-thread reads are not covered (it is a
  separate helper that swallows each failure); that is recorded as a residual.
- **D8:** include the catch in `cli_ingest.py`: a failed `list_messages` is one class-only line and
  exit 1. That growth and the breaker's calls are recorded inline, in the module docstring, and as
  a `docs/ceiling-table.md` hop (with RM-45's descriptive hop promoted to `120ffc9c`).
- D2, D4, D6, D7, D9: as written. `"brain.gws"` also joins `ENTRY_MODULES`.

New tests beyond T1–T9: `tests/test_gmail_timeouts.py` (nodb: the rule's pairs) and
`tests/test_cli_ingest_gmail_bl39.py` (the list catch and its ordinary pair are nodb, because they
end before `connect`; the three CLI stop-rule tests need the database).

## The bug (read from source at `862c5cdc`)

`src/brain/ingest/gmail.py:657` `_run(cmd, runner=None)` is the one shell-out behind
`list_messages` (`gws gmail users messages list`) and `read_message`
(`gws gmail users messages get`, `format=full`). With no `runner`, it calls
`subprocess.run(cmd, capture_output=True, text=True, check=False)`:

1. **No timeout.** A hung `gws` (auth prompt, stalled network, a helper holding stdout) blocks
   `brain ingest-gmail` forever. That breaks CLAUDE.md's "every external HTTP/DB client MUST have
   explicit timeouts".
2. **No stdout cap.** `capture_output` holds the whole answer in memory before anything checks it.
3. **No stdin redirect and no process group.** An auth prompt can wait on the terminal, and on a
   kill a child `gws` started survives.
4. **The error quotes too much.** A non-zero exit raises
   `GmailError(f"{' '.join(cmd)} failed: {proc.stderr.strip()}")`. The argv carries the
   `--params` JSON (the Gmail `q`, which holds the `from:` address and label, or a message id), and
   `gws`'s stderr can quote account text. `cli_ingest.ingest_gmail` prints it with
   `failed thread {tid}: {e}`.

Every line of the production branch is `# pragma: no cover`. No test reaches it.

### Callers (all read)

| Caller | Path to `_run` | What it does with a `GmailError` |
|---|---|---|
| `cli_ingest.ingest_gmail` (`cli_ingest.py:586`) | `gmail.list_messages(...)`, no runner | not caught: it propagates (see D8) |
| `ingest.gmail_brief_skip.read_thread_messages` (`:25`) | `gmail.read_message(stub["id"])`, no runner | the caller decides |
| └ `cli_ingest.ingest_gmail` per-thread loop (`:638`) | | `except (GmailError, psycopg.Error, ValueError, KeyError)`: prints `failed thread …`, counts it, continues |
| └ `cli_ingest._gmail_thread_subject_for_dry_run` (`:726`) | | `except (GmailError, KeyError, IndexError)`: `"(unable to fetch)"` |

Tests replace the function itself:
`monkeypatch.setattr("brain.ingest.gmail._run", fake)` (`tests/test_cli_ingest_gmail.py:163` and
others; `_fake_runner` takes `(cmd, runner=None)`). **`_run`'s name and signature stay as they
are**, so none of those tests change.

### The helpers this models on (read)

- `brain.vault.derived_links.gws.real_gws_runner(args, *, timeout=30, max_stdout_bytes=None)`.
  With a cap it goes through `_run_capped` → `brain.ingest.capped_process.run_capped`: binary
  from `shutil.which`, stdin `/dev/null`, stderr discarded unread, its own session, the whole
  group SIGKILLed at the timeout, past the cap and on any exception (Ctrl-C, SIGTERM). Every
  failure is a `DirectoryRefreshError(message, log_detail=...)`. `log_detail` is content-free by
  design: `"gws CLI not found on PATH"`, `"empty args"`, `"exit N"`, `"timed out after Ns"`,
  `"stdout over N bytes"`, `"could not run gws"`. The *message* is not: on a timeout it ends with
  `' '.join(args)`.
- Its two capped callers: `cli_gmail_attachments.gws_runner` (60 s, `gws_stdout_cap`, which RM-45
  uses in this same command for `attachments get`) and `cli_ingest_calendar.calendar_gws_runner`
  (`GWS_MAX_STDOUT_BYTES = 16 MiB`).
- `brain.ingest.github.gh_runner`, the other pattern: its own `run_capped` call with `which` and
  `popen` seams, raising its module's `GithubError` with a class-only message.
- Tests: `tests/test_capped_process.py` (throwaway `/bin/sh` scripts), `tests/test_ingest_github.py`
  (`gh_runner` against a script named `gh` in `tmp_path`), and `tests/test_log_privacy.py:150`
  `_gws_path` (a PATH holding only a fake `gws`, set with `monkeypatch.setenv`).

## Affected files and the ceiling

Counts are `wc -l` at `862c5cdc`.

| File | Lines now | Change | Expected after | Ceiling |
|---|---|---|---|---|
| `src/brain/ingest/gmail.py` | 668 | `_run`'s production branch replaced; two constants, one import; module docstring gains one sentence | ~685 | stays under 800; no row |
| `src/brain/vault/derived_links/gws.py` | 137 | module docstring: the stale "Mirrors the shell-out shape used by `brain.ingest.gmail._run`" sentence is reworded. Gmail now *uses* this runner | 137 ± 1 | no row |
| `src/brain/vault/derived_links/directory.py` | 683 | `GwsRunner` docstring: drop the stale `gmail.py:179` pointer, reworded in place | net zero | no row |
| `tests/test_gmail_run_cap.py` | new | the tests below, `pytestmark = pytest.mark.nodb` | ~200 | under 800 |
| `tests/test_import_cycles.py` | (read at implementation) | `"brain.ingest.gmail"` added to `ENTRY_MODULES` (D7) | +1 | no row |
| `CHANGELOG.md` | | one `### Fixed` entry under `[Unreleased]` | | n/a |
| `docs/plans/2026-10-23-backlog-reconciliation.md` | | BL-39 row marked closed, naming the PR | | n/a |

**Not touched:** `src/brain/cli_ingest.py` (901, over the ceiling, in `docs/ceiling-table.md`).
See D8. `scripts/check_ceiling_table.py` still runs as evidence that the row set did not move.

## Decisions

**D1. Reuse `real_gws_runner` and translate its error. Do not add a third copy of the capped-run
translation. (Recommended.)** `_run` becomes:

```python
def _run(cmd: list[str], runner: Runner | None = None) -> str:
    """Execute ``cmd`` and return stdout; delegate to ``runner`` when supplied (tests). ..."""
    if runner is not None:
        return runner(cmd)
    try:
        return real_gws_runner(
            cmd, timeout=GWS_TIMEOUT_SECONDS, max_stdout_bytes=GWS_MAX_STDOUT_BYTES
        )
    except DirectoryRefreshError as exc:
        raise GmailError(f"{_label(cmd)} {exc.log_detail}") from None
```

Why: every `gws` call (Calendar, Contacts, attachments, and now messages) keeps one invocation
policy in one place: the binary lookup, `/dev/null` stdin, discarded stderr, and the group kill.
`ingest-gmail --with-attachments` already reaches `real_gws_runner` in the same run, so the two
reads of the same command behave alike.

*Alternative B:* a `gws_runner` in `gmail.py` that calls `run_capped` directly, with `which` and
`popen` seams, like `gh_runner`. That is cleaner layering (ingest → ingest, no
`ingest → vault.derived_links` edge) and needs no PATH set in tests. It costs ~30 lines that
duplicate `gws._run_capped`'s `CappedRun` → error mapping. Pick B if the ruling is that an
ingest module must not import from `vault.derived_links`. `gmail.py` already imports
`brain.vault.slug`, so the package edge exists today.

**D2. Timeout: a module constant `GWS_TIMEOUT_SECONDS = 60.0`, not config. (Recommended.)** The
value matches `cli_gmail_attachments.GWS_TIMEOUT_SECONDS`, which serves the same command, and is
twice `real_gws_runner`'s 30 s default. A `format=full` read of a large thread is one HTTPS round
trip plus JSON encoding. Both `gws` sibling runners hold their timeout as a constant, so an env
var would be the only one, with a validation surface to maintain (YAGNI). `_run` reads the constant **at
call time** (not as a default argument), so a test can lower it with `monkeypatch.setattr`.

**D3. Cap: a module constant `GWS_MAX_STDOUT_BYTES = 48 * 1024 * 1024`, one cap for both calls.
(Recommended; ruling wanted on the size.)** The list call is tiny: one page with Gmail's 500-id
ceiling is ~40 KB. The get call sets the size. With `format=full`, large attachments come back as
`body.attachmentId` with no data, but small parts can be inlined as base64url. Gmail's 25 MB
message limit, with every part inlined, comes to 25 MB × 4/3 ≈ 33.3 MB plus JSON. 32 MiB
(33,554,432 bytes) is only just above that, so 48 MiB leaves headroom for JSON and headers. That
is still bounded, and an oversized message fails just its own thread (D5). *Alternative:* 16 MiB,
matching `cli_ingest_calendar.GWS_MAX_STDOUT_BYTES`, if the ruling prefers a tighter memory bound
over the extreme message.

**D4. Behaviour on timeout and on overflow: raise `GmailError`, the existing class. No new class.
(Recommended.)** Every caller already catches `GmailError` (table above), so the per-thread
failure, the dry-run fallback and the list propagation all keep working unchanged. Messages are
class-only: a fixed label from the argv's leading words (everything before the first `--` flag)
plus `log_detail`.

| Failure | Message |
|---|---|
| timeout | `` `gws gmail users messages get` timed out after 60s `` |
| overflow | `` `gws gmail users messages get` stdout over 50331648 bytes `` |
| non-zero exit | `` `gws gmail users messages list` exit 3 `` |
| no `gws` on PATH | `` `gws gmail users messages list` gws CLI not found on PATH `` |
| could not start | `` `gws gmail users messages get` could not run gws `` |

The message never carries `--params` (the `q`, the address, the label, the message id), stderr,
or `str(exc)`. The translation is `from None`, so a traceback (the list path, D8) cannot print the
`DirectoryRefreshError`'s message, which on a timeout ends in the full argv. A new
subclass (`GmailTimeoutError`) would let a caller tell the cases apart, but no caller wants to
today (YAGNI). If one is added later, its name must end in `Error` and it must be visible to
`tests/log_privacy_rules.py`.

**D5. A timeout fails its thread and the run continues: the existing per-thread contract.
(Recommended; ruling wanted.)** The worst case is a `gws` that hangs on every read: 60 s × the
thread count (`--max` defaults to 50), so ~50 minutes. Today that case blocks forever. Aborting
the run on the first timeout, or adding a consecutive-timeout breaker, would mean editing
`cli_ingest.py`, which is over the ceiling (D8). Recommendation: keep the contract here and add
a BL row only if the ruling wants the breaker.

**D6. stderr: discarded unread (it comes with D1). The child process group is killed on timeout,
past the cap and on interrupt (also from D1, through `run_capped`).** This is a diagnosability
change: today's message shows `gws`'s own error text (for example an expired auth). From now on
the user sees only the exit code, the same trade sec-117 F6 made for attachments and that
`gh_runner` makes. No hint is added to the message. `brain doctor` already probes `gws`.

**D7. Import placement: module-level `from brain.vault.derived_links.gws import real_gws_runner`
and `from brain.errors import DirectoryRefreshError` in `gmail.py`, with the cycle proved absent.
(Recommended.)** `gws.py` loads `capped_process` lazily because of the rev-117 cycle
(`vault → … → derived_links` during `brain.vault` init). `gmail.py` already imports
`brain.vault.slug` at module level, so `brain.vault` is initialized before the new line runs. To
prove it, add `"brain.ingest.gmail"` to `tests/test_import_cycles.py::ENTRY_MODULES`, so it is
imported first in a fresh interpreter. If that or any other entry fails, move the import into
`_run` with a one-line reason, as `gws._run_capped` does.

**D8. Leave `cli_ingest.py` alone. (Recommended; ruling wanted.)** A `GmailError` from
`list_messages` is not caught at `cli_ingest.py:586`, so it ends `brain ingest-gmail` with a
traceback. This is not new: any real `gws` failure on the list does it today. The app sets
`pretty_exceptions_show_locals=False` (`cli.py:416`), so no locals print, and D4's `from None` keeps
the argv out of the chain. Catching it (red line, exit 1) is the better UX, but it grows a file
over the ceiling. That needs a growth record inline and in the docstring, plus a promoted trail
hop in `docs/ceiling-table.md`. That is not this small PR. The recommendation is to file it as a
new BL row. If the ruling wants it here, it is ~6 lines plus the record, and one DB-marked CLI
test.

**D9. Remove the `# pragma: no cover` marks from `_run`.** The new tests reach every line.

## Tests

One new module, `tests/test_gmail_run_cap.py`, with `pytestmark = pytest.mark.nodb`. No
database, no real `gws`, and never the owner's account. Each process is a throwaway `#!/bin/sh`
script named `gws` in `tmp_path/bin`, and PATH is set to **that directory alone** with
`monkeypatch.setenv` (the `_gws_path` pattern). The installed `/opt/homebrew/bin/gws` therefore
cannot be found. Commands inside a stub use absolute paths (`/bin/sleep`, `/usr/bin/head`);
`printf` is a shell builtin. **Every stub is bounded:** no loops, `sleep` ≤ 20 s, and
`head -c` gives a fixed byte count. Each "refuses" test asserts that it finished in under 10 s.
Constants are lowered with `monkeypatch.setattr(gmail, "GWS_TIMEOUT_SECONDS", 0.3)` or
`GWS_MAX_STDOUT_BYTES`, which is pytest's own cleanup-backed double (rule 13). The ordinary-path
tests leave the constants at production values. All data is synthetic: ids `m-synth-1`, the
address `sender@example.test`, the label `test-label`, and a secret marker `SYNTH-STDERR-MARKER`.

All tests drive the production path (`runner=None`) through `gmail.list_messages` /
`gmail.read_message`, the public API every caller uses.

| # | Test | Kind | Stub | Asserts |
|---|---|---|---|---|
| T1 | `test_a_hung_gws_read_is_killed_at_the_timeout` | **regression: the original bug** (rule 12); refuses | `( /bin/sleep 20 ) &` then `/bin/sleep 20` (a grandchild holds stdout) | `read_message` raises `GmailError` equal to `` `gws gmail users messages get` timed out after 0.3s ``; under 10 s. On master it blocks ~20 s, then fails on `json.loads("")`, so it goes red. |
| T2 | `test_a_gws_answer_over_the_cap_is_refused_while_reading` | **regression: the unbounded read**; refuses | `/usr/bin/head -c 300000 /dev/zero` then `/bin/sleep 20` | cap 1000: `GmailError` equal to `` `…messages get` stdout over 1000 bytes ``; under 10 s. On master it holds 300 KB and fails later on the JSON, so it goes red. |
| T3 | `test_an_ordinary_list_and_get_return_their_answers` | keeps ordinary behaviour (pairs T1, T5) | prints a synthetic JSON body chosen by `$5` (`list`/`get`) | `list_messages(label="test-label")` returns the stub ids; `read_message("m-synth-1")` returns the dict; production constants. |
| T4 | `test_an_answer_exactly_at_the_cap_is_returned` | keeps ordinary behaviour (pairs T2) | prints a fixed JSON of known length *n* | cap = *n*: the dict comes back. |
| T5 | `test_a_failing_gws_is_reported_by_exit_code_never_stderr_or_params` | refuses; privacy | `echo SYNTH-STDERR-MARKER >&2; exit 3` | `list_messages(from_addr="sender@example.test")` raises `` `gws gmail users messages list` exit 3 ``. The marker, the address and `--params` are absent from `str(e)`. `e.__cause__ is None` and `e.__suppress_context__`. |
| T6 | `test_a_timeout_message_never_carries_the_query` | privacy (the timeout path's `DirectoryRefreshError` message *does* carry the argv) | as in T1 | `list_messages(from_addr="sender@example.test")` timing out: neither the address nor `"--params"` is in `str(e)`. |
| T7 | `test_a_missing_gws_is_a_gmail_error_naming_the_cli` | refuses | empty `bin` | `GmailError` whose message ends `gws CLI not found on PATH`. |
| T8 | `test_an_injected_runner_still_bypasses_the_subprocess` | keeps ordinary behaviour (the test seam) | empty `bin`, so a subprocess would fail | `list_messages(runner=fake)` returns the fake's answer. |
| T9 | `test_gmail_gws_limits_are_pinned` | pin | none | `GWS_TIMEOUT_SECONDS == 60.0` and `GWS_MAX_STDOUT_BYTES == 48 * 1024 * 1024` (or the values the ruling sets). |

Plus `tests/test_import_cycles.py` gains the `"brain.ingest.gmail"` entry (D7). That module is
subprocess-only. Its marker is read at implementation and kept as it is.

Existing tests that must stay green unchanged: `tests/test_cli_ingest_gmail.py` (DB; replaces
`_run`), `tests/test_gmail_thread.py`, `tests/test_gmail_own_brief_skip.py`,
`tests/test_cli_ingest_gmail_own_brief.py`, `tests/test_cli_ingest_gmail_attachments*.py`,
`tests/test_cli_gmail_attachments.py`, `tests/test_capped_process.py` and
`tests/test_log_privacy.py`. The DB-marked ones run in CI only, because Docker is down. Local
runs are `-m nodb` on the touched modules, under `.claude/full-suite.lock`, after a `df` check
(nothing below 6 GiB), in a uv venv of my own. There is no local full suite.

Named repo-rule scans run every time: `tests/test_log_privacy_rules.py` (nodb),
`tests/test_import_hygiene.py` and `tests/test_import_cycles.py`, plus `ruff check`, `mypy src/`
and `python scripts/check_ceiling_table.py`.

## Mutation proofs

Each mutation is applied in a `git archive` copy of the branch, outside the worktree. The copy
is checked to confirm the mutation applied (a `grep` of the changed line), the named tests are
run there, and the copy is deleted afterwards.

| M | Mutation in `gmail._run` | Must fail |
|---|---|---|
| M1 | drop `timeout=` (falls back to 30 s) | T1, T6 (the 20 s stub finishes, then the JSON fails) |
| M2 | drop `max_stdout_bytes=` (the uncapped `subprocess.run` path) | T2 (returned, not refused). *Measured:* not T1. On a timeout, Python 3.11's `subprocess.run` kills the child and `wait()`s for it on POSIX, so it does not wait out the grandchild that holds the pipe. T2 alone kills M2. |
| M3 | restore master's bare `subprocess.run` body | T1, T2, T5, T6, T7 |
| M4 | `from exc` instead of `from None` | T5 (`__cause__`) |
| M5 | message built from `str(exc)` instead of `exc.log_detail` | T6 (the address and `--params` appear) |
| M6 | no `except`: let `DirectoryRefreshError` escape | T1, T2, T5, T7 (expect `GmailError`); this is also why the CLI's per-thread `except GmailError` needs the translation |
| M7 | the constants bound as default arguments, not read at call time | T1, T2 (the patch has no effect) |
| M8 | `GWS_TIMEOUT_SECONDS = 600.0` | T9 |
| M9 | the import moved to the module top of `gws.py`'s `capped_process` (re-creating the rev-117 cycle) while `gmail.py` keeps its module-level import | `test_import_cycles` for some entry (proves D7's guard can fail; if none fails, D7 records that the cycle test does not see this edge) |

T1's and T2's "under 10 s" bound is what makes M2 and M7 fail: on those mutations the stub's
20 s sleep (or the grandchild holding the pipe) ends the run, not the timeout.

## Log privacy

`gmail.py` has no logger. The only output this path produces is the `GmailError` text that
`cli_ingest` prints to the terminal (`failed thread {tid}: {e}`: a thread id plus D4's class-only
message), or the traceback of D8. After this change, neither carries email content, the query,
an address, a message id, or `gws`'s stderr. `gmail_brief_skip`'s INFO line stays a count. T5 and
T6 pin this, and `tests/test_log_privacy_rules.py` (the G1 AST scan) must stay green with no new
allowlist entry.

## CHANGELOG

Under `## [Unreleased]`:

```markdown
### Fixed — `brain ingest-gmail` cannot hang on, or buffer, an unbounded `gws` answer (BL-39)

- Every `messages list` / `messages get` call now runs through the shared `gws` runner: 60 s
  timeout, a 48 MiB stdout cap read in chunks, no stdin, stderr discarded, and the whole process
  group killed at the timeout, past the cap or on Ctrl-C. A hung or oversized read fails its
  thread; the run continues.
- A failed call is reported by class (`timed out after 60s`, `stdout over … bytes`, `exit N`),
  never with `gws`'s stderr or the query.
```

## Tasks (team-driven-development)

1. Write T1–T9 and the `ENTRY_MODULES` line. Run them on the branch before the fix and record
   that T1, T2, T5, T6, T7 and T9 fail (red), with T1 and T2 failing by stub bound, not by hang.
2. Implement D1–D4, D7 and D9 in `gmail.py`, then the two docstring edits (`gws.py`,
   `directory.py`). Make the tests green. Run `ruff check`, `mypy src/`,
   `scripts/check_ceiling_table.py`, and confirm `wc -l` for each touched file.
3. Mutation proofs M1–M9 in archive copies. Record the results in the PR body.
4. CHANGELOG, plus the BL-39 row closure in the backlog doc.
5. Review and audit loop (CLAUDE.md rule 14) until both pass clean. Commit only with permission.
