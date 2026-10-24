"""The offline test for CSS ``url()``: every stylesheet target in ``brain ui`` resolves (RM-05)."""
from __future__ import annotations

import re
from pathlib import Path
from urllib.parse import unquote, urljoin, urlsplit

import pytest

from brain.ui.app import static_dir
from tests.test_ui_static_assets import _IGNORED_PARTS, _glob_matches, _package_data_globs

#: Opens NO database connection, like ``test_ui_static_assets``: this module
#: reads files off disk and parses them. See
#: ``conftest._session_touches_the_database``.
pytestmark = pytest.mark.nodb

#: ``/* … */`` comments, stripped before any ``url(`` is looked for:
#: ``css/graph.css`` and ``css/related.css`` say "no url()" in a comment.
_CSS_COMMENT_RE = re.compile(r"/\*.*?\*/", re.DOTALL)

#: One ``url(…)`` and its target: double-quoted, single-quoted or bare.
#:
#: QUOTE-AWARE ON PURPOSE. ``css/base.css``'s noise texture is a double-quoted
#: ``data:`` SVG that holds a ``url(%23n)`` of its own. The quoted alternative
#: consumes the whole outer target, and ``finditer`` never overlaps matches, so
#: the inner ``url(`` is never matched by itself. A bare-only pattern would
#: match it and report ``%23n`` as a missing file.
#:
#: The bare alternative refuses a backslash, so a CSS escape there
#: (``url(a\)b)``) matches nothing and the residue check raises. Otherwise it
#: would be cut short at the escape and checked as ``a\``.
_CSS_URL_RE = re.compile(
    r"""\burl\(\s*(?:"([^"]*)"|'([^']*)'|([^\s"'()\\]*))\s*\)""", re.IGNORECASE
)
_CSS_URL_OPEN_RE = re.compile(r"\burl\(", re.IGNORECASE)

#: The served origin a stylesheet's targets are joined against, as a browser
#: joins them. Only its identity matters: a target that leaves it is external.
_UI_ORIGIN = "http://brain-ui.invalid"
_STATIC_URL_PREFIX = "/static/"
_OUTSIDE_STATIC_DIR = "outside static/ (through a link or an encoded ..)"


def _css_url_targets(css: str) -> list[str]:
    """Every ``url()`` target in ``css``, comments excluded, whitespace stripped.

    Raises :class:`ValueError` when a ``url(`` is left over that the pattern
    could not parse: a backslash escape in a quoted or bare target, or a quote
    inside a bare target. Without
    that, a ``url()`` the regex did not understand would go unchecked and the
    test would stay green.
    """
    source = _CSS_COMMENT_RE.sub(" ", css)
    targets = [
        next(group for group in match.groups() if group is not None).strip()
        for match in _CSS_URL_RE.finditer(source)
    ]
    # Replaced with a space, not "", so two pieces cannot join into a new `url(`.
    leftover = _CSS_URL_OPEN_RE.search(_CSS_URL_RE.sub(" ", source))
    if leftover:
        snippet = source[leftover.start() : leftover.start() + 60]
        raise ValueError(f"could not parse url() target: {snippet!r}")
    return targets


def _css_url_problems(stylesheet: Path, static_root: Path) -> tuple[list[Path], list[str]]:
    """Resolve every ``url()`` in ``stylesheet``: ``(files found, problems)``.

    Targets are joined against the stylesheet's served URL, as a browser does,
    so a relative target resolves against the stylesheet's own directory and a
    root-relative ``/static/…`` one resolves to the file the server would send.
    ``data:`` and ``#fragment`` targets fetch no file and are skipped.

    A target with NO PATH is a problem in its own right: an empty ``url()``,
    or a query-only one such as ``url("?v=1")``. Joined as a URL it names the
    stylesheet itself, and joined as a path it names the directory, so neither
    ``exists()`` nor ``is_file()`` would catch it. For the same reason every
    resolved target must pass ``is_file()``, never ``exists()``.
    """
    name = stylesheet.relative_to(static_root).as_posix()
    try:
        targets = _css_url_targets(stylesheet.read_text(encoding="utf-8"))
    except ValueError as exc:
        return [], [f"{name}: {exc}"]

    root = static_root.resolve()
    sheet_url = f"{_UI_ORIGIN}{_STATIC_URL_PREFIX}{name}"
    files: list[Path] = []
    problems: list[str] = []
    for target in targets:
        if target.lower().startswith("data:") or target.startswith("#"):
            continue
        where = f"{name}: url({target!r})"
        if not urlsplit(target).path:
            problems.append(f"{where} has no path, so it names no file")
            continue
        joined = urlsplit(urljoin(sheet_url, target))
        path = unquote(joined.path)
        if f"{joined.scheme}://{joined.netloc}" != _UI_ORIGIN:
            problems.append(f"{where} is not same-origin; brain ui must work offline")
            continue
        if not path.startswith(_STATIC_URL_PREFIX):
            problems.append(f"{where} resolves to {path}, outside {_STATIC_URL_PREFIX}")
            continue
        candidate = (root / path.removeprefix(_STATIC_URL_PREFIX)).resolve()
        if not candidate.is_relative_to(root):
            problems.append(f"{where} resolves to {candidate}, {_OUTSIDE_STATIC_DIR}")
        elif not candidate.is_file():
            problems.append(f"{where} resolves to {path}, which is not a file")
        else:
            files.append(candidate)
    return files, problems


