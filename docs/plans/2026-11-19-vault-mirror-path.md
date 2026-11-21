# Plan: vault mirror paths without the absolute source path (F15)

> **For agentic workers:** REQUIRED SUB-SKILL: Use team-driven-development to implement this plan task-by-task.

- Finding: F15 (MED), QA row AX3. Report: `brain-qa/REPORT.md` line 235. Evidence:
  `brain-qa/logs/a/LANE-A-REPORT.md:30` and `:65-67`. There is no `AX3.txt`; the lane report
  is the evidence. The finding is also in `brain-qa/logs/findings-running.md:21`.
- Base: `66cd9cb2` (local `master` == `second-brain/master` at worktree creation).
- Branch / worktree: `fix/vault-mirror-path`, `.claude/worktrees/fix-f-mirror-path`.
- Test DB: `fix_f_test` on port 5434.
- Status: **APPROVED with conditions** (team-lead, relayed by fix-lead, 2026-11-19); base
  moved to `8e9478e1` (fast-forward). The conditions are in §0 and override the
  sections below where they differ.

## 0. Approval conditions (team-lead), and how each is met

- **(a) A wider safe set; no dot-names.** The safe segment set is
  `[A-Za-z0-9._@+=~-]`, which adds `-` and `_`. `-` can never actually appear,
  because `_short_id` strips dashes first. A segment is also unsafe when it **starts
  with `.`**, which covers `.`, `..`, `...` and anything that would read as hidden.
  An unsafe segment is replaced by the sha256 prefix.
  - The segment always sits between `<date>-` and `-<slug>`, so it is never a whole path
    component. Even so, no output segment is ever `.`, `..` or a leading-dot name.
  - The same reasoning applies to the **date prefix**. `metadata.date[:10]` is taken
    verbatim. A stored `--date` such as `2026/01/15` or `../../../x` would nest or escape
    the vault (QA F4 shows that `--date` is not validated). So a date prefix that holds
    `/`, `\` or NUL, or that starts with `.`, falls back to the `ingested_at` date.
  - That date change goes through the same vacancy guard: an old-rule file already on
    disk is reused.
- **(b)+(d) A zero-change tree snapshot for every bulk command.** This is
  `tests/test_vault_mirror_path_bulk.py`. A `tmp_path` vault is seeded with four legacy
  nested-path mirrors whose rows store those paths:
  - `code` with an absolute path;
  - `screenshot` with an absolute path;
  - `github` with `acme/widgets#7`;
  - `krisp` with an absolute-path-style id.

  Each command runs once through the CLI:
  - `vault export`, and `vault export --force`;
  - `vault sync`, and `vault sync --prune`;
  - `vault relink-derived`;
  - `vault prune-orphans --apply`, and the same with `--include-stale`;
  - `vault sync-summaries`.

  Every file's relative path and sha256, every directory, and every stored
  `vault_path` are compared before and after. Each test lists its allowed writes
  explicitly:
  - `relink-derived` adds the People Hub index;
  - `prune-orphans` removes the one seeded true orphan, in both modes;
  - `sync-summaries` rewrites the one mirror whose row was given a summary;
  - every other command changes nothing.
- **(c) CHANGELOG.** The entry says that existing mirrors keep their paths, that the
  stray directories from earlier runs are NOT cleaned up, and that an opt-in cleanup is a
  later ticket. It is inserted immediately before
  ``### Added — `brain examples`, finding a command by example (RM-53)``.
- **(e) The vacancy guard with an unrecorded legacy file.** The legacy file is on disk
  and the row's `vault_path` is NULL. `export_vault` writes no new file, and the
  ingest mirror-repair branch adopts the legacy path:
  `test_an_unrecorded_legacy_file_is_reused_by_export_and_by_mirror_repair`.
- **(f) Determinism.** The rule reads no clock and no randomness. The new module
  imports neither `time` nor `random`, and `datetime` is used only for formatting the
  dates it is given. `test_the_rule_is_a_pure_function_of_its_inputs` checks that
  repeated calls give the same path.
