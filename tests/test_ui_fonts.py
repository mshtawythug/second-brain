"""The self-hosted reading face (RM-50): what ships, under what licence, and where it applies.

Runs without a browser or a database. ``tests/test_ui_browser_fonts.py`` is the half
that proves the face loads under the real CSP; ``scripts/check_wheel_static.py`` is
the half that proves a real wheel carries it.

Each guard is a helper that takes its input, so the shipped tree is one call and a
synthetic bad tree is the next. A guard that has only ever seen good input has not
been shown to work.
"""
from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from pathlib import Path

import pytest
from starlette.testclient import TestClient

from brain.ui.app import static_dir
from tests.test_ui_security import _client, _context

pytestmark = [
    pytest.mark.nodb,
    # The same TestClient deprecation noise tests/test_ui_security.py scopes out.
    pytest.mark.filterwarnings("ignore::DeprecationWarning"),
]

FONT_BUDGET_BYTES = 120 * 1024
FACE = "Newsreader"
ROMAN = "/static/fonts/newsreader-roman.woff2"

#: Every licence-file name the OFL text is shipped under; one, on purpose.
LICENCE_NAME = "OFL.txt"
SOURCE_NAME = "SOURCE.txt"

_FONT_FACE_RE = re.compile(r"@font-face\s*\{(?P<body>[^}]*)\}")
_SOURCE_LINE_RE = re.compile(r"(?m)^(?P<digest>[0-9a-f]{64})  (?P<name>\S+)$")
#: ``url("../fonts/x.woff2") format("woff2")``, and nothing else in the ``src``.
_RELATIVE_WOFF2_SRC_RE = re.compile(
    r"""\s*src:\s*url\("\.\./fonts/[a-z0-9-]+\.woff2"\)\s+format\("woff2"\)\s*"""
)


def _font_tree_problems(fonts_dir: Path, *, budget: int = FONT_BUDGET_BYTES) -> list[str]:
    """Why ``fonts_dir`` may not ship: budget, file shape, licence, provenance."""
    fonts = sorted(fonts_dir.glob("*.woff2"))
    if not fonts:
        return [f"{fonts_dir.name}: no .woff2 file"]
    problems = [
        f"{font.name}: not a WOFF2 file" for font in fonts if font.read_bytes()[:4] != b"wOF2"
    ]
    total = sum(font.stat().st_size for font in fonts)
    if total > budget:
        problems.append(f"{total} bytes of woff2 is over the {budget} byte budget")
    problems.extend(_licence_problems(fonts_dir / LICENCE_NAME))
    licence = fonts_dir / LICENCE_NAME
    pinned = [*fonts, licence] if licence.is_file() else fonts
    problems.extend(_source_problems(fonts_dir / SOURCE_NAME, pinned))
    return problems


def _licence_problems(licence: Path) -> list[str]:
    """The OFL text must ship, and must not reserve the name a modified subset keeps."""
    if not licence.is_file():
        return [f"{licence.name}: missing; the OFL requires the licence to travel with the font"]
    text = licence.read_text(encoding="utf-8")
    problems = []
    if "SIL OPEN FONT LICENSE Version 1.1" not in text:
        problems.append(f"{licence.name}: not the OFL 1.1 text")
    # The definition inside the OFL body says "Reserved Font Name" too. A reservation
    # is declared in the copyright statement, which is what precedes the licence text.
    statement = text.split("SIL OPEN FONT LICENSE", 1)[0]
    if "reserved font name" in statement.lower():
        problems.append(f"{licence.name}: declares a Reserved Font Name; a subset may not keep it")
    return problems


def _source_problems(source: Path, pinned: list[Path]) -> list[str]:
    """Every pinned file has its current digest on record, and every record has a file.

    The licence is pinned with the fonts: it is the one file the URL scan skips.
    """
    if not source.is_file():
        return [f"{source.name}: missing; nothing records where the fonts came from"]
    recorded = {
        match["name"]: match["digest"]
        for match in _SOURCE_LINE_RE.finditer(source.read_text(encoding="utf-8"))
    }
    problems = []
    for path in pinned:
        digest = hashlib.sha256(path.read_bytes()).hexdigest()
        if recorded.get(path.name) != digest:
            problems.append(f"{path.name}: its digest does not match {source.name}")
    shipped = {path.name for path in source.parent.iterdir()}
    problems.extend(
        f"{source.name} names {name}, which is not shipped"
        for name in sorted(recorded)
        if name not in shipped
    )
    return problems


