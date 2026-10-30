"""Tests for ``scripts/check_ceiling_table.py`` — the ceiling-table CI check (RM-04).

The script lives outside ``src/brain``, so it is loaded with :mod:`importlib.util`
(the pattern ``tests/test_embedding_smoke.py`` uses) and its pure functions and
``main(argv)`` are driven directly on ``tmp_path`` trees and synthetic markdown.

A check that silently finds nothing looks identical to a passing gate, so most
tests here build a tree that MUST fail and assert that it does, and name what.
"""
import importlib.util
import re
import shutil
import subprocess
import sys
from pathlib import Path
from types import ModuleType

import pytest

# Reads files and shells out to `wc`; never connects. The marker lets a run of
# only this module skip the test-database suite lock (tests/conftest.py).
pytestmark = pytest.mark.nodb

REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPT_PATH = REPO_ROOT / "scripts" / "check_ceiling_table.py"
# The table moved out of CLAUDE.md on 2026-10-29; CLAUDE.md keeps only the rule.
REAL_TABLE_DOC = REPO_ROOT / "docs" / "ceiling-table.md"
REAL_CLAUDE_MD = REPO_ROOT / "CLAUDE.md"
TABLE_DOC_REL = Path("docs") / "ceiling-table.md"
REAL_SRC_ROOT = REPO_ROOT / "src" / "brain"

HEADER = "| File | Lines | Why it is not being split |"
SEPARATOR = "|---|---|---|"


