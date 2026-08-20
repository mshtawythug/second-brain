# Wiki → `brain ui` consolidation — session handoff, 2026-08-13

**Written before a context compaction.** Everything a successor needs to resume without
re-deriving it. Figures here were measured at the time stated, not inherited — where a
number is stale-able, the measurement time is given.

---

## 1. Where you are

```
worktree   /Users/mshtawythug/workspace/second-brain-wiki-ui
branch     feat/wiki-to-ui-consolidation
last commit f8c76c0 (2026-08-10)
uncommitted 62 entries (measured 2026-08-13, was 37 earlier the same day)
```

**The session was moved into this worktree with `EnterWorktree(path=…)`.** Before that it
ran from `/Users/mshtawythug/workspace/second-brain` on `feat/agentic-token-reduction` —
a *different* checkout owned by a *different* live Claude session. **Do not exit the
worktree, and do not write to that other checkout.**

### Phase status

| Phase | State |
|---|---|
| 0 Rescue, measure, repair | **COMPLETE** (closed after 5 rule-14 iterations) |
| 1 Rendering parity | **COMPLETE** — but see the live defect in §4 |
| 2 Navigation + content parity | **PLANNED, not started** — 21 tasks, see §3 |
| 3 Identity + defect repair | Not started. **Ruled: token work goes FIRST, before phase 2 builds ten new surfaces** |
| 4 Write-path hardening | **COMPLETE and APPROVED** 2026-08-13 |
| 5 Graph | Not started |
| 6 Move, do not delete | **IN FLIGHT** — agent running, see §2 |
| 7–9 Demote → Dormant → Delete | Not started. Strictly ordered. Phase 9 gated on the user confirming they have stopped using the wiki |

Phase 4's closing numbers: **7,946 passed, EXIT=0 in 8/8 segments, 433 files, 93.98%,
all seven module tiers PASS**, freeze verified by hash *and* `-newermt`.

---

## 2. Agents live at handoff time

Spawned **after** the session moved worktrees? **No — all three predate the move** and were
launched while the session's cwd was the wrong checkout. They were each warned; two
confirmed nothing was written there. **Verify before trusting their paths.**

| Agent | Job | Notes |
|---|---|---|
| `ci-gate-2` | Widen the CI browser gate | **Scope was corrected mid-flight** — see §5 |
| `phase2-planner-2` | Decompose phase 2 | **DONE.** Plan delivered, provenance confirmed |
| `phase6-mover-2` | Three module extractions out of `wiki/` | Writer. Was at 16.5% CPU. Status unknown at handoff |

**How to check they are alive** — `ListAgents` does **not** show spawned teammates, and a
silent inbox proves nothing:
```
tmux list-panes -a -F "#{window_index}.#{pane_index} #{pane_id} #{pane_pid} #{pane_dead} #{pane_title}"
tmux capture-pane -p -t %ID | tail -5      # the shell prompt shows repo + branch
ps -o pid,etime,%cpu,command -p <pids>     # --agent-id in argv confirms identity
```

---

## 3. The phase 2 plan (delivered, accepted)

Full plan is in the `phase2-planner-2` transcript. **Sizing: ~31 P4U ≈ 9–10 agent-days ≈
6× phase 4.** This corrected an earlier coordinator guess of "2–3 sessions" — the planner's
figure is derived task-by-task and replaces it.

**Wave 0 (serial, nothing else starts):** T1 recon SQL → T2 fix the CI selection → T3 pull
the `build_related` scoring move forward and author `compute_related()`.

**Wave 1 (7 parallel, disjoint files):** T4 all phase-2 SQL in `ui/queries.py` · T5
`render.py` heading anchors + TOC extraction + link-kind stamping · T6 pagination · T7
unreachable sources · T8 note-payload defects · T9 explorer counts server half · T10 new
read routes.

