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
    assert "cannot reach the database (OperationalError)" in result.output
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
    assert "cannot reach the database (OperationalError)" in result.output
    _assert_no_traceback(result)
    _assert_no_credentials(result)


#: The calls that open a connection with no refusal around it. ``connect_raw`` is not
#: here: ``cli_ingest_code`` opens through it inside its own one-line handler.
_BARE_CONNECTS = frozenset({"connect", "connect_age"})
_DB_MODULES = frozenset({"db", "brain.db"})


def _module_aliases(tree: ast.Module) -> tuple[set[str], set[str]]:
    """The dotted names bound to ``brain.db`` and to ``psycopg`` in ``tree``."""
    db_names: set[str] = set()
    pg_names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name == "brain.db":
                    db_names.add(alias.asname or "brain.db")
                elif alias.name == "psycopg":
                    pg_names.add(alias.asname or "psycopg")
        elif isinstance(node, ast.ImportFrom) and node.module in (None, "brain"):
            db_names.update(a.asname or a.name for a in node.names if a.name == "db")
    return db_names, pg_names


def _dotted(node: ast.expr) -> str | None:
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        base = _dotted(node.value)
        return None if base is None else f"{base}.{node.attr}"
    return None


def _imports_bare_connect(path: Path) -> bool:
    """True when ``path`` can open a connection that is not ``connect_or_exit``.

    Review rev-137 #5 widened this from ``from .db import connect`` to every spelling
    of the same thing:
    - ``connect`` / ``connect_age`` imported from ``brain.db`` (relative or absolute);
    - the same names reached as attributes of a module bound to ``brain.db``
      (``from . import db``, ``import brain.db [as x]``);
    - ``psycopg.connect`` through any alias, and ``from psycopg import connect``.
    """
    tree = ast.parse(path.read_text(encoding="utf-8"))
    db_names, pg_names = _module_aliases(tree)
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module in (*_DB_MODULES, "psycopg"):
            wanted = _BARE_CONNECTS if node.module != "psycopg" else {"connect"}
            if any(alias.name in wanted for alias in node.names):
                return True
        if isinstance(node, ast.Attribute):
            owner = _dotted(node.value)
            if owner in db_names and node.attr in _BARE_CONNECTS:
                return True
            if owner in pg_names and node.attr == "connect":
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


@pytest.mark.parametrize(
    "raw",
    ["2026-13-45", "yesterday", "0001-01-01T00:00:00+05:00", "9999-12-31T23:00:00-05:00"],
)
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


# ---------------------------------------------------------------------------
# Review round rev-137.
# ---------------------------------------------------------------------------

# Item 2: --metadata values Postgres would reject are usage errors too.

_DEEP = "[" * 100_000 + "]" * 100_000


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ('{"a": NaN}', "finite"),
        ('{"a": Infinity}', "finite"),
        ('{"a": -Infinity}', "finite"),
        ('{"a": 1e400}', "finite"),
        ('{"a": [1, {"b": 1e999}]}', "finite"),
        ('{"a": "x\\u0000y"}', "NUL"),
        ('{"k\\u0000": 1}', "NUL"),
        ('{"a": [{"b": "\\u0000"}]}', "NUL"),
        ('{"a": ' + "1" * 4400 + "}", "ValueError"),
        ('{"a": ' + _DEEP + "}", "RecursionError"),
    ],
    ids=["nan", "inf", "-inf", "1e400", "nested-inf", "nul-value", "nul-key", "nested-nul",
         "huge-int", "deep"],
)
def test_ingest_stdin_metadata_postgres_would_reject_exits_2(
    offline_cli: Path, raw: str, expected: str
) -> None:
    result = _stdin(offline_cli, "--metadata", raw)
    assert result.exit_code == 2, result.output[-500:]
    assert "--metadata" in result.output
    assert expected in result.output
    assert "stdin was empty" not in result.output
    _assert_no_traceback(result)


def test_ingest_stdin_metadata_json_error_keeps_its_position(offline_cli: Path) -> None:
    result = _stdin(offline_cli, "--metadata", '{"a": 1,}')
    assert result.exit_code == 2, result.output
    assert "not valid JSON" in result.output
    assert "line 1, column" in result.output


# Item 3: the parser never raises on an out-of-range offset.


@pytest.mark.parametrize("raw", ["0001-01-01T00:00:00+05:00", "9999-12-31T23:00:00-05:00"])
def test_parse_sent_at_returns_none_on_overflow(raw: str) -> None:
    from brain.ingest.sent_at import parse_sent_at

    assert parse_sent_at(raw) is None


# Item 4: a zip-shaped DOCX that is not a document is one line too.


@pytest.mark.parametrize(
    "members",
    [{"word/document.xml": "<x/>"}, {"[Content_Types].xml": "<not xml"}],
    ids=["no-content-types", "bad-xml"],
)
def test_ingest_malformed_docx_one_line(offline_cli: Path, members: dict[str, str]) -> None:
    import zipfile

    target = offline_cli / "bad.docx"
    with zipfile.ZipFile(target, "w") as archive:
        for name, body in members.items():
            archive.writestr(name, body)
    result = CliRunner().invoke(app, ["ingest", str(target), "--no-enrich"])
    assert result.exit_code == 1, result.output
    assert "cannot read bad.docx: ValueError" in result.output
    _assert_no_traceback(result)


