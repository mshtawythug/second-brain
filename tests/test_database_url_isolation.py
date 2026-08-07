"""Regression: ensure the test session can never connect to the prod DB.

On 2026-05-04 a full-suite run leaked 15 test fixtures into the real
``second_brain`` database. The vector was tests using
``CliRunner().invoke(app, ...)`` without first patching ``DATABASE_URL``
to the test DB — those tests fell through to ``brain.config.Config.load()``
which reads ``DATABASE_URL`` from ``.env`` (= prod).

The fix is a session-scoped autouse fixture in ``tests/conftest.py`` that
forces ``os.environ["DATABASE_URL"] = TEST_DATABASE_URL`` for the entire
session. This file pins the contract.
"""

from __future__ import annotations

import ast
import os
from pathlib import Path

import pytest

from brain.config import Config
from tests.conftest import (
    TEST_DATABASE_URL,
    _assert_not_prod_db,
    _looks_like_prod_db,
)

# Built by concatenation so THIS file never contains the contiguous literal it
# forbids (otherwise the scan below would flag itself).
_STOCK_PORT_TEST_DB_LITERAL = "5433" + "/second_brain_test"


def test_database_url_env_var_points_at_test_db() -> None:
    """``os.environ["DATABASE_URL"]`` must be the test DB during pytest.

    If this fails, the autouse fixture in conftest didn't run. Any
    ``Config.load()`` call from inside a CLI test would then talk to prod.
    """
    assert os.environ.get("DATABASE_URL") == TEST_DATABASE_URL, (
        f"DATABASE_URL is {os.environ.get('DATABASE_URL')!r}; "
        f"expected {TEST_DATABASE_URL!r}. Did the autouse fixture in "
        "tests/conftest.py run?"
    )


def test_config_load_resolves_to_test_database_url() -> None:
    """``Config.load()`` reads from os.environ — must yield the test DB.

    Even though ``Config.load()`` reads ``.env`` files internally via
    ``dotenv_values`` + ``os.environ.setdefault``, it never overwrites an
    existing env var, so the autouse fixture's assignment wins.
    """
    cfg = Config.load()
    assert cfg.database_url == TEST_DATABASE_URL, (
        f"Config.load().database_url is {cfg.database_url!r}; "
        f"expected {TEST_DATABASE_URL!r}."
    )


def test_database_url_does_not_match_prod_naming() -> None:
    """Defense-in-depth: the test DB URL should never end in ``second_brain``.

    A common .env mistake is setting ``TEST_DATABASE_URL`` to the prod
    URL by accident. Catching that here is cheaper than losing data.
    """
    assert not TEST_DATABASE_URL.rstrip("/").endswith("/second_brain"), (
        f"TEST_DATABASE_URL must not point at prod database name; "
        f"got {TEST_DATABASE_URL!r}."
    )


def test_no_test_file_hardcodes_stock_pg_port_for_test_db() -> None:
    """No test module may default the test DB to the stock-pgvector port (5433).

    The GraphRAG suite must run against the Apache-AGE test instance
    (docker-compose.age-test.yml, port 5434). The prod container on 5433 is
    stock pgvector with no ``age`` extension, so any test whose
    ``TEST_DATABASE_URL`` fallback hardcodes ``5433/second_brain_test`` would
    silently connect to the no-AGE instance whenever the env var is unset
    (CI, a fresh shell) — breaking every AGE test and splitting the suite
    across two databases. Each module's fallback (and conftest's) must use
    5434. This scan fails fast if the 5433 test-DB literal is reintroduced
    anywhere under ``tests/``.

    Note: ``5433/second_brain`` *without* ``_test`` (the live eval/canary
    harness ``LIVE_DB_URL``) is intentional read-only access to the prod
    corpus and is NOT matched by this guard.
    """
    tests_dir = Path(__file__).parent
    this_file = Path(__file__).resolve()
    offenders: list[str] = []
    for py_file in sorted(tests_dir.rglob("*.py")):
        if py_file.resolve() == this_file:
            continue  # this guard builds the needle dynamically; skip itself
        if _STOCK_PORT_TEST_DB_LITERAL in py_file.read_text(encoding="utf-8"):
            offenders.append(str(py_file.relative_to(tests_dir)))
    assert not offenders, (
        f"These test modules hardcode the stock-pgvector port for the test DB "
        f"({_STOCK_PORT_TEST_DB_LITERAL!r}); they must use 5434 (the Apache-AGE "
        f"test instance): {offenders}"
    )