- **(g) Containment.** All tests use `tmp_path`. The guard adopts a legacy path only
  when that path is **contained**: relative, no `..` component, no NUL. So an old-rule
  path that would escape the vault (from a hostile date) is never reused.
  `test_a_hostile_external_id_stays_inside_the_vault` runs these ids:
  - `../../etc/x`, `/abs/path`, `..`, `.`, `...`, `.hidden`;
  - NUL, a 5,000-char id, `../` repeated 2,000 times;
  - unicode, backslashes, whitespace, `:`, the empty string, and `--------`.

  For each one it checks four things:
  - the path is not absolute;
  - the path has no `..` part;
  - the parent is `_ingested/code`;
  - the path stays inside `tmp_path` after `resolve()`.

  `test_an_escaping_old_rule_path_is_never_reused` covers the escaping-date case.

**Final rule** (supersedes the snippet in §3):

```python
_SAFE_SEGMENT = re.compile(r"[A-Za-z0-9._@+=~-]+")

def external_segment(external_id: str) -> str:
    legacy = external_id.replace("-", "")[:8]
    if _SAFE_SEGMENT.fullmatch(legacy) and not legacy.startswith("."):
        return legacy
    return sha256(external_id.encode("utf-8", "surrogatepass")).hexdigest()[:8]

def ingested_relative_path(doc, is_taken, *, vault_root):
    current = ladder(candidate(doc, external_segment, safe_date_prefix), doc, is_taken)
    legacy = ladder(candidate(doc, short_id, legacy_date_prefix), doc, is_taken)
    if legacy != current and contained(legacy) and lexists(vault_root / legacy):
        return legacy                     # never duplicate or move an existing file
    return current
```

**Implementation notes (found while running the tests):**

- A forced re-ingest (`force=True`, update branch) re-stamps `ingested_at`
  (`ingest/__init__.py:1419-1423`). That was already the behaviour before this
  change, and it is intended. The mirror's `created`/`updated` lines therefore change.
  - The test asserts the parts that must not change: the same path, a single copy, a
    byte-identical body, and every other frontmatter field equal.
  - "Byte-identical across `--force`" is not achievable without changing that
    pre-existing behaviour, and this PR does not change it.
- `vault relink-derived` creates the `people/` directory and `people/index.md`, even on
  a corpus with no people. Its snapshot test lists both as expected additions.
- Mutation proofs: `test_an_escaping_old_rule_path_is_never_reused` originally passed
  even with the containment check removed. The cause was that `_ingested/code` did not
  exist, so the kernel could not walk the `..` hops in the path. The test now creates the
  directory and asserts that the probe can see the outside file.
- Removing the stored-path short-circuit alone is still caught by the vacancy guard
  (defence in depth). Only the combined mutation (stored path ignored AND no vacancy
  guard) makes the bulk `vault export` snapshot fail.

## 1. The bug

`brain ingest-code` (and `brain ingest-folder --dir`) wrote
`_ingested/code/2026-11-14-/srv/rep-sample-repo-src-widget-py.md`. The stray directory
`2026-11-14-/srv/` was created, and `documents.vault_path` stored that same string.

Root cause, `src/brain/vault/export.py`:

- `_short_id` (`:247-257`) is `value.replace("-", "")[:8]`. It does not sanitize anything.
- `_ingested_relative_path` (`:350-420`) puts that value in the filename unchanged, at
  `:398-404` for every non-manual, non-gmail source and at `:390-396` for the gmail fallback.
- When the external id contains `/`, the 8-character prefix contains `/`, and the
  "filename" becomes nested directories.

### How the external ids are formed today

| Source | external id | Where | Legacy 8-char segment | Unsafe? |
|---|---|---|---|---|
| `ingest-code` (`code`) | absolute file path | `ingest/code_files.py:202` sets `source_path=str(root / file.path)`; `cli_ingest_code.py:212-216` passes no `source_external_id`; `ingest/__init__.py:721-725` defaults it to `doc.source_path` | `/srv/rep`, `/private` | **yes** |
| `ingest-folder` (`screenshot`, `voice`, …) | absolute path of the file in `imported/` | `cli_ingest_folder.py:472-476` (no external id) and the same default at `ingest/__init__.py:721-725` | `/srv/rep` | **yes** |
| `ingest-github` | `<owner>/<repo>#<n>`; README is `<owner>/<repo>:README` | `ingest/github.py:111-113`, `ingest/github_docs.py:183` | `acme/wid` when the owner name is short | **yes**, same bug, not reported by QA |
| `ingest-stdin` / MCP (krisp, slack, …) | whatever the caller passes in `--external-id` | `cli_ingest.py:520`, `mcp_server.py:1688` | usually a UUID or Slack ts | only if the caller puts `/` in it |
| web / bookmarks | sha256 hex of the URL | `ingest/browser.py:66-68` | hex | no |
| social | `x-<id>` | `ingest/social_posts.py:122-125` | `x1234567` | no |
| notion, calendar, apple notes | page UUID / event id / note id | `cli_ingest_notion.py:273`, `calendar.py`, `cli_ingest_apple_notes.py:268` | alnum | no (in practice) |
| `manual` | n/a: the manual branch does not use the external id (`export.py:383-384`) | | | no |

