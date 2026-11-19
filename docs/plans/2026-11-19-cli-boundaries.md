# PR-C2 — other CLI boundaries (F13, F17, F28, F29, F34)

> **For agentic workers:** REQUIRED SUB-SKILL: Use team-driven-development to implement this plan task-by-task.

Branch `fix/cli-boundaries`, worktree `.claude/worktrees/fix-c2-cli-boundaries`, base `ef824ed1`.
Source of truth: `brain-qa/REPORT.md` (F13, F17, F28, F29, F34). The evidence is in
`logs/a/A19.txt`, `logs/c/C02.txt` and `logs/b2/B11.txt`, `B13.txt`, `B21.txt`, `B28.txt`.
PR-C1 owns `cli_ingest.py`, `cli_extract.py`, the ingest pipeline and `reembed`'s
`--batch-size`/`--limit` (F12). This PR touches none of them.

## Root causes

| F | Symptom (QA) | Root cause |
|---|---|---|
| F13 | `capture import` into an unscaffolded vault prints `failed x.md (VaultNoteSyncError)` per file, exit 1. The cause is dropped. | `capture_import.import_file` → `write_vault_note` raises `VaultNoteSyncError("template 'note' not found")` (`vault/note_builder.py:246-250`). The drop-folder runner reports a per-file failure **by class only** (`drop_folder.py:637-639`, `describe_failure`, which is deliberate class-only reporting). Every file is moved into `imported/` and back, and nothing says "run `brain vault init`". |
| F17 | `brain setup`: a refused `docker compose up -d` gives a raw `CalledProcessError` traceback. | `setup.py:1122-1125` calls `subprocess.run(compose_cmd("up","-d"), check=True)`. `setup_cmd` (`cli.py:619-633`) catches only `SetupError`. stderr is not captured, so nothing could be quoted. |
| F28 | `graph --out <missing dir>/x.json` gives a `FileNotFoundError` traceback. | `cli.py:7525` `out.write_text(...)` runs after the DB query, with no check on the parent. |
| F29 | `-n -1` reaches SQL `LIMIT` (`InvalidRowCountInLimitClause`) on `graphrag communities list` and `review list`. Elsewhere it is accepted silently. | 21 `-n`/`--limit` options have no `min` (table below). |
| F34 | `todo --source bogus` exits 0 with "(no action items)". | `cli.py:4401` `source: str` is never validated. The existing `_validate_source_choice` (`cli.py:3865`) is not applied. |

## F29 — every `-n`/`--limit` option in the CLI

Derived by **Typer introspection** (`typer.main.get_command(app)`, walking every command
**and every group callback**), not by grep. The grep missed `gaps` and `connect`, whose
`-n` lives on the group callback. Typer 0.27 vendors click as `typer._click`, so the test
checks `param_type_name == "option"` and does not use `isinstance(click.Option)`.

