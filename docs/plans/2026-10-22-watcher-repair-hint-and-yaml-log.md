# Logs name exceptions by class only: plan and proof

> **For agentic workers:** REQUIRED SUB-SKILL: Use team-driven-development to implement this plan task-by-task.

Branch `fix/watcher-repair-hint-and-yaml-log`, base `second-brain/master` @ `2d900e9`
(PR #56 merged).

## Part 1: no `src/brain` log line carries an exception's message

*(This section was re-derived for the PR #58 review and audit round. The first version covered
`src/brain/vault` only, and its scan had blind spots that the review and audit found; see "Round 2"
below.)*

### Problem

Many logger calls passed the caught exception itself, so its MESSAGE went into the log. That text
can quote what the call failed on:

- **A YAML error built from a STRING quotes the line it failed on.** Measured: `parse_frontmatter`
  on `'---\ntitle: a: SECRET-VALUE\n---\n…'` raises a `ScannerError` whose text contains
  `title: a: SECRET-VALUE`. A STREAM error (parsed from a file handle) carries no snippet (audit F3).
- **A psycopg DETAIL quotes row values.**
- **An `EnrichmentError` or `OllamaContextOverflow` can embed the model's reply.**

CLAUDE.md forbids document content in a WARNING log.

### The scan (G1), and what it now sees

`tests/test_log_privacy.py` scans every module in `src/brain`.

- **A log call** is `<receiver>.<level>(...)`. The receiver can be a name or an attribute whose
  label contains "log": `logger`, `_logger`, `self._logger`, `logging`. The level methods include
  `.log`.
- **A call is a violation** when an argument uses a tainted name, other than through
  `describe_failure(...)`, `type(...)`, `isinstance(...)` or a comparison. The message argument
  counts (f-string, `%` and `.format` alike); `exc_info` is excluded.
- **A tainted name** is:
  - the name an `except` binds;
  - a name assigned from a tainted value inside that handler (an alias);
  - a parameter annotated as an exception type (a helper that logs what it is handed).
- **A traceback** (`.exception(...)`, or a truthy `exc_info=`) is a violation unless the site is
  in `_TRACEBACK_ALLOWLIST`.
- **Both allowlists** are keyed by module, enclosing function and message literal, and every entry
  carries its reason.

### The inventory, re-derived at `35689ff` (after the first vault pass)

**Fixed here, class-only through `describe_failure`:**

| Area | Sites |
|---|---|
| `vault/` (first pass) | 16, in `directory.py` ×4, `fence.py` ×3, `link_rewrite.py` ×3, `rename.py` ×4, `sync.py` ×2 |
| `vault/sync_summaries.py` | 2 (the alias form: `msg = f"...({exc})"`, then logged; audit F1). So `vault/` has **18** log calls, not the 16 this plan first said |
| `vault/sync.py`'s `_SyncError` reasons | 2, not log calls: "could not read file" and "malformed frontmatter" carried `{e}`, and the reason reaches `report.errors`, which the watcher prints to its launchd log and `mcp_server.py` returns over the wire (review of `ad67f43`). So `vault/` is **20** sites |
| `graph_rag/` | 5: `communities_summary` (embedding pass), `extract` (Ollama unavailable), `fuse` ×2, `global_` |
| `ingest/__init__.py` | 5: mirror write ×2, auto-summary ×2 (one an `EnrichmentError` that can embed the model reply), post-commit source hook (psycopg DETAIL) |
| `people.py` | 4 (OSError) |
| `log_rotation.py` | 3 (OSError) |
| `cli.py` | 2: auto-ANALYZE (psycopg), embedder construction |
| `brief.py`, `elicit/detectors.py`, `review/scans.py`, `eval/runner.py`, `chat.py`, `gaps.py` | 1 each (the model-reply and context-overflow cases are `brief`, `elicit` and `review`) |

**Kept raw, on the allowlist:** one site, `gaps.py`'s "column missing" WARNING. It logs `missing`,
a key of `_ADDITIVE_COLUMNS`. `_missing_additive_column` returns one of those keys or None, so the
value is content-free by construction.

**Kept as tracebacks, on the allowlist (9):**

- five in `vault/watch.py`:
  - the worker's "worker failed";
  - the handler's "classify failed" and "schedule failed";
  - the timer-start fallback;
  - the test-only `on_event` hook;
- three in `mcp_server.py` (`_wrap_db_error`, `_wrap_embed_error`, `_wrap_graph_backend_error`);
- one in `ui/app.py` (the last-resort handler).

Each is a deliberate design: the local server log gets the full detail for diagnosis, and the
wire or the browser gets the class only. The reason sits beside each allowlist entry.

**Counting note:** the reviewer counted 31 raw sites in 15 modules outside `vault/` and `graph_rag/`.
This scan finds 20 fixed sites in 10 modules there (21 call sites, counting the allowlisted
`gaps.py` one). A second pass listed every level-method call inside an
`except` handler whose receiver the detector does not count, and it found none. So the gap is a
counting difference, not a missed receiver. Terminal output (`typer.echo`) and exception messages
built for a re-raise are not log calls, and G1 does not cover them.

### Rulings

- **`describe_failure` keeps the diagnosis, content-free (review item 3).** It now appends:
  - an `OSError`'s symbolic errno (`OSError[ENOSPC]`; `errno 99999` when unnamed);
  - a YAML error's 1-based `problem_mark` (`ScannerError[2:9]`);
  - a raise-site `log_detail` string.

  The cause gets the same treatment. None of these can carry the text the call failed on.
- **`DirectoryRefreshError.log_detail` (review item 4).** The gws runner raises with
  `log_detail="gws CLI not found on PATH"`, `"exit N"`, `"timed out after 30s"`,
  `"could not run gws"` or `"empty args"`. The message, which can quote gws's stderr, is never logged.
- **`sync_summaries`' `report.errors` is class-only too (audit F1).** It is printed by
  `brain vault sync-summaries` and can land in a daemon log. It keeps the path, and the line:col
  tells the user where to look.
- **`chat.py`'s DEBUG line logs the reply's LENGTH, not the reply.** The reply is document-derived.
- **`eval/runner.py` keeps the eval query text.** It is not exception text, and it is the row's
  identity in a local, gitignored corpus. Only the exception is now class-only.
- **A frontmatter YAML error names the FILE line (review of `ad67f43`).** The YAML block starts
  after the opening `---` fence, so PyYAML's mark was one line short (`[1:9]` for file line 2).
  `parse_frontmatter` now re-raises a `MarkedYAMLError` with its `problem_mark` and `context_mark`
  shifted one line down. That changes the error's own text the same way.
- **Not covered:**
  - ~~a value derived from an exception OUTSIDE its handler (assigned in the handler, logged after
    it);~~ ~~an exception stored on an attribute and logged elsewhere;~~ ~~a nested `def` or
    `lambda` inside a handler, which starts a fresh scope and so drops the taint;~~ **covered since
    RM-28** (rules T1-T3 in `tests/log_privacy_rules.py`; plan
    `docs/plans/2026-10-30-rm-28-log-privacy-guard.md`). Still not covered: an exception put into a
    container and the container logged later, and a tainted value returned from a function;
  - exception text in two terminal error reports outside vault sync. Both are recorded here,
    not changed, by the coordinator's ruling:
    - **`vault/export.py`'s `summary.errors`:** an `OSError`, so path and strerror only. That is
      content-free.
    - **`cli_sensitivity.py`'s `result.errors`:** the `str()` of a `ValueError` from
      `update_document`, printed to the user's own terminal and never to a daemon log or the MCP
      wire. Its messages are read below, and every one is **content-free**:
      - **`update_document` raises four `ValueError`s:**
        - "document not found: <id>";
        - "embedder is required when new_content is provided";
        - "content is empty";
        - "content collides with existing document <id>".
        They carry a document id, or fixed text.
      - **Same-module callees:** an AST walk from `update_document` reaches `_chunk_search_metadata`,
        `_content_hash`, `_embed_chunks`, `_enrich_post_ingest_hook`, `_frontmatter_allows_secrets`,
        `_guard_content`, `_insert_chunks`, `_parse_sent_at`, `_promote_metadata_to_columns`,
        `mirror_is_stale` and `write_vault_mirror`. None of them raises a `ValueError`; the only
        other raise is a `TypeError`. `_parse_sent_at` swallows `fromisoformat`'s `ValueError`,
        which would quote the date string, and returns None.
      - **Cross-module callees:** of the chunker, the secret guard and `sub_tokens`, only
        `ingest/guard.py` raises a `ValueError`, and its text names the configured guard mode.

### Tests (RED first)

| ID | Test | RED |
|---|---|---|
| G1 | no `src/brain` logger call can carry an exception's text | at `2d900e9`, the 16 vault sites; at `35689ff` (the widened scan), the 27 listed above |
| G2 | the scan flags each leak form: bare, nested `str(e)`, f-string, `%`, `.format`, an alias, an attribute receiver, a non-allowlisted `.exception`, `exc_info=True`, `.log(level, …)`, and an exception parameter | passes (it guards the guard) |
| G3 | the scan accepts `describe_failure(e)` (bare, as an attribute, inside an f-string), `type(e).__name__`, `exc_info=False`, and a conditional on `e is not None`; an unbound `e` is ignored; both allowlists are keyed by module | passes |
| Y1 | a malformed `_people.yml` is logged as `ScannerError[...]` without the quoted line | **at `2d900e9`, RED only on the missing class name.** `load_people_yml` parses a FILE HANDLE, and a stream `MarkedYAMLError` carries no snippet, so nothing leaked (audit F3). The fix is kept as defensive |
| Y2 | `rewrite_vault_links` on malformed frontmatter: the same | logged the quoted line |
| Y3 | `rewrite_derived_fences` on a synced note whose file was then broken: the same | logged the quoted line |
| Y4 | `sync_summaries`: the WARNING and `report.errors` are class-only | at `35689ff`, logged the quoted line (the alias form) |
| Y5 | `sync_vault`'s `report.errors` for malformed frontmatter carries `ScannerError[` and not the quoted line | at `ad67f43`, `malformed frontmatter: mapping values are not allowed here … title: a: synthetic-secret…` |
| F1 | `parse_frontmatter`'s YAML error names the file line: `ScannerError[2:9]` for file line 2 | at `ad67f43`, `ScannerError[1:9]` |
| H1 | a missing gws is logged as "gws CLI not found on PATH" | at `35689ff`, a bare `DirectoryRefreshError` |
| H2 | a failing gws is logged with "exit 2" and without its stderr | at `35689ff`, no exit code |
| D1-D7 | `describe_failure`: class only; a cause; `OSError` errno symbols (ENOSPC, EROFS, a subclass, no errno); an unknown errno; YAML line:col; `log_detail`; a cause's detail | at `35689ff`, the detail was missing and `log_detail` did not exist |

**Two existing tests changed to the new contract.** In `tests/test_ingest_pipeline.py`, the
mirror-failure tests asserted that the exception MESSAGE was logged, which is the leak itself. They
now raise `OSError(ENOSPC, …)` and assert `OSError[ENOSPC]` is logged and the message is not.

### Ceiling

- **Net-zero edits:** `ingest/__init__.py`, `graph_rag/extract.py` and `vault/sync.py`. Each row gets
  a descriptive hop, and `sync.py`'s promotes `5e3c0f2` (1,817).
- **`cli.py` grows** by one import line and its docstring record ("Eighth growth", renumbered when
  PR #57's "Seventh growth" merged first). Its row
  promotes `155e61d` (8,802).
- **`people.py` grows** by the import, one wrapped DEBUG line and its docstring record. Its row
  promotes `9c41783` (1,019).
- **Under 800, no row:** the other touched files, including `errors.py`.

## Part 2: dropped

The plan was for the watcher's shutdown WARNING to name `brain graphrag remove <ids>` as the cheap
graph repair. That is dropped from this branch, because PR #57 (`brain graphrag remove`) updates
`graph_lane.LOST_REMOVES_REPAIR` itself, and `watch.py` imports that constant.

This branch does not touch the constant, the WARNING, W4 or `watch.py`. The branch name keeps
"repair-hint" from the original scope.

## Proof

### Round 1 (`35689ff`, `src/brain/vault` only)

**RED at `2d900e9`:** G1, Y1, Y2 and Y3 failed at their targeted assertions. Y1's RED was the
missing class name only (see the tests table).

### Mutation matrix (five clauses), in a `git archive` copy only: 9 of 9 OK

The judge reads only each JUnit failure's `message`, and the failing set must EQUAL the prediction.
The control passed, the negative controls (a wrong text, a wrong set) were both rejected, and the
archive was restored byte-identical.

| Mutant | Edit | Killed by |
|---|---|---|
| Y1 | `load_people_yml`'s YAML WARNING passes `exc` raw | G1, Y1 |
| Y2 | `rewrite_vault_links`' malformed-frontmatter WARNING passes `e` raw | G1, Y2 |
| Y3 | the fence rewrite's malformed-frontmatter WARNING passes `e` raw | G1, Y3 |
| S1 | both `sync.py` id-write-back ERRORs pass `e` raw (applied 2×, as declared) | G1 |
| R1 | `rename.py`'s "writes failed" ERROR passes `exc` raw | G1 |
| D1 | `refresh_calendar`'s WARNING passes `exc` raw | G1 |
| GM1 | the guard loses its `describe_failure` exemption | G1, G3 |
| GM2 | the guard binds no handler names (so it can never fire) | G2 |
| GM3 | the guard only sees a logger named exactly `logger` (missing `_logger`) | G2 |

**What GM2 and GM3 prove:** G1 passing on the fixed tree is not vacuous. A blind scan (GM2) or a
narrowed one (GM3) is caught by G2, while G1 would still pass.

### Suites (run serially on a sibling test DB): 654 passed

That is `test_vault_log_privacy` plus every suite for the five touched modules:

- `tests/derived_links/`, `test_cli_directory`;
- `test_cli_note_rename`, the four `test_rename_*`;
- `test_cli_relink_derived`, `test_mark_confidential_fence_refresh`;
- `test_vault_link_rewrite`, `test_vault_sync_link_rewrite`;
- the other six `test_vault_sync*`, `test_cli_vault_sync`;
- the repo-hygiene and reading-surface guards.

`ruff check` and `mypy src/` are clean.

### Round 2 (the PR #58 review and audit of `35689ff`): repo-wide

**What the review and audit found** in round 1's scan and fix:

- G1 skipped the message argument, so an f-string, `%` or `.format` slipped through.
- It ignored `.exception(` and a truthy `exc_info=`.
- It missed aliases (`msg = f"...{exc}"`, then logged; audit F1's `sync_summaries` shape) and
  attribute receivers (`self._logger`).
- `describe_failure` dropped the operator detail (ENOSPC versus EROFS, the gws cause and exit code).
- The scope stopped at `src/brain/vault`.

All of it is fixed above.

**RED at `35689ff`** (the tests table has the messages):

- G1, as re-scoped, at the 27 sites;
- Y4, H1, H2, D3-D7;
- C1, `tests/test_chat.py::test_a_bad_reply_is_never_logged`. In a `35689ff` archive copy, its DEBUG
  line logged `response='not json synthetic-secret...'`.

**Mutation matrix (five clauses), in a `git archive` copy only: 28 of 28 OK on the first run.**
Every edit applied its declared count. The control passed, the negative controls were rejected,
and the archive was restored byte-identical.

| Group | Mutants | Killed by |
|---|---|---|
| Round 1's vault sites, re-run | Y1, Y2, Y3, S1 (×2), R1, D1 | G1, plus Y1/Y2/Y3 for their own sites |
| The guard: content-free exemption removed | GM1 | G1 and five G3 forms |
| The guard: a handler binds nothing | GM2 | eight G2 forms, and the raw-allowlist keying test |
| The guard: only `logger` counts as a receiver | GM3 | G2 `nested`, `attr-receiver` |
| The message argument skipped | GA1 | G2 `fstring`, `percent`, `format` |
| No alias propagation | GA2 | G2 `alias`, the raw-allowlist keying test |
| Attribute receivers ignored | GA3 | G2 `attr-receiver` |
| Tracebacks never flagged | GA4 | G2 `exception`, `exc-info`, the traceback-allowlist keying test |
| `exc_info=False` counted as a traceback | GA5 | G3 `exc-info-false` |
| Comparisons not exempt | GA6 | G3 `conditional`, G1 (`db_reconnect.py`'s conditional) |
| Exception parameters not tainted | GA7 | G2 helper |
| Allowlists not keyed by module | GA8 | G1 (the `mcp_server`/`ui` tracebacks, the `gaps` raw site), both keying tests |
| `sync_summaries` alias reverted | SM1 | G1, Y4 |
| No errno detail | SM2 | D3 ×3, D4, D7 |
| No YAML line:col | SM3 | D5 |
| No `log_detail` | SM4 | D6, D7, H1, H2 |
| gws: no "not found on PATH" | SM5 | H1 |
| gws: no "exit N" | SM6 | H2 |
| The cause named by class only | SM7 | D7 |
| `chat.py` logs the reply again | SM8 (two edits) | C1 |
| Repo-wide representatives (ingest `EnrichmentError`, review overflow, cli auto-ANALYZE) | SM9-SM11 | G1 |

**Suites (run serially on a sibling test DB):**

- the 59 targeted test files for every touched module, then the vault, watcher and MCP sets again
  after `describe_failure` changed: 1,408 + 418 passed;
- the two `test_ingest_pipeline.py` mirror-failure tests, which fail by design under the new contract
  and now assert class + errno: 60 passed.

`ruff check` and `mypy src/` are clean.

### Round 3 (the PR #58 review of `ad67f43`)

**The MEDIUM finding: the same leak class, through `report.errors`.**

- `sync.py`'s `_sync_one` raised `_SyncError(f"malformed frontmatter: {e}")` on a STRING parse.
- `report.errors` reaches the watcher's launchd log (`cli.py` prints `error: {path}: {reason}`)
  and the MCP wire (`mcp_server.py`).
- Both `_SyncError` reasons, read and malformed frontmatter, now carry `describe_failure(e)`.
- Tests: Y5 (frontmatter) and Y6 (an unreadable file, `PermissionError[EACCES]`).

**The LOW finding: frontmatter line:col was one line short.** This is fixed in `parse_frontmatter`
(Rulings). Test: F1.

**RED at `ad67f43`,** in an archive copy with the current tests copied in: exactly Y5, Y6 and F1
failed, each at its targeted assertion, and the other 26 passed.

**The mutation matrix on the final tree: 31 of 31 OK.**

- **New mutants:** SY1 (the frontmatter reason quotes `e` again, killed by Y5), SY2 (the read reason,
  Y6) and FM1 (the mark not shifted, F1).
- **Two predictions widened:** SM2 (no errno detail) and SM3 (no YAML mark) were also killed by the new
  tests, which postdate their predictions: SM2 by Y6, SM3 by F1 and Y5. These are correct kills, and
  both re-ran OK.
- **The `context_mark` shift has no mutant.** It changes only the error's own text, which is no longer
  logged, so no log line observes it.

**Suites (run serially): 797 passed.** That is the vault sync, frontmatter, rename, link-rewrite,
derived-links, watcher, MCP-server and ingest-pipeline consumers, plus the privacy and hygiene guards.
`ruff check` and `mypy src/` are clean.