**Wave 2 (6, client surfaces):** T11 pushState · T12 command palette · T13 TOC +
breadcrumbs · T14 backlinks + related panel · T15 lede + icons + link styling · T16
explorer client half. **T13 and T14 are serialised on `js/marginalia.js`.**

**Wave 3:** T18 email-thread mode — **not a port, blocked on a decision, see §4.**

**Wave 4:** T19 stylesheet-order guard · T20 rule-14 loop · T21 write the spec corrections.

### Two process patterns adopted verbatim

**The integrator pattern.** `ui/app.py`, `static/index.html`, `js/main.js`, `js/store.js`
are hotspots every surface wants. **One dedicated owner holds all four for the whole
phase**; implementers deliver a module plus a ≤3-line registration diff, integrator lands
them in batches. This is the fix for the one real coordination failure of the previous
phase — two writers on one tree.

**Per-implementer `TEST_DATABASE_URL`.** The test-DB lock on 5434 is **machine-wide, not
per-worktree**, so seven concurrent writers serialise on it. Additive `CREATE DATABASE` is
authorised; `DROP`/`TRUNCATE`/`compose down -v` are **not**. **Names must be ≤22
characters** — a 23-char name truncates silently and surfaces two files away as a fake
contract violation.

---

## 4. Open defects and contradictions

### LIVE DEFECT — Gmail threads render as escaped text

`ingest/gmail.py:451-456` emits **raw HTML** `<details><summary>…</summary></details>` into
document bodies. `ui/render.py:234` parses with `html=False`. **Every Gmail thread in
`brain ui` renders those blocks as literal escaped text today.**

**Phase 1 — "rendering parity" — exited past this.** It counted markdown constructs only;
its parity audit has zero hits for `details` or `raw HTML`. **This is filed independently
so it survives T18 being descoped.** Three candidate fixes, none chosen — the choice needs
T1's recon counts:

- **(a)** teach `render.py` to recognise and re-emit `<details>`/`<summary>` as
  *structurally generated* safe HTML, keeping `html=False` (never pass-through)
- **(b)** change `ingest/gmail.py` to emit markdown + a fenced marker, re-render affected docs
- **(c)** ship threads as flat headings, drop collapse

`html=True` is not an option — it is an XSS hole and breaks the spec's §2.5 + A3.

### SPEC CONTRADICTION — related-docs is in three phases at once

§11 puts it in phase 2; §11 puts the §9.2 move in phase 6; §9.2's own VERIFIED block says
`compute_related()` is **new code, not a move**, and phase 5. **Ruled: pull the scoring
move forward into wave 0; `compute_related()` is new. Correct the spec (T21).**

### Other spec findings

- **Q15 is already shipped** — `schemas.py:361 result_date()` exists. Do not budget it.
- **TOC anchors do not exist.** `render.py` emits no heading `id`s. And the TOC must walk
  the **stripped** body (`notes_service.py:189` strips a redundant title heading) or it
  lists an H1 absent from the HTML it links into.
- **Path references to `quartz_overrides/` are wrong** — it is `src/brain/quartz_overrides/`.
- **People view cut from phase 2** — the 51 `people/*.md` pages are `kind='vault'` with
  `vault_path` set, so they already render.

### Carried from phase 4, explicitly unclosed

- **EXDEV escape route** — `rename.py:338`'s fallback uses `write_text`, which **follows
  symlinks** where `replace` does not. Instrumented by a spy but **never exercised**; needs
  a genuinely cross-device destination. `plan_rename:199` is its only guard.
- **Restore-fails-while-failing** — `_restore_from_backup` re-calls `_backup_path_for`,
  whose `relative_to` is the same operation that raised. One un-relativizable path **aborts
  the restore for every file in the batch**, leaving a correctly-snapshotted file
  half-written. **Executed, but on a hand-built input** — three parties declined to promote
  it. Latent behind `:199`.
- **Test files over the ceiling** — `test_ui_routes.py` 1230, `test_ui_browser.py` 1213,
  `test_ui_static_behaviour.py` 1329, against an 800-line project limit. Worse each phase.
