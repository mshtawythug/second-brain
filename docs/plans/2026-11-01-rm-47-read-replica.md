# RM-47: using the brain from a second machine, with no bidirectional sync

> **For agentic workers:** REQUIRED SUB-SKILL: Use team-driven-development to implement this plan task-by-task.

- **Ticket:** RM-47, roadmap spec `docs/specs/2026-10-23-roadmap-completion-design.md` §RM-47
  (Appendix B row: "Multi-host via one brain + read replica", size S, wave 9, depends on RM-46).
- **Base:** `master` at `01c58a72` (RM-46 merged, PR #118). Branch `feat/rm-47-read-replica`.

## 1. Goal and acceptance criteria (quoted from the spec)

> **Goal:** Use the brain from a second machine without ever having two writers.
>
> **Scope in:** `docs/guides/second-machine.md`; `backup/restore.py` (writes a marker); a `brain
> doctor` line. **Scope out:** two-way sync (RM-47.D1).
>
> **Design:** **one brain, reached from elsewhere**, which is RM-46, plus a documented one-way
> **read replica** recipe: `brain backup` on the primary, copy the archive, `brain restore` on the
> second machine into its own database, and treat that copy as read-only (writes there are lost at
> the next restore). `brain restore` today persists nothing from the archive's `manifest.json`, so it
> gains one step: after a successful restore it writes `$BRAIN_HOME/state/restored-from.json` with
> the manifest's `created_at` (the `BackupManifest` field) and the archive's file name. The doctor
> line reads that file and reports "read replica, restored from a backup taken at <created_at>".
>
> **Acceptance criteria:**
> 1. After a restore, `restored-from.json` holds the manifest's `created_at`.
> 2. The doctor line names it; without the file, the line is absent.
> 3. The guide's recipe works end to end on the demo sandbox.

RM-47.D1 (bidirectional sync: **never**) is settled by the spec and is not reopened here.

## 2. What exists today (read from source at `01c58a72`)

- `brain.backup.restore.restore_backup(archive, cfg, *, db_leg, vault_leg, runner, clock,
  pre_backup, on_step, embedder, prepared) -> RestoreReport`. It ends with the post-restore
  `brain init` / `ANALYZE` block, then `shutil.rmtree(state.staging_dir, ...)`. `restore.py` is
  **755 lines**, and its spec budget is **+15** (§2.2).
- `BackupManifest.created_at: datetime` is tz-aware. `from_dict` parses it with
  `datetime.fromisoformat`, and `to_dict` writes `.isoformat()`.
- `cfg.brain_home` defaults to `_brain_home_root()` (`config.py:1024`). `$BRAIN_HOME/state` is
  already used (`cli_ingest_folder._voice_state_dir`).
- Doctor rows: `doctor_runtime.runtime_doctor_checks()` composes one leaf per probe
  (`doctor_git.py` is the pattern) and takes no `Config`. `_check(name, status, detail, remedy,
  notes)` renders the house format. `doctor_runtime.py` is **599 lines**: the budget is 563 + 60
  = 623, so 24 lines are left.
- `tests/test_doctor_runtime.py:586` pins the composed row names. The new row is absent without a
  marker, so that pin stays valid as long as the test's `BRAIN_HOME` has no marker (verify while
  implementing).
- `restore_cmd` (in `cli_backup.py`) needs a typed phrase that no flag can bypass whenever the
  target is non-empty. **Every replica refresh after the first is therefore interactive.** The guide
  says so, and nothing here changes that.

## 3. Design

### 3.1 New leaf: `src/brain/backup/replica_marker.py` (≈150 lines, cap 200)

This leaf holds the marker's whole format, so `restore.py` gets only a call and `doctor_replica.py`
gets only a reader.