def _unshipped_targets(
    name: str, files: list[Path], package_root: Path, globs: list[str]
) -> list[str]:
    """A problem for each of ``files`` that no ``package-data`` glob ships.

    ``name`` is the stylesheet that names the files. ``package_root`` is the
    directory the globs are relative to (``brain/ui``). A file found on disk
    but outside every glob loads in the dev tree and is missing from the wheel.
    """
    root = package_root.resolve()
    problems: list[str] = []
    for target in files:
        relative = target.relative_to(root).as_posix()
        if not any(_glob_matches(relative, pattern) for pattern in globs):
            problems.append(
                f"{name}: {relative} exists but matches no package-data glob, "
                f"so the wheel would not ship it"
            )
    return problems


def _stylesheet_problems(static_root: Path, globs: list[str]) -> list[str]:
    """Every problem with every ``url()`` in every stylesheet under ``static_root``.

    For each ``.css`` file: :func:`_css_url_problems` (does each target load?),
    then :func:`_unshipped_targets` (would the wheel ship it?). The package root
    the globs are relative to is ``static_root.parent``. Finding no stylesheet
    at all is itself a problem, so a walk that matched nothing cannot pass.
    """
    stylesheets = sorted(
        path
        for path in static_root.rglob("*.css")
        if path.is_file() and not _IGNORED_PARTS & set(path.parts)
    )
    if not stylesheets:
        return [f"no stylesheet was found under {static_root}, so nothing was checked"]

    problems: list[str] = []
    for stylesheet in stylesheets:
        name = stylesheet.relative_to(static_root).as_posix()
        files, found = _css_url_problems(stylesheet, static_root)
        problems.extend(found)
        problems.extend(_unshipped_targets(name, files, static_root.parent, globs))
    return problems


def test_every_css_url_resolves_on_disk() -> None:
    """A typo'd ``url()`` in a stylesheet is a missing font or image; catch it here.

    ``test_every_referenced_asset_exists_on_disk`` reads only ``index.html``,
    so until RM-05 a stylesheet could name a file that does not exist and
    every test stayed green. Every shipped ``.css`` file is checked, not just
    ``css/*.css``: pyproject also declares a top-level ``static/*.css``.

    Each resolved file must also match a declared ``package-data`` glob, so a
    font in a directory the wheel does not ship fails HERE, naming the
    stylesheet that needs it, not only in ``test_ui_static_assets``'s packaging test.

    No "at least one ``url()``" floor: :func:`_css_url_targets` raises on any
    ``url(`` it cannot parse, so a pattern that stopped matching is already a
    failure, and a stylesheet with no ``url(`` has nothing to check.
    """
    problems = _stylesheet_problems(static_dir(), _package_data_globs("brain.ui"))
    assert not problems, "stylesheet url() targets that would not load:\n" + "\n".join(problems)


#: The ``data:`` SVG in ``css/base.css``, shortened: a double-quoted target
#: whose single-quoted SVG attribute holds a ``url(%23n)`` of its own.
_NESTED_DATA_URI = (
    "data:image/svg+xml,%3Csvg%3E%3Cfilter id='n'/%3E"
    "%3Crect filter='url(%23n)'/%3E%3C/svg%3E"
)


