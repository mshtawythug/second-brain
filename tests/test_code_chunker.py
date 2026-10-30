"""Unit tests for the code-aware chunker (RM-42): exact boundaries and the budget guarantee."""
import re

import pytest
import tiktoken

from brain.ingest.code_chunker import (
    CODE_CHUNK_BUDGET_TOKENS,
    MODULE_SYMBOL,
    NO_PATH,
    chunk_code,
    language_of,
)
from brain.ollama_budget import INGEST_CHUNK_OVERLAP_TOKENS, INGEST_CHUNK_TARGET_TOKENS

_HEADER = re.compile(r"^(?P<path>.*):(?P<start>\d+)-(?P<end>\d+) (?P<symbol>.*)$")
_CL100K = tiktoken.get_encoding("cl100k_base")


def char_count(text: str) -> int:
    """One token per character: small fixtures can exceed the budget."""
    return len(text)


def cl100k_count(text: str) -> int:
    """The tokenizer every embedder's ``count_tokens`` uses."""
    return len(_CL100K.encode(text))


def spans(chunks: list) -> list[tuple[int, int, str]]:
    """``(start, end, symbol)`` parsed from each chunk's header line."""
    out = []
    for chunk in chunks:
        match = _HEADER.match(chunk.content.split("\n", 1)[0])
        assert match is not None, chunk.content[:80]
        out.append((int(match["start"]), int(match["end"]), match["symbol"]))
    return out


def assert_bodies_match_source(chunks: list, source: str) -> None:
    """Every chunk's body is exactly the source lines its header names."""
    lines = source.split("\n")
    for chunk, (start, end, _symbol) in zip(chunks, spans(chunks), strict=True):
        body = chunk.content.split("\n", 1)[1]
        assert body == "\n".join(lines[start - 1 : end])


def assert_every_line_once(chunks: list, source: str) -> None:
    """No non-blank source line is lost or repeated across the chunks."""
    lines = source.replace("\r\n", "\n").split("\n")
    covered = [n for start, end, _symbol in spans(chunks) for n in range(start, end + 1)]
    non_blank = [n for n, line in enumerate(lines, start=1) if line.strip()]
    assert sorted(set(covered)) == sorted(covered)
    assert set(non_blank) <= set(covered)


def assert_indexes(chunks: list) -> None:
    assert [c.index for c in chunks] == list(range(len(chunks)))


# --- the budget constant ------------------------------------------------------


def test_budget_is_the_prose_chunker_ceiling() -> None:
    assert CODE_CHUNK_BUDGET_TOKENS == INGEST_CHUNK_TARGET_TOKENS + INGEST_CHUNK_OVERLAP_TOKENS


# --- AC1: Python boundaries ---------------------------------------------------

PY_SOURCE = '''\
"""Synthetic module docstring."""
import os
from pathlib import Path

CONSTANT = 1


# A helper comment
@decorator
def helper(x):
    return x + 1


async def fetch():
    return None


class Small:
    """Small class."""

    def method(self):
        return 1


if __name__ == "__main__":
    helper(1)
'''


def test_python_top_level_boundaries_are_exact() -> None:
    chunks = chunk_code(PY_SOURCE, count_tokens=char_count, path="pkg/mod.py")
    assert spans(chunks) == [
        (1, 5, MODULE_SYMBOL),
        (8, 11, "helper"),
        (14, 15, "fetch"),
        (18, 22, "Small"),
        (25, 26, MODULE_SYMBOL),
    ]
    assert_bodies_match_source(chunks, PY_SOURCE)
    assert_every_line_once(chunks, PY_SOURCE)
    assert_indexes(chunks)
    assert chunks[0].content == (
        "pkg/mod.py:1-5 <module>\n"
        '"""Synthetic module docstring."""\n'
        "import os\n"
        "from pathlib import Path\n"
        "\n"
        "CONSTANT = 1"
    )


def _method(name: str) -> list[str]:
    return [f"    def {name}(self):"] + [
        f"        value_{i} = {i} * 1000000" for i in range(12)
    ]


