# RM-44 — Social bookmarks from a platform data archive (`brain ingest-bookmarks`)

> **For agentic workers:** REQUIRED SUB-SKILL: Use team-driven-development to implement this plan task-by-task.

Ticket: `docs/specs/2026-10-23-roadmap-completion-design.md`, "#### RM-44". Protocol: §2.4.
Budgets: §2.2. Shared files: §4.2. Base: master `2a4ebba9`.

## Goal

Posts the owner liked or bookmarked on X become searchable, read from the owner's own data-archive
export (path (c)). Kind `social`, content type `social_post`. One document per post. No API, no
login, no network: the command reads two named files under a local folder and nothing else.

## Hard constraints for this ticket (restated for every teammate brief)

- Never open, list or search for a real export. No `~/Downloads`, `~/Documents`, `~/Library`,
  `$HOME` or `/` crawl. Fixtures are synthetic archive trees built in `tmp_path` (or the scratch
  dir for the e2e) from named constants: handles such as `example_author`, invented post ids built
  from fixed instants, and invented text.
- No call to any real service. The command has no network code at all, and a test pins that
  (no `httpx`, `socket` or `urllib.request` import in the new modules).
- Test DB `second_brain_test_rm44` on 5434 only. Never 55432, `~/brain-vault` or `data/postgres`.
  The e2e runs the worktree's interpreter, never the installed `brain`.
- The export is hostile input from the first line of code (threat model below).

## Pinning the field set (the ticket's first task)

Pinned against the archive's published shape, as described by public documentation of the format
(never copied from a real export):

| File | Wrapper | Entry | Fields read | Status |
|---|---|---|---|---|
| `data/like.js` (part 0) and `data/like-part<N>.js` (N ≥ 1) | `window.YTD.like.part<N> = [ … ]` | `{"like": {…}}` | `tweetId` (string, required), `fullText` (string, optional: absent when the post or its account was deleted, made private or blocks archiving), `expandedUrl` (string, optional) | Documented. Baseline. |
| `data/bookmark.js` and `data/bookmark-part<N>.js` | `window.YTD.bookmark.part<N> = [ … ]` | `{"bookmark": {…}}` | the same three fields | **Not confirmed** by any published schema I could find; read only if present (ticket point 1). See D8. |

Every other key in an entry is ignored. Nothing else in the archive is opened: not the account,
DMs, the owner's own posts, media or the HTML viewer. The files are JavaScript; the payload after
the `=` is parsed as JSON and never executed.

## Files

### New (each under 800 lines; estimates in brackets)

