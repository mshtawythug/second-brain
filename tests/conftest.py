"""Test harness — uses a real Postgres test DB and a fake embedder.

The schema is migrated ONCE per session (:func:`_ensure_test_db_initialized`);
each test that takes ``test_db`` then gets a cheap ``TRUNCATE`` data-reset
(:func:`_truncate_reset`). Tests that mutate the schema itself carry
``@pytest.mark.fresh_schema`` and get the full ``DROP SCHEMA`` + migrate reset,
with :func:`_restore_baseline_after_fresh_schema` restoring the migrated-once
baseline afterwards so the next TRUNCATE-only test is unaffected.
"""

import hashlib
import os
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import psycopg
import pytest
from dotenv import dotenv_values, load_dotenv
from psycopg import sql

from brain.config import DEFAULT_OLLAMA_HOST
from brain.db import connect, migration_lock, run_migrations
from brain.ingest import ExtractedDoc, ingest_document
from tests.db_lock import concurrent_suite_message, try_acquire_suite_lock

load_dotenv()

TEST_DATABASE_URL = os.environ.get(
    "TEST_DATABASE_URL",
    # Default to the Apache-AGE test instance (docker-compose.age-test.yml,
    # port 5434, named volume) — a separate container from prod on 5433.
    # Start it: docker compose -f docker-compose.age-test.yml up -d --build
    "postgresql://brain:brain@localhost:5434/second_brain_test",
)

# --- DB-SAFETY HARD GUARD ---------------------------------------------------
# The schema reset below runs ``DROP SCHEMA public CASCADE`` — irreversibly
# destructive. It must NEVER target the prod container. Prod is the
# ``second_brain`` database on localhost:55432 (docker-compose.yml,
# ./data/postgres bind-mount); 5433 was the historical prod mapping and is
# still refused defensively. The test instance is ``*_test`` on port 5434
# (docker-compose.age-test.yml). If ``TEST_DATABASE_URL`` / ``DATABASE_URL`` is
# ever pointed at prod (e.g. an accidental export), we ABORT loudly rather than
# wipe production data.
_PROD_PORTS = frozenset({5433, 55432})
_PROD_DB_NAME = "second_brain"
_LOCAL_HOSTS = frozenset({"", "localhost", "127.0.0.1", "::1", "0.0.0.0"})


def _looks_like_prod_db(host: str | None, port: int | None, dbname: str | None) -> bool:
    """True if (host, port, dbname) resolves to the prod container.

    Refuses ANY known prod host port (55432 current, 5433 historical) on a local
    host, OR the exact prod database name on any host (belt-and-suspenders:
    catches a prod restore under a different db name, or a non-default port
    mapping).
    """
    is_local = (host or "").lower() in _LOCAL_HOSTS
    return (is_local and port in _PROD_PORTS) or (dbname == _PROD_DB_NAME)


def _assert_not_prod_db(host: str | None, port: int | None, dbname: str | None) -> None:
    """Abort the session if a destructive reset would hit the prod database."""
    if _looks_like_prod_db(host, port, dbname):
        raise RuntimeError(
            "REFUSING to run the destructive test schema reset against what "
            f"looks like the PROD database (host={host!r} port={port!r} "
            f"db={dbname!r}). The DROP SCHEMA reset must only ever target the "
            "AGE test instance (port 5434, db '*_test'). Fix TEST_DATABASE_URL "
            "/ DATABASE_URL so it points at the test container."
        )


# Fail fast at import/collection time on the resolved TEST_DATABASE_URL.
_test_url = urlparse(TEST_DATABASE_URL)
_assert_not_prod_db(
    _test_url.hostname, _test_url.port, (_test_url.path or "").lstrip("/")
)


# --- Live PROD DB URL for read-only canary / eval tests ---------------------
# The ``live_db``-marked canary tests query the real prod corpus (read-only) to
# guard search ranking against silent regressions. They must reach PROD (host
# port 55432, db ``second_brain``) — NOT the test DB that the session autouse
# fixture pins into ``os.environ["DATABASE_URL"]``. Resolve the prod URL from an
# explicit ``BRAIN_PROD_DATABASE_URL`` override, else the repo ``.env`` FILE
# (read directly via ``dotenv_values`` so the pinned env var is bypassed), else
# the canonical default. Never returns a ``*_test`` URL: a test-DB value means
# "prod not configured", so canaries skip (unreachable) rather than run against
# the empty test corpus. All canary queries are read-only SELECTs.
_DEFAULT_PROD_DB_URL = "postgresql://brain:brain@localhost:55432/second_brain"


def prod_database_url() -> str:
    """Resolve the live PROD ``DATABASE_URL`` for read-only canary/eval tests."""
    override = os.environ.get("BRAIN_PROD_DATABASE_URL")
    if override:
        return override
    repo_env = Path(__file__).resolve().parent.parent / ".env"
    if repo_env.exists():
        value = dotenv_values(repo_env).get("DATABASE_URL")
        if value and not value.rstrip("/").endswith("/second_brain_test"):
            return value
    return _DEFAULT_PROD_DB_URL


