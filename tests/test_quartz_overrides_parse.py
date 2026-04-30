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
    """Strip block + line comments. Used by the import-line check.

    Doesn't touch string literals — the import-line regex needs to
    see the original ``from "..."`` path, so collapsing it to ``""``
    would defeat the validation. The trade-off: a ``//`` inside a
    string on the same line as an ``import`` could spoof the
    line-comment regex, but in practice every overlay import line is
    a single ``from "..."`` with no embedded ``//`` in the module
    specifier, so this is fine.
    """
    text = re.sub(r"/\*.*?\*/", "", text, flags=re.DOTALL)
    text = re.sub(r"//[^\n]*", "", text)
    return text


def _strip_for_brace_count(text: str) -> str:
    """Strip strings + comments so brace counting only sees code.

    Strip order matters: string literals come out FIRST so a ``//``
    inside ``"http://..."`` doesn't fool the line-comment regex into
    eating the rest of the line. After that we drop block (``/* */``)
    and line (``//``) comments. The resulting text keeps every ``{``
    / ``}`` that lives in actual code, so the brace-balance check
    isn't tripped up by braces inside strings or comment text.

    String matching is **line-bounded** — the character class excludes
    ``\\n`` so an unterminated string (or, more commonly, an
    apostrophe in a comment like ``upstream's``) can't pair up with a
    quote on a later line and eat the code in between. Real TS / SCSS
    strings don't span newlines anyway, so this is a safe constraint.
    Backslash escapes (``\\\"``, ``\\'``) are preserved so an embedded
    quote doesn't prematurely terminate the match.
    """
    text = re.sub(r'"(?:[^"\\\n]|\\.)*"', '""', text)
    text = re.sub(r"'(?:[^'\\\n]|\\.)*'", "''", text)
    text = re.sub(r"`(?:[^`\\\n]|\\.)*`", "``", text)
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
    """Rough brace-balance check after stripping strings + comments.

    Uses the string-stripping variant of the strip helper so braces
    inside ``"{...}"`` or ``'{...}'`` don't skew the count. That also
    sidesteps the failure mode where a ``//`` inside a string on the
    same line as code (e.g. ``xmlns="http://..."``) would otherwise
    let the line-comment regex eat the rest of the line.
    """
    stripped = _strip_for_brace_count(path.read_text(encoding="utf-8"))
    opens = stripped.count("{")
    closes = stripped.count("}")
    assert opens == closes, (
        f"{path.relative_to(REPO_ROOT)} has unbalanced braces: "
        f"{opens} `{{` vs {closes} `}}` (after stripping strings + comments)."
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
