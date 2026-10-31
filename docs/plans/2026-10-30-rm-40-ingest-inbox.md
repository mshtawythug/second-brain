# RM-40 — Ingest tab, phase 4: the inbox folder

> **For agentic workers:** REQUIRED SUB-SKILL: Use team-driven-development to implement this plan task-by-task.

Spec: `docs/specs/2026-10-23-roadmap-completion-design.md` §RM-40; Ingest spec §5.5, §7.4, §10.
Base: `second-brain/master` `88260b9` (RM-33 #105, RM-38 #107 merged).

## Goal

Ingest, by **name**, a file already in the inbox folder, from the browser. `GET /api/ingest/inbox`
lists names; `POST /api/ingest/inbox {name}` queues job kind `ingest.inbox`, which uses RM-33's
move-first step, so the file ends in `<inbox>/imported/` and appears in the tree.

## Decisions (as proposed; see "Rulings and deviations" for what was ruled and built)

- **D1 — which drop folder is the inbox.** The first `[[folder]]` entry (file order) with
  `mode = "ingest"`, `kind = "manual"` and `auto = false`. No new TOML key. `auto = false` keeps the
  `--configured` LaunchAgent off it (RM-33 AC6 already filters on `auto`); `kind = "manual"` keeps
  an `auto = false` screenshot or voice folder from becoming browsable. Alternative: an explicit
  `inbox = true` key (a `settings/drop_folders.py` change, which may collide with RM-39).
- **D2 — a single-file entry point in `drop_folder.py`.** The public API is `run(conn, folder, *,
  since, ingest)`, which takes **every** file in the folder; the single-file logic (`_one`,
  `_open_dirs`, `_Mover`) is private. RM-40 needs one named file. Proposed: a small additive public
  `run_one(conn, folder, name, *, ingest, timeout) -> FileOutcome` (~30 lines: mode/kind check, the
  name rule, `_open_dirs`, `_Mover`, `_one`; no change to `run`). **This edits `drop_folder.py`,
  which impl-rm39 owns — asking before touching it.** The alternative (importing the private names
  from a new UI module) couples the UI to internals and is not recommended.
- **D3 — extensions.** The inbox lists and accepts `folder.extensions ∩ UPLOAD_EXTENSIONS`
  (`.md .markdown .txt .pdf .docx`). Images and audio need OCR / whisper and stay out, as RM-13 keeps
  them out of upload. A `kind = "manual"` folder that omits `extensions` gets the image defaults, so
  the inbox entry must list its extensions (documented; `GET` reports an empty list and the guide
  says why). No settings change.
- **D4 — failure lifecycle.** The UI's ingest step never raises `RejectedFileError`: every failure
  (DB, embedder, secret-guard reject, unreadable file, cancel) moves the file back to the inbox,
  since there is no 10-minute retry loop to protect against and the user can see and retry or delete
  it. `empty` stays in `imported/` (RM-33's rule). The job then fails with the mapped `JobError`
  (`ingest_failure`, re-raised from the captured exception) or ends `cancelled`.
- **D5 — request body.** `{name, tags?, sensitivity?, enrich?, title?}`, validated like
  `parse_file`; unknown fields 400. `sensitivity` defaults to the **folder's configured level**
  (`confidential` by default), not `normal`. Never a path.
- **D6 — the listing.** `GET` always answers 200 `{available, reason, extensions, files}` with
  `files: [{name, size}]`, newest first, capped at 200, built from RM-33's public `scan` (lstat; no
  symlinks, dot-files, sub-directories or dataless files; files modified in the last `SETTLE_S`
  hidden). `files` is empty unless ingest is enabled (loopback, not read-only); `reason` is one of
  the existing ingest reasons, `inbox_unconfigured`, or `inbox_misconfigured` (a `ConfigError` from
  the settings loader, which names the entry and key, never a value). Names are returned only if
  `one_line(name) == name`. No absolute path anywhere in a response.
- **D7 — the document.** Source kind `manual`, `source_path` = the `imported/` path (natural
  dedup, as RM-33), title = the original stem (from `run_one`), metadata
  `{"ingested_via": "ui", "original_filename": <name>}`, `graph_syncer=None` (D9), the progress
  embedder, the existing `_ingest` call shape.
- **D8 — where the folders are read.** Per request, `load_drop_folders(ctx.cfg.brain_home)`: the
  file may change while `brain ui` runs, and it is a small TOML read.

## The name rule and the TOCTOU guard (AC1, AC2)

Submit (route, before queueing): `name` is a `str`; 1–255 UTF-8 bytes; no `/`, no NUL, not `.`/`..`,
no leading `.`, `one_line(name) == name`; extension in D3's set. Then resolve-then-compare (the
`vault/paths.py` rule): `(root / name).resolve().parent == root.resolve()`, and `os.lstat` must be
a regular file without the dataless flag. Codes: 400 `invalid_name`, 415 `unsupported_file`,
404 `inbox_file_unknown`, 400 `not_a_regular_file` (symlink, directory, FIFO), 404
`inbox_unconfigured`, 403/503 from `require_ingest`, 429/503 from `queue_errors`.

