"""The restore gate — no flag may bypass the typed phrase (F3 §5.9, §7).

**No real pg_dump / pg_restore ever runs here.** Every external command goes
through :class:`tests.backup_fakes.RecordingRunner`, which aborts the test if it
is ever handed a production DSN.
"""
from __future__ import annotations

from collections.abc import Callable, Iterator
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import psycopg
import pytest
from typer.testing import CliRunner

from brain.backup import create_backup
from brain.cli import app
from brain.config import Config
from tests.backup_fakes import (
    CONTAINER_VERSION,
    RecordingRunner,
    StubAndRestoreRunner,
    StubDumpRunner,
    drop_restore_artifacts,
    dsn_database,
    live_document_count,
    repo_root_guard,  # noqa: F401 — autouse fixture
    reset_restore_sandbox,
    seed_document_into,
)
from tests.backup_fakes import restore_sandbox_dsn as _restore_sandbox_dsn
from tests.conftest import TEST_DATABASE_URL as _SUITE_DATABASE_URL
from tests.conftest import FakeEmbedder, _looks_like_prod_db
from tests.test_backup_manifest import EMBEDDER_NAME, EMBEDDING_DIM

# C22 (2026-08-07): these tests exercise a REAL restore swap — the live database
# is parked aside and a fresh one takes its name, then teardown reclaims it with
# `DROP DATABASE ... WITH (FORCE)`. Pointed at the suite's own database that
# terminates the session's own backends; twice it crash-restarted the Postgres
# instance and left the database unusable. So the whole module runs against a
# dedicated throwaway on the same server. Every derived name below
# (TEST_DB_NAME, PARKED_DB, MAINTENANCE_DSN) follows from it automatically.

TEST_DATABASE_URL = _restore_sandbox_dsn(_SUITE_DATABASE_URL)

NOW = datetime(2026, 7, 25, 18, 15, 0, tzinfo=UTC)
PROD_DSN = "postgresql://brain:brain@localhost:55432/second_brain"
#: Derived, never hardcoded — a literal scratch-DB name passes only in the
#: sandbox that created it and fails in CI (`second_brain_test`).
TEST_DB_NAME = dsn_database(TEST_DATABASE_URL)


class RefusingConnectionFactory:
    """Records every DSN it is asked to open and REFUSES production.

    The guard the suite leans on: if a regression ever routes `brain restore`
    at the real database, this raises instead of opening it, and ``opened``
    proves after the fact that production was never touched.
    """

    def __init__(self) -> None:
        self.requested: list[str] = []
        self.opened: list[str] = []

    def __call__(self, dsn: str) -> Any:
        self.requested.append(dsn)
        parsed = psycopg.conninfo.conninfo_to_dict(dsn)
        host = parsed.get("host")
        port = parsed.get("port")
        dbname = parsed.get("dbname")
        if _looks_like_prod_db(
            str(host) if host else None,
            int(port) if port else None,
            str(dbname) if dbname else None,
        ):
            raise AssertionError(
                f"brain restore attempted to open the PRODUCTION database: {dsn!r}"
            )
        self.opened.append(dsn)
        from brain.db import connect as _connect

        return _connect(dsn)


@pytest.fixture(autouse=True)
def _restore_sandbox_state() -> Iterator[None]:
    """Give each test a clean sandbox, and reclaim what its restore parked.

    Setup: the sandbox is not the suite database, so ``conftest``'s per-test
    TRUNCATE never reaches it — and teardown below deliberately renames the
    parked database (seeded corpus and all) back over the live name. Without
    this reset the module contaminates itself in both directions: a test
    asserting a non-empty target sees the wrong count, and the empty-target
    tests (``--yes`` skips the typed phrase) face a corpus they never seeded.

    Teardown: a successful restore RETAINS the database it replaced. Without
    this every swap leaks one ``*_replaced_<ts>`` database onto the shared test
    server and leaves a foreign AGE catalog under the live name.
    """
    reset_restore_sandbox(TEST_DATABASE_URL)
    yield
    drop_restore_artifacts(TEST_DATABASE_URL)