@pytest.mark.parametrize(
    ("css", "expected"),
    [
        pytest.param('a { src: url("fonts/a.woff2"); }', ["fonts/a.woff2"], id="double-quoted"),
        pytest.param("a { src: url('fonts/a.woff2'); }", ["fonts/a.woff2"], id="single-quoted"),
        pytest.param("a { src: url(fonts/a.woff2); }", ["fonts/a.woff2"], id="bare"),
        pytest.param('a { src: url(  "a.png"\t); }', ["a.png"], id="whitespace-quoted"),
        pytest.param("a { src: url(  a.png  ); }", ["a.png"], id="whitespace-bare"),
        pytest.param('a { src: url(" a.png "); }', ["a.png"], id="whitespace-inside-quotes"),
        pytest.param(
            "a { src: url(a.woff2) format('woff2'), url('b.woff') format('woff'); }",
            ["a.woff2", "b.woff"],
            id="several-per-line",
        ),
        pytest.param("a { src: URL(a.png); }", ["a.png"], id="upper-case"),
        pytest.param("/* no url(x.png) here */ a { color: red; }", [], id="comment"),
        # A greedy comment pattern would erase everything from the first /* to
        # the last */, and every real stylesheet has several comments.
        pytest.param("/* a */ a { src: url(b.png); } /* c */", ["b.png"], id="between-comments"),
        pytest.param(
            "/*\n * TOKENS ONLY. No url(), nothing external.\n */\na { src: url(b.png); }",
            ["b.png"],
            id="multi-line-comment",
        ),
        pytest.param(
            f'a {{ background-image: url("{_NESTED_DATA_URI}"); }}',
            [_NESTED_DATA_URI],
            id="nested-data-uri",
        ),
        pytest.param("a { src: url(); }", [""], id="empty-bare"),
        pytest.param('a { src: url(""); }', [""], id="empty-quoted"),
    ],
)
def test_css_url_extraction(css: str, expected: list[str]) -> None:
    """The extractor finds every real target, and only real targets."""
    assert _css_url_targets(css) == expected


@pytest.mark.parametrize(
    "css",
    [
        pytest.param('a { src: url("a\\"b.png"); }', id="escaped-quote"),
        pytest.param("a { src: url(a'b.png); }", id="quote-in-bare-target"),
        pytest.param("a { src: url(a\\)b.png); }", id="escaped-paren"),
        pytest.param("a { src: URL(a'b.png); }", id="upper-case-unparseable"),
    ],
)
def test_an_unparseable_css_url_raises(css: str) -> None:
    """A ``url(`` the pattern cannot parse is a failure, never a silent skip."""
    with pytest.raises(ValueError, match="url"):
        _css_url_targets(css)


def _static_tree(root: Path, stylesheet_body: str) -> tuple[Path, Path]:
    """A synthetic ``static/`` with one stylesheet and two real assets.

    Returns ``(static_root, stylesheet)``. ``css/fonts/a.woff2``,
    ``css/fonts/a b.woff2``, ``css/data-logo.png`` and ``img/logo.png`` exist,
    so a test can tell a resolver that rejects everything from one that
    resolves.
    """
    static_root = root / "static"
    (static_root / "css" / "fonts").mkdir(parents=True)
    (static_root / "img").mkdir()
    (static_root / "css" / "fonts" / "a b.woff2").write_bytes(b"font")
    (static_root / "css" / "fonts" / "a.woff2").write_bytes(b"font")
    (static_root / "img" / "logo.png").write_bytes(b"png")
    (static_root / "css" / "data-logo.png").write_bytes(b"png")
    stylesheet = static_root / "css" / "x.css"
    stylesheet.write_text(stylesheet_body, encoding="utf-8")
    return static_root, stylesheet


@pytest.mark.parametrize(
    "target",
    [
        pytest.param("missing.woff2", id="missing"),
        # Not a data: URI, so it must be checked rather than skipped.
        pytest.param("data-missing.woff2", id="data-prefixed-missing"),
    ],
)
def test_a_missing_css_url_target_is_named(tmp_path: Path, target: str) -> None:
    """AC 2: the failure names the stylesheet and the target."""
    static_root, stylesheet = _static_tree(tmp_path, f'a {{ src: url("{target}"); }}')

    files, problems = _css_url_problems(stylesheet, static_root)

    assert files == []
    assert len(problems) == 1
    assert "css/x.css" in problems[0]
    assert target in problems[0]


@pytest.mark.parametrize(
    "target",
    [
        pytest.param("fonts/a.woff2", id="sibling-directory"),
        pytest.param("../img/logo.png", id="parent-then-sibling"),
        pytest.param("/static/img/logo.png", id="root-relative"),
        pytest.param("fonts/a.woff2?v=1#iefix", id="query-and-fragment"),
        pytest.param("fonts/a%20b.woff2", id="percent-encoded"),
        # Starts with "data" but is not a data: URI, so it must be resolved.
        pytest.param("data-logo.png", id="data-prefixed-file-name"),
    ],
)
def test_resolvable_css_urls_pass(tmp_path: Path, target: str) -> None:
    """Guard the guard: real targets resolve, so a reject-everything resolver fails."""
    static_root, stylesheet = _static_tree(tmp_path, f'a {{ src: url("{target}"); }}')

    files, problems = _css_url_problems(stylesheet, static_root)

    assert problems == []
    assert len(files) == 1
    assert files[0].is_file()


