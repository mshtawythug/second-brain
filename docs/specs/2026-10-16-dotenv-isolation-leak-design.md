# Dotenv isolation leak between test modules — design

**Status:** implemented on `test/dotenv-isolation-leak`.
**Plan:** `docs/plans/2026-10-16-dotenv-isolation-leak.md`.

## 1. The bug

When `tests/test_config_dotenv_chain.py` runs before `tests/test_cli_doctor.py`,
seven doctor tests fail. Each one sees `BRAIN_EMBEDDER=none`. Run alone, or in
the alphabetical order CI uses (doctor first), they pass:

```
pytest tests/test_cli_doctor.py                                   # 39 passed
pytest tests/test_config_dotenv_chain.py tests/test_cli_doctor.py # 7 failed, 46 passed
```

The failures were `test_doctor_pings_ollama`, `test_doctor_warns_when_model_missing`,
`test_doctor_reports_ollama_down`, `test_doctor_json_shape_when_healthy`,
`test_doctor_json_exits_nonzero_and_marks_fail_when_ollama_down`,
`test_doctor_failing_probe_does_not_suppress_other_checks` and
`test_doctor_external_probe_output_order_is_deterministic`, at base `210d288`.

## 2. Root cause

Bisected one chain test at a time against
`test_cli_doctor.py::test_doctor_pings_ollama`. Exactly one test leaks:
`test_found_but_incomplete_dotenv_reports_loaded_not_missing`.

1. It writes `BRAIN_EMBEDDER=none` (and no `DATABASE_URL`) into the
   `$BRAIN_HOME/.env` that its `chain_paths` fixture points `_brain_home_dotenv` at.
2. It calls `Config.load()`. `Config._load_field_dict` resolves the chain and
   runs `os.environ.setdefault(key, value)` for every merged key
   (`src/brain/config.py`, the loop after `_resolve_dotenv_chain()`). That
   happens **before** the `DATABASE_URL` check, so `BRAIN_EMBEDDER=none` is in
   the process environment when `ConfigError` is raised.
3. `monkeypatch` restores only the keys it was told about. `chain_paths` calls
   `monkeypatch.delenv` for `DATABASE_URL` and `BRAIN_HOME`, so it never
   recorded `BRAIN_EMBEDDER`. (A `delenv(..., raising=False)` of an absent key
   records nothing either, so pre-emptively deleting it would not have helped.)
   The key outlives the test.
4. Every later test in the session reads `BRAIN_EMBEDDER=none`. The doctor
   tests that expect the Ollama probe get the FTS-only branch instead.

The other chain tests also run `Config.load()` against test-written dotenvs,
but they only write `DATABASE_URL`. The session fixture `_pin_test_database_url`
has already put `DATABASE_URL` into the environment, so `setdefault` changes
nothing and `monkeypatch.delenv` restores it. They are safe only by that
coincidence.

## 3. Where the bug lives: test isolation, not production

The task posed the question CLAUDE.md rule 13 asks: does production mutate
`os.environ` as a side effect a library caller would not expect?

It does mutate it, but on purpose, and other code depends on it:

* The behaviour is documented at the call site. The comment above the
  `setdefault` loop sets out the precedence contract: process env is never
  overwritten, and dotenv values sit beneath it.
* Code outside `brain.config` reads knobs straight from `os.environ` *after*
  `Config.load()`. A value in a user's `.env` reaches it only because the
  loader published it:
  `maintenance.py` reads `BRAIN_WIKI_KEEP_BUILDS` on the line after
  `cfg = Config.load()`. `mcp_server.py` reads `BRAIN_MCP_LOG_LEVEL`,
  `bin/monitor.py` reads `BRAIN_WIKI_PORT`, and `setup.py` reads `VOYAGE_API_KEY`.
* Subprocesses inherit it. `wiki/build_swap.py`, `wiki/build_partial.py`,
  `backup/pgtool.py`, `cli_claude.py` and `bin/_launcher.py` pass `os.environ`
  (or a copy) to child processes, so a `.env` value configures them too.

