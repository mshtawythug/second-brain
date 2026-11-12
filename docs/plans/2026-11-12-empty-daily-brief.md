# Empty daily brief: write no page (0.4.0)

> **For agentic workers:** REQUIRED SUB-SKILL: Use team-driven-development to implement this plan task-by-task.

**Owner request (0.4.0):** "if a daily brief is emplty don't create it".

**Branch:** `fix/empty-daily-brief` off `second-brain/master` (`a8b4a1d1`).

## 1. What happens today (read from source)

- `brain brief` (`cli.brief`, `src/brain/cli.py`) calls `brief_publish.build_brief`. That
  function holds the whole sequence: watch checks, then two payloads. The terminal payload is
  permissive. The published payload is built with `exclude_confidential=True`, and only under
  `--wiki`. Then come the LLM suggestions, the swap of the unnotified watch view into the
  published payload, `brief.write_brief_to_vault`, delivery (RM-16) and `mark_notified`.
- `write_brief_to_vault` always writes `<vault>/daily/<YYYY>/<date>-brief.md`. Nothing in
  `build_brief` inserts a `documents` row. The row the owner sees in `brain ui` comes from the
  vault watcher mirroring that page into Postgres. **Writing no page therefore means no row.**
- The page always renders three sections: recent captures, open action items and pinned docs.
  Each shows an italic placeholder line when it is empty. Watch hits, new entities, possible
  duplicates and suggestions are left out when they are empty. An empty brief is therefore a
  heading followed by three placeholder lines. The owner's example could not be fetched (the
  local UI refused the connection), so this structure comes from `_render_brief_body`.
- The model is called on an empty brief anyway. With `enrich`, `suggest_next_steps` sends a
  prompt whose two lists are both `- (none)`. It can return invented "next steps", and those
  would turn an empty page into a non-empty one.
- The 07:00 job (`templates/bin/_brain-brief-fg.sh`) runs `brain dedup scan || true`, then
  `exec brain brief --wiki`. The plist (`com.brain.brief.plist.j2`) is a one-shot
  `StartCalendarInterval` job with no `KeepAlive`, so launchd never restarts it whatever the exit
  code. It still has to exit 0.
- `--json` emits `BriefData.to_dict()`. MCP `brain_brief` uses the same `to_dict` and only reads.
  It writes no page.

## 2. Definition of "empty" (decided on the data, not on the rendered text)

New pure function in `src/brain/brief.py`:

```python
def is_empty(brief: BriefData) -> bool:
    """True when ``brief``'s assembled (pre-model) data is empty; ``suggestions`` is never read."""
```

A brief is **empty** when all of the following hold (assembled data only):

| Field | Empty when |
|---|---|
| `captures` | `[]` |
| `open_todos` | `[]` |
| `pinned` | `[]` |
| `watch_hits` | `[]` |
| `watch_notes` | `[]` (see D2) |
| `new_entities` | `[]` |
| `possible_duplicates` | `0` |

`date` is never content, and neither is `suggestions` (D3).

- **D1. The fixed-section placeholders are not content.** They are rendering, not data, and the
  predicate never reads rendered text. "Boilerplate-only" means exactly "every field above is
  empty".
- **D2. A watch note counts as content.** Recommendation: count it. A note is either "and N
  more", which comes with hits anyway, or "watch X was skipped: <reason>". The skip note is real
  news for the owner ("your semantic watch could not run"), and the existing test
  `test_a_skipped_semantic_watch_is_named_in_both_payloads` pins that it reaches the page. A
  `limit` note never reaches the published payload (`_check_notes(published=True)`).
- **D3. `suggestions` is NOT in the predicate (team-lead ruling).** The skip decision is made
  BEFORE, and independently of, any model call. Model output can never rescue an otherwise-empty
  brief. `is_empty` reads only the assembled fields above, so a payload carrying suggestions and
  nothing else is still empty. The model runs only when `is_empty` is False.

## 3. Where the decision lives

**One predicate, `brief.is_empty`.** `build_brief` consults it at the two points where it makes a
decision, and nowhere else. The CLI never re-derives emptiness.