@dataclass(frozen=True)
class SubsetClaims:
    """What the recorded subset commands say the files hold."""

    unicodes: tuple[str, ...]
    weight: str


def _subset_claims(source_text: str) -> SubsetClaims:
    """Read the kept ``--unicodes`` and ``wght`` axis out of ``SOURCE.txt``'s commands."""
    unicodes = re.search(r"--unicodes=(\S+)", source_text)
    weight = re.search(r"\bwght=(\d+):(\d+)", source_text)
    assert unicodes, f"{SOURCE_NAME} does not record --unicodes"
    assert weight, f"{SOURCE_NAME} does not record the wght axis"
    return SubsetClaims(tuple(unicodes[1].split(",")), f"{weight[1]} {weight[2]}")


def _font_face_problems(css: str, claims: SubsetClaims) -> list[str]:
    """Each ``@font-face`` swaps, loads one relative woff2, and claims only what was kept.

    A ``unicode-range`` wider than the subset promises glyphs the file lacks, and a
    narrower one sends kept glyphs to the fallback. A ``font-weight`` wider than the
    axis has the browser clamp or synthesise. Neither fails visibly.
    """
    problems = []
    for match in _FONT_FACE_RE.finditer(css):
        declarations = [part.strip() for part in match["body"].split(";")]
        family = next((d for d in declarations if d.startswith("font-family")), "?")
        if "font-display: swap" not in declarations:
            problems.append(f"{family}: no font-display: swap")
        sources = [d for d in declarations if d.startswith("src")]
        if len(sources) != 1 or not _RELATIVE_WOFF2_SRC_RE.fullmatch(sources[0]):
            problems.append(f"{family}: src is not one relative woff2: {sources}")
        if f"font-weight: {claims.weight}" not in declarations:
            problems.append(f"{family}: font-weight is not {claims.weight}")
        ranges = [d for d in declarations if d.startswith("unicode-range:")]
        stated = [" ".join(d.split(":", 1)[1].split()) for d in ranges]
        if stated != [", ".join(claims.unicodes)]:
            problems.append(f"{family}: unicode-range differs from {SOURCE_NAME}'s --unicodes")
    return problems


def _token_stack(tokens_css: str, name: str) -> str:
    """The value of one custom property, whitespace collapsed; first declaration wins."""
    match = re.search(rf"{re.escape(name)}:\s*([^;]+);", tokens_css)
    assert match, f"{name} is not declared"
    return " ".join(match[1].split())


def _chrome_stack_problems(tokens_css: str, face: str) -> list[str]:
    """The instrument's stacks stay system stacks (the ticket's scope-out)."""
    return [
        f"{name} names {face}; the instrument chrome keeps system stacks"
        for name in ("--font-ui", "--font-mono")
        if face.lower() in _token_stack(tokens_css, name).lower()
    ]


# ------------------------------------------------------------ the shipped tree --


def test_the_shipped_fonts_pass_every_tree_guard() -> None:
    assert _font_tree_problems(static_dir() / "fonts") == []


def test_the_shipped_font_faces_match_what_the_subset_was_cut_to() -> None:
    css = (static_dir() / "css" / "reading.css").read_text(encoding="utf-8")
    source = (static_dir() / "fonts" / SOURCE_NAME).read_text(encoding="utf-8")
    assert _FONT_FACE_RE.search(css), "reading.css declares no @font-face"
    assert _font_face_problems(css, _subset_claims(source)) == []


def test_one_family_in_two_styles_is_declared() -> None:
    """One face (RM-50.D2): a roman and an italic, and no second family."""
    css = (static_dir() / "css" / "reading.css").read_text(encoding="utf-8")
    bodies = [match["body"] for match in _FONT_FACE_RE.finditer(css)]
    assert len(bodies) == 2
    assert all(f'font-family: "{FACE}"' in body for body in bodies)
    assert sorted(re.search(r"font-style:\s*(\w+)", b)[1] for b in bodies) == [  # type: ignore[index]
        "italic",
        "normal",
    ]


def test_the_reading_token_leads_with_the_face_and_keeps_its_fallbacks() -> None:
    tokens = (static_dir() / "css" / "tokens.css").read_text(encoding="utf-8")
    stack = _token_stack(tokens, "--font-read")
    assert stack.startswith(f'"{FACE}", ui-serif, Georgia')
    assert stack.endswith("serif")