| Command | Opt | Now | Change | Why |
|---|---|---|---|---|
| `ask` | `-n` | min=1 | — | already bounded |
| `backfill scan-secrets` | `--limit` | min=0 | — | **0 = no limit** (documented in help) |
| `capture review` | `-n` | none | **min=1** | `-1` reaches `list_documents` LIMIT |
| `connect` (group cb) | `-n` | none | **min=1** | to `iter_suggestions` LIMIT |
| `connect list` | `-n` | none | **min=1** | same |
| `dedup list` / `dedup scan` | `--limit` | min=1 | — | |
| `demo query` | `-n` | min=1 | — | |
| `docembed backfill` | `--limit` | min=1 | — | |
| `elicit list` | `-n` | none | **min=0** | **0 = BRAIN_ELICIT_QUEUE_LIMIT** (help + `docs/cli-reference.md:1996`). A negative value was silently treated as 0 and is now refused. |
| `enrich` | `-n` | none | **min=1** | backfill ceiling. 0 or -1 was a silent no-op. |
| `eval`, `explain`, `list`, `search`, `usage`, `tags suggest-merges`, `tags summarize` | | min=1 | — | |
| `gaps` (group cb) | `-n` | none | **min=1** | to `top_search_failures` SQL LIMIT |
| `graphrag build` | `-n` | none | **min=1** | `0` reconciled nothing. "default: all" is `None`, not 0. |
| `graphrag communities build` / `refresh` | `-n` | none | **min=1** | to a summarize SQL LIMIT. "default: all" is `None`. |
| `graphrag communities list` | `-n` | none | **min=1** | **the QA traceback (B21)** |
| `graphrag entities` | `-n` | none | **min=0** | **0 = all** (help). `-1` also meant all (`relational.py:313` `limit <= 0`). That is now refused (see D3). |
| `graphrag entity` | `-n` | min=0 | — | **0 = all neighbours** (help, `docs/graphrag.md:106`) |
| `graphrag search` / `themes` | `-n` | none | **min=1** | accepted silently (B21) |
| `reembed` | `-n` | none | **PR-C1** | owned by C1 (F12). The test excludes it by name. |
| `resurface` | `-n` | none (body check) | **min=1**, body check removed | same exit 2. Typer's message still names `--limit`. |
| `review list` | `-n` | none | **min=1** | **the QA traceback (B28)** |
| `review scan` | `--limit` | none (body check) | **min=1**, body check removed | C27's `--limit 0` exit 2 is kept (Typer exit 2) |
| `timeline` | `-n` | none (lib raises) | **min=1** | rejected at parse time, not after config load |
| `todo` | `-n` | none | **min=1** | `-n 0` returned **one** row (append, then `len >= 0` break) |
| `vault sync-summaries` | `--limit` | none | **min=1** | 0 or -1 inspected nothing, silently |

**D1. No 0 is given a new meaning.** All four documented 0 meanings are kept with
`min=0`: `backfill scan-secrets`, `elicit list`, `graphrag entity`, `graphrag entities`.
Every other option gets `min=1`.

**Coverage test** `tests/test_cli_limit_bounds.py` (pure, no DB):
1. `test_every_limit_option_has_a_minimum`. It walks every registered command and group
   callback. Any option whose opts include `-n`, or any `--…limit…` opt, with an int type,
   must have a `min`. It must be `>= 1`, unless the command path is in
   `ZERO_MEANS = {path: reason}`, where it must be **exactly 0**.
   - `reembed` is excluded, with a comment naming PR-C1 (F12). If it has a `min`, the test
     still requires `>= 1`, so it is correct in either merge order.
   - The test also fails on a stale `ZERO_MEANS` or exclusion entry (a path that no longer
     exists), so the allowlist cannot rot.
2. `test_a_negative_limit_is_refused_before_the_body_runs`, parametrized over each changed
   command, with `-n -1` (and `0` for the min=1 ones). It asserts exit 2 and `is not in the
   range` in the output. The QA cases come first: `graphrag communities list -n -1`,
   `review list -n -1`, `graphrag search q -n -1`, `graphrag themes -n -1`,
   `graphrag entities -n -1`, `todo -n 0`. No DB: click refuses the value before any
   callback runs.
3. **Prove it can fail.** In a `git archive` copy of the branch, delete `min=1` from
   `review list`'s option. Run test 1 there with `PYTHONPATH=<copy>/src`, after confirming
   `brain.__file__` points into the copy. It must fail naming `review list`. Then delete the
   copy. Repeat once with a `ZERO_MEANS` entry changed to `min=1`, to prove the "exactly 0"
   branch can fail too.

## F28 — `graph --out` into a missing directory: **exit 2, do not mkdir**

**D2.** Validate before any work:
`if out is not None and not out.parent.is_dir(): raise typer.BadParameter(..., param_hint="--out")`.
Also `dir_okay=False` on the Option, because `--out <a directory>` was an
`IsADirectoryError` traceback. Reasons:
- `--out` names a file the user typed. A missing parent is almost always a typo, and
  `mkdir -p` would silently create a tree at the typo'd path.
- Shell redirection, `dot -o` and `cp` all fail on a missing parent.
- The check runs before `Config.load()` and the graph query, so no work is wasted. Today
  the query runs first and then crashes.
- `audio --out` does mkdir, but its default is a brain-owned directory under
  `$BRAIN_HOME/audio/`. `graph --out` has no default.

## F34 — `todo --source`