@pytest.fixture
def seed_live_doc() -> Callable[..., str]:
    """``conftest.seed_doc``, but into the sandbox `brain restore` replaces.

    Seeding via ``conftest.seed_doc`` lands in the SUITE database, leaving the
    restore target empty — and an empty target is precisely the branch where
    the gate does NOT demand the typed phrase, so the tests that exist to prove
    the phrase is unbypassable would pass without ever reaching it.

    Builds its embedder explicitly at ``EMBEDDING_DIM`` rather than taking the
    ``fake_embedder`` fixture, whose default only happens to match — see the
    twin fixture in ``test_restore_swap.py``.
    """

    def _seed(*, title: str, content: str) -> str:
        return seed_document_into(
            TEST_DATABASE_URL,
            FakeEmbedder(dim=EMBEDDING_DIM),
            title=title,
            content=content,
        )

    return _seed


@pytest.fixture
def cfg(tmp_path: Path) -> Config:
    return Config(
        database_url=TEST_DATABASE_URL,
        embedder=EMBEDDER_NAME,
        vault_path=tmp_path / "vault",
        backup_dir=tmp_path / "backups",
        brain_home=tmp_path / "brain-home",
    )


@pytest.fixture
def archive(test_db: psycopg.Connection, cfg: Config) -> Path:
    """A real archive taken from the (empty) test database.

    Counts of 0/0 mean the staging-DB verification passes against a database
    the fake runner never actually populated, so the gate can be exercised
    end-to-end without a real pg_restore.
    """
    return create_backup(
        cfg,
        runner=StubDumpRunner(responses={"--version": CONTAINER_VERSION}),
        clock=lambda: NOW,
        embedder=FakeEmbedder(dim=EMBEDDING_DIM),
    ).archive_path


@pytest.fixture
def runner() -> RecordingRunner:
    return StubAndRestoreRunner(
        base_dsn=TEST_DATABASE_URL, responses={"--version": CONTAINER_VERSION}
    )


@pytest.fixture
def wired(
    monkeypatch: pytest.MonkeyPatch, cfg: Config, runner: RecordingRunner
) -> RefusingConnectionFactory:
    """Point the CLI at the test config, the fake runner, and the guarded factory."""
    factory = RefusingConnectionFactory()
    monkeypatch.setenv("DATABASE_URL", cfg.database_url)
    monkeypatch.setattr(Config, "load", classmethod(lambda _cls: cfg))
    monkeypatch.setattr(
        "brain.cli._build_embedder", lambda _cfg: FakeEmbedder(dim=EMBEDDING_DIM)
    )
    monkeypatch.setattr("brain.cli_backup.connect", factory)
    monkeypatch.setattr("brain.backup.restore.connect", factory)
    _inject_fake_runner(monkeypatch, runner)
    return factory


def _inject_fake_runner(
    monkeypatch: pytest.MonkeyPatch, runner: RecordingRunner
) -> None:
    """Route the CLI's ``restore_backup`` through ``runner``.

    Extracted so the one test that cannot use :func:`wired` (it needs a
    production ``Config``, not the test one) can still wire the runner in.
    Without this, its ``assert runner.calls == []`` is structurally incapable
    of failing: nothing ever hands that runner to production code, so the
    assertion reads as "no external command ran" while measuring nothing —
    and a gate regression would spawn a REAL ``pg_dump`` against the
    production DSN with the test still green.
    """
    from brain.backup.restore import restore_backup as real_restore

    def _with_fake_runner(*args: Any, **kwargs: Any) -> Any:
        kwargs["runner"] = runner
        return real_restore(*args, **kwargs)

    monkeypatch.setattr("brain.cli_backup.restore_backup", _with_fake_runner)


def _invoke(archive: Path, *args: str, stdin: str = "") -> Any:
    return CliRunner().invoke(app, ["restore", str(archive), *args], input=stdin)


def test_db_only_and_vault_only_together_is_bad_parameter(
    archive: Path, wired: RefusingConnectionFactory
) -> None:
    result = _invoke(archive, "--db-only", "--vault-only")

    assert result.exit_code == 2


def test_gate_prints_live_counts(
    archive: Path,
    wired: RefusingConnectionFactory,
    runner: RecordingRunner,
    seed_live_doc: Callable[..., str],
) -> None:
    """The user sees what is about to be replaced before deciding."""
    seed_live_doc(title="Larkspur review", content="Synthetic body for the gate test.")

    result = _invoke(archive, stdin="n\n")

    assert "WILL BE OVERWRITTEN" in result.output
    assert "currently holds 1 documents" in result.output
    assert runner.calls == []


