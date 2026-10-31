# RM-41 — Browser history and bookmarks, allowlist only (`brain ingest-web`)

> **For agentic workers:** REQUIRED SUB-SKILL: Use team-driven-development to implement this plan task-by-task.

Ticket: `docs/specs/2026-10-23-roadmap-completion-design.md`, "#### RM-41". Protocol: §2.4.
Egress: §2.5. Budgets: §2.2. Shared files: §4.2. Base: master `9a903c6`.

## Goal

Pages that the owner revisits or bookmarks become searchable, but only for hosts the owner has
allowlisted. Chrome and Safari are read from their local stores (path (c)). The default is
**ingest nothing**: with no allowlist, the command refuses to run. Kind `web`, content type
`web_page`. One document per normalized URL.

## Hard constraints for this ticket (restated for every teammate brief)

- Never open, copy, read or list real browser data (`~/Library/Safari`,
  `~/Library/Application Support/Google/Chrome`, or any other browser profile), in tests,
  e2e or exploration. Fixtures are synthetic SQLite, JSON and plist files built in `tmp_path` or
  scratch from named constants, using `example.com` and `example.org` URLs only.
- `tests/browser_isolation_plugin.py` (below) points the default profile resolver at a path that
  does not exist for every test, so a test that forgets `--profile` fails with "no history found"
  instead of reading the owner's browser.
- Test DB `second_brain_test_rm41` on 5434 only. Never 55432, `~/brain-vault` or
  `data/postgres`.

## Files

### New (each under 800 lines; estimates in brackets)

| File | Purpose |
|---|---|
| `src/brain/ingest/web_url.py` [~150] | Pure. `normalize_url(raw) -> str \| None`: http/https only. Lowercases the scheme and host and IDNA-encodes the host. Drops the trailing dot, the default port, **userinfo**, **the query string and the fragment**. An empty path becomes `/`. Returns `None` for any other scheme, an unparseable URL, or one over `MAX_URL_CHARS` (2048). Also `host_of(url)`. |
| `src/brain/settings/web_allowlist.py` [~140] | A settings leaf (§2.3). It reads `$BRAIN_HOME/web-allowlist.txt` into a frozen `WebAllowlist(exact: frozenset[str], suffixes: frozenset[str])` with `.allows(host) -> bool`. It ignores `#` comments and blank lines. Each entry is validated (D8). The loader raises `ConfigError` with the **line number only**, never the line's text. A missing file returns `None`; a file with no entries returns an empty allowlist. The CLI refuses both. |
| `src/brain/ingest/browser_store.py` [~300] | Reads the stores. `snapshot(db_path) -> ContextManager[Path]`: makes a private `tempfile.mkdtemp()` (0700), byte-copies the DB and its `-wal` (never `-shm`), and removes the directory in `finally` (D2). It opens the copy with `file:<copy>?mode=ro` (`uri=True`, `timeout=5`) and sets `PRAGMA trusted_schema=OFF` and `query_only=ON`. Readers: `read_chrome(profile, since)` and `read_safari(profile, since)` return `BrowserRows(visits: tuple[VisitRow, ...], bookmarks: tuple[BookmarkRow, ...])`. Fixed `SELECT`s only. Bookmarks come from Chrome's `Bookmarks` JSON (an iterative walk with a depth cap) and Safari's `Bookmarks.plist` (`plistlib`, same walk). Errors: `BrowserStoreError` (base), `HistoryNotFoundError`, `FullDiskAccessError` (a `PermissionError` on Safari's files), `UnsupportedSchemaError` (a missing table or column fails loudly, as the ticket's risk line asks). |
| `src/brain/ingest/browser.py` [~250] | The module the ticket names. `SOURCE_KIND = "web"`, `CONTENT_TYPE = "web_page"`, the volatile keys per browser (D4), and `select_pages(rows, allowlist, *, min_visits, limit) -> PageBatch`. That function normalizes each URL, drops a URL off the allowlist **before** anything else is kept, merges the query-string variants of one page, applies the qualification rule (bookmarked, or at least `min_visits` visits in the window; D5, D13), and sorts by last visit, newest first. It caps at `limit` and counts the remainder. `build_document(page, *, fetched_text=None) -> ExtractedDoc`: body = title, normalized URL and host, plus the fetched text when there is any. Titles pass through `one_line`. |
| `src/brain/ingest/web_fetch.py` [~200] | `--fetch` only (D6). `fetch_page(url, allowlist, *, client) -> str \| None` uses `httpx.Client(follow_redirects=False, trust_env=False, timeout=10.0)` with a fixed User-Agent and no cookies. It follows at most 3 redirects by hand, and **every hop** must be https on an allowlisted host. It streams the body and aborts past 1 MiB (the page keeps its title-and-URL body). It accepts `text/html` and `text/plain` only. HTML becomes text through the existing bounded `ingest/notes_html.html_to_markdown` (D15). Raises `WebFetchError` with fixed, class-only messages. The client is injected, so tests use `httpx.MockTransport`. |
| `src/brain/cli_ingest_web.py` [~320] | `brain ingest-web`: the scope checks, the allowlist load, the read, the selection, the ingest loop and the report. It follows `cli_ingest_apple_notes.py`, reusing `_build_embedder`, `_build_enricher`, `_build_graph_syncer`, the secret and egress notices, and `volatile_patch.stored_document` / `refresh_volatile` / `settle`. Two seams are module attributes: `home_dir` (where the default profile resolves) and `brain_home_root`. |
| `docs/guides/browser-history.md` | The allowlist file format, Full Disk Access for Safari (the exit-1 message points here), what is stored and what is dropped, `--fetch` and its egress, and "start with `--dry-run`". |

