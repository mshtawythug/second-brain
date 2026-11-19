"""The ingest CLI boundary refuses bad input in one line (0.4.0 QA F2, F3, F4, F10, F12).

Every test here reproduces a QA symptom: a Rich traceback, or a value that was silently
accepted. None needs a database. "Postgres is down" is a synthetic DSN on port 1, which
refuses at once, and every DSN carries a synthetic password that must never be printed.
"""
from __future__ import annotations

import ast
from pathlib import Path

import psycopg
import pytest
import typer
from typer.testing import CliRunner, Result

from brain.cli import app
from tests.conftest import FakeEmbedder

pytestmark = pytest.mark.nodb

SYNTHETIC_PASSWORD = "synthetic-pw-0000"
SYNTHETIC_USER = "synthetic_user"
SYNTHETIC_DB = "unreachable_test"
UNREACHABLE_URL = (
    f"postgresql://{SYNTHETIC_USER}:{SYNTHETIC_PASSWORD}@127.0.0.1:1/{SYNTHETIC_DB}"
)
SRC = Path(__file__).resolve().parents[1] / "src" / "brain"


@pytest.fixture
def offline_cli(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    """Point the CLI at the unreachable DSN, a fake embedder and a tmp vault."""
    monkeypatch.setenv("DATABASE_URL", UNREACHABLE_URL)
    monkeypatch.setenv("BRAIN_VAULT_PATH", str(tmp_path / "vault"))
    monkeypatch.setenv("BRAIN_GRAPH_ENABLED", "false")
    monkeypatch.setattr("brain.cli._build_embedder", lambda cfg: FakeEmbedder())
    monkeypatch.setattr("brain.cli._build_enricher", lambda cfg: None)
    return tmp_path


def _assert_no_traceback(result: Result) -> None:
    assert "Traceback" not in result.output
    assert result.exception is None or isinstance(result.exception, SystemExit)


def _assert_no_credentials(result: Result) -> None:
    for secret in (SYNTHETIC_PASSWORD, SYNTHETIC_USER, SYNTHETIC_DB, "postgresql://"):
        assert secret not in result.output


# ---------------------------------------------------------------------------
# F2: a refused connection is one line, never the credentials.
# ---------------------------------------------------------------------------


def test_ingest_stdin_unreachable_db_prints_one_line(offline_cli: Path) -> None:
    result = CliRunner().invoke(
        app,
        ["ingest-stdin", "--source", "slack", "--external-id", "qa-1", "--title", "QA",
         "--no-enrich"],
        input="hello synthetic\n",
    )
    assert result.exit_code == 1, result.output
    assert "cannot reach DATABASE_URL 127.0.0.1:1 (OperationalError)" in result.output
    _assert_no_traceback(result)
    _assert_no_credentials(result)


@pytest.mark.parametrize("command", ["ingest", "ingest-dir", "reembed"])
def test_other_ingest_commands_unreachable_db(offline_cli: Path, command: str) -> None:
    corpus = offline_cli / "corpus"
    corpus.mkdir()
    note = corpus / "note.txt"
    note.write_text("synthetic body\n", encoding="utf-8")
    args = {
        "ingest": ["ingest", str(note), "--no-enrich"],
        "ingest-dir": ["ingest-dir", str(corpus), "--no-enrich"],
        "reembed": ["reembed"],
    }[command]
    result = CliRunner().invoke(app, args)
    assert result.exit_code == 1, result.output
    assert "cannot reach DATABASE_URL 127.0.0.1:1 (OperationalError)" in result.output
    _assert_no_traceback(result)
    _assert_no_credentials(result)


def test_connect_or_exit_unparseable_dsn(capsys: pytest.CaptureFixture[str]) -> None:
    from brain import cli_ingest_boundary

    with (
        pytest.raises(typer.Exit) as excinfo,
        cli_ingest_boundary.connect_or_exit(f"not a dsn {SYNTHETIC_PASSWORD}"),
    ):
        pass  # pragma: no cover - the connect refuses first
    assert excinfo.value.exit_code == 1
    err = capsys.readouterr().err
    assert "cannot reach DATABASE_URL (unparseable)" in err
    assert SYNTHETIC_PASSWORD not in err


def _imports_bare_connect(path: Path) -> bool:
    """True when ``path`` imports ``connect`` from ``brain.db`` (relative or absolute)."""
    tree = ast.parse(path.read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.ImportFrom)
            and node.module in ("db", "brain.db")
            and any(alias.name == "connect" for alias in node.names)
        ):
            return True
    return False


def ingest_cli_modules(src: Path) -> list[Path]:
    """Every ingest CLI module, plus the ``ingest-dir --sync`` helper."""
    return sorted([*src.glob("cli_ingest*.py"), src / "cli_import_sync.py"])


def test_ingest_cli_modules_do_not_open_bare_connections() -> None:
    """A bare ``brain.db.connect`` on the ingest surface is a traceback when Postgres is down."""
    modules = ingest_cli_modules(SRC)
    assert len(modules) >= 9  # the set this guard was written against; it must not go empty
    offenders = [p.name for p in modules if _imports_bare_connect(p)]
    assert offenders == [], f"use cli_ingest_boundary.connect_or_exit in: {offenders}"