## 2. Where a mirror path is computed, stored and read

- **Computed:** in exactly one place, `_ingested_relative_path` (`export.py:350`). Only
  `_resolve_relative_path` (`:423-435`) calls it. A grep for `_ingested_relative_path`,
  `_short_id` and `f"_ingested/` across `src/brain` finds no other builder. The rule is
  reached through exactly two public functions:
  - `export_vault` (`:643-692`), which is `brain vault export` and the `mirror` stage of
    `brain rebuild` (`maintenance.py:74-81`).
  - `regenerate_vault_file` (`:695-765`), called by:
    - ingest (`ingest/__init__.py:846`);
    - `update_document`, which `brain edit`/MCP use (`ingest/__init__.py:1935`);
    - `brain tag --regenerate-file` (`cli.py:5736`);
    - sensitivity propagation (`vault/sensitivity_propagate.py:125`).
- **Short-circuit for stored paths:** both callers return the stored path first:
  - `_resolve_relative_path` returns `doc.vault_path` when it is set (`export.py:433-434`);
  - `regenerate_vault_file` does `relative = doc.vault_path or _resolve_relative_path(...)`
    (`:753-755`).

  **A row with a non-NULL `documents.vault_path` never reaches the rule.**
- **Stored:** `regenerate_vault_file` UPDATEs `documents.vault_path` to the path it chose
  (`:760-764`, `IS DISTINCT FROM`). `vault sync` stores the path of the file it reads
  (`sync.py:793` insert, `:841` and `:879` update). `export_vault` does **not** store
  the path. It writes the file and leaves `vault_path` NULL. Usually the watcher or the
  next sync records it.
- **Read:** every reader takes the stored column. None of them recomputes it:
  - sync-summaries (`sync_summaries.py:97-117`);
  - the derived-link fence (`derived_links/fence.py:266-276` for links, `:449-471` for
    targets);
  - prune and drift scans (`queries.py:1314`, `:1391`);
  - the resolver (`vault/resolver.py:236`);
  - `brain ui`, the People Hub and wikilink renderers (`vault/paths.py` `strip_md_extension`).

## 3. The new rule (exact)

New module `src/brain/vault/mirror_path.py`. The path-rule cluster moves out of
`export.py`: `_short_id`, `_date_prefix`, `_parse_iso_datetime`, `_gmail_relative_path`
and `_ingested_relative_path`, plus the new helpers. `export.py` is at 765 lines today;
adding the rule in place would take it past the 800 ceiling, and the CLAUDE.md rule is to
extract. After the move `export.py` shrinks to about 620 lines and the new module is about
220. No test or src module imports the moved private names (grep: only
`_write_doc_file` and `_EXPORT_OWNED_FRONTMATTER_KEYS` are imported from `export.py`, and
both stay). `_paths_taken_by_others` stays in `export.py` because it is the DB oracle, and
`ingest/__init__.py:855` names it there. The module takes the doc through a small
structural `Protocol` (`id`, `title`, `metadata`, `ingested_at`, `source_kind`,
`source_external_id`), which `_DocumentForExport` satisfies, so there is no import cycle.

**External-id segment** (used for every source, including the gmail fallback):

```python
_SAFE_SEGMENT = re.compile(r"[A-Za-z0-9._@+=~]+")

def external_segment(external_id: str) -> str:
    legacy = external_id.replace("-", "")[:8]           # today's _short_id, unchanged
    if _SAFE_SEGMENT.fullmatch(legacy):
        return legacy                                     # byte-identical to today
    return hashlib.sha256(external_id.encode("utf-8")).hexdigest()[:8]
```

- A segment that is already safe stays exactly as it is today. That covers UUIDs, Slack
  ts, `x-<id>`, URL hashes, notion and calendar ids. So for every source except the broken
  ones, the path a NEW doc gets is the path it would have got before this change.
