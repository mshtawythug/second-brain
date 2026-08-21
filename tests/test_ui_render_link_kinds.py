"""``data-brain-link-kind`` stamping — the Python port of ``linkKindMark.ts``.

The wiki overlay gives five link kinds five distinct visual treatments by
stamping one attribute; the inspector needs the same attribute or the same
stylesheet cannot be ported. What is asserted here is the **classification
order**, because that is what a careless port loses: ``tags/x`` is a relative
URL and would fall through to ``wiki`` — or, if ``external`` were tested first,
an absolute ``https://…/tags/x`` would stop being a tag.

Each case is asserted as *the* kind, never as "has an attribute": a stamp with
the wrong value is the failure mode worth catching, and an existence check
survives it.
"""
from __future__ import annotations

import re

import pytest

from brain.ui.render import LINK_KIND_ATTR, classify_link_kind, render_markdown

#: Pure string-in, string-out; no test-DB lock. See ``test_ui_render.py``.
pytestmark = pytest.mark.nodb

_KIND_ATTR = re.compile(rf'\b{re.escape(LINK_KIND_ATTR)}="([^"]*)"')


def _kinds(html: str) -> list[str]:
    """Every stamped kind in ``html``, in document order."""
    return _KIND_ATTR.findall(html)


def _only_kind(source: str) -> str:
    """Render ``source``, assert it produced exactly one stamped link."""
    kinds = _kinds(render_markdown(source))
    assert len(kinds) == 1, f"expected one stamped link, got {kinds}"
    return kinds[0]


# --- the classifier itself -------------------------------------------------


@pytest.mark.parametrize(
    ("url", "kind"),
    [
        ("tags/retro", "tag"),
        ("/tags/retro", "tag"),
        ("./tags/retro", "tag"),
        ("http://example.invalid/x", "external"),
        ("https://example.invalid/x", "external"),
        ("mailto:fixture@example.invalid", "external"),
        ("_ingested/krisp/2026-01-02-abcdef12-standup.md", "ingested"),
        ("/_ingested/slack/thread.md", "ingested"),
        ("./_ingested/gmail/thread.md", "ingested"),
        ("notes/2026-01-02.md", "wiki"),
        ("#a-heading", "wiki"),
        ("", "wiki"),
    ],
)
def test_classify_link_kind_buckets(url: str, kind: str) -> None:
    assert classify_link_kind(url) == kind


def test_prefix_matching_is_case_insensitive() -> None:
    """Hand-edited markdown carries ``HTTPS://`` and ``MailTo:``."""
    assert classify_link_kind("HTTPS://example.invalid") == "external"
    assert classify_link_kind("MailTo:fixture@example.invalid") == "external"
    assert classify_link_kind("Tags/Retro") == "tag"


def test_tag_wins_over_external_for_an_absolute_tag_url() -> None:
    """The ORDER, pinned: reorder the classifier and this is what breaks.

    ``https://…/tags/x`` is both an external URL and a tag URL — the latter by
    INFIX, not by prefix. That distinction is the whole reason this test can
    exist: prefix matching is exactly what fails to see the tag here, and while
    the classifier was prefix-only the two sets were disjoint, so the ordering
    was inert and no test could have defended it. The overlay resolves the
    genuine overlap by testing ``tag`` first, and the stylesheet's tag treatment
    depends on it.

    MUTATION (Appendix C-1's prescribed substitute, run 2026-08-20 — the row had
    carried the substitute test but no observed run):
    ``render.py:181`` ``return lowered.startswith(_TAG_PREFIXES) or _TAG_INFIX in
    lowered`` -> ``return lowered.startswith(_TAG_PREFIXES)``
    -> **2 failed, 24 passed** (baseline: 26 passed).

    TWO, where C-1 predicted one — and the second is the more useful half.
    Alongside this test, ``test_classify_link_kind_buckets[/tags/retro-tag]``
    reddens, because ``_TAG_PREFIXES`` deliberately omits the leading-slash
    ``/tags/`` that the overlay's ``TAG_PREFIXES`` lists: ``_TAG_INFIX`` *is*
    that string and matches it at offset 0. So the infix constant carries the
    absolute host-qualified form AND the leading-slash form, and deleting it
    breaks a case that looks like plain prefix matching.

    Scope, measured on the same run rather than assumed: widened to
    ``test_ui_render.py`` and ``test_ui_render_toc.py`` the mutation reads
    **2 failed, 88 passed** (baseline: 90 passed) — the same two tests and
    nothing else. No other render behaviour depends on the constant.
    """
    assert classify_link_kind("https://example.invalid/tags/retro") == "tag"


