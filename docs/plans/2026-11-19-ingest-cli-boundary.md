# PR-C1: the ingest CLI boundary (0.4.0 QA F1, F2, F3, F4, F7, F10, F12)

Branch `fix/ingest-cli-boundary`, worktree `.claude/worktrees/fix-c1-ingest-boundary`, base
`ef824ed1`. Source of truth: the 0.4.0 manual QA report, lane A rows A01, A03, A04, A07, A18, and
the lane B seed log (F2). Test DB: `fix_c1_test`.

Every finding below is the same defect class: untrusted input, or a dependency that is down,
reaches the user as a Rich traceback (or is silently accepted) instead of one line and a
deliberate exit code. The fix lives at the CLI boundary, in one new leaf module, so the over-800
`cli_ingest.py` shrinks instead of growing.

## 1. Findings, root cause, behaviour change

### F1: an embed timeout prints a full traceback (`ingest`, `ingest-dir`)

- **Root cause.** `ingest_document` embeds before its write transaction
  (`src/brain/ingest/__init__.py:1202`, `_embed_chunks` → `embedder.embed`), and the Ollama
  backend raises `OllamaEmbedError` (`src/brain/embeddings.py:279`, a subclass of
  `brain.errors.EmbedError`). `ingest` (`cli_ingest.py:275-298`) catches only
  `SecretGuardError` / `SensitivityError`; `ingest-dir`'s per-file `except`
  (`cli_ingest.py:409`) is `(ValueError, OSError, psycopg.Error)`. So the error escapes as a
  traceback, and in `ingest-dir` it aborts the walk mid-way.
- **Change, single-document commands (`ingest`, and `ingest-stdin`, which has the same hole).**
  Catch `EmbedError` (the base, so Voyage is covered too) and print one red line on stderr, exit 1:
  `embedding failed for <name> (OllamaEmbedError from ReadTimeout); nothing was stored. Is Ollama
  running? Try: brain doctor`. The cause is printed class-only through
  `brain.errors.describe_failure`, never the message (an Ollama HTTP body or payload repr can
  carry text). "Nothing was stored" is true: embeddings are computed before
  `conn.transaction()` opens (perf-T3, `ingest/__init__.py:1154`).
- **Change, `ingest-dir`. Decision: stop on a *transient* embed failure, continue past a
  permanent one.** `EmbedError.transient` is already set at each raise site (timeout, transport
  error, HTTP 408/429/5xx = transient; anything else is not).
  - **Transient → stop the walk, exit 1.** The embedder is a run-wide dependency, not a property
    of the file. The QA run shows the shape: every file timed out. Continuing would make each
    remaining file wait out the full embed timeout (`_DEFAULT_OLLAMA_TIMEOUT_S = 60`) and then
    fail the same way. A run of 900 files would take 15 hours to fail. Stopping loses nothing:
    the failing file wrote nothing (above), and `ingest-dir` is idempotent (source-path dedup),
    so a re-run skips every file already stored and resumes at the one that failed. Output: the
    per-file `failed:` line, then one red stderr line,
    `ingest-dir: stopped at file k of N, the embedder is not answering (<class>); nothing was
    stored for <name>. Check Ollama (brain doctor), then re-run: files already ingested are
    skipped.` With `--sync`, the prune is **skipped** on a stopped walk, because an aborted run
    should not delete. The planner-stats refresh still runs if anything landed.
  - **Permanent → per-file `failed:` line, and the walk continues** (exit 0, the existing
    `ingest-dir` contract for per-file failures). A permanent error is plausibly file-specific
    (for example a response that does not fit this input), and it fails fast, so continuing costs
    nothing. Stopping on it would also block the walk forever: every re-run would stop at the
    same file.
  - Rejected alternative: BL-39's "three in a row" counter (`ingest/gmail_timeouts.py`). It is
    `GmailTimeoutError`-specific, and for an embedder there is no reason to pay two more
    60-second timeouts to learn what the first one already said.

### F2: a refused DB connection prints a raw psycopg traceback (`ingest-stdin`, and every ingest command)

- **Root cause.** Every ingest command opens `with connect(cfg.database_url) as conn:`
  (`brain.db.connect` → `connect_raw` → `psycopg.connect`, `db.py:40`) with nothing around it.
  The QA logs show the same traceback from `ingest` (A01 retry block), `ingest-stdin` and
  `ingest-gmail` (B seed).
- **Shared helper check (DRY).** No CLI-wide "cannot reach" helper exists. Three neighbours do:
  `ui/server.py:preflight` (raises `BrainError`, its own wording), `cli_ingest_code._ingest_all`
  (its own `database unavailable` line), and `cli_ui._describe_db`. The credential-free DSN
  parser to reuse is `brain.backup.pgtool.dsn_target` → `DsnTarget`, whose docstring already
  states the contract this needs: "Carries no password: this is what error messages print". It
  uses `psycopg.conninfo.conninfo_to_dict`, so it handles URL and key=value DSNs alike.
  `brain.cli` already imports `brain.backup` (through `cli_backup`), so the import costs nothing
  at startup.