# --- no module may pin a DSN that ignores TEST_DATABASE_URL -----------------
# The sibling guard above forbids the *wrong port*. These forbid pinning the DSN
# at all: a module that hard-codes it diverges from the ``test_db`` fixture the
# moment anyone overrides ``TEST_DATABASE_URL``, which is routine (parallel
# agents and CI each run against their own scratch database).


def _dsn_literal(node: ast.expr) -> str | None:
    """Return ``node``'s value when it is a Postgres-DSN string constant."""
    if (
        isinstance(node, ast.Constant)
        and isinstance(node.value, str)
        and node.value.startswith(("postgresql://", "postgres://"))
    ):
        return node.value
    return None


def _pinned_constant_lines(tree: ast.AST) -> list[int]:
    """Lines assigning ``TEST_DATABASE_URL`` straight to a DSN literal.

    ``os.environ.get("TEST_DATABASE_URL", "<fallback>")`` is a ``Call``, not a
    ``Constant``, so the accepted fallback form is not matched.
    """
    hits: list[int] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Assign):
            continue
        names = {t.id for t in node.targets if isinstance(t, ast.Name)}
        if "TEST_DATABASE_URL" in names and _dsn_literal(node.value) is not None:
            hits.append(node.lineno)
    return hits


def _names_a_real_brain_db(dsn: str) -> bool:
    """True when ``dsn``'s database name is a real ``second_brain*`` database.

    Config-*parsing* tests legitimately set ``DATABASE_URL`` to obviously fake,
    non-connecting placeholders (``postgresql://x:y@h:5432/d``) to exercise the
    parser; those never open a socket and must not be flagged. Only a DSN that
    names an actual brain database is one a test could connect to — and
    therefore one that must track the ``TEST_DATABASE_URL`` override.
    """
    dbname = dsn.rstrip("/").rsplit("/", 1)[-1].split("?", 1)[0]
    return dbname.startswith("second_brain")


def _pinned_setenv_lines(tree: ast.AST) -> list[int]:
    """Lines passing a real ``second_brain*`` DSN literal to ``setenv``."""
    hits: list[int] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        if not (isinstance(func, ast.Attribute) and func.attr == "setenv"):
            continue
        if len(node.args) < 2:
            continue
        key = node.args[0]
        keyed_on_db_url = isinstance(key, ast.Constant) and key.value in {
            "DATABASE_URL",
            "TEST_DATABASE_URL",
        }
        dsn = _dsn_literal(node.args[1])
        if keyed_on_db_url and dsn is not None and _names_a_real_brain_db(dsn):
            hits.append(node.lineno)
    return hits


def _env_get_fallback_nodes(tree: ast.AST) -> set[int]:
    """``id()``s of the literal in ``os.environ.get("TEST_DATABASE_URL", <lit>)``.

    That form is the ONE legitimate place a real DSN literal may appear: it is a
    *fallback*, used only when the environment does not override it, so it
    cannot diverge from the fixture's database.
    """
    allowed: set[int] = set()
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        if not (isinstance(func, ast.Attribute) and func.attr == "get"):
            continue
        if len(node.args) < 2:
            continue
        key = node.args[0]
        if isinstance(key, ast.Constant) and key.value == "TEST_DATABASE_URL":
            allowed.add(id(node.args[1]))
    return allowed


def _names_the_test_database(dsn: str) -> bool:
    """True when ``dsn`` names the TEST database family specifically.

    Narrower than :func:`_names_a_real_brain_db`, and the narrowing is the
    point. Only the *test* database is resolved from ``TEST_DATABASE_URL``, so
    only a pinned test DSN can diverge from what the fixture uses. Literals
    naming a deliberately *different* database cannot diverge and are correct:

    * ``second_brain`` — prod. ``test_conftest_prod_guard`` and
      ``test_restore_gate`` must spell it literally to assert the guards REFUSE
      it; substituting the resolved constant would defeat those tests entirely.
    * ``second_brain_demo`` — the isolated ``brain demo`` sandbox on its own
      port, a different database by design.
    * the live eval/canary corpus.

    A first cut flagged all of these, which would have been the opposite failure
    mode: a guard that goes red on 16 legitimate uses gets deleted, and then it
    catches nothing at all.
    """
    dbname = dsn.rstrip("/").rsplit("/", 1)[-1].split("?", 1)[0]
    return dbname.startswith("second_brain") and "test" in dbname


