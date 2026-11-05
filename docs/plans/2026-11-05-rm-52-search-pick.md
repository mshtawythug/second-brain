# RM-52: `brain search --pick`

> **For agentic workers:** REQUIRED SUB-SKILL: Use team-driven-development to implement this plan task-by-task.

Ticket: `docs/specs/2026-10-23-roadmap-completion-design.md`, RM-52. Size XS, wave 9, depends on
RM-09 (merged). The trigger has fired and nothing is built: `fzf` appears nowhere in `src/`, `tests/`
or `docs/` outside the spec.

## Rulings and deviations (recorded after approval)

- **D1 approved: deviates from the spec's wording.** The probe is "stderr is a TTY and `/dev/tty`
  opens", not `stdout.isatty()`. Evidence is in the table below.
- **D2, D4, D6 approved as recommended.** A WARN leaves `brain doctor` at exit 0: `report.failed`
  counts only `fail`.
- **D3 approved after verification.** `_NO_CLICK_SQL` requires `session_id IS NOT NULL`, so plain CLI
  rows stay out; tests pin an aborted pick in, a completed pick out, and plain searches out.
- **D5 changed:** CI installs real `fzf` from apt (0.67.0 on the first run, unpinned), and the real
  test **fails rather than skips** under `GITHUB_ACTIONS`. The script-on-`PATH` end-to-end test is dropped as
  redundant; small scripts still exercise `run_fzf` and the version probe.
- **Version floor added: `fzf` 0.29.** The picker's exact flags were run on real 0.29.0, 0.44.1 and
  0.72.0: real keys, id hidden, `{1}` exactly 36 bytes, no OSC leak. 0.29 is the lowest version run,
  on macOS only; CI's apt build is much newer, so CI does not test the floor. Below it, `--pick` refuses and the doctor row warns. An unreadable version proceeds.
- **`FZF_*` cleared for the child (new).** This replaces "`FZF_DEFAULT_OPTS` is honoured" under Risks.
  `--select-1` and `--filter` pick with no human, `--bind=...:execute` runs commands, and `--listen`
  serves the titles over HTTP. The cost is the user's theme. `FZF_DEFAULT_COMMAND` has no effect with
  stdin piped, and is cleared with the rest. Stdout matching stays as a second layer.
- **ANSI stripping added.** `one_line` removes the ESC byte but leaves a sequence's body as text, so a
  small regex removes whole CSI and OSC sequences first. `one_line` covers tabs, newlines, control
  and bidi characters (Unicode category `C*`).
- **D7, a judgment call for review: the preview and confidential documents.** `brain show` has no
  sensitivity gate, by the documented policy that the local CLI is inside the trust boundary. The
  **set** of documents is not widened: the list is plain search's, under the same filters, and the
  preview only receives offered ids. The **amount shown by default** is: the highlighted row's whole
  body, the first row's from the start, against the table's 120-character snippet. Withholding it per
  row needs a shell branch (`$SHELL` may not be POSIX) or a new `brain show` flag. Kept as the spec's
  plain `brain show {1}` and documented, with `--sensitivity normal` as the way to exclude them.
- **Line budgets:** `search_pick.py` came to about 290 lines against ≤ 180, for the version probe, the
  ANSI regex and the outcome type. `doctor_fzf.py` is 52 against ≤ 45. `cli_search.py` is +11, within
  its +12.

## Goal and acceptance criteria (quoted)

> **Goal:** Pick a search result interactively in the terminal, and record the pick.

> 1. With a fake `fzf` runner that selects the second line, the second result's id is printed.
> 2. The `search_queries` row and the `clicked` interaction share the minted session id.
> 3. Without a TTY, or without `fzf`, the command exits 1 with the message.
> 4. Without `--pick`, output and `search_queries` rows are unchanged (`session_id` stays `NULL`).

Scope out, from the spec: a picker for other commands; making it the default.

## What real `fzf` does

I ran real `fzf` 0.72.0 under a pty with a 40x120 window before writing this. The first attempt used
a 0x0 window, so its preview result was vacuous and was discarded. These results change the design.