- **Change.** A new context manager `connect_or_exit(database_url)` in the new leaf
  `src/brain/cli_ingest_boundary.py` opens through `brain.db.connect_raw` (the same
  pgvector-aware open, the pattern `cli_ingest_code` already uses). A `psycopg.Error` **during
  the connect only** prints one red stderr line and exits 1:
  `cannot reach DATABASE_URL 127.0.0.1:5434 (OperationalError). Is Postgres running? Try: brain doctor`.
  - Never the password, the user name, the full URL or psycopg's message. Only
    `DsnTarget.address` (new property, `host:port`, which `DsnTarget.__str__` then reuses) and
    the exception class.
  - A DSN that `conninfo_to_dict` cannot parse prints `cannot reach DATABASE_URL (unparseable)`.
  - Errors raised inside the `with` body are untouched and propagate exactly as today.
- **Scope.** All `connect` call sites on the ingest surface share the defect and switch to the
  helper. Each switch is an import plus a call rename, net zero lines.
  - `cli_ingest.py`: `ingest`, `ingest-dir`, `ingest-stdin`, `ingest-gmail`, `reembed`.
  - `cli_import_sync.DirSync.dry_run_or_exit` (`ingest-dir --dry-run --sync`).
  - `cli_ingest_bookmarks`, `cli_ingest_web`, `cli_ingest_notion`, `cli_ingest_apple_notes`,
    `cli_ingest_calendar`, `cli_ingest_folder`, `cli_ingest_github` (each a bare
    `with connect(...)`; I will check each site has no surrounding handler before switching it).
  - `cli_ingest_code` already handles this case in one line, with its own `USAGE:` prefix. It is
    left alone so its output does not change.

### F3: bad `--metadata` gives a JSONDecodeError / AttributeError traceback (`ingest-stdin`)

- **Root cause.** `cli_ingest.py:479`: `meta = _json.loads(metadata) if metadata else {}`. Invalid
  JSON raises `JSONDecodeError`. A non-object (`[1,2]`) parses, then `meta.setdefault` /
  `.get` raise `AttributeError`.
- **Change.** `stdin_metadata_or_exit(metadata, date)` in the leaf parses `--metadata`, requires a
  JSON object, and raises `typer.BadParameter(..., param_hint="--metadata")`. That gives the
  Click usage box and **exit 2**, the same shape `--source` already uses in this command. The
  messages are `--metadata is not valid JSON: Expecting property name enclosed in double quotes
  (line 1, column 2)` and `--metadata must be a JSON object, got a list`. The raw value is not
  echoed: the parser's position is enough. Validation moves **before** stdin is read, as
  `--source`'s does, so a typo fails without swallowing the piped transcript.

### F4: `--date 2026-13-45` is accepted with a warning, exit 0 (`ingest-stdin`)

- **Root cause.** `--date` is stored as `metadata.date` unchecked (`cli_ingest.py:481`). The
  pipeline later fails to promote it to `sent_at` and only logs a WARNING
  (`ingest/__init__.py:426`).
- **Change (trivial, so included).** The same `stdin_metadata_or_exit` validates `--date` with
  **the pipeline's own parser**, so the CLI accepts exactly what will promote: RFC 2822 or
  ISO 8601. If the date does not parse, it raises `BadParameter(param_hint="--date")`, exit 2,
  before stdin is read. To reuse that parser without importing a private name across modules,
  `_parse_sent_at` moves verbatim to a new leaf `src/brain/ingest/sent_at.py` as the public
  `parse_sent_at`. `ingest/__init__.py` imports it back under the old private name, so
  `tests/test_email_columns.py` (which imports `brain.ingest._parse_sent_at`) is untouched, and
  the over-800 file shrinks by about 30 lines.
- Not changed: a `date` key *inside* `--metadata`, and the MCP `brain_ingest_stdin` tool's
  `date`. Both stay warn-only. They are recorded as residue (section 5).

### F7: tags stored unnormalized (`ingest`, `ingest-dir`, `ingest-stdin`, `ingest-gmail`)

- **Root cause.** The real insert boundary is `ingest_document`. Its INSERT path passes `tags`
  through raw (`ingest/__init__.py:787` `tags = tags or []` → `_insert_new_document`, the
  `INSERT INTO documents` at :1075). The UPDATE path already normalizes
  (`normalize_tags([*existing, *tags])`, :1392). The four `cli_ingest.py` sites pass
  `tags=list(tag)` (281 / 384 / 522 / 672). The other `ingest-*` commands normalize at their
  CLI layer (`cli_ingest_{code,bookmarks,github,web,notion,apple_notes,calendar}`), which is why
  only these four showed it.