Job (worker thread): re-runs the same name rule and `lstat` (a swap after submit is refused before
anything moves), then `run_one`, whose open is `O_NOFOLLOW | O_NONBLOCK` relative to the folder's
directory fd plus an `fstat` regular check, and whose move is identity-checked. So a file swapped
between listing and open (symlink, FIFO, other file) is refused at three layers. A file gone by then
is `inbox_file_gone`.

## Files

| File | Change |
|---|---|
| `src/brain/drop_folder.py` | **D2 only, if approved:** `run_one` (+ name check). |
| `src/brain/ui/inbox.py` (new, ~150) | Pure: `find_inbox(cfg) -> DropFolder | None`, `inbox_extensions`, `check_name`, `resolve_inbox_file` (resolve-then-compare + lstat), `list_inbox`. Reads, never mutates. |
| `src/brain/ui/ingest_schemas.py` | `InboxSpec`, `parse_inbox`. |
| `src/brain/ui/ingest_service.py` | `INBOX_KIND = "ingest.inbox"`, `submit_inbox`, `inbox_runner` (the mutation stays in the one permitted module), two `JobError` codes. ~495 → ~580. |
| `src/brain/ui/routes_ingest.py` | `inbox_list` (GET), `inbox_submit` (POST). |
| `src/brain/ui/app.py` | Two `Route`s. |
| `src/brain/ui/static/js/ingest_inbox.js` (new) + `ingest.js` + `index.html` + `css/ingest.css` | A third mode, "Inbox": a list of names with a pick button each, refreshed on show; text via `textContent`. |
| Tests | `tests/test_ui_inbox.py` (pure name rule + resolve, symlink/dir/FIFO/swap via `tmp_path`); `tests/test_ui_ingest_inbox.py` (routes on 5434, one per refusal; AC2 swap after listing and after submit; AC4 file in `imported/` and in `/api/tree`); `tests/test_drop_folder.py` rows for `run_one`; `tests/test_cli_ingest_folder.py` row: `--configured` leaves the inbox untouched (AC3); `tests/test_ui_browser_ingest_inbox.py` (list + pick, hermetic) and its `ci.yml` roster entry. |
| Docs | Ingest guide / `docs/guides/drop-folders.md` (the inbox entry), `docs/cli-reference.md` (`brain ui` endpoints), CHANGELOG, Appendix B, backlog; memory `cli.md`, `types.md` ("verify merged"). |

Ceiling: every new file under 400; touched files stay under 800; no ceiling-table row changes.

## Verification

`ruff check && mypy src/`; the named test files under the full-suite lock; 100% line+branch on
new/touched Python; mutation proofs (in `git archive` copies, `PYTHONPATH` pinned, batches ≤5) of:
the `/` check, the `..` check, the resolve-then-compare, the `lstat` symlink refusal, the job-side
re-validation; the browser harness for the Ingest tab only (≥6 GiB free);
`python scripts/check_ceiling_table.py`. Security review with `ecc:security-reviewer` on opus
follows (new traversal surface).

## Rulings and deviations

**Rulings (team-lead, 2026-10-30).** D1–D8 approved as proposed, with these notes:

- **D1:** the inbox is the first usable `mode = "ingest"`, `kind = "manual"`, `auto = false`
  entry; no new key. Documented in `docs/guides/drop-folders.md` and `docs/configuration.md`.
- **D2:** `drop_folder.run_one` is added by this ticket, in its own section after `run`, and it
  reuses `_open_dirs`, `_Mover` and `_one`, with `run` unchanged. RM-39 (impl-rm39) adds
  `_Mover.delete_on_success: bool = False` and sets it only for capture folders. `run_one` refuses
  any mode but `"ingest"` with wording that does not depend on RM-39, and it builds `_Mover`
  with the default, so it never deletes in either merge order. Whichever PR merges second
  resolves the textual conflict.