def test_the_chrome_tokens_keep_their_system_stacks() -> None:
    tokens = (static_dir() / "css" / "tokens.css").read_text(encoding="utf-8")
    assert _chrome_stack_problems(tokens, FACE) == []
    assert _token_stack(tokens, "--font-ui").startswith("ui-sans-serif, system-ui")
    assert _token_stack(tokens, "--font-mono").startswith("ui-monospace")


# ---------------------------------------------------- the guards, on bad input --


def _good_tree(root: Path, *, size: int = 64) -> Path:
    """A synthetic font directory every guard accepts."""
    root.mkdir(exist_ok=True)
    font = root / "a.woff2"
    font.write_bytes(b"wOF2" + b"\0" * (size - 4))
    (root / LICENCE_NAME).write_text(
        "Copyright 2000 The Example Project Authors\n\nSIL OPEN FONT LICENSE Version 1.1\n"
        '"Reserved Font Name" refers to any names specified as such.\n',
        encoding="utf-8",
    )
    _record_digests(root)
    return root


def _record_digests(fonts: Path) -> None:
    """Write a provenance record matching the directory as it stands."""
    lines = [
        f"{hashlib.sha256(path.read_bytes()).hexdigest()}  {path.name}\n"
        for path in sorted(fonts.iterdir())
        if path.name != SOURCE_NAME
    ]
    (fonts / SOURCE_NAME).write_text("".join(lines), encoding="utf-8")


def test_a_good_tree_passes_and_keeps_the_ofls_own_definition_of_the_term(tmp_path: Path) -> None:
    """Keeps: the OFL body defines "Reserved Font Name"; only a declaration is refused."""
    assert _font_tree_problems(_good_tree(tmp_path / "fonts")) == []


def test_a_tree_exactly_at_the_budget_passes_and_one_byte_over_is_refused(tmp_path: Path) -> None:
    fonts = _good_tree(tmp_path / "fonts", size=100)
    assert _font_tree_problems(fonts, budget=100) == []

    problems = _font_tree_problems(fonts, budget=99)

    assert len(problems) == 1
    assert "over the 99 byte budget" in problems[0]


def test_the_budget_counts_every_font_not_each_one(tmp_path: Path) -> None:
    fonts = _good_tree(tmp_path / "fonts", size=60)
    second = fonts / "b.woff2"
    second.write_bytes(b"wOF2" + b"\0" * 56)
    _record_digests(fonts)

    assert any("budget" in p for p in _font_tree_problems(fonts, budget=100))


def test_a_directory_with_no_font_is_refused_rather_than_passing_vacuously(tmp_path: Path) -> None:
    assert _font_tree_problems(tmp_path) == [f"{tmp_path.name}: no .woff2 file"]


def test_a_file_that_is_not_woff2_is_refused(tmp_path: Path) -> None:
    fonts = _good_tree(tmp_path / "fonts")
    font = fonts / "a.woff2"
    font.write_bytes(b"<svg>" + b"\0" * 8)
    _record_digests(fonts)

    assert _font_tree_problems(fonts) == ["a.woff2: not a WOFF2 file"]


def test_a_missing_licence_is_refused(tmp_path: Path) -> None:
    fonts = _good_tree(tmp_path / "fonts")
    (fonts / LICENCE_NAME).unlink()

    assert any("the OFL requires" in p for p in _font_tree_problems(fonts))


def test_a_licence_that_is_not_the_ofl_is_refused(tmp_path: Path) -> None:
    fonts = _good_tree(tmp_path / "fonts")
    (fonts / LICENCE_NAME).write_text("All rights reserved.\n", encoding="utf-8")

    assert any("not the OFL 1.1" in p for p in _font_tree_problems(fonts))


def test_a_declared_reserved_font_name_is_refused(tmp_path: Path) -> None:
    """Subsetting is modification under the OFL; a reserved name could not be kept."""
    fonts = _good_tree(tmp_path / "fonts")
    (fonts / LICENCE_NAME).write_text(
        "Copyright 2000 Example, with Reserved Font Name 'Example'.\n\n"
        "SIL OPEN FONT LICENSE Version 1.1\n",
        encoding="utf-8",
    )

    assert any("declares a Reserved Font Name" in p for p in _font_tree_problems(fonts))