### Changed

| File | Change |
|---|---|
| `src/brain/cli_registry.py` (147) | One import and one `EXTENSION_REGISTRARS` entry, `("ingest-web", register_ingest_web)  # RM-41`. |
| `src/brain/source_kinds.py` (78) | `web` added, with its comment line. |
| `src/brain/graph_rag/extract_policy.py` | `web_page` joins `CONCEPT_SKIP_CONTENT_TYPES`. The policy version is bumped and the docstring gets a line (D12). |
| `src/brain/ui/static/js/results.js`, `discovery.js` | `web: "🌐"` in both `SOURCE_ICONS` tables. `tree.js`: no entry, because the ticket says no month grouping and the fallback is intended. |
| `tests/conftest.py` | One `pytest_plugins` line, `"tests.browser_isolation_plugin"`, with a comment, as RM-32 and RM-38 did. conftest.py is a test file; the ceiling table covers `src/brain` only. |
| `tests/test_source_kinds_identity.py` | `web` is added to `ADDED_KINDS` and to the ticket list in its comment. |
| `tests/test_demo_corpus.py` | `web` is added to `DEMO_EXEMPT_KINDS`, with a reason: the demo manifest seeds bodies, not browser stores, so a demo web row would exercise neither the allowlist nor the store reader. |
| `tests/test_cli_registry_extensions.py`, `tests/test_extract_policy.py` | The new entry, the new type and the version bump. |
| `skills/ingest-brain/SKILL.md` | One routing row: `brain ingest-web --browser chrome --since YYYY-MM-DD`. The row says it **refuses without `$BRAIN_HOME/web-allowlist.txt`**, never to create or extend the allowlist for the user, and to run `--dry-run` first. No `sources/web.md`: the command carries its own scope, as RM-36 did. |
| `docs/cli-reference.md`, `docs/configuration.md` (the allowlist file), `CHANGELOG.md` [Unreleased] | Docs (DoD 13). |
| Spec Appendix B (`RM-41` → `DONE (#PR)`), `docs/plans/2026-10-23-backlog-reconciliation.md` (the roadmap 2.5 row → SHIPPED) | DoD 12. |

**Not touched:** `cli.py`, `cli_ingest.py`, `config.py`, `ingest/__init__.py`, `mcp_server.py`,
`doctor_runtime.py` (D16), `ingest/dispatch.py` (D14). No migration: the ticket uses the
existing `sources` and `documents` tables. No over-ceiling file and no near-ceiling budget is
spent.

## CLI surface

```
brain ingest-web --browser chrome|safari --since YYYY-MM-DD
                 [--profile DIR] [--min-visits N=3] [--max N=500]
                 [--fetch] [--refresh] [--sensitivity normal|confidential]
                 [--tag T ...] [--dry-run] [--enrich/--no-enrich] [--allow-secrets]
```

- `--browser` and `--since` are required. Without them the command exits 2 with
  `ingest-web requires --browser chrome|safari and --since YYYY-MM-DD; whole-history import is
  not offered`. A malformed `--since`, or one in the future, also exits 2.
- **Allowlist:** a missing or empty `$BRAIN_HOME/web-allowlist.txt` exits 2 with a message
  naming the file and pointing to the guide. A malformed line exits 2 and gives the line number
  only. All of these checks run before any browser file is touched.