@pytest.fixture(autouse=True, scope="session")
def _force_test_vault_path(tmp_path_factory: pytest.TempPathFactory) -> Iterator[None]:
    """Prevent the test suite from writing fixtures into ~/brain-vault.

    Without this, any test that invokes a CLI ingest command without an
    explicit ``monkeypatch.setenv("BRAIN_VAULT_PATH", str(tmp_path))``
    override will resolve ``Config.load().vault_path`` to the user's real
    ``~/brain-vault`` and write vault-mirror files there.  On 2026-05-08/09
    ~50 test fixtures leaked into the prod vault, causing
    ``unique constraint violation (mirror drift)`` errors that saturated the
    file watcher.

    Sets ``os.environ["BRAIN_VAULT_PATH"]`` to a session-scoped tmp dir for
    the entire pytest session — mirroring the pattern used by
    :func:`_force_test_database_url` for ``DATABASE_URL``.  Per-test
    ``monkeypatch.setenv("BRAIN_VAULT_PATH", str(tmp_path))`` calls still
    work — ``monkeypatch`` undoes itself per-test, after which this
    session-scope assignment continues to hold.

    Hard-fails immediately if ``BRAIN_VAULT_PATH`` is already set to the real
    vault at session start (e.g., exported in the shell environment).
    """
    real_vault = (Path.home() / "brain-vault").resolve()

    # Pre-flight: fail now rather than silently leak into the live corpus.
    current_env = os.environ.get("BRAIN_VAULT_PATH")
    if current_env is not None:
        current_resolved = Path(current_env).expanduser().resolve()
        assert current_resolved != real_vault, (
            f"BRAIN_VAULT_PATH is already set to the real vault ({real_vault})! "
            "Running the test suite would write test fixtures into your live "
            "knowledge base. Unset BRAIN_VAULT_PATH or point it at a test directory."
        )

    session_vault = tmp_path_factory.mktemp("vault")
    original = os.environ.get("BRAIN_VAULT_PATH")
    os.environ["BRAIN_VAULT_PATH"] = str(session_vault)
    try:
        yield
    finally:
        if original is None:
            os.environ.pop("BRAIN_VAULT_PATH", None)
        else:
            os.environ["BRAIN_VAULT_PATH"] = original


@pytest.fixture(autouse=True, scope="session")
def _force_test_runtime_home(tmp_path_factory: pytest.TempPathFactory) -> Iterator[None]:
    """Isolate the runtime-health inputs `brain doctor` reads (C4).

    The ``config`` / ``daemons`` checks in :mod:`brain.doctor_runtime` inspect
    state OUTSIDE the current process — ``$BRAIN_HOME/.env`` and the installed
    LaunchAgents — which is precisely what makes them able to catch a daemon
    outage, and precisely what would otherwise make every `brain doctor` test
    depend on the developer's machine:

    * ``BRAIN_LAUNCHD_DIR`` → a session tmp dir, so the daemon check reports
      "not installed" instead of reading the real ``~/Library/LaunchAgents``
      and inheriting whatever exit status the user's real daemons last had.
    * ``BRAIN_HOME`` → a session tmp dir holding a synthetic ``.env``, so the
      config check passes deterministically. Without it the check resolves
      ``$BRAIN_HOME`` to the repo root, which has a developer ``.env`` locally
      but NOT in CI (it is gitignored) — the suite would pass here and fail
      there.

    Mirrors :func:`_force_test_vault_path`: session-scoped so per-test
    ``monkeypatch.setenv`` overrides still work and self-restore afterwards.
    """
    runtime_home = tmp_path_factory.mktemp("brain_home")
    # Synthetic only — never copy the real ~/.brain/.env or the repo .env,
    # which hold live secrets (CLAUDE.md Rule 15 / the no-secrets rule).
    (runtime_home / ".env").write_text(
        f"DATABASE_URL={TEST_DATABASE_URL}\n", encoding="utf-8"
    )
    launchd_dir = tmp_path_factory.mktemp("launchagents")

    originals = {
        "BRAIN_HOME": os.environ.get("BRAIN_HOME"),
        "BRAIN_LAUNCHD_DIR": os.environ.get("BRAIN_LAUNCHD_DIR"),
    }
    os.environ["BRAIN_HOME"] = str(runtime_home)
    os.environ["BRAIN_LAUNCHD_DIR"] = str(launchd_dir)
    try:
        yield
    finally:
        for key, original in originals.items():
            if original is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = original


@pytest.fixture(autouse=True, scope="session")
def _force_test_database_url() -> Iterator[None]:
    """Bulletproof prod-DB isolation for the entire test session.

    Without this, any test that uses ``CliRunner().invoke(app, ...)`` without
    first calling ``_patch_embedder`` (or its local copies) reads
    ``DATABASE_URL`` from ``.env`` (= PROD) inside
    ``brain.config.Config.load()``. On 2026-05-04 the post-merge full-suite
    run leaked 15 test fixtures (titles like "Renamed Document",
    "Sample Heading", "person-x sync") into the real ``second_brain`` DB before
    being noticed. This fixture forces
    ``os.environ["DATABASE_URL"] = TEST_DATABASE_URL`` for the whole pytest
    session and restores the original at teardown.

    Per-test ``monkeypatch.setenv("DATABASE_URL", TEST_DATABASE_URL)`` calls
    in individual fixtures (``patch_embedder``, ``_patch_embedder`` siblings)
    still work — ``monkeypatch`` undoes itself per-test, after which this
    session-scope assignment continues to hold. There is no ordering
    requirement; tests that already use those helpers don't need to change.
    """
    original = os.environ.get("DATABASE_URL")
    os.environ["DATABASE_URL"] = TEST_DATABASE_URL
    try:
        yield
    finally:
        if original is None:
            os.environ.pop("DATABASE_URL", None)
        else:
            os.environ["DATABASE_URL"] = original


@pytest.fixture(autouse=True, scope="session")
def _exclusive_test_database() -> Iterator[None]:
    """Refuse to run when another pytest session already owns the test DB.

    Every ``test_db`` consumer resets state by ``TRUNCATE``-ing the shared
    tables, which is safe exactly once per database. Two suites pointed at the
    same ``TEST_DATABASE_URL`` truncate each other's fixtures mid-test.

    On 2026-07-26 two concurrent full-suite runs produced two entirely disjoint
    failure sets — 2 failures in ``tests/test_vault_sync.py`` on one run, 11
    failures across six unrelated files on the next, several surfacing as raw
    ``psycopg`` errors. All of them passed when re-run alone, and about an hour
    went into hunting a production bug that did not exist.

    Same spirit as :func:`_force_test_database_url` and
    :func:`_force_test_vault_path`: fail immediately, and say why, rather than
    letting the suite produce confident nonsense.

    The lock is a Postgres *session*-level advisory lock, so it is released
    automatically when this process exits — a crashed or killed run never
    leaves the database wedged.
    """
    conn = psycopg.connect(TEST_DATABASE_URL, connect_timeout=5)
    with conn.cursor() as cur:
        if not try_acquire_suite_lock(cur):
            conn.close()
            pytest.exit(concurrent_suite_message(TEST_DATABASE_URL), returncode=1)
    try:
        yield
    finally:
        conn.close()  # releases the advisory lock