**D4.** Use `callback=_validate_source_choice` on the existing `--source` Option line
(net-zero). The accepted set is the canonical `brain.source_kinds.VALID_SOURCE_KINDS`, which
is "the real set of sources". It is **not** krisp-only:
- `todo.iter_action_item_docs` keeps the parameter open deliberately.
- `ingest-stdin --source manual --content-type krisp_action_items` is legitimate.
- The same helper already backs `search`, `resurface` and `docs`, so the message is the
  same everywhere: `unknown source 'bogus' (expected: applenotes|calendar|…)`, exit 2.

## F17 — `brain setup` compose failure

**D5.**
- `_compose_up` runs `up -d` with `stderr=subprocess.PIPE, text=True` and **without**
  `check=True`.
- On a non-zero exit it raises `SetupError(compose_failure(...))`. The existing handler in
  `setup_cmd` prints `error: …` and exits **1**. `cli.py` does not change for F17.
- `compose_failure(step, returncode, stderr, *, pg_port)` is a new pure function in
  `src/brain/_compose.py` (74 lines). It builds:
  - `docker compose up -d failed (exit N)`;
  - the last 10 non-empty stderr lines, each through `ingest.text_safety.one_line`, so
    docker's output cannot drive the terminal;
  - a hint: "is Docker running, and is port P free? Fix it and re-run `brain setup`; to see
    the full output run: `<the compose argv>`".
- **Why not tee stderr live (Popen)?** Every existing run_setup test (`tests/test_setup*.py`)
  intercepts only `subprocess.run`. A switch to `Popen` would let those tests reach the real
  `docker compose up -d`. That is a hard safety violation. Keeping `subprocess.run` keeps
  every existing mock in force.
  - The cost: compose's progress lines for `up -d` are captured, not streamed.
  - So setup prints one `Starting Postgres (a first run may build the image; this can take
    a few minutes)…` line first.
  - `pull`'s output still streams, unchanged.
- Risk: a test mock whose `returncode` is a bare `MagicMock` would now read as non-zero.
  All current mocks set `returncode`. I will run all of `tests/test_setup*.py` and
  `tests/test_cli_setup.py` to confirm.
- `ollama list` / `ollama pull` are out of scope: see "Not in scope".

**Tests** (new `tests/test_setup_compose_failure.py`; **a stub `docker` on a tmp PATH**, never
real docker):
- A `tmp/bin/docker` shell script logs its argv to a file.
  - `info` → 0.
  - `compose … pull …` → 1.
  - `compose … up -d` → writes two synthetic lines to stderr and exits 17.
  - Anything else → exit 99 with a "stub: unexpected" message.
  - It never execs anything.
- `monkeypatch.setenv("PATH", f"{stub}:{os.environ['PATH']}")`, with `HOME`, `BRAIN_HOME`
  and `BRAIN_COMPOSE_PROJECT` all in tmp.
- `patch("brain.setup.ensure_shim")` and the `_project_dotenv` patch that the existing tests
  use.
- A free port from `socket.bind(("127.0.0.1", 0))`.
1. RED `test_a_refused_compose_up_names_its_stderr_and_exits_nonzero`: run
   `brain setup --profile minimal --non-interactive --brain-home … --vault … --skip-skill
   --no-daemons --port P` through CliRunner. Assert:
   - exit 1;
   - both synthetic stderr lines and `exit 17` are in the output;
   - the hint names `brain setup` and the port;
   - neither `Traceback` nor `CalledProcessError` appears;
   - the stub log shows `up -d` was the last call, so nothing ran after the failure.
   Today this test fails with the CalledProcessError.
2. Pure unit tests for `compose_failure`:
   - it keeps only the last 10 non-empty lines;
   - it strips an ESC sequence;
   - empty stderr gives "(no stderr)" and still the hint.
   Without the subprocess fix this does not reproduce the bug on its own; it pins the
   message shape.

## F13 — `capture import` into an unscaffolded vault