BIG_CLASS = "\n".join(
    [
        "import os",
        "",
        "# About Big.",
        "@dataclass",
        "class Big:",
        '    """A class too large for one chunk."""',
        "",
        "    limit = 3",
        "",
        *_method("alpha"),
        "",
        "    # Beta's note.",
        *_method("beta"),
        "",
    ]
)


def test_python_large_class_is_split_by_method() -> None:
    chunks = chunk_code(BIG_CLASS, count_tokens=char_count, path="big.py")
    assert spans(chunks) == [
        (1, 1, MODULE_SYMBOL),
        (3, 8, "Big"),
        (10, 22, "Big.alpha"),
        (24, 37, "Big.beta"),
    ]
    assert_bodies_match_source(chunks, BIG_CLASS)
    assert_every_line_once(chunks, BIG_CLASS)


def test_python_statement_directly_after_a_definition_is_kept() -> None:
    source = "def a():\n    return 1\nx = a()\nclass B:\n    pass\ny = B()\n"
    chunks = chunk_code(source, count_tokens=char_count, path="tight.py")
    assert spans(chunks) == [
        (1, 2, "a"),
        (3, 3, MODULE_SYMBOL),
        (4, 5, "B"),
        (6, 6, MODULE_SYMBOL),
    ]
    assert_every_line_once(chunks, source)


def test_python_class_that_fits_is_one_chunk() -> None:
    source = "class Tiny:\n    def a(self):\n        return 1\n"
    chunks = chunk_code(source, count_tokens=char_count, path="t.py")
    assert spans(chunks) == [(1, 3, "Tiny")]


def test_python_nested_class_is_named_and_kept_whole() -> None:
    body = [f"        field_{i} = {i} * 1000000" for i in range(30)]
    source = "\n".join(["class Outer:", "    class Inner:", *body, "", *_method("go")])
    chunks = chunk_code(source, count_tokens=char_count, path="n.py")
    symbols = [s for _start, _end, s in spans(chunks)]
    assert symbols[0] == "Outer"
    assert set(symbols[1:-1]) == {"Outer.Inner"}  # over budget: packed by lines
    assert symbols[-1] == "Outer.go"
    assert_bodies_match_source(chunks, source)


HUGE_FUNCTION = "\n".join(["def huge():", *[f"    total_{i} = {i}" for i in range(100)]])


def test_python_huge_function_is_split_by_lines_contiguously() -> None:
    chunks = chunk_code(HUGE_FUNCTION, count_tokens=char_count, path="h.py")
    got = spans(chunks)
    assert len(got) > 1
    assert {s for _a, _b, s in got} == {"huge"}
    assert got[0][0] == 1
    assert got[-1][1] == 101
    for (_s1, e1, _y1), (s2, _e2, _y2) in zip(got, got[1:], strict=False):
        assert s2 == e1 + 1
    assert all(len(c.content) <= CODE_CHUNK_BUDGET_TOKENS for c in chunks)
    assert_bodies_match_source(chunks, HUGE_FUNCTION)


def test_python_line_longer_than_budget_is_split_by_characters() -> None:
    long_line = '    s = "' + "x" * 2000 + '"'
    source = f"def f():\n{long_line}\n    return s\n"
    chunks = chunk_code(source, count_tokens=char_count, path="f.py")
    got = spans(chunks)
    assert got[0] == (1, 1, "f")
    line_pieces = [c for c, sp in zip(chunks, got, strict=True) if sp[:2] == (2, 2)]
    assert len(line_pieces) >= 3
    assert "".join(c.content.split("\n", 1)[1] for c in line_pieces) == long_line
    assert got[-1] == (3, 3, "f")
    assert all(len(c.content) <= CODE_CHUNK_BUDGET_TOKENS for c in chunks)


def test_python_crlf_is_normalised() -> None:
    chunks = chunk_code("def a():\r\n    pass\r\n", count_tokens=char_count, path="a.py")
    assert [c.content for c in chunks] == ["a.py:1-2 a\ndef a():\n    pass"]