| Setup | Result | Consequence |
|---|---|---|
| stdout piped, stderr on the terminal | Works, rc 0, the line is on stdout | `id=$(brain search --pick q)` works, so the probe must not require a stdout TTY |
| stderr `/dev/null`, stdout piped | rc 2, nothing drawn | `fzf` draws on **stderr**, so stderr is the stream to probe and must never be captured |
| No controlling terminal, stderr not a terminal, nothing auto-accepts | **Hangs** (5s in the bounded run; over 120s in the first run, killed) | `fzf` does not fail fast, and there is no timeout, so the preflight is the only thing preventing a permanent hang |
| stdin is a file, controlling terminal present | Works | Keys come from `/dev/tty`, so stdin needs no probe |
| `--print-query` in the user's `FZF_DEFAULT_OPTS` | rc 0, stdout is a blank line, then the selection | Taking the first line of stdout is wrong, so the output must be matched against the offered ids |
| `--multi` in `FZF_DEFAULT_OPTS` with `select-all` | Our later `--no-multi` wins, one line | Pass `--no-multi` explicitly |
| Esc, Ctrl-C | rc 130, empty stdout | This is an abort |
| Enter with no match | rc 1, empty stdout | Nothing was picked |
| Unknown flag | rc 2 | `fzf` failed |
| `--delimiter <literal tab> --with-nth 2..` | Title and source shown, id absent from the screen | The id is hidden as the spec intends |
| `{1}` in `--preview` | Exactly 36 bytes for a UUID, no trailing tab | `brain show {1}` receives a clean id |
| Preview prints an OSC title-setter and CSI clear | Body rendered, `ESC ]0;` never reached the pty | Hostile bodies in the preview do not drive the terminal |

`brain show` with no `--query` writes no interaction (`cli.py`, `show`), so the spec's plain
`brain show {1}` does not add `opened` rows as the cursor moves. A `--query` in the preview would add
one per cursor move and corrupt RM-22's engagement data, so a test pins the exact preview string.

## Design

Flow in `cli_search.search`:

1. **Preflight, before `Config.load`:** `--pick` with `--json` or `--facets` is a usage error (exit 2).
   Then the terminal probe, then `fzf` on `PATH`, each exit 1 with a fixed message. A refusal embeds
   nothing and writes no row.
2. **Mint** `session_id = uuid4()` only under `--pick`; otherwise it stays `None`. The footer is off
   under `--pick`, which also skips the extra count query.
3. **Search and log** as today, with the session id on the `search_queries` row.
4. **Leave the `with connect` block, then pick.** A human may sit in `fzf` for minutes, so no
   connection is held across that wait.
5. **On a selection:** print the full id to stdout, then record the `clicked` row on a new short
   connection. A logging failure warns by exception type only and never loses the printed id.

Exit codes:

| Case | Exit | stdout | stderr | `clicked` row |
|---|---|---|---|---|
| Picked | 0 | the id | | yes |
| Esc, Ctrl-C, `KeyboardInterrupt` | 130 | | | no |
| Enter with no match (rc 1) | 1 | | `nothing picked` | no |
| No results | 1 | | `(no results)` | no |
| `fzf` rc other than 0, 1, 130 | 1 | | `fzf failed (exit N)` | no |
| `fzf` could not start (`OSError`) | 1 | | fixed string | no |
| rc 0, but no line carries an offered id | 1 | | fixed string | no |
| No terminal, or no `fzf` | 1 | | fixed message | no row at all |

## Files and line budgets

| File | Change | Budget |
|---|---|---|
| `src/brain/search_pick.py` | New leaf: probe, line building, argv, runner, parsing, click record, CLI-facing wrapper | ≤ 180 |
| `src/brain/doctor_fzf.py` | New `fzf` row, modelled on `doctor_git.py` | ≤ 45 |
| `src/brain/cli_search.py` | `--pick` option, preflight, mint, branch; the "CLI searches have no session" comment is rewritten in place | +≤ 12 (spec §2.2), 728 → ≤ 740 |
| `src/brain/doctor_runtime.py` | Import, splice before `replica`, two docstring lines | +4 (603 → 607, cap 623) |
| `src/brain/gaps.py` | Rewrite the stale "MCP-only" comment in place | ±0 |
| `src/brain/engagement.py` | Rewrite the stale "Nothing writes `clicked`" docstring line in place | ±0 |
| `docs/cli-reference.md`, `CHANGELOG.md` | The flag, its exit codes, the `fzf` doctor row | |
| `skills/consult-brain/SKILL.md` | One clause on an existing line: `--pick` is for a human at a terminal, never for agents | +0 (at its 300-line cap) |

`cli.py` is not touched. `_record_interaction_best_effort` stays there because
`tests/test_cli_rate.py` and `tests/test_cli_show_interaction.py` patch `brain.cli.record_interaction`,
and moving it would break those patch points. The leaf has its own guarded write, which also has to
cover the connect, so it is not a copy of that helper. `ceiling-table` is unaffected.

### `search_pick.py` shape

- `has_terminal() -> bool`: `sys.stderr.isatty()` and `/dev/tty` opens
  (`os.open` with `O_RDWR | O_NOCTTY | O_CLOEXEC`, closed at once). These are the two things `fzf`
  was measured to need. An `OSError` means `False`.
