# Wiki → `brain ui` consolidation — session handoff (2026-08-21)

Path convention: `<worktree>` = this checkout (`feat/wiki-to-ui-consolidation`);
`<main-checkout>` = the sibling checkout on `feat/agentic-token-reduction`, owned
by a **different session — never write to it.**

---

## 1. STATE AT `833a395`

**This section describes one commit, named, and does not claim to describe the
branch tip.** The first draft asserted `HEAD = b71c923` and "working tree NOT
clean"; both were false the instant the file was committed, because `833a395`
already sat two commits above `b71c923`. §7 says it: *no digit in any position a
write can invalidate.* "HEAD" and "currently" are such positions — every commit
after this one invalidates them, silently, and a reader has no way to notice.
So the row below names a SHA and the reader derives the rest:

    git log --oneline f8c76c0..        # everything on the branch, tip included

| | |
|---|---|
| Branch | `feat/wiki-to-ui-consolidation` |
| Base | `f8c76c0` |
| Described commit | `833a395` — 21 commits from base (`git rev-list --count f8c76c0..833a395`). `b71c923` is 3 below it and was 18. |
| Pushed | **No** as of `833a395`. No upstream set. Remote is **`second-brain`**, not `origin`. Re-check before assuming; nothing mechanical enforces it (below). |
| Working tree at `833a395` | **Clean.** The interleaved ~6-agent work that earlier drafts described as uncommitted landed in `b7fd0e8` (egress fix) and `d99ca34` (ceiling rework). |
| Prod Postgres | **DOWN** — `second-brain-postgres` exited (255). Left down deliberately. |

**Verify absence on the remote UNPIPED and against the right remote name:**
`git ls-remote --exit-code second-brain 'refs/heads/feat/wiki-to-ui-consolidation'`
→ rc=2 (or rc=0 with empty output) = absent. **Against `origin` you get rc=128,
which reads as absence but means "no such remote."**

### ⚠ Nothing mechanical prevents a push
Earlier briefs claimed two independent guards — "no pre-push hook" and "no
upstream configured". **Neither is a guard.** A real remote exists and is
authenticated. **Only instruction has kept this branch local.**

---

## 2. NEXT STEP

The coordinated read this section used to demand **has happened.** The
interleaved tree was read and committed: `b7fd0e8` (the §4 egress fix) and
`d99ca34` (the ceiling rework). There is no uncommitted backlog to reconcile at
`833a395`.

What remains is the **rule-14 loop**: a fresh reviewer + fresh auditor each
round, run independently, never reusing a previous pair. §5 records iterations
1–4. Iteration 5 ran against `833a395` and produced two HIGH findings, both
pre-existing egress holes of the same shape as §4 (a published surface Quartz's
`RemoveConfidential` cannot reach, because it is not a page):
`wiki/build_homepage._fetch_recent_docs` (the home-page "Recently captured"
rail, rewritten inline inside `<vault>/index.md`) and `people.aggregate_people`
(the People Hub roster pages).

**Derive the state of that work rather than reading it here** — this file cannot
describe commits made after it:

    git log --oneline 833a395..        # empty = nothing landed since

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

## 4. THE HEADLINE SECURITY FINDING (fixed in `b7fd0e8`)

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

**Fixed in `b7fd0e8`:** fail-closed gates on **both** legs — the candidate query
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
  F6 gate had already taken it from **726 (`7b5579e`) to 851 (`b7fd0e8`)** —
  over. (`828` is what this line said until 2026-08-21. It was bound to nothing
  and matched no commit: `git show "${sha}:src/brain/related.py" | wc -l` gives
  726 at `7b5579e` and 851 at `b7fd0e8`, with no boundary between. An unbound
  digit inside the bullet about unbound digits.) All three
  statements were corrected at commit time and the file now has a table row.
  It is over the ceiling **knowingly**, which the rule permits only with a written
  reason; "extract rather than grow past" was not honoured and the extraction is
  outstanding. **Seam:** the lexeme/tsquery tail of `related.py`, already covered
  by `tests/test_build_related_signal.py`. Do it before the next addition there.
  Nothing detected this — a human re-derived the row set. Until the CI check
  exists, that is the only control.