@pytest.fixture(autouse=True, scope="session")
def _force_graph_flags_default() -> Iterator[None]:
    """Isolate the whole suite from the local ``.env``'s GraphRAG feature flags.

    ``Config.load()`` reads the ``.env`` FILE via ``dotenv_values`` (not just
    ``os.environ``) and applies it with ``os.environ.setdefault`` — so a flag
    present in the repo ``.env`` flows into every ``Config.load()`` unless that
    key is ALREADY in ``os.environ``. Once the operational ``.env`` enables the
    graph flags (``BRAIN_GRAPH_ENABLED`` for the prod cutover,
    ``BRAIN_GRAPH_CONCEPTS`` for the concept backfill), any test asserting the
    *disabled* default silently flips — and a bare ``monkeypatch.delenv`` does
    NOT help, because the file value is re-injected after the delete.

    Mirroring :func:`_force_test_database_url`, force both flags to an EMPTY
    string for the session (empty -> the parser uses the code default, currently
    enabled for both flags after the 2026-05-26 default-on flip), so
    ``os.environ.setdefault`` skips the ``.env`` value. Per-test
    ``monkeypatch.setenv("BRAIN_GRAPH_ENABLED", "true")`` in the enabled-path
    tests still overrides this and undoes itself per-test.
    """
    keys = ("BRAIN_GRAPH_ENABLED", "BRAIN_GRAPH_CONCEPTS")
    originals = {key: os.environ.get(key) for key in keys}
    # Honor a genuine operator export, but NOT a value that merely arrived from
    # the repo ``.env`` via the module-level ``load_dotenv()`` at import time.
    # By the time this fixture runs the two are indistinguishable in
    # ``os.environ`` — a naive ``if key not in os.environ`` check therefore
    # hands control straight back to ``.env``, defeating the isolation this
    # fixture exists for (``.env`` currently sets BOTH flags to ``true``).
    # ``dotenv_values()`` re-reads the FILE without touching the environment,
    # so a key whose live value still equals the file's is treated as
    # file-provided and gets neutralized.
    from_dotenv = dotenv_values() or {}
    for key in keys:
        came_from_env_file = (
            key in from_dotenv and os.environ.get(key) == from_dotenv[key]
        )
        if key in os.environ and not came_from_env_file:
            continue  # explicit external override — respect it
        os.environ[key] = ""
    try:
        yield
    finally:
        for key, original in originals.items():
            if original is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = original


def _age_graph_exists(conn: psycopg.Connection, name: str = "brain_graph") -> bool:
    """True iff AGE's catalog exists AND holds a graph called ``name``.

    Deliberately answerable with **plain SQL on plain tables** — no
    ``CREATE EXTENSION``, no ``LOAD 'age'``. ``ag_graph`` is an ordinary table
    in the ``ag_catalog`` schema, and ``to_regclass`` is core Postgres, so this
    probe is safe on a database that has never seen AGE (it returns NULL rather
    than erroring on a missing schema). That is what lets
    :func:`_reset_age_graph` decide whether any AGE work is needed *before*
    doing AGE work.
    """
    reg = conn.execute("SELECT to_regclass('ag_catalog.ag_graph')").fetchone()
    if reg is None or reg[0] is None:
        return False
    row = conn.execute(
        "SELECT 1 FROM ag_catalog.ag_graph WHERE name = %s", (name,)
    ).fetchone()
    return row is not None


#: How many connections :func:`_reset_age_graph` will burn dropping one graph.
#:
#: Two, not more: a fresh backend starts with an EMPTY AGE label cache, so the
#: only failure the retry exists to absorb — a cache poisoned by the drop itself
#: — cannot recur on the second attempt. A third attempt would only ever mask a
#: different, real problem (the graph genuinely refusing to drop) as a slow
#: success, so the loop stops where its justification does.
_AGE_DROP_ATTEMPTS = 2


def _open_age_connection() -> psycopg.Connection:
    """A throwaway autocommit connection dedicated to AGE catalog work.

    Deliberately separate from the caller's connection — see
    :func:`_reset_age_graph` for why that separation is the whole fix. Autocommit
    because AGE catalog DDL wants explicit commits under psycopg v3.
    """
    conn = psycopg.connect(TEST_DATABASE_URL, connect_timeout=5)
    conn.autocommit = True
    return conn