Making `Config.load` pure would silently stop those `.env` knobs from working.
That is a behaviour change to production with no bug report behind it. The
defect is that the tests let a process-global side effect cross a test
boundary. **The fix is in the test harness.** No production code changes, so
the change gets no CHANGELOG entry (the same as #23, #31 and #35).

## 4. The fix

### 4.1 A session-wide `os.environ` guard

New plugin module `tests/env_isolation.py`, loaded from `tests/conftest.py`'s
`pytest_plugins`. It works the same way as `tests/browser_timeout.py`, and lives
in its own module because conftest is already past the 800-line ceiling. It holds:

* `restored_environ()` is a context manager. It snapshots `os.environ`, and on
  exit deletes every key that was added and puts back every key that was
  changed or removed. It touches only the keys that differ and never calls
  `os.environ.clear()`, so a background thread still running at teardown never
  sees an empty environment.
* `_restore_os_environ` is an autouse, function-scoped fixture that wraps every
  test in `restored_environ()`.

**Why session-wide, not "for config tests".** The leak is not specific to the
config test modules. Any test that runs `Config.load()`/`load_minimal()`
against a dotenv it wrote can publish a key, and that includes every CLI test
that goes through `CliRunner`. So can `brain setup`, which assigns
`BRAIN_HOME`/`BRAIN_VAULT_PATH`. Scoping the guard to the tests that call
`isolate_config_dotenv` would leave out exactly the tests that are not obviously
config tests. It also cannot be done with `monkeypatch` alone, because
`monkeypatch` restores only the names it is given, and the leaked names are
whatever a test's `.env` happened to contain.

**Ordering with other fixtures.** Higher-scoped fixtures are set up before
function-scoped ones, so values that session and module fixtures install
(`DATABASE_URL`, `BRAIN_VAULT_PATH`, `BRAIN_HOME`, the launchctl stubs) are part
of the snapshot and survive. `monkeypatch`'s undo and the guard's restore
commute: whichever runs last, the environment ends at the pre-test snapshot.

**Cost.** One `dict(os.environ)` copy and one comparison per test.

### 4.2 Regression tests (`tests/test_env_isolation.py`)

1. **Mechanism, in one test.** Inside `restored_environ()`, a real
   `Config.load_minimal()` against a dotenv holding a sentinel key does publish
   the key. This shows the leak path still exists, so the test is not vacuous.
   After the block the key is gone, a changed key has its old value back, and a
   deleted key is restored.
2. **Across a real test boundary, in a fixed order.** `pytester` runs two inner
   tests in a fixed order. `test_a` publishes a sentinel through
   `Config.load_minimal()`, the same way the chain test leaks.
   `test_b` asserts the sentinel is absent. With `-p tests.env_isolation`, both
   pass. A built-in negative control runs the same pair without the plugin:
   `test_b` must fail. That proves the inner assertion can fail and that the
   leak still reproduces.
3. **Wiring.** The outer session's `request.fixturenames` contains
   `_restore_os_environ`. Without this, deleting the `pytest_plugins` entry
   would leave test 2 green, because test 2 loads the plugin itself, while the
   real suite went unguarded.

The end-to-end check is the bad-order run in §1, which must now be green.

### 4.3 DRY: migrate the inlined dotenv blocks to `isolate_config_dotenv`

`isolate_config_dotenv` gains one keyword, `brain_home_root: Path | None`. When
given, it also patches `_brain_home_root` to return that path. Five of the
inlined blocks patch it, so without the keyword none of them could migrate
exactly.

A block is migrated only when the conversion keeps its isolation exactly: the
same chdir target, the same patched paths or equivalent non-existent files, the
same variables cleared, and the same `_brain_home_root` result. Re-derive the
set with `git grep -l '"_brain_home_dotenv"' -- tests`.

| Module | Differs from the helper by | Outcome |
|---|---|---|
| `test_config_enrich_envvars.py` | nothing | migrated |
| `test_init_auto_runs_backfill.py` | nothing | migrated |
| `test_quartz_email_thread_static.py` | nothing | migrated |
| `test_search_canary_queries.py` | nothing | migrated |
| `test_search_floor_default_excludes_known_bad.py` | nothing | migrated |
| `test_search_vector_floor.py` | nothing | migrated |
| `test_t5_llm_keep_alive.py` | nothing | migrated |
| `test_config_audio_envvars.py` | `clear` list; `setenv DATABASE_URL` | migrated (`clear=`, setenv kept after) |
| `test_config_snippet_max_chars.py` | `_brain_home_root`; clear; setenv | migrated (`brain_home_root=`) |
| `test_config_mcp_ceilings.py` | `_brain_home_root`; clear list; setenv | migrated |
| `test_config_new_fields.py` | `_brain_home_root` (returned); clear; setenv | migrated |
| `test_embeddings_none.py` | `_brain_home_root`; clear | migrated |
| `test_config.py` `isolated_dotenv` | `_brain_home_root`; clear list | migrated |
| `test_config.py` `test_env_var_branch_via_config_load` | both paths `nope.env`; sets `BRAIN_HOME` | migrated: helper first, then `setenv BRAIN_HOME`. Two distinct non-existent files are equivalent to one. |
| `test_config_dotenv_chain.py` `chain_paths` | chdir into `tmp/cwd` (not `tmp`); paths in sub-directories; deletes `DATABASE_URL`; `_brain_home_root` = the brain-home **dir** | **left inline.** It is not an isolation block. It builds the three-link chain that the tests inspect, and the tests assert on its exact paths. |
| `test_config_dotenv_chain.py` `test_dotenv_chain_collapses_duplicate_candidates` | re-points `_brain_home_dotenv` *at the project file* | **left inline.** This is the scenario under test, not isolation. |

The helper's docstring is updated to name the new keyword and the two remaining
inline sites with their reasons. It also says that keys `Config.load`
publishes from a test's `.env` are undone by the guard in §4.1, not by
`monkeypatch`.

## 5. Not in scope

* **The cwd walk-up inside a worktree.** A worktree at
  `<checkout>/.claude/worktrees/<name>` walks up into `<checkout>/.env`. That is
  the developer's real config. `tests/conftest.py`'s module-level `load_dotenv()`
  and any un-isolated `Config.load()` read it there. CI has no such file, and
  the guard stops its keys from crossing test boundaries, but a test still sees
  them within itself. This is recorded here and not fixed, because changing the
  walk-up is a production decision (see `_cwd_dotenv_enabled`).
* A pytest-randomly dependency. The suite does not install it. Ordering checks
  use explicit file orders.