- **`ui/schemas.py` and `ui/__init__.py`** cite a "9,800-line Typer CLI." No
  commit on this branch has held that figure:
  `git show "833a395:src/brain/cli.py" | wc -l` gives **9,038** at the commit
  that wrote this bullet. Reads as inherited from an older tree. Re-derive the
  live figure with `wc -l src/brain/cli.py` — and do not copy one back into the
  citation; a line count in prose is the thing that rots. *(This bullet said
  "actual is ~9,035" until 2026-08-21 — unbound to any commit, and already three
  short at `833a395`.)*
- **Two INERT withholding labels** rest on payload shapes measured 2026-08-21.
  One expires **loudly** (key-set pinned); the other has no pin.
- **`CLAUDE.md`** grows ~40 lines per repair round (six rounds through
  `833a395`). Almost all *correction archaeology*. Load-bearing until a CI gate
  exists, then it belongs in `docs/audits/`. Count it with `wc -l CLAUDE.md`; no
  figure is carried here. *(This bullet said "now 495" until 2026-08-21.
  `git show "833a395:CLAUDE.md" | wc -l` gives **505** — so 495 was not stale, it
  was **false on arrival**: it never matched the commit that shipped it, nor
  `b7fd0e8` below it. Substituting today's count for yesterday's would only
  restart the rot, which is why there is a command here instead.)*
- **`vault/graph.py` sits just under the ceiling.**
  `git show "aa39159:src/brain/vault/graph.py" | wc -l` gives **792** — eight
  lines of headroom at that commit, and the docstring repair that followed spent
  most of them. This digit is SHA-bound rather than replaced by a command because
  the *number itself* carries the argument: headroom is the claim. Re-derive the
  live figure with `wc -l src/brain/vault/graph.py` before adding anything. The
  next addition needs an extraction, not another line.

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
- **A commit message can describe work that is not in that commit.** `d99ca34`
  ("docs(ceiling): give six ceiling records a terminating form") states
  *"mcp_server.py additionally regains a missing hop."* `d99ca34` touches six
  files and `src/brain/mcp_server.py` is not one of them; the hop — `4,356
  (ec6afb6)` in that file's module docstring — is absent at `d99ca34` and
  present at `b7fd0e8`, its direct child. Verify with `git show --stat d99ca34`
  and
  `for s in d99ca34 b7fd0e8; do git show "${s}:src/brain/mcp_server.py" | grep -c '4,356'; done`.
  History is **not** being rewritten to fix this; the correction lives here, and
  a message is evidence about intent, never about content. *(Recorded
  2026-08-21.)*

  **Second instance, same trap, one commit later.** `2b2b321`
  ("fix(security): stop `review weekly` / `brief --wiki` publishing confidential
  docs") asserts *"The MCP twins of both functions gate correctly; the CLI was
  the outlier."* That is true of what those tools RETURN and false of what one of
  them WRITES: `brain_review_weekly` passed its permissively built report
  straight to `emit_weekly_page`, so a single MCP call with
  `include_confidential=true` published the page the CLI had just been stopped
  from publishing. Note the shape — the claim was not careless, it was *scoped to
  the surface the author was looking at* and stated as if scoped to the tool.
  Verify with
  `git show "2b2b321:src/brain/mcp_server.py" | grep -n 'emit_weekly_page(state.cfg.vault_path, report)'`.
  The correction lives here, in that tool's docstring, and in `CLAUDE.md`'s
  `mcp_server.py` ceiling row. *(Recorded 2026-08-24.)*
- **A comment can be false in the very commit that writes it** — staleness is not
  the only failure mode, and a fresh commit date is not a warrant. `2ed2d83`
  ("fix(security): exclude confidential documents from ungated surfaces") added
  the `exclude_confidential=not include_confidential` bridges to
  `brain_backlinks` / `brain_links` **and**, in the same commit, added a note to
  `vault/graph.py` at two sites saying those tools "do not pass this and have no
  `include_confidential` parameter." It closed the gap and documented it as open
  in one write. The repo then disagreed with itself in three places for a day,
  because CLAUDE.md's `mcp_server.py` row and the discovery gate both said
  *gated*. Both sites corrected 2026-08-21 to describe the bridge and its
  inversion instead. *(Recorded 2026-08-21.)*

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