def test_ingested_wins_over_the_wiki_fallback() -> None:
    """``ingested`` is a refinement of ``wiki`` — both are vault-internal."""
    assert classify_link_kind("_ingested/krisp/note.md") == "ingested"


# --- stamping, through the real renderer -----------------------------------


def test_markdown_link_kinds_are_stamped() -> None:
    assert _only_kind("[t](tags/retro)") == "tag"
    assert _only_kind("[e](http://example.invalid/x)") == "external"
    assert _only_kind("[i](_ingested/krisp/note.md)") == "ingested"
    assert _only_kind("[w](notes/2026-01-02.md)") == "wiki"


def test_wikilinks_are_stamped_by_their_target() -> None:
    """A wiki link is not an href, so the TARGET is what gets classified."""
    assert _only_kind("[[Weekly Review]]") == "wiki"
    assert _only_kind("[[tags/retro]]") == "tag"
    assert _only_kind("[[_ingested/krisp/note.md]]") == "ingested"


def test_a_resolved_wikilink_is_stamped_too() -> None:
    html = render_markdown("[[Weekly Review]]", resolver=lambda _t: "doc-1234")

    assert "?id=doc-1234" in html
    assert _kinds(html) == ["wiki"]


def test_an_aliased_wikilink_is_classified_by_target_not_alias() -> None:
    assert _only_kind("[[tags/retro|see the retro tag]]") == "tag"


def test_every_link_in_a_document_is_stamped() -> None:
    source = (
        "- [a](tags/retro)\n"
        "- [b](https://example.invalid/x)\n"
        "- [[_ingested/krisp/note.md]]\n"
        "- [[Weekly Review]]\n"
    )

    assert _kinds(render_markdown(source)) == ["tag", "external", "ingested", "wiki"]


def test_a_link_inside_a_code_fence_is_not_stamped() -> None:
    assert _kinds(render_markdown("```\n[[Weekly Review]]\n```\n")) == []


def test_a_blocked_scheme_is_not_given_a_kind() -> None:
    """A link the allowlist rejects must not be dressed up as a normal one.

    ``ftp:`` passes markdown-it's own ``validateLink`` but fails the render
    module's scheme allowlist, so it is the case that actually reaches the
    blocked branch.
    """
    html = render_markdown("[f](ftp://example.invalid/x)")

    assert "link--blocked" in html
    assert _kinds(html) == []


# --- A3: the new attribute path as an injection surface --------------------


def test_a_quote_in_a_url_cannot_break_out_of_the_kind_attribute() -> None:
    source = '[x](tags/retro"onmouseover="alert(1))'

    html = render_markdown(source)

    assert _kinds(html) == ["tag"]
    # The quote never survives as a live attribute delimiter: whatever the
    # normaliser does to it (percent-encode or escape), no second attribute
    # appears in the anchor.
    assert 'onmouseover="alert(1)"' not in html
    assert '"onmouseover=' not in html


def test_a_quote_in_a_wikilink_target_cannot_break_out() -> None:
    html = render_markdown('[[tags/retro" onmouseover="alert(1)]]')

    assert _kinds(html) == ["tag"]
    assert 'onmouseover="alert(1)"' not in html


def test_the_kind_is_always_one_of_the_known_values() -> None:
    """No caller-controlled string ever reaches the attribute value."""
    source = (
        '[a](tags/x"y)\n\n'
        "[[<script>alert(1)</script>]]\n\n"
        "[b](https://example.invalid/<script>)\n"
    )

    for kind in _kinds(render_markdown(source)):
        assert kind in {"tag", "external", "ingested", "wiki"}


def test_script_in_a_link_label_is_still_escaped() -> None:
    html = render_markdown("[[Weekly<script>alert(1)</script>]]")

    assert "<script>" not in html
    assert "&lt;script&gt;" in html
