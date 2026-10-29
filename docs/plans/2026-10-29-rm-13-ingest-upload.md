# RM-13 — Ingest tab, phase 3: file upload

> **For agentic workers:** REQUIRED SUB-SKILL: Use team-driven-development to implement this plan task-by-task.

**Ticket:** RM-13 in `docs/specs/2026-10-23-roadmap-completion-design.md` (wave 3).
**Design:** `docs/specs/2026-10-23-brain-ui-ingest-tab-design.md` (the "Ingest spec"): §5.2, §6 (the upload
rows), §7.2–§7.4, §3.4 item 3, the §9.2 upload rows, the §9.3 Content-Type test, the §9.4 upload rows and the
upload half of the §10 "After" row.
**Builds on:** RM-10 (#73, the job registry and paste backend), RM-11 (#74, the paste front end, merged at
`0819738`) and RM-07 (#69, the extractor registry `ingest/dispatch.py`).
**Branch:** `feat/rm-13-ingest-upload`, from `second-brain/master` at `0819738`.

## Goal

Upload one `.md`, `.markdown`, `.txt`, `.pdf` or `.docx` file per job from the browser. A file whose type or
size is wrong is refused before it is staged; an oversize body is refused on the stream.

## Files

New (each planned well under 400 lines):

| File | Responsibility |
|---|---|
| `src/brain/ui/upload_checks.py` | Pure checks, no filesystem state: `sanitize_filename(header)` (percent-decode, last component, control characters and leading dots stripped, 120-character cap, extension allow-list), `uploadable_extensions()` (`UPLOAD_EXTENSIONS` ∩ `ingest.dispatch.supported_extensions()`), `upload_cap(ext)`, and `sniff(path, ext)` (magic bytes, strict UTF-8 without NUL, the docx central directory and the zip-bomb bounds). |
| `src/brain/ui/upload_staging.py` | `UploadStaging`: the private `0700` staging root (`brain-ui-ingest-<pid>-*`), one `0700` directory per upload id, the streamed write with its byte counter, the 64 MiB budget, the 15-minute expiry, `claim` / `unclaim` / `release` / `discard`, `close()`, and `sweep_stale_roots()`. Writes only under its own temp root: never the corpus and never the vault (see "Mutation confinement" below). |
| `src/brain/ui/routes_upload.py` | `POST /api/ingest/uploads`, `DELETE /api/ingest/uploads/{upload_id}`, `POST /api/ingest/file`. Thin: gate, header, caps, stream, map errors. A sibling of `routes_ingest.py` so that module stays the paste-and-config module. |
| `src/brain/ui/static/js/upload.js` | `uploadFile(file)` and `discardUpload(id)`: a raw `fetch` with `Content-Type: application/octet-stream`, `X-Brain-Filename` (percent-encoded UTF-8) and the token header from `api.getToken()`. It sends the `File` object itself, never a `FileReader` copy. |
| `src/brain/ui/static/js/ingest_file.js` | File mode: the labelled file input, the drop zone (a real `<button>`), the client-side extension and size checks from the config, staging on selection, Remove, and the submit. |
| `tests/test_ui_upload_names.py` | The §7.4 sanitizer table, and the `UPLOAD_EXTENSIONS ⊆ supported_extensions()` subset test. |
| `tests/test_ui_upload_staging.py` | Sniffing per type, the zip-bomb guard, budget, expiry, claim/release, permissions, and the pid-based startup sweep. |
| `tests/test_ui_ingest_upload.py` | The upload routes and the file job end to end against the test DB: each extension round-trips, the stream caps, the staging budget, expiry, `DELETE`, the whitespace-only `.txt` → `empty`, and the staged directory removed on success, failure and cancel. |
| `tests/test_ui_browser_ingest_upload.py` | The §9.4 upload rows, hermetic on the RM-11 harness. |

Touched:

- `ui/ingest_limits.py`: `UPLOAD_EXTENSIONS`, the per-extension caps, `MAX_STAGED_BYTES`, `UPLOAD_TTL`,
  `MAX_FILENAME_CHARS` and the docx bounds.
- `ui/security.py`: the one Content-Type exception (§7.2), a literal method-and-path match.
- `ui/jobs.py`: `submit(..., on_settled=)`, a cleanup callback the registry calls exactly once when the job
  reaches any terminal state, including a queued job cancelled or dropped at shutdown that never ran.
- `ui/ingest_service.py`: `submit_file` and `file_runner`; `_result_payload` generalised from `PasteSpec` to
  the fields both runners share; `original_filename` in the result.
- `ui/ingest_schemas.py`: `parse_file` → `FileSpec` (`upload_id`, `tags`, `sensitivity`, `enrich`, `title?`).
- `ui/routes_jobs.py`: `original_filename` joins `_NAMING_KEYS`, so the fail-closed lens withholds it.
- `ui/routes_ingest.py`: `config.extensions` from `uploadable_extensions()`; `limits` gains the upload caps.
- `ui/context.py` (`uploads: UploadStaging | None`), `ui/server.py` (build it, sweep at startup),
  `ui/app.py` (the three routes; `uploads.close()` after `jobs.stop()` in the lifespan).
- `static/js/api.js`: exports `getToken()` (§8).
- `static/js/ingest.js`: the Paste / File mode switch and the file-mode submit.
- `static/index.html`: the mode switch and the file-mode fields; `static/css/ingest.css`: their styles.
- `tests/test_ui_ingest_security.py`: the enumeration gains the three routes; the §9.3 parametrized
  Content-Type test over **every** mutating route in `create_app`'s table.
- `tests/test_ui_jobs.py`, `tests/test_ui_ingest_routes.py`, `tests/test_ui_static_behaviour.py` (JS order),
  `tests/ui_ingest_harness.py` (the upload routes in `FakeJobs`).
- `.github/workflows/ci.yml`: `tests/test_ui_browser_ingest_upload.py` in the byte-ordered browser roster.
- Docs: the Ingest spec's §3.1 and §7.4 sentences and its §9.1 `test_ui_upload_names.py` row (the ticket's
  amendment), `docs/cli-reference.md` (the `brain ui` section), `README.md`, `CHANGELOG.md` (Unreleased),
  the backlog doc's rows, Appendix B's RM-13 row (follow-up commit, the only roadmap-spec edit).