- **D6, the L5 exemption (revised ruling):** the listing is NOT gated on
  `BRAIN_UI_SERVE_CONFIDENTIAL_TITLES`. That knob defaults off and an inbox defaults to
  confidential, so gating would make the inbox unusable out of the box. It is exempt because:
  (a) it is a prompted view: the tab fetches it only when the user picks Inbox mode or presses
  Refresh, never on load or in the background, and a browser test pins this;
  (b) it is served only on a loopback, writable server, the trust of `serve_confidential_bodies`;
  (c) it names local files that are not yet brain documents. A job's result keeps the lens, so
  `original_filename` is withheld with the title.

**Additions beyond the plan, all approved:**

- (a) the upload size caps apply to inbox files (413 `inbox_file_too_large`);
- (b) `GET` returns `default_sensitivity`, so the tab preselects the folder's level. Otherwise
  the form would send `normal` and override D5;
- (c) `_ingest` is split into `_ingest` and `_ingest_on(conn, ...)`, because `run_one` needs the
  connection;
- (d) `ingest_schemas._optional_title` is shared by `parse_file` and `parse_inbox`.

**Deviations from the text above:**

- **D6's real response shape:** `{available, reason, extensions, files: [{name, size}],
  truncated, default_sensitivity}`. `reason` is an ingest reason, `inbox_unconfigured`,
  `inbox_misconfigured` or `inbox_unreadable`.
- **The name rule:** no Unicode category-C character (control, format, surrogate, private use)
  and not `one_line(name) == name`. `one_line` also collapses whitespace runs, which would refuse
  real names with two spaces.
- **Tests:** `run_one`'s tests are in `tests/test_drop_folder_run_one.py`, not in rows added to
  `tests/test_drop_folder.py`, to keep this diff clear of RM-39's.
- **The full code list.**

  Route:

  | Status | Codes |
  |---|---|
  | 400 | `invalid_name`, `outside_inbox`, `not_a_regular_file`, `hard_linked_file`, `unknown_field`, `invalid_sensitivity`, `invalid_title`, `invalid_tags`, `invalid_enrich` |
  | 404 | `inbox_file_unknown`, `inbox_unconfigured` |
  | 409 | `inbox_misconfigured`, `inbox_unreadable`, `file_not_on_disk` |
  | 413 | `inbox_file_too_large` |
  | 415 | `unsupported_file` |
  | 403 / 503 / 429 | from the existing ingest gates |

  Job: `inbox_file_changed`, `inbox_file_gone`, `inbox_file_too_large`, `file_not_on_disk`,
  `inbox_unreadable`, `inbox_move_failed`, `inbox_unusable`, and the ingest codes.

## Review round 1 (#110: rev-110 and sec-110)

No critical or high findings, and no crafted request reached a file outside the inbox.

- **M1, the cap was bypassable after the check.** `run_one` takes `max_bytes` and
  `expect_identity` (both default `None`, so `run` is unaffected). It checks the OPENED fd's
  `fstat`: one link, no dataless flag, size at most the cap, and the given `(st_dev, st_ino)`.
  The hash then reads at most `max_bytes + 1`, so a growing file is refused rather than read
  whole. The job pins the identity from the SUBMIT: its own re-check must match, and so must the
  opened fd. Before extraction, the moved file's size is checked once more, because a writer
  still holding it could append after the hash. Refusals are `FileTooLargeError`,
  `FileIdentityError` and `DatalessFileError` (subclasses of `OpenRefusedError`), mapped to job
  codes. One residual remains: a writer can still append after that last check, while the
  extractor reads. That window is narrow, the user's own process is the only writer, and the
  server is loopback-only.
- **L1 (= rev F1), a symlink loop gave a 500.** `lstat` now runs before resolve-then-compare, and
  a `RuntimeError` or `OSError` from the resolve is `not_a_regular_file`.
- **rev LOW, `PermissionError` from lstat.** Any lstat `OSError` other than ENOENT is 409
  `inbox_unreadable`, on the route and in the job.
- **L2, blocking work in async handlers.** `find_inbox`, `list_inbox`, `require_inbox` and
  `resolve_inbox_file` run under `run_in_threadpool`. The listing reads at most
  `MAX_SCANNED = 5000` entries and reports `truncated`. It has its own loop rather than
  `drop_folder.scan`, so the LaunchAgent's scan is unchanged.
- **L3, the dataless check was path-based.** It is now also on the opened fd's `fstat`. The
  path check stays as the pre-open guard.
- **L4, hard links.** `st_nlink != 1` is refused at submit (400 `hard_linked_file`), left out
  of the listing, and refused on the opened fd, as defence in depth.
- **L5:** the revised ruling above. The on-demand fetch is pinned by a browser test.
- **rev F2, no ingestible type.** When the inbox is available but takes no uploadable type, the
  tab says so and names the extensions to add, instead of "The inbox is empty".
- **rev F3:** `inbox.INBOX_KIND` is renamed `INBOX_SOURCE_KIND`, so it no longer clashes with
  the job kind.
- **rev F4 (also the §9.5 sandbox finding):** the folder's level is preselected only while Inbox
  mode is still shown, and leaving Inbox mode restores the config default.
- **rev F5:** a 4xx on submit refreshes the list.
- **§9.5 sandbox pass:** clean on the `brain demo` sandbox. Its one finding, the sticky radio,
  is rev F4.
- **Verification (round 1):** `ruff check`, `mypy src/` and `check_ceiling_table` are clean.
  1,304 named tests pass, with 100% line and branch coverage on `drop_folder`, `ui/inbox`,
  `ui/ingest_service`, `ui/ingest_schemas` and `ui/routes_ingest`. The 76 Ingest-tab browser
  rows pass, 15 of them inbox rows. 20 more mutations were killed (5 for F1 and the EPERM
  path, 15 for M1, L2, L3, L4, L5 and F4), 33 across the PR.

## Review round 2 (#110: sec-110b)

sec-110b verified every round-1 fix at `c87cd49` with proofs of concept, and found three LOW
findings, each reachable only by someone who can write the inbox's `imported/`:

1. **`_duplicate` admitted nothing.** When `imported/<stem>-<sha8><ext>` already exists, it is
   now opened with `_Opening.duplicate_admit`: one link, on disk, within the cap, and no
   identity pin, since it is a different file. A hard link to an outside file with the same
   bytes is therefore refused (`FileIdentityError`), and the drop copy is kept.
2. **Its hash was unbounded.** It now goes through `_hash(fd, opening)`, which reads at most
   the cap plus a byte under `run_one`.
3. **The no-hard-link copy fallback read to EOF.** `_Mover.move` takes `max_bytes` and passes
   it to `_copy_exclusive`, which stops at `max_bytes + 1`, removes its partial copy and
   raises `FileTooLargeError`.

`run` keeps `_PLAIN_OPENING`: no admission, no bound, and the hash called exactly as before.
RM-33's and RM-38's tests are unchanged. There are five new tests in
`tests/test_drop_folder_run_one.py`, five mutations were killed, and `drop_folder.py` keeps
100% line and branch coverage.

**rev-110b F1 (LOW), in the same round:** the result payload's read-back (`_read_back`, a DB
read) ran inside the IngestFn after `ingest_document` had committed. A failure there made
`run_one` move a committed file back to the inbox, while its document kept a `source_path` in
`imported/`.

- `_ingest_on` is split into `_write` (the commit) and `_payload` (the reads after it). The
  inbox step returns right after `_write`.
- The job builds the payload after `run_one` returns, on the same connection, through
  `_inbox_payload`. If the read fails, the job still succeeds, with
  `details_unavailable: "<Class>"`, a `details_unavailable` event and `sensitivity: null`, so
  the lens withholds the title (fail-closed). The tab shows "Saved; its details could not be
  read back." and no counts.
- To keep `ingest_service.py` under the ceiling (751), the refusal type and its fixed
  code/message tables moved to the new pure leaf `ui/inbox_errors.py`. The mutating code stays
  in `ingest_service`, per the confinement rule.
- A regression test makes `_read_back` raise, and a browser row covers the result. Four
  mutations were killed.

**CI fix (at `c522a51`):** `tests/test_log_privacy_rules.py` failed on two counts, and had done
so since `59c7d04` (the earlier CI runs were cancelled, and the named set had never included this
repo-wide rule).
- `InboxRefused` is renamed `InboxRefusedError`, the house suffix the rule requires.
- `_InboxStep.__call__`'s `self.error = exc` gets a narrowly keyed `STORE_ALLOWLIST` entry in
  `tests/log_privacy_allowlists.py`, by team-lead's ruling. The exception is kept only to re-raise
  after `run_one`, where `on_error` maps it class-only, and that mapping needs the instance. The
  shape is the same as `ui/jobs.py`'s `job.error = error`.
- Both changes are mutation-checked. Every test that scans `src/` is now part of the named set.