| File | Purpose |
|---|---|
| `src/brain/ingest/social_archive.py` [~280] | The module the ticket names: the untrusted-file layer. `open_archive(root, *, limits) -> ArchiveFiles` opens `root` once, then `data` with `dir_fd` + `O_DIRECTORY \| O_NOFOLLOW`, then each named part with `dir_fd` + `O_RDONLY \| O_NOFOLLOW \| O_NONBLOCK`, requires `S_ISREG` and `st_nlink == 1` on the open descriptor, and reads at most `max_file_bytes + 1` bytes (one byte more means the file grew: refused). Parts are probed by name, contiguous from part 0, up to `max_parts`; nothing is listed or walked. `parse_payload(text, *, collection, part) -> Iterator[dict]`: strips a BOM, matches the anchored prefix `window.YTD.<collection>.part<N> =` (name and part must be the expected ones), then reads the array **one element at a time** with `json.JSONDecoder.raw_decode`, and allows only whitespace and an optional `;` after the closing `]`. Entry wrapper check (exactly one key, the collection name, mapping to an object) — any other top-level shape refuses the file ("unknown shape", ticket risk line). `SocialArchiveError(BrainError)` with fixed messages that name the collection and part, never content. `ArchiveLimits` (frozen dataclass) carries the caps; tests pass small ones. |
| `src/brain/ingest/social_posts.py` [~250] | Pure logic, no I/O. `SOURCE_KIND = "social"`, `CONTENT_TYPE = "social_post"`, `PLATFORM = "x"`. `snowflake_time(post_id, *, now) -> datetime \| None` (`(id >> 22) + 1288834974657` ms; `None` for a pre-snowflake id whose offset is under one minute). `post_from_entry(entry, collection) -> SocialPost \| Skip` validates `tweetId` (`^[0-9]{1,19}$`, `< 2**63`, checked before `int()`), the text (string, non-blank, ≤ `MAX_TEXT_CHARS`), and derives the handle from `expandedUrl` (below). `merge_posts(...)` folds duplicate ids across likes, bookmarks and parts into one post (collections unioned; first text; first valid handle). `select_posts(posts, *, since, limit) -> PostBatch` (newest first by encoded date, `--since` filter, cap and remainder, counts). `build_document(post) -> ExtractedDoc`; `volatile_metadata(post)`; `VOLATILE_KEYS = ("collections",)`; `external_id(post) = "x-<id>"`. |
| `src/brain/cli_ingest_bookmarks.py` [~300] | `brain ingest-bookmarks`: scope check, archive read, selection, ingest loop and report. Mirrors `cli_ingest_web.py`: `_build_embedder`, `_build_graph_syncer`, secret notice, `volatile_patch.stored_document` / `refresh_volatile` / `settle`, `one_line` on everything printed, `describe_failure` for caught errors. |
| `src/brain/ingest/sensitivity_rerun.py` [~70] | D7. The escalate-only rule for an explicit `--sensitivity` on an already-stored document, lifted out of `cli_ingest_web._apply_explicit_sensitivity` so both commands share it. Returns an outcome (`raised` / `kept_confidential` / `unchanged`) and the mirror failures; the CLI prints. `cli_ingest_web.py` shrinks and its behaviour is unchanged (its existing tests pin that). |
| `tests/social_archive_fixtures.py` | Builders: `post_id_at(instant, seq=0)` (inverse snowflake), `like_entry(...)`, `bookmark_entry(...)`, `write_archive(root, likes=..., bookmarks=None, parts=1, prefix_style=...)`. Named constants only. |

### Changed

| File | Change |
|---|---|
| `src/brain/cli_registry.py` (151) | One import and one `EXTENSION_REGISTRARS` entry, `("ingest-bookmarks", register_ingest_bookmarks)  # RM-44`. |
| `src/brain/source_kinds.py` | `social` added, with its comment line. |
| `src/brain/graph_rag/extract_policy.py` | `social_post` joins `CONCEPT_SKIP_CONTENT_TYPES`; `CONCEPT_SKIP_POLICY_VERSION` `v4` → `v5`, with its docstring line. |
| `src/brain/cli_ingest_web.py` (469) | Calls the shared `sensitivity_rerun` helper instead of its private copy (net negative). |
| `src/brain/ui/static/js/results.js`, `discovery.js` | `social: "🔖"` in both `SOURCE_ICONS` tables. `tree.js`: no entry (ticket: no month grouping); the fallback is intended. |
| `tests/test_source_kinds_identity.py` | `social` in `ADDED_KINDS` and RM-44 in the ticket comment. |
| `tests/test_demo_corpus.py` | `social` in `DEMO_EXEMPT_KINDS`, with a reason (D11). |
| `tests/test_cli_registry_extensions.py`, `tests/test_extract_policy.py` | The new entry; the new type and the `v5` bump. |
| `skills/ingest-brain/SKILL.md` | One routing row (below). |
| `docs/cli-reference.md`, `CHANGELOG.md` [Unreleased → Added] | Docs (DoD 13). `docs/configuration.md`: no new knob or file, so no change (stated in the PR). |
| Spec Appendix B (`RM-44` → `DONE (#PR)`), `docs/plans/2026-10-23-backlog-reconciliation.md` (roadmap 2.9 row: social bookmarks SHIPPED, voice memos SHIPPED by RM-38, receipts still RM-45 → row `PARTIAL`) | DoD 12. |