- A segment that contains any other character (`/`, `\`, `#`, `:`, whitespace, control
  characters, non-ASCII, or the empty string) becomes the first 8 hex digits of the
  sha256 of the **full** external id. That segment is fixed width, deterministic, stable
  across re-runs, the same shape as the UUID segments, and never contains `/`.

Then the candidate is formed as today:
`_ingested/<source>/<YYYY-MM-DD>-<segment>-<slugify(title)>.md`. The `manual` and
thread-gmail shapes are unchanged.

**Vacancy guard (the "existing files are never touched" half of the rule).** The rule
only runs for a row with no stored path. For such a row whose segment is unsafe, the
function first computes the **legacy** choice, today's code verbatim including its
collision ladder. If anything exists on disk at `vault_root / legacy`
(`os.path.lexists`), it returns the legacy path, exactly as today. The new path is used
only when the legacy location is vacant. The new function signature is
`ingested_relative_path(doc, is_taken, *, vault_root: Path)`. Both callers already hold
the vault root.

So the new rule changes behaviour in exactly one case: a row with no stored `vault_path`,
whose external-id segment is unsafe, and whose legacy file does not exist. In that case
there is no file to touch.

### Why hash and not slugify (the report's suggestion)

- `slugify(_short_id(path))` gives `users-m` or `private` for every file on the machine.
  It disambiguates nothing, so collisions would fall through to the doc-id suffix.
- `slugify(full path)` would write the OS username and home directory into every vault
  filename. That is PII-adjacent: the vault is plain Markdown that may be synced or
  shared. It would also eat the filename into a long, variable string.
- The hash keeps the readable part where it already is, in the title slug (code titles
  are `<repo>/<path>`, `code_files.py:190-205`).

### Collision handling

- **Two different absolute paths that would slugify to the same name**, for example
  `/srv/x/pkg/a-b.py` and `/srv/x/pkg/a_b.py`. The title slugs collide
  (`sample-pkg-a-b-py`), but the segments are sha256 prefixes of different strings, so the
  paths differ. Today both have the segment `/srv/x/p`, so the second one already needs
  the doc-id suffix. Test N4 pins this case.
- **A residual 32-bit hash collision with the same date and title slug:** the existing
  ladder is unchanged. It appends `-<first 8 of doc uuid>`, then `-<full doc uuid>`,
  never a counter (`export.py:406-419`). The oracle is also unchanged: the in-run
  `used_paths` set in `export_vault`, and the DB-backed `_paths_taken_by_others` in
  `regenerate_vault_file`.
- **New path against legacy files:** a new-rule segment contains only `[0-9a-f]`.
  Every legacy segment that this rule replaces contains a character outside the
  allowlist. So a new path can never equal a legacy unsafe path. It could equal another
  doc's legacy *safe* path only if that doc's short id equalled our hash prefix and the
  date and title also matched. That doc's row owns the path, so `is_taken` catches it.

## 4. Existing files are never touched, mechanism by mechanism

"Existing doc" means a row whose mirror already exists. The owner's production vault has
two shapes of these:

- **(A)** `vault_path` stored. This is the normal case: every ingest with a configured
  vault stores the path (`export.py:760-764`), and so does every sync.
- **(B)** `vault_path` NULL with a file on disk. This happens when `vault export` wrote
  the file (it does not persist the path) and no sync or watcher event has recorded it
  yet.