def _pinned_dsn_value_lines(tree: ast.AST) -> list[int]:
    """Lines where a real ``second_brain*`` DSN literal appears in ANY position.

    **Value-first, not shape-first — and that distinction is the whole point.**
    The original guard matched two BINDING SHAPES: a module-level
    ``TEST_DATABASE_URL = "<literal>"`` and ``setenv("DATABASE_URL", "<literal>")``.
    On 2026-08-06 it caught one of three violations in the Wave 4 recall modules
    and missed two, because those bind the DSN differently — as a dict value
    (``{"database_url": …}``) and as a keyword argument
    (``Config(database_url=…)``). Shape enumeration is a losing game: a probe of
    ten plausible shapes showed the old detectors blind to six of them
    (different variable name, fixture-local assignment, f-string, dict value,
    kwarg, inline ``connect("…")``).

    A detector that finds one violation in three is worse than none, because it
    teaches people the case is covered. So this matches on the VALUE — any
    string literal naming a real ``second_brain*`` database, wherever it appears
    — and carves out only the single legitimate form (the ``os.environ.get``
    fallback). Obviously-fake placeholders (``postgresql://x:y@h:5432/d``) are
    excluded by :func:`_names_a_real_brain_db`, so the config-parsing tests that
    rely on them stay green.
    """
    allowed = _env_get_fallback_nodes(tree)
    hits: list[int] = []
    for node in ast.walk(tree):
        if not (isinstance(node, ast.Constant) and isinstance(node.value, str)):
            continue
        if id(node) in allowed:
            continue
        dsn = _dsn_literal(node)
        if dsn is not None and _names_the_test_database(dsn):
            hits.append(node.lineno)
    return sorted(set(hits))


#: Modules carved out of the sweep because they pin a test DSN and are not
#: fixable here. **Currently empty**, and that is the desired state.
#:
#: It held four entries on 2026-08-06 (``test_config_audio_envvars``,
#: ``test_config_enrich_envvars``, ``test_config_new_fields``,
#: ``test_t5_llm_keep_alive``) — each binding the test database under a private
#: variable name and feeding it to ``monkeypatch.setenv("DATABASE_URL", …)``,
#: the exact mechanism behind the graphrag false alarm. Two pinned *stale*
#: scratch database names that no longer existed, so they were latent failures
#: as well as divergence risks. All four are fixed; the entries were removed
#: because :func:`test_known_pinned_modules_are_still_pinned` failed until they
#: were, which is precisely what that test is for.
KNOWN_PINNED_MODULES: set[str] = set()


def test_known_pinned_modules_are_still_pinned() -> None:
    """The carve-out list is live, not stale scaffolding.

    An exemption list that outlives the problem silently shrinks the guard's
    coverage. If one of these gets fixed, this fails and the entry must be
    deleted — which is the point: the carve-out costs something to keep.
    """
    tests_dir = Path(__file__).parent
    still_pinned = set()
    for name in KNOWN_PINNED_MODULES:
        path = tests_dir / name
        if not path.exists():
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        if _pinned_dsn_value_lines(tree):
            still_pinned.add(name)

    fixed = {n for n in KNOWN_PINNED_MODULES if (tests_dir / n).exists()} - still_pinned
    assert not fixed, (
        f"{sorted(fixed)} no longer pin a test DSN — remove them from "
        "KNOWN_PINNED_MODULES so the guard covers them again."
    )


