# Wiki → `brain ui` consolidation — session handoff (2026-08-21)

Path convention: `<worktree>` = this checkout (`feat/wiki-to-ui-consolidation`);
`<main-checkout>` = the sibling checkout on `feat/agentic-token-reduction`, owned
by a **different session — never write to it.**

---

## 1. STATE AT HANDOFF

| | |
|---|---|
| Branch | `feat/wiki-to-ui-consolidation` |
| HEAD | `b71c923` (18 commits from base `f8c76c0`) |
| Pushed | **No.** No upstream set. Remote is **`second-brain`**, not `origin`. |
| Working tree | **NOT clean** — substantial uncommitted work from ~6 agents, interleaved |
| Prod Postgres | **DOWN** — `second-brain-postgres` exited (255) |

**Verify absence on the remote UNPIPED and against the right remote name:**
`git ls-remote --exit-code second-brain 'refs/heads/feat/wiki-to-ui-consolidation'`
→ rc=2 (or rc=0 with empty output) = absent. **Against `origin` you get rc=128,
which reads as absence but means "no such remote."**

### ⚠ Nothing mechanical prevents a push
Earlier briefs claimed two independent guards — "no pre-push hook" and "no
upstream configured". **Neither is a guard.** A real remote exists and is
authenticated. **Only instruction has kept this branch local.**

---

## 2. IMMEDIATE NEXT STEP

**The uncommitted tree needs a coordinated read, then a commit.** Several agents'
work is interleaved. Nothing is lost, but no single agent has seen the whole
diff. Do this before anything else.

Then: **iteration 5 of the rule-14 loop** (fresh reviewer + fresh auditor, run
independently, do not reuse a previous pair).

---

## 3. DECISIONS THAT ARE THE USER'S — none of these have been made

1. **Make the repo private.** The upstream repo (`<owner>/second-brain` — resolve
   with `git remote get-url second-brain`) is **PUBLIC** and its
   history carries real PII (owner name, prior employer, internal codenames)
   across ~645 of 688 commits. Going private is immediate, unilateral,
   reversible, and the only step that stops *future* exposure.
2. **Yank PyPI `secondbrain-py` 0.2.1 and 0.3.0; ship a clean 0.3.1.** All four
   published artifacts carry the real name in 4 files. **No git operation
   reaches these.**
3. **Ask the fork owner** (one public fork exists; identify it via the repo's
   forks list — deliberately not named here, per rule 15). A fork retains
   rewritten-away objects **permanently**, and GitHub Support will not purge a
   network containing a third party's fork. **Their answer decides whether a
   history rewrite buys anything.** Proven empirically: an upstream commit made
   after the fork's last push returns HTTP 200 on the fork's URL.
4. **`LICENSE:3`** — real name as MIT copyright holder. Conventional, arguably
   required. A decision, not a defect.
5. **Sync the fixed PII hook into `<main-checkout>`.** `core.hooksPath` points
   there, so **local commits are still gated by the OLD hook**. The fixed copy
   only *ships*.
6. **Confirm the confidential-document count** once Postgres is up:
   `SELECT sensitivity, count(*) FROM documents GROUP BY sensitivity;`
   Current "0" is a **vault-frontmatter proxy only — UNVERIFIED.**
7. **Push / open a PR** — permission granted verbally, deliberately not exercised
   pending (1)–(3).

**Recommended order:** (1) → (2) → (3), then decide about history rewriting.
"Upstream clean" is achievable; **"gone from GitHub" is not.**

---

## 4. THE HEADLINE SECURITY FINDING (fixed, uncommitted)

**`<vault>/static/` is published and bypasses the confidentiality filter.**

- The spec promises a `confidential` note stays *"off the published wiki"*,
  implemented as `RemoveConfidential` at **`shouldPublish`** — which gates
  **content vfiles**. **Asset files never pass through it.**
- `wiki/build_related.regenerate_related_json` writes
  `<vault>/static/related/<slug>.json` with ≤240-char **body excerpts**.
- Published via **`Plugin.Assets()`**, not `Plugin.Static()` — the docstring said
  `Static()`, **which is why the surface looked safe**.
- Caddy binds a **bare `:port`** = all interfaces → **LAN-reachable**.
- **1,154 files / 5,379 body-derived snippets** currently in the served root.
- **Nothing leaks today only because nothing is marked confidential** (unverified
  — see §3.6). The first `mark-confidential` arms it silently.

**Fixed (uncommitted):** fail-closed gates on **both** legs — the candidate query
*and* `_eligible_source_docs`. Mutations proved **half a fix is half a fix**:
closing candidates alone still publishes the confidential doc's own file; closing
the source alone still leaks its snippets into neighbours' payloads.

---

## 5. RULE-14 LOOP — four iterations, not converged

| Iter | Reviewer | Auditor |
|---|---|---|
| 1 | 16 findings | NOT PASSED — 2 ungated surfaces |
| 2 | 13 findings | NOT PASSED — **red suite**, T21 3-of-10 done |
| 3 | 6 findings | NOT PASSED — ceiling rows asserting a false state |
| 4 | 10 findings (2 HIGH) | NOT PASSED — 1 LOW |

**Every round surfaced defects the previous missed entirely.** Iteration 4's HIGH
findings were latent bugs three rounds had walked past.

Last full suite (at `4740f03`): **8,402 passed, coverage 93.28%** (gate 85).
23 errors are `test_restore_gate.py` / `test_restore_swap.py` — **pre-existing**,
proven by AST-normalised byte-identical comparison against base. **Do not run
those two**; their fixtures drop the AGE graph and poison later tests.

