# `brain restore`: a drop hint for the server that was restored, clean `--json`, a scoped preview, no leftover staging dirs (F22, F23, F24, F27)

Branch `fix/restore-hints`, PR-D of the 0.4.0 QA-fix program, cut from master `6c078c1f`.
Source of truth: the QA report's F22 (MED), F23, F24 and F27; evidence row C24
(`brain-qa/logs/c/C24.txt`).

## 1. Findings and root causes

### F22: the post-restore hint names the production container

- Symptom (C24, `--yes --db-only` against the QA lane's own server): `Drop it when satisfied:
  docker exec second-brain-postgres psql -U brain -d postgres -c 'DROP DATABASE brain_qa_c_replaced_…'`.
  The restore ran in a different container. Pasting the hint would reach the production server.
- Root cause: `src/brain/cli_backup.py:242-245` (`_print_report`). It hardcodes the literal
  `second-brain-postgres` and `-U brain`. The brief suspected `postgres_container_name()`, but the
  hint never calls it: it is a string literal. `postgres_container_name()` feeds only
  `resolve_pg_tool(container=…)` in `backup/restore.py:534`.
- What the restore actually used is known, and then dropped. `_restore_database`
  (`backup/restore.py:531-537`) gets a `PgToolPlan` from `resolve_pg_tool`. `plan.container` is set
  only when the container provably serves the DSN's port (`pgtool.container_serves`, or
  `discover_container` from `docker ps`). It is `None` on the host leg, where `pg_restore -h/-p`
  went straight to the DSN. Neither `plan` nor the container reaches `RestoreReport`.

### F23: `restore --json` puts prompts on stdout

- Symptom: `--yes --json` on a non-empty target prints `Type "restore and overwrite my brain" to
  confirm []:` and then the JSON object, both on stdout. A declined `--json` run prints the prompt
  and no JSON at all.
- Root cause: `cli_backup.py:215-232` (`_gate`). `typer.confirm` / `typer.prompt` default to
  stdout, and `Aborted — nothing was changed.` is `secho`'d to stdout. Also
  `cli_backup.py:302-307`: the missing-`.sha256` warning goes to stdout even in `--json` mode.

### F24: `--vault-only` previews (and gates on) the database

- Symptom: `--yes --vault-only` prints `WILL BE OVERWRITTEN: • database brain_qa_c …`, although the
  database is never touched.
- Root cause, two halves:
  1. `backup/restore.py:355` calls `summary_counts(conn)` whatever `db_leg` is. The vault side
     is already scoped one line below (`target_vault_files = … if vault_leg else 0`), which is why
     `--db-only` previews correctly in C24 and `--vault-only` does not.
  2. `cli_backup.py:204-208` (`_print_preflight`) prints the database block unconditionally.
- Consequence beyond the display: `Preflight.target_is_non_empty` (`restore.py:108-110`) reads
  `target_documents`. So `--vault-only` against an empty vault but a non-empty database demands the
  typed phrase for a database it will not touch. On `--db-only` the vault is already excluded.

### F27: a declined or failed restore leaves `$BRAIN_HOME/run/restore-<ts>/` behind

- Symptom: every declined or aborted run leaves an unpacked archive (108K in QA; the full corpus
  dump plus vault tar on a real brain, in plaintext).
- Root cause: `prepare_restore` (`restore.py:195-197`) unpacks into
  `brain_home/run/restore-<ts>`. The only removal is `restore.py:679`, at the end of a successful
  `restore_backup`. These paths all leak the dir:
  - the y/N decline and the typed-phrase refusal (`_gate`, `typer.Abort`);
  - a blocked preflight (`Exit 3`);
  - `--vault-only` on a no-vault archive (`BadParameter` at `cli_backup.py:298`);
  - any `BrainError` from the connect or preflight;
  - any exception inside `restore_backup` (pg_restore failure, verification failure,
    `RestoreAborted`);
  - an `extract_archive` that rejects a hostile member, because `dest.mkdir` has already run
    (`archive.py:169`).
- Is the dir needed after a successful restore? No. Everything after `restore_backup` returns reads
  the in-memory `RestoreReport` and `state.manifest`. `extract_vault_tar` has already copied the
  vault out to `cfg.vault_path`. The `RestoreAborted` recovery is a SQL rename that needs no dump.
  So removal in a `finally` is safe on every path, including success, and `restore_backup` already
  removes it on success today.

## 2. Behaviour changes (exact)

### F22: the hint is built from what the restore used

- `RestoreReport` gains `restore_container: str | None`: the container `pg_restore` ran in, or
  `None` on the host leg. It is threaded out of `_restore_database`, which now also returns
  `plan.container`. It is required (no default). The only constructor is `restore.py:681`.
- New pure function in `cli_backup.py` (rendering lives there):
  `drop_database_command(database: str, *, database_url: str, container: str | None) -> str`.
  - **Container leg** (`container` set): `docker exec <container> psql -U <user> -d postgres -c 'DROP DATABASE <db>'`.
    This is printed only when `resolve_pg_tool` proved that the container publishes the DSN's port.
    That is the same server the parked database lives on.
  - **Host leg** (`container is None`): `psql -h <host> -p <port> -U <user> -d postgres -c 'DROP DATABASE <db>'`.
    There is **no** docker-exec form. When the target container could not be proved, we print none
    rather than guess one. The machine just ran a host `pg_restore` of the right major version, so
    the client tools (`psql`) are installed.
  - Credentials: it reads **only** `host`, `port` and `user` from `dsn_parts(database_url)`, an
    allowlist. `password`, `sslpassword` and every other key are never read, so no URL, password or
    `passfile` can reach the text. A flag whose key is absent is omitted (a socket DSN has no `-h`).
    `psql` then prompts for the password or uses `~/.pgpass`/`PGPASSWORD` as usual.
  - Every interpolated value (container, host, port, user, and the `-c` SQL) goes through
    `shlex.quote`. The database name is already `[a-z0-9_]+` (`db_names._validated_db_name`), so
    the SQL needs no identifier quoting. The `-c` argument is still shell-quoted.
- `_print_report` takes `database_url` and prints `drop_database_command(...)` in place of the
  literal. The JSON payload is **unchanged**, with no new key. A script reading
  `replaced_database` already knows its own server, so a copy-paste line is a human affordance.
  This keeps the JSON contract stable. If fix-lead wants parity, adding `"drop_command"` is a
  one-line change.

### F23: in `--json` mode stdout carries exactly one JSON object

- Decision: **prompts go to stderr in `--json` mode.** "Require `--yes` with `--json`" is rejected.
  It cannot work, because the typed phrase is unbypassable by any flag whenever the target is
  non-empty (`_gate` docstring, §5.9). A non-empty `--yes --json` restore must prompt anyway.
  Scripted restores already pipe the phrase on stdin (C24 does exactly that). Prompting on stderr
  keeps that working and makes stdout parseable.
- `_gate(check, *, yes, err)`: `typer.confirm(..., err=err)`, `typer.prompt(..., err=err)`, and
  the `Aborted — nothing was changed.` line goes to stderr when `err`. Human mode passes
  `err=False`, so the transcript is byte-identical.
- In `--json` mode a declined or refused gate (`typer.Abort`, including EOF on stdin) emits
  `{"restored": false, "aborted": true}` on stdout and exits **1**, the same code as today. Before,
  it produced no JSON at all.
- The missing-sidecar `[warn]` line goes to stderr in `--json` mode (stdout as before in human
  mode).
- Not changed (residual, noted): a generic `BrainError` after the gate still prints one line on
  stderr and leaves stdout empty (exit 1), as it does today and as the blocked/`RestoreAborted`
  JSON paths already distinguish.

### F24: the preview and the gate follow the scope

- `preflight`: `counts = summary_counts(conn) if (db_leg and conn is not None) else None`. This is
  the mirror of the existing vault line, so `--vault-only` reports 0/0/0 for the database.
- `_print_preflight(..., db_leg: bool)` is a new **required** keyword. The two existing nodb call
  sites in `tests/test_cli_backup.py` pass it. The database block prints only when `db_leg`. The
  vault block keeps its current condition: `vault_leg` is already folded into `target_vault_files`.
- Gate consequence, flagged for review because it is a gate change: `--vault-only` against an
  **empty vault** with a **non-empty database** no longer demands the typed phrase, so `--yes` is
  honoured. Why this is correct: the vault-only path never touches the database
  (`restore_backup(db_leg=False)` skips `_restore_database` and `brain init`). The gate's own
  contract is "the phrase is required whenever something would be destroyed". `--db-only` already
  behaves symmetrically for the vault. A non-empty vault still demands the phrase under
  `--vault-only`, and a non-empty database still demands it under the default and `--db-only`.

### F27: the staging dir is removed on every exit path, and only that dir

- New `discard_prepared_restore(state: PreparedRestore, cfg: Config) -> None` in
  `backup/restore.py`, exported from `brain.backup`. It removes `state.staging_dir` only when all
  of these hold:
  - `staging_dir.parent == cfg.brain_home / "run"` (lexical, the same expression
    `prepare_restore` builds it from);
  - the name full-matches `restore-\d{8}-\d{6}`. This is `TIMESTAMP_FORMAT`'s shape, and a test
    pins that a real `prepare_restore` name passes, so a format change cannot silently disable
    cleanup;
  - it is not a symlink.

  Otherwise it does nothing. `shutil.rmtree(..., ignore_errors=True)` is unchanged: a leftover dir
  is untidy, not dangerous, and must not mask the original error.
- `restore_backup`: the body after `state` is obtained runs in `try/finally:
  discard_prepared_restore(state, cfg)`. This replaces the success-only `rmtree` at line 679, so a
  direct API caller is covered too.
- `prepare_restore`: `extract_archive` is wrapped. On any exception the half-made dir is discarded
  (through the same guard) and the exception re-raised.
- `restore_cmd`: everything after `prepare_restore` returns moves into a helper
  `_restore_prepared(...)`. The command becomes `try: _restore_prepared(...) finally:
  discard_prepared_restore(state, cfg)`. This covers the gate, a blocked preflight, the
  `--vault-only`/no-vault `BadParameter`, and connect/preflight errors. The extraction also brings
  `restore_cmd` back under the 50-line function guideline.
- Success path: the dir is removed (as today), and the hint and JSON are unaffected because they
  read only `report` and `state.manifest`.

## 3. Affected files

| File | Now | Change |
|---|---|---|
| `src/brain/cli_backup.py` | 477 | `drop_database_command`, `_gate(err=)`, json abort object, scoped `_print_preflight`, `_restore_prepared` + `finally` (est. +60, stays under 800) |
| `src/brain/backup/restore.py` | 690 | `RestoreReport.restore_container`, `_restore_database` returns the container, scoped counts, `discard_prepared_restore`, `finally`s (est. +30 → ~720, under 800; it is listed as "deliberately absent" in `docs/ceiling-table.md` while under, which stays true) |
| `src/brain/backup/__init__.py` | 62 | export `discard_prepared_restore` |
| `tests/test_restore_gate.py` | 494 | DB-backed CLI regressions (est. +200 → ~700) |
| `tests/test_restore_hints.py` (new) | — | `nodb` unit tests: hint builder, staging guard, hostile-archive cleanup |
| `tests/test_restore_preflight.py` | 353 | scoped-count regression |
| `tests/test_cli_backup.py` | 296 | the two `_print_preflight` call sites pass `db_leg=True`; one `db_leg=False` regression |
| `docs/cli-reference.md` | — | restore section: `--json` prompts on stderr + the abort object; the drop hint; staging dir removed |
| `CHANGELOG.md` | — | the subsection below |

No file is over 800 before or after. `wc -l` will be checked after each edit, and
`scripts/check_ceiling_table.py` run before commit.

## 4. RED regression tests (each written first and seen failing on master's code)

DB-backed tests live in `tests/test_restore_gate.py` and reuse its sandbox, `cfg`
(`tmp_path` BRAIN_HOME/vault/backups), `archive`, `wired` (`RefusingConnectionFactory`), and
`StubAndRestoreRunner`. No real `pg_restore`, `psql` or `docker` ever runs. The DB is
`fix_d_test` (its restore sandbox is derived from it).

F22:
1. `test_drop_hint_names_the_container_the_restore_used`: the runner answers `docker port` with
   nothing and `docker ps` with `synthetic-qa-postgres\t0.0.0.0:<test port>->5432/tcp`, so the
   container leg resolves to that container. Asserts the hint contains `docker exec
   synthetic-qa-postgres psql` and not `second-brain-postgres`. RED: literal.
2. `test_drop_hint_on_the_host_leg_is_psql_against_the_dsn`: docker probes answer nothing. A stub
   `pg_restore`/`pg_dump` (non-executed files, `chmod +x`) sit on a `tmp_path` PATH so `which()`
   picks the host leg. The pre-restore backup is supplied through the existing
   `_inject_fake_runner` seam (`pre_backup=`), because `StubAndRestoreRunner` does not materialise
   a host-leg `-f` dump. Asserts `psql -h localhost -p <port> -U <user> -d postgres`, `DROP
   DATABASE <parked>`, and no `docker exec`. RED.
3. (nodb, `test_restore_hints.py`) `drop_database_command`:
   - a URL DSN and a keyword DSN, each carrying a distinctive synthetic `password=` and
     `sslpassword=`; neither value appears in the output;
   - the container form vs the host form;
   - a socket DSN (no host/port) omits `-h`/`-p`;
   - a host with a space is shell-quoted.

F23:
4. `test_json_with_typed_phrase_keeps_stdout_pure_json`: seeded live doc, `--yes --json`, the
   phrase on stdin. `json.loads(result.stdout)` succeeds and `restored is True`. The prompt text is
   in `result.stderr`. RED (C24's exact symptom).
5. `test_json_declined_emits_an_abort_object`: `--json`, stdin `n\n`. Stdout is
   `{"restored": false, "aborted": true}`, exit 1, `runner.calls == []`. RED.
6. `test_json_missing_sidecar_warning_goes_to_stderr`: stdout still parses. RED.

F24:
7. `test_vault_only_preview_does_not_list_the_database`: seeded live doc and a non-empty vault,
   `--vault-only`, stdin `n\n`. The output has `• vault` and no `• database`. RED.
8. `test_vault_only_with_empty_vault_does_not_gate_on_the_database`: seeded live doc, empty vault,
   `--yes --vault-only`, empty stdin. Exit 0, no typed-phrase prompt, and
   `live_document_count(...)` unchanged (the database untouched). RED (today: phrase demanded,
   EOF, exit 1).
9. (`test_restore_preflight.py`) `test_db_leg_disabled_reports_no_database_counts`: seeded doc,
   `db_leg=False`, so `target_documents == 0` and `target_is_non_empty is False`. RED.
10. (`test_cli_backup.py`, nodb) `_print_preflight(..., db_leg=False)` prints no `• database`. RED
    (the TypeError on the new kwarg counts as RED for the signature. The assertion is what is
    proved after green, and a mutation that drops the condition is run to show it fails).

F27, each asserting `not list((cfg.brain_home / "run").glob("restore-*"))`:
11. `test_declined_restore_leaves_no_staging_dir` (`n\n`). RED.
12. `test_refused_typed_phrase_leaves_no_staging_dir` (seeded, `y\nnope\n`). RED.
13. `test_blocked_restore_leaves_no_staging_dir` (dim mismatch, exit 3). RED.
14. `test_vault_only_on_no_vault_archive_leaves_no_staging_dir` (exit 2). RED.
15. `test_failed_pg_restore_leaves_no_staging_dir`: the runner raises `CalledProcessError` on
    `--exit-on-error`, the real `pg_restore` call only. Exit 1, the live database untouched. RED.
16. `test_successful_restore_removes_staging_dir_and_still_prints_the_hint`. This guards the
    success path. It is green today and pins that the `finally` did not move the removal before
    the hint.
17. (nodb) the `discard_prepared_restore` guard:
    - it removes a real `prepare_restore` dir. Its name is produced by `prepare_restore` with a
      fixed clock, so a `TIMESTAMP_FORMAT` drift fails the test;
    - it leaves a lookalike `restore-…` dir outside `brain_home/run` intact, with a sentinel file
      inside;
    - it leaves a non-matching name under `run/` intact;
    - it leaves a symlink under `run/` pointing at a sentinel dir intact, with the target's
      contents surviving.
18. (nodb) `prepare_restore` on a hostile archive (valid manifest plus a `../escape` member, no
    sidecar) raises `BackupError` and leaves no `restore-*` dir. RED.

Each fix is also mutation-checked after green: revert just that hunk and see its test fail
(project rule "prove the check can fail").

Commands (all under `.claude/full-suite.lock`, `df -g /` ≥ 7 first):
`TEST_DATABASE_URL=postgresql://brain:brain@localhost:5434/fix_d_test pytest -p no:cacheprovider
tests/test_restore_hints.py tests/test_restore_gate.py tests/test_restore_preflight.py
tests/test_cli_backup.py tests/test_restore_swap.py tests/test_restore_db_name_budget.py
tests/test_log_privacy_rules.py tests/test_log_privacy.py tests/test_import_hygiene.py
tests/test_repo_hygiene_files.py --no-cov`. Then `ruff check`, `mypy src/` and
`scripts/check_ceiling_table.py`. `fix_d_test` is dropped by exact name at the end.

## 5. Risks

- **Gate relaxation (F24).** This is the only change that lets `--yes` through where it was
  refused before. It is limited to `--vault-only` with an empty vault, and the database is provably
  untouched on that path. Test 8 asserts the database count is unchanged, and the existing
  `test_yes_flag_cannot_bypass_typed_phrase_on_non_empty_target` (default scope) must stay green.
- **Deletion (F27).** The new `rmtree` call sites can only reach a path that is lexically
  `brain_home/run/restore-YYYYMMDD-HHMMSS` and not a symlink. Test 17 proves the three refusals
  and the one acceptance. Existing behaviour already ran `rmtree` on the same path at
  `prepare_restore` start and on success.
- **Hint correctness (F22).** The container form is only as good as `resolve_pg_tool`'s
  port-publish proof. That proof is already the safety basis for running `pg_restore` itself
  there, so the hint is never more trusting than the restore was.
- **JSON contract.** Additive only: a new abort object where there was no JSON. The success and
  blocked objects are unchanged. `test_json_result_object_has_the_documented_keys` stays green.
- **Concurrency.** Two restores started in the same second share one staging name. This is
  pre-existing, not introduced, and out of scope.

## 6. CHANGELOG (inserted immediately before `### Added — \`brain tags summarize\`, a cited synthesis of one tag (RM-55)`)

```markdown
### Fixed — `brain restore` names the server it restored, keeps `--json` clean, and tidies up (F22, F23, F24, F27)

- After a database restore, the "drop it when satisfied" line named the production container
  (`docker exec second-brain-postgres …`) whatever server the restore ran against, so pasting it
  could reach the wrong server. It now names the container `pg_restore` actually ran in. When the
  host's `pg_restore` was used it prints `psql -h <host> -p <port> -U <user>` for `DATABASE_URL`
  instead, with no `docker exec` and never the password.
- `restore --json` printed its confirmation prompts on stdout, so the output was not JSON. In
  `--json` mode prompts and warnings now go to stderr. The typed phrase still cannot be skipped by
  any flag, so a non-empty restore reads it from stdin as before. A declined run prints
  `{"restored": false, "aborted": true}` and exits 1.
- `restore --vault-only` listed the database under "WILL BE OVERWRITTEN" and counted it toward the
  typed-phrase gate, though it never touches the database. The preview and the gate now follow the
  scope, as `--db-only` already did for the vault.
- A declined, refused or failed restore left the unpacked archive in
  `$BRAIN_HOME/run/restore-<timestamp>/`: a plaintext copy of the dump and vault. It is now
  removed on every exit. The removal only touches a `restore-<timestamp>` directory directly under
  `$BRAIN_HOME/run`, never a symlink.
```

## 7. As implemented (deviations from §2–§4, recorded after review approval)

- **F23, how the prompts reach stderr.** `typer.prompt(..., err=True)` is not enough. The vendored
  Click prints the prompt text to stderr, but it still passes the prompt's last character to
  `input()`, and that writes to stdout. In `--json` mode `_gate` therefore uses
  `_read_line_on_stderr`: the prompt goes to stderr and the answer is read with
  `sys.stdin.readline()`. EOF aborts, as with Click. The y/N answer must be `y` or `yes`. Human
  mode still uses `typer.confirm` / `typer.prompt` unchanged. Each F23 test asserts that
  `result.stdout.startswith("{")`.
- **F27, the symlink leg.** The mutation run showed that an explicit `is_symlink()` check changed
  nothing, because `shutil.rmtree` already refuses a symlinked root (with `ignore_errors` the error
  is swallowed and nothing is deleted). The redundant check was removed. The docstring states the
  property, and `test_discard_leaves_a_symlink_and_its_target` pins it.
- **F27, the API-level `finally`.** `restore_backup`'s own `finally` is covered by a direct test,
  `test_failed_restore_backup_removes_its_own_unpack`. That is a vault-only restore whose
  `pre_backup` raises. The CLI's `finally` would otherwise mask a regression there.
- **Structure.** `restore_backup`'s body after the unpack is now `_apply_restore`.
  `restore_cmd`'s body after `prepare_restore` is now `_restore_prepared`, which calls
  `_run_preflight`, `_show_preflight` and `_result_payload`. The `try/finally` wraps one call in
  each place.
- **Test layout.** The CLI regressions live in a new `tests/test_restore_qa_c24.py`, not in
  `test_restore_gate.py` (which would have passed 800 lines). The new file imports that module's
  fixtures, including its autouse sandbox reset, and marks each fixture parameter `# noqa: F811`,
  following `tests/test_cli_ingest_gmail_attachments_sec117.py`. `_inject_fake_runner` gains an
  optional `pre_backup=`.
- **F22, the secret at CLI level.** The test server enforces its password, so the CLI-level test
  adds a synthetic `sslpassword` (inert without a client key) and asserts three things: that value,
  the full DSN, and the word "password" are all absent from human and JSON output. The unit tests
  cover a synthetic `password=` in both URL and keyword DSNs.

## 8. Not in scope (for the backlog)

- **Same-second staging collision.** Two restores started in the same second share
  `$BRAIN_HOME/run/restore-<ts>`, built at `src/brain/backup/restore.py:225`. `prepare_restore`
  `rmtree`s that path before unpacking, and the first run's `finally` removes the second run's
  unpack. Fix: add a unique suffix (pid or uuid), and widen `_STAGING_NAME_RE` to match.
- **`--json` error paths with empty stdout.** These end non-zero with stdout empty and the
  error on stderr. The docs and CHANGELOG now promise only "stdout carries nothing but JSON":
  - the unpack `BrainError` at `src/brain/cli_backup.py:371`;
  - the `--vault-only` on a no-vault archive `BadParameter` at `:403`;
  - the generic `restore_backup` `BrainError` at `:446`;
  - the connect/preflight `BrainError` at `:472`.

  Fix: emit `{"restored": false, "error": "<class-only message>"}`. Messages can carry paths, so
  decide what is safe to print first.

## 9. Review round 1 (rev-139)

- The `assert runner.calls == []` that `test_blocked_restore_emits_json_issues` lost when the
  new tests were cut out of `test_restore_gate.py` is restored.
- The docs and CHANGELOG no longer promise "exactly one JSON object" (see §8).
- `_read_line_on_stderr` turns Ctrl-C into `typer.Abort`, so a `--json` run still prints the
  `aborted` object. The test is `test_json_ctrl_c_at_the_prompt_emits_the_abort_object`.
- Test-safety hardening. `tests/conftest._looks_like_prod_db` used to refuse a prod port only for
  five literal local host spellings, and `127.1`, `localhost.`, `::ffff:127.0.0.1`,
  `127.0.0.2` and `host.docker.internal` all passed on 55432. It now refuses a prod port on ANY
  host. The two tests that pinned the old scoping are inverted. `test_restore_real_pg_tools.py`
  refuses its `ADMIN_DSN` at import.
