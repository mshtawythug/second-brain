"""Heading anchors and the table-of-contents extraction that links into them.

The contract under test is a **pair**: :func:`brain.ui.render.extract_headings`
mints the ids a TOC points at, and the renderer stamps those same ids onto the
``<h1>``…``<h6>`` elements. A test that only checked one half would pass while
the TOC pointed at nothing.

So every assertion here goes through :func:`_ids_in_html` — it counts how many
times an id actually appears in the rendered document. "The TOC target exists
**and is unique**" is the property; "an ``href="#…"`` was emitted" is not, and
would survive a slugger that hands the same id to every heading.

S4 (phase-2 defect list): the TOC must walk the **same body the renderer
walks**. ``notes_service.read_note`` renders
``strip_redundant_title_heading(body, title)``, so the caller passes the
stripped text to both — which is why neither function does any stripping of its
own, and why :func:`test_headings_come_from_the_text_passed_in` pins that the
extractor reports exactly what it was handed.
"""
from __future__ import annotations

import re

import pytest

from brain.ui.render import Heading, extract_headings, render_markdown

#: Pure string-in, string-out — no connection, no fixtures, no filesystem, so
#: this module never takes the machine-wide test-DB lock.
pytestmark = pytest.mark.nodb

_ID_ATTR = re.compile(r'\bid="([^"]*)"')


def _ids_in_html(html: str) -> list[str]:
    """Every ``id`` attribute value in ``html``, in document order."""
    return _ID_ATTR.findall(html)


def _assert_toc_targets_resolve(source: str) -> list[Heading]:
    """Every extracted heading id appears EXACTLY ONCE in the rendered HTML.

    Returns the headings so a caller can go on to assert about their text or
    level without re-extracting.
    """
    headings = extract_headings(source)
    ids = _ids_in_html(render_markdown(source))
    for heading in headings:
        assert ids.count(heading.id) == 1, (
            f"TOC entry {heading.text!r} points at #{heading.id}, "
            f"which appears {ids.count(heading.id)} times in the HTML"
        )
    return headings


def test_duplicate_heading_text_gets_distinct_resolvable_ids() -> None:
    """Two identically-titled headings must not share an anchor.

    This is the mutation target: drop the duplicate suffix and both headings
    collide, so one id resolves twice and the count assertion fails.
    """
    source = "## Notes\n\ntext\n\n## Notes\n\nmore\n"

    headings = _assert_toc_targets_resolve(source)

    assert len(headings) == 2
    assert headings[0].id != headings[1].id


def test_three_way_duplicate_ids_are_all_distinct() -> None:
    source = "# Log\n\n# Log\n\n# Log\n"

    headings = _assert_toc_targets_resolve(source)

    assert len({h.id for h in headings}) == 3


def test_a_generated_suffix_cannot_collide_with_an_authored_heading() -> None:
    """``## Notes`` twice plus an authored ``## Notes 1`` still resolves.

    A naive counter mints ``notes-1`` for the second ``Notes`` and the authored
    heading slugifies to the same string. Uniqueness is a property of the whole
    document, not of one base name.
    """
    source = "## Notes\n\n## Notes 1\n\n## Notes\n"

    headings = _assert_toc_targets_resolve(source)

    assert len({h.id for h in headings}) == 3


def test_heading_level_and_text_are_reported() -> None:
    source = "# Top\n\n### Deep dive\n"

    headings = extract_headings(source)

    assert [(h.level, h.text) for h in headings] == [(1, "Top"), (3, "Deep dive")]


def test_inline_markup_is_flattened_out_of_the_heading_text() -> None:
    """A TOC shows text, not markup — and the id is minted from that text."""
    source = "## Design **notes** and `code`\n"

    headings = _assert_toc_targets_resolve(source)

    assert headings[0].text == "Design notes and code"
    assert "*" not in headings[0].id
    assert "`" not in headings[0].id


def test_wikilink_label_survives_into_the_heading_text() -> None:
    source = "## See [[Weekly Review|the review]]\n"

    headings = _assert_toc_targets_resolve(source)

    assert headings[0].text == "See the review"


def test_headings_come_from_the_text_passed_in() -> None:
    """S4: no hidden stripping — the caller decides what body is walked.

    ``notes_service`` hands the title-stripped body to the renderer; handing the
    same string here is what keeps the TOC and the HTML in agreement. If this
    function stripped anything on its own the two would drift apart again.
    """
    stripped = "## Body heading\n"
    unstripped = "# Note title\n\n## Body heading\n"

    assert [h.text for h in extract_headings(stripped)] == ["Body heading"]
    assert [h.text for h in extract_headings(unstripped)] == [
        "Note title",
        "Body heading",
    ]


def test_headings_inside_a_blockquote_are_reachable() -> None:
    """The renderer stamps them, so the extractor must list them."""
    source = "> ## Quoted heading\n"

    headings = _assert_toc_targets_resolve(source)

    assert [h.text for h in headings] == ["Quoted heading"]


def test_a_hash_inside_a_fence_is_not_a_heading() -> None:
    source = "```python\n# not a heading\n```\n"

    assert extract_headings(source) == []
    assert _ids_in_html(render_markdown(source)) == []


def test_empty_and_missing_bodies_yield_no_headings() -> None:
    assert extract_headings("") == []
    assert extract_headings(None) == []
    assert extract_headings("just a paragraph\n") == []


def test_heading_text_that_slugifies_to_nothing_still_gets_unique_ids() -> None:
    """Punctuation-only headings cannot all share one fallback id."""
    source = "## ...\n\n## ???\n"

    headings = _assert_toc_targets_resolve(source)

    assert headings[0].id != headings[1].id


def test_script_in_a_heading_cannot_escape_the_id_attribute() -> None:
    """A3: the new attribute path is an injection surface — pin it shut."""
    source = '## <script>alert(1)</script> "onmouseover=x\n'

    html = render_markdown(source)

    assert "<script>" not in html
    for value in _ids_in_html(html):
        assert re.fullmatch(r"[a-z0-9-]+", value), value
    assert extract_headings(source)[0].id == _ids_in_html(html)[0]
