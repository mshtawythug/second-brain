"""Packaging and offline guarantees for the `brain ui` static assets.

This repository has already shipped one broken wheel for exactly this class of
mistake — commit ``ed8195f``, *"ship migrations inside the wheel so brain init
works on pip installs"*. Static assets carry identical risk with a worse failure
mode: a **blank page and no error**, working perfectly in the dev tree because
an editable install reads straight from ``src/``.

So the glob-coverage test below reads the real ``package-data`` patterns out of
``pyproject.toml`` and asserts every shipped file matches at least one. It needs
no wheel build, runs in milliseconds, and turns red the moment someone adds an
asset the declared globs do not cover — including a future ``static/img/``
subdirectory, which the current flat patterns would silently drop.
"""
from __future__ import annotations

import re
import tomllib
from pathlib import Path
from urllib.parse import unquote, urljoin, urlsplit

import pytest

from brain.ui.app import static_dir

#: Opens NO database connection — this module reads files off disk and
#: parses them. The marker lets the session skip the schema reset and, more
#: importantly, the MACHINE-WIDE advisory lock; see
#: ``conftest._session_touches_the_database``.
pytestmark = pytest.mark.nodb

REPO_ROOT = Path(__file__).resolve().parents[1]
PYPROJECT = REPO_ROOT / "pyproject.toml"

#: Files that live under the package but are build artefacts, not shipped data.
_IGNORED_PARTS = {"__pycache__"}

#: The three ways a shipped module names another file, all of which must resolve.
#:
#: THREE PATTERNS, NOT ONE, and the second and third are not hypothetical
#: completeness. The ``from``-bearing form was the only one matched, so
#: ``js/marginalia.js``'s ``await import("/static/js/inspector.js")`` — a real,
#: shipped, LAZY import — was invisible to the resolver. That is the worst
#: variant to miss: a static-import typo blanks the page on load and is noticed
#: immediately, while a dynamic one resolves long after the note paints and
#: breaks only the surface that awaited it.
#:
#: ``import "x"`` (side-effect, no bindings) is included for the same reason
#: even though nothing ships one today — it is one line away, and the cost of
#: covering it now is a regex alternation.
_IMPORT_FROM_RE = re.compile(
    r"""(?:^|\s)(?:import|export)\b[^;]*?from\s+["']([^"']+)["']"""
)
_IMPORT_SIDE_EFFECT_RE = re.compile(r"""(?:^|\s)import\s+["']([^"']+)["']""")
_IMPORT_DYNAMIC_RE = re.compile(r"""\bimport\s*\(\s*["']([^"']+)["']\s*\)""")
_IMPORT_PATTERNS = (_IMPORT_FROM_RE, _IMPORT_SIDE_EFFECT_RE, _IMPORT_DYNAMIC_RE)


def _import_specifiers(source: str) -> list[str]:
    """Every module specifier ``source`` names, in any of the three forms."""
    return [
        specifier
        for pattern in _IMPORT_PATTERNS
        for specifier in pattern.findall(source)
    ]


#: Suffixes whose bytes are not text and cannot be decoded as UTF-8.
#:
#: Used ONLY to skip the source-scanning tests below — never to filter
#: :func:`_shipped_files`, because the packaging guard must keep seeing these.
#: Dropping a binary from that walk is precisely how a PNG would slip out of the
#: wheel unnoticed, which is the failure this module exists to prevent.
#:
#: ``.svg`` is deliberately absent: it is XML, it is scanned as text, and it is
#: the one image format that *can* carry an external fetch (``<image href=…>``).
_BINARY_SUFFIXES = {".png", ".ico"}


