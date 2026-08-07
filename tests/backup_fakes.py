"""Shared test doubles for the `brain backup` / `brain restore` suites (F3 §8).

Importable as ``from tests.backup_fakes import RecordingRunner`` — the same
convention as :class:`tests.conftest.FakeEmbedder` / :class:`FakeRunner`.

The runner here is deliberately paranoid: it is the single boundary through
which :mod:`brain.backup` spawns ``pg_dump`` / ``pg_restore``, so it doubles as
the suite's production-safety tripwire. Every recorded argv is scanned for a
production DSN via :func:`tests.conftest._looks_like_prod_db`; if a regression
ever routes a real dump at port 55432 / database ``second_brain``, the test
explodes instead of touching the user's corpus.
"""
from __future__ import annotations

import subprocess
from collections.abc import Iterator, Mapping, Sequence
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import pytest

from tests.conftest import _looks_like_prod_db

#: The checkout these tests run inside. Nothing a test double writes may land
#: here — see :func:`assert_write_is_sandboxed`.
REPO_ROOT = Path(__file__).resolve().parent.parent


class ProdDsnLeak(AssertionError):
    """A fake runner was handed something that resolves to the prod database."""


class LiveSuiteDatabaseDrop(AssertionError):
    """Raised when a teardown would drop the database the suite is running on.

    C22 (2026-08-07). :func:`drop_restore_artifacts` reclaims a parked database
    by ``DROP DATABASE {live} WITH (FORCE)`` — and ``FORCE`` exists precisely to
    terminate other backends. When ``live`` is the database the pytest session
    itself is connected to, those "other backends" are the session's own: the
    suite-exclusivity lock connection and every pooled connection. Postgres
    terminated them, a backend exited with code 2, the postmaster crash-restarted
    the whole instance, and ``second_brain_test`` was left ``datconnlimit = -2``.
    It happened twice, ~45 minutes apart.

    The existing :class:`ProdDsnLeak` guard protects PRODUCTION from the tests.
    Nothing protected the TEST database from a test — a full suite could destroy
    its own substrate. This closes that asymmetry.
    """


class SandboxEscape(AssertionError):
    """A test double tried to write inside the checkout instead of ``tmp_path``."""


def assert_write_is_sandboxed(path: Path) -> None:
    """Refuse a host write that would land inside the checkout.

    A double that materialises a file MUST stay in ``tmp_path``. This is not
    tidiness: on 2026-07-26 a `docker cp` double treated the destination
    ``second-brain-postgres:/tmp/x.dump`` as a local path and created a
    directory literally named ``second-brain-postgres:`` in the repo root — a
    name that is not even legal to check out on some filesystems, and one that
    ``git add -A`` would have committed. The feature under test overwrites
    databases and vault trees, so a double that can escape its sandbox is the
    exact shape of defect that must fail loudly and immediately.
    """
    resolved = path.resolve()
    if resolved == REPO_ROOT or REPO_ROOT in resolved.parents:
        raise SandboxEscape(
            f"test double tried to write inside the checkout: {resolved}. "
            "Doubles must write only under tmp_path."
        )


def materialise_copy_out(argv: Sequence[str], payload: bytes) -> bool:
    """Play the part of ``docker cp <container>:<remote> <host-path>``.

    Returns True when a host file was written. The copy *into* the container
    (destination of the form ``<container>:/path``) writes nothing — that
    direction has no host side, and treating it as a path is precisely the bug
    :func:`assert_write_is_sandboxed` now guards.
    """
    parts = list(argv)
    if parts[:2] != ["docker", "cp"] or ":" in parts[-1]:
        return False
    destination = Path(parts[-1])
    assert_write_is_sandboxed(destination)
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_bytes(payload)
    return True