- **`dialog.returnValue`** — safety currently belongs to Chromium, not the code.
  Unfalsifiable by construction on the only engine tested; landed with a comment saying so.

---

## 5. Decisions ruled this session

| Item | Ruling |
|---|---|
| **CI browser gate** | **APPROVED**, but the first scope was **wrong** — a named-file selection silently excludes the three new browser test files phase 2 adds. **Widened to cover every `tests/test_ui_browser*.py`**, plus an exhaustiveness guard and a scratch-file probe proving it catches a file that did not exist when the workflow was written |
| **Phase 3 before phase 2** | **Token work first** (`tokens.css`/`components.css` only). Ten new surfaces restyled afterwards is pure rework |
| **`--ink-3`** | Decide from **measured contrast**, do not hold the phase for it |
| **Pagination** | **Over-fetch in the route layer**, not an `offset` on `hybrid_search` — `search.py` is eval-gated and `SearchResult` diverges across branches. **Perf caveat accepted and recorded**: page 4 re-pays a 5,854 ms rank leg |
| **Unreachable sources** | Additive `source_missing` kwarg with a **byte-identical-SQL pin** on the default path — that pin turns "no eval impact" from a claim into a checked property |
| **Scratch DBs** | `second_brain_test_audit` and `second_brain_audit` left in place on 5434. Additive only |

---

## 6. Environment traps — every one of these cost real time

- **A spawned agent inherits the PARENT session's cwd, not the directory its prompt names.**
  Writing the path into the brief does not set it. **Use absolute paths in every command**;
  cwd resets between Bash calls. Visible only via the tmux pane title.
- **`timeout` does not exist on macOS.**
- **zsh `${PIPESTATUS[0]}` is empty** (it is lowercase `pipestatus`). **Piped exit codes
  lie.** Redirect to a log and read `$?` on its own line.
- **zsh `nomatch` voids the whole compound command** when a glob misses — the output reads
  as a failed measurement rather than a command that never ran. Use `find -name`.
- **Several files are untracked** — everything under `static/js/`, `static/css/`,
  `tree_nav.js`, and `tests/test_ui_browser.py`. **`git checkout --` cannot restore them**
  and errors `pathspec did not match any file(s) known to git`. **Take an off-tree byte
  copy before mutating and compare against the copy afterwards.**
- **`find tests -name "test_*.py"` is 433 files; `ls tests/test_*.py` is 409.** The glob
  misses nested directories. Coverage populations built from the glob undercount.
- **A full CI gate gets SIGKILLed under swap exhaustion.** Load peaked at 283 on 2026-08-12
  from non-project apps and killed two 10-minute runs. Use file-partitioned segments.
- **Coverage data has no subtraction operator** — one contaminated segment forces a full
  re-run. **Use per-segment data files + `coverage combine`**, not a shared `--cov-append`.

---

## 7. Method rules this project earned the hard way

These are not style preferences; each was paid for.

- **Prove every check can fail.** Break the thing it protects, watch it go RED, restore,
  verify byte-identical. ~12 documented guards here did nothing.
- **Report the test COUNT, never just `EXIT=0`.** A clean exit on a silently-empty or
  narrowed selection is the most common defect in this repo's history. **The baseline must
  never inherit the mutation's selection** — two agents hit that independently in ten
  minutes.
- **A row that fails everything is close to a row that proves nothing.** A mutation
  breaking 8 of 8 tests proves the suite reacts, not that a specific test is live.
- **Never inherit a count — re-derive it, including a corrected one.** A false margin
  figure travelled through five carriers over four hours and never entered a file. *A wrong
  number in a document gets grepped; a wrong number in conversation gets repeated, and each
  repetition looks like a second source.*
- **Corroboration between two parties who are quoting rather than measuring contributes
  nothing.** Two independent readings sharing a method are one reading with two authors.
