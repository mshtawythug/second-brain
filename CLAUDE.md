# CLAUDE.md

IMPORTANT:
1. You must write automated tests for all code (pytest). Aim for both unit tests and integration tests against a real Postgres test database.
2. You must pass ALL tests before committing.
3. Maintain a minimum test coverage threshold of 85%. Per-module targets: pure logic (chunker, search, format) 95%, ingest pipeline 90%, CLI commands 85%.
4. **NEVER commit or push without explicit user permission.** No exceptions — even in bypass permissions mode.
5. USE Team mode (native agent teams — dispatch Agent teammates; the team auto-forms, no TeamCreate/TeamDelete needed) for any multi-task work to keep the main context window clear. **Always use team-driven execution** (not inline) when executing implementation plans. See "Team Mode Override" section below.
6. After edits, run the full test suite (`pytest`) and lint (`ruff check`). Fix bugs and update tests before claiming work complete.
7. NEVER jump straight to code. Produce a written plan FIRST for any multi-file task. Get explicit approval before writing code.
8. When referencing existing modules/functions, READ the actual source file first. Never guess field names, import paths, or function signatures.
9. All Python code MUST pass `ruff check` with zero warnings AND `mypy src/` with zero errors. See "Linting" below.
10. **Pre-commit check:** Run `ruff check && mypy src/ && pytest` before committing.
11. **Memory updates:** After completing a task that adds/changes any of the following, update the corresponding memory file in the auto-memory directory: CLI commands → `cli.md`, database schema → `schema.md`, Python types/dataclasses → `types.md`, ingest extractors → `extractors.md`, search algorithm changes → `search.md`.
11a. **Memory audits:** When asked to "update memory", audit ALL memory files against the current codebase using parallel Explore agents (one per file). Update any drifted content and the MEMORY.md index.
11b. **Plans, specs & docs:** ALL plans, specs, design docs, and any `.md` files created as part of work MUST be stored under `docs/` — specs in `docs/specs/YYYY-MM-DD-<topic>-design.md`, plans in `docs/plans/YYYY-MM-DD-<topic>.md`. Never leave planning/spec documents in the repo root.
12. **MANDATORY regression tests for bug fixes:** Every bug fix MUST include a test that reproduces the original bug and verifies the fix. No bug fix is complete without a regression test.
13. **NO monkey-patching in tests. NO EXCEPTIONS.**
    - **BANNED:** Reopening production modules/classes to inject constants, methods, or attributes.
    - **BANNED:** Direct attribute assignment on imported modules without restoration.
    - **If a test needs monkey-patching to pass, the production code has a bug.** Fix the production code, not the test.
    - `monkeypatch` (pytest), `unittest.mock.patch`, and `mocker.patch` (pytest-mock) are NOT monkey-patching — they are standard test doubles with automatic cleanup. These are fine.
14. **Post-phase code review + completion audit LOOP (MANDATORY):** After completing each phase/wave of a plan, run a **review + audit loop** that repeats until BOTH pass with zero issues:
    - **Code review:** Dispatch `superpowers:code-reviewer` on ALL changes in the phase against the plan spec.
    - **Completion audit:** Dispatch a separate completion auditor checking: (a) every task implemented — nothing skipped, (b) every fix matches spec exactly, (c) every required test exists and passes, (d) no regressions, (e) no dead code — no orphaned modules, unused functions, or untested branches.
    - **THE LOOP:** If EITHER review finds issues → fix ALL issues → re-run BOTH → repeat. Exit condition: code reviewer says "APPROVED" AND auditor says "AUDIT PASSED". No phase is complete until the loop exits clean.
15. **No PII in checked-in code. NO EXCEPTIONS.** The repo has been explicitly PII-scrubbed across tests, fixtures, docs, and comments — never reintroduce real personal data. Use synthetic / redacted values for names, email addresses, phone numbers, postal addresses, meeting attendees, calendar specifics, company names, customer references, and internal project codenames. When fixing or extending tests, copy the existing synthetic pattern; never paste production data (e.g. real meeting transcripts, real email bodies, real chat threads, employer / coworker names) even temporarily. If a real value seems required for a test to be meaningful, the test is wrong — parameterize the fixture or refactor the production code so it's testable with synthetic data. This rule covers commit messages and PR descriptions too.

## Team Mode Override (MANDATORY — overrides superpowers skill routing)

**ALL agent dispatching MUST use Team mode** (native agent teams — dispatch Agent teammates; the team auto-forms and is torn down automatically, so there is no TeamCreate/TeamDelete and no team_name to pass) instead of standalone Agent subagents. This applies to every superpowers skill that spawns agents:

| Superpowers skill | Use instead | What changes |
|---|---|---|
| `superpowers:subagent-driven-development` | `team-driven-development` skill | Teammates with worktree isolation, SendMessage for coordination (team auto-forms — no TeamCreate) |
| `superpowers:dispatching-parallel-agents` | `team-parallel-dispatch` skill | Parallel teammates with worktree isolation (team auto-forms — no TeamCreate) |
| `superpowers:requesting-code-review` | Dispatch reviewer as a teammate (it auto-joins the active team), otherwise standalone Agent | No team_name needed |
| `superpowers:executing-plans` | Use `team-driven-development` instead of `subagent-driven-development` when it suggests subagents | Same redirect |
| `superpowers:writing-plans` execution handoff | **NEVER offer a choice.** Skip the two-option prompt and proceed directly with `team-driven-development`. | No "Inline Execution" option, no question asked |

**Non-agent superpowers skills are unchanged:** brainstorming, verification-before-completion, using-git-worktrees, finishing-a-development-branch, test-driven-development, systematic-debugging, receiving-code-review, writing-skills.