```python
MARKER_NAME = "restored-from.json"
MARKER_MAX_BYTES = 4096          # a real marker is ~150 bytes
ARCHIVE_NAME_MAX = 255

@dataclass(frozen=True)
class RestoredFrom:
    created_at: datetime        # the manifest's, tz-aware
    archive_name: str           # basename only, sanitised
    restored_at: datetime       # when this restore finished (D2)

def marker_path(brain_home: Path) -> Path: ...                  # brain_home / "state" / MARKER_NAME
def write_marker(brain_home: Path, manifest: BackupManifest, archive: Path,
                 *, now: datetime) -> Path: ...
def read_marker(brain_home: Path) -> RestoredFrom | None: ...   # None == no file
class ReplicaMarkerError(BackupError): ...                      # fixed messages, no paths
```

- **Write:**
  - `state/` is created with mode `0o700`, or refused if it is a symlink or not a directory.
  - The marker is written to a sibling temp file opened `O_CREAT|O_EXCL|O_WRONLY|O_NOFOLLOW`,
    mode `0o600`, then `fsync` and `os.replace` onto `restored-from.json`.
  - `os.replace` replaces a symlink at the destination rather than following it, so a planted link
    cannot redirect the write.
  - The payload is `{"schema": 1, "created_at": iso, "archive": name, "restored_at": iso}`.
- **Archive name:** `archive.name` only, never the full path, because a path can carry a username or
  a mount point. Control and format characters (`unicodedata.category` C*) are stripped, which
  covers ANSI escapes and bidi overrides. The result is capped at 255 characters.
- **Read:**
  - The file is opened `O_RDONLY|O_NOFOLLOW` and `fstat` must show a regular file of at most
    `MARKER_MAX_BYTES`.
  - The JSON must be an object. `created_at` and `restored_at` must parse with `fromisoformat` and
    be tz-aware. `archive` must be a str, and it is re-sanitised on read.
  - Unknown extra keys are ignored, for forward compatibility.
  - A missing file returns `None`. Any other fault raises `ReplicaMarkerError("restored-from marker
    is unreadable")`, with a fixed string that has no path and no file content.

### 3.2 `restore.py` change (≤ 10 lines of the +15 budget)

After the post-restore init block and before the `rmtree`, when `db_restored or vault_restored`
(see D3), the restore calls `write_marker(cfg.brain_home, manifest, archive, now=now)`.

- **On `OSError` / `ReplicaMarkerError`:** the restore does **not** fail, because the data is
  already swapped in and failing now would misreport a completed restore. It reports a step,
  `could not record restored-from marker (<fixed reason>)`, through `on_step`, so nothing is
  swallowed silently. See D4.
- **When the marker is written:** only after a successful restore. A preflight refusal, a
  verification failure or a `RestoreAborted` raises before this line, so an earlier marker is left
  untouched.
- **Exposure:** the marker path is not added to `RestoreReport` (YAGNI). `brain restore --json`
  output is unchanged.

### 3.3 New leaf: `src/brain/doctor_replica.py` (≈60 lines)

`replica_doctor_check(*, brain_home: Path | None = None) -> list[_DoctorCheck]`. `brain_home` is a
test seam whose default is `_brain_home_root()`, the same root `cfg.brain_home` defaults to. The row
name is `"replica"`.

| State | Row |
|---|---|
| No marker | **No row** (AC2). |
| Valid marker | `ok`: "read replica, restored from a backup taken at 2026-10-30 18:15 UTC (brain-backup-….tar.gz); writes here are lost at the next restore". |
| Unreadable marker | `warn`: fixed detail "restored-from marker is unreadable". The remedy names the literal string `$BRAIN_HOME/state/restored-from.json` (re-run `brain restore`, or remove the file on a primary), never the resolved path. |

`doctor_runtime.py` changes by 3 or 4 lines: two docstring lines, one deferred import and one
compose line. That takes it to about 603 of its 623 budget.

### 3.4 Guide: `docs/guides/second-machine.md` (≈130 lines)

1. **Pick one of two shapes.** (a) **One brain, reached from elsewhere:** `brain mcp serve` (RM-46,
   read-only by default; links `mcp-http-transport.md`) or `brain ui --lan`
   (`brain-ui-on-your-phone.md`). (b) **A read replica.** Why there is no two-way sync (D1, one
   paragraph).