# ---------------------------------------------------------------------------
# F3 / F4: --metadata and --date are usage errors, checked before stdin is read.
# ---------------------------------------------------------------------------


def _stdin(offline_cli: Path, *extra: str, body: str = "") -> Result:
    # An EMPTY stdin proves the order: before the fix, "stdin was empty" (exit 1) won.
    return CliRunner().invoke(
        app,
        ["ingest-stdin", "--source", "slack", "--external-id", "qa-1", "--title", "QA",
         "--no-enrich", *extra],
        input=body,
    )


def test_ingest_stdin_metadata_not_json_exits_2(offline_cli: Path) -> None:
    result = _stdin(offline_cli, "--metadata", "{bad json")
    assert result.exit_code == 2, result.output
    assert "--metadata" in result.output
    assert "not valid JSON" in result.output
    assert "stdin was empty" not in result.output
    _assert_no_traceback(result)


@pytest.mark.parametrize("raw", ["[1,2]", '"s"', "3", "null"])
def test_ingest_stdin_metadata_not_object_exits_2(offline_cli: Path, raw: str) -> None:
    result = _stdin(offline_cli, "--metadata", raw)
    assert result.exit_code == 2, result.output
    assert "must be a JSON object" in result.output
    assert "stdin was empty" not in result.output
    _assert_no_traceback(result)


@pytest.mark.parametrize("raw", ["2026-13-45", "yesterday"])
def test_ingest_stdin_bad_date_exits_2(offline_cli: Path, raw: str) -> None:
    result = _stdin(offline_cli, "--date", raw)
    assert result.exit_code == 2, result.output
    assert "--date" in result.output
    assert "stdin was empty" not in result.output
    _assert_no_traceback(result)


@pytest.mark.parametrize("raw", ["2026-11-11", "Tue, 04 May 2026 14:23:01 -0400"])
def test_ingest_stdin_good_date_passes_validation(offline_cli: Path, raw: str) -> None:
    # Validation passes, so the run reaches the (empty) stdin check.
    result = _stdin(offline_cli, "--date", raw)
    assert result.exit_code == 1, result.output
    assert "stdin was empty" in result.output


def test_stdin_metadata_or_exit_merges_date() -> None:
    from brain import cli_ingest_boundary

    meta = cli_ingest_boundary.stdin_metadata_or_exit('{"channel": "qa"}', "2026-11-11")
    assert meta == {"channel": "qa", "date": "2026-11-11"}
    assert cli_ingest_boundary.stdin_metadata_or_exit(None, None) == {}


# ---------------------------------------------------------------------------
# F10: an unsupported or malformed file is one line.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("suffix", [".xyz", ".py"])
def test_ingest_unsupported_extension_exits_2(offline_cli: Path, suffix: str) -> None:
    target = offline_cli / f"widget{suffix}"
    target.write_text("def widget():\n    return 1\n", encoding="utf-8")
    result = CliRunner().invoke(app, ["ingest", str(target), "--no-enrich"])
    assert result.exit_code == 2, result.output
    assert f"does not read {suffix} files" in result.output
    assert "brain ingest-code" in result.output
    assert ".md" in result.output  # the list of types it does read
    assert "ValueError" not in result.output
    _assert_no_traceback(result)


def test_ingest_malformed_pdf_one_line(offline_cli: Path) -> None:
    target = offline_cli / "bad.pdf"
    target.write_bytes(b"not-a-real-pdf synthetic-body-marker")
    result = CliRunner().invoke(app, ["ingest", str(target), "--no-enrich"])
    assert result.exit_code == 1, result.output
    assert "cannot read bad.pdf: ValueError" in result.output
    assert "synthetic-body-marker" not in result.output  # no document content
    _assert_no_traceback(result)


# ---------------------------------------------------------------------------
# F12: reembed's ranges, enforced before any DB or embedder use.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "args",
    [["--batch-size", "0"], ["--batch-size", "-3"], ["--limit", "0"], ["--limit", "-1"]],
)
def test_reembed_rejects_nonpositive_batch_size_and_limit(
    offline_cli: Path, args: list[str]
) -> None:
    result = CliRunner().invoke(app, ["reembed", "--dry-run", *args])
    assert result.exit_code == 2, result.output
    assert "Invalid value" in result.output
    # Port 1 refuses: had the option reached the connect, the F2 line would print.
    assert "cannot reach" not in result.output


# ---------------------------------------------------------------------------
# F4's parser moved to a leaf, unchanged.
# ---------------------------------------------------------------------------


def test_parse_sent_at_moved_unchanged() -> None:
    from brain import ingest
    from brain.ingest import sent_at

    assert ingest._parse_sent_at is sent_at.parse_sent_at


def test_connect_error_class_is_reported_not_its_message() -> None:
    from brain import cli_ingest_boundary

    line = cli_ingest_boundary.unreachable_line(
        UNREACHABLE_URL, psycopg.OperationalError(f"boom {SYNTHETIC_PASSWORD}")
    )
    assert line.startswith("cannot reach DATABASE_URL 127.0.0.1:1 (OperationalError)")
    assert SYNTHETIC_PASSWORD not in line
