# Wiki → `brain ui` consolidation — orchestrator handoff

**Rewritten:** 2026-08-11 17:50 (the previous version described a state ~6 hours stale)
**Contract:** `docs/specs/2026-08-10-wiki-to-ui-consolidation-design.md` — now **1,921 lines** (was 1,333; sixteen corrections written in)
**Branch:** `feat/wiki-to-ui-consolidation` — worktree at `/Users/mshtawythug/workspace/second-brain-wiki-ui`
**HEAD:** `f8c76c0`. **NOTHING COMMITTED.** **32 files uncommitted.**

> ## THE HEADLINE: phase 0 is CLOSED. Phase 1 has STARTED. **Phases 2–9 have not.**
> Phase 0 — rescue, measurement and repair of what already existed — consumed almost the entire session and **closed on its fifth attempt**. Phase 1 has begun (tables, strikethrough, `.note-body` CSS). Everything from phase 2 onward is untouched. Do not let phase 0's detail disguise how little consolidation has happened.

## Phase 0 — CLOSED 2026-08-11 18:57

Both halves of the rule-14 loop exited clean: **reviewer APPROVED + auditor AUDIT PASSED**. Full record: `docs/audits/2026-08-11-phase0-final-audit.md`.

Final gate: **TOTAL 90%**, `--fail-under=85` **EXIT=0**, **409 test files, 7,320 passed**, EXIT=0 across all 8 segments. Exhaustiveness verified three ways (A=B=C=409; `--collect-only` reconciling 7,324 = 7,320 + 4 skipped) and the tree verified frozen for the duration of the run.

All six changed modules pass their tier targets: `security.py` 100% (+4), `telemetry.py` 100% (+5), `search.py` 99.18% (+10), `schemas.py` 95.92% (**+1**), `server.py` 89.89% (+4), `notes_service.py` 94.97% (+9).

### It passed on the FIFTH iteration, and that is the useful part

Anyone reading "phase 0 passed" should know how it got there:

- **Iteration 2** found the HIGH — D3's transaction wrapper created a **disk-vs-DB split-brain**, a rejected edit left on disk while the database rolled back. Proven by a teardown plugin that found the file *after the atomicity test passed green*.
- **Iteration 4** found that fix's **successor**: the guard written to make the class inexpressible policed `update_document` only, while the contract is about **mirror writes** — so `write_vault_mirror` inside a transaction was the same defect, unpoliced.
- **The successor's fix then contained the same defect again**, one level down: `_violates` hardcoded both writer names while only the vacuity counter read the shared `_MIRROR_WRITERS` tuple, so deleting a writer left all twelve tests green — *inside the structure added to stop exactly that*.

Three levels of the same shape, each found only by execution, none by review. **A correct-looking fix to a real defect is where the next defect lives.**

---

## 0. Read this first — unchanged and still binding

- **Never commit or push without the user's explicit permission.** Asked repeatedly, never granted. Work accumulates in the worktree.
- **Never touch `/Users/mshtawythug/workspace/second-brain`.** That is the shared main checkout; another agent (`feat/agentic-token-reduction`) works in it. It was clobbered once by a mid-task clean.
- **Every teammate dispatch uses `isolation: "worktree"`.** Not doing this created the hazard above.
- **The coordinator delegates.** Route verification to teammates rather than doing it inline.
- **No destructive DB or Docker ops. Ever.** No `DROP`, `TRUNCATE`, unbounded `DELETE`, `compose down -v`, `rm -rf data/postgres/`.

---

## 1. Evidence classes — do not flatten these

Every claim below carries one. They are not interchangeable, and collapsing them is the single failure this session produced most often.

| class | means |
|---|---|
| **MEASURED** | executed and a number/behaviour observed (browser, coverage, traceback line) |
| **MUTATION-PROVEN** | the defect was reintroduced and the check went RED, then restored |
| **READ** | confirmed by reading source. Weakest. A green suite is where blind checks hide. |

---

## 2. DONE and verified