- Memory: `cli.md` (the upload endpoints) and `types.md` (`UploadStaging`, `StagedUpload`, `FileSpec`,
  `on_settled`), marked "PR #N, verify merged".

No file in the CLAUDE.md ceiling table is touched. `ingest/__init__.py`, `config.py`, `cli.py` and
`mcp_server.py` do not change.

## Design choices the ticket leaves open (resolved here)

1. **The allow-list is the ticket's constant, intersected with the registry at runtime.**
   `UPLOAD_EXTENSIONS = frozenset({".md", ".markdown", ".txt", ".pdf", ".docx"})`. A test asserts it is a
   subset of `ingest.dispatch.supported_extensions()`, and `uploadable_extensions()` also intersects at
   runtime, so `GET /api/ingest/config`'s `extensions` (filled from it, as RM-10 left owed) can never offer an
   extension no extractor handles. After RM-33 registers image extractors, images stay non-uploadable until a
   ticket adds them to the constant on purpose.
2. **The upload's `kind` is the extractor's registry name** (`text`, `markdown`, `pdf`, `docx`), from
   `ExtractorRegistry.spec_for`, so it is never restated.
3. **Cleanup is the registry's job, not only the runner's `finally`.** A queued file job that is cancelled
   (200) or dropped by `stop()` never runs, so a runner-side `finally` would leak its staged file until the
   process exits. `JobRegistry.submit` gains `on_settled: Callable[[], None] | None`, called once, outside
   the lock, after any terminal transition; a raising callback is logged class-only and never kills the
   worker. The registry stays kind-agnostic.
4. **Claimed uploads stay in the budget until their job settles.** `POST /api/ingest/file` claims the
   upload (a second submit, or a `DELETE`, then gets 404 `upload_unknown`), and a claimed upload never
   expires. If `submit` raises (`queue_full`, `shutting_down`) the claim is undone, so the user can retry.
5. **Staging happens on selection.** The client uploads the file as soon as it is picked or dropped, so a
   sniff refusal (415) arrives before the user fills in tags. Remove (and choosing another file) sends
   `DELETE`. The Ingest button submits `POST /api/ingest/file`. An expired upload (404) asks for the file
   again.
6. **Status order on `POST /api/ingest/uploads`:** the ingest gate (403), the filename (400 `bad_filename`,
   then 415 `unsupported_file` for the extension) — both from the header, before a byte is read — then the
   honest `Content-Length` against the extension's cap (413 `upload_too_large`) and against the remaining
   budget (429 `staging_full`), then the stream counter (413), then the sniff (415). A refused body is never
   written; a partial file is deleted.
