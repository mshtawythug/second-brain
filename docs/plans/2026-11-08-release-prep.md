# Release prep for `secondbrain-py` 0.4.0

> **For agentic workers:** REQUIRED SUB-SKILL: Use team-driven-development to implement this plan task-by-task.

**Goal:** one PR (`chore/release-prep`, base `82e6af05`) that makes master ready for the owner to
tag `v0.4.0`: the version bump, the CHANGELOG cut, release notes with an upgrade note, and the
queued docs follow-ups. Code follow-ups become backlog rows. This PR creates and pushes no tag.

## 1. Version: 0.4.0

- **Current:** `v0.3.0` is the newest tag (2026-08-07). `pyproject.toml:3` is the only place the
  version is written. `brain --version` (`cli._installed_version`) and `mcp_http.py` read it
  through `importlib.metadata`. There is no `__version__` and no lock file.
- **Why a minor bump:** `[Unreleased]` holds about 1,800 lines since 0.3.0:
  - four additive migrations (028–031);
  - dozens of new commands and MCP tools (RM-12 to RM-55);
  - a **Removed** section: the Quartz wiki, with `brain vault render`, `brain wiki install` and
    the `com.brain.build` daemon.

  Under SemVer 0.y.z a breaking change goes in the minor, so a patch is wrong, and 1.0.0 is a
  product decision nobody has made.

## 2. The CHANGELOG cut, and the tag that does not exist yet

**The constraint.** `tests/test_repo_hygiene_files.py` pins these:

- (a) every `## [X]` heading has a link definition;
- (b) every `/releases/tag/vX` definition names an existing git tag;
- (c) `[Unreleased]` compares against `v<newest bracketed version>`;
- (d) no orphaned definitions.

**Why the 0.3.0 approach doesn't work here.** If this PR adds `## [0.4.0]` plus
`[0.4.0]: …/releases/tag/v0.4.0`, check (b) fails on the PR, and on master after the merge, until
someone pushes the tag. 0.3.0 avoided that with direct commits to master, pushed with
`git push --atomic` together with the tag. A reviewed PR can't do that.

**Design (no guard weakened, every SHA green):**