| item | evidence |
|---|---|
| Logo rescue — 5 assets + `package-data` globs | **MUTATION-PROVEN**: dropping `static/js/*.js` names all 8 modules missing from the wheel |
| PII scrub — 25 spec files, `docs/specs/` un-ignored | **MEASURED**: 0 unallowlisted hits under the repo's own 12 secret patterns. *Not* run: the LLM semantic pass |
| 9 UI defects (duplicate `<h1>`, 68ch→48ch, contrast repoints, `aria-selected`, UUID→date, `color-scheme`, edit-mode early return, roving tabindex + APG arrows, focus preservation) | **MEASURED** in a real browser |
| CSS split → `css/{tokens,base,layout,components}.css` | **MEASURED**: 26 browser assertions |
| JS split → 8 modules under `js/` + `theme.js`/`tree_nav.js` at root (**10 shipped JS files**) | **MEASURED** |
| Browser harness — hermetic, API stubbed at the network layer, no Postgres/Ollama/uvicorn | **26 passed** |
| Telemetry `autocommit` fix + embedder warm-up | **MUTATION-PROVEN** |
| `SearchResult.recency_ts` | **MEASURED** — line 672 (naive-tz branch) now covered |
| `strip_redundant_title_heading` — render-time only | **MUTATION-PROVEN**: `body_hash(strip(...))` now fails; it passed the whole suite before |
| **Five-clause mutation harness**, 38 guards | **ALL FIVE MUTATION-PROVEN by independent injection** — see §3 |
| Chain-premise guard | **MUTATION-PROVEN RED two ways**; message verified proxy-honest |
| D1 `telemetry.py` → **100%**, D2 `server.py` → **90%** | **MEASURED** |
| Coverage union | **MEASURED**: TOTAL **90%**, `--fail-under=85` EXIT=0, 7,290 passed / 407 files |

### The suite, as of 17:48
**7,290 passed across 407 test files**, EXIT=0 in all 8 segments. Guard set 130, browser 26. *(A "1,794 passed" figure appears in an earlier report — that was a **subset**, roughly a quarter of the suite. Do not read it as the whole.)*

---

## 3. The five-clause harness — the session's main artifact

Each entry pairs a guard with a defect. Each clause kills a specific way the **harness itself** certifies nothing. **Order matters: (a) must precede (c).**

- **(a)** guard passes on UNMUTATED source — kills the guard that raises on everything.
- **(b)** mutation lands the declared count, replacing **ALL** occurrences — kills both anchor bugs.
- **(c)** guard then raises.
- **(d)** the mutation reaches **the assertion it targets**, and the raise does not come from a helper.
- **(e)** every independent claim has its own entry — a loop over N members is N claims.

Two escape hatches, `UNFIREABLE` (currently empty) and `LOOP_CHAIN_EXEMPTIONS`, both **self-guarding in three directions**: no unfired-and-unexempted assertion; every exemption names an assertion that still exists; **every exempted assertion is still unfireable** (without which the list is a one-way amnesty).

**Why (d) exists, measured not argued:** duplicating `_block`'s anchor makes the raise originate *inside the helper* at line 196. Clause (c) run alone against that same mutation **PASSES** — `pytest.raises(AssertionError)` is satisfied by a raise that never reached the guard's own assertion. That is the empirical proof (a)–(c) were insufficient.

**Non-assertion failures are loud**, not counted as proven: an injected `ValueError` propagates as an ERROR. A `SyntaxError` or collection error cannot be mistaken for a passing mutation.

---

## 4. IN FLIGHT — phase 1

**Started.** Tables and strikethrough enabled via two native `.enable()` calls, clearing **506 documents**; plus the `.note-body` CSS those elements lack. Recon and seven spec corrections are done; the §11 exit criterion was fixed (vault → corpus).

## 4a. OPEN AND UNOWNED — nobody is carrying these

Not blockers for phase 1, but they will rot if left unnamed:

1. **C2 — the empty-vault keyboard entry point.** No owner.
2. **Unreferenced branding assets.** Shipped and packaged, referenced by nothing.
3. **Four files over the 800-line ceiling** (CLAUDE.md limit), with **B3 deferred by agreement** — `test_ui_static_behaviour.py` and `test_ui_browser.py` grew rather than split, and `test_ui_routes.py` is now ~900.
4. **Task #14 — import-time DB connections** in `test_restore_gate.py` / `test_restore_swap.py`. Until this lands, the browser harness is hermetic **only for the named-file selection** (see §7).

---

## 5. CLOSED — the rule-14 loop, all five iterations

Every finding below was **found by execution, none by review**, and each was closed and verified before the loop exited.

- **HIGH (iter 2) — disk-vs-DB split-brain.** CLOSED and verified against pre-fix baselines.
- **MEDIUM (iter 3) ×3** — the AST guard missing `ast.AsyncWith`; a rename test whose docstring claimed a contract it never exercised (`apply_rename` instrumented: **entered 0 times**); and `pytest -m browser` still needing a database. All CLOSED.
- **MEDIUM (iter 4) — the successor gap**, `write_vault_mirror` unpoliced. CLOSED, and the repair mutation-proven in both directions.
- **Carried to phase 4 / A7:** the blanket `except` at `notes_service.py:569-572` and a possibly-dead raise at `:570`.

---

## 6. FOUR DECISIONS ONLY THE USER CAN MAKE