def _reset_age_graph(
    conn: psycopg.Connection,
    *,
    open_age_conn: Callable[[], psycopg.Connection] | None = None,
) -> None:
    """Drop the canonical AGE graph — but only when one actually exists.

    AGE keeps graphs in the ``ag_catalog`` schema plus a per-graph schema —
    neither lives in ``public``, so the ``DROP SCHEMA public CASCADE`` performed
    by :func:`_reset_schema_and_migrate` does NOT clear them. We explicitly drop
    ``brain_graph`` (the canonical graph the G0-2 bootstrap creates) so each
    test starts from a clean graph, mirroring that bootstrap.

    **Why the early-out is not just an optimisation.** This runs from
    :func:`_truncate_reset`, i.e. once per DB test. It used to issue
    ``CREATE EXTENSION IF NOT EXISTS age CASCADE`` + ``LOAD 'age'``
    unconditionally, so *every* test backend loaded AGE and populated its
    per-backend label cache — including the large majority that never touch the
    graph. AGE invalidates that cache poorly across sessions when graphs are
    dropped and recreated, and the result is::

        ERROR:  label (relation) cache corrupted
        FATAL:  terminating connection because protocol synchronization was lost

    which **kills the connection**, so the failure surfaces in the *next* test's
    setup and indicts an innocent bystander. 140 such events are in the test
    container's log, clustered on 2026-07-26, 2026-08-06 and 2026-08-07 — the
    first two being the dates ``tests/db_lock.py`` cites as the
    mysterious-disjoint-failure incidents that motivated the suite lock.

    The repo already knew this hazard from one direction: the standing rule
    against ``--faulthandler-timeout`` exists because its hard kill corrupts the
    AGE label cache and needs a ``DROP DATABASE`` to recover. What was missed is
    that ordinary per-test reset churn reaches the same state with no hard kill
    anywhere. Not touching AGE at all on the ~83% of resets that have no graph
    (measured: 24 of 141 on a 206-test slice) removes most of the exposure.

    Correctness is unchanged: any test that HAS a graph still gets the full
    drop, and any test that USES AGE loads it through production code
    (:func:`brain.db.load_age` / :func:`brain.db.connect_graph`), which issues
    its own ``LOAD``. The extension object is created once per session by
    :func:`_reset_schema_and_migrate` instead of once per test.

    **Why the AGE work runs on a connection of its own.** The early-out above
    bounded how OFTEN a backend loads AGE; it did not change WHICH backend pays
    when it does. On the remaining ~17% of resets, ``conn`` is the connection
    :func:`_truncate_reset` is about to hand to the test (or the one
    :func:`_reset_schema_and_migrate` still has a schema reset to finish on) —
    and the corruption above lands on the connection that just ran
    ``drop_graph``, at its NEXT statement. Both survivors of the 2026-08-07 full
    run are that exact shape:

    * ``test_init_auto_runs_backfill`` errored in teardown on
      ``SELECT 1 FROM pg_extension WHERE extname='vector'`` — the statement
      immediately after this function returned;
    * ``test_cli_agent_flag::test_search_and_ingest_agree_on_the_env_var``
      passed both of its CLI invocations and then died on the verification
      ``SELECT`` — the first use of ``test_db`` after the reset.

    It is also self-sustaining: a drop that dies mid-flight leaves the graph in
    place, so the next test's reset takes the AGE path again and can be poisoned
    again. Two consecutive tests failing that way is the observed signature.

    So the AGE statements go to a throwaway connection that is closed
    immediately, and a poisoned attempt is retried once on a fresh one (a new
    backend starts with an empty label cache). ``conn`` never loads AGE, so it
    cannot be poisoned by this function at all, and a corrupted attempt degrades
    to a retry instead of killing a test. Reproduced at ~1 run in 12 before,
    0 in 40 after.

    Autocommit throughout because AGE catalog DDL wants explicit commits under
    psycopg v3. Nothing resets ``search_path`` any more, and nothing needs to:
    it is set on a connection that is discarded microseconds later, so the
    ``ag_catalog`` namespace can no longer leak onto the session whose later DDL
    must land in ``public``.

    :param open_age_conn: factory for the throwaway AGE connection. Injectable
        so tests can observe which connection the statements land on; resolved
        at CALL time rather than as a signature default, matching
        :func:`brain.db.migration_lock` — a default binds once at import and
        would silently ignore the injection.
    """
    if not _age_graph_exists(conn):
        return

    open_conn = _open_age_connection if open_age_conn is None else open_age_conn
    last_error: Exception | None = None

    for _ in range(_AGE_DROP_ATTEMPTS):
        age_conn = open_conn()
        try:
            # A graph exists, so AGE work is genuinely required: LOAD makes
            # drop_graph callable in this session.
            age_conn.execute("LOAD 'age'")
            age_conn.execute('SET search_path = ag_catalog, "$user", public')
            age_conn.execute("SELECT drop_graph('brain_graph', true)")
            return
        except (psycopg.errors.InternalError, psycopg.OperationalError) as exc:
            # The AGE label-cache corruption and the dead connection it leaves
            # behind. Both are recoverable on a fresh backend; anything else is
            # a real failure and propagates untouched.
            last_error = exc
        finally:
            age_conn.close()

    if last_error is None:  # pragma: no cover - the loop always attempts once
        raise RuntimeError("AGE graph reset made no attempt")
    raise last_error