**Not touched:** `cli.py`, `cli_ingest.py`, `config.py`, `ingest/__init__.py`, `ingest/dispatch.py`
(D12: this is not a per-extension extractor; `.js` must never be dispatched), `mcp_server.py`,
`doctor_runtime.py` (no external binary, so no probe), `vault/export.py`. No migration. No
over-ceiling file and no near-ceiling budget is touched.

## CLI surface

```
brain ingest-bookmarks --format x-archive ARCHIVE_DIR
    [--since YYYY-MM-DD] [--max N (default 2000)] [--sensitivity normal|confidential]
    [--tag T ...] [--dry-run] [--allow-secrets]
```

- `--format` is required; `x-archive` is the only value. Missing or unknown → exit 2:
  `ingest-bookmarks requires --format x-archive and the archive's top folder (the one holding data/)`.
- `ARCHIVE_DIR` must be a folder holding `data/`. A `.zip` path → exit 2, "unzip the archive and
  pass its top folder" (D1). A folder with no `data/like.js` → exit 1, "no likes file found".
- **Scope (protocol point 5, the ticket's accepted deviation):** the archive is the scope (the
  owner chose what to export), plus `--since` and the `--max` cap (D5). `--since` in the future →
  exit 2.
- Report: `found N posts (L liked, B bookmarked)`, then the skip lines that are non-zero
  (`no text`, `malformed`, `undated (excluded by --since)`, `before --since`, `over --max`),
  `bookmarks file not present` when absent (not an error, AC5), and the summary line
  `I ingested, U updated, R refreshed, K unchanged, F failed` + `rare_outcomes`.
- `--dry-run` prints `  <date>  @handle  <one_line(text)[:80]>` per selected post and writes
  nothing (no DB connection is opened).
- No `--enrich` flag: posts are never summarized (D6).

## Document model, dedup and re-runs

- **Dedup:** `(social, "x-<post id>")` (D2; ticket point 3 says `(social, <post id>)` — the `x-`
  namespace keeps one kind safe for a second platform, and avoids BL-36's separator problem in the
  mirror filename). Re-run of the same archive: every post's row exists, so only volatile metadata
  is compared (`refresh_volatile`): no re-chunk, no embed → AC2's no-op.
- **Title:** `@handle: <first 60 chars>` or `<first 80 chars>` of `one_line(text)`. Never depends
  on the collection, so liking → bookmarking does not change the body.
- **Body:** `@handle` line (when known), the post text, then the post URL. Stable across runs.
- **Metadata:** `platform: "x"`, `post_id`, `url`, `handle` (only when known), `date` (ISO 8601
  UTC from the id → `documents.sent_at` through `_parse_sent_at`), `collections` (sorted list of
  `like` / `bookmark`; volatile; the stored list and this run's are **unioned**, so a later archive
  that lacks the bookmarks file never removes `bookmark`, and an identical re-run is a no-op).
- **Post URL (D3):** constructed, never copied: `https://x.com/<handle>/status/<id>`, or
  `https://x.com/i/web/status/<id>` without a handle. The handle comes from `expandedUrl` only
  when `web_url.normalize_url(expandedUrl)` (reused: drops query, fragment, userinfo, `;` params)
  has host `x.com`, `twitter.com`, `mobile.twitter.com` or `www.` of either, and the path is
  exactly `/<handle>/status/<id>` with that entry's id and a handle matching `^[A-Za-z0-9_]{1,15}$`
  (and not `i`). So no query string can be stored (AC3), and an archive cannot plant a
  `javascript:` or off-platform link in the UI.
- **Missing handle:** allowed (AC3): no `handle` key, no `@` line, the `i/web` URL.
- **Date (AC4):** from the id. `--since` therefore filters on when the post was **written**, the
  only date the archive carries for a like (D9); the docs say so.
- **No text** (deleted/private post): skipped and counted (D4). An entry with a malformed id is
  skipped and counted as `malformed`; a post id whose date is more than one day in the future is
  `malformed` too (a forged id).
- **Mirror:** `_ingested/social/<date>-x<7 digits>-<slug>.md` through the existing exporter; its
  collision suffix handles the shared leading digits of ids from the same period.