- **When two hypotheses predict identical output, no amount of rigour on the output
  separates them — change instrument.** Mutation could not distinguish "this arm never
  runs" from "it runs but is redundant"; one coverage run did.
- **A file under active write does not have a state; it has a sequence.** Three samples over
  six seconds — but note that defeats only a *fast* mutate-restore cycle. A 246-second
  mutation is indistinguishable from a settled tree by sampling alone.
- **A freeze must be declared by whoever writes, never inferred by an observer.** A writer
  who is thinking, blocked, or mid-write emits exactly the signal of one that is gone.
- **Before correcting a claim, name the artifact it is about and check that is the artifact
  in front of you.** Two near-misses in one hour were corrections about to be applied to
  things that were already right. **A correction applied to the wrong subject is
  indistinguishable from a fix and makes the record worse while looking like diligence.**
- **A negative result from a search is only as good as its scope.**

---

## 8. What needs the user

| | Status |
|---|---|
| **Commit permission** | **STILL WITHHELD.** 62 uncommitted entries, three days since the last commit. The largest single risk on the board — losing this worktree costs more than any phase. Also: committing would make `isolation: "worktree"` viable for agents, which it currently is not |
| **Phase 9 gate** | Explicit confirmation they have stopped reaching for the wiki. Not schedulable |
| **`--ink-3`** | Being decided from measurement rather than asked |
| **CI gate** | **Approved** — no longer blocking |

**Standing instruction from the user: "Fix what you have to fix and don't wait for me."**
That covers decisions and blockers. **It does not cover committing**, which was ring-fenced
separately and repeatedly.

**Also standing: "only orchestrate."** Dispatch, review, decide — do not implement or verify
personally.

**And: never screenshot the real vault.** Use the synthetic `brain demo` sandbox (port
55433) plus a throwaway `BRAIN_VAULT_PATH`. A demo database alone is not sufficient — the
UI stays read-write against `~/brain-vault` unless the vault path is overridden too.

---

## 8a. LATE DEVELOPMENTS — landed after §1–§8 were written

**The phase 6 move already landed, mid-session, while the planner was planning.**
```
src/brain/related.py             created  18:17:26   610 lines   UNTRACKED (??)
src/brain/wiki/build_related.py  rewritten 18:17:33   815 → 248 lines (thin emitter)
src/brain/connect.py             repointed 18:17:58   (:29)
```
All 15 scoring symbols + 8 constants moved. **No duplication** — `_neighbors_for_source` is
declared once. **`compute_related()` still does not exist anywhere** — the new-code half is
genuinely absent, matching §9.2 correction (d).

**Consequences:** T3 drops **3.0 → ~1.0 P4U** (author `compute_related` only, plus verify
the landed move). Phase 2 total **≈31 → ≈29 P4U**. **S1 is operationally pre-empted but
still unrecorded in the spec** — §11 and §9.2 still say phase 6, so T21 must write it in.

**`src/brain/related.py` is UNTRACKED and load-bearing for `connect.py`.** 610 lines on
disk only. This is now the single most exposed artifact in the tree.

**The PII warning in §9.2 is closed, and the spec's citation was wrong.** It cites
`build_related.py:76-83` as containing real corpus lexemes; that range is a docstring for
`refresh_related`. The tuning comments did **not** carry lexemes into `related.py` —
`:40-90` are entirely generic. **CLAUDE.md r15 satisfied.** (The planner passed that
citation through without opening it, then caught itself — worth noting because the spec has
~17 VERIFIED blocks and executors will read line numbers out of all of them.)

**Planner provenance: CONFIRMED, dispositive.** `ci.yml` is 316 lines in this tree and 198
in the other; `m browser` appears 3× here and 0× there. Three of the four files it cited
line counts for **do not exist** in the other checkout. Nothing needs re-deriving. One
precision fix: `"html": False` is at `render.py:235`, not `:234` — same construct, same
claim, **S2 stands.**