@pytest.fixture(autouse=True)
def repo_root_guard() -> Iterator[None]:
    """Fail the test that leaves a new entry in the checkout root.

    Belt-and-braces behind :func:`assert_write_is_sandboxed`: that catches a
    stray write at the moment it happens, this catches one by any route at all.
    Deliberately non-recursive — sibling agents legitimately add files under
    ``src/`` and ``tests/`` while these run, but top-level entries are stable,
    so comparing only the root keeps the check meaningful without being racy.
    """
    before = set(REPO_ROOT.iterdir())
    yield
    new = sorted(str(p.name) for p in set(REPO_ROOT.iterdir()) - before)
    assert not new, f"test created entries in the repo root: {new}"


def assert_no_prod_dsn(argv: Sequence[str]) -> None:
    """Abort if any argv element parses to the production database.

    Scans for ``postgresql://`` URLs anywhere in the argv (``-d <dsn>`` and
    bare-DSN forms both), plus the bare prod database name appearing as a
    ``-d`` value. Delegates the actual verdict to ``tests/conftest.py`` rather
    than re-deriving it, so the two guards can never drift apart.
    """
    for index, element in enumerate(argv):
        if "://" in element:
            parsed = urlparse(element)
            dbname = (parsed.path or "").lstrip("/")
            if _looks_like_prod_db(parsed.hostname, parsed.port, dbname):
                raise ProdDsnLeak(
                    f"fake runner was handed a PRODUCTION DSN at argv[{index}]: "
                    f"{element!r}. No test may dump or restore production."
                )
        if element == "-d" and index + 1 < len(argv):
            value = argv[index + 1]
            if "://" not in value and _looks_like_prod_db(None, None, value):
                raise ProdDsnLeak(
                    f"fake runner was handed the PRODUCTION database name at "
                    f"argv[{index + 1}]: {value!r}."
                )


class RecordingRunner:
    """Records every argv it is asked to run; never spawns a real process.

    ``responses`` maps a substring of the invocation (``"pg_dump --version"``,
    ``"pg_restore"``, ...) to the stdout the fake should return, so one runner
    can serve both a version probe and a dump. ``raises`` maps the same kind of
    key to an exception raised instead — used to simulate a stopped container
    or a missing binary. First matching key wins, in insertion order.
    """

    def __init__(
        self,
        *,
        responses: Mapping[str, str] | None = None,
        raises: Mapping[str, BaseException] | None = None,
        default_stdout: str = "",
    ) -> None:
        self.responses = dict(responses or {})
        self.raises = dict(raises or {})
        self.default_stdout = default_stdout
        self.calls: list[list[str]] = []
        self.envs: list[dict[str, str] | None] = []

    def run(
        self,
        argv: Sequence[str],
        *,
        timeout: float,
        env: Mapping[str, str] | None = None,
    ) -> subprocess.CompletedProcess[str]:
        assert_no_prod_dsn(argv)
        recorded = list(argv)
        self.calls.append(recorded)
        self.envs.append(dict(env) if env is not None else None)
        joined = " ".join(recorded)
        for key, exc in self.raises.items():
            if key in joined:
                raise exc
        stdout = self.default_stdout
        for key, value in self.responses.items():
            if key in joined:
                stdout = value
                break
        return subprocess.CompletedProcess(recorded, 0, stdout, "")

    @property
    def flat_calls(self) -> list[str]:
        """Every recorded invocation joined into one string per call."""
        return [" ".join(call) for call in self.calls]


def _assert_not_the_running_suite_database(dbname: str) -> None:
    """Refuse to drop the database the current pytest session is using.

    Resolved from ``tests.conftest.TEST_DATABASE_URL`` at CALL time, not import
    time, so a suite pointed at a scratch database via the environment is
    compared against the database it is actually using. (Import-time capture is
    the seam-that-cannot-be-overridden shape this repo has been cataloguing all
    day; not repeating it here.)
    """
    import psycopg

    from tests.conftest import TEST_DATABASE_URL

    session_db = str(
        psycopg.conninfo.conninfo_to_dict(TEST_DATABASE_URL).get("dbname", "")
    )
    if dbname and dbname == session_db:
        raise LiveSuiteDatabaseDrop(
            f"refusing to DROP DATABASE {dbname!r} WITH (FORCE): that is the "
            f"database this pytest session is running on. FORCE terminates "
            f"every other backend, which here means the suite's own "
            f"connections — it crash-restarts the Postgres instance and leaves "
            f"the database invalid (observed twice on 2026-08-07). Point the "
            f"restore/swap tests at a throwaway database instead of "
            f"TEST_DATABASE_URL."
        )