def _glob_matches(relative: str, pattern: str) -> bool:
    """Match a setuptools ``package-data`` pattern against a relative path.

    Deliberately NOT :func:`fnmatch.fnmatch`. ``fnmatch`` translates ``*`` to
    ``.*``, which happily crosses a directory separator — verified:
    ``fnmatch("static/css/app.css", "static/*.css")`` returns **True**. setuptools
    resolves these patterns with :mod:`glob`, where ``*`` stops at ``/``, so
    ``fnmatch`` would report a nested asset as packaged when it is not — turning
    this whole module into a test that cannot fail.

    ``*`` therefore becomes ``[^/]*`` and ``?`` becomes ``[^/]``.
    """
    regex = "".join(
        "[^/]*" if ch == "*" else "[^/]" if ch == "?" else re.escape(ch)
        for ch in pattern
    )
    return re.fullmatch(regex, relative) is not None


def _package_data_globs(package: str) -> list[str]:
    with PYPROJECT.open("rb") as handle:
        data = tomllib.load(handle)
    globs = data["tool"]["setuptools"]["package-data"].get(package)
    assert globs, f"pyproject declares no package-data for {package!r}"
    return list(globs)


def _shipped_files() -> list[Path]:
    package_root = Path(str(static_dir())).parent
    return [
        path
        for path in package_root.rglob("*")
        if path.is_file()
        and not path.name.endswith(".py")
        and not _IGNORED_PARTS & set(path.parts)
    ]


def test_static_dir_resolves_through_importlib_resources() -> None:
    """The `ed8195f` regression class: assets must be locatable as package data."""
    resolved = static_dir()
    assert resolved.is_dir()
    assert (resolved / "index.html").is_file()


def test_every_shipped_asset_matches_a_declared_glob() -> None:
    """The durable guard against a silently unpackaged asset.

    Uses :func:`_glob_matches`, whose ``*`` stops at ``/`` — matching what
    setuptools actually does, and which is exactly why a nested
    ``static/css/app.css`` would NOT match ``static/*.css``.
    """
    globs = _package_data_globs("brain.ui")
    package_root = Path(str(static_dir())).parent

    unmatched = []
    for path in _shipped_files():
        relative = path.relative_to(package_root).as_posix()
        if not any(_glob_matches(relative, pattern) for pattern in globs):
            unmatched.append(relative)

    assert not unmatched, (
        "these files live under src/brain/ui/ but match no package-data glob in "
        f"pyproject.toml, so they would be MISSING from the wheel: {unmatched}. "
        f"Declared globs: {globs}. Either move the file to a covered location "
        f"or ask the pyproject owner to widen the patterns."
    )


def test_a_nested_asset_would_be_caught() -> None:
    """Guard the guard: prove the matcher rejects a nested path.

    Without this, a bug that made every path 'match' would leave the test above
    permanently green and useless.
    """
    globs = _package_data_globs("brain.ui")
    # A path one level deeper than any declared pattern. This used to be
    # "static/css/app.css", which stopped being uncovered the moment the
    # stylesheet was split into static/css/ and "static/css/*.css" was declared
    # -- the assertion was correct and failed loudly, which is the behaviour
    # this file exists to produce. The example has to be a directory NOBODY
    # declares, or the test measures the glob list instead of the matcher.
    assert not any(_glob_matches("static/css/theme/dark.css", g) for g in globs), (
        "a twice-nested path matched a declared glob, so `*` is crossing `/` "
        "and the coverage test above cannot detect an unshipped asset"
    )
    assert not any(_glob_matches("static/img/logo.png", g) for g in globs), (
        "static/img/ is not declared, yet a path under it matched"
    )
    # ...while the two shapes that ARE declared still match, so the matcher is
    # not simply rejecting everything -- which would also make the test above
    # vacuously green.
    # All three declared shapes must still match, so the matcher is not simply
    # rejecting everything. `static/js/main.js` is the one the JS split added:
    # it is the module ENTRY POINT, and without "static/js/*.js" the wheel
    # ships a page whose whole module graph 404s.
    assert any(_glob_matches("static/theme.js", g) for g in globs)
    assert any(_glob_matches("static/js/main.js", g) for g in globs)
    assert any(_glob_matches("static/css/tokens.css", g) for g in globs)