7. **Logging.** The upload id and the size only. Never the filename (§7.5). Every failure is class-only.
8. **One file per job.** Dropping several files is refused with "Drop one file at a time".

### Mutation confinement

`upload_staging.py` writes files, but only under its own private temp root, and deletes them. That is
not corpus data and not the vault: §6 already classes `DELETE /api/ingest/uploads/{id}` as removing "a
staged file, which is not corpus data". The corpus and the vault are still mutated only by
`notes_service.py` and `ingest_service.py` (§1.3). The module docstring says so.

## Security (all mandatory; §7.2–§7.4)

- **No multipart.** D7: the upload is a raw `application/octet-stream` body, so there is no multipart
  parser and no field or part count to bound. `python-multipart` is not imported. No new dependency.
- **One Content-Type exception**, a literal `(POST, /api/ingest/uploads)` match in `RequestGuardMiddleware`;
  JSON is refused there, octet-stream everywhere else. Token, read-only, Origin and `Sec-Fetch-Site` apply
  unchanged. Proven by a test over every mutating route read from `app.routes`.
- **The loopback gate** (`require_ingest`) on all three routes, before the body is read.
- **Streaming caps:** the `Content-Length` precheck, then a counter over `request.stream()`. A missing or
  lying length is caught by the counter; the partial file is deleted.
- **Filenames:** only the final component, no control characters, no leading dots, at most 120 characters;
  the stored path is `<root>/<server uuid>/<sanitized-stem><ext>` and is asserted to resolve inside the root.
- **Sniffing:** `%PDF-`; `PK\x03\x04` plus `word/document.xml` in the central directory; strict UTF-8 without
  NUL for text and markdown. Zip bombs: the sum of member `file_size` ≤ 200 MiB, every member ratio ≤ 100:1,
  and at most 10,000 members, all from `ZipInfo` without extracting.
- **Temp files:** `mkdtemp` (mode `0700`) per process and per upload; files opened `O_CREAT | O_EXCL` with
  mode `0600`; removed on terminal job state, `DELETE`, expiry, any refusal and server shutdown; stale roots
  swept at startup by dead pid.
- **The result lens:** `original_filename` joins `_NAMING_KEYS`.

## Deviations

- **The coordinator's brief asked for bounded multipart parsing.** The spec forbids multipart (D7, §7.2,
  Q11), so there is none to bound. The brief's underlying concern, a body that cannot exhaust memory, is met
  by streaming the raw body to disk under a counter: at most one read chunk is held in memory.
- **The Ingest spec's §9.3 says the Content-Type test extends `tests/test_ui_security.py`.** That file is at
  704 lines; RM-10 already put the ingest rows in `tests/test_ui_ingest_security.py`, and this ticket follows
  that precedent.
- **A third static module, `ingest_file.js`,** beside the spec's `upload.js` and `ingest.js`: file mode in
  `ingest.js` would take it past ~450 lines and give it two reasons to change.
- **A third route module, `routes_upload.py`,** beside `routes_ingest.py`, for the same reason.
- **The staging root is created lazily and swept from the app lifespan,** not in `build_context`:
  `UiContext` is frozen, and sweeping or creating directories while a context is merely built would
  touch the real temp directory from every test that builds one. A server nobody uploads to leaves
  nothing in the temp directory.
- **`tests/test_ui_static_behaviour.py`'s `[hidden]` cascade guard was blind to scoped selectors.**
  It declined every complex selector, so all of `ingest.css` (`.ingest-pane .x`) was invisible to it:
  the two new `[hidden]` rules could be deleted with the guard still green. It now judges a complex
  selector by its subject compound, assuming the ancestor matches (an over-approximation, which only
  makes it stricter). No existing rule became a false positive, and two mutation entries now pin the
  new rules. The `hidden-toggle-scan-finds-nothing` site count is 19 (was 16: RM-13 adds three).
- **RM-11's keyboard-order browser test changed:** the mode switch is now the first tab stop, and the
  type select (paste only) moved into the paste fields, before the tags.
- **The client ignores a configured extension without a cap** (`ingest_file.offered`), so a config
  from an older server, or a test fixture that overrides `limits`, cannot make the page throw.

## Tests (TDD: red first)