def test_python_byte_order_mark_still_parses() -> None:
    source = "﻿import os\n\n\ndef a():\n    pass\n"
    chunks = chunk_code(source, count_tokens=char_count, path="a.py")
    assert spans(chunks) == [(1, 1, MODULE_SYMBOL), (4, 5, "a")]


def test_python_without_definitions_is_one_module_chunk() -> None:
    chunks = chunk_code("x = 1\ny = 2\n", count_tokens=char_count, path="c.py")
    assert spans(chunks) == [(1, 2, MODULE_SYMBOL)]


@pytest.mark.parametrize(
    ("source", "expected"),
    [
        pytest.param(
            'print "hello"\n\ndef f():\n    print "x"\n',
            [(1, 1, MODULE_SYMBOL), (3, 4, "f")],
            id="python2",
        ),
        pytest.param(
            "def broken(:\n    pass\n\n# Note.\n@wrap\nclass Ok:\n    pass\n",
            [(1, 2, "broken"), (4, 7, "Ok")],
            id="syntax-error",
        ),
        pytest.param(
            "def a():\n    return 1\x00\n", [(1, 2, "a")], id="nul-byte"
        ),
        pytest.param(
            "x = " + "-" * 200000 + "1\n\nasync def b():\n    pass\n",
            None,
            id="too-deep",
        ),
    ],
)
def test_python_parse_failure_falls_back_without_raising(
    source: str, expected: list[tuple[int, int, str]] | None
) -> None:
    chunks = chunk_code(source, count_tokens=char_count, path="legacy.py")
    if expected is not None:
        assert spans(chunks) == expected
        assert_bodies_match_source(chunks, source)
    else:
        assert spans(chunks)[-1] == (3, 4, "b")
    assert all(len(c.content) <= CODE_CHUNK_BUDGET_TOKENS for c in chunks)


# --- AC2: the structural fallback --------------------------------------------

JS_SOURCE = """\
// Synthetic module
import { a } from "./a";

/** Adds. */
export function add(x, y) {
  return x + y;
}

export default class Widget {
  render() {
    return null;
  }
}

const helper = () => 1;
constant = 2;

interface Shape {
  area(): number;
}

export default function () {}
"""


def test_javascript_boundaries() -> None:
    chunks = chunk_code(JS_SOURCE, count_tokens=char_count, path="web/app.ts")
    assert spans(chunks) == [
        (1, 2, MODULE_SYMBOL),
        (4, 7, "add"),
        (9, 13, "Widget"),
        (15, 16, "helper"),
        (18, 20, "Shape"),
        (22, 22, "function"),
    ]
    assert_bodies_match_source(chunks, JS_SOURCE)


GO_SOURCE = """\
// Package demo is synthetic.
package demo

import "fmt"

// Greet says hello.
func Greet(name string) string {
\treturn fmt.Sprintf("hi %s", name)
}

func (s *Server) Start() error {
\treturn nil
}

type Server struct {
\tport int
}

var (
\tdebug = false
)
"""


def test_go_boundaries() -> None:
    chunks = chunk_code(GO_SOURCE, count_tokens=char_count, path="demo/main.go")
    assert spans(chunks) == [
        (1, 4, MODULE_SYMBOL),
        (6, 9, "Greet"),
        (11, 13, "Start"),
        (15, 17, "Server"),
        (19, 21, "var"),
    ]
    assert_bodies_match_source(chunks, GO_SOURCE)


RUST_SOURCE = """\
//! Synthetic crate.
use std::fmt;

/// A point.
#[derive(Debug)]
pub struct Point {
    x: i32,
}

impl fmt::Display for Point {
    fn fmt(&self, f: &mut fmt::Formatter) -> fmt::Result {
        write!(f, "{}", self.x)
    }
}

pub(crate) async fn run() {}

pub const MAX: u32 = 3;

macro_rules! square {
    ($x:expr) => { $x * $x };
}

impl {
}
"""