def test_binary_assets_stay_inside_the_packaging_walk() -> None:
    """Guard the guard: ``_BINARY_SUFFIXES`` must not shrink the packaging walk.

    The source-scanning tests skip binaries because PNG bytes are not UTF-8. The
    tempting "fix" for that is to filter binaries out of :func:`_shipped_files`
    instead — which would quietly exempt every image from the glob-coverage
    check above, reintroducing the exact ``ed8195f`` failure the module guards.

    So: assert the walk is exhaustive. Every non-Python file on disk under the
    package appears in it, binary or not.
    """
    package_root = Path(str(static_dir())).parent
    on_disk = {
        path
        for path in package_root.rglob("*")
        if path.is_file()
        and not path.name.endswith(".py")
        and not _IGNORED_PARTS & set(path.parts)
    }
    missing = on_disk - set(_shipped_files())
    assert not missing, f"_shipped_files() skipped real assets: {sorted(missing)}"

    binaries = {p for p in _shipped_files() if p.suffix.lower() in _BINARY_SUFFIXES}
    assert binaries, (
        "no binary asset is present, so this guard proves nothing — if the "
        "branding images were removed on purpose, remove this test with them"
    )


def test_every_referenced_asset_exists_on_disk() -> None:
    """A typo'd href is a blank page; catch it here rather than in a browser."""
    shell = (static_dir() / "index.html").read_text(encoding="utf-8")
    referenced = re.findall(r'(?:href|src)="(/static/[^"]+)"', shell)
    assert referenced, "index.html references no static assets — suspicious"
    for reference in referenced:
        asset = static_dir() / reference.removeprefix("/static/")
        assert asset.is_file(), f"{reference} is referenced but missing"


def test_every_es_module_import_resolves_to_a_file() -> None:
    """A mistyped import specifier is a BLANK PAGE with a fully green suite.

    ``test_every_referenced_asset_exists_on_disk`` above only scans
    ``index.html`` for ``href``/``src`` attributes. An ES module specifier is
    neither — ``import {...} from "/static/tree_nav.js"`` lives inside a ``.js``
    file. So a single typo (``tree-nav.js``) means the module body never
    executes, ``boot()`` never runs, and the entire front end renders nothing —
    while the wheel still builds, the ``static/*.js`` glob still matches, and
    every other test still passes.

    This resolves each specifier to a real file instead — in all three import
    forms, static, side-effect and dynamic. See :data:`_IMPORT_PATTERNS`.
    """
    checked = 0
    for path in _shipped_files():
        if path.suffix != ".js":
            continue
        for specifier in _import_specifiers(path.read_text(encoding="utf-8")):
            assert specifier.startswith("/static/"), (
                f"{path.name} imports {specifier!r}, which is not a same-origin "
                f"/static/ path — the CSP forbids anything else"
            )
            target = static_dir() / specifier.removeprefix("/static/")
            assert target.is_file(), (
                f"{path.name} imports {specifier!r} but no such file exists. "
                f"The module body would never execute and the page would be blank."
            )
            checked += 1
    assert checked, (
        "no ES import was found in any shipped .js file — this test proves "
        "nothing; if the modules were inlined on purpose, remove it with them"
    )


def test_the_import_resolver_would_catch_a_typo() -> None:
    """Guard the guard: one positive per import form, and one shared negative.

    One case per form, because the forms fail independently — the ``from``
    pattern matched a real dynamic import zero times while looking entirely
    healthy against the static ones.
    """
    multiline = 'import {\n  flattenVisible, rovingIndex,\n} from "/static/tree_nav.js";'
    assert _import_specifiers(multiline) == ["/static/tree_nav.js"], (
        "the specifier regex does not match a multi-line import, so it would "
        "silently check nothing"
    )
    side_effect = 'import "/static/js/ledger_status.js";'
    assert _import_specifiers(side_effect) == ["/static/js/ledger_status.js"], (
        "a side-effect import names a file like any other; missing it means an "
        "unresolvable specifier ships unchecked"
    )
    dynamic = 'const inspector = await import("/static/js/inspector.js");'
    assert _import_specifiers(dynamic) == ["/static/js/inspector.js"], (
        "a dynamic import is not matched — js/marginalia.js ships one, and a "
        "typo there breaks the marginalia panel only, long after the note paints"
    )
    # The negative sample must CONTAIN the keyword, or any regex requiring
    # `import` returns [] and this proves nothing. Here `import` appears inside
    # a string literal that is not an import statement.
    assert _import_specifiers('const doc = "import x from \\"/static/ghost.js\\"";') == [], (
        "the regex matches an import-shaped string literal, so it would report "
        "a phantom dependency that does not exist"
    )


