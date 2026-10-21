# Wiki → `brain ui` consolidation — phase 9 (R3, Delete) — implementation plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use team-driven-development to implement this plan task-by-task.

*Drafted 2026-10-21 against `second-brain/master` @ `6948c6f`, on branch `feat/wiki-to-ui-phase9`.
Contract: `docs/specs/2026-08-10-wiki-to-ui-consolidation-design.md` §1.3 (R3), §9 (all
subsections), §10 RA-8, §11 rows 7–9, §12 Q12, Appendix B (the 2026-08-20 importer
re-derivation). It follows phase 8 (`docs/plans/2026-10-13-wiki-to-ui-phase8.md`, ruling R8-2:
the Quartz steps moved to an opt-in `quartz` stage, which this phase deletes). **Every line
reference below was read at `6948c6f`. Re-derive each one at edit time.** The spec has shipped
stale anchors before (§9.2c's table is stale in more than half its rows, B-2), so this plan works
from **symbols and greps, not line numbers.***

## 0. The gate (recorded verbatim in substance)

Spec §11 row 9 gates R3 on two things: **(a)** explicit user confirmation that they have stopped
reaching for the wiki, and **(b)** an explicit decision on RA-8.

- **(a) — given 2026-10-21.** The coordinator showed the user *"Phase 9 (deleting the wiki
  code): this needs your RA-8 decision."* The user instructed: *do everything on the list
  autonomously, and don't stop until all of it is done.* That instruction is the user's word
  for (a). Q12's default is "on your word, no timer", and this is that word.
- **(b) — RA-8 is REJECTED**, per the spec's own recommendation (§10.4 RA-8: *"Rejected, but
  the strongest surviving counter-proposal"*). RA-8 proposed keeping the wiki as a read-only
  fallback for when Postgres is down. **The availability loss is accepted:** once this lands,
  there is no reading surface while Postgres is down. The vault is still plain Markdown on disk,
  so Obsidian and vim keep working. That was always the floor (§1.3).

Prior phases: phase 7 (R1) merged in PR #26, phase 8 (R2) in PR #27 (`e1fc28e`), and A9's UI
watcher line in PR #29.

## 1. What phase 9 is, in one paragraph

§1.3 defines **R3 — Delete**. The Quartz build chain goes: `src/brain/wiki/`,
`src/brain/quartz_overrides/`, `vault/quartz_overlay.py`, `brain vault render`, `brain wiki
install`, the opt-in `quartz` stage of `brain-rebuild`, the Caddy/Quartz setup preflights, the
dormant-wiki doctor check, the Quartz-only `bin/` scripts and templates, and every test that
exists only to cover them. **Everything that writes into the vault survives:** the `mirror`
stage, `prune-orphans`, the People Hub emitter, `related.py`, `person_name.py`, the vault watcher
and the daily brief. **Nothing on the user's machine is touched.** This phase edits repository
code only. It never deletes or writes anything under `~/brain-vault`, `<vault>/.quartz`,
`$BRAIN_HOME`, or launchd (see §6).

## 2. Rulings (implementer, 2026-10-21; the coordinator pre-approved autonomous execution)