- **Surface grep (done).**
  - `mcp_server.brain_ingest_stdin` (:1664): `sorted({*user_tags, "source-mcp"})`, **raw**. Same
    bypass, fixed by the same change.
  - `brain_note_new` (:2520) writes raw tags into frontmatter, but its DB write goes through
    `vault.sync`, which normalizes (`sync.py:676`). Fine as is.
  - The UI ingest schemas normalize (`ui/ingest_schemas.py:179`). `apply_tags` normalizes.
  - The vault-sync INSERT (`vault/sync.py:1429`) receives already-normalized tags.
- **Change.** Normalize at the boundary every caller shares: `tags = normalize_tags(tags or [])`
  in `ingest_document`. That one line is net zero, its comment on the same line, and
  `normalize_tags` is already imported. It covers the four CLI commands, MCP
  `brain_ingest_stdin`, `cli_ingest_folder`, the UI jobs and `brain demo` with one rule in one
  place. Putting it only at the four CLI sites would leave MCP raw, which is the "every write
  boundary" invariant broken again. The CLI-layer `normalize_tags` calls in the other `ingest-*`
  modules become redundant but stay harmless (the rule is idempotent). They are left as they are
  to keep the diff on-scope.

### F10: `ingest <.xyz/.py>` raises a raw ValueError

- **Root cause.** `cli_extract.extract_or_exit` (`cli_extract.py:28-35`) catches
  `ImageError` / `VoiceError` / `OSError`. `ExtractorRegistry.extract_path` raises
  `ValueError("unsupported file type: .xyz")` (`ingest/dispatch.py:123`), which escapes.
- **Change.** Before extracting, `extract_or_exit` asks the registry, through the public
  `dispatch.spec_for(path)`. With no extractor for the suffix, it prints one red stderr line and
  exits **2** (a usage error: the file is fine, the command is wrong):
  `brain ingest does not read .py files (it reads .txt, .md, .markdown, .pdf, .docx, .png, .jpg,
  .jpeg, .m4a, .mp3, .wav). For source code in a git work tree, use: brain ingest-code <repo-dir>`.
  - The list comes from `supported_extensions()`, so it cannot drift from the registry.
  - The suffix goes through `one_line()` (a hostile file name).
  - The `ingest-code` pointer is always printed. No canonical "code extension" set exists to
    gate it on (`ingest-code` classifies by content, not suffix), and `.xyz` may well be code.
  - The check is on the suffix and not a `ValueError` catch, because the registry also raises
    `ValueError` for a malformed PDF/DOCX. Catching it would mislabel those as "unsupported".
- **Adjacent, same function (F10b).** A malformed PDF/DOCX given to `ingest` hits the same
  uncaught `ValueError` traceback (the registry wraps parser errors as `ValueError`,
  `dispatch.py:131`). `extract_or_exit` will catch `ValueError` after the suffix check, print
  the existing `cannot read <name>: <describe_failure(exc)>` line class-only, and exit 1. That
  is the established RM-33 contract for an unreadable file. One RED test covers it. fix-lead can
  drop this if out of scope.

### F12: `reembed --batch-size 0` / `--limit -1` are silently accepted

- **Root cause.** `cli_ingest.py:787-789`: neither option has a range.
  - `--batch-size 0` turns `iter_chunks_missing_embedding` into a no-op.
  - `--limit -1` prints "would embed -1".
- **Change.** `min=1` on both (`typer.Option(None, "--limit", "-n", min=1, ...)`,
  `typer.Option(32, "--batch-size", min=1, ...)`). Click rejects the value with exit 2 before
  `Config.load()`. `--limit 0` is rejected too, which is consistent with "Max chunks to embed".

## 2. Files

| File | Change | Size |
|---|---|---|
| `src/brain/cli_ingest_boundary.py` | **new leaf**: `connect_or_exit`, `exit_on_refusal`, `embed_failure_stops_walk`, `stdin_metadata_or_exit`, with docstrings and the reasons moved out of `cli_ingest.py` | ~150 |
| `src/brain/cli_ingest.py` (over 800) | the five `connect` → `connect_or_exit`; `ingest` / `ingest-stdin` use `with ... , exit_on_refusal(name):` in place of their two try/except blocks (same output; reasons move to the helper docstring); `ingest-dir`'s `EmbedError` branch + stop flag + prune skip; `stdin_metadata_or_exit` replaces the parse + setdefault, moved before the stdin read; `reembed` `min=1` | **net-negative** (target −10 or more), so it owes no growth record |
| `src/brain/cli_extract.py` | suffix refusal (exit 2) + `ValueError` catch; docstring updated | ~55 |
| `src/brain/ingest/__init__.py` (over 800) | `tags = normalize_tags(tags or [])`; `_parse_sent_at` moved out (re-imported under the old name) | **net-negative** (~−30) |
| `src/brain/ingest/sent_at.py` | **new leaf**: `parse_sent_at`, moved verbatim | ~40 |
| `src/brain/backup/pgtool.py` | `DsnTarget.address` property; `__str__` reuses it | +4 (393 → ~397) |
| `src/brain/cli_import_sync.py` + the 7 `cli_ingest_*` modules | `connect` → `connect_or_exit` | net zero each |
| `docs/ceiling-table.md` | rows `cli_ingest.py` and `ingest/__init__.py` (section 4) | — |
| `CHANGELOG.md` | `### Fixed —` subsection at the assigned anchor | — |
| tests | section 3 | — |