**D6 (the cause).** A folder-level refusal before any file moves:
- `capture_import.importer(ctx)` already refuses a vault overlap with `DropFolderError`.
  It now also raises `DropFolderError(UNSCAFFOLDED)` when the vault's note template is
  missing:
  `"the vault has no note template (_templates/note.md): run `brain vault init` to scaffold
  it"`.
  - The text is fixed. `_folder_error` relies on DropFolderError text being fixed, so it
    does not quote the vault path.
- The template path comes from a new public `note_builder.note_template_path(vault,
  template="note")`, and `write_vault_note` uses it too (DRY, no second copy of
  `_templates/<name>.md`).
- Both `brain capture import` and `brain ingest-folder` (capture-mode folders) evaluate
  `importer()` before `run()`. Both already print `skipped <dir>: <message>` and exit 1.
  So the files are never moved into `imported/`. Today they are moved and moved back.
- **Also**: `write_vault_note`'s own reason becomes `template 'note' not found (run `brain
  vault init`)`. That helps `brain capture` and `brain note new` too. The old substring
  is kept, so existing assertions still match.
- `cli_ingest_folder.py` and `drop_folder.py` are **not** edited. The class-only per-file
  detail stays as it is: it is a privacy policy, and the folder-level check makes it
  unreachable for this cause.

**Tests:**
1. RED: rewrite `tests/test_cli_capture_import.py::test_a_vault_with_no_template_sends_the_share_back`.
   It asserts `"VaultNoteSyncError" in output`, which is the bug's symptom. It becomes
   `…_is_refused_naming_vault_init`, asserting:
   - exit 1;
   - `brain vault init` in the output;
   - `VaultNoteSyncError` **not** in the output;
   - the share untouched in place;
   - **no `imported/` directory created**.
   This is DB-backed: `capture import` connects first, so it uses `fix_c2_test`.
2. The same assertion for `ingest-folder --configured` over a capture-mode folder (DB).
3. Pure: `importer(ctx)` raises `DropFolderError` with the hint for a template-less tmp
   vault, and returns a callable for a scaffolded one (`vault.init_vault`).

**D7 (the title) — decided: option (a), no title change (fix-lead, 2026-11-19).** The QA
premise is wrong. Titles are not filename slugs: they are `make_capture_title(body)`, a slug
of the share's first words (`2026-11-14-capture-qa-capture-one-synthetic-captured`). That is
documented, shared behaviour: `docs/guides/mobile-capture.md:120` says "titled from its first
words (as `brain capture` titles a note)", and RM-39 keeps parity with `brain capture` on
purpose. The "file stem" fallback would make it worse for the main use case. The documented iOS
Shortcut names every file `Save to Brain <yyyy-MM-dd HH.mm.ss>.md` (`mobile-capture.md:46,59`),
so a heading-less share would be titled with a timestamp. The heading-first idea is under
"Not in scope".

## Not in scope (for the backlog)

- **Capture-import titles from a leading heading (F13, option b).** If a share's first
  non-blank line is a Markdown ATX heading, the note title would be that heading
  (`one_line`, capped). Otherwise it would stay `make_capture_title(body)`, with no stem
  fallback. Cost: about 10 lines in `capture_import.py` plus a guide edit. It would make
  capture-import titles diverge from `brain capture` titles.
- **`brain setup`'s Ollama steps traceback the same way as F17.**
  - `src/brain/setup.py:1167`: `subprocess.run(["ollama", "list"], capture_output=True,
    check=True, text=True)`.
  - `src/brain/setup.py:1173`: `subprocess.run(["ollama", "pull", model], check=True)`.
  - A non-zero exit from either raises a raw `CalledProcessError` out of `run_setup`. The
    same treatment as `up -d` would fix them.

## Ceiling (docs/ceiling-table.md trail rules)

Estimated net change per file:

- **`cli.py`** (8,704): **net −4**.
  - Additions: F28 +2 check lines and 1 inline comment.
  - Removals: the `review scan` and `resurface` body checks (−2 each), and three multi-line
    Options collapsed to `"--limit", "-n", min=…,` (−1 each).
  - Every other `min=` goes on an existing line, all under 100 cols.
  - It shrinks, so it owes **no growth record**. The row is still touched:
    - promote the trailing descriptive hop "the empty-daily-brief commit" to
      `8,704 (`0f32826`)` (verified: `git show 0f32826d:src/brain/cli.py | wc -l` = 8704,
      and no later commit touched the file);
    - add a descriptive hop for this commit (net-negative, no record owed);
    - move "Authoritative through" to `0f32826`.
