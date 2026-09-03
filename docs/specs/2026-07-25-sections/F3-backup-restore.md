# F3 — `brain backup` / `brain restore`

> Design section of `docs/specs/2026-07-25-agent-memory-safety-ui-design.md`.
> Global constraints (PII, production safety, quality gates, style) are inherited from
> section 4 of that document and are not restated here.

## `brain backup` and `brain restore` — durable, verifiable snapshots

### 1. Goal

Give the user two first-class commands that make the brain recoverable: `brain backup` produces a single timestamped, checksummed archive containing a Postgres dump, the vault tree, and a self-describing `manifest.json`; `brain restore` puts that archive back with a preflight that refuses every incompatible combination *before* touching anything, a typed-phrase gate that `--yes` cannot bypass, an automatic pre-restore backup, and a restore-into-a-staging-database-then-rename swap so the previous database still exists after a successful restore. This matters because this project has already suffered one accidental production wipe (`tests/conftest.py:38-45` exists solely because of it, and CLAUDE.md carries a hard never-destroy-prod rule), yet today the *only* recovery story is a copy-pasteable `docker exec … pg_dump` line buried in planning docs (`docs/plans/2026-05-06-search-ranking-fix.md:513`, `docs/graphrag.md:120`). Recovery must be a command, not a recipe.

### 2. Current state

**What exists.**

- **No backup or restore code at all.** `grep -rn "def backup\|def restore\|\"backup\"" src/brain/*.py` returns nothing. There is no `backup` Typer command in `src/brain/cli.py` (9,760 lines, sub-apps registered at `cli.py:237-328`).
- **Documentation-only recipes.** `docs/graphrag.md:120` and `docs/plans/2026-05-26-graphrag-readme-defaults.md:621` both show `docker exec second-brain-postgres pg_dump -U brain -Fc -d second_brain …`. `docs/plans/2026-05-11-improvement-roadmap.md:297` explicitly names the gap: "`brain backup --to /path/file.tar.gz` does pg_dump + vault snapshot. `brain restore` does the reverse."
- **`.gitignore:16` already ignores `backups/`** — a prior manual convention, unused by code.
- **The compose seam is ready.** `brain._compose.compose_cmd()` (`_compose.py:30-52`) forces `-f <BRAIN_HOME>/docker-compose.yml` + `--project-name`; `compose_project_name()` (`_compose.py:20-27`) resolves `$BRAIN_COMPOSE_PROJECT`. The postgres container name is derived by `setup._container_name_for_project()` (`setup.py:482-493`) — `second-brain-postgres` for the default project, `<project>-postgres` otherwise.
- **A destructive-gate precedent exists.** `uninstall.run_uninstall` prints an itemized destruction list (`uninstall.py:78-92`), calls `typer.confirm("Proceed?", default=False, abort=True)` unless `--yes` (`uninstall.py:97-98`), and for `--remove-db` demands the exact typed phrase `yes, delete my data` **regardless of `--yes`** (`uninstall.py:103-109`). It also uses a dependency-injected `_launchd_uninstall` callable for testability (`uninstall.py:51`, `uninstall.py:117-121`).
- **Docker subprocess translation precedent.** `demo._run_docker` (`demo/__init__.py:266-292`) maps `FileNotFoundError` / `CalledProcessError` / `TimeoutExpired` into one actionable `DemoError` (`errors.py:228`) carrying Docker's own stderr. `demo._run` (`demo/__init__.py:257-263`) uses `check`, `capture_output=True`, `text=True`, `timeout=_SUBPROCESS_TIMEOUT_S`.
- **Everything the manifest needs is already queryable.** `db.run_migrations` (`db.py:235-274`) tracks applied files in `schema_migrations(name TEXT PRIMARY KEY, applied_at)` (`db.py:163-168`) and applies `sorted(migrations_dir().glob("*.sql"))` in name order (`db.py:245`, `db.py:146-160`). `db.ensure_embedding_column` (`db.py:412-478`) reconciles `chunks.embedding`'s declared dim against `embedder.dim` via `db._current_embedding_dim` (`db.py:380-410`, parses `format_type(atttypid, atttypmod)` → `vector(N)`). `queries.embedding_column_state` (`queries.py:896-915`) returns `EmbeddingColumnState(column_type, not_null, has_index)`. `queries.summary_counts` (`queries.py:578-620`) returns `StatusCounts(documents, chunks, sources, last_ingest, by_kind)` (`queries.py:565-575`) in one round-trip. `embeddings.make_embedder(cfg)` (`embeddings.py:392-423`) yields the active `Embedder` (with `.dim`).
- **Post-restore hygiene is already half-built.** `cli._check_chunks_stats` (`cli.py:887-965`) warns `chunks stats WARN — never analyzed … This can happen after pg_restore — planners use default estimates until ANALYZE runs`, remedy `brain analyze`. That command exists at `cli.py:1757-1795` and calls `queries.analyze_tables` (`queries.py:1174-1188`) with identifiers quoted via `psycopg.sql.Identifier`.
- **The AGE mirror is already declared recomputable.** `brain graphrag build --force` (`cli.py:2537`, flag help at `cli.py:2551-2559`) is documented as "the recovery path for a dropped or corrupted AGE mirror … re-reconcile EVERY document from the relational source-of-truth". That source of truth is `graph_entities` / `graph_entity_mentions` / `graph_edge_contributions` / `graph_relationships` / `graph_index_state` (`migrations/012_graphrag.sql:59,85,104,128,157`) and `graph_communities` / `graph_community_members` (`migrations/013_graphrag_communities.sql:60,102`) — all ordinary tables in `public`.
- **Test prod-guard.** `tests/conftest.py:45-46` defines `_PROD_PORTS = {5433, 55432}` and `_PROD_DB_NAME = "second_brain"`; `_looks_like_prod_db(host, port, dbname)` (`conftest.py:50-59`) and `_assert_not_prod_db` (`conftest.py:62-72`) abort loudly. `tests/test_conftest_prod_guard.py` exercises it. The `test_db` fixture is at `conftest.py:371-388`; `@pytest.mark.fresh_schema` triggers `_reset_schema_and_migrate` (`conftest.py:256+`).
- **A trap I must design around:** `conftest.py:259-268` documents that `DROP SCHEMA public CASCADE` **orphans the `vector` and `pgcrypto` extension rows in `pg_extension`**, so a later `CREATE EXTENSION IF NOT EXISTS vector` silently no-ops and leaves the schema without the `vector` type. The test harness works around it with `ALTER EXTENSION vector SET SCHEMA pg_catalog` (`conftest.py:285-286`). Restore must therefore **not** use drop-schema-in-place.

**What is missing:** the commands, the archive format, the manifest, the version-compatibility resolution between the host `pg_dump` (14.23) and the containerized server (16.14), the restore gate, the pre-restore safety net, and every test.

**Assumption I checked and found false:** there is no existing `Config.vault_root` field — the config field is `Config.vault_path` (`config.py:531`), resolved by `_default_vault_path()` (`config.py:489-503`) from `$BRAIN_VAULT_PATH` else `DEFAULT_VAULT_PATH = Path.home() / "brain-vault"` (`config.py:92`). `vault_root` is only a *keyword argument name* on `ingest_document` (`ingest/__init__.py:432`), always passed as `vault_root=cfg.vault_path` (`cli.py:2062`). This spec uses `cfg.vault_path`.