## Sensitivity, secrets and PII

- Default sensitivity `normal` (ticket point 6: third-party public text). `--sensitivity
  confidential` applies to new posts and **raises** stored ones (mirror included); an explicit
  `normal` never lowers a stored confidential post and prints the `brain mark-normal` hint (D7,
  the sec-114 F11 rule).
- Stored about the author: the handle when present, and nothing else. Display names, avatars,
  follower counts and the owner's own account data are never read.
- Every post goes through `ingest_document`'s secret guard; a refusal is per post, counted, with
  the guard's masked message; `--allow-secrets` as elsewhere.
- Nothing logs. Text reaches only the owner's terminal, through `one_line`. Errors are fixed
  messages or a class name (`describe_failure`), so neither log-privacy test needs an allowlist
  entry.
- Links inside the post text (shortened `t.co` links) are the post's own words and stay as text;
  they are not normalized or followed.

## Cost (protocol point 9)

`social_post` skips concept extraction (policy `v5`; one-time re-reconcile of every
`calendar_event`, `screenshot`, `code` and `web_page` document with an empty extraction and no
model call, as each earlier bump did). No summaries (D6). The people aspect runs as for every
document (no model call). Per-run cap `--max` (2000) plus `--since`. Worst-case drain of one capped
run: 2000 short embeds (minutes on local arctic) and no GraphLane model time.

## Security threat model (the export is hostile)