- `--profile DIR` is the directory that holds `History` and `Bookmarks` (Chrome) or `History.db`
  and `Bookmarks.plist` (Safari). It defaults to the macOS location (D9). It is the only way the
  tests and the e2e point the command at a fixture.
- `--min-visits` (min 1) counts the visits inside the `--since` window (D13). `--max` (min 1)
  caps the pages written in one run; the remainder is reported.
- `--fetch` downloads the page text (D6, D7). `--refresh` rebuilds the body of pages that
  already exist (D3). With `--fetch`, it also re-fetches them.
- `--sensitivity` defaults to `normal` (D11). `--dry-run` prints `host  title` lines for the
  pages that would be written, and writes nothing.
- The run ends with a summary line: `N ingested, N refreshed, N unchanged, N skipped, N failed`,
  with the rare outcomes from `volatile_patch.rare_outcomes`.
- Exit 1 cases, each with a fixed message: no history at the profile; Safari without Full Disk
  Access (the message points to `docs/guides/browser-history.md`); an unsupported schema; a
  snapshot that is unreadable twice (the message says to close the browser and retry).

## Allowlist design (D1, D8)

- **File:** `$BRAIN_HOME/web-allowlist.txt`, resolved through `config._brain_home_root`, the
  same way `drop-folders.toml` is.
- **Entries:** one per line. An entry is either an exact host (`docs.example.com`) or a wildcard
  `*.example.com`. The wildcard matches proper subdomains only, never the apex (D1). Entries are
  case-insensitive, IDNA-normalized, and stripped of a trailing dot.
- **Refused entries:** a scheme, a path, a port, whitespace inside the entry, a bare `*`, a `*`
  anywhere except a leading `*.`, a wildcard over a single label (`*.com`), and IP literals (v4
  or v6). The IP rule keeps `--fetch` off loopback and metadata addresses.
- **Default:** no file, or a file with no entries, means **ingest nothing**: the command refuses
  to run. Nothing is ever allowlisted implicitly.
- **Matching:** on the normalized host only. The port and the userinfo are already gone by then.

## Browsers and how their stores are read (D2, D9)

| | Chrome | Safari |
|---|---|---|
| Default profile (macOS) | `~/Library/Application Support/Google/Chrome/Default` | `~/Library/Safari` |
| History | `History`, read from `urls(id, url, title)` joined to `visits(url, visit_time)`, with `visit_time ≥ since`. Time is µs since 1601-01-01 UTC. | `History.db`, read from `history_items(id, url)` joined to `history_visits(history_item, visit_time, title)`. Time is seconds since 2001-01-01 UTC. |
| Bookmarks | `Bookmarks` JSON: `roots.*.children[]`, `type == "url"`, `url`, `name` | `Bookmarks.plist`: `Children[]`, `WebBookmarkType == "WebBookmarkTypeLeaf"`, `URLString`, `URIDictionary.title`, with the Reading List included |
| Permission | none | Full Disk Access. A `PermissionError` maps to exit 1 and the guide. |

- **The live database is never opened by SQLite.** It is byte-copied with its `-wal` into a
  private temporary directory, and the copy is opened read-only. Nothing in the live profile
  directory is created, locked or modified. A test asserts that the directory listing and the
  mtimes of the fixture's "live" files are unchanged after a run, and that no `-shm` appears
  there.
- **Why `mode=ro` and not `immutable=1` (D2).** I measured this on 2026-10-31 with a synthetic
  WAL database in scratch, on SQLite 3.54. A row that was committed but not yet checkpointed is
  **invisible** under `immutable=1` (count 0) and **visible** under `mode=ro` (count 1). The
  browser keeps its most recent visits in the WAL, so `immutable=1` would silently drop exactly
  the newest history. Opening the private copy `mode=ro` lets SQLite rebuild a `-shm` inside our
  temporary directory, never in the profile. A regression test pins this: a fixture row that
  lives only in the WAL must be found.
- **A torn copy.** The browser may write while we copy. A torn WAL tail is discarded by SQLite's
  frame checksums. A torn main file raises `sqlite3.DatabaseError`, so the reader retries the
  snapshot once and then exits 1 ("close the browser and retry").
- **Size bounds.** The reader refuses to copy a store over 1 GiB, or a bookmarks file over
  64 MiB, and exits 1 with a fixed message. It reads at most 200,000 rows per run, then stops
  and reports that it did.

## Document model, dedup, re-runs (D3, D4, D5, D13)

- **One document per normalized URL** (ticket D1). The dedup key is
  `(web, <normalized URL>)` through `ingest_document`'s sourced branch. Query-string variants of
  one page merge into one page, and their visits are summed.