- `pick_refusal() -> str | None`: the fixed message, terminal first, then `fzf`.
- `result_lines(results) -> str`: `<id>\t<title>\t<source>\n`, passing title and source through the
  existing `brain.ingest.text_safety.one_line`. It collapses tabs, newlines and U+2028 and strips
  category `C*`. The title is then capped at 200 characters; an empty title becomes `(untitled)` and
  a `None` source becomes `manual`, matching `brain show`.
- `fzf_argv(binary) -> list[str]`: `--delimiter <tab> --with-nth 2.. --no-multi --preview 'brain show {1}'`.
  Only long-stable flags. The argv carries neither the query nor any title, so neither shows in `ps`.
- `run_fzf(argv, stdin_text) -> FzfRun`: `subprocess.run` with an argv list, `stdin` as bytes, stdout
  piped, **stderr inherited**, `errors="replace"` on both encode and decode. This is the seam the
  tests replace.
- `parse_selection(stdout, offered) -> str | None`: the first line whose first tab field is an
  offered id. Anything else fails closed, so an id we did not offer is never printed or recorded.
- `run_pick(...)`: the CLI-facing wrapper; prints, records and raises `typer.Exit`. `durations.py`
  and `maintenance.py` are the precedent for a non-CLI module importing `typer`.

## Tests

`tests/test_search_pick.py` for pure logic, `tests/test_cli_search_pick.py` through the real CLI on
the `second_brain_test_rm52` database, `tests/test_doctor_fzf.py` for the row. All data is synthetic.
Every guard has a pair:

| Guard | Refuses | Keeps ordinary behaviour |
|---|---|---|
| Terminal probe | stderr not a TTY; `/dev/tty` will not open | A piped stdout with a terminal on stderr is accepted (`$(...)`) |
| `fzf` on `PATH` | Missing: exit 1, install hint, no embed, no row | Present: proceeds |
| Flag combination | `--pick --json`, `--pick --facets`: exit 2 | `--pick --no-meta`, `--pick --brief`, `--pick -n 20` work |
| Line building | Title with tab, newline, U+2028, ESC, C1 stays one three-field line | Accented, CJK and emoji titles survive; a title with spaces is unchanged |
| Selection parsing | Unoffered id, empty stdout, `--print-query`'s blank line alone | A real line, and a blank line followed by a real line, both return the id |
| Preview string | No `--query`, no title, no query text in argv | Exactly `brain show {1}` |
| Click recording | Abort, no match and `fzf` failure write no `clicked` row | A pick writes one, `source='cli'`, with the query and agent id |
| Click failure | A failed connect or write never fails the command | The id is still printed, exit 0, warning by type name only |
| Connection across the wait | The fake runner sees no extra backend from the search during the pick | The search row is committed before `fzf` starts |
| No `--pick` | The runner is never called even when the probe says yes | Output is unchanged, `session_id` is `NULL` (AC4) |

Acceptance criteria 1 to 4 each get a named test. In addition:

- **`brain gaps`:** an aborted pick shows as `no_click`, and a completed pick does not.
- **`engagement_stats`:** the click counts as a read from a search session, tying back to RM-22.
- **A real subprocess on CI:** a small executable `fzf` script on `PATH`, because a faked runner alone
  would hide argv and stream mistakes.
- **Real `fzf` end to end, skipped when `fzf` is absent:** the actual `brain search --pick` under a
  pty, `FZF_DEFAULT_OPTS=--bind=load:up+accept` selecting the second line, then the id and both rows
  checked in the database. A hard kill on timeout means it cannot hang. It also covers preview escape
  handling.
- `tests/test_doctor_runtime.py`: the pinned row list gains `fzf`, 11 becomes 12.
- A real `brain doctor` and a by-hand `--pick` against the synthetic data, recorded in the PR.

**Mutations, each in a git-archive copy, each confirmed applied:** drop the stderr probe; drop the
`/dev/tty` probe; take the first stdout line instead of matching; add `--query` to the preview; move
the pick inside `with connect`; mint the session unconditionally; capture stderr; remove `one_line`.

**Every run also includes the named set:** `tests/test_log_privacy_rules.py`,
`tests/test_log_privacy.py`, `test_import_hygiene*`, `tests/test_vault_delete.py`,
`tests/test_cli_registry*.py`, the src-wide `rglob` scans, the skills tests and
`scripts/check_ceiling_table.py`. Then `ruff check` and `mypy src/`. No full local suite; CI runs it.

## Security

- **Untrusted input:** titles are other people's text (email subjects, web pages). They reach `fzf`
  only over stdin, sanitized and capped. They are never in an argv or a shell string.