### 3. User-visible surface

Two new top-level commands. Nothing existing changes, so there is **zero backward-compatibility risk to existing output**: no current command's stdout, JSON shape, or exit code is touched. The one shared-surface change is additive — `brain doctor` gains one new soft check line (`last backup`), appended to the report via the existing `_DoctorReport.record` mechanism (`cli.py:566-592`), which never flips doctor's exit code and appears as one more element in the existing `--json` array rather than a new top-level key.

#### `brain backup`

```
brain backup [--out DIR] [--no-vault] [--label TEXT] [--json]
```

| Flag | Type | Default | Help text |
|---|---|---|---|
| `--out`, `-o` | `Path` | `$BRAIN_HOME/backups` | `Directory to write the archive into. Created if missing.` |
| `--no-vault` | `bool` | `False` | `Skip the vault tree; back up the database only.` |
| `--label` | `str` | `""` | `Short label folded into the filename and recorded in the manifest (e.g. 'pre-upgrade'). Letters, digits, '-' and '_' only.` |
| `--json` | `bool` | `False` | `Emit the manifest as JSON instead of the human summary.` |

Archive name: `brain-backup-<YYYYMMDD-HHMMSS><-label>.tar.gz`, plus a sidecar `<same>.tar.gz.sha256`.

Human output (literal sample):

```
🧠 brain backup

  database    second_brain @ localhost:55432 (PostgreSQL 16.14)
  pg_dump     16.14 (in container second-brain-postgres)
  vault       /Users/example/brain-vault  (1,207 files)

  [ok]     dumped 1,195 documents / 18,342 chunks / 903 sources  (205.0 MiB)
  [ok]     archived vault  (39.4 MiB)
  [ok]     wrote manifest  (head 023_search_queries_fts_count.sql, arctic/1024)

  archive   /Users/example/.brain/backups/brain-backup-20260725-141203-pre-upgrade.tar.gz
  size      244.6 MiB
  sha256    3f9c1ad2e8b47c05…  (also written to <archive>.sha256)

Restore with:
  brain restore /Users/example/.brain/backups/brain-backup-20260725-141203-pre-upgrade.tar.gz
```

`--json` emits the manifest object verbatim plus three archive-level keys, through the existing `format.emit_json` (`format.py:35-37`):

```json
{
  "schema": 1,
  "archive_path": "/Users/example/.brain/backups/brain-backup-20260725-141203-pre-upgrade.tar.gz",
  "archive_bytes": 256498176,
  "archive_sha256": "3f9c1ad2e8b47c05...",
  "created_at": "2026-07-25T14:12:03.482119+00:00",
  "label": "pre-upgrade",
  "brain_version": "0.2.1",
  "postgres_version": "16.14",
  "postgres_version_num": 160014,
  "pg_dump_version": "16.14",
  "pg_dump_source": "container",
  "container_name": "second-brain-postgres",
  "database_name": "second_brain",
  "dump_format": "custom",
  "dump_excluded_schemas": ["ag_catalog", "brain_graph"],
  "migration_head": "023_search_queries_fts_count.sql",
  "migration_count": 23,
  "embedder": "arctic",
  "embedding_dim": 1024,
  "embedding_column_type": "vector(1024)",
  "embedding_not_null": true,
  "embedding_has_index": true,
  "counts": { "documents": 1195, "chunks": 18342, "sources": 903 },
  "graph_entities": 6595,
  "vault_included": true,
  "vault_path": "/Users/example/brain-vault",
  "vault_file_count": 1207,
  "files": {
    "db/second_brain.dump": { "bytes": 214958080, "sha256": "a11b..." },
    "vault.tar":            { "bytes":  41287680, "sha256": "c07d..." }
  }
}
```

Key types: `schema` int; `archive_bytes`/`postgres_version_num`/`migration_count`/`embedding_dim`/`vault_file_count` int; `graph_entities` `int | null` (null when the graph tables are absent); `created_at` ISO-8601 UTC string; `label`/`brain_version`/`postgres_version`/`pg_dump_version`/`database_name`/`dump_format`/`migration_head`/`embedder`/`embedding_column_type`/`archive_sha256` string; `pg_dump_source` string ∈ `{"container","host"}`; `container_name` `str | null` (null when `pg_dump_source == "host"`); `dump_excluded_schemas` array of string; `vault_included`/`embedding_not_null`/`embedding_has_index` bool; `vault_path` `str | null`; `counts` object of int; `files` object mapping archive-relative path → `{bytes: int, sha256: str}` (the `vault.tar` entry is absent when `vault_included` is false).

Exit codes: `0` success; `1` failure (dump failed, disk full, Docker down); `2` `typer.BadParameter` (bad `--label` characters, `--out` is an existing file).

#### `brain restore`

```
brain restore ARCHIVE [--yes] [--db-only] [--vault-only] [--json]
```

| Flag | Type | Default | Help text |
|---|---|---|---|
| `ARCHIVE` | `Path` (arg, required) | — | `Path to a brain-backup-*.tar.gz produced by 'brain backup'.` |
| `--yes` | `bool` | `False` | `Skip the y/N confirmation. NEVER skips the typed-phrase gate when the target database or vault is non-empty.` |
| `--db-only` | `bool` | `False` | `Restore the database only; leave the vault on disk untouched.` |
| `--vault-only` | `bool` | `False` | `Restore the vault only; leave the database untouched.` |
| `--json` | `bool` | `False` | `Emit a machine-readable result object instead of the human transcript.` |

`--db-only` + `--vault-only` together → `typer.BadParameter` (exit 2).

Human output before the gate (literal sample, non-empty target):

```
🧠 brain restore

  archive     brain-backup-20260725-141203-pre-upgrade.tar.gz
  taken       2026-07-25 14:12:03 UTC  by brain 0.2.1
  contains    1,195 documents / 18,342 chunks / 903 sources / 1,207 vault files
  schema      023_search_queries_fts_count.sql   (installed head: 023_search_queries_fts_count.sql — match)
  embedder    arctic / vector(1024)              (active: arctic / vector(1024) — match)
  postgres    dumped from 16.14                  (target server: 16.14 — ok)
  checksums   OK (archive + 2 members verified)
  disk        needs 733 MiB, 214 GiB free — ok

WILL BE OVERWRITTEN:
  • database second_brain @ localhost:55432
      currently holds 1,190 documents / 18,201 chunks / 901 sources
  • vault /Users/example/brain-vault
      currently holds 1,203 files  (moved aside to /Users/example/brain-vault.replaced-20260725-181500)

A pre-restore backup will be taken first, into:
  /Users/example/.brain/backups/brain-backup-20260725-181500-pre-restore.tar.gz

Proceed? [y/N]:
Type "restore and overwrite my brain" to confirm:
```

Then the transcript:

```
  [ok]     pre-restore backup  (244.1 MiB)  ← undo with: brain restore <that path>
  [ok]     created staging database second_brain_restore_20260725_181500
  [ok]     pg_restore  (1,195 documents / 18,342 chunks — matches manifest)
  [ok]     terminated 2 other connections (brain-mcp, brain-watcher)
  [ok]     swapped: second_brain → second_brain_replaced_20260725_181500
  [ok]     restored vault  (1,207 files)
  [ok]     brain init  (0 new migrations, chunks.embedding vector(1024))
  [ok]     brain analyze  (chunks)

Done. The previous database is retained as second_brain_replaced_20260725_181500.
  Drop it when satisfied:  docker exec second-brain-postgres psql -U brain -d postgres \
                             -c 'DROP DATABASE second_brain_replaced_20260725_181500'
  Rebuild the graph mirror: brain graphrag build --force
  Verify:                   brain doctor && brain search "…"
```

`--json` result object:

```json
{
  "schema": 1,
  "restored": true,
  "db_restored": true,
  "vault_restored": true,
  "archive_path": "…/brain-backup-20260725-141203-pre-upgrade.tar.gz",
  "manifest": { "…": "the full manifest object above" },
  "pre_restore_backup": "…/brain-backup-20260725-181500-pre-restore.tar.gz",
  "replaced_database": "second_brain_replaced_20260725_181500",
  "replaced_vault_path": "/Users/example/brain-vault.replaced-20260725-181500",
  "documents": 1195,
  "chunks": 18342,
  "follow_up": ["brain graphrag build --force", "brain doctor"]
}
```

`replaced_database` / `replaced_vault_path` are `str | null`. Exit codes: `0` success; `1` failure or user abort (`typer.Abort`); `2` `typer.BadParameter`; **`3` preflight incompatibility** (checksum mismatch, embedder/dim mismatch, archive newer than the installed brain, server too old, insufficient disk) — a distinct code so scripts can tell "won't work" from "broke". This mirrors the existing exit-3 convention documented in CLAUDE.md for `brain eval --fail-below`.

#### `brain doctor` addition

One additional soft check, recorded through the existing report object:

```
last backup     WARN — no backup found in /Users/example/.brain/backups. Run: brain backup
last backup     OK (2026-07-25, 244.6 MiB)
```

### 4. Module layout

| Path | Purpose | Est. lines |
|---|---|---|
| `src/brain/backup/__init__.py` | Package docstring; re-exports `create_backup`, `restore_backup`, `BackupManifest`, `RestoreReport`, `latest_backup`. | 45 |
| `src/brain/backup/manifest.py` | `BackupManifest` frozen dataclass, `FileEntry`, `to_dict` / `from_dict` with strict validation, `MANIFEST_SCHEMA = 1`, `collect_manifest(conn, cfg, embedder, …)`. | 230 |
| `src/brain/backup/pgtool.py` | `PgToolPlan` dataclass; `resolve_pg_tool(tool, cfg, runner)` → container-first / host-fallback with the major-version rule; `server_version(conn)`; `CommandRunner` Protocol + `SubprocessRunner`. | 250 |
| `src/brain/backup/archive.py` | `write_archive(staging, dest)`, `read_manifest(archive)`, `extract_archive(archive, dest)` with the hardened member filter, `sha256_file`, `sha256_stream`, `write_sidecar`, `verify_sidecar`. | 240 |
| `src/brain/backup/create.py` | `create_backup(...) -> BackupResult`: staging dir, dump, vault tar, manifest, atomic rename. | 220 |
| `src/brain/backup/restore.py` | `preflight(...) -> Preflight`, `restore_backup(...) -> RestoreReport`: staging DB, `pg_restore`, verify, terminate, rename swap, vault swap, post steps. | 330 |
| `src/brain/backup/discovery.py` | `latest_backup(dir) -> BackupSummary \| None` — used by `brain doctor` and by restore's "undo" hint. | 70 |
| `src/brain/cli_backup.py` | Typer commands `backup_cmd` / `restore_cmd` + `register_backup_commands(app)`. Human/JSON rendering only. | 260 |
| **Changed** `src/brain/errors.py` | Add `BackupError(BrainError)`, `RestoreIncompatible(BackupError)`, `RestoreAborted(BackupError)`, `PgToolUnavailable(BackupError)`. | +40 |
| **Changed** `src/brain/cli.py` | `from .cli_backup import register_backup_commands` + `register_backup_commands(app)` near `cli.py:328`; add `_check_last_backup` to the doctor report. | +45 |
| **Changed** `src/brain/_compose.py` | Move `_container_name_for_project` here as public `postgres_container_name(project: str \| None = None) -> str`; `setup.py:482` becomes a one-line delegation (DRY — two callers now). | +18 / −10 |

Every file stays well under 800 lines; `cli.py` grows by 45 rather than 500 because the commands live in `cli_backup.py`. **No migration is required — this section allocates no migration number** (`024` belongs to AGENT-MEMORY, `025` to SAFETY).

### 5. Design detail

#### 5.1 Dump format decision

**Custom format (`-Fc`), not plain SQL.** Rationale, specific to this schema:

1. `pg_restore` on a custom archive can be driven with `--exit-on-error`, so a half-applied restore fails loudly instead of a `psql` plain-SQL replay that prints errors and exits `0`. Given the wipe history, silent partial success is the failure mode we most need to eliminate.
2. It is self-describing (`pg_restore -l`), so the restore path can enumerate and verify contents before running.
3. It compresses internally (~4-5× on `chunks.content`), keeping a 1,200-document corpus's dump around 200 MiB rather than ~900 MiB.
4. `vector` columns are unaffected by the choice — pgvector registers a normal type with binary send/recv; the dump emits `CREATE EXTENSION IF NOT EXISTS vector` and the column data as a `COPY`-equivalent data block. The HNSW index created by `queries.finalize_embedding_index` (`queries.py:780-812`) is dumped as a `CREATE INDEX` and rebuilt by `pg_restore` — **no manual index rebuild is needed after restore.**

Exact argv:

```
pg_dump -U <user> -d <dbname> -Fc --no-owner --no-privileges
        --exclude-schema=ag_catalog --exclude-schema=brain_graph
        -f /tmp/brain-backup.dump
```

`--no-owner --no-privileges` so the archive restores into a database owned by whatever role the target uses (a fresh machine may not have the same role graph).

#### 5.2 What happens to Apache AGE graph data — explicitly

**AGE graph data does NOT survive the round trip, by design, and is rebuilt afterwards.**

Apache AGE stores each graph in its own schema (here `brain_graph`) whose label tables inherit from `ag_catalog.ag_label_vertex` / `ag_label_edge`, plus registry rows in the extension-owned tables `ag_catalog.ag_graph` and `ag_catalog.ag_label`. `tests/conftest.py:226-230` documents this exact split ("AGE keeps graphs in the `ag_catalog` schema plus a per-graph schema — neither lives in `public`"). AGE does not register its catalog tables with `pg_extension_config_dump`, so `pg_dump` emits `CREATE EXTENSION age` but **not** the `ag_graph`/`ag_label` rows. Restoring that produces label tables in a `brain_graph` schema with no catalog entries — a graph AGE cannot open, which is strictly worse than no graph. Excluding both schemas is therefore correct, not lossy: the graph's **relational source of truth is entirely inside `public`** (`graph_entities`, `graph_entity_mentions`, `graph_edge_contributions`, `graph_relationships`, `graph_index_state` — `migrations/012_graphrag.sql:59,85,104,128,157`; `graph_communities`, `graph_community_members` — `migrations/013_graphrag_communities.sql:60,102`) and is fully dumped. The mirror is regenerated by the command the codebase already documents as exactly this recovery path: `brain graphrag build --force` (`cli.py:2551-2559`, "the recovery path for a dropped or corrupted AGE mirror when documents and config are unchanged"). Restore prints it as a follow-up rather than running it (it can take minutes on a 1,200-doc corpus and is not required for `brain search` to work). The manifest records `graph_entities` so the user can confirm the rebuild reached the same entity count.