- **Body:** `title\n\n<normalized URL>\n\nhost: <host>`, plus the fetched text under `--fetch`.
  The title comes from the bookmark name if there is one, then from the most recent non-empty
  history title, then from the URL.
- **Metadata:** `host`, `url`, and `date` (first seen in the window, promoted to `sent_at`).
  `date` is set at creation and is never volatile. Each browser gets its own volatile keys:
  `<browser>_visits` (visits in the window), `<browser>_last_visit` (ISO UTC) and
  `<browser>_bookmarked` (D4).
- **Re-runs (D3).** When a page's `(web, url)` row already exists, the run does **not** call
  `ingest_document`. It applies `refresh_volatile` with this browser's volatile keys: a
  metadata-only patch, with no re-chunk, no embed and no re-extract. This also makes the title
  sticky, keeps fetched text through a later run without `--fetch`, and means one browser's run
  never drops another browser's keys. (`ingest_document` replaces `metadata` wholesale on a body
  change, which is what would drop them.) `--refresh` takes the full path instead, and the
  stored metadata of the other browser is merged into the new document's metadata first.
- **New pages** go through `ingest_document` and `volatile_patch.settle`. A `same_body` outcome
  is reported, never patched (BL-34).

## Sensitivity, secrets and PII in URLs (D10, D11)

- **Default `normal`**, as the ticket says. The pages are on hosts the owner chose. Use
  `--sensitivity confidential` to override. A re-ingest escalates only.
- **Dropped before anything is stored, and before the allowlist check:** the query string, the
  fragment, userinfo (`user:pass@`), the default port, and every non-http(s) scheme
  (`file:`, `chrome:`, `about:`, `data:`, `javascript:`, …).
- **Path tokens are a known residual (D10).** Some capability URLs carry the token in the path
  (`/s/<token>`, `/reset/<token>`). The mitigations are the allowlist (the owner opts in per
  host), the existing secret guard (`ingest/guard.py`) on every body, refusal per page (as other
  ingesters do), and a guide warning. No heuristic redaction.
- **Never logged:** URLs, titles and hosts are never logged at any level. Errors are class-only
  (`describe_failure`). Refusal messages never quote an allowlist line or a URL. The only places
  URLs or titles appear are the terminal output the owner asked for (`--dry-run` and the
  per-page lines), and that output goes through `one_line`.
- The body holds no visit counts or times; they live in `metadata`.

## Cost (protocol point 9)

`web_page` skips concept extraction (the people aspect still runs). The summary is skipped unless
`--fetch` is given, so without it the run passes `enrich=False`. A title-and-URL body is under
`enrich_min_tokens` anyway. `--max` defaults to 500. Without `--fetch`, the worst case for one
capped run is 500 short embeds and no graph-lane time. With `--fetch`, the fetches take at most
500 × 10 s ≈ 83 min, plus 500 summaries.

## Security threat model

| # | Threat | Mitigation | Test |
|---|---|---|---|
| T1 | Over-collection of private browsing | The allowlist is mandatory and denies by default. Missing or empty → refuse. The wildcard rules are narrow. The host is checked after normalization and before anything is kept. | AC1, AC3; mutation proof on the `allows()` call |
| T2 | Secrets in URLs | Query, fragment, userinfo and `;` path parameters (F3) are dropped; the secret guard runs on the body; D10 residual | AC2; userinfo and fragment cases; mutation proofs |
| T3 | Locking or corrupting the live browser DB | SQLite never opens the live files. The byte copy lands in a 0700 temp dir that is removed in `finally`. The copy is opened `mode=ro` with `query_only`. | Live-dir unchanged test; temp dir removed even on error |
| T4 | A hostile SQLite file (`--profile` is user-supplied) | Fixed SELECTs, `trusted_schema=OFF`, no extension loading. Every object read must be a real table in `sqlite_master` (no views). A progress-handler deadline (60 s) on every query. Only regular files: `lstat`, `O_NOFOLLOW` open, `fstat` the same inode, then a byte-counted copy (1 GiB). Any `OSError` → one line by class and errno. A row cap; `sqlite3.DatabaseError` → exit 1 (sec-114 F5, F6) | Garbage-file and missing-column tests |
| T5 | A hostile bookmarks JSON or plist | A size cap; an iterative walk with a depth cap, a visited set (shared references and cycles are walked once) and a 100,000-node cap; a non-string field is skipped and counted (sec-114 F2) | Deep-nesting and wrong-type fixtures |
| T6 | Terminal injection via titles | `one_line` on every title and host printed | ESC-in-title fixture |
| T7 | `--fetch` egress and SSRF | https only. Every redirect hop must be allowlisted; at most 3 hops. Every resolved address must pass `public_only`, an explicit, version-independent deny list (loopback, RFC 1918, CGNAT, link-local, ULA, `fec0::/10`, multicast, unspecified, reserved, benchmarking, documentation), with the IPv4 inside v4-mapped, NAT64, 6to4 and Teredo addresses checked too. **A name that resolves to a private address is refused** (A1). The connection is pinned to the validated IP (`Host` + `sni_hostname`), with a fresh client and no keep-alive per fetch (F13). One 10 s wall-clock deadline over every hop, enforced by a joined worker thread (F4). A raw-byte 1 MiB cap and any `Content-Encoding` but identity refused (F1). Only allowlisted text codecs (F7). `trust_env=False`, no cookies. Allowlist entries: ASCII only, no address in any form, no localhost, no single label (F8); public suffixes are a documented residual | AC5; MockTransport and fake-resolver tests for each rule; mutation proofs |
| T8 | Hosted-embedder egress | `normal` rows can reach Voyage when that backend is active; the existing egress notice prints, as with every ingester | Covered by the existing notice |
| T9 | Leaks through logs | Class-only errors; URLs never logged | `test_log_privacy_rules.py`, `test_log_privacy.py` |