def test_rust_boundaries() -> None:
    chunks = chunk_code(RUST_SOURCE, count_tokens=char_count, path="src/lib.rs")
    assert spans(chunks) == [
        (1, 2, MODULE_SYMBOL),
        (4, 8, "Point"),
        (10, 14, "fmt::Display for Point"),
        (16, 16, "run"),
        (18, 18, "MAX"),
        (20, 22, "square"),
        (24, 25, "impl"),
    ]
    assert_bodies_match_source(chunks, RUST_SOURCE)


def _java_method(name: str) -> list[str]:
    return [
        f"    public int {name}() {{",
        *[f"        int value{i} = {i} * 1000000;" for i in range(10)],
        "        return 0;",
        "    }",
    ]


JAVA_SOURCE = "\n".join(
    [
        "package demo;",
        "",
        "import java.util.List;",
        "",
        "/** Synthetic service. */",
        "@Service",
        "public final class Greeter {",
        *_java_method("first"),
        "",
        *_java_method("second"),
        "",
        *_java_method("third"),
        "}",
        "",
        "interface Named {",
        "    String name();",
        "}",
        "",
    ]
)


def test_java_oversize_class_is_split_at_blank_lines() -> None:
    chunks = chunk_code(JAVA_SOURCE, count_tokens=char_count, path="Greeter.java")
    got = spans(chunks)
    assert got[0] == (1, 3, MODULE_SYMBOL)
    greeter = [sp for sp in got if sp[2] == "Greeter"]
    # Each method is 13 lines; the class opens at line 5 (its doc comment).
    assert greeter == [(5, 20, "Greeter"), (22, 34, "Greeter"), (36, 49, "Greeter")]
    assert got[-1] == (51, 53, "Named")
    assert all(len(c.content) <= CODE_CHUNK_BUDGET_TOKENS for c in chunks)
    assert_bodies_match_source(chunks, JAVA_SOURCE)
    assert_every_line_once(chunks, JAVA_SOURCE)


SHELL_SOURCE = """\
#!/usr/bin/env bash
set -euo pipefail

# Prints usage.
usage() {
  echo "usage"
}

function main {
  usage
}

main "$@"
"""


def test_shell_boundaries() -> None:
    chunks = chunk_code(SHELL_SOURCE, count_tokens=char_count, path="bin/run.sh")
    assert spans(chunks) == [
        (1, 2, MODULE_SYMBOL),
        (4, 7, "usage"),
        (9, 13, "main"),
    ]
    assert_bodies_match_source(chunks, SHELL_SOURCE)


def test_a_declaration_line_is_never_attached_to_the_next_declaration() -> None:
    # ``@interface`` is a declaration that also starts with an attached prefix.
    source = "@interface Marker {}\nclass Next {}\n"
    chunks = chunk_code(source, count_tokens=char_count, path="Marker.java")
    assert spans(chunks) == [(1, 1, "Marker"), (2, 2, "Next")]


def test_unknown_language_packs_blank_line_blocks_as_module() -> None:
    blocks = ["\n".join(f"key_{b}_{i} = {i * 1000:06d}" for i in range(10)) for b in range(4)]
    source = "\n\n".join(blocks) + "\n"
    chunks = chunk_code(source, count_tokens=char_count, path="settings.cfg")
    got = spans(chunks)
    assert {s for _a, _b, s in got} == {MODULE_SYMBOL}
    # Each block is 169 chars: three (with header) are 538, a fourth would be 709.
    assert [(a, b) for a, b, _s in got] == [(1, 32), (34, 43)]
    assert_bodies_match_source(chunks, source)


def test_no_path_and_no_language_uses_the_placeholder_path() -> None:
    chunks = chunk_code("x = 1\n", count_tokens=char_count)
    assert chunks[0].content == f"{NO_PATH}:1-1 {MODULE_SYMBOL}\nx = 1"


def test_empty_and_blank_content_give_no_chunks() -> None:
    assert chunk_code("", count_tokens=char_count) == []
    assert chunk_code(" \n\t\n", count_tokens=char_count, path="a.py") == []


