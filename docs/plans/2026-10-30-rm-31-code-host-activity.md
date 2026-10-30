# RM-31 — Code-host activity: pull requests, issues and READMEs

> **For agentic workers:** REQUIRED SUB-SKILL: Use team-driven-development to implement this plan task-by-task.

The ticket is the spec: "#### RM-31" in `docs/specs/2026-10-23-roadmap-completion-design.md`, with
§2.4 (the nine Protocol points), §2.2 (the `doctor_runtime.py` budget) and §4.2 (shared files).
This plan is the task list, plus the rulings the ticket leaves to the implementer.

Base: `second-brain/master` at `934a182`. RM-30 (#96) and RM-34 (#97) merge first and each add a
kind; this branch expects one-line set conflicts in `source_kinds.py`, `ADDED_KINDS`,
`DEMO_EXEMPT_KINDS`, the two `SOURCE_ICONS` tables, `EXTENSION_REGISTRARS` and the skill table.

## Files

| File | Change | Size |
|---|---|---|
| `src/brain/ingest/github.py` | NEW leaf: the `gh` reads. `GithubError`, the `GhRunner` type, the production `gh_runner` (timeout, stdout cap, no stderr in any message), `viewer_login`, `search_items`, `fetch_repo`, `fetch_pr`, `fetch_comments`, `fetch_readme`, and a parser for `gh api --paginate`'s concatenated JSON pages. | < 300 |
| `src/brain/ingest/github_docs.py` | NEW leaf, pure: `GithubItem` / `GithubDoc` value objects, `build_item_document`, `build_readme_document`, the comment filter (owner only unless `with_thread`), `VOLATILE_KEYS`. | < 300 |
| `src/brain/cli_ingest_github.py` | NEW: `brain ingest-github`, through `cli_registry.EXTENSION_REGISTRARS` (RM-06). Scope check, cap, ingest loop, report. | < 400 |
| `src/brain/doctor_github.py` | NEW leaf: the `github` doctor row (`gh` on `PATH`). | < 60 |
| `src/brain/doctor_runtime.py` | +3 lines: the deferred import, the call, a docstring entry. Budget §2.2: 563 + 60 = 623; the file is 575 at base. | +3 |
| `src/brain/cli_registry.py` | one import + one tuple entry. | +2 |
| `src/brain/source_kinds.py` | `github` joins `VALID_SOURCE_KINDS`, and the comment names the front-end tables. | +3 |
| `src/brain/ui/static/js/results.js`, `discovery.js` | a `github` glyph in both `SOURCE_ICONS`. `tree.js` is untouched: no month grouping, which the ticket says is the intended fallback (PRs are not a dated feed). | +1 each |
| `skills/ingest-brain/SKILL.md` | the MCP recipe (path (a)), and a table row for `brain ingest-github` (path (b)). | — |
| docs | `docs/cli-reference.md` (new command), `CHANGELOG.md` [Unreleased], Appendix B RM-31 DONE, backlog row 2.1 SHIPPED. No new knob, so `docs/configuration.md` gets no change; the doctor row is listed in `cli-reference.md`'s doctor section if that section lists rows. | — |

No file in the CLAUDE.md ceiling table is touched, so no trail promotion is owed.

## The Protocol (§2.4), as built

1. **Path.** (a) a recipe in `skills/ingest-brain/SKILL.md` for the GitHub MCP connector:
   search the owner's PRs and issues, fetch each, pipe into `brain ingest-stdin --source github`.
   (b) `brain ingest-github`, shelling out to the already-authenticated `gh` (RM-31.D1).
2. **Kind** `github`; `content_type` `github_pr`, `github_issue`, `github_readme`. Glyph `🔀` in
   both `SOURCE_ICONS` tables; `MONTH_GROUPED_PATHS` untouched (intended fallback).
3. **Dedup** `(github, "<owner>/<repo>#<number>")` and `(github, "<owner>/<repo>:README")`,
   through `ingest_document`'s sourced-stdin rule. **Volatile** keys go in metadata only:
   `state`, `draft`, `review_count`, `comment_count`, `labels`, `updated_at`, `closed_at`,
   `merged_at`. A re-run whose body is unchanged applies them with
   `update_document(document_id=…, metadata_patch=…)`, so no re-chunk and no embed (the RM-30
   pattern). A re-run also short-circuits before any per-item fetch when the stored
   `updated_at`, `with_thread` and `repo_private` all equal the new ones, so a repeated run costs
   one search plus one repository read per repository.
4. **Credentials.** `gh auth`'s own; `brain` reads no token, never calls `gh auth token`, never
   passes `-i`/`--include` (headers), and never prints `gh`'s stderr.
5. **Scope.** At least one of `--repo OWNER/REPO` (repeatable) or `--mine`, **and** `--since
   YYYY-MM-DD`. Otherwise exit 2 with exactly "ingest-github requires --repo or --mine, and
   --since". A malformed `--repo`, a malformed `--since` or a future `--since` is refused with the
   same message plus a reason. Semantics (**revised by the #100 review ruling**): every search is
   the owner's activity (author, assignee or reviewer for PRs; author or assignee for issues).
   `--mine` reads it everywhere; `--repo` narrows it to the named repositories and never reads
   their other contributors' items. (The first cut read every item in a `--repo` repository,
   which went past the goal, "your authored activity".)
6. **PII and sensitivity.** Body: the title line, the description, and the **owner's own**
   comments (issue comments, review bodies, inline review comments); other people's only with
   `--with-thread`. Other people's logins never enter the body by default; the item's author login
   is in `metadata.author`. A repository that the API reports as `private`, or whose `visibility`
   is not `public` (an `internal` enterprise repository), is stored `confidential`, and so is any
   item whose repository could not be read (fail closed). The body carries `Visibility: private`
   for those, so a repository that turns private changes the body and the re-ingest goes through
   the pipeline's escalate-only sensitivity update (the RM-30 precedent). The secret guard runs on
   every body (`ingest_document`, `cfg.secret_guard`); a refusal is per item and the run goes on.
7. **Tests.** Synthetic `gh` JSON from named constants (`example-org/example-repo#1`, synthetic
   logins), a fake runner swapped in with `monkeypatch.setattr` (a test double, rule 13), the
   scope refusal, private → confidential, the DB end-to-end run, the skill-text test, and the
   adversarial no-credential test.
8. **Dependencies.** None. `gh` is a soft dependency: absent, the command exits 1 with an install
   hint, and `brain doctor` has a `github` row.
9. **Cost.** Summaries on (`--no-enrich` skips them) and concepts on, as the ticket says. Cap: 500
   documents per run (items first, newest `updatedAt` first, then READMEs in the budget left),
   reported. Worst case 500 × 8–10 minutes ≈ three days of graph lane, which drains by itself
   (PR #54); the docs say to start with a short `--since`.

## Rulings the ticket leaves open

- **R1. Which READMEs.** Every `--repo` repository, and under `--mine` the repositories in the
  results that the authenticated user owns. A README of a repository the owner merely reviewed in
  is not "the owner's own"; it is not fetched. `--no-readmes` skips them all. A README that cannot
  be read (none exists, or the call fails) is counted and skipped.
- **R2. Dating.** A PR or issue's `metadata.date` is its `createdAt` (stable; promoted to
  `sent_at`). A README carries no date.
- **R3. Merged state.** `gh search` reports only `open`/`closed`; the per-PR `gh api
  repos/O/R/pulls/N` read supplies `merged`, so `state` is `open`, `closed` or `merged`.
- **R4. Pagination.** Comment and review reads use `gh api --paginate` (`per_page=100`). The
  pages arrive as concatenated JSON values, which the leaf parses itself rather than relying on
  `--slurp` (newer `gh` only).
- **R5. The runner.** `subprocess.run` with an explicit timeout (60 s per call), `check=False`,
  an environment that disables prompts and the update notifier, and a stdout cap. Every
  `GithubError` message is written by this module and never quotes `gh`'s stdout or stderr, so the
  CLI may print it; that is the property the adversarial test pins. (RM-30's capped `gws` runner
  is the same shape for another binary; it is not on master yet, and sharing it is a follow-up,
  not this ticket.)
- **R6. The cap's report.** When more than 500 documents are due, the run takes the newest 500
  and prints "stopped at the 500-item cap: … matched; narrow --since or --repo".

## Tasks (TDD: each lands RED first)

1. Kind: `ADDED_KINDS`/`DEMO_EXEMPT_KINDS` (with its reason) tests, then `source_kinds.py` and the
   two glyph tables.
2. `ingest/github.py`: runner (missing binary, non-zero exit, timeout, oversize, stderr never in
   the message), page parser, the search argument shapes for `--mine`/`--repo`, union and sort.
3. `ingest/github_docs.py`: body and metadata for PR/issue/README; owner-only comments;
   `--with-thread`; private → confidential and the `Visibility` line; no third-party login in the
   default body.
4. `cli_ingest_github.py`: scope refusal (AC1), dry run, ingest end to end on 5434 (AC2, AC3),
   re-run no-op and metadata-only state change without re-chunk or embed (AC4), the cap (AC6),
   the secret guard per item, the short-circuit, a per-item failure that does not stop the run,
   and the adversarial test.
5. Doctor row in `doctor_github.py`, composed in `doctor_runtime.py`.
6. Skill recipe and its text test (AC5); docs, CHANGELOG, Appendix B, backlog row, memory.

## Risks

- `gh search`'s JSON fields are taken from `gh search prs --help` (gh 2.92); the fixtures mirror
  them. A field the real `gh` renames would show up as a missing value, never a crash: every read
  is `.get` with a type check.
- Rate limits on a long `--since`: about four API calls per PR; the cap bounds a run at about two
  thousand calls, under the 5,000/hour REST limit.

## Review round 1 (#100)

- **rev-100 HIGH:** `gh search issues` (gh 2.92) has no `isDraft`, and gh refuses an unknown
  `--json` field with exit 1, so every real run failed. The field list is now per search type.
  `tests/github_fixtures.py` pins both gh 2.92 field sets (copied from `--help`), and `FakeGh`
  refuses a `--json` field outside its type's set; `gh api` takes no `--json`.
- **Scope ruling:** `--repo` narrows (above). `FakeGh` applies role flags and `--repo` as the
  code host does, so a dropped role would read another person's item, and a test pins that it
  is not ingested.
- **sec-100 LOW:** `is_repo_name` (`fullmatch`, no `.`/`..` segment); `RecursionError` from deep
  JSON is a `GithubError`; the runner streams stdout and kills `gh` past the cap (stderr is
  discarded unread, stdin is closed); titles go through `github_docs.one_line` (no Unicode `C*`
  character), stored and printed; the secret guard gains `gh[ousr]_[A-Za-z0-9]{36,}`
  (`github_app_token`), regenerated into the pre-commit hook.
- **rev-100 LOW:** the skill's sensitivity sentence names every non-public case, and the recipe
  says which content type an issue takes.