def drop_restore_artifacts(base_dsn: str) -> list[str]:
    """Undo a restore test's swap, putting the ORIGINAL database back in place.

    A successful restore deliberately RETAINS the database it replaced and
    leaves a *different physical database* under the live name — that is the
    whole safety property. For the suite it is a problem twice over:

    1. Each swap leaks one ``<db>_replaced_<ts>`` onto the shared test server.
    2. The swapped-in database is a bare migrated shell whose freshly
       bootstrapped Apache AGE catalog is not the one the session-scoped
       fixtures set up. Leaving it in place made the *next* pytest session die
       during setup with ``label (relation) cache corrupted`` — reproduced by
       running the gate and swap modules twice in a row.

    So teardown renames the parked database back over the live name, which
    restores the exact database (and AGE catalog) the fixtures created, then
    drops any remaining staging leftovers.

    Scoped to the suite's own database name and to the two generated suffixes,
    so it can never touch a real database — and it refuses outright if the DSN
    looks like production or names the suite's own database.
    """
    import psycopg
    from psycopg import sql

    params = psycopg.conninfo.conninfo_to_dict(base_dsn)
    live = str(params.get("dbname", ""))
    if _looks_like_prod_db(
        str(params.get("host") or "") or None,
        int(params["port"]) if params.get("port") else None,
        live,
    ):
        raise ProdDsnLeak(f"refusing to drop databases on a production DSN: {live!r}")

    # STRUCTURAL GUARD (C22), deliberately ABOVE both branches below rather
    # than inside the `if parked:` one. `WITH (FORCE)` terminates every other
    # backend on the target. If `live` is the database this pytest session is
    # using, those backends are OURS — the suite lock and the pooled
    # connections — and terminating them crash-restarts the instance.
    #
    # Guarding only the parked branch would leave the leftovers sweep below
    # unguarded, and that sweep's `{live}_restore_%` pattern MATCHES the
    # sandbox database `restore_sandbox_dsn` creates (`{live}_restore_sandbox`)
    # — so a caller passing the suite DSN with no parked database lying around
    # would silently destroy the sandbox mid-session instead of being refused.
    # Refuse loudly and unconditionally: a named failure in one module beats a
    # destroyed database and ~670 downstream errors indicting innocent tests.
    _assert_not_the_running_suite_database(live)

    params["dbname"] = "postgres"
    removed: list[str] = []
    with psycopg.connect(psycopg.conninfo.make_conninfo(**params)) as conn:
        conn.autocommit = True
        parked = [
            str(row[0])
            for row in conn.execute(
                "SELECT datname FROM pg_database WHERE datname LIKE %s ORDER BY 1",
                (f"{live}\\_replaced\\_%",),
            ).fetchall()
        ]
        if parked:
            # Newest wins if several accumulated; drop the swapped-in shell and
            # put the original database back under the live name.
            original = parked[-1]
            conn.execute(
                sql.SQL("DROP DATABASE IF EXISTS {} WITH (FORCE)").format(
                    sql.Identifier(live)
                )
            )
            conn.execute(
                sql.SQL("ALTER DATABASE {} RENAME TO {}").format(
                    sql.Identifier(original), sql.Identifier(live)
                )
            )
            removed.append(original)
            parked = parked[:-1]

        leftovers = parked + [
            str(row[0])
            for row in conn.execute(
                "SELECT datname FROM pg_database WHERE datname LIKE %s",
                (f"{live}\\_restore\\_%",),
            ).fetchall()
        ]
        for name in leftovers:
            # WITH (FORCE) terminates any lingering backend first (PG13+);
            # without it a still-open connection makes the drop fail with
            # "database is being accessed by other users" — flaky teardown.
            conn.execute(
                sql.SQL("DROP DATABASE IF EXISTS {} WITH (FORCE)").format(
                    sql.Identifier(name)
                )
            )
            removed.append(name)
    return removed