def test_a_font_replaced_without_updating_the_record_is_refused(tmp_path: Path) -> None:
    fonts = _good_tree(tmp_path / "fonts")
    (fonts / "a.woff2").write_bytes(b"wOF2 and something else")

    assert _font_tree_problems(fonts) == [f"a.woff2: its digest does not match {SOURCE_NAME}"]


def test_a_licence_edited_without_updating_the_record_is_refused(tmp_path: Path) -> None:
    """OFL.txt is the one file the URL scan skips, so its bytes must be pinned instead."""
    fonts = _good_tree(tmp_path / "fonts")
    with (fonts / LICENCE_NAME).open("a", encoding="utf-8") as handle:
        handle.write("see https://example.org/x\n")

    assert _font_tree_problems(fonts) == [
        f"{LICENCE_NAME}: its digest does not match {SOURCE_NAME}"
    ]


def test_a_digest_line_naming_a_file_that_is_not_shipped_is_refused(tmp_path: Path) -> None:
    """A record that outlived its file claims a provenance nothing can check."""
    fonts = _good_tree(tmp_path / "fonts")
    with (fonts / SOURCE_NAME).open("a", encoding="utf-8") as handle:
        handle.write(f"{'0' * 64}  gone.woff2\n")

    assert _font_tree_problems(fonts) == [f"{SOURCE_NAME} names gone.woff2, which is not shipped"]


def test_a_font_absent_from_the_record_is_refused(tmp_path: Path) -> None:
    fonts = _good_tree(tmp_path / "fonts")
    (fonts / "b.woff2").write_bytes(b"wOF2")

    assert _font_tree_problems(fonts) == [f"b.woff2: its digest does not match {SOURCE_NAME}"]


def test_a_missing_provenance_record_is_refused(tmp_path: Path) -> None:
    fonts = _good_tree(tmp_path / "fonts")
    (fonts / SOURCE_NAME).unlink()

    assert any("nothing records" in p for p in _font_tree_problems(fonts))


_GOOD_FACE = (
    '@font-face { font-family: "X"; src: url("../fonts/x.woff2") format("woff2"); '
    "font-weight: 400 700; font-display: swap; unicode-range: U+0020-007E, U+00A0-017F; }"
)
_SOURCE_COMMANDS = (
    "fonttools varLib.instancer INPUT wght=400:700 opsz=16 -o VARIABLE.ttf\n"
    "pyftsubset VARIABLE.ttf --unicodes=U+0020-007E,U+00A0-017F \\\n"
)
_CLAIMS = _subset_claims(_SOURCE_COMMANDS)


def test_the_claims_are_read_from_the_recorded_commands() -> None:
    assert SubsetClaims(unicodes=("U+0020-007E", "U+00A0-017F"), weight="400 700") == _CLAIMS


@pytest.mark.parametrize(
    "source",
    [
        "pyftsubset VARIABLE.ttf --unicodes=U+0020-007E\n",
        "fonttools varLib.instancer INPUT wght=400:700\n",
    ],
)
def test_a_record_missing_a_claim_is_an_error_not_a_pass(source: str) -> None:
    with pytest.raises(AssertionError, match="does not record"):
        _subset_claims(source)


def test_a_good_font_face_and_a_stylesheet_with_none_both_pass() -> None:
    assert _font_face_problems(_GOOD_FACE, _CLAIMS) == []
    assert _font_face_problems("a { color: red; }", _CLAIMS) == []


def test_a_wrapped_unicode_range_still_matches() -> None:
    """Keeps: a long range wraps in the stylesheet; the line break is not a difference."""
    css = _GOOD_FACE.replace("U+0020-007E, U+00A0-017F", "U+0020-007E,\n    U+00A0-017F")

    assert _font_face_problems(css, _CLAIMS) == []


@pytest.mark.parametrize(
    "bad_range",
    ["U+0020-007E", "U+0020-007E, U+00A0-017F, U+0400-04FF", "U+00A0-017F, U+0020-007E"],
)
def test_a_unicode_range_that_differs_from_the_subset_is_refused(bad_range: str) -> None:
    css = _GOOD_FACE.replace("U+0020-007E, U+00A0-017F", bad_range)

    problems = _font_face_problems(css, _CLAIMS)

    assert len(problems) == 1
    assert "unicode-range differs" in problems[0]


