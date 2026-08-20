# GraphRAG — Custom AGE + pgvector Docker image (G0-1)

> Status: image built + validated. **Cutover of the running DB is NOT applied** —
> this doc records the exact, DB-safe steps for later explicit approval. The cutover
> is **GATED**: every check in "Pre-cutover gate" below must pass first.
> Companion to `docs/specs/2026-05-20-graphrag-design.md` §11 (Ops & deployment).

> **G4-f rebase (2026-05-22).** The image base was rebased
> `pgvector/pgvector:0.8.0-pg16` → `pgvector/pgvector:0.8.2-pg16` and the image tag
> bumped `second-brain-age:pg16-v1.5.0-rc0` → `second-brain-age:pg16-v1.5.0-rc0-pgv0.8.2`.
> Rationale: the **live prod DB now reports pgvector `0.8.2`** (the stock
> `pgvector/pgvector:pg16` tag moved to 0.8.2), so rebasing keeps the gated cutover an
> additive **same-pgvector `0.8.2 → 0.8.2`** image swap. The AGE compile is unchanged
> (same pinned tag `PG16/v1.5.0-rc0`, same commit `0048900f`, same drift guard). Built +
> validated on the throwaway port-5434 AGE **test** container only; prod (port 5433,
> `./data/postgres`) was not touched. The pre-cutover gate + cold-copy smoke below are
> unchanged and still mandatory.

## What G0-1 delivers

A pinned custom Postgres 16 image that bundles everything the GraphRAG waves need
in **one** Postgres:

| Component | Version | Source |
|---|---|---|
| PostgreSQL | 16 | base image |
| pgvector (`vector`) | 0.8.2 | base image `pgvector/pgvector:0.8.2-pg16` |
| `pgcrypto` | 1.3 | postgres contrib (already present) |
| Apache AGE (`age`) | 1.5.0-rc0 | compiled from source, tag `PG16/v1.5.0-rc0` (commit `0048900f`) |

- **Canonical Dockerfile (single source, packaged):** `src/brain/templates/docker/age/Dockerfile`
- **Built image tag:** `second-brain-age:pg16-v1.5.0-rc0-pgv0.8.2`
- **AGE version honesty:** upstream Apache AGE has **no GA PG16 tag** — `PG16/v1.5.0-rc0`
  is the latest released PG16 tag (next is `PG16/v1.6.0-rc0`). It is a **release
  candidate**, labelled `1.5.0-rc0` everywhere (Dockerfile comments, LABELs, compose,
  env, this doc) — never "1.5.0 GA". The build pins the immutable commit `0048900f`
  and a **drift guard** fails the build if `PG16/v1.5.0-rc0` ever stops resolving to it.
- **Base choice:** start `FROM pgvector/pgvector:0.8.2-pg16` and compile AGE against the
  image's own PG16 headers. This keeps the running DB's pgvector ABI / on-disk layout
  identical (the live DB already runs pgvector 0.8.2 on PG16), so an image swap on the
  **same data dir is additive** — no `pg_upgrade`, no re-embed.
- **`shared_preload_libraries=age` is NOT required.** AGE works with the per-session
  `LOAD 'age';` bootstrap that `brain init` / `connect` will issue (G0-2). It can be
  enabled later purely for convenience/perf — see "Optional" below.

## Why a custom image

`pgvector/pgvector:pg16` ships pgvector + pgcrypto but not AGE; `apache/age:PG16` ships
AGE but not our exact pgvector build. Neither alone gives PG16 + pgvector 0.8.2 +
pgcrypto + AGE in one instance, which the design requires (one Postgres, §2.2).

## Build

```bash
# from the repo root (single canonical Dockerfile path)
docker build -t second-brain-age:pg16-v1.5.0-rc0-pgv0.8.2 src/brain/templates/docker/age
# or, equivalently, via compose (does NOT start anything):
docker compose build
```

`brain setup` materializes the same packaged Dockerfile into
`$BRAIN_HOME/docker/age/Dockerfile` before `docker compose up`, so fresh installs build
the identical image. The build installs a toolchain (build-essential,
postgresql-server-dev-16, bison, flex), clones AGE at the pinned commit (drift-guarded
against the `PG16/v1.5.0-rc0` tag), `make && make install`, then purges all build deps.
Final image ≈ 976 MB.

## Validation (already performed, throwaway container)