| Threat | Defence | Test |
|---|---|---|
| Code execution via the `.js` | Never evaluated: anchored prefix regex, then JSON only; anything after `]` other than whitespace and one `;` refuses the file. No `eval`, `exec`, `subprocess` or JS engine; an AST scan test pins it. | payload `[] ; require('x')`; prefix `window.YTD.like.part0 = (function(){})()` |
| Path traversal | No path from the file is ever used. Only fixed names (`data`, `like.js`, `like-part<N>.js`, `bookmark…`) are opened, relative to directory descriptors. | — (no input path is joined); a test that a `..` in `expandedUrl` cannot affect anything |
| Symlinks | `data` opened `O_NOFOLLOW \| O_DIRECTORY` under the root's fd; each file `O_NOFOLLOW`. A symlinked `data/` or `like.js` refuses the run. (The root folder itself is the owner's argument and may be a link.) | symlinked `data`, symlinked `like.js` → `/dev/zero`-like target, symlinked part |
| FIFO / device / folder swapped in | `O_NONBLOCK` + `fstat` `S_ISREG` on the open fd (no TOCTOU between check and read). | a FIFO named `like.js` returns promptly with the refusal |
| Hard link to another file | `st_nlink == 1` required, as RM-35 does. | a hard-linked `like.js` |
| Huge file / file that grows | `fstat` size ≤ `max_file_bytes` (64 MiB) before reading; read `max + 1` bytes; total ≤ `max_read_bytes` (256 MiB) across parts; ≤ `max_parts` (64) per collection. | caps injected small; a file over cap; total over cap; growth simulated by a size cap below the content |
| Huge JSON / element bombs | Element-wise `raw_decode`, so memory is the file text plus one element; `max_entries` (500 000) per run; text over `MAX_TEXT_CHARS` (30 000) skipped as malformed. | entries over cap refuse; a 1 MiB `fullText` is skipped |
| Deep nesting | `RecursionError` (CPython's scanner raises it) and `ValueError` are caught per file and turned into the fixed "malformed" refusal. | `[` × 200 000 inside an entry |
| Huge numbers | `tweetId` must be a digit **string** of ≤ 19 chars before `int()`; a JSON number token is wrong shape. An over-long numeric literal elsewhere hits CPython's int-digit limit → `ValueError` → refusal. | `"tweetId": 1e400`, a 5000-digit literal, a 25-digit id string |
| Wrong encoding / BOM / NUL | Decoded `utf-8` with `errors="strict"` → refusal on invalid bytes (not silently replaced); BOM stripped; control characters never reach the terminal (`one_line`). | invalid UTF-8; a title carrying ESC sequences |
| Forged / pre-snowflake ids | Future dates → malformed; pre-snowflake → undated (excluded by `--since`). | both |
| Terminal injection via handles or text | `one_line` on every printed field; handles regex-validated. | ESC in `fullText` |
| Secret content in posts | The existing secret guard per post. | a synthetic token-shaped string → `refused` |

The zip format is out of scope (D1), so no zip parser is reachable from this command.

## Acceptance criteria → tests

1. Prefix stripped, payload parsed as JSON, never executed → `test_social_archive.py`
   (prefix grammar, trailing-garbage refusal, AST scan for `eval`/`exec`/`subprocess`).
2. One document per post; a re-run is a no-op → `test_cli_ingest_bookmarks.py` (second run:
   `0 ingested … N unchanged`, a counting fake embedder sees zero calls, chunk ids unchanged; a
   post both liked and bookmarked is one document).
3. Query strings stripped; a missing handle allowed → `test_social_posts.py`.
4. A post's date matches its id's timestamp → `test_social_posts.py` at known instants, plus
   `sent_at` checked in the DB test.
5. A missing bookmarks file is not an error → both test files.
6. `--since` honoured → `test_social_posts.py` (boundary day) and the CLI test.

## Test plan (TDD, RED first)

Order: `social_posts` (pure) → `social_archive` (reader, parser) → `sensitivity_rerun` extraction
(web tests stay green unchanged) → CLI + DB → kind wiring, policy, front end, skill, docs. Each
behaviour lands as a failing test first.

- `tests/test_social_posts.py`: snowflake conversion at fixed instants (incl. the epoch, a
  pre-snowflake id, a future id); id validation; handle derivation (each accepted host, query and
  fragment dropped, wrong id in path, `i` handle, over-long handle, `javascript:` URL, off-platform
  host); merge across collections and parts; `select_posts` since/limit/remainder/undated ordering;
  `build_document` body/title/metadata; union of `collections` with stored.
- `tests/test_social_archive.py`: every threat-model row above; part probing (part 0 only; 0–2;
  a gap stops; a part with the wrong `partN` in its prefix refuses); missing bookmarks → empty;
  missing likes → `SocialArchiveError`; wrong wrapper key, non-object entry, non-array payload →
  "unknown shape".
- `tests/test_sensitivity_rerun.py`: raise, keep-confidential, unchanged, mirror failure reported.
- `tests/test_cli_ingest_bookmarks.py` (5434): scope refusals (`--format` missing/unknown, zip
  path, future `--since`) with their exact messages; `--dry-run` writes nothing; AC2/AC5/AC6;
  `--max` remainder line; `--sensitivity` both ways; secret-guard refusal per post; the summary
  line; a refused archive exits 1 without opening a DB connection.
- `tests/test_social_source.py`: the kind in the set and equal to `social_posts.SOURCE_KIND`, the
  glyph in both JS tables, no month grouping, `social_post` in the skip set, the skill row text.
- **Coverage:** 100% on `social_archive.py`, `social_posts.py`, `sensitivity_rerun.py`,
  `cli_ingest_bookmarks.py` (per module `--cov=brain.ingest.social_archive` …).
- **Mutation proofs**, each in a small `git archive` copy (deleted right after its run), never in
  the worktree, with proof the mutation applied; each must turn a named test red: drop the
  trailing-content check; drop `O_NOFOLLOW` on `data`; drop the `S_ISREG` check; drop the
  `nlink` check; drop the size cap; drop the `RecursionError` catch; copy `expandedUrl` instead of
  constructing the URL; drop the id-in-path match; replace the stored-`collections` union with
  overwrite; route stored posts through `ingest_document`; drop the escalate-only guard.
- **Every run includes** `tests/test_log_privacy_rules.py` and `tests/test_log_privacy.py`, plus the
  src-wide scans, each named, never globbed: `test_ceiling_table_check.py`,
  `test_database_url_isolation.py`, `test_compose_no_raw_calls.py`, `test_mcp_tools_seam.py`,
  `test_search_pipeline_boundary.py`, `test_search_fallback_eval_untouched.py`,
  `test_thread_wait.py`, `test_update_document_mirror_ordering.py`, `test_usage_privacy.py`,
  `test_pii_hook_home_path.py`, `test_source_kinds_identity.py`, `test_demo_corpus.py`,
  `test_ci_workflow.py`, `test_import_hygiene.py`; plus the RM-41 files the extraction touches
  (`test_cli_ingest_web.py`, `test_web_source.py`).
- **Protocol for every run:** `df -h /System/Volumes/Data` immediately before (nothing below
  6 GiB); hold `.claude/full-suite.lock` (mkdir + owner file `impl-rm44`; wait with a Monitor
  until-loop; remove only a lock whose owner file is mine); the two exports; a worktree-local uv
  venv with the dev extras. Targeted files only; CI runs the full suite.
- `ruff check` and `mypy src/` clean.

## E2E plan (synthetic only)

In `…/scratchpad/rm44-e2e/`:
1. Build `archive/data/like.js` (two parts), with `tests/social_archive_fixtures.py`: a post with
   a handle and a `?s=20&t=token` `expandedUrl`, one with no handle, one with no text, one older
   than `--since`; and `bookmark.js` with one post that is also liked and one only bookmarked.
2. `BRAIN_HOME=<scratch>/home` with a `.env` pointing `DATABASE_URL` at
   `…:5434/second_brain_test_rm44`, `BRAIN_VAULT_PATH=<scratch>/vault`, `BRAIN_EMBEDDER=none`
   (or the local arctic used by earlier e2e runs); the two exports.
3. Run `<worktree>/.venv/bin/python -m brain …`: no `--format` → exit 2; a `.zip` path → exit 2;
   `--dry-run`; a real run; the same run again (→ all unchanged); delete `bookmark.js` and run
   again (→ unchanged, no error); `brain search` + `brain show` to see the constructed URL with no
   query and the id-derived date; a symlinked `data/` → exit 1.
4. Paste the transcript in the PR; delete the scratch dir.

## Docs and bookkeeping

`docs/cli-reference.md` ("Social bookmarks ingest": the formats, what is read and stored, the
`--since` semantics, "start with `--dry-run`"); `CHANGELOG.md` [Unreleased] → Added;
`skills/ingest-brain/SKILL.md` row:
`| Posts the user liked or bookmarked on X | \`brain ingest-bookmarks --format x-archive <archive-dir>\` | Their own data-archive export, unzipped, top folder (holding \`data/\`). Never look for the archive yourself: ask for the path. \`--since\` filters on when the post was written. \`--dry-run\` first. Nothing is fetched. |`
(then `bin/brain-skills-sync` after merge); spec Appendix B row; backlog roadmap 2.9 row; memory
files `cli.md`, `extractors.md`, `types.md` (rule 11) after merge.

## Overlap

`source_kinds.py`, both `SOURCE_ICONS` tables, `cli_registry.py`, `extract_policy.py`,
`test_extract_policy.py`, `test_source_kinds_identity.py`, `test_demo_corpus.py`,
`test_cli_registry_extensions.py` and `CHANGELOG.md` are shared with any concurrently open source
ticket (RM-45 touches none of the kind files). Whichever merges second does a normal master merge,
never a rebase; the policy version takes the next free number at merge time.

## Decisions

- **D1 — Input form.** (a) Unzipped folder only; a `.zip` path is refused with "unzip it first"
  **(recommended)**: the read is two named files by descriptor, with no archive parser in reach.
  (b) Also accept the `.zip`, by generalizing `notion_archive.ZipExport` + `zip_directory`
  (bounded central directory, in-memory members): more convenience, but it widens RM-35's
  hardened module and the review surface.
- **D2 — External id.** (a) `x-<post id>` **(recommended)**: namespaced for a second platform under
  the one `social` kind (spec RM-44.D1), and no `/` or `:` in the mirror filename (BL-36).
  (b) The bare id, as the ticket's text reads literally.
- **D3 — Stored URL.** (a) Constructed from the validated id and handle **(recommended)**: a query
  string or a hostile link cannot be stored. (b) `normalize_url(expandedUrl)` as stored: keeps the
  archive's own link, but trusts its host.
- **D4 — Posts without text.** (a) Skip and count **(recommended)**: nothing to search. (b) Store a
  placeholder body with the URL.
- **D5 — Run cap.** (a) `--max` default 2000, newest first, remainder reported **(recommended)**,
  consistent with `ingest-web`'s `--max`. (b) No cap beyond `--since` (the ticket's only named
  cap); a first run of a large archive would embed every like.