def test_wrong_typed_phrase_aborts_and_runs_nothing(
    archive: Path,
    wired: RefusingConnectionFactory,
    runner: RecordingRunner,
    seed_live_doc: Callable[..., str],
) -> None:
    # No ``test_db``: it names the SUITE database, which this test does not
    # touch. Keeping it would restate the confusion this repair removed.
    seed_live_doc(title="Larkspur review", content="Synthetic body for the gate test.")
    # The LIVE database — the one the restore would replace — not ``test_db``.
    before = live_document_count(TEST_DATABASE_URL)
    assert before == 1

    result = _invoke(archive, stdin="y\nnope\n")

    assert result.exit_code == 1
    assert runner.calls == []
    assert live_document_count(TEST_DATABASE_URL) == before


def test_yes_flag_cannot_bypass_typed_phrase_on_non_empty_target(
    archive: Path,
    wired: RefusingConnectionFactory,
    runner: RecordingRunner,
    seed_live_doc: Callable[..., str],
) -> None:
    """The exact contract from §5.9: --yes never supplies the phrase."""
    seed_live_doc(title="Larkspur review", content="Synthetic body for the gate test.")
    # Load-bearing: on an EMPTY target --yes legitimately skips the phrase, so
    # without a non-empty live corpus this test proves the opposite of its name.
    assert live_document_count(TEST_DATABASE_URL) == 1

    result = _invoke(archive, "--yes", stdin="")

    assert result.exit_code == 1
    assert runner.calls == []
    assert "Aborted" in result.output


def test_refuses_prod_database_url_without_typed_phrase(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    test_db: psycopg.Connection,
    runner: RecordingRunner,
) -> None:
    """The required guard: a prod DSN plus an empty phrase changes nothing.

    The injected connection factory refuses production outright, so this proves
    the real database is never opened — not merely that the gate said no.
    """
    # Sanity: the DSN under test really is what conftest calls production.
    assert _looks_like_prod_db("localhost", 55432, "second_brain")

    # Build the archive against the TEST database, then re-point the CLI at prod.
    safe_cfg = Config(
        database_url=TEST_DATABASE_URL,
        embedder=EMBEDDER_NAME,
        vault_path=tmp_path / "vault",
        backup_dir=tmp_path / "backups",
        brain_home=tmp_path / "brain-home",
    )
    archive = create_backup(
        safe_cfg,
        runner=StubDumpRunner(responses={"--version": CONTAINER_VERSION}),
        clock=lambda: NOW,
        embedder=FakeEmbedder(dim=EMBEDDING_DIM),
    ).archive_path

    prod_cfg = Config(
        database_url=PROD_DSN,
        embedder=EMBEDDER_NAME,
        vault_path=tmp_path / "vault",
        backup_dir=tmp_path / "backups",
        brain_home=tmp_path / "brain-home",
    )
    factory = RefusingConnectionFactory()
    monkeypatch.setenv("DATABASE_URL", PROD_DSN)
    monkeypatch.setattr(Config, "load", classmethod(lambda _cls: prod_cfg))
    monkeypatch.setattr(
        "brain.cli._build_embedder", lambda _cfg: FakeEmbedder(dim=EMBEDDING_DIM)
    )
    monkeypatch.setattr("brain.cli_backup.connect", factory)
    monkeypatch.setattr("brain.backup.restore.connect", factory)
    # Load-bearing: without this the `runner.calls` assertion below cannot
    # fail, because nothing would ever hand `runner` to production code.
    _inject_fake_runner(monkeypatch, runner)

    result = _invoke(archive, stdin="y\n\n")

    assert result.exit_code != 0
    assert runner.calls == []
    # `opened == []` on its own is satisfied by a CLI that never tried at all.
    # Pin that it DID reach for the production DSN and was refused, so the
    # emptiness below means "refused", not "never got there".
    assert factory.requested, "the CLI never attempted a connection"
    # The prod database was never successfully opened.
    assert factory.opened == []


def test_yes_skips_prompts_on_empty_target(
    archive: Path,
    wired: RefusingConnectionFactory,
    runner: RecordingRunner,
    test_db: psycopg.Connection,
) -> None:
    """Fresh-machine disaster recovery must stay scriptable (§5.9, §6.11)."""
    result = _invoke(archive, "--yes", stdin="")

    assert result.exit_code == 0, result.output
    assert 'Type "restore and overwrite my brain"' not in result.output