Security review: `ecc:security-reviewer` on opus (the ticket requires it, because `--fetch` is
egress), in addition to the rule-14 review-and-audit loop.

## Acceptance criteria → tests

1. No allowlist → exit 2, and the store reader is never called (`test_cli_ingest_web.py`).
2. An allowlisted page with at least 3 visits lands with no query string, fragment or userinfo.
3. A page off the allowlist never becomes a document, even when it is bookmarked or has 100
   visits.
4. A second run with only new visits changes `metadata` only. A counting fake embedder asserts
   zero `embed` calls, and the chunk ids are unchanged.
5. `--fetch` refuses a redirect off the allowlist, and an http hop (`test_web_fetch.py`,
   `httpx.MockTransport`).
6. Safari with a `PermissionError` (an injected opener, or a `chmod 000` fixture copy in
   `tmp_path`) → exit 1 with the guide pointer.

## Test plan (TDD, RED first)

Order: `web_url` → `web_allowlist` → `browser_store` (Chrome, then Safari) → `browser` selection →
`web_fetch` → CLI and DB → front end, policy and docs. Each behaviour gets a failing test first.

- `tests/browser_fixtures.py`: builders for a Chrome `History` (with only the columns that are
  read), Chrome `Bookmarks` JSON, Safari `History.db` and `Bookmarks.plist`. Each builder is
  WAL-capable and built from named constants on `example.com` and `example.org`.
- `tests/test_web_url.py`, `tests/test_web_allowlist.py`: pure logic at **100%**. Each refused
  entry type is its own case.
- `tests/test_browser_store.py`: the WAL-only row is found (the D2 regression); the live dir is
  unchanged; the temp dir is removed; a missing column raises `UnsupportedSchemaError`; the torn
  copy is retried once; the size and row caps; the Safari permission mapping; the Chrome and
  Safari time conversions at known instants.
- `tests/test_browser_pages.py`: the threshold; the window; bookmarks always qualify (D5);
  query variants merge; the off-allowlist drop; the cap and remainder; the title precedence.
- `tests/test_web_fetch.py`: every T7 rule, oversize, a disallowed content type, a timeout
  (raised by the transport), and an assertion that `trust_env` is false.
- `tests/test_cli_ingest_web.py` (5434): AC1–AC6; scope refusals; `--dry-run` writes nothing;
  `--max`; `--sensitivity`; the cross-browser keys survive; `--refresh` keeps the other
  browser's keys; secret-guard refusal per page; the summary line.
- `tests/browser_isolation_plugin.py` + `tests/test_browser_isolation.py`: the default resolver
  points at a nonexistent path per test, and the test proves it.
- `tests/test_web_source.py`: the kind in the set, the glyph in both JS tables, and `web_page` in
  the concept skip set.
- **Coverage:** 100% on the six new modules (`--cov=brain.ingest.web_url` and so on, per module).
- **Mutation proofs**, each in a `git archive` copy and never in the worktree, with proof that
  the mutation applied: remove the `allows()` check; keep the query string; keep userinfo; drop
  the empty-allowlist refusal; skip the per-hop redirect check; accept http in fetch; switch
  `mode=ro` to `immutable=1`; route existing pages through `ingest_document`; drop
  `trust_env=False`. Each must turn a named test red.
