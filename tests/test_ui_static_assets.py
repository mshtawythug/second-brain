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

REPO_ROOT = Path(__file__).resolve().parents[1]
PYPROJECT = REPO_ROOT / "pyproject.toml"

#: Files that live under the package but are build artefacts, not shipped data.
_IGNORED_PARTS = {"__pycache__"}


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
    assert not any(_glob_matches("static/css/app.css", g) for g in globs)
    assert any(_glob_matches("static/app.css", g) for g in globs)


def test_every_referenced_asset_exists_on_disk() -> None:
    """A typo'd href is a blank page; catch it here rather than in a browser."""
    shell = (static_dir() / "index.html").read_text(encoding="utf-8")
    referenced = re.findall(r'(?:href|src)="(/static/[^"]+)"', shell)
    assert referenced, "index.html references no static assets — suspicious"
    for reference in referenced:
        asset = static_dir() / reference.removeprefix("/static/")
        assert asset.is_file(), f"{reference} is referenced but missing"


def test_no_external_urls_anywhere_in_the_static_tree() -> None:
    """The offline guarantee, enforced rather than asserted in prose.

    ``brain ui`` must work with no network at all. A single CDN reference would
    break that silently — the page would render fine on the developer's machine
    and lose its stylesheet on a plane.
    """
    offenders = []
    for path in _shipped_files():
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


@pytest.mark.parametrize(
    ("name", "expected"),
    [("index.html", "text/html"), ("app.css", "text/css"), ("app.js", "javascript")],
)
def test_assets_have_the_expected_kind(name: str, expected: str) -> None:
    import mimetypes

    guessed, _ = mimetypes.guess_type(name)
    assert guessed is not None and expected in guessed