- **D6 — Summaries.** (a) Never: no enricher is built and no flag is offered **(recommended)**:
  posts are short, and a rare long post's summary is not worth an Ollama call. (b) `--enrich`,
  gated by `enrich_min_tokens` as elsewhere.
- **D7 — `--sensitivity` on stored posts.** (a) Lift `cli_ingest_web`'s escalate-only helper into
  `ingest/sensitivity_rerun.py` and use it from both commands **(recommended)**: one rule, and
  `cli_ingest_web.py` shrinks. (b) Copy it (duplication). (c) New posts only (a silent surprise,
  the case sec-114 F11 fixed).
- **D8 — The bookmarks file.** (a) Accept `bookmark.js` / `window.YTD.bookmark.part<N>` /
  `{"bookmark": {tweetId, …}}`, read only if present, and refuse loudly on any other shape
  **(recommended)**: matches the ticket; an unexpected real shape fails visibly rather than
  silently. (b) Likes only, until the bookmarks shape is confirmed from published documentation.
- **D9 — `--since` meaning.** The post's written date, from its id (the archive gives no like
  date). **Recommended as the only option**; documented in the CLI reference and the skill row.
- **D10 — Multi-part files.** (a) `like.js` is part 0, `like-part<N>.js` part N, probed
  contiguously; a prefix naming another part refuses the file **(recommended)**. (b) List `data/`
  and glob — a walk over hostile names for no gain.