class RestoringRunner(RecordingRunner):
    """A RecordingRunner whose fake `pg_restore` really populates the target.

    Without this the happy path could not be tested honestly: a no-op
    `pg_restore` leaves the staging database schema-less, so restore's
    manifest verification correctly refuses to swap it in. Applying the
    migrations reproduces what restoring an *empty* brain's dump actually
    produces — the real schema, zero documents, and the real migration head —
    so verification passes for the right reason rather than being bypassed.

    Still no real subprocess and still no real dump file: only the
    already-migrated shape the archive under test describes.
    """

    def __init__(self, *, base_dsn: str, **kwargs: object) -> None:
        super().__init__(**kwargs)  # type: ignore[arg-type]
        self.base_dsn = base_dsn
        self.restored_databases: list[str] = []
        #: Set False to model a pg_restore that exits 0 but populates nothing —
        #: the truncated-archive case restore must refuse rather than swap in.
        self.migrate_enabled = True

    def run(
        self,
        argv: Sequence[str],
        *,
        timeout: float,
        env: Mapping[str, str] | None = None,
    ) -> subprocess.CompletedProcess[str]:
        completed = super().run(argv, timeout=timeout, env=env)
        parts = list(argv)
        if (
            self.migrate_enabled
            and any(part.endswith("pg_restore") for part in parts)
            and "-d" in parts
        ):
            self._migrate(parts[parts.index("-d") + 1])
        return completed

    def _migrate(self, dbname: str) -> None:
        import psycopg

        from brain.db import connect, run_migrations

        assert_no_prod_dsn(["-d", dbname])
        params = psycopg.conninfo.conninfo_to_dict(self.base_dsn)
        params["dbname"] = dbname
        with connect(psycopg.conninfo.make_conninfo(**params)) as conn:
            conn.autocommit = True
            run_migrations(conn)
        self.restored_databases.append(dbname)


#: Payload every fake dump carries. Synthetic — no real dump is ever replayed.
STUB_DUMP = b"PGDMP-synthetic-custom-format-payload"

#: Real `pg_dump --version` output from the pinned container image.
CONTAINER_VERSION = "pg_dump (PostgreSQL) 16.14 (Debian 16.14-1.pgdg12+1)\n"


def _test_dsn_port() -> int:
    """The port ``TEST_DATABASE_URL`` names — what the fake container publishes."""
    import psycopg

    from tests.conftest import TEST_DATABASE_URL

    port = psycopg.conninfo.conninfo_to_dict(TEST_DATABASE_URL).get("port")
    return int(str(port)) if port else 5432


def container_port_responses() -> dict[str, str]:
    """Make the fake container look like the one serving ``TEST_DATABASE_URL``.

    ``resolve_pg_tool`` now selects the container leg only when the DSN
    provably resolves to that container's published port — the fix for a QA DSN
    on 5434 reaching the production container. Doubles must therefore model the
    port mapping too, otherwise every test silently drops to the host leg and
    stops exercising the production path.
    """
    from brain._compose import postgres_container_name

    port = _test_dsn_port()
    return {
        "docker port": f"0.0.0.0:{port}\n[::]:{port}\n",
        "docker ps": f"{postgres_container_name()}\t0.0.0.0:{port}->5432/tcp\n",
    }