def test_a_face_with_no_unicode_range_is_refused() -> None:
    css = _GOOD_FACE.replace(" unicode-range: U+0020-007E, U+00A0-017F;", "")

    assert any("unicode-range differs" in p for p in _font_face_problems(css, _CLAIMS))


@pytest.mark.parametrize("bad_weight", ["font-weight: 400 900; ", "font-weight: 400; ", ""])
def test_a_font_weight_other_than_the_kept_axis_is_refused(bad_weight: str) -> None:
    css = _GOOD_FACE.replace("font-weight: 400 700; ", bad_weight)

    problems = _font_face_problems(css, _CLAIMS)

    assert len(problems) == 1
    assert "font-weight is not 400 700" in problems[0]


@pytest.mark.parametrize(
    "bad_src",
    [
        'url("https://cdn.example/x.woff2") format("woff2")',
        'url("//cdn.example/x.woff2") format("woff2")',
        'url("/static/fonts/x.woff2") format("woff2")',
        'url("../fonts/x.woff") format("woff")',
        'local("X"), url("../fonts/x.woff2") format("woff2")',
        'url("../fonts/x.woff2") format("woff2"), url("https://cdn.example/y.woff2")',
    ],
)
def test_a_src_that_is_not_one_relative_woff2_is_refused(bad_src: str) -> None:
    css = _GOOD_FACE.replace('url("../fonts/x.woff2") format("woff2")', bad_src)

    problems = _font_face_problems(css, _CLAIMS)

    assert len(problems) == 1
    assert "src is not one relative woff2" in problems[0]


def test_a_font_face_with_no_swap_is_refused() -> None:
    problems = _font_face_problems(_GOOD_FACE.replace("font-display: swap; ", ""), _CLAIMS)

    assert problems == ['font-family: "X": no font-display: swap']


def test_a_chrome_stack_naming_the_face_is_refused() -> None:
    css = '--font-ui: "Newsreader", system-ui; --font-mono: ui-monospace;'

    assert _chrome_stack_problems(css, FACE) == [
        "--font-ui names Newsreader; the instrument chrome keeps system stacks"
    ]


# ------------------------------------------------------ the static font route --


def _font_client(tmp_path: Path) -> TestClient:
    return _client(_context(tmp_path))


def test_a_font_is_served_as_a_font_with_the_hardening_headers(tmp_path: Path) -> None:
    with _font_client(tmp_path) as client:
        response = client.get(ROMAN)

    assert response.status_code == 200
    assert response.headers["content-type"] == "font/woff2"
    assert response.headers["x-content-type-options"] == "nosniff"
    assert "font-src 'self'" in response.headers["content-security-policy"]
    assert response.content[:4] == b"wOF2"


def test_the_font_type_does_not_depend_on_the_hosts_mime_database(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Python 3.11 and 3.12 know no ``.woff2``; only a host file supplies it.

    The stub is what a host without that file looks like. The ``.png`` proves it is
    in force: on a host database alone, this test could not fail.
    """
    monkeypatch.setattr("starlette.responses.guess_type", lambda *_a, **_k: (None, None))

    with _font_client(tmp_path) as client:
        font = client.get(ROMAN)
        image = client.get("/static/icon.png")

    assert font.headers["content-type"] == "font/woff2"
    assert image.headers["content-type"] == "application/octet-stream"


@pytest.mark.parametrize(
    ("path", "expected"),
    [("/static/css/reading.css", "text/css"), ("/static/js/main.js", "javascript")],
)
def test_other_assets_keep_their_own_content_type(
    tmp_path: Path, path: str, expected: str
) -> None:
    with _font_client(tmp_path) as client:
        response = client.get(path)

    assert response.status_code == 200
    assert expected in response.headers["content-type"]


def test_a_conditional_font_request_still_gets_a_304(tmp_path: Path) -> None:
    with _font_client(tmp_path) as client:
        etag = client.get(ROMAN).headers["etag"]
        response = client.get(ROMAN, headers={"if-none-match": etag})

    assert response.status_code == 304
    assert response.content == b""
    assert "content-type" not in response.headers


def test_a_missing_font_is_a_404_not_a_typed_empty_body(tmp_path: Path) -> None:
    with _font_client(tmp_path) as client:
        response = client.get("/static/fonts/missing.woff2")

    assert response.status_code == 404
    assert "font/woff2" not in response.headers.get("content-type", "")


