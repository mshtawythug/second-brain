"""The markdown renderer's XSS and wiki-link behaviour.

Every assertion here is on **rendered output**, never on the options dict. That
is deliberate: an assertion that ``options["html"] is False`` would still pass
if someone swapped the preset for one that escapes nothing, whereas rendering
``<script>`` and checking the escaping survives any refactor.

The rule exists because the F14 design document asserted that
``MarkdownIt("commonmark")`` defaults to ``html=False``. It does not — on
markdown-it-py 4.2.0 that preset renders raw HTML straight through. Had the code
trusted the document, the XSS defence the document describes would have been
absent while reading as present.
"""
from __future__ import annotations

from brain.ui.render import render_markdown


def test_script_tag_is_escaped_not_executed() -> None:
    html = render_markdown("<script>alert(1)</script>")
    assert "&lt;script&gt;" in html
    assert "<script>" not in html


def test_img_onerror_is_escaped() -> None:
    html = render_markdown('<img src=x onerror="alert(1)">')
    assert "<img" not in html
    assert "&lt;img" in html


def test_javascript_href_produces_no_anchor() -> None:
    html = render_markdown("[click](javascript:alert(1))")
    assert "<a" not in html
    assert "javascript:" not in html.lower() or "href" not in html


def test_javascript_href_is_case_folded() -> None:
    """Mixed case must not evade the scheme check."""
    html = render_markdown("[c](JaVaScRiPt:alert(1))")
    assert "<a" not in html


def test_vbscript_and_file_schemes_produce_no_anchor() -> None:
    for source in ("[v](vbscript:x)", "[f](file:///etc/passwd)"):
        assert "<a" not in render_markdown(source)


def test_data_html_image_is_escaped() -> None:
    html = render_markdown("![i](data:text/html,<script>x</script>)")
    assert "<script>" not in html


def test_safe_schemes_still_render() -> None:
    assert '<a href="https://example.com">' in render_markdown(
        "[ok](https://example.com)"
    )
    assert '<a href="notes/a.md">' in render_markdown("[rel](notes/a.md)")
    assert '<a href="#h">' in render_markdown("[anchor](#h)")


def test_fenced_code_is_preserved_verbatim() -> None:
    html = render_markdown("```\nplain [text] here\n```")
    assert "<pre>" in html
    assert "plain [text] here" in html


def test_wikilink_resolves_to_an_anchor() -> None:
    """The resolver receives the target VERBATIM; normalizing is its own job.

    Pinned because the caller in ``notes_service`` lowercases before its dict
    lookup, and a renderer that silently pre-lowered would make that caller
    look redundant while breaking any case-sensitive resolver.
    """
    seen: list[str] = []

    def resolver(target: str) -> str | None:
        seen.append(target)
        return "abc123" if target == "Q3 Planning Sync" else None

    html = render_markdown("[[Q3 Planning Sync]]", resolver=resolver)
    assert seen == ["Q3 Planning Sync"]
    assert 'href="?id=abc123"' in html
    assert "wikilink" in html


def test_wikilink_alias_is_used_as_the_label() -> None:
    html = render_markdown("[[target|Nicer Label]]", resolver=lambda t: "id1")
    assert ">Nicer Label<" in html


def test_unresolved_wikilink_is_marked() -> None:
    html = render_markdown("[[Nothing Here]]", resolver=lambda t: None)
    assert "wikilink--unresolved" in html
    assert "Nothing Here" in html


def test_wikilink_inside_a_code_fence_is_not_linkified() -> None:
    """The reason wikilinks are an inline rule rather than a regex.

    A regex over rendered HTML would rewrite this; the tokenizer skips code
    entirely, so correctness is free.
    """
    html = render_markdown(
        "```python\n# [[NotALink]] stays literal\n```", resolver=lambda t: "id1"
    )
    assert "[[NotALink]]" in html
    assert 'href="?id=id1"' not in html


def test_wikilink_in_inline_code_is_not_linkified() -> None:
    html = render_markdown("Use `[[literal]]` here.", resolver=lambda t: "id1")
    assert "[[literal]]" in html
    assert 'href="?id=id1"' not in html


def test_wikilink_label_is_html_escaped() -> None:
    html = render_markdown("[[t|<script>x</script>]]", resolver=lambda t: "id1")
    assert "<script>" not in html


def test_unterminated_wikilink_degrades_to_text() -> None:
    html = render_markdown("[[unterminated", resolver=lambda t: "id1")
    assert "[[unterminated" in html
    assert "<a" not in html


def test_empty_body_returns_empty_string() -> None:
    assert render_markdown("") == ""
    assert render_markdown(None) == ""