class StubDumpRunner(RecordingRunner):
    """RecordingRunner that also materialises the dump `docker cp` copies out.

    The real flow dumps to a path *inside* the container and then `docker cp`s
    it onto the host. Nothing spawns a real process here, so the fake plays the
    part of that copy — via :func:`materialise_copy_out`, which refuses to
    write anywhere but a sandboxed path.

    It also answers the port probes, so the container leg is selected exactly
    as in production (see :func:`container_port_responses`).
    """

    def __init__(self, **kwargs: object) -> None:
        responses = dict(kwargs.pop("responses", {}))  # type: ignore[arg-type]
        for key, value in container_port_responses().items():
            responses.setdefault(key, value)
        super().__init__(responses=responses, **kwargs)  # type: ignore[arg-type]

    def run(
        self,
        argv: Sequence[str],
        *,
        timeout: float,
        env: Mapping[str, str] | None = None,
    ) -> subprocess.CompletedProcess[str]:
        completed = super().run(argv, timeout=timeout, env=env)
        parts = list(argv)
        if not materialise_copy_out(parts, STUB_DUMP) and (
            "-f" in parts and any(part.endswith("pg_dump") for part in parts)
        ):
            # Host leg: pg_dump writes straight to the host path after -f.
            destination = Path(parts[parts.index("-f") + 1])
            assert_write_is_sandboxed(destination)
            destination.parent.mkdir(parents=True, exist_ok=True)
            destination.write_bytes(STUB_DUMP)
        return completed


class StubAndRestoreRunner(RestoringRunner):
    """Fakes the dump copy AND a pg_restore that really populates staging.

    Answers the port probes too, so the container leg is chosen as in
    production rather than silently dropping to the host leg.
    """

    def __init__(self, **kwargs: object) -> None:
        responses = dict(kwargs.pop("responses", {}))  # type: ignore[arg-type]
        for key, value in container_port_responses().items():
            responses.setdefault(key, value)
        super().__init__(responses=responses, **kwargs)  # type: ignore[arg-type]

    def run(
        self,
        argv: Sequence[str],
        *,
        timeout: float,
        env: Mapping[str, str] | None = None,
    ) -> subprocess.CompletedProcess[str]:
        completed = super().run(argv, timeout=timeout, env=env)
        materialise_copy_out(list(argv), STUB_DUMP)
        return completed


def dsn_database(dsn: str) -> str:
    """The database name a DSN names — URL *or* keyword/value form.

    :func:`restore_sandbox_dsn` returns what
    :func:`psycopg.conninfo.make_conninfo` produces, which is keyword/value
    (``user=... dbname=... host=...``), NOT a URL. ``urlparse(...).path`` on
    that returns the WHOLE string, so a derived ``f"{db}_replaced_%"`` pattern
    silently matches nothing and a derived assertion silently compares
    garbage — it fails loudly here only because the value is so obviously
    wrong. Parse through psycopg, which understands both forms.
    """
    import psycopg

    return str(psycopg.conninfo.conninfo_to_dict(dsn).get("dbname", ""))


def dsn_for_database(dsn: str, dbname: str) -> str:
    """``dsn`` re-pointed at ``dbname``, every other parameter preserved.

    Textual ``dsn.replace(old_db, new_db)`` looks equivalent and is not: it
    rewrites any *other* parameter that happens to contain the database name
    (a password, a host, an ``application_name``), and it cannot be applied at
    all when the database name is a substring of the whole conninfo string.
    """
    import psycopg

    params: dict[str, Any] = dict(
        psycopg.conninfo.conninfo_to_dict(dsn), dbname=dbname
    )
    return str(psycopg.conninfo.make_conninfo(**params))


def reset_restore_sandbox(sandbox_dsn: str) -> None:
    """Clear the restore sandbox's DATA before a test seeds its own corpus.

    The sandbox is NOT the suite database, so ``conftest``'s per-test
    :func:`tests.conftest._truncate_reset` (which the ``test_db`` fixture
    drives) never reaches it. Without an explicit reset the sandbox
    accumulates state across the module: a successful swap test seeds a
    document, and :func:`drop_restore_artifacts` then renames the parked
    database — *with that document still in it* — back over the live name, so
    the next test starts against a corpus it did not create. That silently
    breaks both directions: a test asserting "the live database holds 1
    document" sees 2, and a test asserting the empty-target path (``--yes``
    skips the typed phrase) suddenly faces a non-empty target.

    Reuses ``conftest``'s reset verbatim rather than re-deriving a TRUNCATE, so
    the two can never drift — including its own prod-DSN guard.
    """
    import psycopg

    from brain.db import connect
    from tests.conftest import _truncate_reset

    params: dict[str, Any] = psycopg.conninfo.conninfo_to_dict(sandbox_dsn)
    if _looks_like_prod_db(
        str(params.get("host") or "") or None,
        int(params["port"]) if params.get("port") else None,
        str(params.get("dbname") or "") or None,
    ):
        raise ProdDsnLeak(
            f"refusing to reset what looks like production: {sandbox_dsn!r}"
        )
    with connect(sandbox_dsn) as conn:
        conn.autocommit = True
        _truncate_reset(conn)