---

## 6. OPEN TECHNICAL ITEMS

- **CI check for the ceiling row set.** The table is now rot-proof in every digit,
  but **nothing detects a file newly crossing 800** — it can be perfect and still
  wrong by omission. Needs a build change, deliberately not implemented.
  **This already fired.** `src/brain/related.py` was described here, in CLAUDE.md,
  and in its own module docstring as sitting just *under* the ceiling while the
  uncommitted F6 gate had already taken it from 726 to 828 — over. All three
  statements were corrected at commit time and the file now has a table row.
  It is over the ceiling **knowingly**, which the rule permits only with a written
  reason; "extract rather than grow past" was not honoured and the extraction is
  outstanding. **Seam:** the lexeme/tsquery tail of `related.py`, already covered
  by `tests/test_build_related_signal.py`. Do it before the next addition there.
  Nothing detected this — a human re-derived the row set. Until the CI check
  exists, that is the only control.
- **`ui/schemas.py` and `ui/__init__.py`** cite a "9,800-line Typer CLI"; actual
  is ~9,035. Reads as inherited from an older tree.
- **`vault/graph.py:268` / `:346`** document a hole that does not exist
  (`brain_backlinks`/`brain_links` *do* have the parameter and bridge correctly).
  Repo contradicts itself — the discovery gate lists both as gated.
- **Two INERT withholding labels** rest on payload shapes measured 2026-08-21.
  One expires **loudly** (key-set pinned); the other has no pin.
- **`CLAUDE.md`** grows ~40 lines per repair round (6 rounds, now 495). Almost all
  *correction archaeology*. Load-bearing until a CI gate exists, then it belongs
  in `docs/audits/`.
- **`vault/graph.py` is 792 lines** — 8 from the ceiling. Next growth needs an
  extraction.

---

## 7. HOW TO WORK IN THIS TREE — traps that produced false readings tonight

Full detail is in project memory (`feedback_prove_the_check_can_fail`,
`feedback_parallel_agents_contention`, `feedback_db_safety`,
`feedback_local_vs_ci_divergence`, `quartz.md`). The ones that cost the most:

**Measurement**
- `rc=$?` **on the very next line**. Never piped; never after an `echo` (both
  clobber it). **rc and stdout cannot share a file** — use sibling `.rc`/`.txt`.
- **zsh:** `$pipestatus` (1-indexed), not `PIPESTATUS`; **no word-splitting of an
  unquoted `$VAR`** (pytest then exits 4 — a *usage* error); **unbraced `$c`
  before `:` is a history modifier**, so `git show $c:path` becomes
  `git show <hash>` and `wc -l` counts a whole diff — **fails quietly with
  plausible numbers.** Use `${c}:path`.
- **A task notification's "exit code 0" can sit on a real exit of 1.** Read the
  file. This fired tonight on the run that mattered most.
- **`pytest --collect-only` must exit 0** before any green subset is trusted — a
  collection error hid a correctly-failing guard for days.

**Evidence**
- **A `git archive` baseline silently imports the worktree** (`pip install -e .`
  puts the live tree first on `sys.path`). Pin `PYTHONPATH` **and** assert
  `module.__file__` starts with the export path.
- **Enumerate code structure with AST, not grep** — a triple-quote regex
  mispairs quotes and reports **gated functions as ungated**.
- **A string absence is evidence about a string.** Read the region before
  declaring a concept missing.
- **Count the claims, not the tests.** A control pairs with an *assertion*; a test
  making N negative claims needs N positive ones.
- **Distinguish unpinned from *unpinnable*.** Measure against the *permissive*
  payload first — a control added to an inert claim proves nothing.
- **No digit in any position a write can invalidate.** SHA-bound hops are safe
  (`git show <sha>:path | wc -l` is forever); live head figures are not.

**Concurrency**
- The test-DB advisory lock is **per-database, not machine-wide**. Export
  `TEST_DATABASE_URL=…/second_brain_test_wikiui` — it exists with migrations
  applied. **Create nothing** (67 stale databases accumulated once).
- `bin/brain-ci` **pins the canonical DSN and cannot be redirected** — an unset
  variable lands on top of a running gate.
- **A whole-tree check run while another agent edits measures a tree that never
  existed at rest** — in the accusing direction as well as the reassuring one.
  An asymmetric count (4 files touched → 3 hits) is the fingerprint.
- **`pgrep -f "<pattern>"` matches the watcher's own command line** — a waiter
  built that way waits on itself forever.
- **SIGTERM skips `finally`** — mutation harnesses must trap
  `EXIT TERM INT HUP`.
- **To settle "did my edit land in the wrong tree", grep the other tree for a
  token only your change introduces.** Filenames and mtimes prove nothing; both
  checkouts legitimately edit the same central modules.

---

## 8. STANDING RULES FOR AGENTS IN THIS TREE

- **Never commit, push, or stage** without explicit per-task authorisation.
- **No destructive DB or docker ops.** `DROP DATABASE` on the AGE image takes the
  whole instance into crash recovery.
- **No PII** — synthetic only. Never write the owner's real username or name into
  any tracked file, including a denylist.
- **Kill by explicit PID only** — never `pkill -f pytest`.
- `ruff check` and `mypy src/` clean before reporting.
- **Re-derive every count from disk. Inherit nothing** — including from a brief,
  a prior agent's report, or a correction note. *A number that has been fixed
  once is not thereby right.*