| Mechanism | What it does with a path | Why the fix cannot touch an existing file | Test |
|---|---|---|---|
| `vault export` / `rebuild` mirror stage (`export.py:677-678`) | `_resolve_relative_path` | (A) returns the stored `vault_path` before the rule runs (`:433-434`). (B) the legacy file exists, so the vacancy guard returns the legacy path, the same path as today. The body-hash skip (`:624-627`) then leaves the bytes alone | L1, L2, L10 |
| `regenerate_vault_file` (ingest, edit, tag, sensitivity) | `doc.vault_path or …` (`:753`) | (A) uses the stored path. (B) the vacancy guard picks the legacy path, then stores it, so from then on the row is (A) | L3, L10 |
| `vault sync` / watcher upsert (`sync.py:_sync_one`, `sync_one_file`) | stores the path **of the file it reads** (`:841`, `:879`); never computes one | Nothing moves, and the file is at its legacy path, so `vault_path_changed` is False. Sync's prune is scoped to `kind='vault'` (`sync.py:1723-1724`) | L4 |
| watcher delete / move (`watch.py:1235-1345`, `owed_work.py:52-53`) | reacts to filesystem events | The fix never moves, deletes or renames a file, so no event is generated for an existing mirror. Both SQL statements are scoped to `kind='vault'` anyway (`watch.py:1343`, `owed_work.py:52-53`). The owed-work journal only records deletes and graph removals | L4 (upsert path); static: the change emits no FS operation on legacy paths |
| `prune-orphans --apply --include-stale` (`cli.py:6431`, `queries.py:1314`, `:1391`) | orphan means the id has no row; stale means the id's row stores a **different** `vault_path` | (A) the row's `vault_path` is still the legacy file, so the file is neither orphan nor stale. (B) is never stale (`queries.py` skips NULL rows), and after export/sync the row adopts the legacy file. **The fix never changes a stored `vault_path`. That is the only way a file becomes "stale" and gets deleted by the mirror stage** | L5 |
| `relink-derived` (`cli.py:6719-6770`) | `rewrite_derived_fences` writes into `vault / <stored vault_path>` (`fence.py:449-471`); People Hub links use the stored path | It never computes a mirror path. The fence bytes depend on edges, not on the path rule | L6 |
| re-ingest (no `--force`) | dedup by `source_path` (`ingest/__init__.py:977-988`), then `skip` | No write at all. The mirror-repair branch fires only for NULL `vault_path` (`:829-834`), which is case (B): the vacancy guard picks the legacy path | L7, L10 |
| re-ingest `--force`, update branch (code, folder, sourced stdin) | `update` in place: same row id, and `_update_doc_in_place` never touches `vault_path` | `regenerate_vault_file` uses the stored path | L8, L9 |
| re-ingest `--force`, **replace** branch (stdin content-hash fallback, `:1229-1243`) | DELETE old row, INSERT new uuid with NULL `vault_path` | Today the new row recomputes the legacy path and overwrites the old file in place (old row gone, `is_taken` False). With the fix, the legacy file exists, so the vacancy guard returns the same legacy path. Same behaviour as today, and no second file | L11 |
| wikilinks / backlinks | `[[<vault_path-no-md>]]` (`resolver.py:236`), fence bare stem (`fence.py:272-276`), `[[brain:<id>]]`, `[[krisp:<ext>]]` (`resolver.py:112`, `:143`), title, alias | Every form resolves either through stored data that the fix leaves alone or through the id. The owner's hand-written `[[…]]` points at a file that never moves | L14 |

**No migration in this PR.** Existing stray directories stay. An opt-in relocation
command would have to do four things:

- move the file and update `vault_path` in one step;
- survive the live watcher's create and delete events (ingested deletes are ignored, and
  the create re-syncs by id);
- rewrite full-path wikilinks;
- re-render fences.

That is a separate, riskier ticket. I recommend filing it as a backlog item and not
building it here.

## 5. Files

- **NEW** `src/brain/vault/mirror_path.py`: the moved path-rule cluster,
  `external_segment`, the vacancy guard, and a one-line module docstring.
- `src/brain/vault/export.py`:
  - remove the moved helpers and import them from `mirror_path`;
  - `_resolve_relative_path` gains `vault_root`;
  - `export_vault` (`:678`) and `regenerate_vault_file` (`:753-755`) pass it;
  - the module docstring points at `mirror_path`.

  The net effect is that the file shrinks.
- `docs/vault.md`: one short paragraph saying how mirror filenames are formed and that a
  stored mirror path is never recomputed.
- `CHANGELOG.md`: entry below.
- Tests: three new files (below). No existing test changes. Every existing path assertion
  is either a stored `vault_path` fixture or a safe segment. The `ingest-web` test pins
  `count("/") == 2` (`tests/test_cli_ingest_web.py:278`) on sha256 ids, which stay
  verbatim.

## 6. Tests (TDD; RED first)

### Pure rule tests: `tests/test_vault_mirror_path_rule.py`, `-m nodb`

These use a small stand-in dataclass, an `is_taken` set, and a tmp vault root.

- **N1 RED.** An absolute-path external id (`/srv/synthetic/repo/src/a.py`, kind `code`)
  gives `_ingested/code/<date>-<sha8>-<slug>.md`. The path has exactly two `/`, and the
  segment is `sha256(ext)[:8]`. Fails today: the segment is `/srv/syn`.
- **N2 RED.** The github id `acme/widgets#7` and the README id `acme/widgets:README` add
  no extra directory.