- **`setup.py`** (1,224): **grows by about 4** (the stderr capture, the failure branch, the
  "Starting Postgres" line, the import). That is a written growth record, needed in both
  places:
  - an inline comment at `_compose_up`;
  - a one-line pointer in the module docstring.
  - Its trail is SHA-bound through `5cba1e9` with nothing later
    (`git log 5cba1e9..HEAD -- src/brain/setup.py` is empty), so there is nothing to
    promote. Add a descriptive hop and the growth paragraph.
- New or edited files under 800: `_compose.py` (74), `capture_import.py` (257),
  `vault/note_builder.py` (304), `_capture_command.py` (573, −1), `cli_connect.py` (294).
- Run `python scripts/check_ceiling_table.py` after the edits.

## Docs

- `docs/cli-reference.md:478`: the `graphrag entities --limit 0 (and any limit < 1)` row
  becomes "`--limit 0` means all; a negative value is refused (exit 2)".
- The CHANGELOG (below).

## CHANGELOG (inserted immediately before `### Added — engagement-aware ranking stage, off by default (RM-22b)`)

```
### Fixed — CLI boundaries: limits, `graph --out`, `todo --source`, setup and capture-import errors (F13, F17, F28, F29, F34)

- Every `-n`/`--limit` option now has a minimum. A negative value (and 0 where 0 has no
  meaning) exits 2 with a usage error, instead of a SQL `LIMIT` traceback
  (`graphrag communities list`, `review list`) or being accepted silently. Where 0 is
  documented, it keeps its meaning: `graphrag entities`/`entity -n 0` show all, `elicit list
  -n 0` uses `BRAIN_ELICIT_QUEUE_LIMIT`, and `backfill scan-secrets --limit 0` means no limit.
  `graphrag entities -n -1` used to mean "all" by accident and is now refused. `todo -n 0`
  used to print one item.
- `brain graph --out` into a directory that does not exist (or onto a directory) exits 2,
  naming the path, before it queries the graph. Before, it ran the query and then crashed
  with a traceback.
- `brain todo --source` is checked against the known source kinds and exits 2 on an
  unknown one.
- `brain setup`: a failed `docker compose up -d` prints the last lines of its stderr and a
  hint, and exits 1. Before, it raised a `CalledProcessError` traceback.
- `brain capture import` (and `ingest-folder` over a capture folder) into a vault with no
  `_templates/note.md` stops before moving any file and says to run `brain vault init`.
  Before, it printed `(VaultNoteSyncError)` for each file.
```

## Verification (Phase 2)

- RED first. Each new or changed test runs against unmodified code and fails for the stated
  reason.
- Then: `ruff check`, `mypy src/`, `python scripts/check_ceiling_table.py`.
- Targeted tests, under the lock and the disk check:
  - `tests/test_cli_limit_bounds.py`
  - `tests/test_setup_compose_failure.py`, `tests/test_setup*.py`, `tests/test_cli_setup.py`
  - `tests/test_cli_capture_import.py`, `tests/test_capture_import.py`
  - `tests/test_cli_resurface.py`, the `review scan` / `graphrag entity` / `entities` CLI tests
  - `tests/test_cli_graph*.py`, `tests/test_examples_rot.py`
  - the repo-rule scans: `test_log_privacy_rules`, `test_log_privacy`, `test_import_hygiene`,
    `test_repo_hygiene_files`
- The mutation proof for the coverage test, as in F29 above.
- Manual CLI checks:
  - `brain todo --source bogus`, `brain graph --out /tmp-missing/x`, and the `-n -1` cases,
    all with `BRAIN_PROD_DATABASE_URL=postgresql://localhost:1/none`. They fail at parse
    time, so no DB is needed.
  - `brain setup` only with the PATH stub, never real docker.
- The test DB `fix_c2_test` is dropped by exact name at the end.
- After merge, the coordinator updates the `cli.md` memory with the new `min` bounds and the
  `todo --source` validation.
