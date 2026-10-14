# Wiki → `brain ui` consolidation — phase 8 (R2, Dormant) — implementation plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use team-driven-development to implement this plan task-by-task.

*Drafted 2026-10-13 against `feat/wiki-to-ui-phase7` @ `0965b61`; branch `feat/wiki-to-ui-phase8`,
rebased onto `master` after phase 7 merges if needed. Contract:
`docs/specs/2026-08-10-wiki-to-ui-consolidation-design.md` §1.3 (R2), §8.5 (A9), §9.4 (A10),
§9.5, §9.6, §11 (phase-8 row), §12 Q8 and Q10. Every line reference below was read at `0965b61`.
**Re-derive each one at edit time:** the spec has shipped stale anchors before (§9.5b).*

**Status: DRAFT. Rulings R8-1–R8-7 decided by team-lead 2026-10-13 (§1). p7-reviewer's plan review is folded in (2026-10-14; where each finding landed is in §11). No code is written until the plan is approved (CLAUDE.md rule 7).**

## 0. What phase 8 is, in one paragraph

§1.3 defines **R2 — Dormant**: *"Quartz build steps off by default; `com.brain.build` daemon
uninstalled. Code present, tests green. The vault-mirror steps of the old `wiki` stage keep
running."* §11's phase-8 row: *"**A10** re-form the `wiki` stage into a `mirror` stage; delete only
the two Quartz steps. Uninstall `com.brain.build`; keep `com.brain.watcher`/`com.brain.brief`.
**Q8** launchd gate. **Q10** Publish tab. Exit: No Quartz build runs unless requested. Mirror
maintenance still runs. Suite green."*

The user has decided (2026-10-12):

- **Q8:** the vault-sync watcher is installed unconditionally, and `brain doctor` reports it when
  it is not running.
- **Q10:** the Publish tab is deleted.
- **Phase 9 stays gated.**
- **Ask before touching any launchd job on this machine.**

**Out of scope:** every R3 deletion. `wiki/`, `quartz_overrides/`, `quartz_overlay.py`,
`brain wiki install`, `brain vault render`, Caddy, the wiki tests, `_check_caddy` and
`_check_quartz_sha` all stay. Q17 (retiring the whole tab strip) stays undecided; only Publish goes.

## 1. Rulings — decided by team-lead, 2026-10-13