**CI gate: built and mutation-proven, but the SCOPE IS STILL WRONG.**
`ci.yml:316` reads `pytest tests/test_ui_browser.py -m browser --no-cov` — **a named file.**
Phase 2's three new browser files would be silently unrun. A widening instruction has been
re-sent; **verify it landed before trusting the gate.** What is already proven: additive
third job, no `services:`, `addopts` untouched, local run **33 passed**, mutation of the
"Saved"-while-discarding regression test → **RED**, restore byte-identical, GREEN→RED→GREEN.
Unverified by its author and correctly declared: the cache key (never exercised), anything
on ubuntu, `actions/cache@v4` inputs, no actionlint available.

**T1's recon query is written, ready to run read-only against prod (55432):**
```sql
SELECT d.content_type,
       count(*) FILTER (WHERE d.content LIKE '%<details>%') AS with_details,
       count(*)                                             AS total
FROM documents d GROUP BY d.content_type ORDER BY with_details DESC;
```
`with_details` decides T18: **triple digits → (a) structural re-emission**; low double
digits or fewer → **(c) flat headings**. **(b) re-ingest stays off the table unless forced**
— it rewrites bodies, moving `content_hash`, dedup, chunking, embeddings and the edit
path's `body_hash` at once: a corpus migration wearing a rendering fix's costume.
**Pair it with a count of documents containing ANY raw HTML block** — `<details>` may not be
the only construct `html=False` is silently escaping, and nobody has looked.

**Cross-session coordination is live and working.** The other session
(`second-brain-fd`, owns `/Users/mshtawythug/workspace/second-brain` on
`feat/agentic-token-reduction`) has been given an **all-clear to DROP and recreate
`second_brain_test` on 5434** — 121,538 orphaned relation files against 351 live relations.
Confirmed: **nothing of ours leaked into their tree.** They are changing `SearchResult`
additively (`summary` appended last, `d.summary` as index 6, `meta[5]` still `recency_ts`,
`hybrid_search` signature unchanged) — **our over-fetch pagination avoids the collision
entirely.** Coordinate before ever adding an `offset`.

**Two traps inherited from them, both paid for:**
- **`bin/brain-ci` re-execs itself** — a parent/child pair with identical argv is **one**
  run. Check `ppid` before killing anything; they lost ~30 minutes to that misread.
- **A piped background command's notification reports the PIPE's exit status.** They were
  handed "exit code 0" for a run that had failed. Write `$?` to a separate `.exit` file.

## 8b. POST-COMPACTION — resolved, and the one thing that got worse

Written after the compaction. §9 below is superseded by §8c.

**RESOLVED — the CI browser gate.** `ci.yml:328` is now `pytest tests/test_ui_browser*.py
-m browser --no-cov`, a glob. Verified directly, not on report. `tests/test_ci_workflow.py`
carries the coverage guard, and it discovers browser modules **by marker content, not by
filename** — the important property, because a filename oracle would agree with the
workflow's own glob *by construction* and the two would go blind together on naming drift.
Proven RED by a probe file that did not exist when the workflow was written.
`ci-gate-2` also self-reported a regression it had missed: its first report called the
change "purely additive" without having run the guard that rejects any `--no-cov` in the
workflow. It found that unprompted and repaired it job-aware instead of deleting the guard
that caught it.

**STILL OPEN from that work:** the anti-vacuity clause at `test_ci_workflow.py:565` is
verified to EXIST (read directly) but NOT verified to FIRE. And `tests/test_ui_browser.py:58`
still says "WHY IT IS NOT IN THE GATE" about a file that is now in the gate.

**RESOLVED — attribution, by a different instrument than the one I was waiting on.**
`phase2-planner-2` produced an mtime table: only seven files modified today (18:17–18:32),
everything else Aug 10–12 i.e. phases 0/1/4. That attributes the tree by timestamp without
needing the writer's testimony. **I blocked the phase for ~13 minutes on a question another
instrument had already answered.**