1. **This PR** retitles `## [Unreleased]` as `## 0.4.0 - 2026-11-08`. The heading is
   **unbracketed**, the convention the file already uses for `0.1.0` ("no tag, nothing to link
   to"), and an HTML comment under it says the brackets and link come once `v0.4.0` exists.
   - A new empty `## [Unreleased]` goes on top.
   - The link block is unchanged: `[Unreleased]` still compares `v0.3.0...HEAD`, which is true
     until the tag exists.
   - Checks (a)–(d) all pass.
2. **The owner** tags the merge commit by SHA, then pushes it. This follows the memory's
   `traffic-stats` cron warning: never tag `second-brain/master` by name. `release.yml` publishes
   once `ci-gate` sees the green `ci` run for that SHA.
3. **A follow-up PR after the tag** brackets the heading and adds
   `[0.4.0]: …/releases/tag/v0.4.0`. It retargets `[Unreleased]` to `compare/v0.4.0...HEAD` and
   removes the comment. With the tag present its CI is green.
   - The date changes there too if the tag is cut on a later day.
   - I draft that diff in the PR body so it is mechanical.

**Rejected:**

- Weakening (b) to allow a missing tag for the pyproject version. That reopens the 0.3.0
  dangling-link class for as long as a tag is forgotten.
- A `compare/` link for 0.4.0. Check (b)'s regex only sees `/releases/tag/`, so that games the
  test, and the link still 404s.

**Test change this forces.** `tests/test_reading_surface_pointers.py:393-409` reads "the text
after `## [Unreleased]` up to the next `\n## [`" and expects the R3 upgrade sentences there.
- Today the text sits in the unbracketed `0.4.0` section and the split runs on to `## [0.3.0]`,
  so the tests pass, but only by accident.
- They would go red at step 3.

**Fix:** a small helper that returns the body of the `0.4.0` section, bracketed or not:
`^## \[?0\.4\.0\]?`. Both tests read that. I'll prove it can fail: drop one sentence from a
git-archive copy and confirm the test goes red.

**RM-14c / RM-22b wording.** A `### Not in this release` list in the 0.4.0 section has one line
per ticket: "deferred to a later release; the reranker and document leg ship off". In Appendix B
each row's status cell becomes `TODO (deferred past 0.4.0; owner decision)`. If the owner rules
otherwise, either change is one line in each file.

## 3. Release notes (top of the 0.4.0 section)

`### Upgrading from 0.3.0`, five bullets:

1. Take a backup (`brain backup`), then stop the daemons (`bin/brain-down`).
2. Run `brain init` once.
   - It applies migrations **028–031** in order. All four are additive.
   - 028 sets `lock_timeout = '3s'`. **031 has no `lock_timeout`**: it is one
     `CREATE TABLE IF NOT EXISTS`, so stop every writer first (that's why `brain-down` comes
     first).
3. `bin/brain-up`. This also removes `com.brain.build`, per the existing R3 note, which stays
   where it is.
4. **The document leg is off** (`BRAIN_DOC_LEG=false`), and the reranker is off. Turn the leg on
   only after `brain init` has applied 031, then run `brain docembed backfill`.
5. A pointer to the existing per-ticket **Upgrade** lines below (030, RM-46, RM-47 and others).

Plus a one-line known limitation under the RM-20 entry: under `brain ui`'s wrapped embedders the
leg reports `no_document_vectors` (sec-127 I6). It fails safe.

## 4. Follow-up triage

Key: (i) = fixed in this PR, docs only. (ii) = new backlog row in
`docs/plans/2026-10-23-backlog-reconciliation.md` §2.3, numbered from BL-45 (BL-42 is
used/skipped). (iii) = already done.

| # | Item | Verdict | Where / what |
|---|---|---|---|
| 1 | Spec ~3388: "the query goes only to the local chat model" | (i) | `roadmap-completion-design.md:3388`. Reword to what #123 shipped, matching CHANGELOG RM-54: the chat model at `OLLAMA_HOST`; with `BRAIN_EMBEDDER=voyage`, Voyage embeds the paraphrases; with `BRAIN_RERANKER=voyage`, found passages can go to Voyage (confidential rows excepted). Marked "amended 2026-11-08". |
| 2 | `brain show` prints raw escapes | (ii) BL-45 | `cli.py` `show` echoes `doc.title`/`summary`/`content` with `typer.echo` (~5423–5433). The fix reuses `brain.preview_filter`. Merged with #3: one "untrusted text to the terminal" row. |
| 3 | `brain ask` Rich output | (ii) BL-45 | `cli.py:5028` `console.print(result.answer)` interprets Rich markup, and escapes pass through. Fix: `markup=False` / `Text(...)` plus the preview filter. |
| 4 | INFO-1: process-wide cap on concurrent HTTP chat calls | (ii) BL-46 | `brain_tags_summarize` and `brain_ask` over `brain mcp serve`. Merged with #6 into one chat-transport row. |
| 5 | nan timeout | (ii) BL-47, **or** ruling (§5) | `config.py:1644`: `nan <= 0` is False, so `BRAIN_ENRICH_TIMEOUT_SECONDS=nan` loads. The same hole is at `BRAIN_ASK_TIMEOUT_SECONDS` (:2360), and `inf` passes both. The `<= 0` float knobs need one shared `isfinite` helper. `config.py` is over the ceiling, so it needs a growth record. |
| 6 | L4: the chat transport has no cap on reply size or total time | (ii) BL-46 | sec-123 L4, which the reviewer OK'd to defer. |
| 7 | BL-33 | (iii) | Already a backlog row (BL-33, §2.3). The queue item was "record it", and that's done. No change. |
| 8 | Drop-folders wording | (i) | `skills/ingest-brain/sources/drop-folders.md:46-47` still says "a folder was skipped". The code (`cli_ingest_folder.py:241-247`) only warns about a missing configured folder, with exit 0. New text per aud-105. **Post-merge op:** `bin/brain-skills-sync`. |
| 9 | sec-126 I2: non-JSON list answer → traceback | (ii) BL-48 | `ingest/gmail.py:130,150`. Catch `ValueError` beside `GmailError`. |
| 10 | sec-126 I4: DB error text in the per-thread failure line | (ii), added to **BL-25** | `cli_ingest.py:712`. BL-25 is the "CLI echoes print raw exception text" sweep, so this is one more site, not a new row. |
| 11 | sec-126b I-a: `from exc` keeps argv in the chain | (ii) BL-48 | `gws.py:93-97`, the uncapped callers only. Use `from None`, which also covers the `CalledProcessError` cause at :87-92. |
| 12 | sec-126b I-b: `call_label` stops only at `--` | (ii) BL-48 | `gws.py:49`. Stop at any `-` word. |
| 13 | sec-126b I-c: double-signal race | (ii), added to **BL-43** | `capped_process.py:129-133`. It's the same shared runner as BL-43. |
| 14 | sec-127 I6: wrapped UI embedders make the leg inert | (iii) recorded, plus (ii) BL-49 | Already in the RM-20 plan's D15 follow-ups. Gets a backlog row, BL-49 "doc-leg adoption prerequisites", so the adopting PR finds it. CHANGELOG known-limitation line (§3). |
| 15 | doc_leg savepoint (UndefinedTable aborts `brain explain`'s transaction) | (ii) BL-49, **or** ruling (§5) | `search_doc_leg._cosines` runs a bare `conn.execute`. `_apply_one` catches the error, but the non-autocommit transaction is dead after it. RM-54's pattern (`search_pipeline.py:406`) is `with conn.transaction():`. |
| 16 | BL-35 has no Source column | (i) | The row has 2 cells, and the table has 3. Add `RM-42 review (PR #109, F1).` |
| 17 | pypdf>=6.14 `apply_configuration` note | (i) | `pyproject.toml` pypdf evidence block. Measured 2026-11-08 in throwaway uv venvs: absent in 6.14.0-6.17.0, present from 6.18.0 with 6 of the 8 fields `pdf_limits` sets, all 8 in 6.21.4. Written as measured; the floor is unchanged (raising it would be a dependency decision, flagged in the PR body). |
| 18 | rev-107b guide notes | (i) | `docs/guides/drop-folders.md` voice section. (a) Homebrew's prefix and `/Applications` are group-writable, so a model there is refused: keep it in `~/models` and don't chmod. (b) Limits: only mode bits are checked (ACLs aren't), and an "ignore ownership" volume (uid 99) is refused. (c) Rewrap line 162 (121 chars). |
| 19 | Appendix B / BL-8 "user confirmed 2026-10-30" | (iii) | Already present: Appendix B RM-24, 48, 49 and 51, and backlog BL-8 (line 210). No change. |
| 20 | 11-05: restore preflight prints manifest strings unfiltered; container-restore cleanup keeps the 7200 s timeout | (ii) BL-50 | RM-47 plan §10.2 / sec-119 I6; sec-119c N5. |
| 21 | 11-05: `brain ui` 500 / bad-Host 400 carry no CSP or nosniff | (ii) BL-51 | sec-120 S4. The middleware docstring over-claims. |
| 22 | 11-05: `brain demo --database-url` on an empty DB → raw `UndefinedTable` | (ii) BL-52 | |
| 23 | 11-05: automatic graph rebuild after restore | (ii) BL-53 | A feature idea. Today it's a manual `graphrag build --force`. |
| 24 | 11-05: `list_review_queue` docstring (sec-118h INFO-C); `preview_filter.py:24` comment (sec-121c N3) | dropped | Ruled out of this PR (zero `src/` diff). Left for the next PR that opens either file. |
| 25 | 11-05: the RM-52 plan says `preview_filter.py` is 65 lines | no change | Plans are point-in-time records. The count drifted after the plan. |
| 26 | 11-05: "~/.brain/state by default" | (i) guide, (ii) doctor string → BL-52 | `docs/guides/second-machine.md:86`: a source checkout's `$BRAIN_HOME` is the checkout (`config._brain_home_root`). The doctor string is pinned by `test_doctor_replica.py`, so it goes with the backlog row. |
| 27 | 11-05: INFO-A/INFO-B (review gate id shape, an index) | no row | They're optional hardening with no defect. I'll add one sentence under §2.3 "Closed, so not listed" saying why they aren't rows. |
| 28 | 11-05: global CLAUDE.md `--since 30d` for `ingest-gmail` | owner | It's in the user's global file, outside the repo. I'll flag it in the PR body and won't change it. |
| 29 | #71 (Dependabot coverage) | no change | Stays open until a measured bump after the release (11-01 handoff). |
| 30 | New: `release.yml` doesn't check that tag == pyproject version | (ii) BL-54, optional | Tagging a SHA whose pyproject still says 0.3.0 cuts a GitHub Release, then PyPI rejects the existing version: it fails late but safe. Report only. |

The backlog's §3 rank-12 row (3.6, document-level embeddings) gets a pointer to BL-49.

## 5. Code fixes, ruled

**Ruling (coordinator, 2026-11-08):** A and B are backlog rows BL-49 and BL-47, not in this PR; the
optional docstrings (#24) are dropped; BL-54 is recorded. The pypdf floor stays at `>=6.14`; raising it
is backlog BL-55 (measured: no `apply_configuration` in 6.14.0–6.17.0, 6 of 8 fields at 6.18.0, all 8 at
6.21.4).

### As proposed

I recommend **none** in this PR. It stays docs, version and one test helper, so the release
commit carries no new behaviour.

- **A. Savepoint (#15).** Two lines in `search_doc_leg.py` (149 lines, not in the table) plus a DB
  regression test on 5434:
  1. rename `document_embeddings` inside the test's transaction;
  2. run `brain explain`'s path with the leg on;
  3. assert that a later statement succeeds;
  4. roll back.

  It's only reachable if the owner turns the leg on before `brain init`, which the upgrade note
  says not to do.
  - **Recommendation:** BL-49 now, with the fix in the leg's adopting PR.
  - **Alternative:** a separate tiny PR before the tag, if you want 0.4.0 to carry it.
- **B. isfinite (#5).** The honest fix is every `<= 0` float knob through one helper, in an
  over-ceiling `config.py`. That's not tiny. **Recommendation:** BL-47.

## 6. Memory fixes (after approval, outside the repo)

- **`cli.md`**
  - RM-55 heading "NOT merged" → merged `cfcf327d` (#124).
  - RM-20 `docembed` heading "open" → merged `82e6af05` (#127); 031 not applied to prod.
  - RM-45 "#117, open" → merged `f00eab0f`; RM-43/RM-41 "verify merged" → #113 `92f0a06a`,
    #114 `2a4ebba9`.
  - **Add** an RM-54 section, which is missing everywhere: `brain search|explain --rewrite`,
    ≤2 paraphrases, weights 1.0/0.5, chat failure → original plus a notice, MCP/UI can't ask for it.
  - The MCP count after #124 (52 tools, 29 read-only), re-derived from source.
- **`schema.md`**: migration 031 "OPEN — not applied" → merged in #127 (`82e6af05`); **NOT applied
  to prod**; owner step backup → brain-down → brain init → brain-up.
- **`types.md`**
  - RM-55 tag summary → merged; RM-20 → merged; RM-45 → merged.
  - **Add** RM-54 `brain.query_rewrite`: `QueryRanking`, `QueryHit`, `FusedDoc`, `fuse_rankings`,
    `clean_paraphrases`, `make_rewriter`, `rewrite_notice`, constants. Also `brain.citations`
    (#124) and `brain.confidential_lookup` (#127). Read from source.
- **`search.md`**: RM-20 → merged. **Add** an RM-54 rewrite-leg section: paraphrase searches under
  a savepoint, RRF fusion, `explain` attribution, `search_queries` keeps the original only.
- **`MEMORY.md`**: refresh the index hooks for cli, schema, types and search (they still say #117
  open, RM-55 on branch, #127 open). `extractors.md`'s RM-45 line will be checked too.

## 7. Files touched

- `pyproject.toml`: version, plus the pypdf note.
- `CHANGELOG.md`
- `tests/test_reading_surface_pointers.py`: the section helper.
- `docs/specs/2026-10-23-roadmap-completion-design.md`: line 3388, plus the RM-14c/RM-22b cells.
- `docs/plans/2026-10-23-backlog-reconciliation.md`: BL-35 cell, BL-45 to BL-54, BL-25/BL-43
  addenda, the rank-12 pointer.
- `skills/ingest-brain/sources/drop-folders.md`
- `docs/guides/drop-folders.md`
- `docs/guides/second-machine.md`
- This plan.

## 8. Test plan

Every pytest run holds `.claude/full-suite.lock` and checks `df -g /` ≥ 6 GiB. It uses my own uv
venv, which is deleted at the end. No local full suite, no Ollama, no prod.

1. **Targeted nodb tests:**
   - `tests/test_repo_hygiene_files.py`
   - `tests/test_reading_surface_pointers.py`
   - `tests/test_screenshot_source.py`
   - `tests/test_voice_source.py`
   - `tests/test_doctor_replica.py` (guide)
   - `tests/test_ci_workflow.py`
   - `tests/test_release_workflow_contract.py`
   - `tests/test_dev_dependency_pins.py` (pyproject)
   - any test that greps the files in §7, re-derived with `grep -rln` before running.
2. **Mutation proof** for the new section helper: in a git-archive copy, delete one pinned
   sentence and expect red; restore and expect green. Delete the copy.
3. **Hypothetical step-3 check**, also in a git-archive copy: apply the post-tag diff plus a
   throwaway local `v0.4.0` tag. The hygiene and pointer tests stay green. The tag is
   **local to the copy only, never pushed**, and is deleted with the copy.
4. **Build:** `uv build` in a scratch dir, then wheel metadata `Version: 0.4.0`. `brain --version`
   from a fresh venv install prints `brain 0.4.0`.
5. `ruff check`, `mypy src/`, `python scripts/check_ceiling_table.py`.
6. **Scans:** a raw C0/C1/bidi scan of every changed file, plus a PII review of the diff and PR
   body.
7. CI (all five jobs) is the full-suite evidence. Then the review + audit loop (rule 14) on the PR.