Validated on a **throwaway** container — separate name `brain-age-smoke`, separate
port `5455`, ephemeral `tmpfs` data dir, **no prod volume** — then torn down. The
running `second-brain-postgres` (port 5433, `./data/postgres`) was never touched.

```bash
docker run -d --name brain-age-smoke \
  -e POSTGRES_USER=brain -e POSTGRES_PASSWORD=brain -e POSTGRES_DB=second_brain \
  -p 5455:5432 --tmpfs /var/lib/postgresql/data \
  second-brain-age:pg16-v1.5.0-rc0-pgv0.8.2

docker exec -i brain-age-smoke psql -U brain -d second_brain -v ON_ERROR_STOP=1 <<'SQL'
CREATE EXTENSION IF NOT EXISTS vector;
CREATE EXTENSION IF NOT EXISTS pgcrypto;
CREATE EXTENSION age CASCADE;
LOAD 'age';
SET search_path = ag_catalog, "$user", public;
SELECT create_graph('smoke');
SELECT * FROM cypher('smoke', $$ CREATE (p:Person {name:'synthetic-node'}) RETURN p $$) AS (p agtype);
SELECT * FROM cypher('smoke', $$ MATCH (p:Person) RETURN p.name $$) AS (name agtype);
SQL

docker rm -f brain-age-smoke   # tear down ONLY the throwaway
```

Result (all green, one instance): `age` extversion `1.5.0` (built from the
release-candidate tag `PG16/v1.5.0-rc0` — not a GA release), `pgcrypto 1.3`,
`vector 0.8.2`; `create_graph('smoke')` OK; Cypher round-trip returned `"synthetic-node"`.

---

## Cutover of the running container — NOT applied; gated on explicit approval

The running DB still uses `pgvector/pgvector:pg16`, and the repo-root
`docker-compose.yml` now pins that **stock** image too — G0-2a reverted the prod
service to stock so nobody cuts over prod by accident (AGE runs for tests via the
separate `docker-compose.age-test.yml` on port 5434). Swapping prod to the AGE image
is an **additive, same-major (PG16→PG16), same-pgvector (0.8.2→0.8.2) image swap on the
same data dir** — no data migration — but it now requires **editing
`docker-compose.yml` to re-add the AGE `build:`+`image:` stanza first** (step 3 below).
**The cutover is BLOCKED until every check in the gate below passes AND the user
explicitly approves.** Do not run the edit/stop/up steps otherwise.

### Pre-cutover gate (ALL must pass — do not proceed on any failure)

Run these read-only checks and record the output. Every box must be checked.

**(a) Live server major version**
```bash
docker exec second-brain-postgres \
  psql -U brain -d second_brain -tAc "SELECT current_setting('server_version_num');"
# expect 16xxxx (PG16). Record it.
```

**(b) Current running image + digest** (so you can roll back to the exact image)
```bash
docker inspect second-brain-postgres --format '{{.Config.Image}}'
docker inspect second-brain-postgres --format '{{index .Image}}'   # image ID
docker image inspect "$(docker inspect second-brain-postgres --format '{{.Config.Image}}')" \
  --format '{{join .RepoDigests "\n"}}'                            # registry digest(s)
```

**(c) Live pgvector version (installed AND available default)**
```bash
docker exec second-brain-postgres \
  psql -U brain -d second_brain -tAc "SELECT extversion FROM pg_extension WHERE extname='vector';"
docker exec second-brain-postgres \
  psql -U brain -d second_brain -tAc \
  "SELECT default_version FROM pg_available_extensions WHERE name='vector';"
# record both (live installed 0.8.2; default available 0.8.2)
```

**(d) New image PG major + pgvector version** (the image you are cutting over to)
```bash
docker run --rm second-brain-age:pg16-v1.5.0-rc0-pgv0.8.2 postgres --version          # -> PG 16.x
docker run --rm second-brain-age:pg16-v1.5.0-rc0-pgv0.8.2 \
  sh -c "psql --version >/dev/null 2>&1; cat \"\$(pg_config --sharedir)/extension/vector.control\" | grep default_version"
# -> default_version = '0.8.2'
```

**(e) Compatibility assertion — confirm BEFORE proceeding:**
- New image PG major **==** live PG major (both 16). A different major needs `pg_upgrade`
  — **STOP**, this runbook does not cover it.
- New image pgvector **>=** live pgvector (0.8.2 >= 0.8.2). A lower pgvector can refuse to
  load existing index/opclass data — **STOP** if new < live.