**WORSE — `phase6-mover-2` never reported and went unresponsive.** Three contacts, CPU
decaying 7.9% → 5.8% → 3.0%, no reply. Its work appears to have landed cleanly
(`related.py` 613 lines, `build_related.py` reduced to a 248-line emitter, `connect.py`
repointed, no duplication, no stranded importers — all confirmed independently by the
planner). But it never declared a freeze.

**A correction I owe the record.** I told the user a `shutdown_request` "converts an
inferred freeze into a guaranteed one, by construction." **That is only true if the agent
processes the request.** A wedged agent processes neither my questions nor my shutdown —
the mover was still alive minutes after the request. The guarantee arrives when the process
*exits*, not when the request is *sent*. Do not treat a sent shutdown as a freeze.
`SIGKILL` is NOT the fix: killing mid-write can truncate a file, converting an unresponsive
agent into a corrupted tree.

**THE TOP RISK, and it is sharper than §8 states: the migration is HALF-STAGED.**
`people.py` and `person_name.py` are staged renames (`AM`); `related.py` is **untracked**
(`??`); `build_related.py` and `connect.py` are **unstaged** (` M`). A commit of that
subset produces a tree where `build_related.py` imports from a `brain.related` that is not
in the commit — so **`import brain.cli` passes on this machine and fails for everyone
else.** Self-concealing: local verification comes back green *precisely because* the
untracked file is still sitting on disk. All four must move together or not at all.

**Plans were never on disk.** `phase2-planner-2` was dispatched read-only with no Write
tool, so the entire phase 2 plan (30.0 P4U, T1–T21, wave order, spec defects) existed
**only inside its own message history** — one session exit from gone. It caught this itself
and refused to route around the constraint. A scribe is transcribing it to
`docs/plans/2026-08-13-wiki-to-ui-phase2.md`. **Never dispatch a planner without checking
whether it can persist its own output.**