# Item 5: every spelling of a bare connection is caught, not only `from .db import connect`.


@pytest.mark.parametrize(
    "source",
    [
        "from .db import connect\n",
        "from brain.db import connect\n",
        "from .db import connect_age\n",
        "from . import db\n\ndef f(u):\n    return db.connect(u)\n",
        "from brain import db as d\n\ndef f(u):\n    return d.connect_age(u)\n",
        "import brain.db\n\ndef f(u):\n    return brain.db.connect(u)\n",
        "import brain.db as bdb\n\ndef f(u):\n    return bdb.connect(u)\n",
        "import psycopg\n\ndef f(u):\n    return psycopg.connect(u)\n",
        "import psycopg as pg\n\ndef f(u):\n    return pg.connect(u)\n",
        "from psycopg import connect\n",
    ],
)
def test_bare_connect_guard_catches_every_spelling(tmp_path: Path, source: str) -> None:
    module = tmp_path / "cli_ingest_synthetic.py"
    module.write_text(source, encoding="utf-8")
    assert _imports_bare_connect(module)


@pytest.mark.parametrize(
    "source",
    [
        "from .db import connect_raw\n",
        "from .cli_ingest_boundary import connect_or_exit\n",
        "import psycopg\n\ndef f(e):\n    return isinstance(e, psycopg.Error)\n",
    ],
)
def test_bare_connect_guard_allows_the_sanctioned_paths(tmp_path: Path, source: str) -> None:
    module = tmp_path / "cli_ingest_synthetic.py"
    module.write_text(source, encoding="utf-8")
    assert not _imports_bare_connect(module)


# rev-137b #2: a lone surrogate is refused like a NUL (Postgres cannot store either).


@pytest.mark.parametrize(
    "raw",
    ['{"a": "x\\ud800y"}', '{"k\\udfff": 1}', '{"a": [{"b": "\\udc00"}]}'],
    ids=["surrogate-value", "surrogate-key", "nested-surrogate"],
)
def test_ingest_stdin_metadata_surrogate_exits_2(offline_cli: Path, raw: str) -> None:
    result = _stdin(offline_cli, "--metadata", raw)
    assert result.exit_code == 2, result.output[-500:]
    assert "--metadata" in result.output
    assert "UTF-8" in result.output
    assert "stdin was empty" not in result.output
    _assert_no_traceback(result)


@pytest.mark.parametrize("value", ["inf", "-inf", 1e300, 99999999999, -99999999999, "NaN"])
def test_promote_skips_a_duration_outside_int4(value: object) -> None:
    from brain.ingest import _promote_metadata_to_columns

    assert "duration_min" not in _promote_metadata_to_columns({"duration_min": value})


@pytest.mark.parametrize("value", [0, 42, 42.7, "42.7", 2**31 - 1, -(2**31)])
def test_promote_keeps_a_duration_inside_int4(value: object) -> None:
    from brain.ingest import _promote_metadata_to_columns

    assert _promote_metadata_to_columns({"duration_min": value})["duration_min"] == int(
        float(value)  # type: ignore[arg-type]
    )


# ---------------------------------------------------------------------------
# Decision after rev-137c: the unreachable-DB line names no address at all. Four review
# rounds of libpq-parsing edge cases showed that printing host:port safely means copying
# libpq's parser; `brain doctor` shows the target instead.
# ---------------------------------------------------------------------------

_ANY_DSN = [
    # Every DSN from the four review rounds' leak lists ...
    "postgresql://u:Pa@ss/word@127.0.0.1:1/x_test",
    "postgresql://u:Pa@1234/x@127.0.0.1/x_test",
    "postgresql://u:Pa@ss:5432/x@127.0.0.1/x_test",
    "postgresql://synthetic_user:20261119/x@localhost:5434/x_test",
    "postgresql://u:QZJ?x@127.0.0.1:1/x_test",
    "postgresql://u:QZJ#x@127.0.0.1:1/x_test",
    "postgres://u:Q@ZJ@127.0.0.1:1/x_test",
    "postgresql://127.0.0.1:1/x_test?password=a&host=QZJ",
    "postgresql://127.0.0.1:1/x_test?password=a&port=4242",
    "postgresql://127.0.0.1:1/x_test?password=#&port=4242",
    "postgresql://127.0.0.1:1/x_test?password=#&host=QZJ",
    "postgresql://u:pw@127.0.0.1:1/x_test?password=a&ho%73t=QZJ",
    "postgresql://u:pw@127.0.0.1:1/x_test?password=a&po%72t=4242",
    "postgresql://u:pw@127.0.0.1:1/x_test?password=a&hostaddr=10.0.0.9",
    "postgresql://u:pw@127.0.0.1:1,QZJ:2/x_test",
    "POSTGRESQL://u:Pa@ss/x@127.0.0.1/x_test",
    "postgresql://u:p@127.0.0.1:0/x_test",
    "postgresql://u:p@127.0.0.1:70000/x_test",
    "postgresql://u:p@127.0.0.1:QZJ/x_test",
    "host=127.0.0.1 port=1 password=x host=QZJ",
    "host=127.0.0.1 port=1 password=x port=4242",
    "port=1 password=x host=QZJ",
    "host=127.0.0.1 password=x port=4242",
    "host=127.0.0.1 port=0",
    "host=127.0.0.1 port=1 password=x\thost=QZJ",
    "host=127.0.0.1 port=1 password=x\x0bhost=QZJ",
    "host=127.0.0.1 port=1 password=x\nport=4242",
    "password=x host=QZJ",
    "host=127.0.0.1 port=70000",
    # ... plus a clean URI, a clean key=value DSN and one with no host at all.
    f"postgresql://{SYNTHETIC_USER}:{SYNTHETIC_PASSWORD}@127.0.0.1:1/{SYNTHETIC_DB}",
    f"host=127.0.0.1 port=1 user={SYNTHETIC_USER} password={SYNTHETIC_PASSWORD}"
    f" dbname={SYNTHETIC_DB}",
    f"dbname={SYNTHETIC_DB} user={SYNTHETIC_USER}",
]