- **Shell:** `brain` builds no shell string. `fzf` runs the fixed `brain show {1}` and quotes the field
  itself; the field is a UUID the database produced.
- **Output trust:** `fzf`'s stdout is matched against the offered ids, so a line built by hostile text,
  or by the user's own `FZF_DEFAULT_OPTS`, cannot be printed or recorded.
- **Confidential data:** no egress. `--sensitivity` documents that the local CLI is inside the trust
  boundary, and the human table already shows these titles. `--pick` adds no new read path and sends
  nothing off the machine. The F6 residue audit is unaffected: no new `documents` statement.
- **Logs:** the query and titles are never logged. Warnings carry the exception type only.
- **Errors:** fixed strings, no paths. The one variable is `fzf`'s integer exit code.
- **Timeouts:** the DB connect keeps `connect_timeout`. `fzf` deliberately has **no timeout**, because
  a human is choosing. This is the one recorded exception to the timeout rule, and the terminal probe
  is what makes it safe.
- **Output cap:** the input is bounded by `--limit`, and `--no-multi` limits the echo to one line.

## Risks

- **`FZF_DEFAULT_OPTS` is honoured** so the user's theme keeps working. Odd settings such as `--print0`
  fail closed with the fixed error rather than misreporting.
- **`brain` must be on `PATH` for the preview.** If it is not, the pane says so and picking still
  works. The docs note this.
- **Default `--limit` is 5**, a short list for a picker. It stays as is, and the docs suggest `-n 50`.
- **An `fzf`-less CI skips the real `fzf` test.** The script-on-`PATH` test still runs there (see D5).
- **`brain doctor`:** a missing `fzf` adds a permanent WARN on those installs, like `gh` and `git` (D4).
- **A session on the same id in `brain usage`:** pick sessions now count in its distinct-session
  figures, which is accurate and additive. Non-pick searches are unaffected.

## Decisions

**D1. Which stream decides "has a terminal"? This deviates from the spec's wording.**
The spec says "`--pick` works when stdout is a TTY", and also that printing the id keeps the command
scriptable. Those conflict: under a stdout check `id=$(brain search --pick q)` and
`brain search --pick q | xargs brain show` are refused, though `fzf` handles both. A stdout check also
accepts `2>/dev/null`, where `fzf` exits 2.
- (a) Spec wording, `stdout.isatty()`. Loses scripting, and does not prevent the no-terminal hang.
- (b) **Recommended:** stderr is a TTY and `/dev/tty` opens, the two things `fzf` was measured to need.
  AC3 still holds, and piped or backgrounded agents with no terminal are still refused.

**D2. No results.**
- (a) **Recommended:** `(no results)` on stderr, exit 1, `fzf` not started. An empty `$(...)` with exit
  0 is a trap for scripts.
- (b) Exit 0, matching plain `brain search`.

**D3. An aborted pick and `brain gaps`.**
A minted session id makes an aborted pick's row eligible for `no_click`, which until now was MCP-only.
- (a) **Recommended:** let it count. The user saw results and took none, which is what `no_click`
  means. Update the stale comment.
- (b) Exclude CLI rows from `no_click`, which needs a SQL change and a new predicate for no clear gain.

**D4. `brain doctor` status when `fzf` is missing.**
- (a) **Recommended:** WARN with an install remedy, the same as `git` and `gh`, and the spec's row.
- (b) OK with a note, quieter but unlike its siblings.

**D5. Real `fzf` on CI.**
- (a) **Recommended:** no CI change. The real test skips without `fzf`, runs locally and is recorded in
  the PR; the script-on-`PATH` test runs on CI.
- (b) Install `fzf` in the CI job. Stronger on flag drift across versions, but it widens an XS ticket
  into CI config.

**D6. `--pick` with `--json` or `--facets`.** The spec does not cover this.
- (a) **Recommended:** usage error, exit 2, before any work. Stdout has one job under `--pick`.
- (b) Silently ignore them.

**RM-52.D1 (spec, settled):** the picker is not on by default for a TTY.

## Sequence

1. `search_pick.py` and its pure tests (RED, then GREEN).
2. `doctor_fzf.py`, the `doctor_runtime.py` splice, the pin update.
3. The `cli_search.py` wiring and the DB-backed CLI tests, ACs 1 to 4.
4. The script-on-`PATH` and real-`fzf` pty tests.
5. Comment fixes in `gaps.py` and `engagement.py`, then docs, changelog, skill clause.
6. Mutations in a git-archive copy, the named test set, `ruff`, `mypy`.
7. Review and audit loop per CLAUDE.md rule 14.

After merge, for the coordinator: the Appendix B tracker row and `cli.md` in auto-memory.