- **Every run includes** `tests/test_log_privacy_rules.py` and `tests/test_log_privacy.py`, plus
  these src-wide scans, named and never globbed: `test_ceiling_table_check.py`,
  `test_database_url_isolation.py`, `test_compose_no_raw_calls.py`, `test_mcp_tools_seam.py`,
  `test_search_pipeline_boundary.py`, `test_search_fallback_eval_untouched.py`,
  `test_thread_wait.py`, `test_update_document_mirror_ordering.py`, `test_usage_privacy.py`,
  `test_pii_hook_home_path.py`, `test_source_kinds_identity.py`, `test_demo_corpus.py`,
  `test_ci_workflow.py`.
- **Protocol for every run:** the disk check (≥ 6 GiB), holding `.claude/full-suite.lock`, and
  the shell exports. Targeted files only; CI runs the full suite.
- `ruff check` and `mypy src/` are clean.

## E2E plan (synthetic only)

In the scratch dir `…/scratchpad/rm41-e2e/`:
1. Build `profile/History` (WAL mode, with one row that stays in the WAL) and `profile/Bookmarks`
   with `tests/browser_fixtures.py`, using `example.com` pages (one allowlisted with 4 visits and
   a `?token=…` query, one off-list with 50 visits, one allowlisted bookmark with 0 visits).
2. Set up `BRAIN_HOME=<scratch>/home`: a `.env` pointing `DATABASE_URL` at
   `…:5434/second_brain_test_rm41`, `BRAIN_VAULT_PATH=<scratch>/vault`, the embedder used by
   prior e2e runs (local arctic on Ollama, or `none`), and `web-allowlist.txt` set to
   `example.com`. Export `BRAIN_PROD_DATABASE_URL=postgresql://localhost:1/none` and
   `BRAIN_SKIP_CANARY=1`.
3. Run the **worktree's** interpreter (`<worktree>/.venv/bin/python -m brain …`), never the
   installed `brain`. The runs: without the allowlist → exit 2; `--dry-run`; a real run; a
   second run after appending a visit row (→ refreshed, no new chunks); `brain search` and
   `brain show` on the test DB to see the stripped URL; the Safari fixture with a `chmod 000`
   copy → exit 1.
4. Paste the transcript in the PR. Delete the scratch dir afterwards.

## Docs and bookkeeping

`docs/guides/browser-history.md`; `docs/cli-reference.md` ("Browser history ingest");
`docs/configuration.md` (`web-allowlist.txt`); `CHANGELOG.md` [Unreleased] → Added;
`skills/ingest-brain/SKILL.md` row (then `bin/brain-skills-sync` after merge); spec Appendix B
row; backlog roadmap 2.5 row; memory files `cli.md`, `extractors.md`, `types.md` (rule 11).

## Overlap with PR #113 (RM-43)

Both PRs touch `source_kinds.py`, both `SOURCE_ICONS` tables, `cli_registry.py`,
`extract_policy.py` (+ `test_extract_policy.py`), `test_source_kinds_identity.py`,
`test_demo_corpus.py`, `test_cli_registry_extensions.py` and `CHANGELOG.md`. Whichever merges
second does a union master merge (a normal merge, no rebase). See D12 for the policy version.

## Decisions