def _raise_error(error: type[Exception], database_url: str) -> Exception:
    """The failure ``connect_raw`` raises, its message quoting the whole DSN."""
    if issubclass(error, UnicodeEncodeError):
        # Raised before libpq for a DSN that cannot be encoded (a lone surrogate).
        return error("utf-8", database_url, 0, 1, f"synthetic failure for {database_url}")
    return error(f"synthetic failure for {database_url}")


class _RecordingConnect:
    """A ``connect_raw`` that never connects: it records the DSN and raises ``error``.

    The error's MESSAGE quotes the whole DSN, as psycopg's can quote host, port, user and
    dbname. The printed line must not depend on it.
    """

    def __init__(self, error: type[Exception]) -> None:
        self.error = error
        self.seen: list[str] = []

    def __call__(self, database_url: str, **_: object) -> psycopg.Connection:
        self.seen.append(database_url)
        raise _raise_error(self.error, database_url)


@pytest.mark.parametrize(
    "error", [psycopg.OperationalError, psycopg.ProgrammingError, UnicodeEncodeError]
)
@pytest.mark.parametrize("dsn", _ANY_DSN)
def test_unreachable_db_line_is_fixed_text_whatever_the_dsn(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    dsn: str,
    error: type[Exception],
) -> None:
    from brain import cli_ingest_boundary

    fake = _RecordingConnect(error)
    monkeypatch.setattr(cli_ingest_boundary, "connect_raw", fake)
    with (
        pytest.raises(typer.Exit) as excinfo,
        cli_ingest_boundary.connect_or_exit(dsn),
    ):
        pass  # pragma: no cover - the fake refuses first
    assert fake.seen == [dsn]  # the fake, not a real server, received the DSN
    assert excinfo.value.exit_code == 1
    expected = (
        f"cannot reach the database ({error.__name__}). Is Postgres running? Try: brain doctor\n"
    )
    assert capsys.readouterr().err == expected



# Audit after 8d8a970f, item 1: ingest-gmail's per-thread failure line names the error
# class for a psycopg / ValueError / KeyError failure, never its text (a psycopg DETAIL can
# quote row values; a parser error can quote the message body).

_GMAIL_MARKER = "SYNTHETIC-MARKER-QZJ-0137"


class _OfflineConnection:
    """What ``connect_raw`` returns here: no server behind it, and no row is stored."""

    autocommit = False

    def execute(self, *_: object, **__: object) -> _OfflineConnection:
        return self

    def fetchone(self) -> None:
        return None

    def close(self) -> None:
        pass


@pytest.mark.parametrize(
    "error",
    [
        psycopg.errors.DataError(f"bad row {_GMAIL_MARKER}"),
        ValueError(f"malformed {_GMAIL_MARKER}"),
        KeyError(_GMAIL_MARKER),
    ],
    ids=["psycopg", "value", "key"],
)
def test_ingest_gmail_failed_thread_line_names_the_class_not_the_text(
    offline_cli: Path, monkeypatch: pytest.MonkeyPatch, error: Exception
) -> None:
    from tests.test_cli_ingest_gmail import _fake_runner, _msg

    monkeypatch.setattr(
        "brain.cli_ingest_boundary.connect_raw", lambda *_a, **_k: _OfflineConnection()
    )
    msgs = {"m1": _msg(id="m1", subject="Hi", from_addr="n@example.com", body="Hello")}
    monkeypatch.setattr("brain.ingest.gmail._run", _fake_runner(msgs))

    def fail(*_a: object, **_k: object) -> None:
        raise error

    monkeypatch.setattr("brain.cli_ingest.ingest_document", fail)
    result = CliRunner().invoke(app, ["ingest-gmail", "--label", "synthetic", "--no-enrich"])
    assert result.exit_code == 0, result.output
    assert f"failed thread tm1 (1 messages): {type(error).__name__}" in result.output
    assert _GMAIL_MARKER not in result.output
    _assert_no_traceback(result)
