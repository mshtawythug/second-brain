# F25: an installed `brain` writes `brain owner` and `brain eval` files under `$BRAIN_HOME`

- **Finding:** F25 (MED), QA report row C19 (`brain-qa/REPORT.md`, evidence `logs/c/C19.txt`).
- **Branch:** `fix/wheel-install-paths`, worktree `.claude/worktrees/fix-e-wheel-paths`.
- **Base:** `6c078c1f` (local `master` == `second-brain/master` at worktree creation).
- **Test DB:** `fix_e_test`. Only the eval CLI tests need it.

## 1. The bug, with evidence

C19 ran a non-editable wheel install (`venv-ef824ed1`) with `BRAIN_HOME` set to a lane home.
`qbrain owner set 'qa-owner@example.com, QA Owner'` created
`venv-ef824ed1/lib/python3.11/.env` (it did not exist before). The lane's `$BRAIN_HOME/.env` was
never touched. `owner show` then read the value back from the venv file. Root causes:

| Site | Code | Under a wheel install it resolves to |
|---|---|---|
| `src/brain/cli.py:7910` `_owner_dotenv_path()` | `_config_module._project_dotenv()` | `<venv>/lib/python3.11/.env` |
| `src/brain/config.py:699` `_project_dotenv()` | `Path(__file__).resolve().parent.parent.parent / ".env"` | (same, since `config.py` is at `site-packages/brain/config.py`) |
| `src/brain/cli.py:371` `_BASELINES_DIR` | `cli.py` parents ×3 `/ tests/eval/baselines` | `<venv>/lib/python3.11/tests/eval/baselines` (`--record-baseline` **writes** here) |
| `src/brain/eval/corpus.py:20` `_DEFAULT_CORPUS_PATH` | parents ×4 `/ tests/eval/golden_corpus.yaml` | `<venv>/lib/python3.11/tests/eval/golden_corpus.yaml` |
| `src/brain/eval/answer_eval.py:29` `_DEFAULT_ANSWER_CORPUS_PATH` | parents ×4 `/ tests/eval/answer_corpus.yaml` | same dir (`brain eval --answer`) |
| `src/brain/eval/concept_extraction.py:82` `_DEFAULT_FIXTURE_PATH` | parents ×4 `/ tests/eval/concept_extraction_fixture.yaml` | same dir. It has no CLI surface and is changed only so all four eval defaults share one helper (DRY). |

All four eval defaults are evaluated at import time.

## 2. Detection: reuse the one heuristic that already exists