def seed_document_into(dsn: str, embedder: object, *, title: str, content: str) -> str:
    """Ingest one synthetic document into the database ``dsn`` names.

    ``conftest.seed_doc`` writes through the ``test_db`` connection, i.e. into
    the SUITE database — which since C22 is deliberately NOT what the restore
    suites replace. A seed that lands there leaves the restore target empty, so
    ``brain restore`` takes its empty-target branch and every "the live corpus
    is non-empty" assertion silently stops testing anything.

    Opens and CLOSES its own connection rather than holding one open for the
    test. Production's own swap would survive an open connection — it calls
    ``_terminate_other_sessions`` first — but TEARDOWN would not:
    :func:`drop_restore_artifacts` issues a bare ``ALTER DATABASE {original}
    RENAME TO {live}`` with no terminate step, and Postgres refuses a rename
    while any other session is connected.
    """
    import psycopg

    from brain.ingest import ExtractedDoc, ingest_document

    params: dict[str, Any] = psycopg.conninfo.conninfo_to_dict(dsn)
    if _looks_like_prod_db(
        str(params.get("host") or "") or None,
        int(params["port"]) if params.get("port") else None,
        str(params.get("dbname") or "") or None,
    ):
        # Every other DB-touching helper here guards; this one writes DOCUMENTS,
        # which is how ~15 test fixtures once landed in the real corpus.
        raise ProdDsnLeak(f"refusing to ingest into a production DSN: {dsn!r}")

    with psycopg.connect(dsn) as conn:
        conn.autocommit = True
        result = ingest_document(
            conn,
            embedder=embedder,  # type: ignore[arg-type]
            doc=ExtractedDoc(
                title=title,
                content=content,
                content_type="note",
                source_path=None,
                metadata={},
            ),
            source_kind="manual",
            tags=[],
        )
    assert result.document_id is not None
    return str(result.document_id)


def live_document_count(dsn: str) -> int:
    """Documents in the database ``dsn`` names, over a FRESH connection.

    Fresh on purpose: after a swap the live NAME points at a different physical
    database, and a connection opened before the rename still holds the old
    one — so a cached connection would answer about the wrong database and
    report success either way.
    """
    import psycopg

    with psycopg.connect(dsn) as conn:
        row = conn.execute("SELECT count(*) FROM documents").fetchone()
    assert row is not None
    return int(row[0])


#: Cache so repeated calls in one session reuse the same sandbox database.
_RESTORE_SANDBOX: dict[str, str] = {}