- [ ] (a) live server_version_num is 16xxxx
- [ ] (b) current image + digest recorded for rollback
- [ ] (c) live pgvector installed + default-available versions recorded
- [ ] (d) new image PG major + pgvector version recorded
- [ ] (e) new == live on PG major AND new pgvector >= live pgvector
- [ ] cold-copy smoke (below) passed
- [ ] explicit user approval to cut over

### Cold-copy smoke gate (rehearse the swap on a COPY — never the live dir)

Prove the AGE image boots cleanly against a **copy** of the real data before touching
prod. This never mounts `./data/postgres`; it mounts a throwaway copy on a separate port.

**Prod stays on the stock image for the entire gate.** Seeding the copy must not leave
prod down: use the no-downtime hot-backup method by default. The stop→copy fallback is
only for when `pg_basebackup` is unavailable, and it **restarts prod immediately after the
copy — before any AGE testing — unconditionally**, so a smoke failure or a declined
cutover can never leave prod stopped.

**Preferred — no downtime (hot base backup, prod keeps running):**
```bash
# 1. take a consistent base backup WITHOUT stopping prod
TMPDATA="$(mktemp -d)/postgres"; mkdir -p "$TMPDATA"
docker exec second-brain-postgres \
  pg_basebackup -U brain -D - -Ft -X fetch | tar -xf - -C "$TMPDATA"
```

**Fallback — brief stop, then IMMEDIATE unconditional restart (only if `pg_basebackup` is unavailable):**
```bash
# 1a. stop prod briefly purely to take a consistent filesystem copy
docker compose stop postgres
# 1b. copy the data dir to a temp location (NOT a move)
TMPDATA="$(mktemp -d)/postgres"; cp -a data/postgres "$TMPDATA"
# 1c. RESTART PROD NOW — before any AGE smoke — on the stock image. Run this
#     unconditionally; do NOT gate it on the smoke result or the cutover decision.
docker compose start postgres
docker exec second-brain-postgres pg_isready -U brain -d second_brain   # prod is back up
```

With prod running again (stock image) and a copy in `$TMPDATA`, run the smoke against the COPY:
```bash
# 2. start the AGE image against the COPY on a separate name + port (5456), no prod mount
docker run -d --name brain-age-coldsmoke -p 5456:5432 \
  -v "$TMPDATA:/var/lib/postgresql/data" second-brain-age:pg16-v1.5.0-rc0-pgv0.8.2
# 3. verify boot + pgvector loads + existing data is queryable
docker exec brain-age-coldsmoke pg_isready -U brain -d second_brain
docker exec brain-age-coldsmoke psql -U brain -d second_brain -tAc \
  "SELECT extversion FROM pg_extension WHERE extname='vector';"
docker exec brain-age-coldsmoke psql -U brain -d second_brain -tAc \
  "SELECT count(*) FROM documents;"   # sanity: real rows present + readable
# 4. tear down the cold smoke + delete the copy
docker rm -f brain-age-coldsmoke
rm -rf "$TMPDATA"
```

The smoke touches only the throwaway copy + container; **prod is already back on the
stock image before this point**. If the cold smoke boots, `vector` is present, and
`documents` is queryable, the swap is safe — **only now**, with user approval, proceed to
the real cutover. If any step fails, **STOP**: prod is unaffected (still on the stock
image) and no further action is needed.

### Cutover (only after the gate passes + user approves)

1. **Back up first (mandatory).**
   ```bash
   docker exec second-brain-postgres pg_dumpall -U brain > ~/brain-backup-$(date +%Y%m%d).sql
   # the cold copy from the smoke step doubles as a filesystem backup if you keep it
   ```
2. **Stop the running container** (data persists in `./data/postgres`):
   ```bash
   docker compose stop postgres        # or: bin/brain-down
   ```