#### 5.3 `pg_dump` / `pg_restore` version resolution

**The rule** (PostgreSQL's own): the dump/restore utility must be **greater than or equal to the server's major version**. A 14.x `pg_dump` against a 16.x server aborts with `server version: 16.14; pg_dump version: 14.23 / aborting because of server version mismatch` — empirically verified on this machine (Homebrew `pg_dump` 14.23 vs the containerized 16.14 server). Minor version is irrelevant; only the major (`160000 // 10000 == 16`) matters.

```python
@dataclass(frozen=True)
class PgToolPlan:
    """How to invoke a pg_dump/pg_restore that is version-compatible with the server."""
    tool: str                    # "pg_dump" | "pg_restore"
    source: str                  # "container" | "host"
    version: str                 # "16.14"
    major: int                   # 16
    container: str | None        # container name when source == "container"
    argv_prefix: list[str]       # e.g. ["docker","exec","--env-file",…,"second-brain-postgres","pg_dump"]

def resolve_pg_tool(
    tool: str,
    *,
    server_major: int,
    container: str,
    runner: CommandRunner,
) -> PgToolPlan: ...
```

Resolution order:

1. **Container first (the default path).** Probe `docker exec <container> <tool> --version`. Parse `pg_dump (PostgreSQL) 16.14` with `re.match(r"^\S+ \(PostgreSQL\) (\d+)\.(\d+)", out)`. If `major >= server_major` → use it. This always holds for the brain's own container (the binary ships with the server).
2. **Host fallback**, only if step 1 fails (Docker daemon down, container stopped, or the user runs against a non-Docker Postgres). Probe `shutil.which(tool)`; if absent → `PgToolUnavailable`. Run `<tool> --version`; if `major < server_major` → `PgToolUnavailable` with the exact remediation:

```
pg_dump on this machine is 14.23 but the server is PostgreSQL 16.14.
pg_dump must be >= the server major version. Either:
  • start the brain container so the matching pg_dump can be used:
      brain-up          (or: docker compose --project-name brain up -d)
  • or install PostgreSQL 16+ client tools:
      brew install postgresql@16
```

3. `server_major` comes from `int(conn.execute("SHOW server_version_num").fetchone()[0]) // 10000`, and the display string from `SHOW server_version`.

**Container invocation** is built through the existing seam rather than ad hoc: the container name comes from the newly-public `_compose.postgres_container_name()` (moved from `setup.py:482-493`, so `$BRAIN_COMPOSE_PROJECT` isolation keeps working), and compose-level operations (only used for the "is the stack up?" probe) go through `compose_cmd("ps", "--quiet", "postgres")` (`_compose.py:30`). The Postgres password is **never** placed in argv — it is written to a `0600` temp file `PGPASSWORD=<value>` and passed via `docker exec --env-file <file>` (verified supported: `docker exec --env-file list`), deleted in a `finally`. Password/user/dbname are parsed from `cfg.database_url` with `urllib.parse.urlparse`, never string-split by hand.

The dump is written to `/tmp/<uuid>.dump` **inside** the container, then `docker cp <container>:/tmp/<uuid>.dump <staging>/db/second_brain.dump`, then `docker exec <container> rm -f /tmp/<uuid>.dump` in a `finally`. Streaming to stdout was rejected: `pg_restore` of a custom-format archive from a non-seekable stdin degrades, and `docker cp` keeps dump and restore symmetric.

Subprocess boundary translation reuses the shape of `demo._run_docker` (`demo/__init__.py:266-292`) — `FileNotFoundError` → "Docker CLI not found…", `CalledProcessError` → exit code + captured stderr, `TimeoutExpired` → timeout message; all three become `BackupError`. Timeouts: `_PROBE_TIMEOUT_S = 30`, `_DUMP_TIMEOUT_S = 3600`, `_RESTORE_TIMEOUT_S = 7200` (module constants, no magic numbers).

#### 5.4 Archive layout and integrity

Staging directory (created as `<out>/.brain-backup-<ts>.partial/`, same filesystem as the destination so the final move is a rename):

```
manifest.json
db/second_brain.dump          pg_dump -Fc output
vault.tar                     uncompressed tar of the vault tree (omitted with --no-vault)
```

The final artifact is `tarfile.open(dest_tmp, "w:gz", compresslevel=6)` over the staging dir, then `os.replace(dest_tmp, dest)` — the same atomic-rename discipline as `vault._atomic.atomic_write_text` (`vault/_atomic.py:6-21`), so an interrupted backup never leaves a plausible-looking truncated `.tar.gz`. The vault is an inner **uncompressed** `vault.tar` so exactly two members need checksums (per-file checksums over 1,200 vault files would bloat the manifest); the outer gzip compresses it.

**Two integrity layers, both verified at restore:**

1. **Per-member**: `manifest.files["db/second_brain.dump"].sha256` and `…["vault.tar"].sha256`, computed streaming (`hashlib.sha256` over 1 MiB chunks — the same `hashlib.sha256` idiom already used at `ingest/__init__.py:127`, `vault/frontmatter.py:116`).
2. **Whole-archive**: `<archive>.tar.gz.sha256`, `sha256sum` format (`<hex>  <basename>\n`), written next to the archive. Catches truncation from a copy to a USB stick or cloud drive. The manifest cannot contain its own archive's hash (circular), which is exactly why the sidecar exists.

**Hardened extraction** (restore reads an attacker-influenceable file): a `_safe_members(tar, root)` generator rejects, per member, any of — absolute path, any `..` path component, `member.issym() or member.islnk()`, `member.isdev()`, or a resolved destination not under `root`. Raises `BackupError` naming the offending member. This is implemented explicitly rather than relying on `tarfile`'s `filter="data"`, which only landed in 3.11.4 while `requires-python = ">=3.11"` (`pyproject.toml:8`).

#### 5.5 Dataclasses

```python
@dataclass(frozen=True)
class FileEntry:
    """One checksummed member inside the archive."""
    bytes: int
    sha256: str

@dataclass(frozen=True)
class BackupManifest:
    """Everything needed to validate a later restore. Serialized as manifest.json."""
    schema: int
    created_at: datetime
    label: str
    brain_version: str
    postgres_version: str
    postgres_version_num: int
    pg_dump_version: str
    pg_dump_source: str
    container_name: str | None
    database_name: str
    dump_format: str
    dump_excluded_schemas: tuple[str, ...]
    migration_head: str
    migration_count: int
    embedder: str
    embedding_dim: int
    embedding_column_type: str
    embedding_not_null: bool
    embedding_has_index: bool
    counts: Mapping[str, int]
    graph_entities: int | None
    vault_included: bool
    vault_path: str | None
    vault_file_count: int | None
    files: Mapping[str, FileEntry]

@dataclass(frozen=True)
class BackupResult:
    archive_path: Path
    archive_bytes: int
    archive_sha256: str
    manifest: BackupManifest

@dataclass(frozen=True)
class PreflightIssue:
    code: str          # "checksum" | "embedder" | "dim" | "migration_head" | "server_version" | "disk"
    fatal: bool
    message: str
    remedy: str

@dataclass(frozen=True)
class Preflight:
    manifest: BackupManifest
    target_documents: int
    target_chunks: int
    target_sources: int
    target_vault_files: int
    required_bytes: int
    free_bytes: int
    issues: tuple[PreflightIssue, ...]

    @property
    def blocked(self) -> bool:
        return any(issue.fatal for issue in self.issues)

@dataclass(frozen=True)
class RestoreReport:
    db_restored: bool
    vault_restored: bool
    pre_restore_backup: Path | None
    replaced_database: str | None
    replaced_vault_path: Path | None
    documents: int
    chunks: int
    follow_up: tuple[str, ...]
```

All frozen; every helper returns a new object rather than mutating inputs.

#### 5.6 Public function signatures

```python
def collect_manifest(
    conn: psycopg.Connection[Any],
    cfg: Config,
    embedder: Embedder,
    *,
    label: str,
    pg_dump_plan: PgToolPlan,
    vault_included: bool,
    vault_file_count: int | None,
    now: datetime,
) -> BackupManifest: ...

def create_backup(
    cfg: Config,
    *,
    out_dir: Path | None = None,
    include_vault: bool = True,
    label: str = "",
    runner: CommandRunner | None = None,
    clock: Callable[[], datetime] | None = None,
    on_step: Callable[[str], None] | None = None,
) -> BackupResult: ...

def preflight(
    archive_dir: Path,
    manifest: BackupManifest,
    cfg: Config,
    embedder: Embedder,
    conn: psycopg.Connection[Any] | None,
    *,
    vault_path: Path,
    db_leg: bool,
    vault_leg: bool,
) -> Preflight: ...

def restore_backup(
    archive: Path,
    cfg: Config,
    *,
    db_leg: bool = True,
    vault_leg: bool = True,
    runner: CommandRunner | None = None,
    clock: Callable[[], datetime] | None = None,
    pre_backup: Callable[[], Path] | None = None,
    on_step: Callable[[str], None] | None = None,
) -> RestoreReport: ...

def latest_backup(directory: Path) -> BackupSummary | None: ...
```

The `runner` / `clock` / `pre_backup` / `on_step` parameters are the dependency-injection seams — the same pattern as `run_uninstall(_launchd_uninstall=…)` (`uninstall.py:51`) and the chunker's injected `count_tokens`. Tests pass fakes; production passes `None` and gets the real implementations. **No production module is ever reopened to inject attributes.**

`CommandRunner` is a Protocol:

```python
class CommandRunner(Protocol):
    """Boundary for every external process this package spawns."""
    def run(self, argv: Sequence[str], *, timeout: float) -> subprocess.CompletedProcess[str]: ...
```

#### 5.7 SQL (all parameterized; identifiers quoted, never concatenated)

Manifest collection:

```python
# migration head + count
row = conn.execute(
    "SELECT max(name), count(*) FROM schema_migrations"
).fetchone()

# graph entity count — tolerant of a pre-GraphRAG database
if _table_exists(conn, "graph_entities"):          # db.py:171
    row = conn.execute("SELECT count(*) FROM graph_entities").fetchone()
```

Counts reuse `queries.summary_counts(conn)` (`queries.py:578`) — no new count SQL. Embedding shape reuses `queries.embedding_column_state(conn)` (`queries.py:896`) and `embedder.dim`.

Restore, on a connection to the **maintenance** database (`postgres`), `autocommit = True` (`CREATE`/`ALTER`/`DROP DATABASE` cannot run inside a transaction block):

```python
conn.execute(
    sql.SQL("CREATE DATABASE {name} OWNER {owner}").format(
        name=sql.Identifier(staging_db), owner=sql.Identifier(owner)
    )
)

# Terminate other sessions on the live DB before the rename.
rows = conn.execute(
    """
    SELECT pid, coalesce(application_name, '?')
    FROM pg_stat_activity
    WHERE datname = %s AND pid <> pg_backend_pid()
    """,
    (live_db,),
).fetchall()
conn.execute(
    """
    SELECT pg_terminate_backend(pid)
    FROM pg_stat_activity
    WHERE datname = %s AND pid <> pg_backend_pid()
    """,
    (live_db,),
)

conn.execute(
    sql.SQL("ALTER DATABASE {old} RENAME TO {parked}").format(
        old=sql.Identifier(live_db), parked=sql.Identifier(parked_db)
    )
)
conn.execute(
    sql.SQL("ALTER DATABASE {staging} RENAME TO {live}").format(
        staging=sql.Identifier(staging_db), live=sql.Identifier(live_db)
    )
)
```

Every database name is either read from the parsed DSN or generated from a timestamp against `^[a-z0-9_]+$` before being wrapped in `sql.Identifier` — belt and braces, mirroring how `brain analyze` validates against `queries.list_public_tables` before quoting (`cli.py:1786-1790`, `queries.py:1156-1170`).

#### 5.8 Restore data flow

1. **Read-only phase.** Verify the sidecar hash; extract to a staging dir under `$BRAIN_HOME/run/restore-<ts>/`; parse `manifest.json` (reject `schema != 1`); verify each member's sha256; connect read-only to the target and gather live counts.
2. **Preflight** (`preflight()`), producing `PreflightIssue`s:
   - *checksum* — any mismatch → fatal.
   - *embedder* — `manifest.embedder != cfg.embedder` → **fatal**, message: `Archive was embedded with 'voyage' but BRAIN_EMBEDDER is 'arctic'. Embeddings cannot be re-projected across models; restoring would leave every vector meaningless. Set BRAIN_EMBEDDER=voyage and retry, or restore into a database you will re-embed with 'brain reembed'.` (deliberately echoing the wording of the existing `ensure_embedding_column` error at `db.py:472-477`).
   - *dim* — `manifest.embedding_dim != embedder.dim` → **fatal**, same reasoning. Both checks fire even when the names match (a user could repoint `BRAIN_QWEN3_MODEL`).
   - *migration_head* — archive head `==` installed head → no issue. Archive head `<` installed head → **non-fatal note**: "`brain init` will apply N newer migrations after restore" (the design already guarantees this works: `run_migrations` is name-ordered and idempotent, `db.py:245-274`). Archive head `>` installed head → **fatal**: "archive was created by a newer brain (head `027_…`, installed head `023_…`); upgrade with `pipx upgrade secondbrain-py` first."
   - *server_version* — `manifest.postgres_version_num // 10000 > target_major` → **fatal** ("cannot restore a PostgreSQL 17 dump into a 16 server"). Older-or-equal → fine.
   - *disk* — via `shutil.disk_usage`, require `dump_bytes * 3 + vault_bytes * 2` free on the Postgres data path (`cfg.brain_home / "data" / "postgres"`, falling back to the staging path when that directory does not exist) — 3× because the staging database, the parked database, and the extracted dump coexist at peak. Insufficient → **fatal**.
   Any fatal issue → print all issues and exit `3`. Nothing has been modified.
3. **Gate** (§5.9).
4. **Pre-restore backup**: `create_backup(cfg, out_dir=cfg.brain_home / "backups", include_vault=vault_leg, label="pre-restore")`. If it fails → **abort the whole restore** (exit 1). A restore without a safety net is exactly the scenario the wipe rule exists to prevent.
5. **DB leg**: create staging DB → `pg_restore --no-owner --no-privileges --exit-on-error -d <staging>` → **verify** (staging `documents`/`chunks` counts equal `manifest.counts`, `max(schema_migrations.name)` equals `manifest.migration_head`) → mismatch drops the staging DB and exits 1 with "nothing was changed" → terminate other sessions → two-statement rename swap.
6. **Vault leg**: if `<vault_path>` exists and is non-empty, `Path.rename` it to `<vault_path>.replaced-<ts>` (instant, undoable, never `shutil.rmtree`); extract `vault.tar` to `<vault_path>` through the hardened member filter.
7. **Post-restore**, run automatically because both are idempotent and non-destructive:
   - `db.run_migrations(conn)` + `db.ensure_embedding_column(conn, embedder)` + `db.bootstrap_age(conn)` — i.e. exactly what `brain init` does (`db.py:235`, `db.py:412`, `db.py:314`). This is what applies any newer migrations noted in preflight and re-bootstraps AGE.
   - `queries.analyze_tables(conn, ["chunks", "documents"])` (`queries.py:1174`) — this is the wiring the existing doctor warning demands (`cli.py:951-965`: "`chunks stats WARN — never analyzed` … This can happen after `pg_restore`"). Without it, the very first thing the user sees after a successful restore is a doctor warning.
   - **Not** run automatically, printed as follow-up: `brain graphrag build --force` (§5.2) and `brain doctor`.

#### 5.9 The gate — matching and exceeding `brain uninstall`

Printed before the gate: the full "WILL BE OVERWRITTEN" block from §3 with **live counts read from the target database**, the vault path and its live file count, the aside-path the vault will be moved to, and the pre-restore backup path. `brain uninstall` lists paths (`uninstall.py:84-91`); this lists paths *and* the live row counts about to be replaced, which is strictly more information.

Then:

```python
if not yes:
    typer.confirm("Proceed?", default=False, abort=True)      # uninstall.py:98

if target_is_non_empty:                                       # NEVER bypassed by --yes
    answer = typer.prompt('Type "restore and overwrite my brain" to confirm')
    if answer != _RESTORE_PHRASE:
        typer.secho("Aborted — nothing was changed.", fg="yellow")
        raise typer.Abort()
```

**Recommendation, adopted: `--yes` may NOT bypass the typed phrase when the target is non-empty.** `target_is_non_empty` is `True` when (the DB leg is active and `documents > 0`) **or** (the vault leg is active and `vault_path` contains at least one file). When both targets are empty — the disaster-recovery case on a fresh machine — `--yes` skips both prompts, because nothing is being destroyed and forcing an interactive prompt there would push users toward scripting around the gate. This is a strict superset of the `uninstall.py:103-109` contract (which unconditionally prompts for `--remove-db`), adding an empty-target escape hatch that is safe by construction.

#### 5.10 Error handling

New exceptions, all inheriting `BrainError` (`errors.py:16`):

- `BackupError(BrainError)` — dump/restore/archive failure; carries the underlying stderr.
- `PgToolUnavailable(BackupError)` — no version-compatible `pg_dump`/`pg_restore`; message includes both remediations.
- `RestoreIncompatible(BackupError)` — carries `issues: tuple[PreflightIssue, ...]`; CLI maps to exit 3.
- `RestoreAborted(BackupError)` — the swap failed after the first rename; carries `recovery_sql: str`.

`cli_backup.py` wraps every core call in `except BrainError as exc:` → `typer.secho(str(exc), fg="red")` + `typer.Exit(1)` (or 3 for `RestoreIncompatible`), the pattern already used by the demo CLI. No bare `except:`; subprocess failures are caught as the three specific `subprocess` exception types plus `FileNotFoundError`; DB failures as `psycopg.Error` / `psycopg.OperationalError`.

### 6. Edge cases and failure modes

1. **Host `pg_dump` is 14.23 and the container is stopped.** Container probe fails; host probe finds 14 < 16 → `PgToolUnavailable`, exit 1, with both remediations printed (start the stack, or `brew install postgresql@16`). Never attempts the dump — a truncated or aborted dump masquerading as a backup is the worst possible outcome.
2. **Backup interrupted (Ctrl-C, disk full) mid-dump.** The archive is assembled in `<out>/.brain-backup-<ts>.partial/` and only `os.replace`d into place at the very end. A `try/finally` removes the staging dir and the in-container `/tmp/*.dump`. No `.tar.gz` and no `.sha256` sidecar are ever left behind, so `latest_backup()` and the user can never mistake a partial for a usable backup.
3. **Restoring an archive taken with a different embedder or dim.** Preflight fails fatally with the "embeddings are not re-projectable" explanation *before* the pre-restore backup, the staging DB, or any rename. Exit 3. This is the single most important abort — a silent restore here would leave 18,000 chunks whose vectors are noise, and hybrid search would degrade in a way that looks like a ranking bug, not a corruption.
4. **Restore fails halfway during `pg_restore`.** The live database has not been touched — everything so far happened in `second_brain_restore_<ts>`. The staging DB is dropped in a `finally`, and the command exits 1 with `Restore failed; nothing was changed. Your database is untouched.` plus `pg_restore`'s stderr.
5. **Restore fails between the two `ALTER DATABASE … RENAME` statements.** `ALTER DATABASE` cannot be transactional, so this window exists and must be handled, not wished away. The live name is briefly absent. The command raises `RestoreAborted` with the literal recovery SQL printed (and included in `--json` as `recovery_sql`):
   ```
   ALTER DATABASE second_brain_replaced_20260725_181500 RENAME TO second_brain;
   ```
   …together with the pre-restore backup path. Both databases still exist; no data is lost.
6. **Another process holds a connection to the live database** (the `brain vault sync --watch` daemon or `brain-mcp`; memory records a `relink-derived ↔ watcher` deadlock, so concurrent daemons are a live reality here). The rename would fail with `database is being accessed by other users`. Restore lists the offending `application_name`s, prints them, then calls `pg_terminate_backend` on every non-self PID for that database, and retries the rename once. If a daemon reconnects and it fails again, restore aborts *before* the first rename with `Stop the watcher and MCP first: brain-down, or launchctl unload …`.
7. **`--no-vault` archive restored with the vault leg requested.** `manifest.vault_included == false` and `--vault-only` (or the default both-legs) was asked for. With `--vault-only` → `typer.BadParameter` (exit 2): "this archive contains no vault". With the default → non-fatal note, the vault leg is skipped, and the summary says `vault  SKIPPED (not present in archive)` so the user is never left believing their notes were restored.
8. **The vault path is a symlink into iCloud** (an explicitly supported setup — `config.py:89-91`). `Path.rename` across a filesystem boundary raises `OSError: Invalid cross-device link`. Restore detects this (`os.stat().st_dev` differs) and falls back to extracting into `<vault_path>.restored-<ts>` **without** moving the old vault aside, printing: `Vault restored alongside the original at <path> — move it into place manually (cross-device link).` It never deletes the user's notes to work around a rename limitation.
9. **Corrupt or truncated archive** (bad USB copy). The sidecar hash mismatches → exit 3 before extraction. If the sidecar is *missing* (user deleted it), a warning is printed and the per-member manifest checksums still gate the restore; if those also fail, exit 3.
10. **Tar member escaping the extraction root** (`../../.ssh/authorized_keys`, an absolute path, or a symlink). `_safe_members` rejects it and raises `BackupError` naming the member; nothing is written outside the staging root.
11. **Restoring into a completely empty database on a fresh machine.** `documents == 0` and the vault dir is empty/absent → `--yes` skips both prompts (§5.9), the pre-restore backup is still taken (it is tiny), the swap still happens, and the post-restore `run_migrations` applies any newer migrations. This is the primary disaster-recovery path and it must be scriptable.
12. **Archive whose migration head is newer than the installed package** (restoring a backup from a machine running a newer brain). Fatal, exit 3, `upgrade secondbrain-py first` — because `run_migrations` (`db.py:245-274`) applies pending files by name and would leave the schema ahead of the code with no downgrade path.

### 7. Security and safety

| Risk | Guard |
|---|---|
| Restore destroys the live database | Never drops it. Restores into `second_brain_restore_<ts>`, verifies counts against the manifest, then **renames** the live DB to `second_brain_replaced_<ts>`. The old database survives the command; the user drops it manually when satisfied. |
| Restore destroys the vault | Never `rmtree`s. Renames to `<vault_path>.replaced-<ts>` before extracting. |
| An automated agent runs `brain restore --yes` | The typed phrase `restore and overwrite my brain` is required whenever the target is non-empty and cannot be supplied by any flag — matching the `uninstall.py:103-109` contract, which exists for the same reason. |
| Postgres password leaks into `ps` output | Never in argv. Written to a `0600` temp file consumed by `docker exec --env-file`, removed in a `finally`. Not written into the manifest, not logged. |
| Backup archive contains the whole corpus in plaintext | Default `--out` is `$BRAIN_HOME/backups`, which the repo `.gitignore:16` already covers; the archive is created with mode `0600` and the containing directory `0700`. The human output states the archive contains the full corpus. Encryption is deliberately out of scope (see Open Questions). |
| Tar path traversal / symlink attack on restore | `_safe_members` rejects absolute paths, `..` components, symlinks, hardlinks, device nodes, and any resolved path outside the root — implemented explicitly rather than relying on 3.11.4+ `filter="data"`. |
| SQL injection through a database name | Every identifier goes through `psycopg.sql.Identifier`; the generated staging/parked names are additionally regex-validated `^[a-z0-9_]+$`. All values are `%s` + tuple. |
| `--label` injected into a filesystem path | Validated `^[A-Za-z0-9_-]{1,40}$` at the Typer boundary → `typer.BadParameter` (exit 2). |
| A test suite run wipes production | Every DB-touching test goes through the session-scoped `_force_test_database_url` fixture (`conftest.py:153-183`) and asserts via `_looks_like_prod_db` (`conftest.py:50`) that no DSN handed to the fake runner resolves to port 55432 / db `second_brain`. The fake runner itself raises if it ever sees a prod DSN — see §8. |
| PII in tests/fixtures | Every fixture uses synthetic titles and `*.example.com` addresses; no real transcript, email body, name, or employer appears in any test, fixture, docstring, or commit message. |

### 8. Test plan

**Red-first failing test (proves the gap):** `tests/test_cli_backup.py::test_backup_command_is_registered` — `CliRunner().invoke(app, ["backup", "--help"])` and assert `result.exit_code == 0`. Today this fails with exit code `2` and `No such command 'backup'`. Written and observed failing before any implementation.

**`tests/test_backup_pgtool.py`** (pure logic, target 95%):
- `test_container_pg_dump_preferred_when_major_matches` — fake runner returns `pg_dump (PostgreSQL) 16.14` for the container probe; plan is `source == "container"`.
- `test_host_pg_dump_rejected_when_older_than_server` — container probe raises `FileNotFoundError`; host reports `14.23`; server major 16 → `PgToolUnavailable`, message contains both `brain-up` and `postgresql@16`. **This encodes the empirically verified 14.23-vs-16.14 constraint.**
- `test_host_pg_dump_accepted_when_newer_than_server` — host `17.2`, server major `16` → accepted (the `>=` rule, not `==`).
- `test_version_parse_rejects_garbage` — unparseable `--version` output → `PgToolUnavailable`, not `IndexError`.
- `test_password_never_appears_in_argv` — assert no recorded argv element contains the password; assert an `--env-file` argument is present.
- `test_container_name_follows_compose_project` — with `BRAIN_COMPOSE_PROJECT=qa` (via `monkeypatch.setenv`, an env boundary, allowed), the plan targets `qa-postgres`; unset → `second-brain-postgres`.

**`tests/test_backup_manifest.py`** (pure logic, 95%):
- `test_roundtrip_preserves_every_field` — `from_dict(to_dict(m)) == m`.
- `test_rejects_unknown_schema_version` — `{"schema": 2, …}` → `BackupError`.
- `test_rejects_missing_required_key` — each required key removed in turn → `BackupError` naming the key (parameterized).
- `test_collect_manifest_reads_live_db` (`test_db`) — ingest three synthetic docs via the fake embedder, assert `counts.documents == 3`, `migration_head == "023_search_queries_fts_count.sql"` (read from `sorted(migrations_dir().glob("*.sql"))[-1].name`, not hard-coded, so it survives migration 024/025), `embedding_dim == fake.dim`.
- `test_graph_entities_null_when_table_absent` — on a `@pytest.mark.fresh_schema` DB with `graph_entities` dropped, `graph_entities is None` rather than raising.

**`tests/test_backup_archive.py`** (pure logic, 95%):
- `test_sha256_matches_hashlib` over a known byte string.
- `test_extract_rejects_absolute_member` / `..._parent_traversal_member` / `..._symlink_member` / `..._device_member` — each constructs a hostile tar in `tmp_path` and asserts `BackupError` and that nothing was written outside the root.
- `test_sidecar_written_and_verified` and `test_sidecar_mismatch_detected` (flip one byte of the archive).
- `test_archive_is_created_atomically` — no `.tar.gz` exists when the writer raises mid-stream; the `.partial` dir is cleaned up.

**`tests/test_backup_create.py`** (integration, `test_db`, 90%):
- `test_create_backup_produces_readable_archive` — fake `CommandRunner` writes a byte-identical stub dump file; assert the `.tar.gz` contains `manifest.json`, `db/second_brain.dump`, `vault.tar`; assert the sidecar matches.
- `test_no_vault_omits_vault_member` — `vault_included is False`, no `vault.tar`, `files` has one entry.
- `test_label_appears_in_filename_and_manifest`.
- `test_bad_label_rejected` (CLI level, exit 2).
- `test_dump_argv_excludes_age_schemas` — the recorded argv contains `--exclude-schema=ag_catalog` and `--exclude-schema=brain_graph` and `-Fc`.

**`tests/test_restore_preflight.py`** (integration, `test_db`, 90%) — every case asserts the target DB is byte-identical afterwards:
- `test_embedder_mismatch_is_fatal` — manifest `embedder="voyage"`, active `arctic` → `blocked`, issue code `embedder`, exit 3.
- `test_dim_mismatch_is_fatal` — same name, `embedding_dim=4096` vs active `1024` → blocked.
- `test_newer_migration_head_is_fatal` — manifest head `099_future.sql` → blocked with the upgrade remedy.
- `test_older_migration_head_is_a_note_not_fatal` — manifest head `001_init.sql` → not blocked, one non-fatal issue.
- `test_checksum_mismatch_is_fatal` — corrupt `db/second_brain.dump` inside the staging dir.
- `test_newer_server_dump_is_fatal` — `postgres_version_num = 170004` against a 16 server.
- `test_insufficient_disk_is_fatal` — injected `disk_usage` probe returning a tiny `free`.
- `test_clean_archive_has_no_fatal_issues`.

**`tests/test_restore_gate.py`** (CLI, `CliRunner`, 85%) — **no real dump/restore ever runs**; a `RecordingRunner` fake records argv and asserts on every invocation that the DSN it is handed does not satisfy `_looks_like_prod_db` (imported from `tests.conftest`, `conftest.py:50`), so a regression in the guard makes the test explode rather than touch production:
- `test_wrong_typed_phrase_aborts_and_runs_nothing` — `input="y\nnope\n"` → exit 1, `RecordingRunner.calls == []`, target counts unchanged.
- **`test_refuses_prod_database_url_without_typed_phrase`** — the required guard test. `monkeypatch.setenv("DATABASE_URL", "postgresql://brain:brain@localhost:55432/second_brain")`, `input="y\n\n"` (empty phrase), `--yes` **not** passed. Asserts exit 1, `RecordingRunner.calls == []`, and no `psycopg.connect` was ever attempted against port 55432 (the injected connection factory records and refuses). The prod database is provably never opened.
- **`test_yes_flag_cannot_bypass_typed_phrase_on_non_empty_target`** — `--yes` plus `input=""` (EOF on the prompt) → `typer.Abort`, exit 1, nothing run. This is the exact contract from §5.9.
- `test_yes_skips_prompts_on_empty_target` — empty `test_db`, empty tmp vault, `--yes`, no stdin → proceeds.
- `test_correct_phrase_proceeds` — `input="y\nrestore and overwrite my brain\n"` → the fake runner records a `pg_restore` invocation against the **staging** database name, never the live one.
- `test_db_only_and_vault_only_together_is_bad_parameter` — exit 2.
- `test_gate_prints_live_counts` — the pre-gate output contains the target's actual document count.

**`tests/test_restore_swap.py`** (integration, `test_db` + a second staging database on the same **test** server, 90%):
- `test_successful_swap_retains_previous_database` — after restore, `second_brain_test_replaced_<ts>` exists and the live name holds the restored rows.
- `test_verification_failure_drops_staging_and_leaves_live_untouched` — manifest counts deliberately disagree with the restored staging DB → exit 1, staging DB gone, live DB unchanged.
- `test_rename_failure_after_first_rename_reports_recovery_sql` — injected failure on the second `ALTER DATABASE` → `RestoreAborted` whose message contains the literal `ALTER DATABASE … RENAME TO second_brain_test`.
- `test_post_restore_runs_analyze` — assert `pg_stat_user_tables.last_analyze` for `chunks` is non-NULL afterwards, i.e. `brain doctor`'s `chunks stats WARN — never analyzed` (`cli.py:951-965`) does **not** fire after a restore. This is the regression test that wires the existing warning to its fix.
- `test_vault_moved_aside_not_deleted` — the pre-existing vault file is still readable at `<vault>.replaced-<ts>`.

**`tests/test_cli_backup.py`** (CLI, 85%): command registration (the red-first test), `--json` shape (every documented key present with the documented type), human output contains the archive path and sha256 prefix, and `brain doctor` gains the `last backup` line in both human and `--json` modes.

Coverage: the pure-logic modules (`manifest`, `archive`, `pgtool`, `discovery`) target 95%; `create`/`restore` target 90%; `cli_backup` 85%. All fixtures use synthetic content (`"Larkspur quarterly review"`, `casey@example.com`) matching the existing `brain demo` corpus convention.

### 9. Open questions — with recommended answers

1. **Should the archive be encrypted at rest?** — **No, not in v1.** The corpus is already unencrypted in `./data/postgres` and in the vault; encrypting only the backup adds key management and a new way to lose everything (lost passphrase = lost backup) without closing a real gap. Ship `0600` file modes and a documented note that the archive is plaintext; revisit if/when off-machine sync lands.
2. **Should `brain backup` also copy the `./data/postgres` bind mount (a physical backup)?** — **No.** A physical copy of a *running* cluster is inconsistent unless taken under `pg_start_backup`/stop, and stopping the container mid-command is unacceptable. The logical `-Fc` dump is consistent by construction. Note in `--help` that the bind mount is *not* what gets copied, and that `docker compose down -v` does **not** wipe it (the data lives at `$BRAIN_HOME/data/postgres`, a host bind mount, not a Docker-managed volume) — so users do not confuse compose teardown with data loss in either direction.
3. **Should restore run `brain graphrag build --force` automatically?** — **No; print it.** It can run for minutes on a 1,200-doc corpus and is not needed for `brain search`. Restore's job is to end fast and correct; graph rebuild is an explicit, resumable follow-up. The manifest's `graph_entities` count gives the user a target to verify against.
4. **Should `brain backup` prune old archives (`--keep N`)?** — **Not in v1.** Automatic deletion of backups in a codebase whose defining incident was accidental deletion is the wrong first feature. `brain doctor` warns when the newest backup is older than 30 days; pruning is manual until there is evidence of disk pressure.
5. **Should `--out` default to `$BRAIN_HOME/backups` or the repo's `backups/`?** — **`$BRAIN_HOME/backups`.** It works identically for pipx installs and dev checkouts (`_brain_home_root`, `config.py:410-430`, resolves the repo root in a dev checkout anyway, so the dev experience is unchanged), and it keeps archives out of any directory a `git clean -xdf` might reach.
6. **Should there be a `brain restore --undo`?** — **No; print the two-line recovery instead.** The parked database and the moved-aside vault are both durable and named deterministically, and the pre-restore backup is itself restorable with the ordinary `brain restore <path>`. A dedicated `--undo` would add a second destructive code path guarding the same state; the printed `ALTER DATABASE … RENAME` and `brain restore <pre-restore path>` cover it with zero new risk surface.
7. **Should the staging-database swap be replaced by an in-place `DROP SCHEMA public CASCADE` + restore, which is simpler?** — **No, definitively.** `tests/conftest.py:259-268` documents that this orphans the `vector` and `pgcrypto` rows in `pg_extension` so a later `CREATE EXTENSION IF NOT EXISTS vector` silently no-ops, leaving a schema with no `vector` type — the test harness needs an `ALTER EXTENSION vector SET SCHEMA pg_catalog` dance to survive it (`conftest.py:283-286`). Reproducing that fragility in the user-facing recovery path is unacceptable; the staging-DB-plus-rename approach sidesteps it entirely and retains the previous database as a bonus.
8. **Does this section need a migration?** — **No.** It reads existing schema (`schema_migrations`, `documents`, `chunks`, `sources`, `graph_entities`) and adds no columns or tables. Migrations `024` and `025` belong to the AGENT-MEMORY and SAFETY sections respectively; this section allocates none.