The exit-code contract of the new leaf, stated in its docstring:

| Exit | Meaning |
|---|---|
| 1 | an environment failure (DB unreachable, embedder down, an unreadable file); the existing guard refusals keep their exit 1 |
| 2 | a usage error (bad `--metadata` / `--date`, an unsupported file type, an out-of-range `reembed` option) |

## 3. RED tests (written first; each fails on `ef824ed1` for the stated reason)

**`tests/test_cli_ingest_boundary.py`, marked `nodb`.** These are pure: CliRunner, a fake
embedder, tmp_path vault/home. "Connection refused" uses a synthetic DSN on port 1,
`postgresql://synthetic_user:synthetic-pw-0000@127.0.0.1:1/unreachable_test`, never a real
database.

| Test | Proves | Fails today because |
|---|---|---|
| `test_ingest_stdin_unreachable_db_prints_one_line` (F2, the QA symptom) | exit 1; stderr has `cannot reach DATABASE_URL 127.0.0.1:1`; no `Traceback`, no `OperationalError:` frame text, and neither `synthetic-pw-0000`, `synthetic_user` nor `postgresql://` appear anywhere in the output | raw psycopg traceback, exit 1 |
| `test_ingest_and_ingest_dir_and_reembed_unreachable_db` (F2, parametrized over `ingest <file>`, `ingest-dir <dir>`, `reembed`) | same one line, exit 1, no password | traceback |
| `test_connect_or_exit_unparseable_dsn` (F2) | a malformed DSN prints `(unparseable)` and exit 1, with no traceback | helper is new |
| `test_connect_or_exit_lets_body_errors_through` (F2) | a `psycopg.Error` raised inside the body propagates unchanged and is not relabelled "cannot reach". Uses the fix_c1_test DB, so this one test sits in the DB file below | helper is new |
| `test_ingest_cli_modules_do_not_open_bare_connections` (F2 guard) | AST scan: no `brain/cli_ingest*.py` or `cli_import_sync.py` imports `connect` from `brain.db` (`cli_ingest_code`'s `connect_raw` is allowed, with its own handler). **Mutation-proven** in Phase 2: revert one module's import in a git-archive copy and watch it fail | 8 modules import it today |
| `test_ingest_stdin_metadata_not_json_exits_2` (F3) | `--metadata '{bad json'` → exit 2, message names `--metadata` and "not valid JSON", no traceback; stdin is never read (a stdin that raises on read proves it) | JSONDecodeError traceback, exit 1 |
| `test_ingest_stdin_metadata_not_object_exits_2` (F3, parametrized `[1,2]`, `"s"`, `3`, `null`) | exit 2, "must be a JSON object" | AttributeError traceback |
| `test_ingest_stdin_bad_date_exits_2` (F4, `2026-13-45`, `yesterday`) | exit 2, names `--date`; a valid ISO date and an RFC 2822 date both pass validation | exit 0 with a warning |
| `test_ingest_unsupported_extension_exits_2` (F10, parametrized `.xyz`, `.py`) | exit 2, one line, names the suffix and `brain ingest-code`, no `ValueError`/`Traceback` | ValueError traceback, exit 1 |
| `test_ingest_malformed_pdf_one_line` (F10b) | `bad.pdf` (bytes `not-a-real-pdf`) → exit 1, `cannot read bad.pdf: ValueError from …`, no traceback | traceback |
| `test_reembed_rejects_nonpositive_batch_size_and_limit` (F12, parametrized `--batch-size 0`, `--batch-size -3`, `--limit 0`, `--limit -1`) | exit 2, before any DB or embedder use (a DATABASE_URL on port 1 would otherwise print the F2 line, so seeing "Invalid value" proves the order) | exit 0, "would embed -1" |
| `test_parse_sent_at_moved_unchanged` | `brain.ingest.sent_at.parse_sent_at is brain.ingest._parse_sent_at` | module is new |

**`tests/test_cli_ingest_boundary_db.py`** uses the `test_db` fixture against `fix_c1_test`.
It uses `FakeEmbedder`, a subclass `_TimingOutEmbedder` whose `embed` raises
`OllamaEmbedError("Ollama request failed: timed out", transient=True)`, and a
`_RejectingEmbedder` raising `transient=False`. These are injected through the existing
`brain.cli._build_embedder` patch point with `monkeypatch.setattr`, a standard test double, and
never touch real Ollama.

| Test | Proves | Fails today because |
|---|---|---|
| `test_ingest_embed_timeout_prints_one_line` (F1, the QA symptom) | exit 1; stderr `embedding failed for sample.txt (OllamaEmbedError)`; no `Traceback`; no row in `documents` | Rich traceback |
| `test_ingest_stdin_embed_timeout_prints_one_line` (F1) | same for `ingest-stdin` | traceback |
| `test_ingest_dir_stops_on_transient_embed_failure` (F1) | with 3 files and the embedder failing from the 2nd: exit 1; file 1 ingested, file 2 `failed:`, file 3 **never attempted** (the fake counts calls), the stop line names "2 of 3"; a re-run with a working embedder skips file 1 and ingests 2 and 3 | traceback on file 2, walk aborted with no summary |
| `test_ingest_dir_continues_past_permanent_embed_failure` (F1) | `transient=False` → every file gets `failed:`, all attempted, exit 0 | traceback |
| `test_ingest_dir_sync_skips_prune_when_stopped` (F1) | `--sync --apply` with a stale document whose file is gone, plus a transient failure → the stale document is **still there** after the run | traceback before prune |
| `test_cli_ingest_tags_are_normalized` (F7, the QA symptom; parametrized over `ingest`, `ingest-dir`, `ingest-stdin`) | `-t qa-a -t Markdown_Test -t "Interview Prep" -t MARKDOWN-test` stores `["qa-a", "markdown-test", "interview-prep"]` (deduped, first-seen order) in `documents.tags` and `chunks.tags_text` | stored raw: `Markdown_Test` |
| `test_ingest_gmail_tags_are_normalized` (F7) | gmail insert path through the existing gws fake runner in `test_cli_ingest_gmail.py`'s pattern | raw |
| `test_mcp_ingest_stdin_tags_are_normalized` (F7, MCP bypass) | `brain_ingest_stdin(tags=["Team_Notes"])` stores `["source-mcp", "team-notes"]` | raw `Team_Notes` |
| `test_connect_or_exit_lets_body_errors_through` | see above | — |

Hygiene scans to run: `tests/test_log_privacy_rules.py`, `tests/test_log_privacy.py`,
`tests/test_import_hygiene.py`, `tests/test_repo_hygiene_files.py` (CHANGELOG), and
`scripts/check_ceiling_table.py`.

Existing suites to re-run, targeted, because the refactor touches their paths:
- `test_cli_ingest.py`, `test_cli_ingest_stdin.py`, `test_cli_ingest_guard.py`
- `test_cli_ingest_dir_sync.py`, `test_cli_ingest_dir_walk.py`
- `test_cli_ingest_gmail*.py`, `test_cli_reembed.py`, `test_email_columns.py`
- `test_ingest_stdin_action_items.py`, `test_ingest_source_kind_validation.py`
- the 7 switched `test_cli_ingest_<x>.py`
- `test_backup_*` that touch `DsnTarget`

## 4. Ceiling table (`docs/ceiling-table.md`)

- **`cli_ingest.py`.**
  - Promote the trailing descriptive hop "947 (the BL-39 review round …)" to `2a95c955`. Verified:
    `git show 2a95c955:src/brain/cli_ingest.py | wc -l` = 947, and no commit touches the file
    after it.
  - Move "Authoritative through" to `2a95c955`.
  - Add a descriptive hop for this PR's commit: net-negative; the refusal handling, embed-failure
    stop and metadata parse move to the leaf `cli_ingest_boundary.py`. No delta, no SHA.
  - The module docstring gets no growth paragraph: the file shrinks. It does get one sentence
    naming the new leaf, written so the docstring's own growth stays inside the net-negative
    total.
- **`ingest/__init__.py`.**
  - The trail is already bound through `dc5a5f2`, and no commit touches the file after it, so
    there is nothing to promote.
  - Add a descriptive hop for this PR: net-negative; `_parse_sent_at` moves to
    `ingest/sent_at.py`, and `normalize_tags` on the insert path is net zero.
- **New files.** Both new files stay under 800.
- **`pgtool.py`.** Stays under 800 (393 → about 397).

## 5. Risks and residue

- **Typer `min=` on `int | None`.** Click's `IntRange` accepts a `None` default. I will confirm
  with ctx7 (Typer docs) before relying on it.
- **`exit_on_refusal` refactor.** It reproduces the existing stderr text and exit 1 for
  SecretGuard/Sensitivity byte for byte. The existing guard tests are the check.
- **Normalizing inside `ingest_document`.** It changes the stored form for any caller that
  passed non-canonical tags. That *is* the invariant (tag-normalization memory), and the
  `brain backfill normalize-tags` subcommand already fixes historical rows. A grep of tests
  found no test that expects a raw-case tag back from `ingest_document`.
- **`describe_failure` on `OllamaEmbedError`.** It prints `OllamaEmbedError from ReadTimeout`:
  class names only, no content. This satisfies `test_log_privacy_rules`.
- **Residue (not fixed here, for fix-lead to place).**
  - **(a) Embed failures still traceback elsewhere.** An `EmbedError` still escapes as a
    traceback from `ingest-gmail` (its per-thread `except` lacks it), from `reembed`, and from
    the other `ingest-*` commands' `_guarded` helpers. Same class as F1; not in the QA rows.
  - **(b) `date` keys stay warn-only.** A `date` key inside `--metadata`, and MCP
    `brain_ingest_stdin`'s `date`, are still warn-only.
  - **(c) Duplicate DSN describer.** `cli_ui._describe_db` duplicates `DsnTarget.__str__`.
  - **(d) Unvalidated MCP metadata.** MCP `brain_ingest_stdin` does not validate `metadata` /
    `content_type`.
- **Memory.** `cli.md` (the `reembed` ranges, exit codes) is owed after merge, per CLAUDE.md
  rule 11. I will flag it to fix-lead rather than edit the auto-memory from a worktree.

## 6. CHANGELOG (inserted immediately before `### Added — document-level embeddings as a re-scoring search stage, off by default (RM-20)`)

```markdown
### Fixed — the ingest commands refuse bad input in one line, not a traceback (QA F1, F2, F3, F4, F7, F10, F12)

- An embedding failure during `brain ingest` or `brain ingest-stdin` is one line naming the
  error class, and exit 1. Nothing was stored. `brain ingest-dir` stops at the first file whose
  embedding times out or cannot reach Ollama, prints where it stopped, and exits 1; a re-run
  skips the files already ingested. With `--sync`, a stopped run prunes nothing. An embedding
  error that is not a timeout or connection failure fails only that file, and the walk goes on.
- Every ingest command prints `cannot reach the database (<ErrorClass>). Is Postgres running?
  Try: brain doctor` and exits 1 when Postgres is down, instead of a psycopg traceback. The line
  names no host, port or other part of the URL. (This draft first printed `host:port`; §11
  dropped the address, and `brain doctor` shows the target.)
- `brain ingest-stdin --metadata` must be a JSON object, and `--date` must parse as ISO 8601 or
  RFC 2822. Either mistake is a usage error (exit 2) before stdin is read. A bad `--date` used to
  be stored with only a warning.
- Tags given to `brain ingest`, `ingest-dir`, `ingest-stdin`, `ingest-gmail` and the MCP
  `brain_ingest_stdin` tool are normalized (lowercase, hyphenated, deduplicated) like every other
  tag write. Run `brain backfill normalize-tags` once to fix rows stored before this release.
- `brain ingest` on a file type it does not read is one line listing the types it does read,
  pointing at `brain ingest-code` for source code, with exit 2. A malformed PDF or DOCX is one
  `cannot read` line, with exit 1.
- `brain reembed --batch-size` and `--limit` must be at least 1 (exit 2). `--batch-size 0` used
  to do nothing, and `--limit -1` reported "would embed -1".
```

## 7. Not in scope (fix-lead files these in the backlog PR)

Line numbers are on this branch's head.

1. **`EmbedError` still escapes as a traceback** from these per-item loops. Each `except`
   tuple lacks it:
   - `ingest-gmail`: `src/brain/cli_ingest.py:708`.
   - `reembed`: `src/brain/cli_ingest.py:871`, `embedder.embed`, unguarded.
   - `ingest-github`: `src/brain/cli_ingest_github.py:275`.
   - `ingest-bookmarks`: `src/brain/cli_ingest_bookmarks.py:249`.
   - `ingest-web`: `src/brain/cli_ingest_web.py:351`.
   - `ingest-apple-notes`: `src/brain/cli_ingest_apple_notes.py:237`.
   - `ingest-calendar`: `src/brain/cli_ingest_calendar.py:238`.
   - `ingest-folder`: `src/brain/cli_ingest_folder.py:397`. `_ingest` is only guarded for
     `SecretGuardError`; whether the caller turns an `EmbedError` into a re-queue needs
     checking.

   Not affected: `ingest-notion` (`cli_ingest_notion.py:252`) and `ingest-code`
   (`cli_ingest_code.py:230`). Both catch `BrainError`, which `EmbedError` subclasses.
2. **A `date` key inside `--metadata` is still warn-only.** It is not checked like `--date`.
   The pipeline logs and skips it at `src/brain/ingest/__init__.py:387`. The MCP
   `brain_ingest_stdin` `date` (`src/brain/mcp_server.py:1667`) is warn-only too.
3. **MCP `brain_ingest_stdin` does not validate `metadata`** (`src/brain/mcp_server.py:1665`).
   The SDK types it as a dict, but its values reach `_promote_metadata_to_columns` unchecked.
4. **`cli_ui._describe_db`** (`src/brain/cli_ui.py:150`) duplicates
   `DsnTarget.__str__` / `.address` (`src/brain/backup/pgtool.py`). It should call
   `dsn_target`.
5. **`cli_ingest_code` keeps its own refused-connect line** (`USAGE: database unavailable (...)`)
   rather than `connect_or_exit`. That is deliberate here, so its output stays the same. It
   could converge.

## 8. Review round rev-137 (fixes in a second commit; each one was RED against `8be6e673` first)

1. **F2 host leak.** An unescaped `@` in the password parsed into the host
   (`3ticPw@127.0.0.1:1`), and a percent-encoded control character decoded into it.
   `db_address` now prints `(unparseable)` unless every host is a host name, an IPv4
   address, a bare IPv6 address or an absolute socket path. The whole line also goes
   through `one_line`.
2. **`--metadata` values Postgres refuses.**
   - `NaN` / `Infinity` are refused through `parse_constant`.
   - A non-finite float (`1e400`) and a NUL in any key or string are refused by an
     iterative walk, so deep nesting cannot overflow the stack.
   - Any other `ValueError` (the integer digit limit) and a `RecursionError` are named by
     class only.
   - `JSONDecodeError` is still caught first and keeps its position.
   - Every case raises `BadParameter`, exit 2.
3. **`--date` overflow.** `parse_sent_at` returns `None` on `OverflowError`. That covers
   both `--date` and the pipeline's `sent_at` promotion.
4. **Malformed DOCX.** `_docx_errors` also wraps `KeyError`, `zipfile.BadZipFile` and
   `SyntaxError`, the base of lxml's `XMLSyntaxError`. lxml is only a transitive dependency
   and ships no stubs.
5. **The source-scan guard.** It now also catches `connect_age`, a `brain.db` bound as a
   module (`from . import db`, `import brain.db [as x]`) and then `.connect` /
   `.connect_age`, and `psycopg.connect` (any alias, or `from psycopg import connect`).
   Each spelling is mutation-proven in a git-archive copy.
6. **`ingest-dir`'s per-file `failed:` line** names the error class only, through the same
   helper as the embed failure (`file_failure_stops_walk`). `cli_ingest.py` shrinks by one
   more line.
7. **CHANGELOG.** It names the `ingest-code` exception and the wider DOCX / metadata
   refusals.

## 9. Review round rev-137b (third commit; each new test RED against `7a897c1c` first)

1. **The address rule is structural now.** It lives in the new leaf `src/brain/dsn_address.py`;
   the module docstring is the rule.
   - A password's text only extends rightwards, so host and port are printed only from where it
     cannot reach.
   - For a URI, that is the authority: at most one `@`, and no `@` after it. The query must not
     set host, hostaddr or port; keys are compared percent-decoded, as libpq decodes them.
   - For `key=value`, host and port appear at most once each, and before `password`.
   - In both forms, the port is 1 to 65535 and the host must have a valid shape.
   - The tests include a 400-sample property test over the characters that move libpq's parse
     boundaries, with six DSN shapes per sample. Each output must be either `127.0.0.1:1` or
     `(unparseable)`.
   - Trying to break the rule found percent-encoded query keys (`ho%73t=`). That bypass is fixed
     and pinned.
2. **Metadata.**
   - A lone surrogate is refused, in a key or a value. `encodes_utf8` moved from
     `ui/ingest_schemas.py` to `ingest/text_safety.py`, so the UI and the CLI share one rule.
   - `duration_min` outside int4, or infinite (`OverflowError`), is skipped with a warning in
     `_promote_metadata_to_columns`. That covers CLI and MCP. `ingest/__init__.py` is net −1.
3. **Unpinned code is removed, not pinned.**
   - `one_line()` on the "cannot reach" line is redundant: the host pattern admits no control
     character, and `describe_failure` emits only class names.
   - The `parse_constant` hook is redundant: the walk's `math.isfinite` refuses `NaN` and
     `Infinity` too.
   - The mutation proofs show the remaining mechanism is what pins each behaviour.

## 10. Review round rev-137c (one commit; each new test RED against `11f61663` first)

1. **`#` in a URI query.** libpq does not end a query at `#`, so `?password=#&port=4242`
   printed `127.0.0.1:4242`. The query check now reads to the end of the DSN; the authority
   still stops at `#`.
   - Both URIs are in `_LEAKY_URIS`.
   - The property test adds `#&key=` pieces, plus a seeded set that always places `#`
     directly before an `&key=`.
2. **Quadratic host pattern.** A host entry over 255 characters is `(unparseable)` before the
   pattern runs. The tests assert the result for 16k colons (in both key=value and URI
   form), 256 characters and a 300-character list entry. A 255-character host still prints.
3. **libpq defaults.** With no `host`, or with `hostaddr` / `service`, the address is
   `(libpq default)`. With no `port`, it is `<host> (default port)`.
   - **Chosen over reading PGHOST/PGPORT:** reading them would reimplement libpq's
     resolution order (PGHOST, PGHOSTADDR, PGSERVICE, `pg_service.conf`, the socket
     directory). That kind of partial copy is how the old line came to print a wrong
     address.
   - `db_address` now reads `conninfo_to_dict` itself, so it can see `hostaddr` /
     `service`. The `DsnTarget.address` property added to `backup/pgtool.py` in round 1 is
     reverted, and `pgtool.py` is unchanged from `ef824ed1`.
   - §7 item 4 now reads: `cli_ui._describe_db` duplicates `DsnTarget.__str__`.

## 11. Decision: the unreachable-database line names no address (supersedes F2's address in §1, §8–§10)

**Address display was removed after four review rounds of libpq-parsing edge cases. `brain
doctor` shows the target.** That is the team-lead's decision on #137 after rev-137c. The
rounds kept finding inputs where a password, or a default libpq chose, ended up in the
printed host or port:
- an unescaped `@`, `/`, `?` or `#`;
- percent-encoded query keys;
- `#` inside a query;
- PGHOST / PGPORT, `hostaddr`, `service`;
- quadratic host matching.

Each fix was one more partial copy of libpq's parser.

- **The line.** It is now exactly `cannot reach the database (<ErrorClass>). Is Postgres
  running? Try: brain doctor`. There is no host, port, user, database name or any other DSN
  text in it.
- **Deleted.**
  - `src/brain/dsn_address.py`, and `unreachable_line`'s DSN argument.
  - The address tests: the leak lists, the 460-sample property test, the long-host,
    PGHOST/PGPORT and hostaddr/service tests, the clean-DSN and well-formed-host tests, the
    unparseable-DSN test, and the class-only `unreachable_line` test.
- **Dead-code re-check.**
  - `backup/pgtool.py` is already identical to `ef824ed1`, since the round-1 `address`
    property was reverted in rev-137c.
  - `ingest/text_safety.encodes_utf8` is still used by the `--metadata` walk and `brain ui`.
  - `cli_ingest_boundary` keeps `one_line` for the embed-failure and per-file lines.
- **The proof.** It is one parametrized test,
  `test_unreachable_db_line_is_fixed_text_whatever_the_dsn`. Its inputs are every DSN from
  the old leak lists, plus a clean URI, a clean `key=value` DSN and a no-host DSN, each run
  under both `OperationalError` and `ProgrammingError`.
  - A recording fake `connect_raw` never connects: it raises an error whose message quotes
    the whole DSN.
  - The test asserts stderr **equals** the fixed text. Nothing from the DSN can appear.
- The CLI tests for `ingest-stdin`, `ingest`, `ingest-dir` and `reembed` still refuse
  through a real port-1 connect. They expect the new wording, and they still assert that
  no credential appears.
- Everything else in #137 is unchanged: metadata, date, DOCX, tags, reembed,
  `connect_or_exit`, the scan guard, `ingest-dir`.

## 12. Post-approval audit of `8d8a970f` (one commit; each new test RED against `8d8a970f` first)

1. **`ingest-gmail`'s per-thread `failed thread` line.** For a psycopg, `ValueError` or
   `KeyError` failure it now prints `describe_failure(e)`, the class only. A `GmailError`
   keeps its message: BL-39 already made those class-only where they could quote `gws`
   output. `cli_ingest.py` is net −2.
   - Test: `test_ingest_gmail_failed_thread_line_names_the_class_not_the_text`, with 3 cases,
     all carrying a synthetic marker.
   - It runs with no database: an offline `connect_raw` fake, the existing gws runner fake,
     and `ingest_document` patched to raise.
2. **`connect_or_exit` also catches `UnicodeError`.** A DSN that cannot be encoded (a lone
   surrogate) raises `UnicodeEncodeError` before libpq. The `_ANY_DSN` equality test gains
   `UnicodeEncodeError` as a third error class, adding 32 cases.
   - RED against `8d8a970f`: 35 failed, the 32 new error cases plus the 3 gmail cases. The 64
     existing cases passed.