def test_no_external_urls_anywhere_in_the_static_tree() -> None:
    """The offline guarantee, enforced rather than asserted in prose.

    ``brain ui`` must work with no network at all. A single CDN reference would
    break that silently — the page would render fine on the developer's machine
    and lose its stylesheet on a plane.
    """
    offenders = []
    for path in _shipped_files():
        if path.suffix.lower() in _BINARY_SUFFIXES:
            continue
        text = path.read_text(encoding="utf-8")
        for number, line in enumerate(text.splitlines(), start=1):
            stripped = line.strip()
            # Comment lines may legitimately cite a URL (spec links, licences).
            if stripped.startswith(("*", "//", "<!--", "#")):
                continue
            if "http://" in line or "https://" in line:
                # An XML namespace is a identifier, not a fetch.
                if "www.w3.org" in line:
                    continue
                offenders.append(f"{path.name}:{number}: {stripped[:80]}")
    assert not offenders, f"external URLs found in the static tree: {offenders}"


def test_no_inline_script_or_style_in_the_shell() -> None:
    """The CSP guarantee.

    ``default-src 'none'; script-src 'self'; style-src 'self'`` is only
    sustainable with no inline script and no inline style. The moment one
    appears, the policy needs a nonce or ``'unsafe-inline'`` — so this test is
    what keeps the strict CSP honest.
    """
    raw = (static_dir() / "index.html").read_text(encoding="utf-8")
    # Strip HTML comments first: this file documents the no-inline rule in
    # prose, and the word "<script>" inside a comment is not a script tag.
    shell = re.sub(r"<!--.*?-->", "", raw, flags=re.DOTALL)
    assert not re.search(r"<script(?![^>]*\bsrc=)[^>]*>", shell), (
        "index.html contains an inline <script>; the CSP forbids it"
    )
    assert "<style" not in shell, "index.html contains an inline <style>"
    assert not re.search(r'\sstyle="', shell), (
        "index.html contains a style attribute"
    )
    assert not re.search(r'\son[a-z]+="', shell), (
        "index.html contains an inline event handler"
    )


def _shipped_by_suffix(suffix: str) -> list[str]:
    """Every shipped asset with ``suffix``, as a static-relative posix path.

    DERIVED from disk rather than listed. The hand-written version named two of
    the ten shipped ``.js`` files, so eight modules — the whole of ``js/``
    except ``main.js`` — had no Content-Type coverage at all, and adding a
    ninth module would have inherited the same silence. A roster has to be
    maintained to stay true; a query is true by construction.
    """
    root = Path(str(static_dir()))
    return sorted(
        path.relative_to(root).as_posix()
        for path in root.rglob(f"*{suffix}")
        if path.is_file() and not _IGNORED_PARTS & set(path.parts)
    )