| # | Question | Options | Recommendation |
|---|---|---|---|
| D1 | What the allowlist matches | (a) hosts plus `*.domain` (the ticket); (b) URL prefixes as well | **(a)**. `*.example.com` matches subdomains only, so the apex needs its own line. That is explicit, least-surprise behaviour for an opt-in list. Prefixes can come later. |
| D2 | How the snapshot is opened | (a) `immutable=1` (what the brief named); (b) `mode=ro` on the private copy; (c) open the copy read-write, checkpoint, then reopen immutable | **(b)**. Measured: (a) loses every visit still in the WAL. (b) never touches the live DB, which is the invariant behind the brief's wording. (c) is more code for the same result. |
| D3 | Re-run behaviour for a page that already exists | (a) metadata-only always, with `--refresh` to rebuild; (b) rebuild the body every run | **(a)**. It meets AC4 by construction, keeps titles and fetched text sticky, and keeps the other browser's keys. |
| D4 | The same URL in both browsers | (a) one document with per-browser volatile keys; (b) an external id per browser | **(a)**. It follows the ticket's `(web, <normalized URL>)` key, and search shows one hit per page. |
| D5 | Does `--since` bound bookmarks? | (a) bookmarks always qualify on an allowlisted host; (b) by date added | **(a)**. Safari bookmarks carry no reliable date added, and the allowlist plus `--max` already bound the run. |
| D6 | `--fetch` in this PR | (a) yes (AC5); (b) defer it to a follow-up | **(a)**. AC5 needs it. The plan above fixes the egress rules for the security review. |
| D7 | What `--fetch` fetches | (a) new pages only, with `--refresh --fetch` to re-fetch; (b) every qualifying page, every run | **(a)**. A daily run then makes no network calls for known pages. |
| D8 | A malformed allowlist line | (a) refuse the run (exit 2, line number only); (b) skip the line with a warning | **(a)**. It fails closed: a typo must not quietly widen or narrow scope. |
| D9 | Profile location and platform | (a) macOS defaults, Chrome profile `Default`, `--profile` to override, non-macOS requires `--profile`; (b) per-OS default tables | **(a)**. The owner is on macOS, and the ticket's scope is Chrome and Safari. |
| D10 | Tokens in URL paths | (a) allowlist + secret guard + a guide warning; (b) heuristically redact high-entropy path segments; (c) host-only documents | **(a)**. (b) is guesswork that changes dedup keys. (c) defeats per-page documents (ticket D1). |
| D11 | Default sensitivity | (a) `normal` (the ticket); (b) `confidential` | **(a)**, as the ticket decides. Public pages on hosts the owner chose. |
| D12 | The concept-skip policy version | `v3` here; #113 also claims `v3` | Write **`v3`**. If #113 merges first, the union merge makes it `v4`, with both types and both docstring lines. |
| D13 | What `--min-visits` counts | (a) visits inside the `--since` window; (b) lifetime `visit_count` | **(a)**. It follows "revisits", and `--since` keeps its meaning. |
| D14 | A `dispatch.py` entry | (a) none; (b) register `History` | **(a)**. The registry is keyed by file extension and serves file ingests. A browser store is not a file to ingest. |
| D15 | HTML to text for `--fetch` | (a) reuse `notes_html.html_to_markdown` (bounded, stdlib, hardened in #103); (b) a new `html.parser` converter | **(a)** (DRY). It already drops `script`, `style` and `img`. |
| D16 | A `brain doctor` row | (a) none; (b) an allowlist-status row | **(a)**. There is no external binary, the command's own refusal reports a missing allowlist, and it keeps `doctor_runtime.py` (also touched by #113) out of this PR. |


All sixteen decisions were approved as recommended (team-lead, 2026-10-31).

## Amendments at approval (team-lead, 2026-10-31)

- **A1, `--fetch` SSRF.** The host allowlist is not enough on its own. At every hop,
  `web_fetch` resolves the host through an injected `Resolver` and passes every address to an
  injected `AddressPolicy`. The production default refuses loopback, private (RFC 1918 and ULA),
  link-local, multicast, unspecified and reserved addresses, and a hop is refused if **any**
  resolved address is refused. The request then **connects to the validated address**: the URL's
  host is replaced by the IP literal, `Host:` carries the name, and httpcore's `sni_hostname`
  request extension carries the name for SNI and for certificate verification (verified in
  `httpcore/_sync/connection.py`, 1.0.9). There is therefore no second DNS lookup between the
  check and the connect, and DNS rebinding is closed. Tests use `httpx.MockTransport` with a
  fake resolver and never touch the network. One test pins that the default policy refuses
  each private class.
- **A2, `--allow-secrets`.** This is a shared convention. `cli_ingest._ALLOW_SECRETS_HELP` backs
  the flag on `ingest`, `ingest-dir`, `ingest-stdin`, `ingest-gmail`, `ingest-calendar`,
  `ingest-github`, `ingest-apple-notes`, `ingest-notion` and `ingest-folder`, and every one of
  them passes it to `ingest_document`'s secret guard. It is kept, matching
  `ingest-apple-notes` exactly.
- **A3, Full Disk Access.** An EPERM or EACCES on Safari's `History.db`, `Bookmarks.plist` or
  their directory exits 1 with a single fixed line telling the user to grant the terminal Full
  Disk Access, pointing to the guide. There is no traceback, and the message carries neither
  the path nor any file contents.
- **A4, the #113 union.** When team-lead says #113 has merged: a normal master merge (no
  rebase) that takes the union of `source_kinds`, the icon tables, the registrars, `CHANGELOG`
  and `extract_policy`. This ticket's version then becomes `v4`.