- **N3 GREEN guard (zero churn).** Safe ids keep the legacy segment byte for byte:
  - a UUID gives its first 8 hex digits with the dashes removed;
  - `1234567.890` gives `1234567.`;
  - `x-1234567890` gives `x1234567`;
  - `local-<doc8>` is unchanged.
- **N4 RED.** `/srv/x/pkg/a-b.py` and `/srv/x/pkg/a_b.py` have the same date and the same
  title slug. They get two different paths and neither gets a doc-id suffix.
- **N5 GREEN.** The collision ladder works on a hash segment. When the base path is taken
  the result gets `-<doc8>`; when that is also taken it gets `-<full uuid>`. Both are
  deterministic.
- **N6 RED.** The vacancy guard returns the legacy path, byte for byte, when a file (or a
  symlink) already exists there. It returns the new path when the legacy path is vacant.
- **N7 GREEN guard.** When `vault_path` is set, `_resolve_relative_path` returns it as it
  is for an unsafe external id. The test asserts that by passing a `vault_root` that does
  not exist.
- **N8 RED.** A parametrized table checks that a new segment never contains a character
  outside `[0-9a-f]` when the legacy segment is unsafe. Inputs: `/`, `\`, `#`, `:`, a
  space, a tab, `é`, the empty string, and `--------`.

### Legacy-preservation tests: `tests/test_vault_mirror_path_legacy.py`

These run against the real `fix_f_test` DB and a tmp vault.

The fixture `legacy_code_doc` does the following:

1. `ingest_document` a code doc with `source_path="/srv/synthetic/repo/src/a.py"`, DB only
   (no `vault_root`), and a fixed `metadata.date`.
2. `UPDATE documents SET vault_path = L`, where L is the exact legacy shape
   `_ingested/code/<date>-/srv/syn-<slug>.md`.
3. Call `regenerate_vault_file` to write the file at L.
4. Snapshot the bytes.

The helper `_mirrors_of(vault, id)` lists every file under `_ingested/` whose frontmatter
`id` is this doc. Each test asserts all three of:

- `vault_path == L`;
- the bytes at L are identical to the snapshot;
- `_mirrors_of == [L]`, so no second copy exists anywhere.

| Test | What it runs |
|---|---|
| L1 | `export_vault` |
| L2 | `export_vault(force=True)` |
| L3 | `regenerate_vault_file(force=True)` |
| L4 | `sync_vault(prune=True)` plus `sync_one_file(L)`, the watcher's upsert path |
| L5 | `brain vault prune-orphans --apply --include-stale` through the CLI, with `BRAIN_VAULT_PATH` set to the tmp vault. Expects "0 orphan files" and the file still present |
| L6 | `rewrite_derived_fences(conn, {id}, vault_path=vault)`. Expects 0 written and the bytes unchanged |
| L7 | re-ingest the same file with `vault_root` set. Expects `skip` and no write |
| L8 | re-ingest with `force=True` (update branch) |
| L9 | re-ingest with a changed body. The path stays L; only the body changes, as expected |
| L10 (shape B) | Same seeding, then NULL the `vault_path` so the file exists but no path is stored. `export_vault` writes no new file. Then re-ingest with `vault_root` (the orphan-repair branch, `ingest/__init__.py:829-848`). The row adopts L, and `_mirrors_of == [L]`. **RED today? No: today also picks L.** It is a guard that the vacancy check covers shape B |
| L11 (replace branch) | Stdin doc, kind `krisp`, external id `/srv/synthetic/one`, fixed date. Mirror it at L. Then re-ingest the same content with external id `/srv/synthetic/two` and `force=True`, which takes the content-hash replace path. Exactly one mirror exists under `_ingested/krisp/`, at L, carrying the **new** id. That is today's in-place overwrite: no orphan, no second file |
| L14 (wikilinks) | A legacy doc at L plus a brand-new code doc ingested with `vault_root`. The new doc lands at the new path and L is untouched. A synthetic vault note containing `[[<L without .md>]]` and `[[<new path without .md>]]` resolves both, via `resolve_link`, to the right ids |

### CLI reproduction of the QA bug: `tests/test_vault_mirror_path_cli.py`

This file reuses `tests/code_repo_fixtures.py` and the fixture patterns of
`test_cli_ingest_code.py` and `test_cli_ingest_folder.py`, with `BRAIN_VAULT_PATH` set to
the tmp vault.

