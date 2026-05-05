"""Static smoke tests for the Phase 3.4 lowercase-tag rendering contract.

Background
----------

P3.4 of the Wiki UX Overhaul flips tag chips from CSS-uppercased to
lowercase rendering. Tags are stored normalized (casefold + hyphen-
separated) by ``brain.tags.normalize_tags``; rendering uppercase via
CSS contradicted that and made the chips feel shouty.

Diagnosis (logged in `_links.scss`'s P3.4 comment block) confirmed the
uppercasing source was a single CSS rule at
``quartz_overrides/quartz/styles/brain/_links.scss``: the
``a[data-brain-link-kind="tag"], a.tag-link`` block carried
``text-transform: uppercase``. The component layer (``TagList.tsx``)
already passes raw tag values through unchanged. Fix: drop the
``uppercase`` rule and replace it with an explicit
``text-transform: none`` so future upstream-default flips can't
silently uppercase tags again.

These tests are static-source only — the project doesn't run a JS /
Quartz toolchain in CI. Pattern matches
``tests/test_quartz_search_static.py`` and
``tests/test_quartz_contentindex_draft_filter.py``.
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
LINKS_SCSS = (
    REPO_ROOT
    / "quartz_overrides"
    / "quartz"
    / "styles"
    / "brain"
    / "_links.scss"
)


@pytest.fixture(scope="module")
def links_scss_source() -> str:
    """Read the Lane B `_links.scss` once per module."""
    assert LINKS_SCSS.is_file(), f"missing _links.scss at {LINKS_SCSS}"
    return LINKS_SCSS.read_text(encoding="utf-8")


def _strip_comments(text: str) -> str:
    """Drop block + line comments before scanning for declarations.

    Without this, the surrounding documentation block (which mentions
    "uppercase" multiple times for context) would trigger the
    no-uppercase assertion below. The fixture's documentation is
    explicitly allowed to discuss the historical rule; only declared
    CSS rules should be considered.
    """
    text = re.sub(r"/\*.*?\*/", "", text, flags=re.DOTALL)
    text = re.sub(r"//[^\n]*", "", text)
    return text


def _tag_pill_block(text: str) -> str:
    """Extract the body of the `a[data-brain-link-kind="tag"], a.tag-link {}`
    block.

    The fix lives inside this block, so static assertions are scoped
    here rather than running over the whole file. We pin the opening
    selector verbatim so a future rename surfaces as a clear "block
    not found" failure rather than a silent test pass.
    """
    # Selector form is two lines, both flush-left. Match the multi-line
    # comma list with a regex so future indentation/spacing tweaks
    # don't break this extractor.
    pattern = re.compile(
        r'a\[data-brain-link-kind="tag"\]\s*,\s*\n\s*a\.tag-link\s*\{',
    )
    match = pattern.search(text)
    start = match.end() - 1 if match else -1
    assert start >= 0, (
        "expected `a[data-brain-link-kind=\"tag\"], a.tag-link {` block "
        "in _links.scss — has the selector been renamed?"
    )
    # `start` points at the opening `{`; advance past it before
    # depth-walking the body so we don't immediately hit it again.
    body_start = start + 1
    depth = 1
    i = body_start
    while i < len(text) and depth > 0:
        ch = text[i]
        if ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
        i += 1
    return text[body_start : i - 1]


def test_tag_pill_block_does_not_uppercase(links_scss_source: str) -> None:
    """The tag-pill rule does not declare ``text-transform: uppercase``.

    This is the single seam between the canonical lowercased tag
    storage and the visible chip — letting any uppercase declaration
    back in here silently breaks the P3.4 contract.

    Block extraction runs on the raw source so the selector marker
    matches verbatim (stripping comments first leaves blank lines that
    misalign the marker). Content scanning then strips comments inside
    the block so the historical-context paragraph is ignored.
    """
    block = _strip_comments(_tag_pill_block(links_scss_source))
    assert "uppercase" not in block.lower(), (
        "tag-pill block must NOT contain `uppercase` — P3.4 stipulates "
        "lowercase rendering matching `brain.tags.normalize_tags`"
    )


def test_tag_pill_block_explicitly_pins_text_transform_none(
    links_scss_source: str,
) -> None:
    """The tag-pill rule explicitly declares ``text-transform: none``.

    Pinning the value (rather than just dropping the line) defends
    against a future upstream Quartz default flip and documents the
    intent in code. ``none`` is the safer pin than ``lowercase``: the
    DOM text from ``TagList.tsx`` is already lowercase, so ``none`` is
    a literal "leave the text alone" instruction.
    """
    block = _strip_comments(_tag_pill_block(links_scss_source))
    assert "text-transform: none" in block, (
        "expected `text-transform: none` declaration in the tag-pill "
        "block to defend against a future default-uppercase flip"
    )


def test_no_uppercase_text_transform_on_tag_selectors(
    links_scss_source: str,
) -> None:
    """No declaration anywhere in `_links.scss` uppercases a tag selector.

    Belt-and-suspenders: this catches the case where a future hand
    edit introduces a second tag-targeting selector (e.g. a sidebar-
    specific override) that re-uppercases the chip. We strip comments
    first so the historical-context paragraphs are ignored.
    """
    code = _strip_comments(links_scss_source)
    # Find every `text-transform: <value>;` declaration; for each, walk
    # backwards a small window and check whether a tag selector is in
    # scope.
    for match in re.finditer(
        r"text-transform\s*:\s*([a-z-]+)\s*;",
        code,
    ):
        value = match.group(1).strip().lower()
        if value != "uppercase":
            continue
        # Look back ~600 chars for a containing block opening that
        # mentions a tag selector.
        window_start = max(0, match.start() - 600)
        window = code[window_start : match.start()]
        if "tag-link" in window or 'data-brain-link-kind="tag"' in window:
            raise AssertionError(
                "found `text-transform: uppercase` in scope of a tag "
                "selector inside _links.scss — P3.4 forbids this"
            )


def test_tag_pill_block_keeps_letter_spacing(links_scss_source: str) -> None:
    """``letter-spacing: 0.04em`` is preserved (it isn't uppercase-specific).

    Why test this: the Linear variant's tag-chip aesthetic relied on
    BOTH uppercasing and letter-spacing. A future maintainer might
    delete letter-spacing as "uppercase-tracking residue" without
    realizing the chip's airy density depended on it. Pinning the
    declaration here documents that letter-spacing survived the P3.4
    scope reduction intentionally.
    """
    block = _strip_comments(_tag_pill_block(links_scss_source))
    assert "letter-spacing: 0.04em" in block, (
        "expected `letter-spacing: 0.04em` in the tag-pill block — "
        "it survived the P3.4 uppercase removal intentionally"
    )