| # | Question | Ruling |
|---|---|---|
| **R8-1** | **What "unconditionally" means for Q8.** `_maybe_install_launchd` (`setup.py:831`) has four gates: `wiki_installed` (`:860`); `sys.platform != "darwin"` (`:863`); `--non-interactive` never installs (`:866`, the documented "ticket contract"); and an interactive prompt that defaults to **No** (`:877-881`). It is reached only when `prof.offer_daemons` is set, the `full` profile (`:1341`). | **Remove the `wiki_installed` gate only.** The user's Q8 was asked in exactly these words: *"should `brain setup` install the vault-sync watcher launchd job unconditionally (today it only installs if the wiki is installed)?"* So "unconditionally" means "not conditional on the wiki". **Keep** the macOS gate, the `--non-interactive`-never-installs contract and the prompt. The `brain doctor` check (T4) is the loud half. No user question is needed |
| **R8-2** | **How a Quartz build is "requested" at R2.** A10 says delete the two Quartz steps (`maintenance.py:80-87`). The exit criterion says "unless requested", and R2 is meant to be reversible ("code present"). | **Accepted: move the two steps into an opt-in `quartz` stage** that `brain-rebuild --only quartz` selects and the default plan never runs. R3 deletes the stage |
| **R8-3** | **`--wiki-only` and `--clean-cache`.** | **Accepted.** `--wiki-only` exits 2 with a message naming `--only mirror` and `--only quartz`. `--clean-cache` is valid only when the `quartz` stage is selected; otherwise it is a usage error |
| **R8-4** | **Wiki freshness in `brain doctor` at R2.** `wiki_freshness_doctor_check` (`doctor_runtime.py:654`) has two FAIL branches, and both name a bare `brain-rebuild` as the remedy. The first is "NO completed build" (`:676-685`, remedy `:683`). The second is a newest build more than `WIKI_STALE_DAYS` (3, `:62`) old (`:690-700`, remedy `:698`). After A10, a bare `brain-rebuild` no longer builds. | **Accepted, and a must: a permanently red doctor is a defect.** At R2 **both** branches report the wiki dormant, as OK, and neither FAILs. With a build: "dormant since R2, last built <date>; `brain-rebuild --only quartz` rebuilds". With no completed build: "dormant since R2, never built; `brain-rebuild --only quartz` builds". Both remedies name **`brain-rebuild --only quartz`**, never a bare `brain-rebuild`. The `wiki_stale` coupling in `daemon_doctor_check` (`:355`, `:458`) is removed. It reads `any(status == "fail")` over this check (`:121`), so it also used to see the no-build branch |
| **R8-5** | **A9's shape.** | **The doctor check, now:** no watcher → WARN; installed but down → FAIL, as today (`doctor_runtime.py:450`). **The UI status line is out of phase 8.** §11's phase-8 row does not name A9, and its exit criterion is "No Quartz build runs unless requested. Mirror maintenance still runs. Suite green." A9 belongs to **phase 4's** row ("A9 watcher dependency + doctor check"), and the "doctor **and** the UI status line" minimum is A9's own wording in §8.5. Phase 4 never shipped A9 (`git grep -i "watcher not running" src` finds only `monitor.py`'s build-watcher message). Phase 8 ships A9's doctor half because Q8 asks for it; **the UI status line is a recorded follow-up** (§10), owed by A9 |
| **R8-6** | **`brain-up` at R2.** | **Accepted.** The overlay (`brain-up.sh:105`) and the cold-start Quartz build (`:131-153`, `:193`) run only with `BRAIN_WIKI_BUILD=1`. The banner says the wiki is dormant and names `brain-rebuild --only quartz`. Caddy is untouched |
| **R8-7** | **`brain-monitor` at R2.** There is no `--probe` flag. Probe is the positional command `brain-monitor probe [N]` (`monitor.py:477`; the bare-integer timeout is accepted at `:530`). `_probe` already exits 1 at once when the build pid is dead (`:371-373`), so it never times out. Its message, "build watcher not running — start with brain-up", is now wrong advice: `brain-up` no longer starts a build watcher. | **Accepted.** The minimal change: the build watcher is labelled "dormant (R2)" in the snapshot (`:204-206`), and `probe` keeps its immediate exit 1 but says the build is dormant and names `brain-rebuild --only quartz` (or `BRAIN_WIKI_BUILD=1 brain-up`) instead of "start with brain-up". Its Quartz-awareness goes at R3 |

## 2. Inventory — everything that runs a Quartz build today (read at `0965b61`)

| Path | What it does | Phase-8 fate |
|---|---|---|
| `src/brain/maintenance.py:70-88` `wiki_steps`, stage `"wiki"` `:127` | export → sync-summaries → prune-orphans → `vault render --overlay --no-build` → `python -m brain.wiki.build_swap` | **A10:** a `mirror` stage with the first three steps; the last two go to the opt-in `quartz` stage (R8-2) |
| `maintenance.py:42` `ALL_STAGE_IDS`; `:140-148` `wiki_only`; `:266` `stage_id == "wiki" and clean_cache`; `:296` the argparse description; `:300-302` `--wiki-only`; `:317-319` `--clean-cache`; `:344` | stage registry and flags | §9.2d(6), every site. R8-3 |
| `src/brain/templates/launchd/com.brain.build.plist.j2` → `.shims/_brain-build-fg` → `python -m brain.wiki.build_watcher` | rebuilds on every vault change, KeepAlive | **no longer installed**; an existing install is removed on the next `brain-install-launchd` (T2). The template and wrapper stay until R3 |
| `src/brain/bin/launchd.py:32` `_LABELS` | install, uninstall and doctor share one tuple that includes `com.brain.build`; `install_plists` installs every label (`:151`) and shims `_brain-build-fg` (`:143-144`) | split into installed and retired labels (T2) |
| `src/brain/templates/bin/brain-up.sh:105`, `:131-153`, `:193` | overlay + cold-start build on every `brain-up` | R8-6 (T5) |
| `brain-up.sh:155-175` `ensure_supervised` | runs `brain-install-launchd` on **every** `brain-up` | unchanged, but it now installs only watcher + brief. **Launchd STOP point S2** |
| `brain vault render` (`cli.py:6682`) and `brain wiki install` | user-invoked | **requests** — stay until R3 |

**Mirror maintenance must keep running.** Spec §9.4 and §9.2d(5): `vault prune-orphans` is a
deleter, and it and `emit_people_pages` live or die together. The `mirror` stage keeps
`prune-orphans` with the exact argv and `fatal=False` it has today (`:73-80`). T1 pins that with a
test. The People Hub emitter is not touched in phase 8.

## 3. STOP-and-ask points — launchd and installed agents

The user's rule is to ask before touching any launchd job on this machine. The code changes
themselves are safe to write and test. Every test runs against `BRAIN_LAUNCHD_DIR=<tmp>` and a
fake `BRAIN_LAUNCHCTL`, as `tests/test_bin_launchd.py` already does. **Running** any of the
following for real is a stop:

- **S1. `brain-install-launchd`, `brain-uninstall-launchd`, `brain setup --daemons`, or
  `brain uninstall`, against the real `~/Library/LaunchAgents`.** After T2,
  `brain-install-launchd` will boot out and delete an installed `com.brain.build`. That is the
  phase's intended migration, and on this machine it needs the user's go.
- **S2. `brain-up` or `brain-down` without `BRAIN_NO_BUILD_WATCHER=1`.** `brain-up` runs
  `brain-install-launchd` (S1); `brain-down` boots out `com.brain.watcher` and `com.brain.build`
  (`brain-down.sh:56-57`). Every e2e uses `BRAIN_NO_BUILD_WATCHER=1` plus a fake launchctl.
- **S3. Reading live launchd state** (`launchctl list` / `print` on the real domain) is read-only
  and allowed, but only to report. Nothing is booted out to "test" the doctor check.
- **S4. Verifying the migration end to end on this machine** (the exit criterion's "`com.brain.build`
  uninstalled") is a stop. Propose the exact commands, wait for the user, run them, and record the
  output.

**After merge, the migration runs itself.** `brain-up` runs `brain-install-launchd` on every
start (`brain-up.sh:155-175`, `ensure_supervised`). So **the user's next `brain-up` after phase 8
merges boots out and deletes `com.brain.build`** without any separate step. That is the intended
R2 transition, but it must not surprise anyone: the **PR body** and the **CHANGELOG** both say so,
in plain words, with the way back (`brain wiki install`, then `brain-install-launchd` from a
pre-phase-8 build, or `brain-rebuild --only quartz` for a one-off rebuild). **The first way back is
not durable on this release:** its `brain-up` runs `brain-install-launchd` on every start, which
boots out and deletes `com.brain.build` again. So keeping the daemon means staying on the earlier
release, and on this release the wiki is rebuilt on request with `brain-rebuild --only quartz`
(T7 review, LOW).

## 4. Exit criterion, made testable

Every item is a test that fails if the behaviour reverts.

- **E1** The default `brain-rebuild` plan contains no Quartz step. `--dry-run` lists `mirror` and
  never `quartz`; no default selection spawns `brain.wiki.build_swap` or `vault render`. The opt-in
  stage stays out of every selection that does not name it: `--skip graph` (and any other `--skip`)
  still excludes `quartz` (`select_stages`, `maintenance.py:135-166`). A selection that names both
  keeps registry order: `--only mirror,quartz` runs `mirror` first.
- **E2** `--only mirror` runs exactly `vault export`, `vault sync-summaries` and
  `vault prune-orphans --apply --include-stale`, in that order, with the same fatal flags. Mirror
  maintenance still runs.
- **E3** `--wiki-only` exits 2, and its message names the replacement (R8-3).
- **E4** A Quartz build runs only when requested: `--only quartz` runs overlay + `build_swap`, and
  `--clean-cache` is refused without it (R8-2, R8-3).
- **E5** `install_plists` writes and bootstraps `com.brain.watcher` and `com.brain.brief` only. It
  never writes `com.brain.build.plist` or the `_brain-build-fg` shim. A pre-existing
  `com.brain.build` is booted out and its plist removed (fake launchctl, tmp dir).
- **E6** `uninstall_plists` still boots out a legacy `com.brain.build` **and removes its plist**
  (`bin/launchd.py:209-229`). `brain-down` still **boots it out** (`brain-down.sh:56-57`). It never
  removes a plist: that is `brain-uninstall-launchd`'s job (`brain-down.sh:42-43`).
- **E7** `brain setup` offers daemons whether or not the wiki is installed (R8-1). `--non-interactive`
  still installs nothing. The prompt (`setup.py:877-881`; `:879` today reads "auto-start
  Postgres + wiki watcher on login") names the vault-sync watcher and the daily brief. It drops "wiki watcher",
  and it **also drops "auto-start Postgres"**: no Postgres LaunchAgent exists. The only labels are
  watcher, build and brief (`bin/launchd.py:32`), so that clause was already false.
- **E8** `brain doctor` reports a missing watcher (R8-5), warns about a still-installed
  `com.brain.build`, and never FAILs on wiki staleness **or on a `.quartz` with no completed build**
  (R8-4; both branches are dormant OK and name `brain-rebuild --only quartz`). The retired label is reported by the
  new check ONLY. `daemon_doctor_check`'s status loop covers the installed labels; today it FAILs any
  plist that is "installed but NOT LOADED" (`doctor_runtime.py:441-443`). `brain-down` boots the
  build daemon out but leaves its plist, and a loaded legacy build exiting 0 reports OK, so both
  would contradict the WARN. Tests cover a retired plist that is **loaded** and one that is **not
  loaded**: one WARN each, and no FAIL and no OK from the status loop.
- **E9** `brain-up` runs no overlay and no Quartz build unless `BRAIN_WIKI_BUILD=1`, and its banner
  says the wiki is dormant (R8-6).
- **E10** The Publish tab is gone: its button, its section, `"publish"` in `main.js`'s tab list,
  and the `ui/__init__.py` docstring sentence. Every remaining tab still switches, in a real
  browser, with **no `pageerror` and no console error**.
- **E11** Docs and skills no longer describe a wiki that rebuilds itself. Pinned by
  `tests/test_reading_surface_pointers.py`, the same way phase 7 did it.

## 5. Tasks (TDD — test first, see it fail, then implement)

Order: **T0 first, alone, and nothing else starts until it is merged into the phase branch.**
Then **T1 → T2 → T3 → T4 → T5 → T6 → T7.** **T1 and T2 are NOT file-disjoint.** Both edit
`tests/test_bin_scripts.py`: T1 at `:674-700` (the `brain-rebuild --dry-run` stage list), T2 at
`:291-361` and `:930-1000` (the `com.brain.build` bootstrap pins). So they run **in sequence, T1
then T2**, never in parallel worktrees. T0 also edits that file (`_make_env`, the `stub_dir`
fixture) and T5 edits it after T2, so every task that touches it is serial. Only **T6** touches
files disjoint from all the others, so it alone can run in a parallel worktree. T3 depends on T2's
label split. T4 depends on T2. T5 depends on T1's stage ids.

### T0 — Make the suite unable to touch the real launchd (machine safety; review MEDIUM)

**Why first.** `tests/conftest.py`'s session fixture `_force_test_runtime_home` (`:156-198`) points
`BRAIN_LAUNCHD_DIR` at a tmp dir (`:190`) but leaves **`BRAIN_LAUNCHCTL` unset**. Three paths fall
back to the real binary: `install_plists` runs `launchctl bootout` and `bootstrap`
(`bin/launchd.py:164-175`); `install_main` and `uninstall_main` read `BRAIN_LAUNCHCTL or
"launchctl"` (`:246`, `:257`); and `_maybe_install_launchd` does the same (`setup.py:893`).
Phase 8 adds tests on exactly these paths: T2's retired-label bootout, R8-1's gate removal, and
T3's "`--daemons` installs without a prompt", which runs on this Mac. One un-doubled call and the
TEST SUITE touches the user's launchd, which is the thing §3 forbids.

- **A second hole, found while checking this task against the source:** the env stub alone does
  not cover a DIRECT call. `install_plists` and `uninstall_plists` take
  `launchctl: str = "launchctl"`, a literal default (`bin/launchd.py:128`, `:198`) that never
  reads the environment. So `install_plists(brain_home, launchd_dir)` with no `launchctl=` runs the
  real binary whatever conftest sets. Every current test passes `launchctl=` explicitly
  (`test_bin_launchd.py:129,158,183,190,216,271,424,499`), so this is latent, but phase 8 adds
  callers. **Fix:** the default becomes `None`, resolved through ONE helper (e.g.
  `bin/launchd.resolve_launchctl()`: `BRAIN_LAUNCHCTL` or `"launchctl"`). `install_main`,
  `uninstall_main`, `setup._maybe_install_launchd` (`:893`) and `doctor_runtime._default_launchctl`
  (`:92-94`) all use it, rather than four copies of the same `or`. (`brain uninstall` reaches
  launchctl only through `uninstall_main`, injected as `_launchd_uninstall` in `uninstall.py`, so
  the resolver covers it as well.)
- **A third hole: the subprocess tests rebuild the environment from scratch.**
  `tests/test_bin_scripts.py`'s `_make_env` (`:147-171`) sets `PATH` to
  `f"{stub_dir}:/usr/bin:/bin"` (`:163`) and starts from an empty dict. That **drops
  `BRAIN_LAUNCHCTL` and the conftest `PATH`-shadow dir**, so neither of the conftest layers reaches a
  `brain-up`, `brain-down` or `brain-install-launchd` subprocess. `/bin/launchctl` exists on macOS,
  and `brain-down.sh` runs `${BRAIN_LAUNCHCTL:-launchctl}` (`:37`) for `print` and then `bootout`
  (`:50-51`). So a new phase-8 test of `brain-down`, `brain-up` or `brain-install-launchd` that
  forgets the `BRAIN_LAUNCHCTL` extra **runs the real launchctl and can boot out the user's live
  watcher.** The existing ones are safe only because each one remembers that extra (`:331`, `:557`,
  `:950`, `:1028`, `:1072`, `:1120`). **Fix, in T0:**
  1. The `stub_dir` fixture (`:92-101`) seeds a **recording, non-zero `launchctl` stub** in
     `stub_dir`. It records argv to its own log, e.g. `launchctl.unexpected`, and exits 1. A test that
     needs a working launchctl overwrites it with `_write_stub(stub_dir, "launchctl", body=...)`, as
     `:315`, `:543`, `:943`, `:1021`, `:1063` and `:1110` already do.
  2. `_make_env` **defaults `BRAIN_LAUNCHCTL`** to `stub_dir / "launchctl"`. `extras` still overrides
     it.
  3. A **static guard**:
     `shutil.which("launchctl", path=_make_env(stub_dir=..., vault_dir=...)["PATH"])` resolves to a
     path inside `stub_dir`, never `/bin/launchctl`, and `_make_env(...)["BRAIN_LAUNCHCTL"]` is that
     same stub. The mutations are to drop the seeded stub, or to drop the default; either one turns
     the guard red, and neither runs anything.
  4. **Audit every test that builds an environment from scratch** (`git grep -n '"PATH":' tests`,
     re-derive at edit time). At `0965b61` the list is:

     | Site | Runs | Can it reach launchctl? |
     |---|---|---|
     | `tests/test_bin_scripts.py:163` (`_make_env`) | `brain-up`, `brain-down`, `brain-install-launchd`, `brain-uninstall-launchd`, `brain-rebuild` | **Yes.** Fixed by 1–3 |
     | `tests/test_bin_scripts.py:835` | `_brain-watcher-fg` with an empty stub dir | No. The wrapper never calls launchctl, and its only mentions are comments. Gets the same default anyway, for uniformity |
     | `tests/test_brain_skills_sync.py:48`, `:633`, `:676` | `bin/brain-skills-sync` (`PATH=/usr/bin:/bin`) | No. The script has no launchctl reference |
     | `tests/test_install_sh.py:31` | `install.sh` with `BRAIN_INSTALL_SH_DRY_RUN=1` | No, but only because of dry-run. A non-dry `install.sh` `exec`s `brain setup` (`install.sh:62-68`) with the **real `HOME`** (`test_install_sh.py:32`). Record that every test here must stay dry-run |
     | `tests/test_conftest_db_lock_gate.py:107` | a child `pytest` with `-p tests.conftest` | No. The child loads conftest, so the T0 session layers re-apply inside it |
     | `tests/test_mcp_server_protocol.py:46` | `brain-mcp` over stdio | No. It inherits `PATH`, so it still sees the conftest shadow dir, and no MCP tool calls launchctl (`git grep -l launchctl src` names only `bin/launchd.py`, `setup.py`, `doctor_runtime.py`, `uninstall.py` and the shell/plist templates) |
     | `tests/test_pii_hook_home_path.py:179`, `tests/test_secret_pattern_hook_parity.py:182` | `scripts/hooks/pre-commit` | No |

     The list goes into the T0 commit message, so the next author re-derives it rather than
     trusting it.
- **Files:** `tests/conftest.py` (the session fixture: `BRAIN_LAUNCHCTL` plus the `PATH`-shadow
  dir); `tests/test_bin_scripts.py` (the `stub_dir` fixture and `_make_env`, per the third hole);
  `src/brain/bin/launchd.py` (the `None`
  defaults and the shared resolver); `src/brain/setup.py:893` and `src/brain/doctor_runtime.py:92-94`
  (call the resolver). A stub script (a `tests/` helper, or written into the session tmp dir)
  records its argv to a file and **exits non-zero**.
- **Ceiling bookkeeping is owed by T0, not T3.** `setup.py` is a **listed** over-800 file (§6), and
  T0 edits it (`:893`) and lands first. So the T0 commit carries the full ceiling record for
  `setup.py`. It promotes the trail's trailing descriptive hop and adds a descriptive hop for T0.
  Then it either (a) adds the inline comment at `:893` and the module-docstring pointer, or (b)
  records in the row that the change is **net-zero**. Swapping the inline `or` for a resolver call
  should be net-zero or net-negative, so (b) is expected. `wc -l` decides, not this paragraph.
- **TDD:**
  1. A guard test: with only the conftest environment, an un-doubled `install_plists(...)` call
     fails (the stub's non-zero bootstrap raises `LaunchdError`). The stub's record shows the call
     reached the stub, so the real `launchctl` was never run.
  2. The same for `install_main()` and for `_maybe_install_launchd(..., daemons=True)` with
     `install_plists` NOT doubled.
  - **Isolation for every T0 guard test** (review LOW). An un-doubled `install_plists` **writes real
    plists** into `BRAIN_LAUNCHD_DIR` (`bin/launchd.py:159-162`, e.g. `com.brain.watcher.plist`) and
    shims into `brain_home` (`:142-144`) **before** the stub's non-zero bootstrap raises. Conftest's
    `BRAIN_LAUNCHD_DIR` and `BRAIN_HOME` are **session-scoped** (`_force_test_runtime_home`,
    `tests/conftest.py:156-198`). So a guard test left on them would leave a `com.brain.watcher.plist`
    in the session dir, and every later doctor test would see an "installed" watcher, which pollutes
    the `daemons` check by test order. Each guard test therefore `monkeypatch.setenv`s
    **`BRAIN_LAUNCHD_DIR` and `BRAIN_HOME` to per-test `tmp_path` dirs** and passes them explicitly
    where the API takes them. Each one also patches **`sys.platform` to `"darwin"`**, because
    `_maybe_install_launchd` returns early off macOS (`setup.py:863`). Without the patch the guard
    passes vacuously on Linux CI, never reaching the call it guards.
  3. Audit the other reader: `doctor_runtime._default_launchctl` (`:92-94`) also honours
     `BRAIN_LAUNCHCTL`, so a doctor call that reaches `launchctl list` without an explicit
     `launchctl=` now gets the stub's non-zero exit (a "warn"). Run the full suite once with the
     stub in place. Every test that changes outcome is either given an explicit double or
     documented as relying on the stub. Tests that already set `BRAIN_LAUNCHCTL` are
     unaffected. Some use `monkeypatch.setenv` (`test_bin_launchd.py:123,230`), which overrides the
     session value. Others pass it in the subprocess env (`test_bin_scripts.py:331,557,950,1028,1072,1120`),
     which overrides the new `_make_env` default.
- **Mutation plan:** (a) remove the conftest `BRAIN_LAUNCHCTL` line; (b) restore
  `install_plists`'s literal `"launchctl"` default; (c) drop the seeded `launchctl` stub from
  `stub_dir`, and separately (d) drop `_make_env`'s `BRAIN_LAUNCHCTL` default. The static guard
  goes red under (c) and (d) without executing anything. The guard test must go red under each, at its
  "the stub recorded the call" assertion. That is the proof it would have caught a real
  `launchctl` call, without making one: under the mutation, the guard asserts BEFORE invoking that
  the resolved binary is the stub and FAILS there, rather than running `launchctl`.
- **Risk to manage while building it: the mutation proof itself must never execute the real
  binary.** Checking the resolver before calling is NOT enough. Under mutation (b) the resolver
  still returns the stub, the pre-check passes, and `install_plists` then runs a bare `launchctl`.
  So the backstop has two layers:
  1. `BRAIN_LAUNCHCTL` points at the recording stub (env stub);
  2. conftest **prepends a session tmp dir to `PATH`** that holds a SECOND stub named `launchctl`
     (PATH stub), recording to a different file and exiting non-zero.

  A bare `launchctl`, whether from Python's default or a shell `${BRAIN_LAUNCHCTL:-launchctl}`,
  then resolves to the PATH stub, never `/bin/launchctl`. Under either mutation the call lands on a
  stub, and the guard goes red because the env stub's record is empty while the PATH stub's is not.
  The real binary is never reached. Absolute-path invocations do not exist in `src/`
  (`git grep -n '/bin/launchctl' src` is empty at `0965b61`, re-check); the guard asserts that too.
- **Rule for every later task:** T3's tests double `install_plists`, as `tests/test_setup.py:671`
  does with `patch("brain.bin.launchd.install_plists", ...)`; T2's use the recording fake launchctl
  passed explicitly. The conftest stub is the backstop, not the double.


### T1 — A10: the `mirror` stage (and the opt-in `quartz` stage)

- **Files:** `src/brain/maintenance.py` (376 lines; under the ceiling).
- **Tests:** `tests/test_maintenance.py` (417 lines). The stage registry is pinned at `:21`, and
  `wiki_only`/`"wiki"` selection at `:88-121`. The `build_watcher` strings at `:141,149` are
  process-list fixtures for `ingest_in_flight`, not stage argv. Spec §9.3 trap 5 calls them
  "asserts `build_watcher` argv", which is wrong; they stay, since a dormant install can still run
  one. `tests/test_quartz_parser_cache_static.py:476-482` keeps passing: `_PARSER_CACHE_RELPATH`
  survives with the `quartz` stage (R8-2). `tests/test_bin_scripts.py:674-700`
  (`test_brain_rebuild_dry_run_lists_stages`, whose stage tuple ends in `"wiki"`). This is the
  shared file that makes T1 → T2 serial (§5).
- **TDD:** red tests first for E1–E4: the dry-run plan text; the `mirror` steps' argv, order and
  fatality, compared against a literal so the test cannot follow a refactor; `--wiki-only` exit 2
  and its message; `--only quartz` runs build_swap with `BRAIN_WIKI_RELOAD=1`; `--clean-cache`
  without `quartz` exits 2. Then implement: `ALL_STAGE_IDS`, `build_stages`, `select_stages` (a
  `default=False` stage concept, or an explicit exclusion; decide from the code),
  `run_stages`'s cache wipe keyed to `"quartz"`, `_parse_args`, and `main`.
- **Mutation plan:** put a Quartz step back in `mirror` (E1 red); drop `prune-orphans` from `mirror`
  (E2 red; this is the §9.4 deleter coupling, which deserves its own guard); make `--wiki-only` an
  alias for `--only mirror` (E3 red); include `quartz` in the default selection (E1/E4 red); key the
  cache wipe back to `"wiki"` (E4 red).
- **Risks:** someone's script calls `--wiki-only` (the message tells them what to use);
  `BRAIN_WIKI_KEEP_BUILDS` (`:336`) still feeds `--keep` for the `quartz` stage; trap 1
  ("wiki" is overloaded), so never grep-replace.

### T2 — Stop installing `com.brain.build`; migrate it away

- **Files:** `src/brain/bin/launchd.py` (281 lines).
- **Tests:** `tests/test_bin_launchd.py` (506 lines; parametrises over `_LABELS` at
  `:132,163,185,192,236,320,341,359,366,374,412`, so every site needs a decision);
  `tests/test_log_rotation.py` (references `com.brain.build`; re-derive why). New tests go in a new
  module (`tests/test_bin_launchd_retired.py`) so the old one does not grow past 800.
  **Also `tests/test_bin_scripts.py` (starts after T1, §5).** Two subprocess tests pin
  `com.brain.build` as an INSTALLED label and **go red** under E5, so each needs rewriting, not
  deleting:
  - `test_brain_up_bootstraps_launchd_when_supervisor_not_skipped` (`:291-361`). It asserts
    `len(bootstrap_calls) == 3` (`:349`), a `com.brain.build.plist` bootstrap (`:355`), and the build
    plist on disk (`:361`). It becomes 2 bootstraps (watcher + brief), with no build plist written.
  - `test_install_launchd_writes_plists_and_bootstraps` (`:930-1000`). It asserts the build plist
    exists and checks its content (`:964-966`, `:984-992`), plus 3 bootstraps including build
    (`:998-1000`). It becomes "no build plist, 2 bootstraps". Add a pre-seeded-build case, or leave
    that to the new module.

  The `brain-uninstall-launchd` and `brain-down` tests that follow (`:1004` onwards) pin E6 and
  stay green.
- **TDD:** red tests first for E5 and E6, with a recording fake launchctl and a tmp
  `launchd_dir`:
  - install writes and bootstraps exactly watcher + brief;
  - it shims only `_brain-watcher-fg` and `_brain-brief-fg`;
  - a pre-seeded `com.brain.build.plist` is booted out and deleted;
  - a second install is idempotent;
  - uninstall still sweeps all three.

  Then implement: `_INSTALLED_LABELS = (watcher, brief)`, `_RETIRED_LABELS = (build,)`, and their
  union for **uninstall only**. The doctor's status loop covers `_INSTALLED_LABELS`; the retired
  label is reported only by T4's new check (E8). The names are a proposal; read the importers
  first: `doctor_runtime.py:40` imports `_LABELS as DAEMON_LABELS`, and **`DAEMON_LABELS` has
  two consumers. Both switch to the installed labels:**
  - `daemon_doctor_check`'s status loop (`:400`);
  - `daemon_code_doctor_check` (`:575`), which compares each installed plist's pinned `BRAIN_PY`
    against the running code.

  If only the first one switches, a leftover `com.brain.build.plist` still gets a stale-code
  verdict from the second. That would make the retired label reported by two checks, which is
  what E8 forbids.
- **Mutation plan:** add `com.brain.build` back to the installed set (E5 red); skip the retired
  bootout (E5 red); drop the retired label from uninstall (E6 red).
- **Risks:** this is the code path that touches launchd when a user runs it: **S1, S4**. The
  bootout of the retired label must suppress "not loaded" as `install_plists` already does (`:166-170`).

### T3 — Q8: the launchd gate in `brain setup`

- **Files:** `src/brain/setup.py` (1,364 lines, **listed over the ceiling**; see §6),
  `_maybe_install_launchd` `:831-895` and its call at `:1341-1349`.
- **Tests:** `tests/test_setup.py` (1,322 lines; `:436` patches `brain.wiki.install.wiki_install`).
  Put the new tests in a new module (`tests/test_setup_daemons.py`).
- **Two existing tests pin the gate R8-1 removes, and would stay green for the wrong reason.
  Both are rewritten in T3, not left alone:**
  - **`test_launchd_skipped_when_wiki_interactively_declined` (`test_setup.py:1281-1322`).** It
    patches `typer.confirm` to `False` for **every** prompt (`:1297`), so the wiki decline and the
    daemon prompt both answer No. Once the `wiki_installed` early return is gone, the daemon
    prompt's own No still keeps `install_plists_calls == []`. The test stays green and no longer
    tests anything. **Rewrite:** `typer.confirm` gets `side_effect=[False, True]`, so the wiki is
    declined and the daemons accepted. The assertion inverts: `install_plists` is called **exactly
    once**. Rename it to what it now pins, e.g. `test_launchd_offered_when_wiki_interactively_declined`.
    **Mutation:** restore the `wiki_installed` early return. The rewritten test must go red at the
    `called once` assertion. The old `return_value=False` shape would not.
  - **`test_setup_launchd_skipped_when_skip_wiki` (`test_setup.py:384-400`).** Its rationale
    assertion, `"wiki not installed" in out_skipped or "wiki" in out_skipped.lower()` (`:396`), is
    satisfied by any dry-run output that mentions the wiki at all, which every `full` run does. It
    cannot fail. **Tighten, and make it platform-correct.** `_dry_run_output`
    (`test_setup.py:245-282`) does not patch `sys.platform`, so on Linux CI the macOS gate
    (`setup.py:863-865`) prints `[skipped] launchd install (not macOS)` and returns before the
    `--non-interactive` line is ever reached. A test that demands the `--non-interactive` line
    therefore passes on this Mac and fails on Linux CI. So, under `--skip-wiki` with a
    `--non-interactive` full run:
    - **on darwin**, assert the `--non-interactive` skip line (`setup.py:869-872`);
    - **elsewhere**, assert the `not macOS` skip line (`:864`). This is the same branch the test
      already takes for its second half (`if sys.platform != "darwin":`, `test_setup.py:411-414`);
    - **on both**, assert that `launchd install (wiki not installed)` (`:861`) is **absent**. That
      single assertion is the one the restored-early-return mutation turns red on either platform,
      because the wiki gate runs before the platform gate (`:860` precedes `:863`).

    **Do not patch `sys.platform` inside `_dry_run_output`.** Every other caller of the shared
    helper would silently change platform. Rename the test to match, since `--skip-wiki` no longer
    suppresses launchd.
- **Doubles:** every test here doubles `install_plists`, as `tests/test_setup.py:671` does. The
  T0 stub is the backstop.
- **`_SHIM_NAMES` (`setup.py:185-191`, written at `:1099`)** includes `_brain-build-fg`. **Drop it**
  in phase 8. Nothing installed at R2 invokes the shim: T2 stops both the plist and
  `install_plists`'s own `ensure_shim` of it (`bin/launchd.py:143-144`). The template
  (`src/brain/templates/bin/_brain-build-fg.sh`) stays until R3, and an existing shim on disk is
  left alone. A test asserts setup no longer writes it.
- **TDD:** red tests first for E7, per R8-1:
  - daemons offered with `wiki_installed=False`;
  - `--non-interactive` still installs nothing;
  - `--daemons` installs without a prompt;
  - the prompt text names the vault watcher and the brief.

  Then remove the `wiki_installed` parameter, or keep it with a different meaning; read every
  caller first, including `tests/test_reading_surface_pointers.py`, which builds
  `_print_final_report` with `wiki_installed`.
- **Mutation plan:** restore the `wiki_installed` early return (E7 red); make `--non-interactive`
  install (contract test red); restore the "wiki watcher" prompt text (E7 red).
- **Risks:** `_print_final_report` also takes `wiki_installed` (`:903-930`) and must keep it: the
  wiki is still installable at R2, and its line still prints, labelled legacy. Ceiling bookkeeping.

### T4 — A9 / Q8: the watcher check, and dormant wiki freshness in `brain doctor`

- **Files:** `src/brain/doctor_runtime.py` (703 lines; under the ceiling, but close). Put the new
  check in a **new module** (`src/brain/doctor_watcher.py`) so `doctor_runtime.py` shrinks or holds
  rather than approaching 800.
- **Tests:** `tests/test_doctor_runtime.py` (725 lines; `:33` hard-codes the three labels; `:250-310`
  exercise `com.brain.build` and `wiki_stale`). New tests go in `tests/test_doctor_watcher.py`.
- **Scope:** the doctor half of A9 only (R8-5). The UI status line is a follow-up.
- **TDD:** red tests first for E8, all through the existing seams (`launchd_dir`, `launchctl`,
  `platform`, `now`):
  - macOS, no watcher plist → WARN with remedy `brain-install-launchd` (R8-5);
  - installed but down → FAIL (the existing behaviour, re-pinned);
  - a retired `com.brain.build` plist present → WARN naming it;
  - a stale wiki → OK "dormant", never FAIL (R8-4); the remedy names `brain-rebuild --only quartz`;
  - **a `.quartz/` with no completed build → OK "dormant, never built", never FAIL** (R8-4). This is
    the other FAIL branch, `doctor_runtime.py:676-685`. Its remedy also names `brain-rebuild --only
    quartz`, not the bare `brain-rebuild` at `:683`;
  - non-macOS → a process scan finds a running `brain vault sync --watch`, or WARN.

  **The process scan is a seam, not a live `ps` call in tests.** Thread a `processes:
  Sequence[str] | None = None` keyword through `runtime_doctor_checks` (`doctor_runtime.py:97-104`,
  beside `launchd_dir`/`launchctl`/`platform`/`now`) into the new check. This is the same pattern as
  `maintenance.ingest_in_flight(processes=None)` (`maintenance.py:208-211`): `None` means "take a
  snapshot", and tests pass a literal list. To share the parser rather than copy it, **make
  `_snapshot_processes` public** (`maintenance.py:176`, e.g. `snapshot_processes`) and update its
  one caller (`:210`). Then remove `daemon_doctor_check`'s `wiki_stale` parameter and its branch.
- **Mutation plan:** report the missing watcher as OK (red); FAIL on staleness again (red); FAIL on
  the no-completed-build branch again, or restore its bare `brain-rebuild` remedy (red); drop the
  retired-plist warning (red); flip the non-macOS process match (red).
- **Risks:** a doctor that goes red on a fresh minimal install (R8-5); the `ps` scan's portability
  (it is already used by `ingest_in_flight`); **S3** (read-only probing only).

### T5 — `brain-up`, `brain-status`, `brain-monitor` at R2

- **Files:** `src/brain/templates/bin/brain-up.sh` (231 lines), `brain-status.sh` (83),
  `src/brain/bin/monitor.py` (570).
- **Tests:** `tests/test_bin_scripts.py` (1,144 lines; the `build_swap` assertion is at `:283`, and
  `:220-221` is that test's docstring; `:409` pins the phase-7 banner).
  `tests/test_reading_surface_pointers.py`. New tests go in a new module if they would push
  `test_bin_scripts.py` further.
- **TDD:** red tests first for E9, using the existing hermetic shims (`BRAIN_NO_BUILD_WATCHER=1`,
  `BRAIN_PY`, tmp pid paths):
  - with `BRAIN_WIKI_BUILD` unset, neither `vault render --overlay` nor `build_swap` is spawned
    (and the cold-start build does not run when `current/` is missing);
  - `BRAIN_WIKI_BUILD=1` restores both;
  - the banner says "dormant";
  - `brain-status` labels the build watcher dormant;
  - `brain-monitor probe` (a positional command; there is no `--probe` flag: `monitor.py:477`,
    `:530`). The test calls `main(["probe"])` with `BRAIN_VAULT_PATH` set to a tmp dir and
    `brain.bin.monitor.BUILD_PID` patched to a tmp path with no live pid. This is the precedent at
    `tests/test_bin_monitor.py:226-237`, which pins only `rc == 1`. The new test asserts exit 1 **and**
    a stderr message that says the build is **dormant** and names `brain-rebuild --only quartz` /
    `BRAIN_WIKI_BUILD`, and that does not contain "start with brain-up" (R8-7). `_probe` already
    exits 1 at once on a dead pid (`:371-373`). Only the message changes; there is no timeout to
    remove.

  Also in `brain-up.sh` (the same file, so the same task): the header (`:2` "Start the
  second-brain wiki + sync watcher"; `:6` "This script still builds and serves the wiki"; `:14-30` describe the cold start and "Watcher B" as a running,
  launchd-supervised Quartz rebuilder) and **both** wiki banner lines. `:226` reads "wiki: … (legacy;
  still built until R2 …)", false once R2 lands. `:228` reads "build: re-rendering on vault
  changes". `:226` moved here from T7 because T5 owns this file. All of them now describe a dormant
  wiki, and both banner lines are pinned by a test.
- **Mutation plan:** default `BRAIN_WIKI_BUILD` to 1 (E9 red); restore the `:228` "re-rendering"
  banner line (red); restore `:226`'s "still built until R2" (red); delete the dormant banner line
  (red); restore the probe's "start with brain-up" message (red at the message assertion; the exit
  code alone cannot tell the two apart, which is why the precedent test is not enough).
- **Risks:** **S2.** Never run `brain-up` without `BRAIN_NO_BUILD_WATCHER=1`.

### T6 — Q10: delete the Publish tab

- **Files:** `src/brain/ui/static/index.html` (`:76` button, `:177-189` section),
  `src/brain/ui/static/js/main.js` (`:108` tab list), `src/brain/ui/__init__.py` (`:3-8` docstring),
  and `src/brain/ui/notes_service.py:565`. Its `set_draft` docstring calls the draft toggle "The one
  instant slice of the deferred Publish tab", and the next sentence says the toggle shows or hides
  the note in "the next Quartz build". Once the tab is gone, both clauses describe things that do
  not exist at R2. Reword the docstring only; `set_draft`'s behaviour does not change. The `draft`
  flag still gates every reading surface.
- **Tests:**
  - **Static** (`tests/test_ui_static_behaviour.py` style): no `data-tab="publish"`, no
    `id="tab-publish"`; **every name in `main.js`'s tab list has a `tab-<name>` section, and every
    `.tab` button has a pane.**
  - **Browser** (new module, `browser` marker, added to `.github/workflows/ci.yml`'s explicit list,
    which `tests/test_ci_workflow.py` enforces): clicking each remaining tab shows its pane, AND the
    page raises **no `pageerror` and logs no console error**. Collect both the way the existing
    harnesses do (`tests/ui_graph_harness.py:459`, `tests/test_ui_browser_api.py:204`). The pane
    check alone is not enough: see the mutation plan.
- **TDD:** the static and browser tests first, red against `0965b61`'s tab bar. Then delete.
- **Mutation plan:** delete the section but leave `"publish"` in `main.js`'s list. `"publish"` is
  the LAST name in the loop (`main.js:108`), so each click first switches the earlier panes and
  then throws on `$("tab-publish")` being `null`. A pane-only browser test **survives** this
  mutation (review finding). The `pageerror`/console-error assertion and the list↔section static
  test must both go red. Also: re-add the button (static red).
- **Risks:** Q17 interplay. Ingest and Agent stay; the `.deferred-tab` cascade tests
  (`test_ui_static_behaviour.py:945-960`, `:1552-1638`) still hold with two deferred tabs.

### T7 — Docs, skills, CHANGELOG, closeout

- **Files** (re-derive with `git grep -n "until R2\|still built\|still builds\|wiki-only\|clean-cache"`):
  - `docs/vault-and-wiki.md` (`:9` callout; `:353-362` rebuild flags);
  - `skills/brain-maintenance/SKILL.md` (`:225-231`);
  - `README.md:14` ("still builds");
  - `docs/guides/brain-ui-on-your-phone.md:121-123` ("## The wiki is still there": "Until phase 8 … the Quartz wiki keeps building");
  - **not** `src/brain/templates/bin/brain-up.sh`. T5 owns every line of it (`:2`, `:6`,
    `:14-30`, `:226`, `:228`) because T5 edits that file. `:226` ("legacy; still built until R2")
    and `:6` ("This script still builds and serves the wiki") both moved to T5;
  - **spec §11 row 8 and §9.4 (A10): record R8-2 as a deviation**, the way phase 5 recorded its
    ruling R2 in its §11 row. The two Quartz steps moved to an opt-in `quartz` stage instead of
    being deleted. R3 deletes that stage;
  - CHANGELOG `[Unreleased]`. Say that the next `brain-up` after upgrading removes
    `com.brain.build` (§3), and give the way back;
  - the PR body: the same migration note;
  - spec §11's phase-8 row, marked exited only when E1–E11 hold.
- **Tests:** extend `tests/test_reading_surface_pointers.py` so each sentence fails if it reverts.
- **Closeout:** this file's §10 (rulings, verification, mutation catalog in the phase-7 §10.9
  format, with its validator); auto-memory updates per CLAUDE.md rule 11 (`cli.md` for
  `brain-rebuild` flags; `vault.md`).

## 6. Ceiling (CLAUDE.md "File-size ceiling")

- `src/brain/setup.py`: **listed**. Its trail on this branch ends at `4974f24` (the phase-7 row).
  Touching it means: promote any trailing descriptive hop; add the inline comment at the growth
  site and the module-docstring pointer; add a descriptive hop for this change. **Two tasks touch
  it, and each owes its own hop.** **T0** comes first (`:893`, the resolver call). It promotes
  nothing new, since `4974f24` is already SHA-bound, and it adds T0's descriptive hop, net-zero
  expected (see T0). **T3** comes later and promotes T0's hop to its real SHA before adding its
  own. T3 should be **net-negative or net-zero** (a gate removed, `_SHIM_NAMES` shortened, the
  prompt reworded); if so, say so in the row.
- `src/brain/doctor_runtime.py` (703): new code goes to `doctor_watcher.py`, so it does not grow.
- Every other touched source file is under 800 and stays under. New test modules land under 800;
  `tests/test_cli_ui.py`'s round-11 split is the precedent.
- The row set is re-derived at closeout (`find src -name '*.py' | xargs wc -l | sort -rn`), not
  inherited.

## 7. Guards that must be proven able to fail

Strict five clauses, as in phase 7: passes unmutated; the mutation lands (numstat on a clean tree);
red; red at the targeted `>` or `E` line; restored and green. The per-task mutation plans above are
the starting set; the catalog records the final one. **While a run of record is going, mutation
proofs happen only in a `git archive` copy, never in the worktree** (phase-7 lesson, §10.22).

## 8. Risks

- **R2 is not reversible on the user's machine by code alone.** Once `com.brain.build` is booted
  out, getting it back means `brain wiki install` plus a reinstall from an earlier release, and
  even that lasts only until the next `brain-up` on this one. Mitigation: S1/S4; the CHANGELOG
  names the way back and says it is not durable.
- **A stale wiki looks like a broken wiki.** Caddy keeps serving the last build indefinitely.
  Mitigation: the banner, `brain-status` and the doctor all say "dormant" (E8, E9).
- **The watcher check cries wolf** on machines that never opted into daemons (R8-5 severity).
- **`prune-orphans` without the People Hub emitter deletes 51 vault documents** (§9.4). Phase 8
  changes neither, and E2's guard pins `prune-orphans` in `mirror`.
- **Trap 1: "wiki" is overloaded** (`[[wikilink]]` vs the Quartz site). No grep-driven edits; every
  site is read.
- **Spec anchors drift.** Every line number here is from `0965b61`; re-derive at edit time.

## 9. Environment and guardrails

- Worktree `.claude/worktrees/phase8`, branch `feat/wiki-to-ui-phase8` from `0965b61`, with its
  own Python 3.11 venv.
- Test DB is a sibling of `second_brain_t_auditmeas`, never shared with a concurrent suite.
- Prod 55432 and the live vault are off-limits; the CLI is exercised only against `brain demo`
  (55433) and a throwaway vault.
- No `DROP DATABASE`, no `compose down -v`, **no real launchctl (§3)**, no PII, no push, no
  `--no-verify`.
- Exit codes are read from files. The suite of record runs once, on the final code SHA, with
  nothing touching the worktree.
- Rule 14 applies: the review + completion-audit loop runs until both pass clean.

## 10. Closeout

*(Written at the end of the phase: rulings as decided, verification table, mutation catalog with
its validator.)*

**Recorded follow-ups (not phase 8):**

- **A9's UI status line** (spec §8.5): "watcher not running — external edits will not appear in
  search" in `brain ui`'s status line, via a `/api/status` field. It is owed by A9 (phase 4's row)
  and is not a phase-8 exit criterion (R8-5).

## 11. Plan review — where each finding landed (p7-reviewer, folded in 2026-10-14)

Every cited line was re-read in `.claude/worktrees/phase8` at `0965b61` while folding these in.
Where a reviewer anchor was off by one, the plan carries the re-derived one.

| Finding | Sev | Landed in |
|---|---|---|
| Prior (2): `DAEMON_LABELS` also read by `daemon_code_doctor_check` (`doctor_runtime.py:575`) | nit | T2: both consumers (`:400`, `:575`) switch to the installed labels |
| Prior (5): `brain-up.sh:226` belongs to T5 | nit | T5 (TDD + mutation plan) and T7's file list. **Also moved `:6`** ("still builds and serves the wiki"), for the same reason. Not in the finding |
| 1: `_make_env` drops `BRAIN_LAUNCHCTL` and the PATH shadow, and `/bin/launchctl` exists | MEDIUM | T0 "third hole": seeded non-zero stub, `_make_env` default, `shutil.which` static guard, mutations (c) and (d), and the from-scratch env audit table; T0 Files |
| 2: two `test_setup.py` tests pin the removed gate | MEDIUM | T3 "Two existing tests…": `:1281-1322` rewritten with `side_effect=[False, True]` and asserted called once, with the restore-early-return mutation; `:384-400` assertion tightened |
| 3: the no-build branch still FAILs with a bare `brain-rebuild` | LOW | R8-4, E8, T4 TDD + mutation plan: dormant OK, both remedies `brain-rebuild --only quartz` |
| 4: no `--probe` flag | LOW | R8-7 and T5: positional `probe [N]`, a `main(["probe"])` message test, and the "start with brain-up" mutation |
| 5: guard tests pollute the session `BRAIN_LAUNCHD_DIR`/`BRAIN_HOME`, and skip on Linux | LOW | T0 "Isolation for every T0 guard test": per-test `BRAIN_LAUNCHD_DIR`/`BRAIN_HOME`, `sys.platform` patched to darwin |
| 6: non-macOS process scan needs a seam | LOW | T4: `processes: Sequence[str] \| None` through `runtime_doctor_checks`; `_snapshot_processes` made public |
| 7: T0 edits listed `setup.py` first | LOW | T0 "Ceiling bookkeeping is owed by T0" and §6 (two hops, T0 then T3) |
| 8: T2 misses `test_bin_scripts.py` pins; T1/T2 not disjoint | LOW | T2 Tests (`:291-361`, `:930-1000`) and §5 ordering (T1 → T2 serial; only T6 parallel) |
| Nit: brain-down only boots out | nit | E6 |
| Nit: "auto-start Postgres" | nit | E7 |
| Nit: `notes_service.py:565` Publish-tab docstring | nit | T6 Files |
| Nit: `launchctl=` refs | nit | T0, now `129,158,183,190,216,271,424,499` |
| Re-review LOW: T3's tightened skip-wiki test demanded the `--non-interactive` line, which Linux never prints (`_dry_run_output` does not patch `sys.platform`, so the macOS gate `setup.py:863-865` returns first) | LOW | T3: platform-correct assertions (darwin → `--non-interactive` line; elsewhere → `not macOS`, as `test_setup.py:411-414` already does; both → `wiki not installed` absent), and `_dry_run_output` is NOT patched |
| Re-review nit: R8-4 cited `:677-685` while T4 cited `:676-685` | nit | R8-4, now `:676-685` (the `if newest is None:` line), matching T4 |
