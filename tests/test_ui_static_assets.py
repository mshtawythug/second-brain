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