`config._brain_home_root` (config.py:708) already decides "dev checkout or not" this way:
`<config.py>/../../../pyproject.toml` exists means a source checkout. An editable install
(`pip install -e`, which is how `ci.yml`, `eval.yml` and `benchmark.yml` install) has `__file__` in
the checkout, so it counts as a checkout. A non-editable wheel (`site-packages/brain/...`,
including the daemons' `uv tool` install from `daemon-src`) does not. A non-editable install into
a venv that sits *inside* a checkout (`<repo>/.venv/lib/python3.11/site-packages/brain`) does not
count either, because its parents ×3 is `.venv/lib/python3.11`. That is correct: the package is
not running from the checkout.

**Decision:** I will not add a second heuristic. I will extract the existing one into a function
that both `_brain_home_root` and the new call sites use:

- `checkout_root_for(module_file: Path) -> Path | None` is pure. It returns
  `module_file.parent.parent.parent` when that directory holds `pyproject.toml`, else `None`. It
  does not call `resolve()`; callers resolve. That keeps `_brain_home_root(_config_file=...)` and
  its existing test seam byte-for-byte equivalent.
- `source_checkout_root() -> Path | None` is `checkout_root_for(Path(__file__).resolve())` for the
  running package. This is **the seam the tests drive with `monkeypatch`**.

`setup._is_repo_checkout(directory)` asks a different question: is `$BRAIN_HOME` itself a
checkout? It stays as it is.

## 3. Scope decision: fix the call sites, not `_project_dotenv` or the dotenv chain

`_project_dotenv()` is link 2 of `Config.load`'s chain (`_resolve_dotenv_chain`, config.py:839).
It is the **highest-precedence file** and it outranks `$BRAIN_HOME/.env`. It is also read by
`provision_brain_home_dotenv` (setup.py:611) and rendered by `brain doctor` through
`dotenv_chain()`. About 40 tests patch it. Making it return `$BRAIN_HOME/.env` for a wheel would
reorder precedence for every command on a wheel install: `$BRAIN_HOME/.env` would jump above the
cwd walk-up. It would also change doctor's chain rendering. That is a config-resolution change,
not a fix for "writes land in the venv".

**Decision:** leave `_project_dotenv()` and the chain exactly as they are. Fix only the writers and
the defaults:

- `brain owner set/add/remove` write `writable_dotenv()`. That is `_project_dotenv()` in a checkout
  (unchanged) and `_brain_home_dotenv()` otherwise. The dev path still goes through
  `config._project_dotenv`, so every existing owner test that patches it keeps working.
- The eval defaults derive from `eval_data_dir()`. That is `<checkout>/tests/eval` in a checkout
  (the same paths as today) and `_brain_home_root() / "eval"` otherwise.
- All of these are resolved **lazily at call time** and never cached, following
  `config.default_backup_dir()`. A `BRAIN_HOME` change is then honoured, and tests can steer them.

**Residual, documented rather than fixed here.** A wheel user who already ran `owner set` before
this fix has a stale `<venv>/lib/python3.X/.env`. Link 2 still reads it, and it outranks
`$BRAIN_HOME/.env`. Until they delete it, `owner show` keeps reporting the stale list. The
CHANGELOG carries an upgrade line for that. I checked: `brain doctor` does **not** list the whole
chain (doctor_runtime.py:222 looks up only the `$BRAIN_HOME/.env` link), so the CHANGELOG names the
path itself rather than pointing at doctor.

`docs/configuration.md` row 2 says `<repo>/.env` is "Source checkouts only", but the code includes
it unconditionally. Gating link 2 on `source_checkout_root()` would close the residual and make
the doc true. It would take about 3 lines in `_resolve_dotenv_chain` plus 1 in
`provision_brain_home_dotenv`, so that setup never symlinks `$BRAIN_HOME/.env` at a stale venv
file. That is a read-precedence change, so I propose it as a **follow-up**. Fix-lead can pull it
into this PR. If so, I would add a chain test for a simulated wheel and a provision test.

## 4. Behaviour change, exactly

Inside a source checkout (editable install) **nothing changes**:

- `owner` writes `<repo>/.env`, even when `BRAIN_HOME` points elsewhere.
- The eval defaults stay `<repo>/tests/eval/{golden_corpus.yaml,answer_corpus.yaml,baselines/,concept_extraction_fixture.yaml}`.
- The eval gate (`eval.yml`'s `ci.json` + `golden_corpus.yaml` self-arming, `--fail-below`) and
  `ci.json`'s bytes are unaffected. No baseline re-record is triggered, because this is not a
  ranking, corpus or config-signature change.

Outside a checkout (non-editable install):

- `owner set/add/remove` read and write `$BRAIN_HOME/.env` (`~/.brain/.env` when `BRAIN_HOME` is
  unset), never anything under the package. `owner show` is unchanged and still goes through
  `Config.load`, which reads `$BRAIN_HOME/.env` as link 4.
- If `$BRAIN_HOME` does not exist, the write fails through the existing
  `error: failed to write <path>: ...` path with exit 1. The command does not create directories:
  `brain setup` owns creating `$BRAIN_HOME`.
- `brain eval` defaults to `$BRAIN_HOME/eval/golden_corpus.yaml`. `--record-baseline`/`--baseline`
  use `$BRAIN_HOME/eval/baselines/`. `--answer` uses `$BRAIN_HOME/eval/answer_corpus.yaml`.
  `--corpus` still overrides.

**One hardening, applied in both modes: the owner writer writes through a symlink.**
`provision_brain_home_dotenv` deliberately makes `$BRAIN_HOME/.env` a symlink to a checkout's
`.env` (the dev case). This user's daemons run a non-editable install with `BRAIN_HOME=~/.brain`.
After this fix, `owner set` from that install targets `$BRAIN_HOME/.env`. The current writer does
`os.replace(tmp, path)`, which would replace the link with a regular file. That silently forks the
config, which is the drift the symlink exists to prevent. So `_owner_write_dotenv` resolves `path`
first and writes the target, with the sidecar `.tmp` placed beside the target. In a checkout this
is a no-op unless the repo `.env` itself is a symlink. In that case the old behaviour (replace the
link with a copy) was the same bug. Fix-lead can drop this item, but then the wheel-plus-symlink
case regresses into drift.

## 5. Files

| File | Change |
|---|---|
| `src/brain/install_paths.py` (**new**, ~70 lines) | One-line docstring: "Where user-writable files live: the source checkout in dev, `$BRAIN_HOME` in an installed wheel." Contains `checkout_root_for`, `source_checkout_root`, `writable_dotenv`, `eval_data_dir` and `eval_baselines_dir`. Imports only `pathlib` at module level. `brain.config` is imported **lazily inside** `writable_dotenv`/`eval_data_dir`, following `log_rotation.py`/`vault/owed_work.py`, because `config` imports `checkout_root_for` from here and a module-level import would form a cycle. |
| `src/brain/config.py` | `_brain_home_root` calls `checkout_root_for(_config_file or Path(__file__).resolve())` in place of its inline 3-line check. It adds one import line. The net line change is ≤ 0. The docstring keeps its priority list and names the shared helper inline, adding no lines. |
| `src/brain/cli.py` | `_owner_dotenv_path()` returns `writable_dotenv()`, and its docstring is reworded in place. `_owner_write_dotenv` resolves the path (write-through, §4). The `_BASELINES_DIR` constant and its 5-line comment are deleted. `eval_cmd` uses `eval_baselines_dir()` at its two sites and `default_corpus_path()` in place of `_DEFAULT_CORPUS_PATH`. The `eval` docstring's `tests/eval/...` mentions are reworded in place to "`tests/eval/` in a checkout, `$BRAIN_HOME/eval/` otherwise". Target: **net-negative**, about −5, so no growth record is owed. |
| `src/brain/eval/corpus.py` | `_DEFAULT_CORPUS_PATH` becomes `default_corpus_path()`, which is `eval_data_dir() / "golden_corpus.yaml"`. `load_corpus(None)` calls it. The comment is updated. |
| `src/brain/eval/answer_eval.py` | `_DEFAULT_ANSWER_CORPUS_PATH` becomes `default_answer_corpus_path()`. |
| `src/brain/eval/concept_extraction.py` | `_DEFAULT_FIXTURE_PATH` becomes `_default_fixture_path()`. |
| `tests/test_cli_eval.py` (4 sites), `tests/test_eval_stage_skips.py` (1), `tests/test_eval_fail_below.py` (1) | `monkeypatch.setattr("brain.cli._BASELINES_DIR", d)` becomes `monkeypatch.setattr("brain.cli.eval_baselines_dir", lambda: d)`. This is mechanical. |
| `tests/test_eval_corpus.py` | Its 4 `_DEFAULT_CORPUS_PATH` references (one in a `skipif`) become `default_corpus_path()`. |
| `tests/test_install_paths.py` (**new**) | The regression and pin tests in §6. |
| `tests/test_cli_eval.py` | One added DB-backed CLI test (§6, R5). |
| `docs/eval-gate.md`, `tests/eval/baselines/README.md` | One sentence each: in an installed wheel the defaults are `$BRAIN_HOME/eval/...`, and the gate described here is a checkout workflow and is unchanged. |
| `docs/configuration.md`, `skills/brain-graph/SKILL.md` | The `brain owner` row and paragraph name the file written: the repo `.env` in a checkout, `$BRAIN_HOME/.env` otherwise, written through a symlink. |
| `docs/ceiling-table.md` | `cli.py` row: promote the trailing descriptive hop ("the empty-daily-brief commit") to `0f32826d` at 8,704, then add a descriptive net-negative hop for this change. `config.py` row: promote "the phase-9 (R3) docs commit ..." to `cbe2f54c` at 2,669 (untouched since), then add a descriptive net-zero or negative hop. I will re-derive both with `git log --oneline` and `git show "${sha}:path" \| wc -l` at commit time. |
| `CHANGELOG.md` | The §8 text goes immediately before `### Added — opt-in query rewriting, \`--rewrite\` (RM-54)`. |
| Auto-memory `cli.md` | Note where `owner` writes and the eval defaults (CLAUDE.md rule 11). |

New module size is ~70 lines, well under 800. If `cli.py` or `config.py` came out net-positive
after all, each growth would get an inline reason at the line and a docstring pointer, per
`docs/ceiling-table.md`. The plan is to avoid that.

## 6. Tests (TDD: RED first, then fix, then green)

The shared fixture `wheel_install` in `tests/test_install_paths.py` builds a **package dir that is
not inside a repo**: `tmp/venv/lib/python3.11/site-packages/brain/`, with no `pyproject.toml`
anywhere above it inside `tmp`. Then it:

- monkeypatches `brain.install_paths.source_checkout_root` to
  `lambda: checkout_root_for(fake_pkg / "install_paths.py")`, which drives the **real** predicate
  against the fake layout, so it returns `None`;
- monkeypatches `brain.config._project_dotenv` to `lambda: tmp/venv/lib/python3.11/.env`, the path
  it really resolves to under a wheel;
- sets `BRAIN_HOME=tmp/brain_home` (created) and `HOME=tmp/fakehome`, so `Path.home()` can never reach a
  real home;
- sets `BRAIN_IGNORE_CWD_DOTENV=1`, chdirs to an empty `tmp/cwd`, and unsets
  `BRAIN_OWNER_PARTICIPANTS`.

Everything lives under `tmp_path`. No test touches a real home, `~/.brain` or the repo `.env`.

The RED tests below fail on today's code. Each one asserts the specific bad destination, not just
"something differs".

| # | Test | Proves | RED today because |
|---|---|---|---|
| R1 | `test_owner_set_writes_brain_home_dotenv_on_wheel_install` | `owner set "Pat Example,fixture@example.com"` writes the quoted, normalised line to `tmp/brain_home/.env`. `tmp/venv/lib/python3.11/.env` does **not** exist, and nothing new appears anywhere under `tmp/venv` (rglob snapshot before and after). | The writer targets `_project_dotenv()`, which is the venv file. |
| R2 | `test_owner_add_remove_then_show_on_wheel_install` | `add` then `remove` round-trip in `tmp/brain_home/.env`. `owner show` (with a dummy `DATABASE_URL`, no connection) lists exactly the remaining entries. The venv file is never created. | Same as R1. |
| R3 | `test_owner_write_keeps_brain_home_symlink` | `tmp/brain_home/.env` is a symlink to `tmp/dotfiles/brain.env`. After `owner set`, it is **still a symlink**, the target holds the line, and no `.env.tmp` is left behind. | The write goes to the venv. With only the path fix, the link would be replaced by a file, so this also pins the §4 hardening. |
| R4 | `test_eval_defaults_resolve_under_brain_home_on_wheel_install` | `eval_data_dir()`, `eval_baselines_dir()`, `default_corpus_path()`, `default_answer_corpus_path()` and the concept default all sit under `tmp/brain_home/eval`. `load_answer_corpus()` with no argument raises `EvalCorpusError` naming `tmp/brain_home/eval/answer_corpus.yaml`. | Today `load_answer_corpus()` silently loads the repo's committed file, and the path functions do not exist. This deliberately does **not** drive `brain eval --answer` on unfixed code, because that would reach a live Ollama. |
| R5 | `test_brain_eval_default_corpus_on_wheel_install` (CLI, nodb) | `brain eval` with no `--corpus` exits 1, and stderr names `tmp/brain_home/eval/golden_corpus.yaml`. The load fails before `Config.load`/DB. | Stderr names `<repo>/tests/eval/golden_corpus.yaml`. That file is gitignored and absent in the worktree. |
| R6 | `test_brain_eval_record_baseline_on_wheel_install` (in `test_cli_eval.py`, DB `fix_e_test`) | `--corpus <tmp yaml> --record-baseline wheel-sim` writes `tmp/brain_home/eval/baselines/wheel-sim.json` and nothing under the repo's `tests/eval/baselines/`. | It writes into the worktree's `tests/eval/baselines/wheel-sim.json`. That path is gitignored and I will delete it right after the RED run. |

The pin tests below must pass both before and after the change.

| # | Test | Proves |
|---|---|---|
| P1 | `test_checkout_root_for_repo_layout` / `..._site_packages_layout_is_none` | The predicate on synthetic `repo/src/brain/x.py` (with `pyproject.toml`) returns `repo`, and on `venv/.../site-packages/brain/x.py` returns `None`. P1 is RED only by import; it pins the extracted heuristic. |
| P2 | `test_this_checkout_is_detected` | Unpatched, `source_checkout_root()` equals the root above `brain.config.__file__` and holds `pyproject.toml`. The test suite therefore runs in dev mode, and `writable_dotenv() == config._project_dotenv()`. |
| P3 | `test_owner_set_in_checkout_writes_project_dotenv_even_with_brain_home_set` | Dev behaviour is unchanged. `_project_dotenv` is patched to `tmp/project.env` and `BRAIN_HOME=tmp/brain_home`. `set` writes `tmp/project.env`, and `tmp/brain_home/.env` is never created. |
| P4 | `test_eval_defaults_in_checkout_are_unchanged` | Every default equals the old expression byte-for-byte, for example `Path(corpus.__file__).resolve().parents[3] / "tests/eval/golden_corpus.yaml"`. `eval_baselines_dir()` is `<repo>/tests/eval/baselines`, which holds the committed `ci.json`. |
| P5 | `test_install_paths_imports_cold` | `python -c "import brain.install_paths"` and `python -c "import brain.config"` both succeed in a fresh interpreter. This guards against the config/install_paths cycle. |
| — | existing `TestBrainHomeResolution` (test_config.py) | It keeps passing unchanged, which shows `_brain_home_root`'s three branches survived the extraction. |

**Mutation check**, per memory "Prove the check can fail". In a `git archive` copy, never the live
worktree mid-run, I will try three mutations. Making `writable_dotenv` always return
`_project_dotenv()` should turn R1–R3 red and leave P3 green. Making `eval_data_dir` always return
the checkout path should turn R4–R6 red. Dropping the `resolve()` in the writer should turn only R3
red.

**Commands.** Each run holds `.claude/full-suite.lock` and needs `df -g /` ≥ 7, inside my own uv
`.venv`:

```
pytest tests/test_install_paths.py tests/test_cli_owner.py tests/test_config.py \
  tests/test_config_dotenv_chain.py tests/test_eval_corpus.py tests/test_answer_eval.py \
  tests/test_concept_extraction_metric.py tests/test_eval_workflow_contract.py \
  tests/test_ceiling_table_check.py tests/test_log_privacy_rules.py tests/test_log_privacy.py \
  tests/test_import_hygiene.py tests/test_repo_hygiene_files.py --no-cov
TEST_DATABASE_URL=postgresql://brain:brain@localhost:5434/fix_e_test \
  pytest tests/test_cli_eval.py tests/test_eval_stage_skips.py tests/test_eval_fail_below.py --no-cov
ruff check && mypy src/ && python scripts/check_ceiling_table.py
```

When I'm done, I will drop `fix_e_test` by its exact name.

## 7. Risks

1. **Ceiling-table merge conflicts.** Fix PRs A, C1, C2 and D may also promote the same `cli.py`
   (and maybe `config.py`) trailing hop. Whichever merges second has to re-promote on top. I am
   flagging this to fix-lead for merge ordering.
2. **The stale venv `.env`** from the bug outranks `$BRAIN_HOME/.env` (§3 residual). The
   CHANGELOG upgrade line covers it. The follow-up gate closes it.
3. **The daemons' non-editable install.** After this fix, `owner` writes from that install land in
   `~/.brain/.env`. If that is a symlink to the repo `.env`, the write-through keeps one file.
4. **Lazy defaults vs. import-time constants.** The six test monkeypatch sites change. Anything
   else that imported the old private names is found by grep (only `cli.py` and the tests). No
   public API changes.
5. **The rename changes no `brain eval` output** in a checkout. The `baseline saved: <path>` line
   prints the same path.

## 8. CHANGELOG text

```markdown
### Fixed — an installed `brain` keeps `brain owner` and `brain eval` files in `$BRAIN_HOME` (F25)

- `brain owner set`, `add` and `remove` wrote `BRAIN_OWNER_PARTICIPANTS` to a `.env` beside the
  installed package (`<venv>/lib/python3.X/.env` for a `pip` or `uv tool` install) instead of your
  config. Outside a source checkout they now write `$BRAIN_HOME/.env` (default `~/.brain/.env`),
  the file `brain setup` creates. When that file is a symlink, its target is updated and the link
  is kept. A source checkout still writes the repo `.env`.
- `brain eval`'s default golden corpus, answer corpus and baselines directory pointed at
  `tests/eval/` beside the installed package, so `--record-baseline` wrote into the virtualenv.
  Outside a source checkout they are now `$BRAIN_HOME/eval/golden_corpus.yaml`,
  `$BRAIN_HOME/eval/answer_corpus.yaml` and `$BRAIN_HOME/eval/baselines/`. A source checkout still
  uses `tests/eval/`, so the eval gate (`ci.json`, `golden_corpus.yaml`) is unchanged.
- Upgrading: if you ran `brain owner set`, `add` or `remove` on an installed `brain`, look for
  `<venv>/lib/python3.X/.env`, the directory above `site-packages`. It is still read and it
  outranks `$BRAIN_HOME/.env`, so move its `BRAIN_OWNER_PARTICIPANTS` line into `$BRAIN_HOME/.env`
  and delete it.
```

## 9. Phase-2 amendments (fix-lead review, 2026-11-19)

**Approved with these rulings, and how the code follows them.**

- **Writing through a symlink is kept, and the edge cases are defined.**
  `install_paths.dotenv_write_target(path)`:
  - A non-link path is returned unchanged, so a checkout's regular repo `.env` behaves exactly as
    before.
  - A link to an existing file returns the resolved target. The target may be anywhere, including
    outside `$BRAIN_HOME`, because that is what `brain setup`'s dev-shape link is for.
  - A **dangling** link raises `DanglingDotenvLinkError`. `brain owner` prints one line naming the
    link and its missing target, then exits 1. Nothing is written: the old target is not
    re-created, and the link is not replaced with a file. This mirrors `brain setup`, which
    reports a dangling link as `dangling` rather than replacing it, because the user's config
    presumably moved.
  - The check runs in `_owner_dotenv_path()`, before the read, so `set`, `add` and `remove` all
    refuse.
- **The eval paths in a checkout are byte-identical to before.** `test_eval_defaults_in_checkout_are_unchanged`
  rebuilds the old module-constant expressions verbatim (`Path(<module>.__file__).resolve().parent…/
  "tests"/"eval"/…`) and asserts that every new function returns an equal path. It also asserts
  that the committed `ci.json` sits in the resolved baselines directory.
- **No RED test reaches Ollama.** The answer-corpus regression calls `load_answer_corpus()`
  directly. Before the fix, that loaded the committed repo file; it did not run
  `brain eval --answer`.
- **Test seam.** `cli.py` imports the module (`from . import cli_tags, install_paths`) and calls
  `install_paths.eval_baselines_dir()`. The six existing tests therefore patch
  `brain.install_paths.eval_baselines_dir` instead of `brain.cli._BASELINES_DIR`. `cli.py`'s
  `config as _config_module` import was used only by `_owner_dotenv_path`, so it is gone.
- The shared simulation is `tests/wheel_install.py::simulate_wheel_install`, used by
  `tests/test_install_paths.py` and by one test in `tests/test_cli_eval.py`.

**Final test names**, matching §6:

| Plan | Test name |
|---|---|
| R1 | `test_owner_set_writes_brain_home_dotenv_on_wheel_install` |
| R2 | `test_owner_add_remove_then_show_on_wheel_install` |
| R3 | `test_owner_write_keeps_brain_home_symlink` |
| R3b (new) | `test_owner_refuses_dangling_brain_home_symlink` |
| R4 | `test_answer_corpus_default_on_wheel_install` and `test_eval_default_paths_on_wheel_install` |
| R5 | `test_eval_corpus_default_on_wheel_install` |
| R6 | `tests/test_cli_eval.py::test_brain_eval_record_baseline_on_wheel_install` |
| P1–P5 | as named in §6 |

## 10. Not in scope (backlog)

- **The package-relative `.env` stays in the read chain outside a checkout.**
  `src/brain/config.py:837` (`ordered: list[Path] = [_project_dotenv()]` in
  `_resolve_dotenv_chain`) still reads `<venv>/lib/python3.X/.env` on an installed `brain`, and
  that file outranks `$BRAIN_HOME/.env`. `src/brain/setup.py:611` (`provision_brain_home_dotenv`)
  can still symlink `$BRAIN_HOME/.env` at that file if it exists. Not changed here, on purpose: a
  user, or the owner's own daemon install, who ran `owner set` before this fix keeps their real
  owner settings in that file. Dropping it from the chain would lose them silently. The CHANGELOG
  tells affected users to move the line, then delete the file.
- **The docs disagree with the code.** `docs/configuration.md:809` says link 2
  (`<repo>/.env`) is "Source checkouts only". The code consults it unconditionally. Gate the
  link, or correct the row, when the item above is taken up.