def test_language_hint_wins_over_the_extension() -> None:
    source = "def a():\n    pass\n"
    chunks = chunk_code(source, count_tokens=char_count, path="bin/tool", language=" Python ")
    assert spans(chunks) == [(1, 2, "a")]


# --- language resolution ------------------------------------------------------


@pytest.mark.parametrize(
    ("path", "hint", "expected"),
    [
        ("a.py", None, "python"),
        ("a.PYI", None, "python"),
        ("a.tsx", None, "javascript"),
        ("a.mjs", None, "javascript"),
        ("a.go", None, "go"),
        ("a.rs", None, "rust"),
        ("A.java", None, "java"),
        ("a.zsh", None, "shell"),
        ("a.cfg", None, None),
        ("Makefile", None, None),
        (None, None, None),
        (None, "golang", "go"),
        ("a.go", "cobol", "go"),
        (None, "cobol", None),
    ],
)
def test_language_of(path: str | None, hint: str | None, expected: str | None) -> None:
    assert language_of(path, hint) == expected


# --- header safety ------------------------------------------------------------


def test_header_path_is_one_line_without_control_characters() -> None:
    chunks = chunk_code("x = 1\n", count_tokens=char_count, path="a\nb\x1b[31m.py")
    first_line, body = chunks[0].content.split("\n", 1)
    assert first_line == "a b[31m.py:1-1 <module>"
    assert body == "x = 1"


def test_header_path_that_sanitises_to_nothing_uses_the_placeholder() -> None:
    chunks = chunk_code("x = 1\n", count_tokens=char_count, path="\x1b\n")
    assert chunks[0].content.startswith(f"{NO_PATH}:1-1 ")


def test_long_path_is_shortened_from_the_left() -> None:
    path = "dir/" * 500 + "leaf.py"
    chunks = chunk_code("x = 1\n", count_tokens=cl100k_count, path=path)
    first_line = chunks[0].content.split("\n", 1)[0]
    assert first_line.startswith("…")
    assert first_line.endswith("/leaf.py:1-1 <module>")
    assert len(first_line) < 300


def test_path_of_expensive_characters_is_shortened_to_its_token_cap() -> None:
    path = "\U0001d518" * 200 + ".py"
    chunks = chunk_code("x = 1\n", count_tokens=cl100k_count, path=path)
    shown = chunks[0].content.split("\n", 1)[0].rsplit(":", 1)[0]
    assert shown.startswith("…")
    assert shown.endswith(".py")
    assert cl100k_count(shown) <= CODE_CHUNK_BUDGET_TOKENS // 4


def test_long_symbol_is_capped() -> None:
    name = "f" + "x" * 300
    chunks = chunk_code(f"def {name}():\n    pass\n", count_tokens=char_count, path="a.py")
    symbol = spans(chunks)[0][2]
    assert len(symbol) == 80
    assert name.startswith(symbol)


# --- the budget, on the real tokenizer ----------------------------------------


@pytest.mark.parametrize(
    "source",
    [
        pytest.param(
            "def huge():\n" + "\n".join(f"    v{i} = compute({i}, 'x')" for i in range(5000)),
            id="5000-line-function",
        ),
        pytest.param("def f():\n    s = '" + "Zq9" * 7000 + "'\n", id="20kb-line"),
        pytest.param(
            "class Big:\n"
            + "\n".join(
                f"    def m{j}(self):\n"
                + "\n".join(f"        a{i} = {i} * {j}" for i in range(400))
                for j in range(3)
            ),
            id="class-with-oversize-methods",
        ),
        pytest.param("\n\n\n".join(f"blk{i} = 1" for i in range(3000)), id="many-blocks"),
    ],
)
def test_every_chunk_is_within_budget_on_cl100k(source: str) -> None:
    chunks = chunk_code(source, count_tokens=cl100k_count, path="x/" * 1000 + "y.py")
    assert chunks
    if "Zq9" not in source:  # a character-split line repeats its line number
        assert_every_line_once(chunks, source)
    assert max(cl100k_count(c.content) for c in chunks) <= CODE_CHUNK_BUDGET_TOKENS
    assert_indexes(chunks)