- **D11 — Demo corpus.** (a) Exempt `social` in `DEMO_EXEMPT_KINDS` **(recommended)**: the
  manifest seeds bodies, not archives, so a demo row would exercise neither the prefix parser nor
  the reader; the 22-document pin stays. (b) Seed one synthetic post.
- **D12 — Extractor registry.** (a) Not registered in `ingest/dispatch.py` **(recommended)**: the
  ticket's command is the entry point, and `.js` files must never be picked up by `ingest` or
  `ingest-dir`. (b) Register with `in_directory_walks=False`.
- **D13 — Handle in the body.** (a) Yes, an `@handle` line when known **(recommended)**: searchable
  by author, which is the reason to store it. (b) Metadata only.

## Approval conditions (team-lead, 2026-10-31)

D1–D13 accepted as recommended. Three conditions:

- **C1, scope check.** The ticket names one format only: `brain ingest-bookmarks --format x-archive
  <archive-dir>` (scope in), with browser bookmarks (RM-41) and any platform API scoped out. RM-44.D1
  says one kind serves any platform, but the ticket names no second service or format, so nothing is
  deferred. `x-archive` alone meets all six acceptance criteria; the mapping is in "Acceptance
  criteria → tests" above (AC1 `test_social_archive.py`; AC2, AC5, AC6 `test_cli_ingest_bookmarks.py`;
  AC3, AC4, AC6 `test_social_posts.py`; AC5 also `test_social_archive.py`).
- **C2, sensitivity.** The ticket says default `normal` (protocol point 6). New posts are stored
  `normal` unless `--sensitivity confidential`. A re-run goes through the D7 helper, so it can only
  escalate a stored post, never lower it.
- **C3, D7 regression proof.** `brain ingest-web`'s behaviour does not change. Its existing
  sensitivity tests in `tests/test_cli_ingest_web.py` run unchanged against the extracted helper
  (`git diff` of that file is empty), and the PR says so.
