"""Fail when CLAUDE.md's file-size ceiling table and the source tree disagree.

The ceiling table in ``CLAUDE.md`` ("File-size ceiling — 800 lines") lists every
module under ``src/brain/`` that is over 800 lines. Its digits were made
rot-proof long ago; its *row set* was not, because nothing noticed a file that
crossed 800 without getting a row (``related.py`` did exactly that), or a row
that outlived its file. This script is that check (roadmap RM-04, backlog BL-1).
It runs in ``ci.yml``'s ``lint`` job and in ``bin/brain-ci``'s lint stage.

The row set is parsed from the one table whose header row is exactly ``HEADER``:
each data row's first cell is a backticked path relative to ``src/brain/``, and
its Lines cell carries ``wc -l src/brain/<the same path>``. The header is the
anchor because it is unique in the file; a heading-text anchor would break on
the next rewording, and a missing header exits 2 rather than passing.

Exit codes: 0 the table and the tree agree; 1 they disagree (every problem is
named, with its remedy); 2 ``CLAUDE.md``, the table header or ``src/brain/`` was
not found, or the header line appears more than once.

It does NOT check what a row says (the trail, the recorded reasons) — that is a
review — nor the near-ceiling budgets in the roadmap spec. Stdlib only, so the
lint job needs nothing beyond its checkout.
"""
import argparse
import re
import sys
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path

CEILING = 800
HEADER = "| File | Lines | Why it is not being split |"

# Separator row under the header, e.g. `|---|---|---|` or `| :-- | --- |`.
SEPARATOR_RE = re.compile(r"^\|(?:\s*:?-+:?\s*\|)+\s*$")
# A row's first two cells. Only these are read, so a `|` in the prose of the
# Why cell cannot shift them; the Lines cell never contains a bare `|`.
ROW_RE = re.compile(r"^\|(?P<first>[^|]*)\|(?P<lines>[^|]*)\|")
# The first cell: exactly one backticked path and nothing else.
PATH_CELL_RE = re.compile(r"^\s*`(?P<path>[^`\s]+)`\s*$")
# The Lines cell's measuring command.
WC_RE = re.compile(r"`wc -l src/brain/(?P<path>[^`\s]+)`")

REPO_ROOT = Path(__file__).resolve().parent.parent

ADD_ROW = (
    "add a row in the CLAUDE.md trail form (re-derive command in the Lines cell, a "
    "trail and the recorded reason in the Why cell), or extract until it is back "
    "under the ceiling"
)
REMOVE_ROW = (
    "remove the row and add a 'deliberately absent' note below the table, as "
    "backup/restore.py and related.py have"
)
REMOVE_GONE_ROW = "remove the row (a deleted file has no size to record)"


class TableNotFound(Exception):
    """CLAUDE.md has no line equal to ``HEADER``, or more than one."""


@dataclass(frozen=True)
class TableRow:
    """One data row: the first cell (backticks stripped when well-formed) and the Lines cell."""

    path: str
    lines_cell: str
    path_cell_ok: bool = True
    has_lines_cell: bool = True


@dataclass(frozen=True)
class MissingRow:
    """A file over the ceiling that the table does not list."""

    path: str
    lines: int


@dataclass(frozen=True)
class Mismatch:
    """A row that names its file badly, so it cannot be trusted as a record."""

    path: str
    reason: str


@dataclass(frozen=True)
class CeilingReport:
    """Every disagreement between the table and the tree."""

    rows: int
    over: int
    missing_rows: list[MissingRow] = field(default_factory=list)
    stale_rows: list[str] = field(default_factory=list)
    mismatched_rows: list[Mismatch] = field(default_factory=list)


def count_lines(path: Path) -> int:
    """Newline count — the number ``wc -l`` prints, which the table's commands use."""
    return path.read_bytes().count(b"\n")


def over_ceiling(src_root: Path) -> dict[str, int]:
    """Every ``*.py`` under ``src_root`` over ``CEILING`` lines, keyed by POSIX rel path."""
    over: dict[str, int] = {}
    for path in sorted(src_root.rglob("*.py")):
        if not path.is_file():
            continue
        lines = count_lines(path)
        if lines > CEILING:
            over[path.relative_to(src_root).as_posix()] = lines
    return over


def table_rows(claude_md: str) -> list[TableRow]:
    """The ceiling table's data rows, in order. Raises ``TableNotFound`` without the header."""
    lines = claude_md.splitlines()
    anchors = [i for i, line in enumerate(lines) if line.rstrip() == HEADER]
    if len(anchors) != 1:
        # Two tables under one header would make "which row set?" a guess.
        raise TableNotFound(f"{len(anchors)} lines equal to {HEADER!r}; expected exactly 1")
    start = anchors[0]

    rows: list[TableRow] = []
    for line in lines[start + 1 :]:
        if not line.startswith("|"):
            break
        if SEPARATOR_RE.match(line):
            continue
        match = ROW_RE.match(line)
        if match is None:
            # Fewer than two cell separators: there is no Lines cell to read.
            lone = PATH_CELL_RE.match(line[1:].rstrip().removesuffix("|"))
            rows.append(
                TableRow(
                    path=lone.group("path") if lone else line.strip(),
                    lines_cell="",
                    path_cell_ok=lone is not None,
                    has_lines_cell=False,
                )
            )
            continue
        first = match.group("first")
        path_match = PATH_CELL_RE.match(first)
        if path_match is None:
            rows.append(
                TableRow(path=first.strip(), lines_cell=match.group("lines"), path_cell_ok=False)
            )
        else:
            rows.append(TableRow(path=path_match.group("path"), lines_cell=match.group("lines")))
    return rows