| # | Question | Ruling |
|---|---|---|
| **R9-1** | **The rule that sorts every ambiguous item.** | **Producers die; legacy *cleanup* survives.** Code that builds, serves, installs or configures the Quartz wiki is deleted. Code that *removes leftovers* of an older install survives, because machines upgrading from a pre-R3 install still carry those leftovers. This follows the spec's own recommendation for `uninstall.py` (§9.5 correction 5). It decides: `_RETIRED_LABELS` / the `com.brain.build` sweep in `bin/launchd.py`, `retired_daemon_doctor_check`, `brain-down`'s `bootout com.brain.build` + legacy pid/pkill sweep, and `uninstall.py`'s `Caddyfile`/`build` removes all **stay**. |
| **R9-2** | **`brief --wiki`.** | **KEEP, flag name unchanged.** It is a *vault write*, not a Quartz step: `write_brief_to_vault(cfg.vault_path, …)` writes `<vault>/daily/<YYYY>/<date>-brief.md` (its help text already says so). The test is §9.2's own: *where the output lands*. It lands in the vault, and the vault survives. Renaming the flag would break the user's habit for no gain. |
| **R9-3** | **`--clean-cache` and `--only quartz` after the stage is gone.** | `--clean-cache` is **deleted**: it only ever wiped the Quartz parser cache (§9.2d(6)), and argparse now rejects it with exit 2. `--only quartz` exits 2 through the existing "unknown stage id" path, which names the valid stages. `--wiki-only` **keeps** its explicit exit-2 message, reworded to say the wiki was deleted and naming `--only mirror`. The `opt_in` machinery on `Stage` has no remaining user, so it is **deleted** rather than left as dead code (CLAUDE.md rule 14(e)). |
| **R9-4** | **`EXIT_CONFIG_ERROR` in `brain doctor`'s daemon check.** | **The special case is deleted with its import.** Exit 3 was `build_swap`'s "misconfigured" contract. No surviving daemon emits it: the watcher is `brain vault sync --watch`, whose `ConfigError` surfaces through the Typer group as exit 2, and the brief is a one-shot. Exit 3 from a surviving daemon is therefore a plain non-zero exit and reports as `last exit 3` with the reload remedy. A regression test pins that. |
| **R9-5** | **`brain setup --skip-wiki` / `--wiki-port`.** | **Deleted** from the CLI and from `run_setup`. Typer rejects them with exit 2. No in-repo caller passes them (`install.sh`, `scripts/`: none; re-derive with `git grep -n 'skip-wiki\|wiki-port'`). The `offer_wiki` `SetupProfile` field goes with them (§9.5). |
| **R9-6** | **`brain-monitor`'s Quartz half and the `probe` command.** | **Deleted.** §9.5 correction 1 says its build-freshness section "is Quartz and goes with it". `probe` only ever timed the build watcher's rebuild. With no build watcher there is nothing to time, so `brain-monitor probe` is removed (exit 2 via argparse). The sync-watcher half stays. `BRAIN_WIKI_PORT` goes. This is a separate, last-but-one commit, because another agent is concurrently editing `src/brain/bin/monitor.py` and `templates/bin/brain-status.sh`. The hunks stay minimal and isolated so a rebase is mechanical. |
| **R9-7** | **Prose in surviving modules that names Quartz.** | **Two classes, treated differently.** (i) A *live-coupling* claim naming a deleted module as a current consumer or importer is false and misleading, so it is fixed. Examples: `vault/paths.py` listing `brain.wiki.build_homepage` as a consumer, and `related.py` saying `brain.wiki.build_related` imports its helpers. (ii) *Rationale and provenance* prose stays as history: "Quartz publishes `_ingested/`, so this gate exists", or "ported from `brain.wiki.build_homepage`". The confidentiality gates themselves are **not** removed: the vault is still a publishable directory (Obsidian Publish, git, any static-site tool), and loosening an egress gate is a separate security decision this deletion phase does not make. Vault-format behaviour written for Quartz compatibility (path-form wikilink rewrite, `daily/index.md`, the derived-edges fence) **stays**: §1.3 and Q11 keep the vault format unchanged. |
| **R9-8** | **Directory skips for `.quartz/`** (`vault/sync.py`, `vault/watch.py`, `bin/monitor.py` `_VAULT_SKIP_DIRS`). | **KEEP.** The user's vault still holds a `.quartz/` workspace (tens of thousands of files under `node_modules`) until they remove it by hand (§6). Dropping the skip would make the watcher and sync walk it. |
| **R9-9** | **`BRAIN_USER_EMAIL` / `Config.user_email`.** | **KEEP** (§9.3 trap 7). It now has a live reader: `ui/routes_meta.py` projects `user_email` for the email-thread mode. Only the `.env.example` comment changes (it says the Quartz build reads it). |
| **R9-10** | **Historical docs** (`docs/plans/*`, `docs/specs/*` other than the consolidation spec's row-9 note, `docs/handoff/*`, `docs/audits/*`, past CHANGELOG entries). | **Untouched.** They are dated records. Rewriting them would falsify history. |
| **R9-12** | **`related._iter_hybrid_neighbors` after the emitter dies.** *(Added during execution, C6.)* It was the corpus-wide driver for `wiki/build_related.regenerate_related_json`; with the emitter deleted it has **no production caller** (re-derive with `git grep -n _iter_hybrid_neighbors -- src`), only the ranking and gating tests. | **Moved to `tests/related_harness.py`** as `iter_hybrid_neighbors`, the same loop over the same production helpers (`_eligible_source_docs`, also `brain connect`'s; `_corpus_common_lexemes` and `_neighbors_for_source`, both behind `compute_related`). Leaving it in `related.py` would be dead production code (rule 14(e)); deleting it would drop the corpus-wide ranking and gate coverage. The tests still exercise shipped code paths; only the joining loop lives in `tests/`. |
| **R9-11** | **Auto-memory** (`quartz.md`, `vault.md`, `cli.md`, `MEMORY.md`; CLAUDE.md rule 11). | **Owed by the coordinator after merge.** It lives outside the repo, it is shared with the main session, and it should describe merged `master`, not an unmerged branch. The report lists exactly what changes. |

## 3. Inventory — re-derived at `6948c6f`

### 3.1 The A12 import probe (before)

`python -c "import brain.cli"` with a `sys.modules` diff loads five wiki/Quartz modules:
`brain.vault.quartz_overlay`, `brain.wiki`, `brain.wiki.build_swap`, `brain.wiki.errors`,
`brain.wiki.install`. This matches the spec's 2026-08-20 re-run (five).

### 3.2 Import statements outside `src/brain/wiki/` (`^\s*(from|import)\s+.*(\bwiki\b|quartz)`)

| Site (symbol, not line) | Scope | Fate |
|---|---|---|
| `cli.py` `from .vault.quartz_overlay import OverlayError, apply_overlay, plan_overlay` | module | removed with `brain vault render` (C1) |
| `cli.py` `from .wiki.install import WikiInstallError` / `wiki_install as _wiki_install` | module | removed with the `brain wiki` group (C1) |
| `cli.py` `from .wiki.build_swap import resolve_build_timeout_s` (inside `vault render`) | function | removed with `vault render` (C1) |
| `setup.py` `from .wiki import QUARTZ_PINNED_COMMIT, QUARTZ_REPO_URL` | module | removed with `_check_quartz_sha` (C2) |
| `setup.py` `from .wiki.install import wiki_install` (inside `_maybe_install_wiki`) | function | removed with `_maybe_install_wiki` (C2) |
| `doctor_runtime.py` `from .wiki.build_swap import EXIT_CONFIG_ERROR` | module | removed (R9-4, C3) |

There are also two subprocess or string invocations, which are not imports. Both run
`python -m brain.wiki.build_swap`: `maintenance.py`'s `quartz` stage (C4) and
`templates/bin/brain-up.sh`'s cold-start build (C5). A third, `brain.wiki.build_watcher`, is
reached through `templates/bin/_brain-build-fg.sh` and `bin/_brain-build-fg` (C5).

### 3.3 Deleted — source and assets

- `src/brain/wiki/` (14 modules) and `src/brain/quartz_overrides/` (73 files), ≈24,500 lines
  together, plus `src/brain/vault/quartz_overlay.py` and the repo-root `quartz.config.ts`.
- `cli.py`: `brain vault render` with `_check_quartz_workspace`, the render output-dir guard, and
  its helpers and comment block. Also the `wiki_app` Typer group and `brain wiki install`, the
  `quartz/npx` doctor probe (`npx` existed only for `vault render`), and `brain setup`'s
  `--wiki-port` / `--skip-wiki`.
- `setup.py`: `_check_caddy`, `_check_quartz_sha`, `_maybe_install_wiki`, `offer_wiki`, the
  `wiki_port` / `skip_wiki` plumbing, the wiki-port preflight, and `_print_final_report`'s
  wiki lines.
- `doctor_runtime.py`: `wiki_freshness_doctor_check`, `newest_completed_build`,
  `WIKI_REBUILD_REMEDY`, `BUILD_COMPLETE_MARKER`, the `EXIT_CONFIG_ERROR` branch, and the
  "wiki build" lines of the module docstring.
- `maintenance.py`: the `quartz` stage, `quartz_steps`, `_PARSER_CACHE_RELPATH`,
  `_KEEP_DEFAULT`, `BRAIN_WIKI_KEEP_BUILDS`, `build_stages(keep=)`, `--clean-cache`, and the
  `opt_in` machinery (R9-3).
- Templates: `templates/Caddyfile.j2`, `templates/launchd/com.brain.build.plist.j2`, and
  `templates/bin/_brain-build-fg.sh`.
- `bin/`: `_brain-build-fg`, `brain-verify-fastpath`, `brain-wiki-gif`, and
  `brain-wiki-gif-capture.cjs`.
- `templates/bin/brain-up.sh`: `apply_overlay`, `current_healthy`, `cold_start_build`,
  `wait_for_url`, `BRAIN_WIKI_BUILD`, `BRAIN_OPEN_BROWSER` (it only ever opened the wiki),
  `BRAIN_NO_OVERLAY`, `BRAIN_WIKI_PORT`, `BRAIN_WIKI_KEEP_BUILDS`, and the wiki and build
  banner lines.
- `templates/bin/brain-status.sh`: the build row, the `current`/build-id block, and the
  wiki-URL probe (R9-6).
- `src/brain/bin/monitor.py`: the build-watcher state, the `.quartz/current` and `builds`
  readout, the wiki URL, the build-log tail and stream, `probe`, and `BRAIN_WIKI_PORT` (R9-6).
- `pyproject.toml`: the `brain.quartz_overrides` package-data and exclude-package-data
  entries, the `e2e` marker (every user of it is a deleted Quartz test), and the
  `wiki/build_related.py` pointer in the pgvector evidence block (→ `related.py`).
- `scripts/check_index_size.py` and its test `tests/test_search_size_budget.py`: the Quartz
  contentIndex size budget.
- `scripts/verify_release.py`: checks 4 (scratch-vault Quartz build) and 5 (`build_swap` must
  fail loudly), plus `--include-build`; the default `--checks` becomes `1,2,3`.
- `scripts/smoke-linux.sh` and `scripts/smoke-macos.sh`: the Caddy, cold-start-build and
  build-watcher bring-up and the wiki-URL assertions. The vault-watcher bring-up stays.

### 3.4 Deleted — tests (re-derived from imports/paths, never from a count)

- `tests/wiki/` (all of it), every `tests/test_quartz_*` (`.py` and the `.ts`),
  `tests/quartz_e2e_helper.py`, and `tests/fixtures/quartz_e2e_vault/`.
- `test_wiki_install.py`, `test_docs_assets_wiki.py`, `test_cli_vault_render.py`,
  `test_packaging_overlay.py`, `test_build_related.py`, `test_build_related_idempotence.py`,
  `test_build_related_schema.py`, `test_brain_recent_homepage.py`, `test_buildid_etag.py`,
  `test_contentindex_slim.py`, `test_explorer_filter.py`, `test_explorer_groupfn.py`,
  `test_graph_chip_toggle.py`, `test_graph_label_placement.py`,
  `test_graph_workbench_static.py`, `test_verify_fastpath_static.py`,
  `test_doctor_npx_check.py`, and `test_search_size_budget.py`. Each is read before it is
  deleted, to confirm it covers *only* Quartz assets or `brain.wiki` code (§9.3 trap 1).
- **`test_build_related_signal.py` is NOT deleted wholesale** (§9.3 trap 5): it drives
  `brain.related._iter_hybrid_neighbors`. Only the cases that call `regenerate_related_json`
  go.

### 3.5 Modified tests (surgery, not deletion)

`test_doctor_runtime.py` (the EXIT_CONFIG_ERROR cases and wiki freshness), `test_doctor_watcher.py`
(its `wiki_freshness_doctor_check` import), `test_maintenance.py`, `test_bin_scripts.py`,
`test_bin_scripts_r2.py`, `test_bin_monitor.py`, `test_bin_launchd.py`,
`test_bin_launchd_retired.py` (prose saying the template stays "until R3"),
`test_packaging_templates.py` (the `_brain-build-fg` row), `test_setup.py`,
`test_setup_daemons.py`, `test_setup_dotenv_knobs.py` and `test_setup_dotenv_provision.py`
(the `skip_wiki=` kwarg only), `test_daily_index.py` (its Quartz-config cross-checks),
`test_reading_surface_pointers.py`, `test_thread_wait.py` (its list of wiki test files),
`tests/import_rules.py` (`brain.wiki` in `FORBIDDEN_PACKAGES`), `test_compose_no_raw_calls.py`
(the `quartz_overrides` skip dir), `test_verify_release_launchd_labels.py`, and the prose in
`env_isolation.py`. Each is re-derived at edit time with
`git grep -nE 'brain\.wiki|quartz|build_swap|build_watcher|…' tests`.

### 3.6 KEPT — and why

| Kept | Why |
|---|---|
| `brain-rebuild` `mirror` stage (`vault export`, `sync-summaries`, `prune-orphans --apply --include-stale`) — argv and `fatal` flags **unchanged** | §9.4: the mirror keeps 1,257+ ingested docs readable outside the DB. `prune-orphans` is a deleter coupled to the People Hub emitter (§9.2d(5)). |
| `brain/people.py` (`emit_people_pages` etc.), `brain vault relink-derived` steps 1–6 | §9.2: the People Hub writes into the vault (51 live documents). |
| `brain/related.py`, `brain/person_name.py` | §9.2: `brain connect`, `brain people`, `graphrag`, `review`, and the `brain ui` related panel. |
| `com.brain.watcher`, `com.brain.brief`, `bin/_brain-watcher-fg`, `templates/bin/_brain-watcher-fg.sh`, `_brain-brief-fg.sh` | §9.1, §8.5: the vault watcher becomes *more* important. |
| `brief --wiki` | R9-2: it is a vault write. |
| `vault/daily_index.py`, `derived_links` and the fence, `vault/link_rewrite.py`, `vault/slug.py` (≠ `wiki/slug.py`), `log_rotation.py`, `tests/test_wiki_link_parser.py` | §9.3 traps 1, 2, 3, 9. |
| `_RETIRED_LABELS` + sweep, `retired_daemon_doctor_check`, `brain-down`'s legacy bootout/pid/pkill, `uninstall.py` `Caddyfile`/`build` | R9-1: legacy cleanup. |
| `.quartz/` directory skips | R9-8. |
| `Config.user_email` / `BRAIN_USER_EMAIL` | R9-9. |
| `docker-compose*.yml` | §9.3 trap 6: there are no wiki references to remove. |

## 4. Commit sequence — A12 holds after EVERY commit

Every commit ends with: `python -c "import brain.cli"` (A12, the first check), `ruff check`,
`mypy src/`, the targeted pytest slice named in the row, and a residue grep for the symbols
the commit removed. Source and its tests leave in the **same** commit (§9.2d(4)).

| # | Commit | Verification beyond the standard four |
|---|---|---|
| **C1** | `refactor(cli)!: remove brain vault render, brain wiki install and the quartz/npx doctor probe`. Deletes `vault/quartz_overlay.py`, `test_quartz_overlay.py`, `test_cli_vault_render.py`, `test_wiki_install.py` and `test_doctor_npx_check.py`. | `brain vault --help` lists no `render`; `brain --help` lists no `wiki`; the probe no longer loads `brain.vault.quartz_overlay` or `brain.wiki.install` |
| **C2** | `refactor(setup)!: drop the wiki install, Caddy and Quartz-SHA preflights`. Covers `setup.py`, `cli.py`'s `setup` options, the setup tests and `offer_wiki`. | `test_setup*.py`; `brain setup --help` has no `--skip-wiki` / `--wiki-port`; the probe no longer loads `brain.wiki` from `setup` |
| **C3** | `refactor(doctor): drop the dormant wiki-build check and build_swap's exit-code contract`, plus a regression test for R9-4. | `test_doctor_runtime.py`, `test_doctor_watcher.py`, `test_cli_doctor.py`; the probe shows **zero** wiki/Quartz modules from here on |
| **C4** | `refactor(rebuild)!: delete the opt-in quartz stage and --clean-cache` | `test_maintenance.py`, `test_bin_scripts*.py`; `brain-rebuild --dry-run` lists `mirror` and no `quartz`; the mirror argv is pinned unchanged |
| **C5** | `refactor(bin)!: brain-up/brain-down stop knowing about Quartz; delete the build-daemon template, wrappers and wiki scripts` | `test_bin_scripts*.py`, `test_bin_launchd*.py`, `test_packaging_templates.py`; `bash -n` on every edited shell template |
| **C6** | `refactor!: delete brain.wiki, quartz_overrides and their tests`. Also covers `pyproject.toml` package-data and markers, the scripts, and test surgery on the survivors. | `git grep -n 'brain\.wiki\|quartz_overrides\|quartz_overlay'` returns only justified residue (§7); `tests/test_ui_static_assets.py` and `test_packaging_*` stay green; a built wheel holds no `quartz_overrides/` and no `wiki/` (`python -m build --wheel` into the scratchpad, then `unzip -l`) |
| **C7** | `refactor(monitor,status): drop the Quartz build half of brain-monitor and brain-status` (R9-6, isolated for the concurrent editor) | `test_bin_monitor.py`, `test_bin_scripts*.py` |
| **C8** | `docs: retire the wiki from README, docs/, CLAUDE.md, skills and CHANGELOG; exit note on spec §11 row 9` | `test_reading_surface_pointers.py`, `test_docs_assets*.py`, `test_agent_skills*`; the CLAUDE.md over-800 row set re-derived with `find src -name '*.py' \| xargs wc -l \| sort -rn` |

The final verification (§5) runs after C8. Rule-14 review fixes land as **new** commits only.

## 5. Final verification

1. `python -c "import brain.cli"`, and a `sys.modules` probe showing **zero** `wiki` / `quartz`
   modules.
2. `ruff check && mypy src/`.
3. Residue greps across tracked files, excluding the R9-10 history docs:
   `brain\.wiki`, `quartz_overrides`, `quartz_overlay`, `build_swap`, `build_watcher`,
   `vault render`, `wiki install`, `--only quartz`, `BRAIN_WIKI_`, `_brain-build-fg`,
   `Caddyfile`. Every hit is either removed or listed in §7 with its reason.
4. Package data: `tests/test_ui_static_assets.py` and `tests/test_packaging_*.py`, plus a built
   wheel inspected for the `brain/ui/static/**` assets and the absence of
   `quartz_overrides`/`wiki`.
5. The full `tests/` suite in a **sibling** test DB on port 5434
   (`TEST_DATABASE_URL=…/second_brain_test_phase9`). No file in the worktree is mutated while
   it runs. Exit codes are read from files, not from pipes. If time does not allow, at minimum
   every test touching vault, ui, cli, mcp, people, related, rebuild, maintenance, install,
   setup, doctor, bin and packaging runs. CI on the PR runs the full suite either way.
6. CLI end-to-end against the **demo sandbox or the test DB only, never prod**:
   `brain --help`, `brain vault --help`, `brain-rebuild --dry-run`, `brain setup --help`.

## 6. On the user's machine — NOT touched; manual cleanup they may do

This phase deletes repository code only. The following leftovers of the retired wiki may sit
on the user's machine. **Nothing here removes them.** They are inert once phase 9 is installed
and can be deleted by hand:

- `<vault>/.quartz/` is the Quartz workspace, its `node_modules`, `builds/`, `current` and
  `.cache/`. Removing it is safe, and the vault's notes are not inside it. The sync and watcher
  skips stay (R9-8), so leaving it costs only disk.
- `$BRAIN_HOME/Caddyfile` and a running `caddy` (if one was started) serve the last wiki build
  on `:8080`. Stop Caddy, and the file can go. `brain uninstall` still removes the file
  (R9-1).
- `$BRAIN_HOME/build/` and `$BRAIN_HOME/run/brain-build.pid`. `brain uninstall` and
  `brain-down` still clean these up.
- A leftover `com.brain.build` LaunchAgent is already removed by the next
  `brain-install-launchd` since phase 8, and `brain doctor` WARNs while one is present.

## 7. Residue policy — what a grep will still find, and why

Filled in at closeout (§9) with the exact grep output. Expected classes: R9-7(ii) rationale and
provenance prose; R9-1 legacy-cleanup code; R9-8 `.quartz` skips; R9-10 history docs; the
`brief --wiki` flag (R9-2); `[[wikilink]]` vocabulary, which is vault syntax and not the site
(§9.3 trap 1).

## 8. Ceiling (CLAUDE.md "File-size ceiling")

The tabled files this phase touches are `cli.py` (shrinks; trail promotion + descriptive hop),
`setup.py` (shrinks), and `related.py` (a docstring fix only, R9-7(i)). Each gets its
trailing descriptive hop promoted to its real SHA, plus a fresh descriptive hop for this change.
`wiki/build_watcher.py` leaves the table, and so does any other row whose file is deleted. The
over-800 row set is **re-derived on the final tree**, never edited by hand from this list.

## 9. Closeout

**Built 2026-10-21 on `feat/wiki-to-ui-phase9`.** The commit order differs from §4's table in
one respect: the CLI removal (§4 C1) shipped **inside** the package-deletion commit, because
`cli.py` turned out to be the last importer of `brain.wiki`. A12 is satisfied most directly when
the importing statement and the module leave in one commit, and the pre-commit `mypy src/` gate
refused a split anyway (the orphaned `wiki/install.py` imported the deleted overlay module).

| Commit | What |
|---|---|
| `06bad1e` | this plan |
| `4f8dbf0` | `brain-rebuild`: the opt-in `quartz` stage, `--clean-cache` and `Stage.opt_in` deleted (R9-3) |
| `6074b97` | `brain setup`: wiki install, Caddy / Quartz-SHA / wiki-port preflights, `offer_wiki`, `--skip-wiki` / `--wiki-port` (R9-5) |
| `57f564c` | `brain doctor`: wiki-build check deleted; `EXIT_CONFIG_ERROR` special case deleted (R9-4, regression test mutation-checked) |
| `9657733` | `brain-up` / `brain-down`; the `com.brain.build` template, both `_brain-build-fg` wrappers, `brain-verify-fastpath` |
| `885b5a6` | `src/brain/wiki/`, `quartz_overrides/`, `vault/quartz_overlay.py`, `brain vault render`, `brain wiki install`, the `quartz/npx` probe, root `quartz.config.ts`, `Caddyfile.j2`, `check_index_size.py`, verify_release checks 4–5, the smoke scripts' Quartz steps, their tests; `_iter_hybrid_neighbors` to `tests/related_harness.py` (R9-12) |
| `6238fc9` | `brain-monitor` / `brain-status` Quartz half, `brain-monitor probe` (R9-6) |
| `cbe2f54` | docs, skills, CLAUDE.md, spec row-9 exit note, CHANGELOG |
| `a4d9963` | merge of `second-brain/master` (`6ba3084`: PR #41, PR #42), conflicts resolved toward R3 |

**Size.** `git diff --shortstat 6948c6f cbe2f54` (phase 9's own commits, before the merge):
243 files, +1,414 / −53,773; `src` −24,693, `tests` −26,213. 172 files deleted.

**A12 probe.** `python -c "import brain.cli"` passed after every commit; the `sys.modules` diff
went from five wiki/Quartz modules (§3.1) to **none**.

**Verification.**
- `ruff check` and `mypy src/` (216 source files) clean at every commit and after the merge.
- Targeted slices per commit, each green; the new guards were mutation-checked (re-adding the
  exit-3 special case, a wiki curl in `brain-up`, and the previous `brain-monitor` /
  `brain-status` each turned their tests red).
- Before the merge, one broad run of the default suite in the sibling DB: **8,559 passed, 3
  skipped, 1 failed**. The failure is `test_skills_sync_is_not_drifted`, which compares the
  repo's skills with the copies installed in `~/.claude/skills` on this machine. It skips where
  none are installed (CI), and clears after `bin/brain-skills-sync`, which writes outside the
  repo and is left to the operator.
- After the merge: the affected slices (bin scripts, monitor, install_config, setup tilde,
  packaging templates, reading-surface pointers, docs assets, launchd) green; `pytest
  --collect-only` collects 8,829 tests with no error.
- Built wheel: 39 `brain/ui/static/**` assets, nothing under `wiki/` or `quartz_overrides/`.

**Residue (§7), by class.** `git grep -n 'brain\.wiki\|quartz_overrides\|quartz_overlay'`
still hits prose only: provenance ("ported from `brain.wiki.build_homepage`", the
`linkKindMark.ts` origin of `render.py`'s link kinds), confidentiality rationale written when
Quartz published the vault (R9-7(ii); the gates stay), the `import_rules.py` note on why
`brain.wiki` left its forbidden list, and the legacy sweeps (R9-1: `brain-down`'s
`pkill -f brain.wiki.build_watcher`, the `com.brain.build` bootout and plist sweep, the
doctor WARN, `uninstall.py`'s `Caddyfile`/`build`). The `.quartz` walk skips stay (R9-8). The
`'wiki'` value in the frozen migrations' CHECK lists stays (migrations are never edited).

**Owed after merge (R9-11).** Auto-memory: delete `quartz.md`; update `vault.md`, `cli.md` and
the `MEMORY.md` index lines that describe the wiki, `brain vault render`, `brain wiki install`,
the `quartz` stage or `--clean-cache`.