def _reset_schema_and_migrate(conn: psycopg.Connection) -> None:
    """Drop and recreate the public schema then run all migrations.

    ``DROP SCHEMA public CASCADE`` orphans the vector and pgcrypto extensions
    in pg_extension (the namespace OID dies, but the pg_extension row stays).
    A subsequent ``CREATE EXTENSION IF NOT EXISTS vector`` then silently no-ops
    because pg_extension still has the row — leaving the new public schema
    without the vector type.

    Workaround for vector: move it to pg_catalog (which survives the DROP)
    before dropping the schema, then move it back to public afterwards.
    pgcrypto's gen_random_uuid() conflicts with pg_catalog, so we handle it
    by dropping and reinstalling it explicitly.

    AGE graph state lives in ``ag_catalog`` (outside ``public``) and so
    survives the schema drop; :func:`_reset_age_graph` clears the canonical
    ``brain_graph`` first so each test gets a clean graph.

    The whole body runs under :func:`brain.db.migration_lock`, which is what
    stops a non-pytest writer — ``brain init`` / ``brain demo`` /
    ``brain backup restore`` with ``DATABASE_URL`` pointed at a test database —
    from landing inside this window. The suite-exclusivity lock in
    :mod:`tests.db_lock` only excludes other *pytest* sessions; it cannot see a
    bare CLI invocation. Wrapping here rather than relying on
    :func:`run_migrations`' own acquisition matters because the destructive
    half (``DROP SCHEMA public CASCADE``) runs BEFORE that call. The lock is
    re-entrant, so the nested acquire inside ``run_migrations`` is a no-op.
    """
    # DB-SAFETY: guard the ACTUAL connection target (not just the env var) — if
    # this connection somehow points at the prod container, abort before the
    # destructive DROP SCHEMA below ever runs.
    _assert_not_prod_db(conn.info.host, conn.info.port, conn.info.dbname)

    with migration_lock(conn):
        # Ensure the AGE extension object exists. This lives HERE, on the
        # session-scoped / fresh_schema path, rather than in
        # :func:`_reset_age_graph` where it used to run once per test: the .so
        # ships in the AGE image but the extension object must be created once
        # per database, and once per database is exactly what this path is.
        conn.execute("CREATE EXTENSION IF NOT EXISTS age CASCADE")

        # Reset AGE graph state first — it lives outside public and would otherwise
        # leak across tests. Scopes ag_catalog to its own statements (no leakage).
        _reset_age_graph(conn)

        # Move vector to pg_catalog before the schema drop so it survives.
        # pgcrypto cannot go to pg_catalog (DuplicateFunction on gen_random_uuid).
        if conn.execute("SELECT 1 FROM pg_extension WHERE extname='vector'").fetchone():
            conn.execute("ALTER EXTENSION vector SET SCHEMA pg_catalog")

        # Drop pgcrypto explicitly: chunks has no pgcrypto dependency, so no
        # cascade needed.
        conn.execute("DROP EXTENSION IF EXISTS pgcrypto")

        conn.execute("DROP SCHEMA public CASCADE; CREATE SCHEMA public;")

        # Move vector back to public — run_migrations sees it there and skips
        # re-install.
        if conn.execute("SELECT 1 FROM pg_extension WHERE extname='vector'").fetchone():
            conn.execute("ALTER EXTENSION vector SET SCHEMA public")

        # pgcrypto is gone, so run_migrations will reinstall it from migration 001.
        run_migrations(conn)


@pytest.fixture(autouse=True, scope="session")
def _ensure_test_db_initialized(
    _force_test_database_url: None, _exclusive_test_database: None
) -> Iterator[None]:
    """Reset the test DB to a known migrated state at session start.

    Tests that don't take the per-test ``test_db`` fixture (e.g. doctor checks
    that the pgvector extension is installed) still need the schema to be in a
    migrated state. Doing this once per session — before any test runs — makes
    that starting state deterministic regardless of any prior aborted runs.

    Depends on :func:`_force_test_database_url` so the schema reset happens
    against ``second_brain_test``, never against prod.

    Also depends on :func:`_exclusive_test_database` — and that ordering is
    load-bearing, not decorative. Session-scoped autouse fixtures have no
    guaranteed order relative to one another, so without this parameter the
    ``DROP SCHEMA`` below can run *before* the concurrency lock is taken and
    destroy a rival suite's schema mid-run. Observed on 2026-07-26: the reset
    reached ``ALTER EXTENSION vector SET SCHEMA public`` on an already-``[BAD]``
    connection and every test errored in setup. Requesting the lock here makes
    pytest resolve it first, so a second suite exits cleanly before touching
    anything.
    """
    with connect(TEST_DATABASE_URL) as conn:
        conn.autocommit = True
        _reset_schema_and_migrate(conn)
    yield


def _truncate_reset(conn: psycopg.Connection) -> None:
    """Cheap per-test reset: empty every user table + reset the AGE graph.

    The migrate-once session fixture (:func:`_ensure_test_db_initialized`) applies
    all migrations exactly once at session start; this is the per-test cleanup that
    every test uses UNLESS it carries ``@pytest.mark.fresh_schema``. It touches
    DATA only — never the schema or extensions — so it is immune to the
    vector-extension orphaning that historically bricked the whole test DB when a
    ``DROP SCHEMA`` reset was killed mid-flight (see
    :func:`_reset_schema_and_migrate`). There is deliberately NO extension/schema
    shuffling here: the only DDL is ``TRUNCATE`` (data, not schema) plus the
    canonical-graph drop that mirrors the full reset.

    Two steps:

    1. :func:`_reset_age_graph` drops the canonical ``brain_graph``. AGE graph
       state lives in ``ag_catalog`` + a per-graph schema, NOT ``public``, so
       ``TRUNCATE`` on public tables cannot reach it; this mirrors exactly what the
       full reset does (and idempotently ensures the ``age`` extension exists).
    2. ``TRUNCATE ... RESTART IDENTITY CASCADE`` every base table in ``public``
       EXCEPT ``schema_migrations`` — that table records the migrate-once state and
       MUST survive, or the next test would find an unmigrated schema. Identifiers
       are schema-qualified and quoted via :class:`psycopg.sql.Identifier`.

    **Deliberately NOT under** :func:`brain.db.migration_lock`, unlike
    :func:`_reset_schema_and_migrate`. The lock exists to serialise SCHEMA
    mutation; this function touches DATA only (``TRUNCATE`` plus the AGE graph
    drop) and runs once per test, so taking a cross-process advisory lock
    thousands of times per suite would cost more than it protects. The genuine
    window it leaves is narrow — a bare ``brain init`` against the test DB
    concurrently with a TRUNCATE — and the pytest-vs-pytest case is already
    covered by the suite-exclusivity lock in :mod:`tests.db_lock`. Recorded
    here so the asymmetry reads as a decision rather than an oversight.

    Tests that mutate the schema itself (DDL — dropped indexes, resized embedding
    columns, re-run migrations, own-connection ``DROP SCHEMA``) must instead carry
    ``@pytest.mark.fresh_schema``; the :func:`test_db` fixture routes those to the
    full reset and :func:`_restore_baseline_after_fresh_schema` re-establishes the
    migrated-once baseline afterwards.
    """
    # DB-SAFETY: guard the ACTUAL connection target before any writes — mirrors
    # the guard in :func:`_reset_schema_and_migrate`.
    _assert_not_prod_db(conn.info.host, conn.info.port, conn.info.dbname)

    # AGE graph state lives outside ``public`` — TRUNCATE can't reach it. Drop the
    # canonical graph first, exactly as the full reset does.
    _reset_age_graph(conn)

    rows = conn.execute(
        "SELECT tablename FROM pg_tables "
        "WHERE schemaname = 'public' AND tablename <> 'schema_migrations'"
    ).fetchall()
    tables = [str(r[0]) for r in rows]
    if not tables:
        return
    stmt = sql.SQL("TRUNCATE TABLE {} RESTART IDENTITY CASCADE").format(
        sql.SQL(", ").join(sql.Identifier("public", t) for t in tables)
    )
    conn.execute(stmt)


