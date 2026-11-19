"""`brain restore` QA row C24 regressions, end to end through the CLI (F22-F27).

F22: the post-restore drop hint named the production container whatever server
the restore ran against. F23: `--json` printed prompts on stdout. F24:
`--vault-only` previewed — and gated on — the database it never touches. F27: a
declined, refused or failed restore left `$BRAIN_HOME/run/restore-<ts>/`, a
plaintext unpack of the archive, behind. Plan:
docs/plans/2026-11-19-restore-hints.md.

Runs on `test_restore_gate.py`'s throwaway restore sandbox and reuses its
fixtures, so the same guards apply: :class:`RefusingConnectionFactory` refuses
production, and every external command goes through a fake runner — no real
`docker`, `pg_restore` or `psql` ever runs.
"""
from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

import psycopg
import pytest

from brain.backup import create_backup
from brain.config import Config
from tests.backup_fakes import (
    CONTAINER_VERSION,
    RecordingRunner,
    StubAndRestoreRunner,
    StubDumpRunner,
    live_document_count,
    repo_root_guard,  # noqa: F401 — autouse fixture
)
from tests.conftest import FakeEmbedder
from tests.test_backup_manifest import EMBEDDING_DIM
from tests.test_restore_gate import (  # noqa: F401 — fixtures, incl. the autouse sandbox reset
    NOW,
    TEST_DATABASE_URL,
    TEST_DB_NAME,
    RefusingConnectionFactory,
    _inject_fake_runner,
    _invoke,
    _restore_sandbox_state,
    archive,
    cfg,
    runner,
    seed_live_doc,
    wired,
)

PHRASE = "restore and overwrite my brain"
#: Synthetic, and harmless to libpq: ``sslpassword`` only decrypts a client key,
#: and none is configured. The server-side password cannot be synthetic (the
#: test server enforces it), so the unit tests in ``test_restore_hints.py`` cover
#: a synthetic ``password=``; here the real one is asserted absent by its shape.
SSL_SECRET = "Synthetic-SslKeyPass-91c2"


def _sandbox_port() -> str:
    port = psycopg.conninfo.conninfo_to_dict(TEST_DATABASE_URL).get("port")
    return str(port) if port else "5432"


def _discovered_container_runner(container: str) -> RecordingRunner:
    """The configured container serves nothing; ``docker ps`` finds ``container``.

    So the container leg is chosen by discovery, exactly as for the QA lane's
    own Postgres in C24 — and the configured name must not leak into the hint.
    """
    return StubAndRestoreRunner(
        base_dsn=TEST_DATABASE_URL,
        responses={
            "--version": CONTAINER_VERSION,
            "docker port": "",
            "docker ps": f"{container}\t0.0.0.0:{_sandbox_port()}->5432/tcp\n",
        },
    )


def _staging_dirs(cfg: Config) -> list[Path]:  # noqa: F811 — the imported fixture
    return sorted((cfg.brain_home / "run").glob("restore-*"))


def _seed_vault(cfg: Config) -> None:  # noqa: F811 — the imported fixture
    cfg.vault_path.mkdir(parents=True, exist_ok=True)
    (cfg.vault_path / "larkspur.md").write_text("synthetic note", encoding="utf-8")


@pytest.fixture
def vault_archive(test_db: psycopg.Connection, cfg: Config) -> Path:  # noqa: F811 — the imported fixture
    """An archive that carries a vault, taken before the test sets the vault up.

    The shared ``archive`` fixture is taken with no vault directory, so it
    carries no vault, and ``--vault-only`` on it is refused (exit 2) before the
    preview. The vault is removed again afterwards: each test seeds what it needs.
    """
    import shutil

    _seed_vault(cfg)
    path = create_backup(
        cfg,
        runner=StubDumpRunner(responses={"--version": CONTAINER_VERSION}),
        clock=lambda: NOW,
        embedder=FakeEmbedder(dim=EMBEDDING_DIM),
    ).archive_path
    shutil.rmtree(cfg.vault_path)
    return path


@pytest.mark.parametrize(
    "container", ["synthetic-qa-postgres", "synthetic`id`;rm-postgres"]
)
def test_drop_hint_names_the_container_the_restore_used(
    monkeypatch: pytest.MonkeyPatch,
    archive: Path,  # noqa: F811 — the imported fixture
    cfg: Config,  # noqa: F811 — the imported fixture
    wired: RefusingConnectionFactory,  # noqa: F811 — the imported fixture
    container: str,
) -> None:
    """F22: C24 printed `docker exec second-brain-postgres …` for another server."""
    import shlex

    runner = _discovered_container_runner(container)  # noqa: F811 — the imported fixture
    _inject_fake_runner(monkeypatch, runner)

    result = _invoke(archive, "--yes")

    assert result.exit_code == 0, result.output
    assert any(container in call for call in runner.calls), runner.flat_calls
    hint = next(
        line for line in result.output.splitlines() if "Drop it when satisfied" in line
    )
    command = hint.split("Drop it when satisfied:", 1)[1].strip()
    words = shlex.split(command)
    assert words[:3] == ["docker", "exec", container]
    assert words[-2] == "-c"
    assert words[-1].startswith(f"DROP DATABASE {TEST_DB_NAME}_replaced_")
    assert "second-brain-postgres" not in result.output