1. `test_ui_upload_names.py`: the sanitizer table (`../../x.pdf`, `..\\x.pdf`, `/etc/passwd.txt`, a NUL byte,
   a 300-character name, percent-encoding, dot-only, an unknown or missing extension, case), and the subset.
2. `test_ui_upload_staging.py`: each sniff, the zip-bomb bounds, the budget, expiry by an injected clock,
   claim/unclaim/release, `0700`/`0600` modes, nothing written outside the root, the sweep.
3. `test_ui_jobs.py`: `on_settled` runs once on every terminal path (succeeded, failed, cancelled while
   running, cancelled while queued, dropped by `stop()`), and a raising callback does not kill the worker.
4. `test_ui_ingest_upload.py`: the routes and the file job against the test DB.
5. `test_ui_ingest_security.py`: the enumeration, the Content-Type matrix, and the loopback / read-only /
   Origin rows on the new routes.
6. `test_ui_ingest_routes.py`: `config.extensions` and the upload limits.
7. `test_ui_browser_ingest_upload.py`: pick a `.md` and ingest it; drop a file; the keyboard path through
   the drop-zone button; a refused type and an oversize file client-side; a 415 from the server; Remove sends
   `DELETE`; an expired upload; both themes.

## Mutation proofs (in `git archive` copies only)

The Content-Type exception widened to a prefix; the exception's method check removed; the loopback gate
removed from the upload route; the stream counter removed; the sanitizer's last-component step removed; the
docx ratio check removed; `on_settled` not called for a queued cancel; `original_filename` dropped from
`_NAMING_KEYS`. Each must turn its test red.

## Results

**Full suite of record** (once, serially, fresh Python 3.11 `.venv` by uv, `GITHUB_ACTIONS=true CI=true`,
sibling DB `second_brain_test_rm13` on 5434, at `9064fef`): **9892 passed, 3 skipped, 312 deselected**
in 23:46; total coverage **95.90%**. New and touched modules: `ingest_service.py`, `jobs.py`,
`routes_ingest.py`, `routes_upload.py`, `security.py` 100%; `upload_checks.py` 98%;
`upload_staging.py` 95%. `ruff check` clean, `mypy src/` clean (246 files),
`scripts/check_ceiling_table.py` agrees (16 files, one row each), and
`grep -c '^| .*no commit through' CLAUDE.md` is still 1. One test was added after the run
(`test_the_sweep_never_removes_its_own_root`, `dabe501`, found by the mutation harness below) and
was run on its own (30 passed in its file).

**Browser:** `tests/test_ui_browser_ingest.py`, `..._lifecycle.py` and `..._upload.py` together,
59 passed (under the shared lock).

**Mutation proofs:** 21 of 21 killed, each in its own `git archive` copy of `HEAD`
(`PYTHONDONTWRITEBYTECODE=1`, `__pycache__` purged, `PYTHONPATH` pinned and `brain` resolved by
realpath into the copy, the anchor found exactly once and the file shown changed, exit 1 required and
exit 0/5 refused, `-m browser` for the browser rows, the copy deleted after each):

| Mutation | Killed by |
|---|---|
| Content-Type exception widened to the `/api/ingest/` prefix | `test_octet_stream_is_415_on_every_route_but_the_upload` |
| Exception's method check dropped | `test_the_exception_is_post_only` |
| Loopback gate removed from the upload route | `test_non_loopback_refuses_every_ingest_mutation_even_with_a_token` |
| Stream counter removed | `test_a_chunked_body_over_the_cap_is_413_mid_stream` |
| `Content-Length` precheck removed | `test_an_honest_length_over_the_cap_is_refused_before_anything_is_written` |
| Budget counter removed | `test_the_budget_is_rechecked_by_the_counter` |
| Sniff skipped | `test_a_sniff_mismatch_is_415_and_leaves_nothing` |
| Sanitizer keeps the whole path | `test_the_sanitizer_table` |
| Sanitizer keeps control characters | `test_the_sanitizer_table` |
| docx ratio check removed | `test_a_docx_whose_member_ratio_exceeds_the_bound_is_refused` |
| docx expanded-total check removed | `test_a_docx_whose_expanded_total_exceeds_the_bound_is_refused` |
| Allow-list replaced by the registry | `test_a_registered_extension_outside_the_allow_list_is_not_uploadable` |
| `on_settled` not run on a queued cancel | `test_on_settled_runs_when_a_queued_job_is_cancelled_and_never_runs` |
| File job's cleanup hook dropped | `test_an_unreadable_file_fails_with_a_fixed_message_and_is_deleted` |
| `unclaim` on a full queue dropped | `test_a_full_queue_is_429_and_the_upload_survives_for_a_retry` |
| `original_filename` out of `_NAMING_KEYS` | `test_a_confidential_upload_withholds_its_filename_from_the_wire` |
| `source_path=None` dropped | `test_each_extension_round_trips_and_the_staged_file_is_deleted` |
| Sweep's own-pid check dropped | `test_the_sweep_never_removes_its_own_root` (**survived** until that test was added) |
| A stale upload answer not discarded (JS) | `test_an_upload_removed_while_in_flight_is_deleted_when_it_lands` |
| Drop-zone button not wired to the input (JS) | `test_the_whole_upload_works_from_the_keyboard` |
| `upload.js` sends JSON (JS) | `test_a_picked_file_is_staged_raw_then_ingested_as_a_file_job` |