@pytest.mark.parametrize(
    ("suffix", "expected", "least"),
    [(".html", "text/html", 1), (".css", "text/css", 4), (".js", "javascript", 10)],
)
def test_every_shipped_asset_of_a_kind_resolves_to_the_right_content_type(
    suffix: str, expected: str, least: int
) -> None:
    """A wrong Content-Type on a module is a blank page: browsers REFUSE to
    execute ``text/plain`` from a ``<script type="module">``.

    ``least`` guards the query itself. Without it a glob that silently matched
    nothing would make this pass over an empty list — the vacuous-green shape
    this file exists to prevent. It is a FLOOR, not an equality, so adding a
    module does not break an unrelated test; the packaging guard above is what
    notices a new file, and it is exact.
    """
    import mimetypes

    names = _shipped_by_suffix(suffix)
    assert len(names) >= least, (
        f"expected at least {least} shipped {suffix} files, found {len(names)}: "
        f"{names}. Either assets went missing or the glob stopped matching — "
        f"and a Content-Type check over an empty list proves nothing."
    )
    wrong = {
        name: mimetypes.guess_type(name)[0]
        for name in names
        if expected not in (mimetypes.guess_type(name)[0] or "")
    }
    assert not wrong, (
        f"these shipped assets do not resolve to a {expected!r} Content-Type: "
        f"{wrong}. A module served as text/plain is refused by the browser and "
        f"the page renders blank."
    )


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
_CSS_URL_RE = re.compile(
    r"""\burl\(\s*(?:"([^"]*)"|'([^']*)'|([^\s"'()]*))\s*\)""", re.IGNORECASE
)
_CSS_URL_OPEN_RE = re.compile(r"\burl\(", re.IGNORECASE)

#: The served origin a stylesheet's targets are joined against, as a browser
#: joins them. Only its identity matters: a target that leaves it is external.
_UI_ORIGIN = "http://brain-ui.invalid"
_STATIC_URL_PREFIX = "/static/"


def _css_url_targets(css: str) -> list[str]:
    """Every ``url()`` target in ``css``, comments excluded, whitespace stripped.

    Raises :class:`ValueError` when a ``url(`` is left over that the pattern
    could not parse (an escaped quote, a quote inside a bare target). Without
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

    An EMPTY target is a problem in its own right: joined as a URL it names the
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
        if not target:
            problems.append(f"{where} is empty, so it names no file")
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
            problems.append(f"{where} resolves through a link to {candidate}, outside static/")
        elif not candidate.is_file():
            problems.append(f"{where} resolves to {path}, which is not a file")
        else:
            files.append(candidate)
    return files, problems


def test_every_css_url_resolves_on_disk() -> None:
    """A typo'd ``url()`` in a stylesheet is a missing font or image; catch it here.

    ``test_every_referenced_asset_exists_on_disk`` reads only ``index.html``,
    so until RM-05 a stylesheet could name a file that does not exist and
    every test stayed green. Every shipped ``.css`` file is checked, not just
    ``css/*.css``: pyproject also declares a top-level ``static/*.css``.

    Each resolved file must also match a declared ``package-data`` glob, so a
    font in a directory the wheel does not ship fails HERE, naming the
    stylesheet that needs it, not only in the packaging test above.

    No "at least one ``url()``" floor: :func:`_css_url_targets` raises on any
    ``url(`` it cannot parse, so a pattern that stopped matching is already a
    failure, and a stylesheet with no ``url(`` has nothing to check.
    """
    static_root = static_dir()
    package_root = static_root.parent
    globs = _package_data_globs("brain.ui")
    stylesheets = _shipped_by_suffix(".css")
    assert stylesheets, "no shipped stylesheet was found, so this test checks nothing"

    problems: list[str] = []
    for name in stylesheets:
        files, found = _css_url_problems(static_root / name, static_root)
        problems.extend(found)
        for target in files:
            relative = target.relative_to(package_root.resolve()).as_posix()
            if not any(_glob_matches(relative, pattern) for pattern in globs):
                problems.append(
                    f"{name}: {relative} exists but matches no package-data glob, "
                    f"so the wheel would not ship it"
                )
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
        pytest.param(
            "a { src: url(a.woff2) format('woff2'), url('b.woff') format('woff'); }",
            ["a.woff2", "b.woff"],
            id="several-per-line",
        ),
        pytest.param("a { src: URL(a.png); }", ["a.png"], id="upper-case"),
        pytest.param("/* no url(x.png) here */ a { color: red; }", [], id="comment"),
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
    ],
)
def test_an_unparseable_css_url_raises(css: str) -> None:
    """A ``url(`` the pattern cannot parse is a failure, never a silent skip."""
    with pytest.raises(ValueError, match="url"):
        _css_url_targets(css)