2. **Replica recipe.**
   - On the primary, run `brain backup`. Copy the archive **and** its `.sha256` sidecar over an
     encrypted channel (`scp`/`rsync -e ssh`).
   - On the replica, install, run `brain setup` with the **same `BRAIN_EMBEDDER`** (restore refuses
     a mismatch), then `brain init`, `brain restore <archive>` and `brain doctor`.
3. **Refresh.** Repeat the restore. Because the target is now non-empty, every refresh asks for the
   typed phrase, so it cannot run from cron. Each refresh parks a `<db>_replaced_<ts>` database and
   a pre-restore archive, and the guide shows how to drop and prune them.
4. **Read-only discipline.** Do not ingest, edit, capture or run the vault watcher or ingest
   launchd daemons on the replica. Anything written there is lost at the next restore. Never restore
   a replica's archive onto the primary.
5. **Security.** The replica holds the whole corpus, including `confidential` rows, which the
   `brain mcp serve` path excludes by default. Keep it on an encrypted disk (FileVault). Treat the
   archive like the corpus: no unencrypted cloud drives, delete copies after restoring.
6. **The `replica` doctor row,** and what to do on a primary that was restored from a backup for
   disaster recovery (D1).

### 3.5 Other docs

- `docs/README.md`: one row for the guide.
- `docs/cli-reference.md`: the `brain backup / brain restore` section gets the marker paragraph and
  the doctor row.
- `CHANGELOG.md`: an `[Unreleased]` entry.
- `skills/brain-maintenance`: `SKILL.md` is at its 300-line cap, so a new
  `reference/second-machine.md` is added. A pointer goes into `SKILL.md` only by replacing an
  existing line, at net zero; otherwise the reference stays discoverable from the existing
  reference listing (to verify against the skills tests).
- No `docs/configuration.md` change, because no setting is added.

## 4. Tests (TDD: red first, each test run under the lock plus a disk check)

### 4.1 `tests/test_replica_marker.py` (nodb)

Each guard has a "refuses" test and a "keeps ordinary behaviour" test.

| # | Refuses | Keeps ordinary |
|---|---|---|
| 1 | — | `write_marker` stores the manifest's exact `created_at` (round-trips equal), `archive` = basename of a nested path, and `restored_at` = `now` (AC1 at unit level). |
| 2 | A full path never reaches the file: `archive` holds no `/` and no parent directory names. | A plain `brain-backup-20261030-181500.tar.gz` name is stored unchanged. |
| 3 | An archive name with ANSI escapes, a bidi override and a NUL has them stripped. | A name with spaces and non-ASCII letters is kept verbatim. |
| 4 | A name over 255 characters is truncated to 255. | A name of exactly 255 is kept. |
| 5 | The marker is mode `0o600` and `state/` is `0o700`. | — |
| 6 | `state/` as a symlink → `ReplicaMarkerError`, and the link target directory is unchanged. | A pre-existing real `state/` (holding voice-failure files) is reused, and its other files are untouched. |
| 7 | `restored-from.json` as a symlink to a file elsewhere → that target is **not** written, and the link is replaced by a regular file. | A pre-existing regular marker is overwritten with the new values. |
| 8 | — | No temp file is left in `state/` after a write. |
| 9 | Read: a symlinked marker → unreadable. | Read: a regular marker → `RestoredFrom`. |
| 10 | Read: over 4096 bytes → unreadable. | Read: a marker with extra unknown keys → parsed. |
| 11 | Read: invalid JSON, a non-object, a missing key, a wrong type, an unparseable or naive `created_at` → unreadable, and the message holds no path and no file content. | Read: a missing file → `None`. |
| 12 | Read: an archive name hand-edited to carry an escape is re-sanitised. | — |

### 4.2 `tests/test_doctor_replica.py` (nodb)