## Decisions taken during implementation

- **D17, the source key is the URL's SHA-256, not the URL.** The end-to-end run found that
  `vault/export._short_id` names the mirror file after the first 8 characters of
  `sources.external_id`, stripping only dashes. For a URL that keeps `https://`, so
  `_ingested/web/<date>-https:/-<slug>.md` was written into a directory called `https:`. Dedup is
  unchanged: one key per normalized URL, 1:1. The URL is in `metadata.url` and in
  `sources.metadata.url`. A regression test pins one flat file under `_ingested/web/`.
  `vault/export.py` (near ceiling, budget spent) is not touched.
- **A latent bug found, not fixed here.** GitHub's external ids (`<owner>/<repo>#<n>`, RM-31)
  hit the same `_short_id` rule whenever the owner name is shorter than 8 characters. Fixing
  `_short_id` would move existing GitHub mirror files in the owner's vault, so it was reported
  to team-lead as a separate item.
- **The secret guard's notice quotes the title.** In `warn` and `reject` mode, the shared
  notice prints the document title with the finding masked. It does this for every source, and
  this ticket keeps that convention.
- **D12, resolved at the #113 merge (A4).** RM-43 merged first with `v3` (`code`), so this
  ticket's bump is **`v4`** (`web_page`). The union keeps both types and both docstring lines.

## Security fix round (sec-114, team-lead rulings, 2026-10-31)

| # | Ruling | Where |
|---|---|---|
| F1 HIGH | The body is read with `iter_raw()` and the 1 MiB cap counts raw bytes. Any `Content-Encoding` other than identity or absent is refused, so no decompressor runs. | `web_fetch._read_text` |
| F2 | The bookmark walk skips nodes it has already visited (by `id`), which also breaks cycles, and stops after `MAX_BOOKMARK_NODES` (100,000). The run reports the stop. | `browser_store._walk` |
| F3 | `;` path parameters are dropped from every segment before the allowlist check and the key. | `web_url.normalize_url` |
| F4 | One wall-clock deadline: the whole fetch runs in a worker thread joined for `TIMEOUT_SECONDS`, the deadline is checked before every hop, and each hop's httpx timeout is the time that remains. Measured first: `client.close()` from another thread does NOT interrupt a request blocked on headers, which is why a join is used. | `web_fetch.fetch_page` |
| F5 | `lstat` (regular only), `O_NOFOLLOW \| O_NONBLOCK` open, `fstat` the same inode, then a byte-counted copy. `OSError` → `StoreUnreadableError("(OSError ENOSPC)")`: one line, no path. | `browser_store.snapshot` |
| F6 | `sqlite_master` must list each object as `type='table'`. A progress-handler deadline (`QUERY_TIMEOUT_SECONDS`) aborts the query; that abort is not retried. | `browser_store._require_tables`, `_query` |
| F7 | Only allowlisted text codecs; anything else decodes as UTF-8 with replacement. | `web_fetch._text_codec` |
| F8 | Entries must be ASCII (a punycode hint is given). No address in any form (WHATWG "ends in a number"), no `localhost`, no single label. `allows()` refuses an address host. The public-suffix residual is in the guide. | `settings/web_allowlist.py`, `web_url.is_numeric_host` |
| F9 | `public_only` is an explicit deny list. Embedded IPv4 in v4-mapped, NAT64 (`/96` and `/48`), 6to4 and Teredo is decoded and checked. Note: an embedded *public* address is allowed, which is looser than the 3.11.17 `ipaddress` tables. | `web_fetch.public_only` |
| F10 | `capped_process.sigterm_as_system_exit(also=(SIGHUP,))` wraps the read. The stale-snapshot sweep (3 h) runs at startup through the shared leaf `ingest/stale_tempdirs.py`, which `voice.py` now uses too. | `cli_ingest_web`, `browser_store.sweep_stale_snapshots` |
| F11 | An explicit `--sensitivity confidential` raises stored pages (`set_document_sensitivity` + `propagate_sensitivity_to_vault`). Without the flag nothing changes. **Escalate only:** an explicit `normal` never lowers a confidential page; it prints a `brain mark-normal` hint (the repo-wide rule, flagged to team-lead). | `cli_ingest_web._apply_explicit_sensitivity` |
| F12 | No change (ruling). | — |
| F13 | `CONNECTION_LIMITS` (no keep-alive) and a fresh client per fetch. | `web_fetch.make_client`, `fetch_page` |
| F14 | This table, T2/T4/T5/T7 above, the guide, and the module docstrings. | — |