3. **Point the prod compose service at the AGE image, THEN bring it up.** The
   repo-root `docker-compose.yml` ships the **stock** `pgvector/pgvector:pg16`
   image (G0-2a reverted it so prod can't be cut over accidentally), so the
   `build:`+`image:` stanza must be re-added to the `postgres` service before the
   build/up step. Edit `docker-compose.yml` → `services.postgres`:
   ```yaml
   # Replace the stock `image: pgvector/pgvector:pg16` line with the AGE stanza:
       build:
         context: ./src/brain/templates/docker/age
         dockerfile: Dockerfile
       image: second-brain-age:pg16-v1.5.0-rc0-pgv0.8.2
   ```
   Then build + start on the new image (port 5433 + `./data/postgres` unchanged):
   ```bash
   docker compose up -d --build postgres   # builds second-brain-age:pg16-v1.5.0-rc0-pgv0.8.2 then starts it
   docker exec second-brain-postgres pg_isready -U brain -d second_brain
   ```
   Data dir, port (5433), credentials unchanged → all documents/chunks/embeddings come
   back exactly as before; pgvector data is byte-compatible (same 0.8.2).
4. **Enable AGE on the existing database** (additive DDL — done by `brain init` once the
   G0-2 bootstrap lands; manual equivalent):
   ```sql
   CREATE EXTENSION IF NOT EXISTS age CASCADE;   -- one-time per database
   LOAD 'age';                                    -- per session (the bootstrap does this)
   SET search_path = ag_catalog, "$user", public;
   -- brain init's idempotent bootstrap then runs SELECT create_graph('brain_graph')
   --   (only if absent), plus label + property-index creation.
   ```
5. **Verify:** `brain doctor` (G0-2 adds an AGE line) and `brain status`.

### Rollback

Stop the container and revert the step-3 `docker-compose.yml` edit — i.e. drop the
AGE `build:`+`image:` stanza and restore `image: pgvector/pgvector:pg16` (the stock
image+digest recorded in gate step (b)) — then `docker compose up -d`. (Reverting to
stock is just undoing the step-3 edit, since the committed compose is already stock.) The
data dir is untouched by the AGE image (AGE only *adds* its catalog objects when you run
`CREATE EXTENSION age`). If you never ran `CREATE EXTENSION age`, the rollback is a pure
no-op. Worst case, restore from the step-1 dump.

### Optional: always-loaded AGE

Per-session `LOAD 'age'` is the default and is sufficient. To load AGE at server start,
add to the compose service `command: ["postgres", "-c", "shared_preload_libraries=age"]`.
Requires a restart; does **not** alter on-disk data. Not needed for G0-2's session
bootstrap; leave it off unless profiling shows a reason.

## Files changed by G0-1

- `src/brain/templates/docker/age/Dockerfile` — **new**, canonical packaged Dockerfile
  (PG16 + pgvector 0.8.2 + pgcrypto + AGE 1.5.0-rc0; drift-guarded commit pin).
- `docker-compose.yml` — postgres service `build: ./src/brain/templates/docker/age` +
  `image: second-brain-age:pg16-v1.5.0-rc0-pgv0.8.2` (port 5433 + `./data/postgres` unchanged).
  **(Reverted to stock `pgvector/pgvector:pg16` by G0-2a** — prod stays stock until the
  gated cutover above; AGE runs for tests via `docker-compose.age-test.yml` on 5434.)
- `src/brain/templates/docker-compose.yml.j2` — same image tag; build context
  `{{ brain_home }}/docker/age` (installer materializes the Dockerfile there).
- `src/brain/setup.py` — new `materialize_age_dockerfile()` + a setup step that copies
  the packaged Dockerfile into `$BRAIN_HOME/docker/age/` before `docker compose up`
  (dry-run aware).
- `pyproject.toml` — `brain.templates` package-data now includes `docker/age/Dockerfile`.
- `src/brain/templates/env.example` + repo-root `.env.example` — comment noting the AGE image.
- `tests/test_packaging_templates.py` — Dockerfile packaged/loadable + materialized-into-
  `$BRAIN_HOME` regression tests.

## Notes for G0-2 (AGE bootstrap in `init` / `connect`)

- `CREATE EXTENSION age CASCADE;` then `LOAD 'age';` then
  `SET search_path = ag_catalog, "$user", public;` **must run in the same session** as
  any Cypher; AGE catalog DDL wants **autocommit / explicit commits** under psycopg v3.
- Check `ag_catalog.ag_graph` and only `SELECT create_graph('brain_graph')` when absent
  (create_graph is not idempotent — it errors if the graph exists).
- Cypher `RETURN` queries need a trailing column-definition list, e.g.
  `... AS (v agtype)`. Confirmed working: `cypher('g', $$ ... $$) AS (col agtype)`.
- No `shared_preload_libraries` dependency — the session `LOAD 'age'` path is validated.
- `doctor` should assert: `vector`, `pgcrypto`, `age` extensions present (age extversion
  `1.5.0`, built from the rc tag `PG16/v1.5.0-rc0`; not GA), `LOAD 'age'` succeeds, and
  `brain_graph` exists in `ag_catalog.ag_graph`.