Plus the two static-guard entries in `tests/test_ui_static_behaviour.py` (the `.ingest-group` and
`.ingest-staged` `[hidden]` rules deleted), which `test_every_guard_can_fail` runs on every suite.

## End-to-end

A real `brain ui` against a scratch DB on 5434 (`second_brain_test_rm13_e2e`), a scratch `BRAIN_HOME` and a
scratch vault, never the real vault: upload synthetic `.md`, `.txt`, `.pdf` and `.docx` files with `curl`,
then a traversal filename and an oversize file; the transcript goes in the PR.

## Risks

- **A staged file outliving its job** if a callback path is missed. Covered by `on_settled` on every
  terminal transition and by the shutdown `close()`; the startup sweep is the backstop.
- **pdfplumber memory on a 25 MiB PDF** (spec R6). One running job; the cap is a constant.
- **R9, the precedent of a non-JSON route.** The parametrized test makes the exception one reviewed line.

### Transcript (real `brain ui`, scratch DB `second_brain_test_rm13_e2e` on 5434, scratch `BRAIN_HOME`, vault and `TMPDIR`, `BRAIN_EMBEDDER=none`, synthetic files)

```
== config
 enabled True ['.txt', '.md', '.markdown', '.pdf', '.docx'] {'.txt': 5242880, '.md': 5242880, '.markdown': 5242880, '.pdf': 26214400, '.docx': 26214400}
== upload flonk-notes.md
 {"upload_id":"2205…d69c","size":61,"kind":"markdown"} [HTTP 201]
  staged: -rw------- … 61 … flonk-notes.md
  job ingest.file succeeded ingested title='Flonk notes' file='flonk-notes.md' chunks=1 None
  staged dir removed
== upload zibbit.txt            → 201 text;  job succeeded ingested title='zibbit';   staged dir removed
== upload quarkle-paper.pdf     → 201 pdf;   job succeeded ingested title='quarkle-paper'; staged dir removed
== upload zibbit-memo.docx      → 201 docx;  job succeeded ingested title='Sample Heading'; staged dir removed
== same .md again (dedup)       → job succeeded skipped title='Flonk notes'
== traversal filename ..%2F..%2F..%2F..%2Ftmp%2Fescape.txt
 → 201; landed at <TMPDIR>/brain-ui-ingest-<pid>-…/<upload_id>/escape.txt; nothing written to /tmp/escape.txt
== DELETE that staged upload    → 200 {"upload_id":"…","deleted":true}
== absolute-path filename %2Fetc%2Fpasswd.txt → 201 (stored as passwd.txt inside the root)
== oversize .txt (5 MiB + 1, honest length) → 413 upload_too_large
== oversize .txt, chunked (no Content-Length) → 413 upload_too_large
== fake.pdf (sniff)             → 415 unsupported_file "the file is not a PDF"
== .exe                         → 415 unsupported_file
== JSON body to the upload route → 415 unsupported_media_type
== octet-stream to paste        → 415 unsupported_media_type
== cross-origin upload          → 403 origin_mismatch
== documents: 4 rows, every one source_path NULL, metadata ingested_via=ui and original_filename set
== vault mirrors: 4 files under _ingested/manual/
== after shutdown (SIGTERM): no staging root left in TMPDIR (only tiktoken's own cache)
== server log lines naming any filename: 0
```

The scratch DBs (`_rm13_e2e`, `_rm13_mut`) were dropped and the scratch directories deleted afterwards.