The changes to `build_brief` (`src/brain/brief_publish.py`, 258 lines):

1. **Move the unnotified-view swap before the suggestions block.** Today the published payload
   gets `watch_hits=view.hits` *after* enrichment. The emptiness check has to see the final
   published watch fields. Moving the swap is safe for the suggestion logic, because
   `_suggestion_inputs` already strips every watch field, so `withheld` is unchanged.
2. **The page decision.** `publish = published is not None and not is_empty(published)`. It is
   keyed on the **published** (gated) payload, never on the terminal one, for two reasons:
   - The page shows the published payload, so that payload is the one that is or is not empty.
   - It is an F6 property. The page's presence becomes a function of non-confidential data
     alone, so its absence reveals nothing about confidential rows. Keying the decision on the
     permissive payload would let one confidential capture create an empty-looking page, which
     is the exact case the owner complained about.
3. **Gate the suggestion calls before generation.**
   - Terminal: call `suggest_next_steps(data)` only if `not is_empty(data)`.
   - Published: call nothing unless `publish`. If `publish` holds, the existing rule stands: a
     second call when `withheld`, otherwise reuse the terminal suggestions.
   - Result: **an empty brief makes no Ollama call** on either leg.
4. **When the page is skipped:**
   - `write_brief_to_vault` is not called, and the function returns `(data, None)`.
   - No delivery happens. `_deliver` is not called, so no empty mail or Slack post is sent and no
     warning is logged.
   - **`mark_notified(view.pending)` still runs**, exactly as it would after a written page with
     delivery off. When the published payload is empty, every pending hit is one the page could
     never list: it is confidential or draft-excluded. An over-cap hit implies that the watch's
     capped hits are on the page, so the page is not empty. Leaving those hits pending would let
     them pile up until the next non-empty page. Marking them keeps the RM-15.D3 rule ("every
     unnotified hit the build read is marked") intact.
   - One `INFO` log: `brief page skipped for <date>: nothing to publish`. It carries the date
     only, never titles.
   - **An existing page for the same date is left untouched.** It is never deleted or
     overwritten. The owner's standing rule protects vault content. A same-day re-run that finds
     nothing also stops overwriting a good 07:00 page with an emptier one.
5. **The return type is unchanged:** `tuple[BriefData, Path | None]`. Under `wiki`, `None` now
   means "skipped". The caller passed `wiki`, so it can tell the two cases apart. About fifteen
   test call sites unpack this tuple. Changing the return type would churn all of them and buy
   nothing.

## 4. CLI, `--json`, MCP and launchd

- **Terminal output (`cli.brief`).** A non-empty brief prints exactly as today. With `--wiki` and
  a skipped page, the existing `Wrote … (confidential documents excluded)` line is replaced by one
  line:
  `No page written for <date>: nothing to publish (confidential documents excluded).`
  Exit code is 0. That is `elif wiki:` plus one echo, about **+2 to 3 lines in `cli.py`** (see §7).
  The terminal digest itself is unchanged, even when it is empty (Q1).
- **`--json`.** The shape is unchanged: the same keys, with empty lists and a `0` count. Stdout
  stays pure JSON, and the skip line is not printed in JSON mode, matching today, where
  `--json --wiki` does not print the `Wrote` line either. No `"empty"` key is added (Q2).
- **MCP `brain_brief`.** It creates nothing, so the page rule does not apply. I recommend reusing
  the predicate to skip the model call on an empty payload:
  `if not no_enrich and not is_empty(data):`. That is a net-zero-line edit, because the import is
  function-local and the `if` line is rewritten in place (Q3).
- **07:00 job.** No script or plist logic changes. `brain brief --wiki` exits 0 on an empty brief
  and writes its one line to `com.brain.brief.out.log`. A one-shot `StartCalendarInterval` job
  cannot crash-loop. I would only correct the plist comment ("The brief writes the daily digest
  page") to add "unless the brief is empty". It is a comment-only edit, and it takes effect at the
  next `brain-up` reinstall. **Never kickstart `com.brain.brief`.**
- **`--force` / `--allow-empty`:** recommend **not adding** one. Nothing useful is on an empty
  page, and `brain brief` at the terminal still shows the empty digest. YAGNI. It can be added
  later without breaking anything (Q4).

## 5. TDD: the tests come first, each with a RED proof

New file `tests/test_brief_empty.py`, well under 800 lines. It follows the existing patterns in
`tests/test_brief_publish.py` and `tests/test_brief_publish_delivery.py`: the `test_db` fixture,
`ingest`/`WordEmbedder`, a counting double on `brain.brief.suggest_next_steps` (pytest
`monkeypatch`, which is allowed), `chat.chat_json` set to raise, and `delivery_channel=` and
`delivery_env=` as seams. It uses no real Ollama, and all data is synthetic.

"RED" below means the test fails on unchanged `a8b4a1d1`. "Characterization" means it must pass
both before and after the change, which is how "created exactly as before" is proved.

**Unit tests (pure, no database):**

| # | Test | Before → after |
|---|---|---|
| U1 | `is_empty` of an all-empty `BriefData` is `True` | RED (no function) → green |
| U2 | Parametrized over the 7 content fields: one item in that field alone makes it `False` (`watch_notes` uses a skipped note; `possible_duplicates=1`) | RED → green |
| U3 | A `BriefData` whose only content is `suggestions=["…"]` is still empty (model output cannot rescue it) | RED → green |

**Integration tests (`build_brief` against the test DB):**

| # | Scenario | Asserts | Before → after |
|---|---|---|---|
| I1 | Empty DB, `wiki`, `enrich`, delivery configured with a recording channel | `written is None`; no file under the tmp vault (`rglob("*")` empty); 0 calls to suggest; channel never sent; no WARNING | RED (page written, model called, mail sent) |
| I2 | One capture | Page written, and its text equals what master produces (golden assembled in the test from `write_brief_to_vault` semantics); 1 suggest call; one send | Characterization |
| I3 | Only a confidential capture | Terminal shows it; **no page**; exactly 1 suggest call (terminal only) | RED |
| I4 | Terminal empty, but the published payload holds an unnotified watch hit older than `since_hours` (seeded by updating `first_seen_at`) | Page **written** with that hit; 0 suggest calls, because the prompt inputs are empty | RED with M6 (§6); characterization today |
| I5 | Only a confidential watch hit | No page; that hit **is** marked notified | RED |
| I6 | Only a skipped semantic watch | Page written (D2). The existing test already covers this, and I would keep it unchanged | Characterization |
| I7 | A page for `ON_DATE` already exists, then an empty run | File bytes unchanged | RED (overwritten today) |
| I8 | Empty DB, `wiki`, `enrich`; the fake model WOULD return suggestions | No page; the fake is called 0 times | RED (called, page written with suggestions) |

**CLI tests (`CliRunner`):**

| # | Scenario | Asserts | Before → after |
|---|---|---|---|
| C1 | `brief --no-enrich --wiki --date D` on an empty DB | Exit 0; the skip line; no page | RED |
| C2 | Same, with `--json` | Exit 0; stdout parses; the key set equals the current `to_dict` keys; no page | RED (page) |
| C3 | One capture | The `Wrote …` line is unchanged | Characterization |

**MCP test, if Q3 is accepted:**

| # | Scenario | Asserts | Before → after |
|---|---|---|---|
| M | `brain_brief` on an empty DB | 0 suggest calls | RED |

**Existing tests that assumed an empty page.** One is known:
`test_no_watches_means_no_section` (empty DB, `wiki=True`, asserts a page). It gets one seeded
capture, so it keeps testing "no watch section" on a page that exists. Every other `wiki=True` or
`--wiki` test in `test_brief*.py`, `test_cli_vault_emit_confidential.py`, `test_brief_near_dup.py`
and `test_brief_new_entities.py` gets read during implementation. Any test that relied on an empty
page is changed the same way, and each change is listed with its reason in the PR body. A test
is never loosened to pass.

## 6. Mutation proofs (the guard must be able to fail)

Each mutant is applied in a **`git archive` copy, never the worktree**. The diff is applied and
verified with `git diff`/`grep`, then the named tests are run. Every mutant must turn at least one
named test red.

| Mutant | Must fail |
|---|---|
| M1: `is_empty` returns `False` | U1, U3, I1, I8, C1, C2, I5, I7 |
| M2: `is_empty` returns `True` | U2, I2, C3, I4 |
| M3a–g: drop each field from `is_empty` (7 mutants) | The matching U2 case |
| M3s: `is_empty` also requires `suggestions == []` | U3 |
| M4: run the suggestions before the guard | I1, I8 (call count) |
| M5: key the page on `data` instead of `published` | I3 (and I4) |
| M6: leave the view swap after the emptiness check | I4 |
| M7: skip `mark_notified` on a skipped page | I5 |
| M8: deliver even when skipped | I1 (channel) |
| M9: the MCP leg ignores the predicate | M |

The results (the mutant, the failing tests, and the run date) are recorded in this plan's
"Proof record" section when they run.

## 7. Docs, CHANGELOG and the ceiling table

- **CHANGELOG.** A new `### Changed — an empty daily brief writes no page` entry under
  `## 0.4.0 - 2026-11-08`. The heading stays **unbracketed**, as its HTML comment requires. The
  entry covers:
  - the rule;
  - that confidential-only content counts as empty for the page;
  - that there is no delivery and no model call on an empty brief;
  - that existing pages are left alone, so older empty pages stay until the owner removes them;
    brain never deletes vault files.
- **`docs/cli-reference.md` (`### brain brief`) and `skills/brain-proactivity/SKILL.md`.** One
  sentence in each, on the `--wiki` row: "An empty brief writes no page." The skill edit takes
  effect locally at the next `bin/brain-skills-sync`, which the owner runs.
- **`docs/configuration.md` (`BRAIN_BRIEF_DELIVERY`).** "Once the page is written" becomes
  "sends nothing for an empty brief".
- **Plist comment.** As in §4.
- **The ceiling table:**
  - `cli.py` is 8,696 lines and has a row. It grows by about 2–3 lines. The written reason is
    that the skip line must be printed where the `Wrote` line is printed, and only the caller
    knows that `wiki` was asked for. The record goes in an inline comment, a module-docstring
    pointer and a trail update in `docs/ceiling-table.md`. The trailing descriptive hop is
    promoted to a real SHA and count first, then a new descriptive hop is added, following the
    table's trail rules.
  - `mcp_server.py` is 5,174 lines. The edit is net zero, so it needs no growth record. Its
    trail already ends on a SHA-bound hop (`0f9f28b0`), so there is nothing to promote. The
    first pass of this change left its row untouched; review rev-132 asked for a descriptive hop
    recording the net-zero touch, which the review-round commit adds.
  - `brief.py` (414 lines, growing by about 15) and `brief_publish.py` (258, growing by about 15)
    stay far under 800.
  - `scripts/check_ceiling_table.py` is run.

## 8. Verification (owner's standing rules)

- Every shell exports `BRAIN_PROD_DATABASE_URL=postgresql://localhost:1/none BRAIN_SKIP_CANARY=1`.
- The test DB is `postgresql://brain:brain@localhost:5434/brief_empty_test`.
- Each run uses its own uv venv, and **never connects to 55432**.
- Every pytest run holds `.claude/full-suite.lock`, and `df -h /` must show at least 6.5 GiB
  first. Only named files run locally:
  - the new `test_brief_empty.py`;
  - `test_brief*.py`;
  - `test_cli_vault_emit_confidential.py` and `test_mcp_vault_emit_confidential.py`;
  - `test_mcp_brief.py`;
  - `test_examples_rot.py`;
  - the four hygiene files (`test_log_privacy_rules.py`, `test_log_privacy.py`,
    `test_vault_delete.py`, `test_import_hygiene.py`).

  CI runs the full suite.
- Checks before pushing:
  - `ruff check` and `mypy src/`;
  - `scripts/check_ceiling_table.py`;
  - the src-wide scans;
  - a raw-bidi/C* character scan of the new files.
- The CLI is exercised end to end only against a throwaway database and a tmp vault, never
  `~/brain-vault` or `~/.brain`.
- Commit trailer: `Claude-Session: https://claude.ai/code/session_01387iyKA2utQ48EnD1kcsZf`.
  The PR body ends with the same URL. No PII appears anywhere.

## 9. Risks

1. **A same-day re-run no longer refreshes the page** once it would be empty. This is intended,
   and I7 pins it.
2. **Old empty pages and their document rows stay** until the owner deletes them. Brain never
   deletes vault files. The CHANGELOG says so.
3. **The view swap moves earlier in `build_brief`.** That reorders code with F6 weight. It is
   covered by the existing watch tests, I4 and M6. The `withheld` comparison provably ignores
   watch fields.
4. **Marking confidential-only hits on a skipped page.** This matches today's behaviour (page
   written and delivery off → marked). I5 pins it.
5. **An existing test may encode an empty page in a way not caught by §5's list.** Handled by
   the file-by-file read and the reasons listed in the PR body.

## 10. Open questions, each with a recommendation

- **Q1.** Should an empty *terminal* digest collapse to a single "nothing to brief" line?
  **Recommend no**: keep the terminal unchanged. The request is about creation, and changing it
  would also mean restructuring `_print_brief`, which grows `cli.py` further.
- **Q2.** Should an `"empty": bool` key be added to `--json` / MCP? **Recommend no**: empty
  lists already say it, and the MCP wire shape stays stable.
- **Q3.** Should MCP `brain_brief` skip the model on an empty payload? **Recommend yes**: it is
  the same predicate, a net-zero-line edit, and it removes the invented-steps risk.
- **Q4.** Should `--force` / `--allow-empty` be added? **Recommend no** (YAGNI).
- **Q5.** Does a skipped-watch note alone count as content (D2)? **Recommend yes**: it is real
  news, and an existing test pins it.
- **Q6.** Should confidential-only pending hits be marked notified on a skipped page (§3.4)?
  **Recommend yes**: it preserves RM-15.D3.

## Rulings (team-lead, 2026-11-12)

Q1 no, Q2 no, Q3 yes, Q4 no, Q5 yes, Q6 yes, all as recommended. D3 made explicit: the
pre-model predicate alone decides; I8 and U3 pin it.

## Proof record

The runs below happened on 2026-11-12. Every run held `.claude/full-suite.lock` and started only
with free disk at or above 6.5 GiB. Each used its own uv venv (Python 3.11.15) and the test
database `brief_empty_test` on 5434. No real Ollama call was made.

**RED.** This ran against a `git archive` copy of `2aea107d`, which is the plan commit and still
has master's code. The new `tests/test_brief_empty.py` was added to the copy, plus an inert
stub `def is_empty(...): return False` so the file imports. Nothing in the copy calls the stub.
Result: **11 failed, 11 passed.**

- 10 tests failed for a behavioural reason:
  - an empty or confidential-only brief wrote a page;
  - the model was called on an empty brief, through the CLI or MCP;
  - a confidential-only watch hit was left unmarked;
  - an existing page was overwritten;
  - the CLI printed no skip line;
  - `--json --wiki` wrote a page.
- I4 (`test_an_old_unnotified_hit_…`) failed first on its `is_empty` precondition, so it was red
  only through the stub. Mutant M6 below is its real proof.
- The 11 tests that passed are the characterization and one-item cases:
  - U2, which has 7 parameter cases;
  - the field-classification guard;
  - I2, the page exactly as before;
  - C3, the `Wrote` line;
  - the MCP real-item test.

**GREEN.** This ran on `0f32826d` and gave **531 passed.** It covered:

- `test_brief_empty`, every `test_brief*`, `test_mcp_brief` and both `*_vault_emit_confidential`;
- `test_examples_rot`, `test_delivery_isolation`, `test_doctor_runtime` and
  `test_repo_hygiene_files`;
- `test_log_privacy_rules`, `test_log_privacy`, `test_vault_delete` and `test_import_hygiene`.

`ruff check` was clean, `mypy src/` was clean (369 files), and `scripts/check_ceiling_table.py`
agreed (16 files, one row each).

**Mutation.** Each mutant ran in its own `git archive` copy of `0f32826d`. The mutation script (kept
outside the repo) refuses unless its target text occurs exactly once, and the diff was counted before
every run. The test file was `tests/test_brief_empty.py`. **Every mutant was killed.**

| Mutant | Failing tests |
|---|---|
| M1 `is_empty` → False | 11, including U1, U3, I1, I3, I5, I7, I8, C1, C2 and M |
| M2 `is_empty` → True | 12, including U2 (all 7), I2, I3, I4, C3 and the MCP real-item test |
| M3 captures | U2[captures], I2, I3, C3 and the MCP real-item test |
| M3 open_todos / pinned / watch_notes / new_entities / possible_duplicates | the matching U2 case (one each) |
| M3 watch_hits | U2[watch_hits] and I4 |
| M3s `or brief.suggestions` | U3 |
| M4 terminal model call ungated | I1, I8 and I4 |
| M5 page keyed on the terminal payload | I3, I5 and I4 |
| M6 view swap after the check | I4 |
| M7 no marking on a skipped page | I5 |
| M8 the skip keeps the published payload | I1, I3, I5, I7, I8, C1 and C2 |
| M9 MCP ignores the predicate | M (empty) |
| M10 CLI skip line removed | C1 |

**Scans.** These are the src-wide scans plus every test that reads the docs, skills, plist or
CHANGELOG (`tests/` files that name them).
`test_skills_token_routing.py::test_skills_sync_is_not_drifted` fails locally by design. It
compares the repo's skills with the copies installed on this machine, and this change edits
`skills/brain-proactivity/SKILL.md`. `bin/brain-skills-sync --check` names that skill alone as
STALE. CI has no installed skills and skips the test. The owner's `bin/brain-skills-sync`
clears it, and it is not run on their behalf. The `test_bin_scripts` launchd failures in the
worktree come from the wrappers exec-ing `<repo>/.venv/bin/…`, and a worktree has no `.venv`.
A re-run in an archive copy with a `.venv` was stopped for a disk hold. On team-lead's ruling,
**CI's full suite is the run of record for the scan set** and replaces that local re-run.

## Review round rev-132 (2026-11-12)

The audit passed, with two LOW findings and one ruling. All three are fixed in `683b67c2`.

1. **LOW: ceiling table.** The `mcp_server.py` row now carries a descriptive hop for the
   net-zero touch, and §7 says so.
2. **LOW: the skip line.** The CHANGELOG and `docs/cli-reference.md` quote the full line,
   `No page written for <date>: nothing to publish (confidential documents excluded).`
3. **Ruling: a drafts-only page is written but not delivered.**
   - The behaviour:
     - `_deliver` sends nothing when `is_empty(without_drafts(published))`.
     - The page is still written.
     - It returns True, so the pending watch hits follow the existing `resolved` rule and are
       marked notified. A send would have dropped the same draft hits and marked them too.
     - One INFO line is logged.
   - Tests:
     - Two `_deliver` unit tests use an injected fake draft lookup: drafts-only gives 0 sends
       and `resolved`; a draft beside a non-draft item gives exactly 1 send without the draft.
     - Two `build_brief` twins against the DB: the drafts-only page is written, nothing is sent,
       there is no WARNING, and the hit is marked; with a non-draft item there is exactly 1 send
       without the draft.
   - **Docker was down** (the test container was stopped by the disk emergency), and conftest's
     session fixtures need the DB. So the unit tests ran under `pytest --noconftest`; they need
     only `tmp_path`. The DB-backed twins have CI as their evidence.
   - **RED** (archive of `5759430c` plus the new tests): the drafts-only unit test failed because
     one message was sent; its twin passed. **GREEN** on the worktree: 2 passed. ruff, mypy and
     the ceiling check are clean.
   - **Mutation** (each in a `git archive` copy of `683b67c2`; all killed):

     | Mutant | Failing test |
     |---|---|
     | D1 guard removed | drafts-only |
     | D2 guard on `published` instead of the draft-free payload | drafts-only |
     | D3 the skip returns False (hits left pending) | drafts-only (`resolved`) |
     | D4 guard always true | the non-draft twin |