def test_no_test_module_pins_a_database_dsn() -> None:
    """No test module hard-codes a DSN that would ignore ``TEST_DATABASE_URL``.

    Regression for 2026-07-26. Five graphrag modules carried a bare
    ``TEST_DATABASE_URL = "postgresql://.../second_brain_test"`` instead of
    importing the resolved constant, so under an override the ``test_db``
    fixture seeded database *A* while the CLI/MCP under test read database *B*.
    The shared default database was in Postgres' ``invalid`` state at the time
    (an interrupted ``DROP DATABASE`` leaves ``datconnlimit = -2``), which
    turned that divergence into ~60 hard connection failures across
    ``test_mcp_graphrag``, ``test_cli_graphrag_search`` and
    ``test_graphrag_build`` — and got recorded as pre-existing *product*
    breakage. It was neither pre-existing nor product breakage.

    Note the milder, worse variant: had the default database merely been stale
    rather than invalid, those tests would have read and written a different
    database while still reporting green.

    ``conftest.py`` defines the constant (from the environment, with a literal
    fallback) and ``db_lock.py`` renders the DSN into operator guidance text,
    so both are exempt.
    """
    tests_dir = Path(__file__).parent
    this_file = Path(__file__).resolve()
    exempt = {"conftest.py", "db_lock.py"} | KNOWN_PINNED_MODULES
    offenders: list[str] = []
    for py_file in sorted(tests_dir.rglob("*.py")):
        if py_file.resolve() == this_file or py_file.name in exempt:
            continue
        tree = ast.parse(py_file.read_text(encoding="utf-8"), filename=str(py_file))
        rel = py_file.relative_to(tests_dir)
        # Value-first sweep: catches a real second_brain* DSN literal in ANY
        # binding position. Supersedes the two shape-specific detectors, which
        # are retained because their labels name the specific mistake.
        shape_specific = set(_pinned_constant_lines(tree)) | set(
            _pinned_setenv_lines(tree)
        )
        for line in _pinned_constant_lines(tree):
            offenders.append(f"{rel}:{line} (TEST_DATABASE_URL = <literal>)")
        for line in _pinned_setenv_lines(tree):
            offenders.append(f"{rel}:{line} (setenv DATABASE_URL <literal>)")
        for line in _pinned_dsn_value_lines(tree):
            if line not in shape_specific:
                offenders.append(f"{rel}:{line} (DSN literal pinned in place)")

    assert not offenders, (
        "These test modules pin a database DSN and would ignore a "
        f"TEST_DATABASE_URL override: {offenders}. Use "
        "`from tests.conftest import TEST_DATABASE_URL` (or "
        '`os.environ.get("TEST_DATABASE_URL", "<fallback>")` for the module '
        "that defines it), then pass that constant wherever the DSN is needed."
    )


def test_dsn_pinning_detectors_match_the_shapes_they_forbid() -> None:
    """The detectors fire on the exact shapes removed on 2026-07-26.

    Without this, an AST refactor could leave the scan above matching nothing
    and every module would pass by detecting nothing at all.
    """
    dsn = "postgresql://brain:brain@localhost:5434/second_brain_test"
    pinned = ast.parse(
        f'TEST_DATABASE_URL = "{dsn}"\n'
        "def f(monkeypatch):\n"
        f'    monkeypatch.setenv("DATABASE_URL", "{dsn}")\n'
    )
    assert _pinned_constant_lines(pinned) == [1]
    assert _pinned_setenv_lines(pinned) == [3]

    accepted = ast.parse(
        "import os\n"
        f'TEST_DATABASE_URL = os.environ.get("TEST_DATABASE_URL", "{dsn}")\n'
        "def f(monkeypatch):\n"
        '    monkeypatch.setenv("DATABASE_URL", TEST_DATABASE_URL)\n'
    )
    assert _pinned_constant_lines(accepted) == []
    assert _pinned_setenv_lines(accepted) == []

    # A deliberately fake, non-connecting placeholder used by the config-parsing
    # tests must NOT be flagged — it names no real database.
    placeholder = ast.parse(
        "def f(monkeypatch):\n"
        '    monkeypatch.setenv("DATABASE_URL", "postgresql://x:y@h:5432/d")\n'
    )
    assert _pinned_setenv_lines(placeholder) == []
    assert _names_a_real_brain_db(dsn) is True
    assert _names_a_real_brain_db("postgresql://x:y@h:5432/d") is False


#: Binding shapes the ORIGINAL shape-specific detectors were blind to. Each was
#: a real miss: on 2026-08-06 the guard flagged one of three violations in the
#: Wave 4 recall modules, because those bind the DSN as a dict value and as a
#: keyword argument rather than as a module-level constant. A detector that
#: finds one in three is worse than none — it teaches people the case is
#: covered. Pinned here so the value-first sweep can never regress to
#: shape-matching.
MISSED_SHAPES = {
    "different variable name": 'DB_URL = "{dsn}"',
    "fixture-local assignment": 'def fx():\n    url = "{dsn}"\n    return url',
    "f-string": 'X = f"{dsn}"',
    "dict value": 'CFG = {{"database_url": "{dsn}"}}',
    "keyword argument": 'c = Config(database_url="{dsn}")',
    "inline connect()": 'conn = connect("{dsn}")',
}