def test_drop_hint_on_the_host_leg_is_psql_against_the_dsn(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    archive: Path,  # noqa: F811 — the imported fixture
    cfg: Config,  # noqa: F811 — the imported fixture
    wired: RefusingConnectionFactory,  # noqa: F811 — the imported fixture
) -> None:
    """F22: no container serves the DSN, so the hint must not name one."""
    bin_dir = tmp_path / "host-bin"
    bin_dir.mkdir()
    for tool in ("pg_restore", "pg_dump"):
        stub = bin_dir / tool
        stub.write_text("#!/bin/sh\nexit 99\n", encoding="utf-8")  # never executed
        stub.chmod(0o755)
    monkeypatch.setenv("PATH", str(bin_dir))
    runner = StubAndRestoreRunner(  # noqa: F811 — the imported fixture
        base_dsn=TEST_DATABASE_URL,
        responses={"--version": CONTAINER_VERSION, "docker port": "", "docker ps": ""},
    )
    _inject_fake_runner(
        monkeypatch, runner, pre_backup=lambda: tmp_path / "synthetic-pre.tar.gz"
    )

    result = _invoke(archive, "--yes")

    assert result.exit_code == 0, result.output
    assert any(call[0] == str(bin_dir / "pg_restore") for call in runner.calls)
    hint = next(
        line for line in result.output.splitlines() if "Drop it when satisfied" in line
    )
    assert "docker" not in hint
    assert f"psql -h localhost -p {_sandbox_port()} -U " in hint
    assert f"DROP DATABASE {TEST_DB_NAME}_replaced_" in hint


@pytest.mark.parametrize("json_mode", [False, True], ids=["human", "json"])
def test_no_credential_reaches_restore_output(
    monkeypatch: pytest.MonkeyPatch, archive: Path, cfg: Config, json_mode: bool  # noqa: F811 — the imported fixture
) -> None:
    """F22: neither the password nor sslpassword appears, human or JSON."""
    secret_cfg = Config(
        database_url=psycopg.conninfo.make_conninfo(
            TEST_DATABASE_URL, sslpassword=SSL_SECRET
        ),
        embedder=cfg.embedder,
        vault_path=cfg.vault_path,
        backup_dir=cfg.backup_dir,
        brain_home=cfg.brain_home,
    )
    factory = RefusingConnectionFactory()
    monkeypatch.setattr(Config, "load", classmethod(lambda _cls: secret_cfg))
    monkeypatch.setattr(
        "brain.cli._build_embedder", lambda _cfg: FakeEmbedder(dim=EMBEDDING_DIM)
    )
    monkeypatch.setattr("brain.cli_backup.connect", factory)
    monkeypatch.setattr("brain.backup.restore.connect", factory)
    _inject_fake_runner(monkeypatch, _discovered_container_runner("synthetic-qa-postgres"))

    result = _invoke(archive, "--yes", *(["--json"] if json_mode else []))

    assert result.exit_code == 0, result.output
    assert SSL_SECRET not in result.output
    assert secret_cfg.database_url not in result.output
    assert "password" not in result.output.lower()
    if not json_mode:
        assert "Drop it when satisfied" in result.output


def test_json_with_typed_phrase_keeps_stdout_pure_json(
    archive: Path,  # noqa: F811 — the imported fixture
    wired: RefusingConnectionFactory,  # noqa: F811 — the imported fixture
    seed_live_doc: Callable[..., str],  # noqa: F811 — the imported fixture
) -> None:
    """F23: C24 printed the typed-phrase prompt on stdout ahead of the JSON."""
    import json

    seed_live_doc(title="Larkspur review", content="Synthetic body for the gate test.")

    result = _invoke(archive, "--yes", "--json", stdin=f"{PHRASE}\n")

    assert result.exit_code == 0, result.output
    assert result.stdout.startswith("{"), result.stdout  # not even a stray space
    payload = json.loads(result.stdout)
    assert payload["restored"] is True
    assert f'Type "{PHRASE}"' in result.stderr


def test_json_yes_on_empty_target_is_pure_json(
    archive: Path, wired: RefusingConnectionFactory, test_db: psycopg.Connection  # noqa: F811 — the imported fixture
) -> None:
    import json

    result = _invoke(archive, "--yes", "--json", stdin="")

    assert result.exit_code == 0, result.output
    assert json.loads(result.stdout)["restored"] is True