**Why:** Team mode provides shared task lists, inter-teammate communication via SendMessage, and better coordination visibility than isolated subagents.

**NEVER** fall back to standalone Agent subagents for execution or parallel dispatch.

**Plan header override:** When writing plan documents, use `> **For agentic workers:** REQUIRED SUB-SKILL: Use team-driven-development to implement this plan task-by-task.`

## Architecture Principles

### DRY
- Search for existing shared code before creating new modules/utilities.
- Reusable patterns: `src/brain/` for shared utilities. Extract anything used in 2+ places.
- Prefer composition over copy-paste.

### SOLID Principles (MANDATORY)
- **Single Responsibility**: Each module has ONE reason to change. Extractors only extract. The chunker only chunks. The embedder only embeds. The search module only searches. The CLI only orchestrates.
- **Open/Closed**: Open for extension, closed for modification — adding a new ingest source (e.g., `notion.py`) means adding a new extractor module + dispatcher entry, not editing existing extractors.
- **Liskov Substitution**: All extractors return `ExtractedDoc`. All search backends conform to the same result shape. Callers don't care which subclass.
- **Interface Segregation**: Modules expose narrow, focused public APIs. Don't force a consumer to import a kitchen-sink module to get one helper.
- **Dependency Inversion**: High-level modules accept dependencies via constructor / function args (e.g., `count_tokens` callable on the chunker, `client` on the embedder). Tests pass fakes; production passes real ones.

### Coding Conventions
- PEP 8, `snake_case` everywhere.
- Type hints on every function signature. `mypy src/` must pass.
- f-strings for string formatting (no `%` or `.format()`).
- `pathlib.Path` for filesystem paths, never raw strings.
- Dataclasses for value objects (`ExtractedDoc`, `Chunk`, `SearchResult`).
- Custom exceptions inherit from a project-specific base (`BrainError` if needed); never `raise Exception(...)`.
- **Every module has a one-line docstring** at the top describing its purpose.
- **Use parameterized SQL** (`%s` placeholders + tuple). NEVER concatenate user input into SQL strings.
- **Every external HTTP/DB client MUST have explicit timeouts.** Voyage SDK retries handled in `embeddings.py`; Postgres connections set `connect_timeout`.
- **No bare `except:`** — always catch specific exceptions (`psycopg.OperationalError`, `voyageai.error.RateLimitError`, etc.).

### File-size ceiling — 800 lines

The global rules set an **800-line ceiling** per file (200–400 typical). It binds
**new modules and new growth**, not a retroactive split mandate: this repo keeps
**a standing list of files already over it** (the table below — re-derive the
list, never count it from memory), and splitting a 9,000-line CLI to satisfy a
number is how you turn a working module into six broken ones.

The rule as it actually applies here:

- A **new** module lands under 800. No exceptions.
- An **existing** file under 800 must stay under it — extract rather than grow
  past. This is why `brain/backup/db_names.py` exists: `backup/restore.py` was
  745 before the identifier byte-budget guard (`f056c08`) and 806 after, so the
  guard was moved out rather than left over the line.
- A file **already over** may grow only for a written reason, recorded in **two
  complementary places — not either/or**: (1) an inline comment **at the point of
  growth**, for the reviewer reading the diff, and (2) a one-line pointer **in the
  module docstring**, for the person who opens a 4,000-line file and asks why it
  is allowed to be this big. They read the top; they do not `git blame` line 583.
  Without (2) the justification exists but is undiscoverable, and a rule whose
  evidence cannot be found is not auditable.

  "It was already over" is not a reason.

**Files over the ceiling** (`find src -name '*.py' | xargs wc -l | sort -rn` —
re-derive, never inherit.)

**No row below carries a head count any more.** Each row's **Lines** cell holds
the command that measures the file; each **Why** cell opens with a **Trail** of
SHA-bound counts, which are frozen —
`git show 3b16527:src/brain/cli.py | wc -l` returns the same digit forever.
Every trail starts at `f8c76c0`, this branch's base. A trail is authoritative
only *through the last SHA it names*; enumerate anything after it with
`git log --oneline f8c76c0..HEAD -- <path>`, and take the present count from the
Lines cell.

The head counts are **deleted rather than refreshed**, and that is the whole
fix: **a live digit is invalidated by the write that states it** — and so is a
delta. Five successive repairs of this table each corrected a stale number and
shipped a fresh one; the most recent was titled *"stop asserting no-growth for
two files that grew"* and grew both files by writing their own pointers. The
rule that terminates the regress is not a better number, and not a timestamp
proving when a number was true: it is **no digit in any position that a write
can invalidate.** Earlier passes recorded dates and clock times precisely
because a live digit needs an as-of; a frozen digit needs none, which is how you
can tell the form is right.

**There is deliberately no "head" SHA here any more, and there cannot be one.**
The slot used to read `head 4740f03`; before that, other values. It was wrong
every time, and not through carelessness — a header cannot name the commit that
writes it, because that commit has no hash until after the write. Every value
ever typed there was the author's *starting* commit, i.e. stale on arrival. So
the slot has been replaced with a claim that stays true: `b71c923` is not
asserted to be head, only to be the commit the trails below were enumerated at
— which is why three rows can say "no commit **through `b71c923`** touched it"
without that going stale. It is a fact about a frozen commit and cannot rot. For
the present head, run `git rev-parse --short HEAD`; to see what moved since,
`git diff --stat b71c923..HEAD -- src`.