def restore_sandbox_dsn(suite_dsn: str) -> str:
    """A throwaway database for restore/swap tests, created on first use.

    C22. The restore suites exercise a real swap: `brain restore` parks the live
    database aside and puts a fresh one under its name, and teardown reclaims
    it with ``DROP DATABASE {live} WITH (FORCE)``. Pointed at
    ``TEST_DATABASE_URL`` that is the database the whole pytest session is
    using, so ``FORCE`` terminates the session's own backends — twice on
    2026-08-07 that crash-restarted the Postgres instance and left the database
    ``datconnlimit = -2``.

    Note the destructive swap happened on EVERY run, not only when a stale
    ``_replaced_`` database was lying around; the leftover only widened the
    window. Whether it took the instance down was a matter of which backends
    Postgres happened to terminate.

    So the restore suites get their own database. Same server (so the tests
    still exercise real cross-database DDL), different name.

    **The guard's actual scope — do not over-read it.**
    :func:`_assert_not_the_running_suite_database` protects
    :func:`drop_restore_artifacts`, the TEARDOWN path. It does NOT sit on the
    production path a restore takes: ``brain restore`` terminates every other
    backend on the live database (``restore._terminate_other_sessions``) and
    then renames it (``restore._swap_databases``), and that is what actually
    took the instance down. Nothing structural stops a future test from
    building a ``Config`` whose ``database_url`` is the suite's own database
    and handing it to ``restore_backup``; what stops it today is the
    convention that these modules take their DSN from HERE. Treat that as a
    convention with a loud teardown backstop, not as an impossibility — an
    over-claimed guard is how the next person concludes the path is safe
    without checking.
    """
    import psycopg
    from psycopg import sql

    if suite_dsn in _RESTORE_SANDBOX:
        return _RESTORE_SANDBOX[suite_dsn]

    params = psycopg.conninfo.conninfo_to_dict(suite_dsn)
    live = str(params.get("dbname", ""))
    if _looks_like_prod_db(
        str(params.get("host") or "") or None,
        int(params["port"]) if params.get("port") else None,
        live,
    ):
        raise ProdDsnLeak(f"refusing to build a restore sandbox on prod: {live!r}")

    sandbox = f"{live}_restore_sandbox"
    # Postgres truncates identifiers at 63 BYTES, silently. `_restore_database`
    # then appends `_replaced_<15-char stamp>` (25 more) to this name, and
    # `_validated_db_name` checks the character class, NOT the length — so an
    # overflow shows up as `database "..." does not exist` from a connection
    # whose name was truncated on the server but not in Python, with nothing
    # pointing at the cause. The sandbox suffix consumed the headroom that used
    # to exist; parallel-worktree databases already on this server
    # (`second_brain_test_review_a`, 26 chars) are over the line.
    if len(sandbox) + 25 > 63:
        raise AssertionError(
            f"suite database name {live!r} is too long: the restore sandbox "
            f"({sandbox!r}) plus the generated '_replaced_<stamp>' suffix "
            f"exceeds Postgres' 63-byte identifier limit, which truncates "
            f"SILENTLY. Use a suite database name of at most "
            f"{63 - 25 - len('_restore_sandbox')} characters."
        )
    admin = dict(params, dbname="postgres")
    with psycopg.connect(psycopg.conninfo.make_conninfo(**admin)) as conn:
        conn.autocommit = True
        exists = conn.execute(
            "SELECT 1 FROM pg_database WHERE datname = %s", (sandbox,)
        ).fetchone()
        if exists is None:
            conn.execute(
                sql.SQL("CREATE DATABASE {}").format(sql.Identifier(sandbox))
            )
    dsn = psycopg.conninfo.make_conninfo(**dict(params, dbname=sandbox))
    # A brand-new database is an empty shell; the restore suites expect the live
    # database to carry the migrated schema (that is what `brain restore`
    # replaces). Migrate under the same mutex production uses so a concurrent
    # runner cannot interleave DDL here.
    #
    # UNCONDITIONALLY, not only when this call created it. The sandbox outlives
    # the session, and `CREATE DATABASE` + migrate are not atomic — a Ctrl-C
    # between them, or simply a new migration landing after the sandbox was
    # first built, leaves it behind at an older head. Treating "the database
    # exists" as proof it is migrated then fails as
    # ``_verify_staging``'s "restored schema head is '028' but the manifest
    # records '027'" on every swap test, whose remedy — drop a database nobody
    # knows exists — is undiscoverable from that message. `run_migrations` is
    # idempotent, so re-running costs one catalog query on the common path.
    from brain.db import connect as _connect
    from brain.db import run_migrations as _run_migrations

    with _connect(dsn) as migrate_conn:
        migrate_conn.autocommit = True
        _run_migrations(migrate_conn)
    _RESTORE_SANDBOX[suite_dsn] = dsn
    return dsn