**Spec defects now: S1 (open, unrecorded in the spec), S8 (§9.2c's importer list names
modules that no longer exist), S9 (§11's phase 6 row describes work already done).
S10 CLOSED** — the PII citation range was wrong, the substance was fine.

## 8d. THE INFRASTRUCTURE FINDINGS — read these before trusting any coordination

**`SendMessage` returns `{"success": true}` with a routing confirmation and delivers
NOTHING.** One teammate had **five** consecutive sends to another silently dropped, all
acknowledged. Its sends to me landed throughout, so the failure was one-directional and
recipient-specific. **The receipt is an artefact of the send, not evidence of delivery.**

Two consequences, both of which bit this session:

1. **I judged `phase6-mover-2` "unresponsive" after three messages and a
   `shutdown_request` went unanswered, and froze the phase on it.** That judgement was
   probably wrong — the process was in state `R` burning CPU the whole time and may never
   have received a word. **An agent that receives nothing is indistinguishable from an
   agent ignoring everything.** Never conclude from silence; confirm liveness with `ps
   -o pid,stat,etime,%cpu` and confirm work by its effects on disk.
2. **The durable transcript is the recovery route.** The scribe rebuilt a 57 KB plan
   verbatim from `~/.claude/projects/<slug>/<session>.jsonl` (records 13937/14212/14563/
   14669) after every send failed — and disclosed that route in the document rather than
   letting it read as a normal transcription. Prefer this to a re-send you cannot trust.

**Channels fail independently — check all of them before concluding absence.** A teammate
recorded a complete proof via **TaskUpdate** while I watched SendMessage and the tree, and
I sent it a "something is wrong with your loop" prod on the strength of watching the wrong
channel. TaskGet/TaskList, the inbox, and the transcript are three separate channels.

**RETRACTED: "`find -newermt` silently matches nothing on macOS."** I wrote that here,
told the user, and told a scribe to propagate it. **It is false.** The scribe refused,
re-derived it, and found `-newermt` and `-mmin` agree at every window on real
`/usr/bin/find`. Re-derived here too, and the cause is worse than the claim:

- **`find` on this box is `bfs`**, a drop-in replacement earlier in PATH — not BSD `find`.
- **`bfs` did not fail silently.** It printed `bfs: error: Invalid timestamp` to stderr.
- **The pipe hid both.** `| wc -l` turned the failure into a plausible `0`, and the
  `echo $?` after it captured **`wc`'s** exit status, not `find`'s — reporting 0 for a
  command that errored.

So the anti-guard was **my measurement**, not the tool — the repo's own piped-exit-code
rule breaking in the hands of someone who had restated it to four agents that same session.
**Run diagnostics bare, read stderr, `$?` on its own line, and `type -a` the binary before
attributing behaviour to it.** `-newermt` in the freeze-verification rule stands as
written.

**Load average on this box runs 151–204 with many agents up.** A 50-second pytest run took
**923s (15:23)**. Budget for it; do not read slowness as a hang.

## 8e. `docs/plans/` IS GITIGNORED — the plan on disk is not a plan that is safe

`git check-ignore -v docs/plans/2026-08-13-wiki-to-ui-phase2.md` → `.gitignore:57`.
The 57,857-byte plan **cannot be committed**, will not survive a clean clone, and per
auto-memory **migrating trees silently drop gitignored files**. Exposure reduced, not
closed.

**A recurrence, not a discovery** — phase-0 handoff §7 item 1 already recorded it. It has
now cost a second agent the time to re-find it, which is stronger evidence for acting than
a fresh finding would be.

**The argument is already written in `.gitignore` itself, lines 53–56**, justifying why
`docs/specs/` was un-ignored: rule 11b mandates specs live there, so leaving it ignored put
every design doc one clean checkout from loss. **Rule 11b mandates plans in `docs/plans/`
in the same sentence.** The reasoning transfers unchanged; the PII precondition is already
met for this file. `docs/audits/` carries the identical exposure. **User decision — do not
edit `.gitignore` unilaterally.**

## 8f. CI browser gate — CLOSED, with four honestly unverifiable items

`ci.yml:328` runs `pytest tests/test_ui_browser*.py -m browser --no-cov` in a dedicated job
with no `services:` and no `env:`. **Five guards in `tests/test_ci_workflow.py`, each
proven to fire:** exhaustiveness-by-marker (not by filename — a filename oracle would agree
with the workflow's own glob *by construction* and both would go blind together),
anti-vacuity, path-selection-required, no-DB, and a job-aware `--no-cov` carve-out proven
un-abusable from two directions. `tests/test_ui_browser.py` is at `59153bed…eb76` with the
false gate claims gone; probe files removed.

**These four are unverifiable from this machine, not merely unverified** — all need a real
CI run. **If the first run comes back red, look here in this order:**
1. `playwright install --with-deps chromium` — system deps are the one thing the cache
   cannot restore.
2. `actions/cache@v4` input names — unvalidated, no actionlint available here.
3. Bash's expansion of `tests/test_ui_browser*.py` — on a no-match it passes the literal
   through and pytest errors loudly (designed behaviour, untested on a runner).
4. The cache key — pinned to the resolved playwright version, so a stale-browser mismatch
   should be impossible *by construction*, but that construction has never been exercised.

## 8g. PII — FOUR CONFIRMED LEAKS IN `HEAD`, TWO OF THEM SHIPPED

**None of this is remediated.** Every fix is working-tree only; `HEAD` still carries every
original value, and will until the user authorises a commit.

| # | Location | What | Fixed? |
|---|---|---|---|
| 1 | `src/brain/templates/env.example:46` | real full name | fixed in tree |
| 2 | `src/brain/quartz_overrides/quartz.layout.ts:160` | real name inside a real email subject — leaks a name **and** a correspondence fact | fixed in tree |
| 3 | `tests/test_build_related_signal.py:245` | real surname-derived lexeme + real corpus doc ID | fixed in tree |
| 4 | `src/brain/wiki/fastpath_manifest.py:79-84` + `tests/wiki/test_fastpath_fingerprint_parity.py:422` | internal project codenames + vendor reference | **NOT fixed** |

**#1 and #4 ship inside the installed package** (`src/brain/templates/`, `src/brain/wiki/`),
and this project publishes to PyPI. **Whether they reached a released artefact is unverified
and is a user decision** — deliberately not investigated by any agent here.

**#4 is not a free fix.** The terms sit in `_IGNORED_FIELDS` (declared line 64), which
carries its own *"Changes to this list must bump FINGERPRINT_VERSION"* comment at line 61.
Editing it invalidates every cached fingerprint and forces a full rebuild. **A reported
"latent TS/Python parity bug" that would have made this cheaper was WITHDRAWN** — it compared
`_STRUCTURAL_FIELD_ORDER` (20 entries on both sides) when the finding is in `_IGNORED_FIELDS`,
which has no TS counterpart at all. Treat the rebuild cost as real.

**DO NOT "fix" these** — named so a future sweep does not break things:
- `LICENSE:3` — real name as MIT copyright holder. **Required.** Scrubbing voids the grant.
- `tests/test_search_canary_queries.py` — real corpus doc IDs **are the assertions**; strip
  them and the test asserts nothing. Proven deliberate: line 63's `COMPANY_REDACTED` shows a
  human scrubbed that file and consciously kept the IDs. A design decision to revisit, not a
  scrub target.
- `person-x`, `person-b`, `topic-b`, `pat`, `Pat Morgan`, `COMPANY_REDACTED`, and every
  `@example.*` address — established synthetic fixtures, working as intended.

**Why they survived — proven, not guessed.** The pre-commit PII gate DOES scan `src/` and
DOES fire (mutation-proved: denylisted token planted → exit 1 with the gate's own message;
removed → exit 0). It is **not vacuous**. But it inspects only `git diff --cached` — a
**new-lines gate** — and it was added 2026-06-03 while the leaks entered 2026-05-11. It never
saw them and never can. **There is no full-tree PII scan anywhere**; the only referencers of
the lists are the hook plus two unit tests of the hook's own patterns. So **everything
committed before 2026-06-03 has never been machine-scanned.**

**Bound any CI step built on this.** A full-tree denylist scan covers **seven curated terms**.
It would NOT have caught the corpus doc ID in leak #3, and does not cover the canary IDs. A
green check here cannot mean "no PII", and must not be labelled as if it does.

**Fragility:** `core.hooksPath` for this worktree points at
`/Users/mshtawythug/workspace/second-brain/scripts/hooks` — the **sibling checkout**.
Byte-identical today (verified), but this repo's gate lives in another repo and would change
or stop silently if that one is edited, moved, or deleted.

## 8c. Immediate next actions (supersedes §9)

1. **Do not unfreeze `src/` on an mtime.** `related.py` has held at 613 lines / `18:30:23`
   for a long while. That is a sampled quiet interval, not a property of the process —
   the same error as calling a database idle because a test suite was between cases.
   Unfreeze when the mover process is **gone from `ps`**, or when `tree-forensics` reports.
2. **Get the plan onto disk and confirm it** — check the file exists and is non-trivial;
   do not trust the scribe's completion report alone.
3. **`ci-gate-2` owes two things:** the anti-vacuity mutation (and proof the failure comes
   from line 565's assertion, not line 577's and not an incidental `IndexError` from a
   helper), and the `test_ui_browser.py:58` docstring correction.
4. **Then phase 2 wave 0:** T1 recon (read-only `SELECT`, gates T18) → T3 `compute_related()`.
   T2 is DELETED — the CI work absorbed it. T4–T21 are cleared to dispatch.
5. **Commit permission is the top risk** — and it is now a *shaped* question, not a plain
   one: the four migration files must be committed together. Raise it with that constraint
   attached, never as a bare "may I commit".