_OUTSIDE_STATIC = "outside /static/"
_NOT_SAME_ORIGIN = "not same-origin"


@pytest.mark.parametrize(
    ("target", "reason"),
    [
        pytest.param("../../app.py", _OUTSIDE_STATIC, id="parent-of-static"),
        pytest.param("../../../../etc/hosts", _OUTSIDE_STATIC, id="far-above-root"),
        pytest.param("/elsewhere/a.woff2", _OUTSIDE_STATIC, id="root-relative-outside-static"),
        # Percent-decoded only after urljoin, so the `..` survives the /static/
        # prefix check and is caught by the containment check after resolve().
        pytest.param("%2e%2e/%2e%2e/app.py", _OUTSIDE_STATIC_DIR, id="encoded-dot-dot"),
        pytest.param("//cdn.invalid/a.woff2", _NOT_SAME_ORIGIN, id="protocol-relative"),
        pytest.param("https://cdn.invalid/a.woff2", _NOT_SAME_ORIGIN, id="other-origin"),
        # The path names a real local file: only the origin check rejects these.
        pytest.param(
            "https://cdn.invalid/static/img/logo.png",
            _NOT_SAME_ORIGIN,
            id="other-origin-static-path",
        ),
        pytest.param(
            "//cdn.invalid/static/img/logo.png",
            _NOT_SAME_ORIGIN,
            id="protocol-relative-static-path",
        ),
    ],
)
def test_a_css_url_escaping_static_fails(tmp_path: Path, target: str, reason: str) -> None:
    """AC 3: a target outside ``static_dir()`` fails, even when the file exists."""
    static_root, stylesheet = _static_tree(tmp_path, f'a {{ src: url("{target}"); }}')
    (tmp_path / "app.py").write_text("", encoding="utf-8")

    files, problems = _css_url_problems(stylesheet, static_root)

    assert files == []
    assert len(problems) == 1
    assert "css/x.css" in problems[0]
    assert target in problems[0]
    # The reason, too: each check must be the one that fires, or removing it
    # would leave the case red only because a later check happened to catch it.
    assert reason in problems[0]


def test_an_unparseable_css_url_is_a_problem(tmp_path: Path) -> None:
    """The resolver reports a ``url(`` it cannot parse; it never drops it."""
    static_root, stylesheet = _static_tree(tmp_path, 'a { src: url("a\\"b.png"); }')

    files, problems = _css_url_problems(stylesheet, static_root)

    assert files == []
    assert len(problems) == 1
    assert "css/x.css" in problems[0]
    assert "could not parse" in problems[0]


def test_unshipped_targets_reports_only_files_no_glob_ships(tmp_path: Path) -> None:
    """The ``package-data`` clause of the real-tree test, against the real globs.

    Today's stylesheets name no file at all (their one ``url()`` is a ``data:``
    SVG), so on the real tree this clause checks nothing. This test is what
    keeps it honest until RM-50 gives it a font to check.
    """
    globs = _package_data_globs("brain.ui")
    package_root = tmp_path / "ui"
    unshipped = package_root / "static" / "fonts" / "a.woff2"
    shipped_image = package_root / "static" / "brain-logo.png"
    shipped_sheet = package_root / "static" / "css" / "x.css"
    for path in (unshipped, shipped_image, shipped_sheet):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"asset")

    problems = _unshipped_targets(
        "css/x.css", [unshipped, shipped_image, shipped_sheet], package_root, globs
    )

    assert len(problems) == 1
    assert "css/x.css" in problems[0]
    assert "static/fonts/a.woff2" in problems[0]


def test_stylesheet_problems_include_an_unshipped_target(tmp_path: Path) -> None:
    """The real-tree test's packaging clause, through the helper it calls.

    A font that loads in the dev tree but sits in a directory no glob ships
    must come back as a problem, or the wheel ships a page without its font.
    """
    static_root = tmp_path / "ui" / "static"
    (static_root / "css").mkdir(parents=True)
    (static_root / "fonts").mkdir()
    (static_root / "fonts" / "a.woff2").write_bytes(b"font")
    (static_root / "css" / "x.css").write_text(
        'a { src: url("../fonts/a.woff2"); }', encoding="utf-8"
    )

    problems = _stylesheet_problems(static_root, _package_data_globs("brain.ui"))

    assert len(problems) == 1
    assert "css/x.css" in problems[0]
    assert "matches no package-data glob" in problems[0]