- No marker → `[]` (AC2, absent).
- Valid marker → one `ok` row whose detail contains the formatted `created_at` and the archive name
  (AC2).
- Unreadable marker → `warn` with the fixed detail. Neither the resolved path nor any file bytes
  appear in `detail`, `remedy` or `lines`. The row is a WARN, never a FAIL (`lines[0].err` is
  false).
- In `test_doctor_runtime.py`, a composition test with a marker in a temp `BRAIN_HOME` shows the
  `replica` row in `runtime_doctor_checks()`. The existing pinned-names test stays unchanged and
  green, since it has no marker.

### 4.3 Restore integration (`tests/test_restore_swap.py`, the real restore sandbox on the test DB)

This uses the module's existing fixtures (`archive`, `cfg`, `runner`, `_restore`). The file is 395
lines, with room to spare.

- A successful restore → `cfg.brain_home/state/restored-from.json` exists, and its `created_at`
  equals `read_manifest(archive).created_at` (**AC1**). Its `archive` equals `archive.name`.
- The verification-failure path (the existing test's setup) → no marker is written. A pre-seeded
  marker stays byte-identical. This is the refuses / keeps pair for "only after success".
- `--db-only` (`vault_leg=False`) → marker written (D3).
- A marker write failure (a regular **file** named `state` under `brain_home`) → the restore still
  returns `db_restored=True`, and the step list carries the fixed "could not record" message (D4).

### 4.4 CLI end-to-end (`tests/test_cli_backup.py` or a doctor CLI test)

`brain doctor` with `BRAIN_HOME` pointed at a temp directory holding a synthetic marker prints the
`replica` line, and prints no such line without it. Whether this is cheap through the existing CLI
doctor fixtures is checked while implementing. If it needs a live DB it is skipped, because §4.2
plus the composition test already cover it.

### 4.5 The named set on every run

`tests/test_log_privacy_rules.py`, `tests/test_log_privacy.py`, `tests/test_import_hygiene*.py`,
`tests/test_vault_delete.py`, `tests/test_cli_registry*.py`, every src-wide rglob scan, the skills
tests, and `scripts/check_ceiling_table.py`. Also `ruff check`, `mypy src/`, and coverage on the new
modules (95% or more, since they are pure logic).

### 4.6 AC3: the guide's recipe end to end on the demo sandbox

This follows the recommended option of D5. Everything runs on the **test server (5434)**, never
55432, with two temporary `BRAIN_HOME`s and two throwaway databases
(`second_brain_test_rm47_primary`, `second_brain_test_rm47_replica`):

1. `brain demo --database-url <primary>` seeds the synthetic demo corpus.
2. `brain backup` runs on the primary.
3. `brain init` runs on the empty replica DB, then `brain restore <archive> --yes`. The target is
   empty, so there is no phrase.
4. `brain doctor` shows the `replica` row with the archive's `created_at`.
5. `brain search` on the replica returns the demo hit.
6. Refresh: a second `brain backup` and `brain restore`. This one shows the phrase gate (fed on
   stdin), and the marker's `created_at` advances.

The transcript, made of synthetic data only, goes in the PR body. Afterwards the two throwaway
databases and any `_replaced_` or `_restore_` leftovers are dropped by exact name. Docker,
`brain demo teardown` and `down -v` are never used.

## 5. Line budgets

| File | Now | After | Budget / rule |
|---|---|---|---|
| `backup/restore.py` | 755 | ≤ 765 | +15 (spec §2.2). Stays under 800, with no ceiling row. |
| `doctor_runtime.py` | 599 | ≤ 604 | 623 (563 + 60). |
| `backup/replica_marker.py` | new | ≤ 200 | < 800 |
| `doctor_replica.py` | new | ≤ 80 | < 800 |
| `tests/test_restore_swap.py` | 395 | ≤ 480 | < 800 |
| `tests/test_doctor_runtime.py` | 618 | ≤ 640 | < 800 |

No file crosses 800, so `docs/ceiling-table.md` is not touched. `check_ceiling_table.py` is run to
prove it.

## 6. Security

- **Untrusted input:** the marker file on read, since anything on disk could have edited it, and the
  archive file name, which the user controls and which flows into terminal output. The read is fd
  based with `O_NOFOLLOW`, a regular-file check and a 4 KiB cap. The JSON is schema-checked, and
  names are sanitised on both write and read.
- **Filesystem writes:** one file, written atomically with `O_EXCL|O_NOFOLLOW` on the temp file,
  modes 0600 and 0700, and refused when the directory is a symlink.
- **Error strings:** fixed text, with no resolved paths and no file content (the house rule).
- **Confidential egress:** the marker holds no corpus content: two timestamps and a file name. The
  doctor row reads no database. No `documents` read is added, so the F6 residue audit set is
  unchanged; `grep` re-derives it to prove that.
- **Network, subprocesses, parsers:** none are added.
- **The replica itself:** it holds the whole corpus, confidential rows included. The guide gives the
  encrypted-disk and archive-handling rules, and contrasts them with `brain mcp serve`, whose
  confidential lenses default to exclude.
- **Review:** `ecc:security-reviewer` on opus, per the standing pattern for filesystem-writing PRs.

## 7. Risks

1. **A user writes to the replica.** The guide, the doctor row text ("writes here are lost at the
   next restore") and the refresh section all say it is read-only. The spec accepts this risk.
2. **A primary restored for disaster recovery shows "read replica" forever** (see D1).
3. **Importing `brain.backup.replica_marker` from the doctor runs `brain/backup/__init__.py`**,
   which imports psycopg and the restore module. Doctor already imports `brain.backup` through
   `cli_backup.backup_doctor_checks`, so the cold-import cost is unchanged. The leaf is imported
   inside the function, as doctor_runtime's other leaves are. `test_import_hygiene*` will confirm.
4. **The AC3 demo run on 5434** needs host or container `pg_dump`/`pg_restore` selection to pick the
   5434 container. `resolve_pg_tool` discovers a container by its published port, so this should
   work; if it does not, I report back rather than work around it.
5. **Pinned lists:** the doctor row-name pin (`test_doctor_runtime.py:586`) is unaffected without a
   marker. `grep` checks for any other pin on doctor row names or `brain/backup` module lists.

## 8. Decision points

- **D1: what the row says on a machine restored for disaster recovery.** The marker is written on
  *every* successful restore, including a primary recovering from its own backup, which is not a
  replica.
  - (a) Write it always, and word the row "read replica, restored from a backup taken at X (archive
    N); writes here are lost at the next restore". The guide and the WARN remedy say a recovered
    primary removes `$BRAIN_HOME/state/restored-from.json`.
  - (b) Add an opt-in `brain restore --replica` that alone writes the marker. This is a CLI change
    outside the spec's scope, and a replica set up without the flag shows nothing.
  - (c) Clear the marker on the next local write, such as ingest. That touches many write paths and
    is far out of scope.
  - **Recommendation: (a).** It is the spec's design and wording verbatim, with the one-line
    disaster-recovery note in the guide.
- **D2: marker fields.** The spec names `created_at` and the archive file name.
  - (a) Exactly those two.
  - (b) Add `restored_at` and a `schema: 1` version key. That costs about two lines, and lets the
    row and a future staleness check say how old the replica is without guessing.
  - **Recommendation: (b).** The row still names `created_at` as AC2 asks.
- **D3: which restores write the marker.**
  - (a) Any successful restore that restored at least one leg (db or vault).
  - (b) Only when the database leg ran.
  - **Recommendation: (a).** A `--vault-only` restore also makes the vault a copy of the archive,
    and treating that as "replica-shaped" is the conservative reading.
- **D4: marker write failure after a successful swap.**
  - (a) Report it as a step and carry on.
  - (b) Raise. That would make a completed restore exit 1, and the user could retry a destructive
    operation needlessly.
  - **Recommendation: (a),** with a test that pins the reported step.
- **D5: how AC3 ("works end to end on the demo sandbox") is demonstrated.**
  - (a) The Docker `brain demo` sandbox on 55433. Its teardown is `docker compose down -v`, which my
    standing rules forbid.
  - (b) `brain demo --database-url` seeding throwaway databases on the 5434 test server, run by
    hand, with the transcript in the PR.
  - (c) (b) plus an automated pytest for the same flow. That is heavy, and duplicates the restore
    integration tests, which already fake `pg_restore`.
  - **Recommendation: (b).**
- **D6: staleness (an optional extra).** Should the row turn WARN when the replica's backup is older
  than `STALE_BACKUP_DAYS` (which `last backup` already uses), with "refresh with brain restore" as
  the remedy?
  - **Recommendation: no.** It is out of the spec's scope, and a deliberately frozen replica would
    WARN forever. It can be added later from `restored_at`.

## 9. Task order (for execution after approval)

1. `replica_marker.py` plus `test_replica_marker.py` (red, then green).
2. The `restore.py` call plus the `test_restore_swap.py` additions.
3. `doctor_replica.py` plus the compose line, `test_doctor_replica.py` and the composition test.
4. Docs: the guide, the README row, cli-reference, CHANGELOG and the skill reference.
5. The named set, `ruff`, `mypy`, coverage, the ceiling check and the F6 grep.
6. The AC3 manual run (D5), with the transcript recorded.
7. The review and audit loop (CLAUDE.md §14) plus `ecc:security-reviewer` on opus.
8. Push and open the PR, but only within this ticket's flow and on the coordinator's go.

## 10. Deviations found during implementation (approved by the coordinator mid-ticket)

The AC3 end-to-end run (D5) found two defects in `brain restore` that predate RM-47 and that the
recipe cannot work around. Both are fixed in this PR.

1. **AGE restore failure.** `pg_dump --exclude-schema=ag_catalog` still dumps the `age`
   EXTENSION entry, which `pg_restore --exit-on-error` replays as `CREATE EXTENSION IF NOT EXISTS
   age WITH SCHEMA ag_catalog`. That fails on a fresh staging database (`schema "ag_catalog" does
   not exist`), so every real restore of an AGE brain aborted before the swap. The fix is a new
   leaf, `backup/staging_prep.py` (`prepare_staging_extensions`). It creates each extension in
   `STAGING_EXTENSIONS = ("age",)` that the server offers, in the staging database before
   `pg_restore`, then commits. `restore.py` gains 3 lines. The regression test runs a runner that
   records the staging database's extensions when `pg_restore` starts; the pair covers an
   extension the server does not offer, and the failure path is a fixed `BackupError`.
2. **Preflight label.** "WILL BE OVERWRITTEN: • database X" printed the archive's
   `database_name`, not the target's. `_print_preflight` now takes `target_database`, taken from
   `cfg.database_url`.

Line budget after both: `backup/restore.py` 755 → 767, which is +12 of its +15 (spec §2.2).

### 10.1 The coordinator's follow-ups to fix A

These ran on throwaway databases on 5434 only. Every step printed and asserted its host and port,
and the databases were dropped by exact name afterwards.

- **Graph data.** The primary held the demo corpus with embeddings and HNSW, plus
  `graphrag build --backfill`: 66 entities, 84 mentions, 11 relationships, and AGE with 87
  vertices and 95 edges over 4 labels.
  - After the restore, all 7 `graph_*` tables matched row for row.
  - The AGE mirror is empty after the restore. That is by design: `EXCLUDED_SCHEMAS =
    ("ag_catalog", "brain_graph")`, F3 §5.2.
  - `brain graphrag build --force` rebuilt it exactly: 87 vertices, 95 edges, the 4 labels, and
    `graph drift OK`.
  - Nothing in `public` depends on `ag_catalog`. No migration references `agtype` or `ag_catalog`,
    and the dump has no such object entries.
- **Other defects of the same kind:** none found.
  - vector: the extension, NOT NULL and HNSW restore.
  - Owner statements are skipped by `--no-owner`, and there are no GRANT entries.
  - `search_path=''` in the dump breaks no index build.
- **Existing archives.** The backup side is unchanged by this PR. An archive taken by master's
  code fails master's restore and succeeds with this branch's.
- **Regression test.** `tests/test_restore_real_pg_tools.py` runs a real `pg_dump`→`pg_restore`
  through production's container discovery.
  - Source: a throwaway database. Target: the restore sandbox.
  - It fails on master with the production error, as a mutation run in a git-archive copy shows.
  - Its pair, a source without `age`, passes.
  - Locally it is skipped without `docker`. Under GitHub Actions a missing `docker` is an error,
    not a skip, so CI cannot pass it vacuously. The CI log at `f881bbe3` shows
    `tests/test_restore_real_pg_tools.py ..` (both passed) and no SKIPPED entry for it.

### 10.2 The sec-119 / rev-119 fix round

| Item | Change | Tests |
|---|---|---|
| sec L1, L2, rev #1 | `read_marker` catches `RecursionError` and `OverflowError`. `_aware` range-checks with `astimezone(UTC)`. `_read_capped` turns any `OSError` from `fstat` or `read` into the fixed error and closes the fd on every path (`_read_all`). | `test_replica_marker.py`: deep nesting, out-of-range min/max for both timestamps, an edge-of-range keep, and `fstat`/`read` `OSError` with an fd-leak check |
| Defence in depth | `replica_doctor_check` never raises. Any failure, expected or not, becomes the fixed WARN. | `test_doctor_replica.py`: out of range, deep nesting, an unexpected `RuntimeError`, and a valid marker still OK |
| sec L3 | `.gitignore` gains `state/` beside `run/`. | `git check-ignore -q state/restored-from.json` |
| sec L4 | Guide: an encrypted channel (`scp`/`ssh`), and a LAN share (SMB, NFS) is cleartext. The sidecar catches damage, not tampering, so restore only your own archives. Incoming dir `chmod 700`, archive `chmod 600`. The skill reference mirrors this. | docs |
| rev #2 | Guide and skill: `"${BRAIN_HOME:-$HOME/.brain}/state/restored-from.json"`. Doctor: prose, "remove restored-from.json from $BRAIN_HOME/state (~/.brain/state by default)". `config._brain_home_root` resolves `$BRAIN_HOME`, then the dev-checkout repo root, then `~/.brain`. | `test_doctor_replica.py` (`MARKER_PROSE`) |
| rev #3, sec I2 | `schema` must be exactly the int `MARKER_SCHEMA` (a bool is refused). The docstring says so. | `schema` 2, 0, "1", True and missing are refused; 1 reads |
| sec I4 | The staging prep runs only when the dump's TOC (`pg_restore -l`, no DB connection) has an `EXTENSION - age` entry. `_restore_into` moved, unchanged apart from the TOC step, to the new leaf `backup/restore_run.py`, so `restore.py` is now 690 lines (it was 755 at `01c58a72`). | `test_restore_toc.py` (the parser and its look-alike lines). `test_restore_swap.py`: an AGE TOC gets `age`, read before the data restore; a plain TOC on an AGE-capable server gets no extension. `test_restore_real_pg_tools.py`: both pairs probe staging's extensions at real `pg_restore` time. |
| INFO | `MARKER_UNREADABLE` is a public constant, and the doctor imports it. | — |

**Follow-up for the backlog (sec-119 I6, predates this PR).** The restore preflight still echoes
archive-controlled manifest strings unfiltered (`brain_version`, `migration_head`, `embedder`,
`embedding_column_type`, `postgres_version`). A crafted manifest could put ANSI or OSC sequences on
the terminal just before the destructive gate. The fix is to send them through a shared
control-character stripper, lifting `replica_marker._display_name` into a utility.