def _static_tree(root: Path, stylesheet_body: str) -> tuple[Path, Path]:
    """A synthetic ``static/`` with one stylesheet and two real assets.

    Returns ``(static_root, stylesheet)``. ``css/fonts/a.woff2`` and
    ``img/logo.png`` exist, so a test can tell a resolver that rejects
    everything from one that resolves.
    """
    static_root = root / "static"
    (static_root / "css" / "fonts").mkdir(parents=True)
    (static_root / "img").mkdir()
    (static_root / "css" / "fonts" / "a b.woff2").write_bytes(b"font")
    (static_root / "css" / "fonts" / "a.woff2").write_bytes(b"font")
    (static_root / "img" / "logo.png").write_bytes(b"png")
    stylesheet = static_root / "css" / "x.css"
    stylesheet.write_text(stylesheet_body, encoding="utf-8")
    return static_root, stylesheet


def test_a_missing_css_url_target_is_named(tmp_path: Path) -> None:
    """AC 2: the failure names the stylesheet and the target."""
    static_root, stylesheet = _static_tree(tmp_path, 'a { src: url("missing.woff2"); }')

    files, problems = _css_url_problems(stylesheet, static_root)

    assert files == []
    assert len(problems) == 1
    assert "css/x.css" in problems[0]
    assert "missing.woff2" in problems[0]


@pytest.mark.parametrize(
    "target",
    [
        pytest.param("fonts/a.woff2", id="sibling-directory"),
        pytest.param("../img/logo.png", id="parent-then-sibling"),
        pytest.param("/static/img/logo.png", id="root-relative"),
        pytest.param("fonts/a.woff2?v=1#iefix", id="query-and-fragment"),
        pytest.param("fonts/a%20b.woff2", id="percent-encoded"),
    ],
)
def test_resolvable_css_urls_pass(tmp_path: Path, target: str) -> None:
    """Guard the guard: real targets resolve, so a reject-everything resolver fails."""
    static_root, stylesheet = _static_tree(tmp_path, f'a {{ src: url("{target}"); }}')

    files, problems = _css_url_problems(stylesheet, static_root)

    assert problems == []
    assert len(files) == 1
    assert files[0].is_file()


@pytest.mark.parametrize(
    "target",
    [
        pytest.param("../../app.py", id="parent-of-static"),
        pytest.param("../../../../etc/hosts", id="far-above-root"),
        pytest.param("/elsewhere/a.woff2", id="root-relative-outside-static"),
        pytest.param("//cdn.invalid/a.woff2", id="protocol-relative"),
        pytest.param("https://cdn.invalid/a.woff2", id="other-origin"),
        # The path names a real local file: only the origin check rejects these.
        pytest.param("https://cdn.invalid/static/img/logo.png", id="other-origin-static-path"),
        pytest.param("//cdn.invalid/static/img/logo.png", id="protocol-relative-static-path"),
    ],
)
def test_a_css_url_escaping_static_fails(tmp_path: Path, target: str) -> None:
    """AC 3: a target outside ``static_dir()`` fails, even when the file exists."""
    static_root, stylesheet = _static_tree(tmp_path, f'a {{ src: url("{target}"); }}')
    (tmp_path / "app.py").write_text("", encoding="utf-8")

    files, problems = _css_url_problems(stylesheet, static_root)

    assert files == []
    assert len(problems) == 1
    assert "css/x.css" in problems[0]
    assert target in problems[0]


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
    ],
)
def test_an_empty_css_url_fails(tmp_path: Path, body: str) -> None:
    """AC 6: an empty target fails instead of resolving to the stylesheet or its directory."""
    static_root, stylesheet = _static_tree(tmp_path, body)

    files, problems = _css_url_problems(stylesheet, static_root)

    assert files == []
    assert len(problems) == 1
    assert "css/x.css" in problems[0]
    assert "empty" in problems[0]


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