def _row_mismatch(row: TableRow) -> str | None:
    if not row.has_lines_cell:
        return "its row has no Lines cell"
    if not row.path_cell_ok:
        return "its first cell must be one backticked path relative to src/brain/"
    measured = WC_RE.findall(row.lines_cell)
    if not measured:
        return f"its Lines cell must hold `wc -l src/brain/{row.path}`"
    if any(other != row.path for other in measured):
        named = ", ".join(f"wc -l src/brain/{other}" for other in measured)
        return (
            f"its Lines cell measures another file ({named}); "
            f"it must be `wc -l src/brain/{row.path}`"
        )
    return None


def compare(over: Mapping[str, int], rows: Sequence[TableRow]) -> CeilingReport:
    """Diff the over-ceiling set against the table's rows. Pure."""
    seen: set[str] = set()
    stale: list[str] = []
    mismatched: list[Mismatch] = []
    for row in rows:
        reason = _row_mismatch(row)
        if reason is None and row.path in seen:
            reason = "it is a duplicate row; each file gets one"
        if reason is not None:
            mismatched.append(Mismatch(path=row.path, reason=reason))
        if row.path_cell_ok:
            if row.path not in over and row.path not in seen:
                stale.append(row.path)
            seen.add(row.path)
    missing = [
        MissingRow(path=path, lines=lines)
        for path, lines in sorted(over.items())
        if path not in seen
    ]
    return CeilingReport(
        rows=len(rows),
        over=len(over),
        missing_rows=missing,
        stale_rows=stale,
        mismatched_rows=mismatched,
    )


def has_problems(report: CeilingReport) -> bool:
    return bool(report.missing_rows or report.stale_rows or report.mismatched_rows)


def format_report(report: CeilingReport, src_root: Path) -> list[str]:
    """One message per problem, naming the file and the remedy.

    Reads the tree only to say WHY a row is stale: the file is gone, or how
    many lines it has now.
    """
    messages: list[str] = []
    for missing in report.missing_rows:
        messages.append(
            f"src/brain/{missing.path}: {missing.lines} lines (> {CEILING}) and no row in "
            f"CLAUDE.md's ceiling table. Remedy: {ADD_ROW}."
        )
    for path in report.stale_rows:
        target = src_root / path
        if not target.is_file():
            messages.append(
                f"row `{path}`: src/brain/{path} no longer exists. Remedy: {REMOVE_GONE_ROW}."
            )
        else:
            messages.append(
                f"row `{path}`: src/brain/{path} has {count_lines(target)} lines, at or "
                f"under the {CEILING}-line ceiling. Remedy: {REMOVE_ROW}."
            )
    for mismatch in report.mismatched_rows:
        messages.append(f"row `{mismatch.path}`: {mismatch.reason}. Remedy: fix the row.")
    return messages


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--repo-root",
        type=Path,
        default=REPO_ROOT,
        help="repository root holding CLAUDE.md and src/brain/ (default: this checkout)",
    )
    args = parser.parse_args(argv)
    claude_md = args.repo_root / "CLAUDE.md"
    src_root = args.repo_root / "src" / "brain"

    if not claude_md.is_file():
        print(f"check_ceiling_table: no CLAUDE.md at {claude_md}", file=sys.stderr)
        return 2
    if not src_root.is_dir():
        print(f"check_ceiling_table: no src/brain directory at {src_root}", file=sys.stderr)
        return 2
    try:
        rows = table_rows(claude_md.read_text(encoding="utf-8"))
    except TableNotFound as exc:
        print(
            f"check_ceiling_table: cannot find CLAUDE.md's ceiling table ({exc}). The check "
            f"anchors on exactly one line that is:\n  {HEADER}\nIf the header was reworded, "
            "update HEADER in scripts/check_ceiling_table.py in the same change.",
            file=sys.stderr,
        )
        return 2

    report = compare(over_ceiling(src_root), rows)
    if has_problems(report):
        print(
            f"check_ceiling_table: CLAUDE.md's ceiling table and src/brain/ disagree "
            f"({report.over} files over {CEILING} lines, {report.rows} rows):",
            file=sys.stderr,
        )
        for message in format_report(report, src_root):
            print(f"  - {message}", file=sys.stderr)
        return 1

    print(
        f"check_ceiling_table: the ceiling table and src/brain/ agree "
        f"({report.over} files over {CEILING} lines, one row each)."
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