def test_correct_phrase_proceeds_against_the_staging_database(
    archive: Path,
    wired: RefusingConnectionFactory,
    runner: RecordingRunner,
    seed_live_doc: Callable[..., str],
) -> None:
    """pg_restore must target the STAGING database, never the live one."""
    seed_live_doc(title="Larkspur review", content="Synthetic body for the gate test.")

    result = _invoke(archive, stdin="y\nrestore and overwrite my brain\n")

    assert result.exit_code == 0, result.output
    restore_calls = [call for call in runner.calls if "pg_restore" in " ".join(call)]
    assert restore_calls, runner.flat_calls
    targeted = [call[call.index("-d") + 1] for call in restore_calls if "-d" in call]
    assert targeted, restore_calls
    for target in targeted:
        assert target.startswith(f"{TEST_DB_NAME}_restore_")
        assert target != TEST_DB_NAME


def test_missing_archive_exits_one(
    tmp_path: Path, wired: RefusingConnectionFactory
) -> None:
    result = _invoke(tmp_path / "no-such-archive.tar.gz")

    assert result.exit_code == 1
    assert "no such archive" in result.output.lower()


def test_corrupt_archive_exits_three_before_extraction(
    archive: Path, wired: RefusingConnectionFactory, runner: RecordingRunner
) -> None:
    """A truncated copy is refused with the distinct 'won't work' code (§6.9)."""
    corrupted = bytearray(archive.read_bytes())
    corrupted[-1] ^= 0xFF
    archive.write_bytes(bytes(corrupted))

    result = _invoke(archive, stdin="y\n")

    assert result.exit_code == 3
    assert runner.calls == []


def test_missing_sidecar_warns_but_proceeds(
    archive: Path, wired: RefusingConnectionFactory
) -> None:
    """A deleted sidecar is 'unknown', not 'corrupt' — warn and fall back (§6.9)."""
    archive.with_suffix(archive.suffix + ".sha256").unlink()

    result = _invoke(archive, stdin="n\n")

    assert ".sha256 is missing" in result.output


def test_vault_only_on_a_no_vault_archive_is_bad_parameter(
    test_db: psycopg.Connection, cfg: Config, wired: RefusingConnectionFactory
) -> None:
    """§6.7: never let the user believe notes were restored when none exist."""
    no_vault = create_backup(
        cfg,
        include_vault=False,
        runner=StubDumpRunner(responses={"--version": CONTAINER_VERSION}),
        clock=lambda: NOW,
        embedder=FakeEmbedder(dim=EMBEDDING_DIM),
    ).archive_path

    result = _invoke(no_vault, "--vault-only")

    assert result.exit_code == 2
    assert "contains no vault" in result.output


def test_embedder_mismatch_exits_three_and_prints_the_remedy(
    monkeypatch: pytest.MonkeyPatch,
    archive: Path,
    cfg: Config,
    wired: RefusingConnectionFactory,
    runner: RecordingRunner,
) -> None:
    """The single most important abort, surfaced at the CLI (§6.3)."""
    monkeypatch.setattr(
        "brain.cli._build_embedder", lambda _cfg: FakeEmbedder(dim=1024)
    )

    result = _invoke(archive, stdin="y\n")

    assert result.exit_code == 3
    assert "cannot be re-projected" in result.output
    assert runner.calls == []


def test_json_result_object_has_the_documented_keys(
    archive: Path, wired: RefusingConnectionFactory, test_db: psycopg.Connection
) -> None:
    import json

    result = _invoke(archive, "--yes", "--json", stdin="")

    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    for key in (
        "schema",
        "restored",
        "db_restored",
        "vault_restored",
        "archive_path",
        "manifest",
        "pre_restore_backup",
        "replaced_database",
        "replaced_vault_path",
        "documents",
        "chunks",
        "follow_up",
    ):
        assert key in payload, f"missing key: {key}"
    assert payload["restored"] is True
    assert payload["follow_up"] == ["brain graphrag build --force", "brain doctor"]


def test_blocked_restore_emits_json_issues(
    monkeypatch: pytest.MonkeyPatch,
    archive: Path,
    wired: RefusingConnectionFactory,
    runner: RecordingRunner,
) -> None:
    """--json must stay parseable even when the restore is refused."""
    import json

    monkeypatch.setattr(
        "brain.cli._build_embedder", lambda _cfg: FakeEmbedder(dim=1024)
    )

    result = _invoke(archive, "--json", stdin="y\n")

    assert result.exit_code == 3
    payload = json.loads(result.output)
    assert payload["restored"] is False
    assert payload["blocked"] is True
    assert any(issue["code"] == "dim" for issue in payload["issues"])
    assert runner.calls == []
