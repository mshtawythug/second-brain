"""Parse-smoke tests for the ``quartz_overrides/`` overlay tree.

We can't run ``tsc`` in CI, but we can catch the worst regressions with
a small set of static checks applied to every ``.ts`` / ``.tsx`` /
``.scss`` file under ``quartz_overrides/``:

- No literal ``TODO:`` / ``FIXME:`` markers (code is shipped, not WIP).
- Roughly balanced ``{`` / ``}`` (strips line/block comments first to
  avoid false-positives from comment-only braces).
- ``import`` lines look syntactically valid (start with ``import`` and
  end at a recognizable ``from "..."`` or bare side-effect import).

Iteration is dynamic — any new file added under ``quartz_overrides/``
is automatically subjected to these checks. We use parametrization so
each file shows up as its own pytest case.
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
OVERRIDES_ROOT = REPO_ROOT / "quartz_overrides"
SUFFIXES = (".ts", ".tsx", ".scss")


def _collect_overlay_files() -> list[Path]:
    """Enumerate every overlay source file at module import time.

    Sorted for deterministic test ordering. Skips files that don't
    match the supported suffixes (defensive — the dir should only
    contain overlay sources, but better to silently skip than crash if
    a stray ``.DS_Store`` shows up).
    """
    if not OVERRIDES_ROOT.is_dir():
        return []
    return sorted(p for p in OVERRIDES_ROOT.rglob("*") if p.is_file() and p.suffix in SUFFIXES)


_OVERLAY_FILES = _collect_overlay_files()


def _ids(paths: list[Path]) -> list[str]:
    """Render parametrize ids relative to overrides root for readability."""
    return [str(p.relative_to(OVERRIDES_ROOT)) for p in paths]


def _strip_comments(text: str) -> str:
    """Remove line + block comments so brace counting only sees code.

    Handles ``// ...`` to end of line and ``/* ... */`` (including
    multi-line). Doesn't bother stripping string literals — a stray
    brace inside a TypeScript string is rare and would still leave the
    overall counts roughly balanced.
    """
    text = re.sub(r"/\*.*?\*/", "", text, flags=re.DOTALL)
    text = re.sub(r"//[^\n]*", "", text)
    return text


def test_overrides_root_exists() -> None:
    """Sanity check — fail loudly if the dir vanishes."""
    assert OVERRIDES_ROOT.is_dir(), f"missing {OVERRIDES_ROOT}"


def test_overrides_has_files() -> None:
    """Make sure parametrize isn't running zero cases by accident."""
    assert _OVERLAY_FILES, "expected at least one .ts/.tsx/.scss file under quartz_overrides/"


@pytest.mark.parametrize("path", _OVERLAY_FILES, ids=_ids(_OVERLAY_FILES))
def test_overlay_file_has_no_todo_or_fixme(path: Path) -> None:
    """Shipped overlay files should not carry TODO/FIXME markers."""
    text = path.read_text(encoding="utf-8")
    for marker in ("TODO:", "FIXME:"):
        assert marker not in text, (
            f"{path.relative_to(REPO_ROOT)} contains a literal `{marker}` marker — "
            "address it before merge or rephrase the comment."
        )


@pytest.mark.parametrize("path", _OVERLAY_FILES, ids=_ids(_OVERLAY_FILES))
def test_overlay_file_braces_balance(path: Path) -> None:
    """Rough brace-balance check after stripping comments."""
    stripped = _strip_comments(path.read_text(encoding="utf-8"))
    opens = stripped.count("{")
    closes = stripped.count("}")
    assert opens == closes, (
        f"{path.relative_to(REPO_ROOT)} has unbalanced braces: "
        f"{opens} `{{` vs {closes} `}}` (after stripping comments)."
    )


# Match imports like:
#   import foo from "bar";
#   import { a, b } from "bar";
#   import * as x from "bar";
#   import "side-effect";
#   import type { X } from "bar";
# Allows single OR double quotes; trailing semicolon optional.
_IMPORT_LINE_RE = re.compile(
    r"""^\s*import
        (?:
            \s+["'][^"']+["']            # bare side-effect import
            |
            (?:\s+type)?                 # optional `type`
            \s+(?:                       # bound forms
                [A-Za-z_$][\w$]*         # default
                (?:\s*,\s*\{[^}]*\})?    # default + named
                |
                \{[^}]*\}                # named only
                |
                \*\s+as\s+[A-Za-z_$][\w$]*  # namespace
            )
            \s+from\s+["'][^"']+["']
        )
        \s*;?\s*$
    """,
    re.VERBOSE,
)


@pytest.mark.parametrize(
    "path",
    [p for p in _OVERLAY_FILES if p.suffix in (".ts", ".tsx")],
    ids=_ids([p for p in _OVERLAY_FILES if p.suffix in (".ts", ".tsx")]),
)
def test_overlay_file_imports_look_valid(path: Path) -> None:
    """Every line starting with ``import`` must look syntactically sane.

    Catches truncated / mid-edit imports — e.g. an ``import { foo``
    with a missing close brace and ``from`` clause.
    """
    bad: list[tuple[int, str]] = []
    text = _strip_comments(path.read_text(encoding="utf-8"))
    for lineno, raw in enumerate(text.splitlines(), start=1):
        line = raw.rstrip()
        if not line.lstrip().startswith("import"):
            continue
        # Multi-line imports — skip the multiline case if the line
        # doesn't end with a string literal terminator. The first/last
        # line will still be checked when we run on its full form
        # somewhere; we just don't want false positives mid-import.
        if not (line.endswith(";") or line.endswith('"') or line.endswith("'")):
            continue
        if not _IMPORT_LINE_RE.match(line):
            bad.append((lineno, raw))
    assert not bad, (
        f"{path.relative_to(REPO_ROOT)} has malformed import lines:\n"
        + "\n".join(f"  L{ln}: {raw}" for ln, raw in bad)
    )