1. **Commit permission.** 32 files uncommitted, asked repeatedly, never granted. **This is the top risk** — see §7.
2. **CI gate for the browser harness** (task #3) — **blocks phase 2**. Outward-facing: edits `.github/workflows/ci.yml`. **Cost measured:** free locally (4.3 GB of browsers already cached at `~/Library/Caches/ms-playwright`); ~150 MB once in CI, then cached. **The cost is a cached chromium and nothing else — for the NAMED-FILE selection.** That qualifier is load-bearing and was measured: against an unreachable database, `tests/test_ui_browser.py -m browser` passes 26, while `tests -m browser` **fails EXIT=2**, and `--collect-only` reproduces the failure — so the reach is at *import* time in `test_restore_gate.py` / `test_restore_swap.py`, before marker deselection. **The CI job must name the file** until task #14 lands, or it will need a database after all.
3. **`--ink-3`** — **blocks phase 3 implementation.** The token **does not exist**; the audit table it came from did not reproduce. Needs a fresh decision, not a reused number.
4. **The PII comment at `wiki/build_related.py:76-83`.**

---

## 7. STANDING RISKS — these outlive every agent here

1. **`docs/specs/` is untracked.** 1,921 lines of corrected spec exist only as untracked files plus temp copies under `/private/tmp`, **which do not survive a reboot**. `git status` shows one `?? docs/specs/` line that itemises nothing. The directory is no longer gitignored, so `git add` would work — **it needs commit permission.** **`docs/plans/` (this file) and `docs/audits/` are STILL gitignored and carry the same exposure.**

   **Do not answer this with more snapshots.** Five were taken this session and each went stale the moment anyone edited. **A backup that is silently stale is the same shape as a guard that silently passes — reassuring, and wrong in the direction that stops you looking.** And the count makes it worse, not better: five copies make *"this reflects the file"* feel more warranted, exactly as a long list of green checks feels more trustworthy regardless of whether any of them can fail. **Quantity of reassurance is not evidence, and it is the one thing that reliably stops people looking.** An accepted risk is a fact someone owns; a stale backup is a risk nobody knows they hold. Escalate the commit as a decision with its cost, and if it is declined, **record the exposure as accepted** rather than copying again.
2. **`SearchResult` merge hazard is field ORDER, not membership.** Both new fields are defaulted, so a naive merge yields a *valid* dataclass and nothing fails loudly — but anything constructing it **positionally past `tags`** silently binds to the wrong field. The `meta[N]` renumber and a positional-construction sweep must land in **one pass**, with the eval gate re-run once afterwards rather than trusting either branch's pre-merge "ranking unchanged" claim.
3. **`notes_service.py` sits at +0 margin** — 180 of 180 statements required for its 90% tier. **One additional uncovered statement drops it below.** It is also the module the D3 repair is editing.
4. **The coverage union has an expiry date.** 90% describes the tree at 17:48. The D3 fix changes it. **Re-run after iteration 3.**

---

## 8. Environment — this will cost you time

- **Test-DB lock is MACHINE-WIDE**, not per-worktree. Every non-browser pytest selection takes it, and runs serialize across all agents. **Escape hatch:** point a run at its own DB with `TEST_DATABASE_URL`. **Browser-only runs (`-m browser`) no longer take the lock** — `_session_touches_the_database` opts out when *every* collected item carries the `browser` marker (conservative both ways: empty list or mixed selection → still takes it).
- **Full `bin/brain-ci` gates die for unknown reasons.** Four attempted, three killed mid-run without writing an exit code. Several causes proposed and each falsified. **Use ~5-minute segments.**
- **`${PIPESTATUS[0]}` is EMPTY in zsh** — it is lowercase `pipestatus` and 1-indexed. Piped exit codes lie: `cmd | tail; echo $?` reports *tail's* status. This produced multiple false readings.
- **zsh `nomatch` voids the WHOLE compound command.** `rm -f .coverage.probe*` with no match aborts the entire line — the `rm` never runs *and neither does anything after it* — while the output reads as a failed measurement rather than a command that never executed. **Use `find -name`, which has no glob-failure semantics.**
- **zsh does not word-split unquoted parameters.** `$VAR` holding a file list passes as ONE argument. Use an array or `${=VAR}`.
- **`timeout(1)` does not exist on macOS.** Do not copy hook examples that use it.
- **`pgrep` cannot be trusted here.** `pgrep -fl pytest` self-matches. Authoritative: `ps -Ao pid,command | grep -E "venv/bin/python.*pytest" | grep -v "zsh -c" | grep -v grep`
- **Never share a venv across worktrees.**
- **A diagnostic that shares the machine with its subject is not independent.** Running diagnostics *while* the job under diagnosis was still executing produced a false root cause about the tooling; pytest-cov erases its data file at start, so the racing runs looked like broken measurement.
- **Read-only work leaves no disk trace.** An agent doing verification writes nothing for long stretches — **silence is not death.** Check for a live pytest process before assuming an agent has hung.
- **DECLARE INTENT BEFORE TAKING THE LOCK — team convention.** Anyone starting a run that takes the machine-wide test-DB lock announces it first, with its expected duration, and says when it releases; anyone who needs the lock sooner says so *before* the long run starts. No tooling, no scheduler — the point is that **this information exists only before the fact.** Once a long run is underway the blocked party has two bad options: wait out a duration they cannot see, or interrupt someone else's work. From inside a blocked session a 20-second guard check and a 15-minute union are indistinguishable.

  This shares a root cause with the line above: **every observable signal here is an artifact, and an agent that is waiting produces none.** Announcing intent is the only signal that survives that, and the only one available *before* the cost is paid rather than after.

  It was adopted after a blocked agent was mistaken for a dead one and a replacement was nearly dispatched into its worktree — where **the cost would not have been the waiting, but two writers colliding on 30+ uncommitted files.**

  **It is a reduction, not a guarantee.** The lock is machine-wide, so agents in the *other* checkout share it and cannot be reached by any convention of ours — one eight-minute block this session came from there. Treat a declared-intent protocol as lowering the collision rate among the agents who follow it, never as a lock you can rely on being free.

- **THE LOCK AND THE TREE ARE DIFFERENT RESOURCES.** Every convention above governs the *lock* — who holds it, for how long, when it releases. **None of it governs writes.** A file edit takes no lock, emits no announcement, and is invisible to every signal we have. So *"the lock is free"* is **not** a freeze and must never be relayed as one. This voided a completed 20-minute union: `render.py` was edited 11 minutes into it, four segments measured one version and four another.

  **No amount of inspection substitutes, and here is why:** a lock is a thing you hold and release, so its status is **queryable**. A write is instantaneous and leaves no state behind. You cannot ask *"is anyone editing?"* the way you can ask *"is the lock held?"* — so a freeze has to be **declared**, not detected, and it is a **window**, not a status. In the implementer's words: *"there is nothing you could inspect that would tell you I have stopped. **The confirmation is the mechanism.**"* Get it from whoever writes, in their words — *"no writes to `src/` or `tests/` until you report"* — never from whoever holds the lock.

  **Confirm a freeze at file level, never with `pytest`.** The implementer verified seven mutation markers absent across `src/` and `tests/` and parsed **666 Python files with `ast`**, catching a half-written file — all file-level, taking **no lock**. That is the only shape of confirmation that does not undermine itself: **a confirmation that needs the lock becomes another writer competing for the resource whose freedom it is trying to certify.**

  **Then verify afterwards:** `find src tests -name "*.py" -newermt <start> ! -newermt <end>` must return empty. **That check cannot prevent an edit; it establishes whether the result can be trusted, which is the achievable thing.**

- **BUILD THE TEST PARTITION WITH `find tests -name "test_*.py"` — 433 files, NOT `ls tests/test_*.py`.** The top-level glob sees **409** and silently misses `tests/wiki/` (18 files) and `tests/derived_links/` (6), i.e. **658 tests**. Every union run before 21:15 today used the wrong glob. **Take the third source from bare `pytest --collect-only`**, which honours `testpaths = ["tests"]` and finds **8,071** — *not* `--collect-only` over your own partition, which is independent of the partition's contents but not of its derivation, and reconciles perfectly while measuring the same wrong set.

- **Read coverage percentages at two decimal places before comparing them to a target.** `coverage` displays `render.py` as **95%**; the true figure is **94.85%**, and its tier is 95% — it is one statement *below*, not at, its threshold. **An integer percentage does not tell you which side of a threshold you are on.** Report **margin in statements** alongside every module.

---

## 9. The lesson this session produced

Nearly every defect found — in the product, the tests, and the tooling — had one shape: **something that looked like evidence and was not.**

**Coverage cannot verify, but it can locate.** A real `body_hash` bug was closed with line coverage **identical before and after** (196/32/84%) — the buggy line was reachable, executed and covered the whole time. Meanwhile the *miss map* pointed at one contiguous block, `_update_ingested_note`, which proved to be both untested **and** carrying a live atomicity bug, found there before a reviewer independently reported it. **Keep "which contiguous blocks does nothing execute"; retire "does this module clear its target".**

**Ask what observably DIFFERS before choosing what to assert on.** A golden *score* test was commissioned for the `meta[5]` hoist and would have stayed green through the exact defect it was written for — the boost computes identically either way; what breaks is a field going `None`. The same question retroactively catches the `@import` guard, the `inspector-head-is-first` misfire and the `body_hash` gap: **each was an assertion aimed at something that does not change when the defect is present.**

**Vigilance is not a fix; an invariant is.** The anchor-uniqueness trap recurred five times against rising attention, twice in code written *while discussing it*. It stopped only when `_block` was made to assert its anchor matches exactly once — which then exposed a sixth instance passing **by luck of file order** through four review rounds by three agents.

**Where the regress stops.** Guarding the guard invites guarding that. It terminates on a property, not fatigue: **the regress ends when the remaining uncertainty is written down rather than hidden. A documented proxy is stable; an undocumented one rots.** Every defect here was *believed-fine*, never known-and-accepted.

**Counting is not locating.** A presence-count cannot distinguish a defect from its own documentation — which is why `check_single_innerhtml` counts `.innerHTML` *with the dot*, why the `@import` guard strips comments first, and why grepping `68ch` "found" a stale value that was the recorded evidence for the current one.

**None of these were found by being more careful. All were found by running things.** Care produced the code; execution produced the corrections.

Durable lessons: `feedback_prove_the_check_can_fail.md`, `feedback_parallel_agents_contention.md`, `feedback_local_vs_ci_divergence.md`.