@pytest.mark.parametrize(
    ("args", "stdin"),
    [((), "n\n"), (("--yes",), ""), ((), "y\nnot the phrase\n")],
    ids=["declined", "yes-eof", "wrong-phrase"],
)
def test_json_abort_emits_an_abort_object(
    archive: Path,  # noqa: F811 — the imported fixture
    cfg: Config,  # noqa: F811 — the imported fixture
    wired: RefusingConnectionFactory,  # noqa: F811 — the imported fixture
    runner: RecordingRunner,  # noqa: F811 — the imported fixture
    seed_live_doc: Callable[..., str],  # noqa: F811 — the imported fixture
    args: tuple[str, ...],
    stdin: str,
) -> None:
    """F23: an aborted `--json` run printed a prompt and no JSON at all."""
    import json

    seed_live_doc(title="Larkspur review", content="Synthetic body for the gate test.")

    result = _invoke(archive, *args, "--json", stdin=stdin)

    assert result.exit_code == 1
    assert result.stdout.startswith("{"), result.stdout
    assert json.loads(result.stdout) == {"restored": False, "aborted": True}
    assert runner.calls == []
    assert live_document_count(TEST_DATABASE_URL) == 1
    assert _staging_dirs(cfg) == []



class _InterruptingStdin:
    """A stdin whose read is interrupted, as by Ctrl-C at the prompt."""

    def readline(self) -> str:
        raise KeyboardInterrupt


def test_json_ctrl_c_at_the_prompt_emits_the_abort_object(
    monkeypatch: pytest.MonkeyPatch,
    archive: Path,  # noqa: F811 — the imported fixture
    cfg: Config,  # noqa: F811 — the imported fixture
    wired: RefusingConnectionFactory,  # noqa: F811 — the imported fixture
    runner: RecordingRunner,  # noqa: F811 — the imported fixture
    seed_live_doc: Callable[..., str],  # noqa: F811 — the imported fixture
) -> None:
    """F23: Ctrl-C at a `--json` prompt is an abort, not a traceback.

    ``CliRunner`` only takes str/bytes input, so the interrupt is injected by
    pointing ``brain.cli_backup``'s ``sys`` at a stdin that raises.
    """
    import json
    from types import SimpleNamespace

    seed_live_doc(title="Larkspur review", content="Synthetic body for the gate test.")
    monkeypatch.setattr(
        "brain.cli_backup.sys", SimpleNamespace(stdin=_InterruptingStdin())
    )

    result = _invoke(archive, "--json", stdin="y\n")

    assert result.exit_code == 1, result.output
    assert json.loads(result.stdout) == {"restored": False, "aborted": True}
    assert runner.calls == []
    assert live_document_count(TEST_DATABASE_URL) == 1
    assert _staging_dirs(cfg) == []

def test_json_missing_sidecar_warning_goes_to_stderr(
    archive: Path, wired: RefusingConnectionFactory, test_db: psycopg.Connection  # noqa: F811 — the imported fixture
) -> None:
    import json

    archive.with_suffix(archive.suffix + ".sha256").unlink()

    result = _invoke(archive, "--yes", "--json", stdin="")

    assert result.exit_code == 0, result.output
    assert json.loads(result.stdout)["restored"] is True
    assert ".sha256 is missing" in result.stderr


def test_vault_only_preview_does_not_list_the_database(
    vault_archive: Path,
    cfg: Config,  # noqa: F811 — the imported fixture
    wired: RefusingConnectionFactory,  # noqa: F811 — the imported fixture
    seed_live_doc: Callable[..., str],  # noqa: F811 — the imported fixture
) -> None:
    """F24: C24's `--vault-only` preview listed `• database …`."""
    seed_live_doc(title="Larkspur review", content="Synthetic body for the gate test.")
    _seed_vault(cfg)

    result = _invoke(vault_archive, "--vault-only", stdin="n\n")

    assert "WILL BE OVERWRITTEN" in result.output
    assert f"• vault {cfg.vault_path}" in result.output
    assert "• database" not in result.output


def test_vault_only_with_empty_vault_does_not_gate_on_the_database(
    vault_archive: Path,
    cfg: Config,  # noqa: F811 — the imported fixture
    wired: RefusingConnectionFactory,  # noqa: F811 — the imported fixture
    seed_live_doc: Callable[..., str],  # noqa: F811 — the imported fixture
) -> None:
    """F24: `--vault-only` never touches the database, so it cannot gate on it."""
    seed_live_doc(title="Larkspur review", content="Synthetic body for the gate test.")

    result = _invoke(vault_archive, "--yes", "--vault-only", stdin="")

    assert result.exit_code == 0, result.output
    assert f'Type "{PHRASE}"' not in result.output
    assert live_document_count(TEST_DATABASE_URL) == 1  # the database is untouched