@pytest.fixture
def test_db(request: pytest.FixtureRequest) -> Iterator[psycopg.Connection]:
    """Per-test test-DB connection with a clean starting state.

    Default (no marker): a cheap ``TRUNCATE`` reset (:func:`_truncate_reset`) on
    top of the schema migrated ONCE per session — data cleared, schema untouched.

    ``@pytest.mark.fresh_schema``: the full ``DROP SCHEMA`` + migrate reset
    (:func:`_reset_schema_and_migrate`), for tests that mutate the schema itself.
    :func:`_restore_baseline_after_fresh_schema` restores the migrated-once
    baseline after such a test so the next TRUNCATE-only test is not poisoned.
    """
    with connect(TEST_DATABASE_URL) as conn:
        conn.autocommit = True
        if request.node.get_closest_marker("fresh_schema"):
            _reset_schema_and_migrate(conn)
        else:
            _truncate_reset(conn)
        yield conn


@pytest.fixture(autouse=True)
def _restore_baseline_after_fresh_schema(
    request: pytest.FixtureRequest,
) -> Iterator[None]:
    """Restore the migrated-once baseline after any ``fresh_schema`` test.

    ``fresh_schema`` tests mutate the schema (dropped indexes, resized embedding
    columns, re-run migrations, own-connection ``DROP SCHEMA``). The per-test
    :func:`_truncate_reset` that every OTHER test uses clears DATA only — it cannot
    undo a schema mutation. So after a ``fresh_schema`` test finishes we run the
    full drop+migrate reset once, re-establishing the baseline the next
    TRUNCATE-only test relies on. This also covers tests that DDL on their own
    connection and never take the :func:`test_db` fixture (e.g. the
    ``run_migrations`` tests in ``test_db.py``).

    Autouse so it fires for EVERY test, but the teardown is just a marker check for
    the ~5.3k non-schema tests — only the handful of ``fresh_schema`` tests pay the
    reset.
    """
    yield
    if request.node.get_closest_marker("fresh_schema"):
        with connect(TEST_DATABASE_URL) as conn:
            conn.autocommit = True
            _reset_schema_and_migrate(conn)