@pytest.mark.parametrize(
    ("body", "expected"),
    [
        pytest.param('a { src: url("missing.woff2"); }', "missing.woff2", id="missing-target"),
        pytest.param('a { src: url("a\\"b.png"); }', "could not parse", id="unparseable"),
    ],
)
def test_stylesheet_problems_include_a_missing_target(
    tmp_path: Path, body: str, expected: str
) -> None:
    """The real-tree test's load clause, through the helper it calls.

    Today's stylesheets have no target that fails to load, so on the real tree
    nothing shows that :func:`_stylesheet_problems` keeps what
    :func:`_css_url_problems` reports. Without this test, dropping those
    problems would leave the real-tree check passing whatever the CSS says.
    """
    static_root = tmp_path / "ui" / "static"
    (static_root / "css").mkdir(parents=True)
    (static_root / "css" / "x.css").write_text(body, encoding="utf-8")

    problems = _stylesheet_problems(static_root, _package_data_globs("brain.ui"))

    assert len(problems) == 1
    assert "css/x.css" in problems[0]
    assert expected in problems[0]


def test_stylesheet_problems_report_an_empty_walk(tmp_path: Path) -> None:
    """No stylesheet found is a failure, never a vacuous pass."""
    static_root = tmp_path / "ui" / "static"
    static_root.mkdir(parents=True)

    problems = _stylesheet_problems(static_root, _package_data_globs("brain.ui"))

    assert len(problems) == 1
    assert "no stylesheet was found" in problems[0]


def test_a_css_url_through_a_symlink_out_of_static_fails(tmp_path: Path) -> None:
    """AC 3: a link inside ``static/`` that points outside it does not count."""
    static_root, stylesheet = _static_tree(tmp_path, 'a { src: url("leak.woff2"); }')
    outside = tmp_path / "outside.woff2"
    outside.write_bytes(b"font")
    (static_root / "css" / "leak.woff2").symlink_to(outside)

    files, problems = _css_url_problems(stylesheet, static_root)

    assert files == []
    assert len(problems) == 1
    assert "leak.woff2" in problems[0]
    assert _OUTSIDE_STATIC_DIR in problems[0]


def test_data_and_fragment_urls_pass(tmp_path: Path) -> None:
    """AC 4: ``data:`` and ``#fragment`` targets fetch no file, so they pass."""
    static_root, stylesheet = _static_tree(
        tmp_path,
        f'a {{ background: url("{_NESTED_DATA_URI}"); }}\n'
        "b { background: url(DATA:image/png;base64,AAAA); }\n"
        "c { filter: url(#shadow); }\n",
    )

    files, problems = _css_url_problems(stylesheet, static_root)

    assert files == []
    assert problems == []


def test_a_commented_out_css_url_is_not_checked(tmp_path: Path) -> None:
    """AC 5: a ``url()`` that appears only inside a comment is ignored."""
    static_root, stylesheet = _static_tree(
        tmp_path, "/* TOKENS ONLY. No url(missing.png), nothing external. */\na { color: red; }"
    )

    files, problems = _css_url_problems(stylesheet, static_root)

    assert files == []
    assert problems == []


@pytest.mark.parametrize(
    "body",
    [
        pytest.param("a { src: url(); }", id="bare"),
        pytest.param('a { src: url(""); }', id="double-quoted"),
        pytest.param("a { src: url('  '); }", id="whitespace"),
        pytest.param('a { src: url("?v=1"); }', id="query-only"),
        pytest.param('a { src: url("?#iefix"); }', id="query-and-fragment-only"),
    ],
)
def test_an_empty_css_url_fails(tmp_path: Path, body: str) -> None:
    """AC 6: a target with no path fails instead of resolving to the stylesheet itself."""
    static_root, stylesheet = _static_tree(tmp_path, body)

    files, problems = _css_url_problems(stylesheet, static_root)

    assert files == []
    assert len(problems) == 1
    assert "css/x.css" in problems[0]
    assert "names no file" in problems[0]


@pytest.mark.parametrize(
    "target",
    [pytest.param("./", id="dot-slash"), pytest.param("..", id="dot-dot")],
)
def test_a_css_url_naming_a_directory_fails(tmp_path: Path, target: str) -> None:
    """``is_file()``, not ``exists()``: a directory exists and is not an asset."""
    static_root, stylesheet = _static_tree(tmp_path, f'a {{ src: url("{target}"); }}')

    files, problems = _css_url_problems(stylesheet, static_root)

    assert files == []
    assert len(problems) == 1