- **L12 RED.** Run `brain ingest-code <fixture repo>`. Every stored `vault_path` has
  parent `_ingested/code`, its segment is 8 lowercase hex characters, and the file exists.
  `_ingested/code` contains no subdirectories. Today `tmp_path` gives the stray directory
  `…-/private/`.
- **L13 RED.** Run `brain ingest-folder --dir <drop> --since 7d` on a synthetic PNG, with
  the fake OCR engine from the folder tests. The mirror's parent is `_ingested/screenshot`.

### Regression runs (targeted only; CI runs the full suite)

- The new files above.
- `test_vault_export.py`
- `test_vault_export_gmail_slug.py`
- `test_vault_sync_code_mirror.py`
- `test_cli_prune_orphans.py`
- `test_queries_mirror_clobber.py`
- `test_ingest_duplicate_titles.py`
- `test_vault_sensitivity_roundtrip.py`
- `test_class_only_reports.py`, which monkeypatches `brain.vault.export._write_doc_file`.
  That function stays where it is.
- `test_cli_ingest_code.py`
- `test_cli_ingest_folder.py`
- `test_cli_ingest_web.py`
- the hygiene scans: `test_log_privacy_rules.py`, `test_log_privacy.py`,
  `test_import_hygiene.py`, `test_repo_hygiene_files.py` (for the CHANGELOG).

Then `ruff check` and `mypy src/`. Every pytest run is taken under
`.claude/full-suite.lock` with the `df` check first.

## 7. Risks

- **Mixed shapes in one source directory.** Old code docs keep their stray paths and new
  ones land flat. This is accepted: it is the price of "existing paths untouched". The
  relocation command is a backlog item.
- **The vacancy guard reads the disk** in `export_vault`. One `lexists` per row, and only
  for unsafe-segment rows with no stored path. Negligible.
- **An unrelated file at a doc's legacy path.** The guard makes the doc reuse that path
  and overwrite the file, which is exactly today's behaviour (today never checks the
  disk). No new clobber class is introduced.
- **Daemon install.** The watcher and the 10-minute `ingest-folder --configured` job run
  from the non-editable daemon install. They pick up the rule only after a reinstall.
  There is no ordering hazard: the watcher never computes a path.
- **No schema change and no migration.**
- **Accepted pre-existing clobber in `vault export`** (raised in review). When two rows
  compute the same path and one of them has no stored `vault_path`, the in-run
  collision set of `export_vault` can let one row write over the other's file. That
  happens because `export_vault` neither stores nor reads the paths of other rows on
  disk. This was already the case before this change, which neither adds nor removes it,
  and it is accepted here.

## 7a. Not in scope (backlog)

- **BL-97: relocate the existing stray directories.** This would be an opt-in command;
  see §4 for what it must do.
- **Hardening of the vacancy guard** (the auditor's idea). Reuse an old-rule file only
  when its frontmatter `id` is this document's id, or resolves to no live row. Today the
  guard reuses any file found at the old-rule path. That is exactly the old behaviour
  (the old rule wrote there without looking), so it is not a regression. The check would
  narrow the pre-existing clobber above.

## 8. CHANGELOG (under `## 0.4.0 - 2026-11-08`, immediately before the RM-53 `brain examples` subsection)

```markdown
### Fixed — vault mirror filenames no longer contain the source path (F15)

- `brain ingest-code`, `brain ingest-folder` and `brain ingest-github` named a document's
  mirror after the first 8 characters of its external id. For a file path or an
  `owner/repo#N` id those characters contain `/`, so the "filename" became stray nested
  directories: `_ingested/code/2026-11-14-/srv/rep-sample-repo-src-widget-py.md`.
- That part of the filename is now the first 8 hex digits of the id's SHA-256 whenever
  the old part held a character that is not safe in a filename, or started with a dot.
  Ids that were already safe (UUIDs, Slack timestamps, URL hashes) keep exactly the part
  they had.
- A `metadata.date` holding a path separator now falls back to the ingest date instead of
  nesting the file.
- **Existing mirrors keep their paths.** No file is moved, renamed, rewritten or deleted
  by this change:
  - a document that already has a stored `vault_path` keeps it, and so do the wikilinks
    that point at it;
  - a document whose old-style file is already on disk keeps using that file.

  The new name applies only to a document mirrored for the first time.
- **The stray directories that earlier runs created are NOT cleaned up.** An opt-in
  cleanup command is a later ticket.
```