class FakeEmbedder:
    """Deterministic embedder for tests — hashes text into a stable vector.

    Default ``dim`` matches the qwen3 production schema (4096); pass a
    different ``dim`` to construct vectors at other sizes if a test needs
    it (e.g. 1024 for the arctic / voyage backend paths). ``dim`` is
    exposed as an instance attribute to satisfy the
    :class:`brain.ingest.Embedder` Protocol.
    """

    def __init__(self, dim: int = 4096) -> None:
        self.dim = dim

    def embed(self, texts: list[str], input_type: str = "document") -> list[list[float]]:
        return [self._vec(t, input_type) for t in texts]

    def count_tokens(self, text: str) -> int:
        # rough approximation: 1 token per ~4 chars
        return max(1, len(text) // 4)

    def _vec(self, text: str, input_type: str) -> list[float]:
        h = hashlib.sha256((input_type + ":" + text).encode()).digest()
        # 32 bytes → 32 floats, tiled to ``self.dim``.
        floats = [b / 255.0 - 0.5 for b in h]
        # Tile enough copies to cover ``dim``, then truncate.
        repeats = (self.dim + len(floats) - 1) // len(floats)
        return (floats * repeats)[: self.dim]


@pytest.fixture
def fake_embedder() -> FakeEmbedder:
    return FakeEmbedder()


class CountingEmbedder:
    """Wrap an embedder to count embed calls — used by `brain edit` tests
    that need to assert the title-only path didn't re-embed.

    Mirrors the wrapped embedder's ``dim`` so it satisfies the same Protocol.
    """

    def __init__(self, inner: Any) -> None:
        self._inner = inner
        self.dim = inner.dim
        self.embed_calls = 0
        self.token_calls = 0

    def embed(
        self, texts: list[str], *, input_type: str = "document"
    ) -> list[list[float]]:
        self.embed_calls += 1
        return self._inner.embed(texts, input_type=input_type)  # type: ignore[no-any-return]

    def count_tokens(self, text: str) -> int:
        self.token_calls += 1
        return self._inner.count_tokens(text)  # type: ignore[no-any-return]


@pytest.fixture
def counting_embedder(fake_embedder: FakeEmbedder) -> CountingEmbedder:
    return CountingEmbedder(fake_embedder)


class FakeRunner:
    """Test double for :class:`brain.vault.derived_links.directory.GwsRunner`.

    Records every ``args`` list it's invoked with so tests can assert the
    Calendar/Contacts subcommand shape. ``response`` defaults to an empty
    JSON list (a valid empty success that still advances the high-water
    mark in ``directory_refresh_state``). ``raises`` simulates a
    subprocess failure — ``refresh_calendar`` / ``refresh_contacts``
    catch ``(OSError, DirectoryRefreshError, RuntimeError)`` from the
    runner and downgrade them to soft warnings, so most call sites pass
    a ``RuntimeError`` here to exercise that branch without dragging in
    real subprocess machinery.

    Importable via ``from tests.conftest import FakeRunner`` — same
    pattern as :class:`FakeEmbedder` above.
    """

    def __init__(
        self,
        *,
        response: str = "[]",
        raises: BaseException | None = None,
    ) -> None:
        self.response = response
        self.raises = raises
        self.calls: list[list[str]] = []

    def __call__(self, args: list[str]) -> str:
        self.calls.append(list(args))
        if self.raises is not None:
            raise self.raises
        return self.response


@pytest.fixture
def patch_embedder(monkeypatch: pytest.MonkeyPatch) -> Callable[[object], None]:
    """Wire ``DATABASE_URL`` env var and swap ``brain.cli._build_embedder`` to
    return ``embedder``. Returns a callable so tests pick which embedder
    (fake vs counting) to install."""

    def _install(embedder: object) -> None:
        monkeypatch.setenv("DATABASE_URL", TEST_DATABASE_URL)
        monkeypatch.setattr("brain.cli._build_embedder", lambda cfg: embedder)

    return _install


@pytest.fixture
def seed_doc(
    test_db: psycopg.Connection, fake_embedder: FakeEmbedder
) -> Callable[..., str]:
    """Factory fixture: ingest a manual document and return its UUID.

    Defaults match the simplest test seed; override any kwarg as needed."""

    def _seed(
        *,
        title: str = "Initial Title",
        content: str = "Initial body content.",
        content_type: str = "note",
        metadata: dict[str, Any] | None = None,
        tags: list[str] | None = None,
    ) -> str:
        result = ingest_document(
            test_db,
            embedder=fake_embedder,
            doc=ExtractedDoc(
                title=title,
                content=content,
                content_type=content_type,
                source_path=None,
                metadata=metadata or {},
            ),
            source_kind="manual",
            tags=tags or [],
        )
        assert result.document_id is not None
        return result.document_id

    return _seed


@pytest.fixture
def fixtures_dir() -> Path:
    return Path(__file__).parent / "fixtures"


# ---------------------------------------------------------------------------
# LLM hermeticity — no test reaches a live Ollama unless it opts in.
# ---------------------------------------------------------------------------
# Ingesting a document fires TWO independent LLM round-trips: the post-ingest
# auto-summary hook (via ``cli._build_enricher``) and, because
# ``BRAIN_GRAPH_CONCEPTS`` defaults on, concept extraction inside the graph
# syncer. Neither was stubbed, so `ingest-dir` over the fixtures directory —
# including the 51 KB ``playwright_tree.txt`` — made one live call per document.
# Measured at ~19-27 s each against a single-slot Ollama, which turned
# ``test_cli_auto_analyze`` into a 5-minute-per-test crawl that reads exactly
# like a deadlock: 0% CPU, and Postgres reporting ``idle in transaction`` /
# ``ClientRead`` because the client is parked on an outbound socket while
# holding a transaction open.
#
# Two layers below, with SEPARATE opt-outs because they answer different
# questions. ``_stub_llm_backends`` supplies fast deterministic doubles at the
# two production seams — opt out with ``@pytest.mark.real_llm_backends`` when a
# test asserts on the concrete enricher/extractor types. ``_forbid_live_ollama``
# is the backstop that makes any *remaining* outbound call fail loudly rather
# than silently degrade — opt out with ``@pytest.mark.live_ollama`` only when
# reaching a real endpoint IS the test. A factory test needs the first marker
# and not the second, so collapsing them into one would quietly drop the
# network guard from tests that should still have it.

def _port_of(host: str) -> int:
    """The TCP port a client would actually dial for ``host``.

    An explicit ``:port`` wins; otherwise the scheme's default, which is what
    httpx does with a base URL. Shared by the guard and its fallback so the two
    cannot disagree about what "the Ollama port" means.
    """
    parsed = urlparse(host if "//" in host else f"http://{host}")
    if parsed.port is not None:
        return parsed.port
    return 443 if parsed.scheme == "https" else 80


#: The port ``_forbid_live_ollama`` bans when ``OLLAMA_HOST`` is unset.
#:
#: DERIVED from ``brain.config.DEFAULT_OLLAMA_HOST``, never hardcoded. A second
#: literal here would be a second source of truth for one value: the day the
#: config default moves, the guard would go on banning the OLD port — a port
#: nothing dials. It would stop guarding silently, every eval gate would keep
#: skipping for the *wrong* reason (a plain connection refusal rather than the
#: guard standing down), and nothing would turn red. That near-miss is real:
#: redirecting only one of the two sources made a `live_ollama` counterfactual
#: appear to refute the marker's whole purpose.
#: Pinned by tests/test_conftest_ollama_port.py.
_OLLAMA_DEFAULT_PORT: int = _port_of(DEFAULT_OLLAMA_HOST)


def pytest_configure(config: pytest.Config) -> None:
    """Register markers owned by this file.

    Declared here rather than in ``pyproject.toml`` so the marker ships with
    the fixtures that implement it — and because ``--strict-markers`` is on,
    an unregistered marker is a hard error rather than a warning.
    """
    config.addinivalue_line(
        "markers",
        "real_llm_backends: build the REAL enricher / extractor objects "
        "instead of the default doubles (for factory tests that assert on "
        "the concrete types; implies no network by itself)",
    )
    config.addinivalue_line(
        "markers",
        "live_ollama: test deliberately opens a connection to a real Ollama "
        "endpoint (disables the live-connection guard)",
    )


def _ollama_port() -> int:
    """The port the active config would reach Ollama on.

    ``OLLAMA_HOST`` overrides — the operator-facing knob, and the same one
    ``Config.load()`` honours. With it unset we fall back to the application's
    own default rather than to a literal of our own, so the guard always bans
    the port the code under test actually dials.
    """
    return _port_of(os.environ.get("OLLAMA_HOST") or DEFAULT_OLLAMA_HOST)


class LiveOllamaForbidden(BaseException):
    """A test opened a socket to Ollama without ``@pytest.mark.live_ollama``.

    Inherits :class:`BaseException` **deliberately**. Both LLM surfaces are
    contractually never-raise around transport failures — ``OllamaExtractor.
    extract`` swallows an outage and returns ``[]``, and the enrich hook
    catches :class:`OllamaUnavailable` — so an ``Exception`` subclass would be
    caught and logged at WARN. That is precisely the silent degradation this
    guard exists to make loud.
    """


def _fake_ollama_transport() -> Any:
    """An ``httpx.MockTransport`` answering every Ollama chat call.

    The canned body carries the union of the keys the enricher's parsers look
    for (``summary`` at ``enrichment.py:435``, ``tags`` at ``:477``), so one
    handler satisfies ``summarize`` / ``propose_tags`` / the group-summary
    variants without per-test wiring.
    """
    import json as _json

    import httpx

    payload = _json.dumps(
        {"summary": "Synthetic test summary.", "tags": [], "entities": []}
    )

    def _handler(request: Any) -> Any:
        return httpx.Response(
            200,
            json={
                "message": {"role": "assistant", "content": payload},
                "done": True,
            },
        )

    return httpx.MockTransport(_handler)


def build_fake_enricher() -> Any:
    """A real ``OllamaEnricher`` wired to a mock transport — no network.

    Deliberately the REAL class rather than a hand-rolled stub: all ten public
    methods keep their genuine parsing/validation logic, so a test exercising
    ``propose_tags`` or ``summarize_group`` gets true behaviour, and
    ``isinstance(x, OllamaEnricher)`` still holds. This is the pattern
    ``tests/test_enrichment.py`` and ``OllamaExtractor``'s own docstring
    already establish.
    """
    import httpx

    from brain.enrichment import OllamaEnricher

    return OllamaEnricher(
        host="http://fake-ollama.test",
        model="fake-model:test",
        client=httpx.Client(
            base_url="http://fake-ollama.test", transport=_fake_ollama_transport()
        ),
    )


class FakeEntityExtractor:
    """Deterministic ``EntityExtractor`` double — the graph concept seam.

    Returns no entities: the person aspect (which needs no LLM) still
    reconciles normally, and the concept watermark is still written, so the
    graph stays consistent. Tests that care about concept CONTENT inject their
    own extractor via ``make_graph_syncer(extractor=...)``, which this default
    never overrides.
    """

    @property
    def version(self) -> str:
        return "fake-extractor@test"

    def extract(self, text: str) -> list[Any]:
        return []


@pytest.fixture
def fake_enricher() -> Any:
    """Explicit handle on the same double ``_stub_llm_backends`` installs."""
    return build_fake_enricher()


@pytest.fixture(autouse=True)
def _stub_llm_backends(
    request: pytest.FixtureRequest, monkeypatch: pytest.MonkeyPatch
) -> Iterator[None]:
    """Default every LLM seam to a fast local double.

    Two seams cover all the LLM traffic an ingest can generate:

    - ``brain.cli._build_enricher`` — the auto-summary hook. Already the
      documented monkeypatch point; ``cli_ingest`` delegates to it.
    - ``brain.graph_rag.extract.make_extractor`` — concept extraction. Patched
      at the FACTORY rather than at ``make_graph_syncer``, because all three
      consumers (``cli.py`` graphrag build, ``mcp_server.py``, and
      ``graph_rag/sync.py``) import it function-locally and therefore resolve
      it at call time. Stubbing ``make_graph_syncer`` instead would inject a
      double *ahead* of the fake a test had already installed on
      ``make_extractor`` — silently defeating it, which is how the first cut
      of this fixture broke ``test_graphrag_concepts``.

    This only sets a DEFAULT: a test that patches the same attribute itself
    (``test_cli_enrich.py``, ``test_graphrag_concepts.py``) applies its patch
    afterwards and wins. So the blast radius is exactly the set of tests that
    were silently relying on a live Ollama.

    ``brain.enrichment.make_enricher`` is deliberately left alone — its unit
    tests assert on the concrete return type. Factory tests for
    ``make_extractor`` opt out with ``@pytest.mark.real_llm_backends``.
    """
    if request.node.get_closest_marker("real_llm_backends"):
        yield
        return

    monkeypatch.setattr(
        "brain.cli._build_enricher", lambda cfg: build_fake_enricher()
    )
    monkeypatch.setattr(
        "brain.graph_rag.extract.make_extractor", lambda cfg: FakeEntityExtractor()
    )
    yield


@pytest.fixture(autouse=True)
def _forbid_live_ollama(
    request: pytest.FixtureRequest, monkeypatch: pytest.MonkeyPatch
) -> Iterator[None]:
    """Fail loudly if anything still opens a socket to Ollama.

    The stubs above remove the KNOWN call sites; this catches the next one
    somebody adds. Guards the Ollama port only — Postgres (5434) and every
    other destination pass through untouched.
    """
    if request.node.get_closest_marker("live_ollama"):
        yield
        return

    import socket

    port = _ollama_port()
    real_connect = socket.socket.connect

    def _guarded(self: Any, address: Any, *args: Any, **kwargs: Any) -> Any:
        if isinstance(address, tuple) and len(address) >= 2 and address[1] == port:
            raise LiveOllamaForbidden(
                f"{request.node.nodeid} tried to connect to Ollama at "
                f"{address[0]}:{address[1]}. Tests must not call a live LLM: "
                "inject a double at the seam (see `_stub_llm_backends`), or "
                "mark the test `@pytest.mark.live_ollama` if the live call "
                "is the point."
            )
        return real_connect(self, address, *args, **kwargs)

    monkeypatch.setattr(socket.socket, "connect", _guarded)
    yield