def test_vault_only_with_non_empty_vault_still_demands_the_phrase(
    vault_archive: Path,
    cfg: Config,  # noqa: F811 — the imported fixture
    wired: RefusingConnectionFactory,  # noqa: F811 — the imported fixture
    runner: RecordingRunner,  # noqa: F811 — the imported fixture
) -> None:
    """F24 mirror: the vault IS replaced, so `--yes` still cannot skip the phrase."""
    _seed_vault(cfg)

    result = _invoke(vault_archive, "--yes", "--vault-only", stdin="")

    assert result.exit_code == 1
    assert f'Type "{PHRASE}"' in result.output
    assert runner.calls == []
    assert (cfg.vault_path / "larkspur.md").is_file()


def test_declined_restore_leaves_no_staging_dir(
    archive: Path, cfg: Config, wired: RefusingConnectionFactory  # noqa: F811 — the imported fixture
) -> None:
    """F27: C24's declined runs each left `run/restore-<ts>/` behind."""
    result = _invoke(archive, stdin="n\n")

    assert result.exit_code == 1
    assert _staging_dirs(cfg) == []


def test_refused_typed_phrase_leaves_no_staging_dir(
    archive: Path,  # noqa: F811 — the imported fixture
    cfg: Config,  # noqa: F811 — the imported fixture
    wired: RefusingConnectionFactory,  # noqa: F811 — the imported fixture
    seed_live_doc: Callable[..., str],  # noqa: F811 — the imported fixture
) -> None:
    seed_live_doc(title="Larkspur review", content="Synthetic body for the gate test.")

    result = _invoke(archive, stdin="y\nnope\n")

    assert result.exit_code == 1
    assert _staging_dirs(cfg) == []


def test_blocked_restore_leaves_no_staging_dir(
    monkeypatch: pytest.MonkeyPatch,
    archive: Path,  # noqa: F811 — the imported fixture
    cfg: Config,  # noqa: F811 — the imported fixture
    wired: RefusingConnectionFactory,  # noqa: F811 — the imported fixture
) -> None:
    monkeypatch.setattr(
        "brain.cli._build_embedder", lambda _cfg: FakeEmbedder(dim=1024)
    )

    result = _invoke(archive, stdin="y\n")

    assert result.exit_code == 3
    assert _staging_dirs(cfg) == []


def test_vault_only_on_no_vault_archive_leaves_no_staging_dir(
    test_db: psycopg.Connection, cfg: Config, wired: RefusingConnectionFactory  # noqa: F811 — the imported fixture
) -> None:
    no_vault = create_backup(
        cfg,
        include_vault=False,
        runner=StubDumpRunner(responses={"--version": CONTAINER_VERSION}),
        clock=lambda: NOW,
        embedder=FakeEmbedder(dim=EMBEDDING_DIM),
    ).archive_path

    result = _invoke(no_vault, "--vault-only")

    assert result.exit_code == 2
    assert _staging_dirs(cfg) == []


def test_failed_pg_restore_leaves_no_staging_dir(
    monkeypatch: pytest.MonkeyPatch,
    archive: Path,  # noqa: F811 — the imported fixture
    cfg: Config,  # noqa: F811 — the imported fixture
    wired: RefusingConnectionFactory,  # noqa: F811 — the imported fixture
    seed_live_doc: Callable[..., str],  # noqa: F811 — the imported fixture
) -> None:
    """F27: a failure inside `restore_backup` must clean up too."""
    import subprocess

    seed_live_doc(title="Larkspur review", content="Synthetic body for the gate test.")
    failing = StubAndRestoreRunner(
        base_dsn=TEST_DATABASE_URL,
        responses={"--version": CONTAINER_VERSION},
        raises={
            "--exit-on-error": subprocess.CalledProcessError(
                1, ["pg_restore"], "", "synthetic pg_restore failure"
            )
        },
    )
    _inject_fake_runner(monkeypatch, failing)

    result = _invoke(archive, stdin=f"y\n{PHRASE}\n")

    assert result.exit_code == 1, result.output
    assert any("--exit-on-error" in call for call in failing.calls)
    assert live_document_count(TEST_DATABASE_URL) == 1
    assert _staging_dirs(cfg) == []


def test_successful_restore_removes_staging_dir_and_still_prints_the_hint(
    archive: Path,  # noqa: F811 — the imported fixture
    cfg: Config,  # noqa: F811 — the imported fixture
    wired: RefusingConnectionFactory,  # noqa: F811 — the imported fixture
    test_db: psycopg.Connection,
) -> None:
    result = _invoke(archive, "--yes")

    assert result.exit_code == 0, result.output
    assert "Drop it when satisfied" in result.output
    assert _staging_dirs(cfg) == []