**Two rows of this table were false when you last read them, and they were false
in the specific way that hides itself.** `connect.py` and `timeline.py` both
recorded *no growth* (`925 → 925`, `844 → 844`) while on disk they were 952 and
877 — both grown by `ec6afb6`, both already over the ceiling at base. A row that
asserts no-growth tells a reader checking compliance that there is nothing to
check, so the error suppressed its own detection. Both are now carried in the
`mcp_server.py` row's rot-proof form (no live digit; base plus commit trail plus
a `wc -l` command), and both files gained the module-docstring pointer that
requirement (2) always owed and that neither had. Requirement (1) — inline
reasoning at the growth site — was satisfied for both all along; it was only
ever the discoverable half that was missing.

**Read every trail as: code growth PLUS the ceiling record that justifies it.**
Writing the required docstring record into a file makes that file longer, which
is a small, honest irony rather than a measurement error: of the +30 `search.py`
gained between `f8c76c0` and `0473b5f`, sixteen lines are the `recency_ts` work
and fourteen are the record saying why. That delta is safe to state only because
**both** of its endpoints are commits — it is a fact about two frozen trees, not
a claim about the working one. Where the split matters,
`git diff f8c76c0..<sha> -- <path>` shows the code delta alone.

| File | Lines | Why it is not being split |
|---|---|---|
| `cli.py` | **re-derive: `wc -l src/brain/cli.py`** | Trail: 9,019 (`f8c76c0`) → 9,019 (`3b16527`, touched, no net change) → 9,035 (`c62e3de`). One Typer app; every command shares its option decorators and error mapping. A split was scoped during the GraphRAG build (G0–G4) and deferred deliberately. **No longer unchanged:** `_VALID_SOURCE_KINDS` became a re-export of `brain.source_kinds` so the ingest *write* boundaries could enforce it — `cli` imports `cli_ingest`, so the set could not have stayed here. Net *less* duplication. **Recorded** inline + in the docstring. |
| `mcp_server.py` | **re-derive: `wc -l src/brain/mcp_server.py`** | Same shape — one MCP tool registry. **This cell deliberately holds no digit, because the digit has now been wrong three times.** It read `→ 4,137 (+128)` (a value the file never held, wrong *when written* in `0473b5f`); it was corrected to `4,191` on 2026-08-21; and `4,191` was stale within the hour, because the F6 listing-gate work landed in the same file that same afternoon. A live count in a checked-in table is a measurement of a tree that stops existing at the next commit, and this is the one row in this table whose subject is also actively edited by whoever reads it. What does not rot is the base and the per-commit trail: 4,009 (`f8c76c0`, branch base) → 4,043 (`3b16527`) → 4,055 (`c62e3de`) → 4,191 (`2ed2d83`) → 4,356 (`ec6afb6`). (The trail stopped at `2ed2d83` until 2026-08-21: the prose already described the `ec6afb6` listing-gate work, but the *numeric* trail — the half this cell calls rot-proof — was missing its last hop, so the two halves of the same row disagreed. `0473b5f` touched the file at net zero and is correctly absent from a **growth** trail.) Five growths, each reasoned inline where it landed and summarized in the module docstring: `_split_source_filter` (so `source="none"` means the same thing here as in the UI); `source` validation at the `brain_ingest_stdin` write boundary; the F6 confidential lens on `brain_list`; the same lens on `brain_backlinks`/`brain_links`, whose polarity **inverts** across the `vault.graph` boundary; and the same lens on the four UNPROMPTED listing tools — `brain_brief`, `brain_review_weekly`, `brain_timeline`, `brain_connect_list` — which took no document id and no query at all, so every document they named was one the caller had not asked for. Two of those (`brain_brief`, `brain_review_weekly`) were BODY egress, not title egress: `todo.iter_action_item_docs` selects `documents.content` and parses action-item text out of it. The docstring also carries the decorator hazard that cost `brain_recall` its registration. **The disclosure the rule requires is *what grew and why*, which is above; the measurement belongs in the command, which is in the Lines cell.** |
| `ingest/__init__.py` | **re-derive: `wc -l src/brain/ingest/__init__.py`** | Trail: 2,298 (`f8c76c0`) → 2,352 (`3b16527`) → 2,366 (`0473b5f`). Dispatcher + both pipelines; the extractors are already separate modules. Growth is the extraction of `mirror_is_stale` / `write_vault_mirror` so a caller owning the outer transaction can defer the mirror write past its own commit. Near-zero net-new logic. **Now recorded.** |
| `config.py` | **re-derive: `wc -l src/brain/config.py`** | Trail: 2,250 (`f8c76c0`) → 2,289 (`3b16527`) → 2,303 (`0473b5f`). Knobs belong beside the other knobs. Growth is one knob (`BRAIN_UI_SERVE_CONFIDENTIAL_TITLES`) plus its tri-state parse and the ruling for why it is not `serve_confidential_bodies`. **Now recorded** — and the docstring names the real next move: the *prose* is what makes this file large, so split the rationale out, not the knobs. |
| `vault/sync.py` | **re-derive: `wc -l src/brain/vault/sync.py`** | Trail: 1,747 (`f8c76c0`) → 1,817 (`c62e3de`). One reconciliation algorithm; the walk and the per-file upsert only make sense together. Growth is `_source_from_frontmatter` validating a file's `source:` against `brain.source_kinds` — the **third and last** unvalidated write boundary into `sources.kind` (`cli_ingest` and `mcp_server` closed the other two, and 2-of-3 was a worse resting state than 0-of-3). Most of the +70 is the ruling on the FAILURE MODE — dropped and warned, never rejected and never substituted — which is the non-obvious part and the reason a reviewer opens this function at all. **Recorded** inline + in the docstring. |
| `queries.py` | **re-derive: `wc -l src/brain/queries.py`** | Trail: 1,642 (`f8c76c0`) → 1,642 (`3b16527`, touched, no net change). Flat read-helper collection — cohesive, low coupling; splitting buys nothing. |
| `setup.py` | **re-derive: `wc -l src/brain/setup.py`** | Trail: 1,356 (`f8c76c0`); no commit through `b71c923` touched it. One linear install script with three profile branches. |
| `wiki/build_watcher.py` | **re-derive: `wc -l src/brain/wiki/build_watcher.py`** | Trail: 1,073 (`f8c76c0`); no commit through `b71c923` touched it. |
| `vault/watch.py` | **re-derive: `wc -l src/brain/vault/watch.py`** | Trail: 1,070 (`f8c76c0`); no commit through `b71c923` touched it. |
| `people.py` | **re-derive: `wc -l src/brain/people.py`** | Trail: did not exist at `f8c76c0` → 934 (`3b16527`). **Not growth.** `wiki/build_people.py` renamed to `brain/people.py`; the only content change is rewriting `brain.wiki._person_name` imports/references to `brain.person_name`. Verified by diff — same line count, no new logic. |
| `connect.py` | **re-derive: `wc -l src/brain/connect.py`** | One scoring algorithm plus the `## See Also` writeback primitives its two callers (CLI, MCP) share. **This row read `925 → 925` while the file was 952** — asserting no-growth for a day after `ec6afb6` landed, which is worse than a stale digit because it tells a compliance reader to skip the file. No digit here now; base and trail: 925 (`f8c76c0`, branch base) → 925 (`3b16527`, touched, no net change) → 952 (`ec6afb6`) → 972 (`56cc984` — the commit that wrote this very row, whose +20 lines *are* the ceiling record) → **the commit that added this clause** — named descriptively and carrying *no delta*, because a hop cannot know its own SHA and a delta would be invalidated by the same write that states it. **A record of a file's size is invalidated by the act of writing the record**, so no hop can name its own commit: the hash does not exist until after the write. That, not carelessness, is why this trail and `timeline.py`'s each stopped one hop short twice running — and why the fix is not a better digit but a *terminating form*: SHA-bound historical hops (frozen, cannot rot), a descriptive last hop for the in-flight change, and `wc -l` for the present. Read it as authoritative only **through the last SHA it names**; enumerate the rest with `git log --oneline f8c76c0..HEAD -- src/brain/connect.py`. The growth is the F6 confidential gate on `iter_suggestions`, which gates **both** joins — a suggestion names two documents, so a source-only filter would still publish every confidential doc that was somebody's suggested *target*. **Now recorded** inline + in the docstring. |
| `graph_rag/extract.py` | **re-derive: `wc -l src/brain/graph_rag/extract.py`** | Trail: 885 (`f8c76c0`) → 885 (`3b16527`, touched, no net change). |
| `search.py` | **re-derive: `wc -l src/brain/search.py`** | Trail: 837 (`f8c76c0`) → 853 (`3b16527`) → 867 (`0473b5f`). Already over at base. Growth is `SearchResult.recency_ts` (its read hoisted out of the boost branch so a hit's shown date and its ranking date cannot disagree) plus `source_missing` threading. **Ranking unchanged — no eval re-baseline implied.** The inline comments were always there; the missing half was the docstring pointer, **now written**. |
| `timeline.py` | **re-derive: `wc -l src/brain/timeline.py`** | One temporal-bucketing algorithm; the three query helpers exist only to share its WHERE-clause composition. **This row read `844 → 844` while the file was 877** — the same self-hiding no-growth assertion as `connect.py`, from the same commit. No digit here now; base and trail: 844 (`f8c76c0`, branch base) → 877 (`ec6afb6`) → 900 (`56cc984` — the commit that wrote this very row, whose +23 lines *are* the ceiling record) → **the commit that added this clause**, named descriptively and with no delta, for the reason the `connect.py` row gives. See the `connect.py` row for the general form; use `git log --oneline f8c76c0..HEAD -- src/brain/timeline.py` for anything after the last SHA named. The growth is the F6 confidential gate threaded through `_compose_doc_filter` out to `build_timeline`, applied in the **predicate, not the projection**, so one clause drops the document from `doc_ids`, `doc_titles`, the co-topic tally, the count arithmetic, the auto-granularity probe and the synthesis bundle at once — none separately gated. Withholding only `doc_titles` would still publish the ids. **Now recorded** inline + in the docstring. |
| `cli_ingest.py` | **re-derive: `wc -l src/brain/cli_ingest.py`** | Trail: 844 (`f8c76c0`) → 867 (`c62e3de`). Over the ceiling the day it was extracted from `cli.py`. Growth is `--source` validation at the top of `ingest_stdin`: `sources.kind` is bare `TEXT NOT NULL` with no CHECK, so this guard is all that stood between a typo and a permanently mis-bucketed row. **Recorded.** |
| `related.py` | **re-derive: `wc -l src/brain/related.py`** | Trail: 714 (`3b16527`, where the wiki/ui split created this module) → 720 (`0473b5f`) → 726 (`7b5579e`) → **the commit that added this row**, descriptive and with no delta. **This file CROSSED the ceiling rather than staying under it, and that is a rule violation, not a deferral.** The rule above says an existing file under 800 must “extract rather than grow past”; the F6 confidential gate on `compute_related` / `_eligible_source_docs` grew it past instead. It is here, over, on purpose: the alternative was to ship the security fix with a docstring claiming the file was still under — which is what the first draft did. The extraction is not folded into the same commit because a 250-line module move inside a body-egress fix makes both harder to review; the seam (the lexeme/tsquery tail, already covered by `tests/test_build_related_signal.py`) is named in the module docstring. **Recorded** inline at the gate + in the docstring. |
| `enrichment.py` | **re-derive: `wc -l src/brain/enrichment.py`** | Trail: 808 (`f8c76c0`); no commit through `b71c923` touched it. |

`backup/restore.py` is deliberately **absent**: it crossed the ceiling on this
branch and was brought back under by the `db_names.py` extraction. Trail:
745 (`f8c76c0`) → 806 (`f056c08`) → 755 (`0473b5f`); present count via
`wc -l src/brain/backup/restore.py`. Its absence is conditional on it being
**under** the ceiling, which is a live condition and not a settled one — if a
later commit pushes it back over, it needs a row.

`wiki/build_related.py` is a different case and **is not gone**: it was
**split, not renamed**. The scoring half moved to `brain/related.py`; the file
itself survives as the thin emitter §9.2d(2) requires, because
`wiki/build_swap.py` and `wiki/build_watcher.py` still call `refresh_related`.
Both files exist at HEAD — re-derive with
`wc -l src/brain/wiki/build_related.py src/brain/related.py`, and note the split
**added** lines overall rather than shrinking anything.

*(Corrected 2026-08-21. This paragraph read: "`wiki/build_related.py` (815) is
also gone — renamed to `brain/related.py` and *shrunk* to 714 in the process."
Three claims, three wrong: the file exists, it was a split rather than a rename,
and the "shrunk to 714" figure was stale in the very commit that wrote it —
`0473b5f` is the commit that took `related.py` past 714. Contrast the `people.py`
row above, which says "renamed" and **is correct**: `wiki/build_people.py` really
is gone. Two adjacent moves of the same shape, one a rename and one a split, is
exactly the pair that invites a reader to assume the second from the first.)*

`source_kinds.py` is **new on this branch and deliberately not in the table** — a
new module lands under 800, and this one exists precisely so a constant could
stop being copied: it is now the single definition behind both
`cli._VALID_SOURCE_KINDS` and `ui.schemas.VALID_SOURCE_KINDS`.

**`ui/schemas.py` does not keep its own copy.** It re-exports the canonical
object (`VALID_SOURCE_KINDS = _CANONICAL_SOURCE_KINDS`), so there is one
frozenset and both names bind it — the drift risk is *deleted*, not guarded. The
old justification for a second literal (reaching the set meant importing the
Typer CLI into every HTTP handler) is satisfied by the extraction itself:
`brain.source_kinds` does not import `typer`. `tests/test_ui_schemas.py` asserts
**identity** (`is`), not equality, because the surviving failure mode is someone
restating the literal — which produces an equal-but-separate object that `==`
would wave through.

*(Corrected 2026-08-21; the same false claim was in `src/brain/source_kinds.py`'s
own docstring and is fixed there too. The line count this paragraph used to cite
is dropped rather than refreshed — nothing here needs it, and it was one more
number to rot.)*

**Closed 2026-08-20 — the four unrecorded growths now carry records.**
`mcp_server.py`, `ingest/__init__.py`, `config.py` and `search.py` each grew on
this branch without the two-place record. Every one now has a module-docstring
pointer, and each already had (or has been given) the inline comment at the
point of growth. Two files were added to the list while closing it: `cli.py` and
`cli_ingest.py`, both of which *this* work grew, and both recorded in the same
edit rather than left for the next pass — which is the only way the rule holds.

**One thing the ceiling audit found that the ceiling is not about.** Reading
*what* had grown in `mcp_server.py` — rather than only *how much* — surfaced a
shipped regression in `3b16527`: `_split_source_filter` was inserted between an
`@mcp_app.tool()` decorator and `def brain_recall`, so the decorator bound to the
helper. `brain_recall` was silently dropped from the MCP surface and a private
helper was published in its place. Nothing failed: not import, not mypy, not the
suite. Fixed, and guarded from both directions by
`tests/test_mcp_tool_registration.py`. The lesson is about the audit, not the
rule — *how much a file grew* is a number, *what grew in it* is a review.

There is **no automated gate** on this — the ceiling is a review checkpoint, not
CI. If you add a file to this table, add the row in the same edit.

**The residue — this table can be rot-proof in every digit and still be wrong by
omission.** Deleting the live head counts closes one failure mode: a row stating
a size the next commit falsifies. It does not touch the other, which is larger.
**Nothing detects a file that newly crosses 800 and never gets a row.** A stale
digit at least has a cell you can check; a missing row has nothing to check, so
it cannot be caught by reading this table more carefully — the *row set itself*
is a live claim, and the only thing re-deriving it is a human remembering to.

**It is not hypothetical, and it is no longer even a prediction: it happened in
the same change set that wrote this paragraph.** This text used to say
`src/brain/related.py` "currently sits a handful of lines under the ceiling", so
the next feature to touch it would open the gap. In fact the feature had already
landed — the F6 confidential gate had taken that file from 726 to 828 — so the
sentence predicting the gap was itself written across it, and the file's own
docstring claimed "under 800" at the same time. Three separate documents
asserted a state the tree contradicted, and every digit in this table was
rot-proof throughout. That is the by-omission failure, demonstrated rather than
described: **rot-proofing the digits does not detect a missing row.** `related.py`
now has one.

Closing it is a **build change, not a docs change**: a CI step that runs
`find src -name '*.py' -exec wc -l {} + | sort -rn`, diffs the over-800 set
against the rows here, and fails when a file is in one and not the other. That
check is deliberately **not implemented** — it needs its own review, its own
tests, and a decision about where the row set gets parsed from, none of which
belong in a docs pass. Until it exists this paragraph is the gate: when you
audit this section, **re-derive the row set, not just the digits.**

> **Merge note.** `feat/agentic-token-reduction` carries its own version of this
> section, with the same heading and at the same position, holding *that*
> branch's measurements. That is deliberate: two same-titled sections at one
> anchor make git conflict here and force a human to reconcile the two tables.
> Filing this under a different heading or elsewhere in the file would merge
> clean and leave the repo with two contradictory ceiling tables, which is the
> worse outcome. Resolve by re-deriving the counts on the merged tree.

### Linting — Ruff + mypy
Run after every change: `ruff check` (lint) or `ruff check --fix` (auto-fix), then `mypy src/`. Config in `pyproject.toml` under `[tool.ruff]` and `[tool.mypy]`.

**Key rules:** Line length 100, target Python 3.11, `from __future__ import annotations` not needed (3.11+ has native PEP 604 union syntax). Sort imports with `ruff check --select I --fix`.

### Eval gate (CI)
The eval-marker harness (`tests/test_eval_harness_live.py`) is **excluded from the default pytest invocation** (`pyproject.toml` → `addopts = "... -m 'not eval'"`). Rationale: it requires a live Postgres + Ollama, would slow every local `pytest` run, and the threshold assertions assume the live brain corpus.

**CI enforces it separately.** `.github/workflows/eval.yml` runs on every PR, every push to `main`/`master`, and manual `workflow_dispatch`:

1. Brings up the pinned Apache AGE test instance via `docker compose -f docker-compose.age-test.yml up -d --build` (PostgreSQL 16 + pgvector 0.8.6 + AGE, port **5434**, db `second_brain_test` — the same instance the local test suite uses). A GitHub Actions `services:` block can't `build:` an image inline, so compose is used instead of a service container; one eval-marked test reaches the AGE-backed graph layer.
2. Installs the package with `pip install -e ".[dev]"` and waits for `pg_isready` on port 5434.
3. Runs `pytest -m eval --no-cov -v`. The eval-marked tests SKIP cleanly without a live corpus + Ollama, so in CI this is import/collection regression coverage — it turns red only when the harness itself breaks.
4. Conditionally runs `brain eval --baseline ci --diff --fail-below`, but only when `tests/eval/baselines/ci.json` exists (dormant otherwise, printing a skip notice — recording that baseline needs a live corpus + Ollama, so it is a coordinator step, not CI's).
5. Tears down the AGE instance with `docker compose -f docker-compose.age-test.yml down -v` (always, even on failure).

**Decision recorded (Wave A.1):** the eval marker stays OFF in the default `pytest` invocation. Gate lives in CI only. Local devs run `pytest -m eval` manually when needed.

**Updating a committed baseline:**
1. Locally with a populated brain + Ollama: `brain eval --record-baseline ci`
2. Inspect the diff: `git diff tests/eval/baselines/ci.json`
3. Commit the new baseline JSON alongside the change that justifies the new numbers.

The `--fail-below` flag exits with code `3` (distinct from `1` = generic error and `2` = Typer BadParameter) when any mean metric — nDCG@5, MRR, or Recall@20 — regresses by more than `1e-4` (one unit at the baseline's 4-decimal serialization precision). Uniform threshold across all three metrics — no per-metric overrides (kept simple; the one downstream consumer is the CI workflow). `--fail-below` requires `--diff`; passing it alone exits `2` (Typer BadParameter).

`tests/eval/baselines/.gitignore` ignores `*.json` by default; explicitly-named committed baselines (currently `ci.json`) are allowlisted with `!ci.json`. Add new committed baselines the same way — do not blanket-allow.

**Wave A.1 source:** audit `docs/audits/2026-05-14-q1-codex-cumulative-review.md`, plan `docs/plans/2026-05-14-plan-audit-gap-remediation.md`. The plan tracks the remaining waves (A.2 person-variant key expansion, A.3 EXEC tracker reconciliation, A.4 first committed `ci.json` baseline).

### Migration Safety
- Migrations are raw SQL files in `src/brain/migrations/` (shipped inside the `brain` package so wheel/pip installs bundle them), applied in name order by `brain init` via `db.migrations_dir()`.
- **Never reference Python code in migrations** — they are pure SQL, frozen in time.
- **Every migration must be idempotent or applied to a fresh schema.** During development, `docker compose down && rm -rf data/postgres && docker compose up -d && brain init` resets cleanly. (`docker compose down -v` alone won't wipe the data — Postgres is mounted from a host bind-mount at `./data/postgres`, not a Docker-managed volume.)
- **Schema changes** = new numbered migration file. Never edit `001_init.sql` once shipped.

### Security Standards
- UUID primary keys everywhere.
- Parameterized SQL queries only.
- Never hardcode secrets — `VOYAGE_API_KEY` and `DATABASE_URL` come from `.env` (gitignored). Never commit `.env`.
- Never log full document content at INFO level — it can contain sensitive transcripts/emails. Log titles + IDs only.

## Workflow

### Plan → Approve → Implement → Verify
1. **Plan** — List affected files, modules, tests, risks.
2. **Approve** — Present plan to user. Do NOT write code until approved.
3. **Implement** — File by file. Use teammates for large tasks (per Team Mode Override above).
4. **Verify** — Run `ruff check && mypy src/ && pytest`. Then exercise the CLI end-to-end (`brain doctor`, `brain status`, a sample `brain search`).

### Teammate Usage
Use teammates for: parallel test creation, codebase exploration, tasks consuming >30% of context window, implementation when plan is approved. See "Team Mode Override" — always Team mode, never standalone subagents.

## Project Overview

Local personal knowledge base ("second brain") with hybrid search, designed to be queried by Claude from any conversation. Stores career documents, interview prep, Krisp call transcripts, Slack threads, and selected Gmail in Postgres + pgvector. Searches use Reciprocal Rank Fusion of FTS rank + vector cosine similarity.

**Embeddings:** Pluggable via the `BRAIN_EMBEDDER` env var. Default `arctic` =
Snowflake Arctic Embed v2 (1024-dim, local Ollama, free, Apache 2.0).
Alternatives: `voyage` (Voyage AI `voyage-3.5` SaaS, paid, 1024-dim) and
`qwen3` (Qwen3-Embedding-8B, local Ollama, free, China-origin, 4096-dim — no
HNSW index because pgvector caps at 2000 dims for `vector`).

Full design in `docs/specs/2026-04-24-second-brain-design.md`. Implementation plan in `docs/plans/2026-04-24-second-brain.md`. Pluggable-embedder retrofit plan in `docs/plans/2026-04-28-local-embeddings-qwen3-8b.md`.

## Tech Stack

- **Language:** Python 3.11+
- **CLI framework:** Typer
- **Database:** PostgreSQL 16 + pgvector (Docker, port 55432)
- **Embeddings:** Pluggable backends behind a single `Embedder` Protocol.
  Default `arctic` (Snowflake Arctic Embed v2 over local Ollama, 1024-dim).
  Alternates: `voyage` (Voyage AI SaaS, 1024-dim) and `qwen3` (Qwen3-Embedding-8B
  over local Ollama, 4096-dim). Selected at setup time via `BRAIN_EMBEDDER`.
- **PDF/DOCX:** pypdf, pdfplumber, python-docx
- **Markdown:** markdown-it-py
- **Tokenization:** tiktoken (offline `cl100k_base`, used by every backend for chunker budgeting)
- **Output:** Rich (colored tables, JSON)
- **Graph retrieval (experimental):** Apache AGE (openCypher graph in-Postgres) +
  networkx (Louvain community detection). Entity-centric GraphRAG alongside the
  vector/FTS search. Needs the custom AGE Postgres image; default-OFF via
  `BRAIN_GRAPH_ENABLED`.
- **Tests:** pytest, real Postgres test DB, fake embedder fixture
- **Lint/Type:** ruff, mypy

## Build & Run Commands

```bash
# Setup (default arctic backend — local Ollama, free, no API key)
brew install ollama                      # macOS; on Linux follow ollama.com/install
brew services start ollama
ollama pull snowflake-arctic-embed2

cp .env.example .env                     # BRAIN_EMBEDDER=arctic by default
python3.11 -m venv .venv
source .venv/bin/activate
pip install -e ".[dev]"
docker compose up -d                     # Postgres + pgvector on port 55432
brain init                               # applies migrations + aligns chunks.embedding dim
brain reembed                            # backfill any NULL embeddings + finalize column
brain doctor                             # health check (env, Postgres, embedder, gws CLI)

# Daily use
brain ingest <file>                      # ingest a single file
brain ingest-dir <dir>                   # recursive ingest
brain search "..."                       # hybrid search
brain show <id-prefix>                   # full document
brain reembed                            # backfill missing embeddings; idempotent

# Switching backends (destructive — requires data wipe + re-ingest)
docker compose down && rm -rf data/postgres
# edit .env (BRAIN_EMBEDDER=qwen3 / voyage / arctic)
docker compose up -d && brain init && brain ingest-dir <…> && brain reembed

# GraphRAG (experimental — entity graph alongside vector/FTS search)
# Requires the custom Apache AGE Postgres image
#   (second-brain-age:pg16-v1.5.0-rc0-pgv0.8.6), NOT the stock pgvector prod
#   image. `pip install -e ".[dev]"` pulls the networkx dep. Set
#   BRAIN_GRAPH_ENABLED=true in .env to enable the ingest-time graph sync.
brain init                               # also bootstraps AGE + applies graph migrations when the image ships AGE
brain graphrag build --backfill          # backfill the people graph from existing docs
brain graphrag communities build         # detect + summarize communities (needed for --mode global)
brain graphrag search "..."              # graph retrieval (modes: auto|local|themes|global|fuse)
brain graphrag themes --person "Jane Doe"  # "themes in my conversations with X"
brain graphrag search "..." --mode fuse  # RRF of the graph leg + vector/FTS hybrid leg
brain graphrag communities list          # admin view of materialized communities
brain doctor                             # also reports AGE + graph health (soft check)
# Usage skill (Claude Code): skills/brain-graph/SKILL.md (brain-graph) — graph
#   retrieval for themes / patterns / connections across interactions (themes
#   with a person, what connects A and B, recurring themes), alongside plain
#   hybrid search.

# Testing
pytest                                   # full suite
pytest --cov=brain --cov-report=term     # with coverage
pytest tests/test_chunker.py -v          # single file

# Linting
ruff check                               # lint
ruff check --fix                         # auto-fix
mypy src/                                # type check
```

## Architecture

### Layout
```
src/brain/
  cli.py            — Typer app, all commands (init / doctor / status /
                      ingest* / search / show / list / tag / edit / rm / reembed)
  config.py         — env loading; selects BRAIN_EMBEDDER ∈ {arctic, voyage, qwen3}
  db.py             — psycopg connection + migration runner;
                      ensure_embedding_column() reconciles chunks.embedding
                      dim against the active embedder
  embeddings.py     — `_OllamaEmbedderBase` shared transport; concrete
                      `ArcticEmbedder`, `Qwen3Embedder`, `VoyageEmbedder`;
                      `make_embedder(cfg)` factory dispatched on cfg.embedder
  errors.py         — BrainError base + id-prefix exceptions, OllamaEmbedError
  queries.py        — read helpers (resolve_document_prefix, fetch_document,
                      list_documents, summary_counts) + reembed helpers
                      (iter_chunks_missing_embedding, finalize_embedding_index,
                      embedding_column_state)
  search.py         — hybrid FTS + vector via RRF
  format.py         — human + JSON output
  edit_session.py   — JSON-header + body editor flow used by `brain edit`
  mcp_server.py     — stdio MCP server (brain-mcp entry point)
  chat.py           — shared public chat_json() Ollama call (brief / ask / audio)
  activity.py       — shared time-windowed activity reader (brief + weekly review)
  resurface.py      — `brain resurface` spaced-repetition scoring core
  brief.py          — `brain brief` daily-digest assembly + next-step suggestions
  review/           — `brain review` package: scans (contradiction/staleness),
                      weekly synthesis, queue queries, emit/render
  timeline.py       — `brain timeline` temporal bucketing over graph entities
  connect.py        — `brain connect` auto-link scoring core
  cli_connect.py    — `brain connect` CLI sub-app (list/refresh/accept/reject/stats)
  ask.py            — `brain ask` agentic plan/reflect/synthesize loop
  audio.py          — `brain audio` two-host script generation + TTS Protocol
  gaps.py           — `brain gaps` search-failure clustering + detector
  eval/             — retrieval + answer eval harness; answer_eval.py backs
                      `brain eval --answer`
  ingest/
    __init__.py     — Embedder Protocol (declares `dim`); dispatcher +
                      ingest_document() / update_document() pipelines
    chunker.py      — paragraph-aware chunking (uses embedder.count_tokens)
    text.py / markdown.py / pdf.py / docx.py — file extractors
    gmail.py        — shells out to gws CLI
    stdin.py        — generic stdin ingester (Krisp, Slack)

src/brain/migrations/ — numbered SQL files (packaged inside the brain package; applied by brain init)
  001_init.sql                    — base schema (chunks.embedding starts as vector(1024))
  002_qwen3_embedding.sql         — drops + re-adds chunks.embedding as vector(4096), nullable
                                    (ensure_embedding_column then resizes for the active backend)

scripts/embedding_smoke.py        — retrieval sanity check for a corpus +
                                    seeded queries (used during backend swaps)

tests/              — real-DB fixture, fake embedder, one test file per module
docs/specs/         — design specs
docs/plans/         — implementation plans
```

### Key Patterns
- **Idempotent ingest:** `documents.content_hash` is UNIQUE on `documents`. Re-ingesting the same file is a no-op unless `--force`.
- **Source dedup:** `sources(kind, external_id)` is UNIQUE. Re-ingesting the same Krisp/Gmail message updates the existing row instead of duplicating.
- **Hybrid search:** Reciprocal Rank Fusion (k=60) combines FTS rank and vector cosine similarity. Ranks chunks; groups by document; returns top N docs with best matching chunk as snippet.
- **Claude-orchestrated ingestion:** Krisp and Slack don't have CLIs. Claude calls the MCP, then pipes content into `brain ingest-stdin --source krisp/slack ...`.
- **Pluggable embedders behind one Protocol.** All three backends conform to `brain.ingest.Embedder` (`dim: int`, `embed(texts, *, input_type)`, `count_tokens`). The factory `make_embedder(cfg)` is the only place that knows about concrete backends; ingest, search, and reembed depend only on the Protocol.
- **Dim-aware finalize.** `queries.finalize_embedding_index` applies `NOT NULL` on `chunks.embedding` for every backend, then conditionally creates an HNSW cosine index when `embedder.dim ≤ 2000` (arctic, voyage). For Qwen3 (4096) the index is skipped — pgvector 0.8.x caps HNSW/IVFFlat at 2000 for `vector`; sequential cosine scan is acceptable at personal-corpus scale.
- **Backend-aware `brain init`.** `init` runs migrations, then `db.ensure_embedding_column` reconciles `chunks.embedding`'s declared dim against `embedder.dim` — drop + re-add on a fresh DB, error with a destructive-reset hint if chunks already exist at a different dim.
- **Switching backends is destructive.** Embeddings cannot be re-projected across models; switching requires `docker compose down && rm -rf data/postgres && brain init && brain ingest-dir … && brain reembed`. Postgres data is a host bind-mount (`./data/postgres`), not a Docker volume — `docker compose down -v` alone is *not* sufficient.

## Lessons Learned

These are universal mistakes — never repeat them.

1. **Never guess fields/paths** — Read the actual source file before referencing any field, function, or import path.
2. **Never declare "done" without running it** — Run the CLI command end-to-end before claiming a task complete. Tests passing ≠ feature working.
3. **Never copy-paste code** — Extract shared patterns into reusable functions. If you write something twice, refactor on the third write.
4. **Test mocks must match signatures** — After changing a function's signature, grep ALL call sites and update them. Run `mypy src/` to catch drift.
5. **Four Phase Tests** — Every test follows setup → exercise → verify → teardown. Clear separation between phases. No mystery guests — make test data explicit, don't rely on invisible factory defaults.
6. **No test failures are pre-existing** — Every failure is a regression until proven otherwise. Investigate immediately, never dismiss as "pre-existing" or "flaky" without evidence.
7. **The Scout Law — leave the code better than you found it** — Before completing any task, ALL tests in the suite must pass — not just the ones related to your changes. If you encounter broken tests from other areas, fix them.

## Default Design Skill: open-design

For any design work — UI/UX, components, slides/decks, branding, motion, design reviews, image/video generation, design systems — prefer skills from the **open-design** collection (installed in `~/.claude/skills/`).

Examples: `frontend-design`, `design-review`, `design-brief`, `creative-director`, `ui-ux-pro-max`, `web-design-guidelines`, `color-expert`, `brand-guidelines`, `theme-factory`, `pptx-generator`, `slides`, `apple-hig`, plus 70+ design-system templates.

The Anthropic `frontend-design` plugin has been disabled in favor of this collection. The local open-design app also runs at http://localhost:7456 (Docker).