def _load() -> ModuleType:
    spec = importlib.util.spec_from_file_location("check_ceiling_table", SCRIPT_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    # Registered before exec so dataclass machinery can resolve the module.
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def ceiling() -> ModuleType:
    return _load()


def _row(path: str, lines_path: str | None = None, why: str = "One algorithm.") -> str:
    named = path if lines_path is None else lines_path
    return f"| `{path}` | **re-derive: `wc -l src/brain/{named}`** | {why} |"


def _table_doc(*rows: str, tail: str = "\nProse after the table.\n") -> str:
    body = "\n".join([HEADER, SEPARATOR, *rows])
    return f"# Ceiling table\n\nSome prose.\n\n{body}\n{tail}"


def _write_py(src_root: Path, rel: str, lines: int) -> Path:
    path = src_root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("x = 1\n" * lines, encoding="utf-8")
    return path


def _write_table_doc(repo: Path, text: str) -> None:
    target = repo / TABLE_DOC_REL
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(text, encoding="utf-8")


def _repo(tmp_path: Path, table_doc: str | None, files: dict[str, int]) -> Path:
    src_root = tmp_path / "src" / "brain"
    src_root.mkdir(parents=True)
    for rel, lines in files.items():
        _write_py(src_root, rel, lines)
    if table_doc is not None:
        _write_table_doc(tmp_path, table_doc)
    return tmp_path


def _run(ceiling: ModuleType, repo: Path) -> int:
    return int(ceiling.main(["--repo-root", str(repo)]))


# ---------------------------------------------------------------------------
# AC1 — the real repository passes.
# ---------------------------------------------------------------------------


def test_real_repo_passes(ceiling: ModuleType, capsys: pytest.CaptureFixture[str]) -> None:
    status = _run(ceiling, REPO_ROOT)

    out, err = capsys.readouterr()
    assert status == 0, f"the ceiling table and the tree disagree on this branch:\n{err}"
    assert "agree" in out


def test_script_runs_as_a_subprocess_on_the_real_repo() -> None:
    """The exact command ci.yml runs, from the repo root, exits 0."""
    result = subprocess.run(
        [sys.executable, "scripts/check_ceiling_table.py"],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )

    assert result.returncode == 0, result.stdout + result.stderr


def test_parser_reads_every_real_row(ceiling: ModuleType) -> None:
    """Every real row parses, and each Lines cell names its own path.

    The expected count is re-derived by an independent line scan, not
    written down, because a digit here would rot like the table's own did.
    """
    text = REAL_TABLE_DOC.read_text(encoding="utf-8")
    lines = text.splitlines()
    start = lines.index(HEADER)
    independent = []
    for line in lines[start + 2 :]:
        if not line.startswith("|"):
            break
        independent.append(re.match(r"\| `([^`]+)` \|", line).group(1))  # type: ignore[union-attr]

    rows = ceiling.table_rows(text)

    assert [row.path for row in rows] == independent
    assert len(rows) > 0
    for row in rows:
        assert f"`wc -l src/brain/{row.path}`" in row.lines_cell


def test_real_row_set_equals_real_over_ceiling_set(ceiling: ModuleType) -> None:
    rows = ceiling.table_rows(REAL_TABLE_DOC.read_text(encoding="utf-8"))
    over = ceiling.over_ceiling(REAL_SRC_ROOT)

    assert {row.path for row in rows} == set(over)


# ---------------------------------------------------------------------------
# AC2 / AC3 — a file over the ceiling with no row; the boundary is "> 800".
# ---------------------------------------------------------------------------


def test_801_line_file_without_row_fails_and_names_it(
    ceiling: ModuleType, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    repo = _repo(tmp_path, _table_doc(_row("big.py")), {"big.py": 900, "new/grown.py": 801})

    status = _run(ceiling, repo)

    err = capsys.readouterr().err
    assert status == 1
    assert "new/grown.py" in err
    assert "801" in err
    assert "add a row" in err.lower()
    assert "big.py" not in err.replace("new/grown.py", "")


def test_800_line_file_is_not_over(
    ceiling: ModuleType, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    repo = _repo(tmp_path, _table_doc(_row("big.py")), {"big.py": 801, "edge.py": 800})

    assert "edge.py" not in ceiling.over_ceiling(repo / "src" / "brain")
    assert _run(ceiling, repo) == 0
    capsys.readouterr()


def test_row_for_800_line_file_is_stale(
    ceiling: ModuleType, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    repo = _repo(tmp_path, _table_doc(_row("edge.py")), {"edge.py": 800})

    status = _run(ceiling, repo)

    assert status == 1
    assert "edge.py" in capsys.readouterr().err


# ---------------------------------------------------------------------------
# AC4 — a row whose file is gone, or no longer over.
# ---------------------------------------------------------------------------


def test_row_for_deleted_file_fails(
    ceiling: ModuleType, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    repo = _repo(
        tmp_path, _table_doc(_row("big.py"), _row("wiki/gone.py")), {"big.py": 900}
    )

    status = _run(ceiling, repo)

    err = capsys.readouterr().err
    assert status == 1
    assert "wiki/gone.py" in err
    assert "no longer exists" in err
    assert "remove the row" in err.lower()


def test_row_for_799_line_file_fails(
    ceiling: ModuleType, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    repo = _repo(
        tmp_path, _table_doc(_row("big.py"), _row("shrunk.py")), {"big.py": 900, "shrunk.py": 799}
    )

    status = _run(ceiling, repo)

    err = capsys.readouterr().err
    assert status == 1
    assert "shrunk.py" in err
    assert "799" in err
    assert "deliberately absent" in err


# ---------------------------------------------------------------------------
# AC5 — mismatched rows: a copied Lines cell, and other malformed rows.
# ---------------------------------------------------------------------------


def test_lines_cell_naming_another_path_fails(
    ceiling: ModuleType, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    repo = _repo(
        tmp_path,
        _table_doc(_row("big.py"), _row("other.py", lines_path="big.py")),
        {"big.py": 900, "other.py": 900},
    )

    status = _run(ceiling, repo)

    err = capsys.readouterr().err
    assert status == 1
    assert "other.py" in err
    assert "wc -l src/brain/big.py" in err


def test_lines_cell_without_wc_command_is_mismatched(ceiling: ModuleType) -> None:
    rows = ceiling.table_rows(_table_doc("| `big.py` | 900 | One algorithm. |"))

    report = ceiling.compare({"big.py": 900}, rows)

    assert [m.path for m in report.mismatched_rows] == ["big.py"]
    assert not report.missing_rows and not report.stale_rows


def test_first_cell_without_backticks_is_mismatched(ceiling: ModuleType) -> None:
    rows = ceiling.table_rows(
        _table_doc("| big.py | **re-derive: `wc -l src/brain/big.py`** | Why. |")
    )

    report = ceiling.compare({"big.py": 900}, rows)

    assert report.mismatched_rows, report
    assert "backticked" in report.mismatched_rows[0].reason


def test_row_without_lines_cell_is_mismatched(ceiling: ModuleType) -> None:
    """A table line with fewer than two cell separators has no Lines cell."""
    rows = ceiling.table_rows(_table_doc("| `big.py`"))

    report = ceiling.compare({"big.py": 900}, rows)

    assert [row.path for row in rows] == ["big.py"]
    assert [(m.path, m.reason) for m in report.mismatched_rows] == [
        ("big.py", "its row has no Lines cell")
    ]
    assert not report.missing_rows and not report.stale_rows


def test_row_without_lines_cell_or_path_is_mismatched(ceiling: ModuleType) -> None:
    rows = ceiling.table_rows(_table_doc("| not a path"))

    report = ceiling.compare({}, rows)

    assert [m.reason for m in report.mismatched_rows] == ["its row has no Lines cell"]


def test_duplicate_row_is_mismatched(ceiling: ModuleType) -> None:
    rows = ceiling.table_rows(_table_doc(_row("big.py"), _row("big.py")))

    report = ceiling.compare({"big.py": 900}, rows)

    assert len(report.mismatched_rows) == 1
    assert "duplicate" in report.mismatched_rows[0].reason


# ---------------------------------------------------------------------------
# AC6 — the anchor is missing: exit 2, never a silent pass.
# ---------------------------------------------------------------------------


def test_claude_md_without_header_exits_2(
    ceiling: ModuleType, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    repo = _repo(tmp_path, "# Ceiling table\n\nNo table at all.\n", {"big.py": 900})

    status = _run(ceiling, repo)

    assert status == 2
    assert HEADER in capsys.readouterr().err


def test_reworded_header_exits_2(
    ceiling: ModuleType, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    text = _table_doc(_row("big.py")).replace(HEADER, "| File | Lines | Why not split |")
    repo = _repo(tmp_path, text, {"big.py": 900})

    assert _run(ceiling, repo) == 2
    capsys.readouterr()


def test_missing_table_doc_exits_2(
    ceiling: ModuleType, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    repo = _repo(tmp_path, None, {"big.py": 900})

    assert _run(ceiling, repo) == 2
    assert "docs/ceiling-table.md" in capsys.readouterr().err


def test_table_in_claude_md_is_not_read(
    ceiling: ModuleType, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Regression (2026-10-29): the table moved to docs/ceiling-table.md.

    A complete table left in CLAUDE.md must not satisfy the check; only the
    table in docs/ceiling-table.md counts, and a row missing there is caught.
    """
    repo = _repo(tmp_path, _table_doc(_row("big.py")), {"big.py": 900, "grown.py": 900})
    (repo / "CLAUDE.md").write_text(
        _table_doc(_row("big.py"), _row("grown.py")), encoding="utf-8"
    )

    status = _run(ceiling, repo)

    err = capsys.readouterr().err
    assert status == 1
    assert "grown.py" in err
    assert "docs/ceiling-table.md" in err


def test_real_claude_md_carries_no_ceiling_table() -> None:
    """CLAUDE.md keeps the rule only; a second copy of the table would drift unchecked."""
    lines = REAL_CLAUDE_MD.read_text(encoding="utf-8").splitlines()

    assert HEADER not in [line.rstrip() for line in lines]
    assert "docs/ceiling-table.md" in REAL_CLAUDE_MD.read_text(encoding="utf-8")


def test_missing_src_root_exits_2(
    ceiling: ModuleType, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _write_table_doc(tmp_path, _table_doc(_row("big.py")))

    assert _run(ceiling, tmp_path) == 2
    assert "src/brain" in capsys.readouterr().err


def test_duplicate_header_line_exits_2(
    ceiling: ModuleType, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    text = _table_doc(_row("big.py")) + "\n" + "\n".join([HEADER, SEPARATOR, _row("x.py")]) + "\n"
    repo = _repo(tmp_path, text, {"big.py": 900})

    assert _run(ceiling, repo) == 2
    assert "expected exactly 1" in capsys.readouterr().err


def test_header_quoted_mid_line_is_not_an_anchor(ceiling: ModuleType) -> None:
    """Prose that quotes the header in backticks (the table doc does) is not a table."""
    text = _table_doc(_row("big.py"), tail=f"\nThe header is `{HEADER}` exactly.\n")

    assert [row.path for row in ceiling.table_rows(text)] == ["big.py"]


def test_table_rows_raises_table_not_found(ceiling: ModuleType) -> None:
    with pytest.raises(ceiling.TableNotFound):
        ceiling.table_rows("no table here\n")


# ---------------------------------------------------------------------------
# AC7 — deleting any one real row turns the check red, and names that file.
# (The git-archive mutation run is reported in the PR; this is its in-suite twin.)
# ---------------------------------------------------------------------------


def _real_row_lines() -> list[str]:
    lines = REAL_TABLE_DOC.read_text(encoding="utf-8").splitlines()
    start = lines.index(HEADER)
    rows = []
    for line in lines[start + 2 :]:
        if not line.startswith("|"):
            break
        rows.append(line)
    return rows


@pytest.mark.parametrize(
    "row_line",
    _real_row_lines(),
    ids=lambda line: re.match(r"\| `([^`]+)`", line).group(1),  # type: ignore[union-attr]
)
def test_deleting_any_real_row_is_caught(ceiling: ModuleType, row_line: str) -> None:
    text = REAL_TABLE_DOC.read_text(encoding="utf-8")
    mutated = text.replace(row_line + "\n", "", 1)
    assert mutated != text, "mutation did not apply"
    deleted = re.match(r"\| `([^`]+)`", row_line).group(1)  # type: ignore[union-attr]

    report = ceiling.compare(ceiling.over_ceiling(REAL_SRC_ROOT), ceiling.table_rows(mutated))

    assert [m.path for m in report.missing_rows] == [deleted]
    assert ceiling.has_problems(report)


# ---------------------------------------------------------------------------
# Parser and counting details.
# ---------------------------------------------------------------------------


def test_parser_stops_at_first_non_table_line(ceiling: ModuleType) -> None:
    text = _table_doc(
        _row("big.py"),
        tail="\nProse.\n\n| `later.py` | **re-derive: `wc -l src/brain/later.py`** | x |\n",
    )

    assert [row.path for row in ceiling.table_rows(text)] == ["big.py"]


def test_parser_ignores_pipes_inside_the_why_cell(ceiling: ModuleType) -> None:
    rows = ceiling.table_rows(_table_doc(_row("big.py", why="a | b | c")))

    assert [row.path for row in rows] == ["big.py"]
    assert "wc -l src/brain/big.py" in rows[0].lines_cell


def test_table_at_end_of_file_is_read(ceiling: ModuleType) -> None:
    """No line after the table: the row loop ends without its break."""
    text = "\n".join(["# Ceiling table", "", HEADER, SEPARATOR, _row("a.py"), _row("b/c.py")])

    assert [row.path for row in ceiling.table_rows(text)] == ["a.py", "b/c.py"]


def test_header_with_trailing_whitespace_still_anchors(ceiling: ModuleType) -> None:
    text = _table_doc(_row("big.py")).replace(HEADER, HEADER + "  ")

    assert [row.path for row in ceiling.table_rows(text)] == ["big.py"]


def test_count_lines_matches_wc_semantics(ceiling: ModuleType, tmp_path: Path) -> None:
    no_final_newline = tmp_path / "a.py"
    no_final_newline.write_bytes(b"one\ntwo\nthree")
    empty = tmp_path / "b.py"
    empty.write_bytes(b"")

    assert ceiling.count_lines(no_final_newline) == 2
    assert ceiling.count_lines(empty) == 0


@pytest.mark.skipif(shutil.which("wc") is None, reason="no wc on PATH")
def test_count_lines_agrees_with_wc_on_a_real_file(ceiling: ModuleType) -> None:
    target = REAL_SRC_ROOT / "cli.py"
    wc = subprocess.run(
        ["wc", "-l", str(target)], capture_output=True, text=True, timeout=30, check=True
    )

    assert ceiling.count_lines(target) == int(wc.stdout.split()[0])


def test_over_ceiling_walks_nested_dirs_and_only_py(ceiling: ModuleType, tmp_path: Path) -> None:
    src_root = tmp_path / "src" / "brain"
    _write_py(src_root, "vault/graph.py", 801)
    _write_py(src_root, "top.py", 1000)
    _write_py(src_root, "small.py", 10)
    (src_root / "data.txt").write_text("x\n" * 2000, encoding="utf-8")
    (src_root / "pkg.py").mkdir()  # a directory rglob("*.py") yields; not a file
    _write_py(src_root, "pkg.py/inner.py", 900)

    over = ceiling.over_ceiling(src_root)

    assert over == {"pkg.py/inner.py": 900, "top.py": 1000, "vault/graph.py": 801}


def test_success_summary_reports_the_count(
    ceiling: ModuleType, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    repo = _repo(
        tmp_path, _table_doc(_row("a.py"), _row("b/c.py")), {"a.py": 801, "b/c.py": 900}
    )

    assert _run(ceiling, repo) == 0
    assert "2 files" in capsys.readouterr().out