@pytest.mark.parametrize("label", sorted(MISSED_SHAPES))
def test_value_first_sweep_catches_shapes_the_old_detectors_missed(
    label: str,
) -> None:
    """Each previously-missed binding shape is now caught.

    This is the guard-on-the-guard for the 2026-08-06 gap. The lesson is that
    enumerating binding SHAPES is a losing game — there is always another way to
    spell it — so the sweep matches the VALUE instead. These cases pin that.
    """
    dsn = "postgresql://brain:brain@localhost:5434/second_brain_test"
    src = MISSED_SHAPES[label].format(dsn=dsn)
    tree = ast.parse(src)

    assert _pinned_dsn_value_lines(tree), (
        f"the value-first sweep missed the {label!r} shape:\n{src}\n"
        "It has regressed to shape-matching, which is what let two of three "
        "Wave 4 violations through."
    )


def test_value_first_sweep_still_allows_the_one_legitimate_form() -> None:
    """The ``os.environ.get`` fallback is not flagged, and fakes are not either.

    Without this the sweep could "pass" by flagging everything, which would be
    just as useless as flagging nothing — conftest itself and every
    config-parsing test would go red and the guard would get deleted.
    """
    dsn = "postgresql://brain:brain@localhost:5434/second_brain_test"

    fallback = ast.parse(
        f'import os\nTEST_DATABASE_URL = os.environ.get("TEST_DATABASE_URL", "{dsn}")\n'
    )
    assert _pinned_dsn_value_lines(fallback) == [], (
        "the os.environ.get fallback is the one legitimate place a real DSN "
        "literal may appear — it cannot diverge from the fixture's database."
    )

    fake = ast.parse('c = Config(database_url="postgresql://x:y@h:5432/d")')
    assert _pinned_dsn_value_lines(fake) == [], (
        "obviously-fake placeholders must stay allowed or every config-parsing "
        "test goes red."
    )


# --- conftest prod-DB destructive-reset guard (DB-safety) -------------------
# conftest._assert_not_prod_db is the hard guard that aborts the destructive
# DROP SCHEMA reset if it would ever target the prod container. These pin its
# contract (prod port on localhost, or the prod db name on any host, is
# refused; the AGE test instance passes).


def test_prod_db_guard_refuses_prod_port_on_localhost() -> None:
    """The guard aborts on the prod container (localhost + prod port)."""
    prod_port = 5400 + 33  # avoid the contiguous "5433/second_brain_test" literal
    assert _looks_like_prod_db("localhost", prod_port, "second_brain") is True
    with pytest.raises(RuntimeError, match="PROD database"):
        _assert_not_prod_db("localhost", prod_port, "second_brain")


def test_prod_db_guard_refuses_prod_dbname_on_any_host() -> None:
    """Belt-and-suspenders: the prod database NAME is refused on any host/port."""
    assert _looks_like_prod_db("db.internal", 6000, "second_brain") is True
    with pytest.raises(RuntimeError, match="PROD database"):
        _assert_not_prod_db("db.internal", 6000, "second_brain")


def test_prod_db_guard_allows_age_test_instance() -> None:
    """The AGE test instance (5434 / *_test) passes the guard untouched."""
    assert _looks_like_prod_db("localhost", 5434, "second_brain_test") is False
    _assert_not_prod_db("localhost", 5434, "second_brain_test")  # must not raise


def test_prod_db_guard_scopes_prod_port_refusal_to_local_hosts() -> None:
    """The prod-port refusal is scoped to local hosts (the prod container).

    A remote host on the same port with a ``*_test`` db is not the prod box,
    so it is allowed — proving the guard isn't an overbroad port blocklist.
    """
    prod_port = 5400 + 33
    assert _looks_like_prod_db("remote.example", prod_port, "second_brain_test") is False
    _assert_not_prod_db("remote.example", prod_port, "second_brain_test")  # no raise
