"""The Notes HTML → Markdown converter (RM-36, acceptance criterion 3). Synthetic bodies only."""
from __future__ import annotations

import pytest

from brain.ingest.notes_html import (
    MAX_NESTING,
    MAX_OPEN_INLINE,
    MAX_OUTPUT_CHARS,
    TRUNCATION_MARKER,
    _convert,
    _Converter,
    html_to_markdown,
)

# The shape Notes returns: one <div> per line, a <div><br></div> per blank line.
NOTE_BODY = (
    "<div><h1>Trip plan</h1></div>"
    "<div>Pack the <b>blue</b> bag.</div>"
    "<div><br></div>"
    "<div><h2>Checklist</h2></div>"
    '<ul class="checklist">'
    '<li class="checked">Book tickets</li>'
    "<li>Water plants</li>"
    "</ul>"
    "<div>See <a href=\"https://example.com/route\">the route</a>.</div>"
)


def test_a_typical_note_keeps_headings_lists_links_and_checklists() -> None:
    assert html_to_markdown(NOTE_BODY) == (
        "# Trip plan\n"
        "Pack the **blue** bag.\n"
        "\n"
        "## Checklist\n"
        "- [x] Book tickets\n"
        "- [ ] Water plants\n"
        "See [the route](https://example.com/route)."
    )


@pytest.mark.parametrize("level", range(1, 7))
def test_every_heading_level(level: int) -> None:
    assert html_to_markdown(f"<h{level}>Title</h{level}>") == "#" * level + " Title"


def test_nested_and_ordered_lists() -> None:
    body = "<ol><li>one<ul><li>inner</li><li>more</li></ul></li><li>two</li></ol>"

    assert html_to_markdown(body) == "1. one\n  - inner\n  - more\n2. two"


def test_a_list_item_outside_a_list_is_still_a_bullet() -> None:
    assert html_to_markdown("<li>stray</li>") == "- stray"


def test_an_unmatched_list_end_is_ignored() -> None:
    assert html_to_markdown("<div>a</div></ul><div>b</div>") == "a\nb"


@pytest.mark.parametrize(
    ("item", "expected"),
    [
        ('<li class="Checked done">x</li>', "- [x] x"),  # per word, any case
        ('<li class="unchecked">x</li>', "- [ ] x"),  # a word, not a substring
        ('<li data-checked="TRUE">x</li>', "- [x] x"),
        ('<li data-checked="false">x</li>', "- [ ] x"),
        ("<li>x</li>", "- [ ] x"),
    ],
)
def test_checklist_state_from_the_item(item: str, expected: str) -> None:
    assert html_to_markdown(f'<ul class="Apple-Checklist">{item}</ul>') == expected


def test_checkbox_inputs_mark_items_and_bare_lines() -> None:
    body = (
        '<ul><li><input type="checkbox" checked>done</li>'
        '<li><input type="CHECKBOX">open</li></ul>'
        '<div><input type="checkbox"/>loose</div>'
        '<input type="text">'
    )

    assert html_to_markdown(body) == "- [x] done\n- [ ] open\n- [ ] loose"


def test_a_checkbox_in_an_already_boxed_item_adds_no_second_box() -> None:
    body = '<ul class="checklist"><li class="checked"><input type="checkbox">x</li></ul>'

    assert html_to_markdown(body) == "- [x] x"


def test_a_checkbox_in_an_ordered_item() -> None:
    assert html_to_markdown('<ol><li><input type="checkbox" checked>x</li></ol>') == "1. [x] x"


@pytest.mark.parametrize(
    ("anchor", "expected"),
    [
        ('<a href="https://example.com/a b(c)">t</a>', "[t](https://example.com/a%20b%28c%29)"),
        ('<a href="HTTP://example.com">t</a>', "[t](HTTP://example.com)"),
        ('<a href="mailto:someone@example.com">mail</a>', "[mail](mailto:someone@example.com)"),
        ('<a href="https://example.com"></a>', "<https://example.com>"),
        ('<a href="https://example.com"/>', "<https://example.com>"),
        ('<a href="javascript:alert(1)">click</a>', "click"),
        ('<a href="data:text/html,x">data</a>', "data"),
        ('<a href="applenotes:note/1">other note</a>', "other note"),
        ("<a>no target</a>", "no target"),
    ],
)
def test_links_keep_only_safe_targets(anchor: str, expected: str) -> None:
    assert html_to_markdown(f"<div>{anchor}</div>") == expected


def test_an_unmatched_link_end_is_ignored() -> None:
    assert html_to_markdown("<div>text</a></div>") == "text"


@pytest.mark.parametrize(
    ("body", "expected"),
    [
        ("<b>bold</b> <strong>strong</strong>", "**bold** **strong**"),
        ("<i>it</i> <em>em</em>", "*it* *em*"),
        ("<tt>mono</tt> <code>c</code>", "`mono` `c`"),
        ("a<b> spaced </b>b", "a **spaced** b"),
        ("<b></b>x<b> </b>", "x"),  # empty emphasis leaves no stray markers
        ("x</b>", "x"),  # an end with no start
        ("<b><i>x</b></i>", "***x**"),  # mis-nested: the outer end skips the inner mark
    ],
)
def test_inline_emphasis(body: str, expected: str) -> None:
    assert html_to_markdown(f"<div>{body}</div>") == expected


def test_emphasis_across_a_link_end_does_not_lose_the_link() -> None:
    """Mis-nested markup Notes does not emit, but must not drop text either."""
    assert html_to_markdown('<div><a href="https://e.x"><b>bold</a></b></div>') == (
        "[**bold](https://e.x)"
    )
    assert html_to_markdown('<div><a href="https://e.x">pre<b>x</a></b></div>') == (
        "[pre**x](https://e.x)"
    )


def test_emphasis_closed_after_its_line_ended_is_ignored() -> None:
    assert html_to_markdown("<b>one<br>two</b>") == "**one\ntwo"


def test_attachments_media_and_non_text_are_dropped() -> None:
    body = (
        "<head><title>t</title></head>"
        '<div>before<img src="data:image/png;base64,AAAA">after</div>'
        '<div><object data="x"><b>fallback</b></object>kept</div>'
        '<embed src="x"><div>still here</div>'
        "<script>alert(1)</script><style>p{}</style>"
        "<video><source src='v'>caption</video>"
        "<svg><text>t</text></svg><iframe>f</iframe><audio>a</audio>"
    )

    assert html_to_markdown(body) == "beforeafter\nkept\nstill here"


def test_blockquote_pre_hr_and_tables() -> None:
    body = (
        "<blockquote><div>quoted</div><blockquote>deeper</blockquote></blockquote>"
        "</blockquote>"
        "<pre>line 1\n  <b>line</b> 2\n</pre>"
        "<hr>"
        "<table><tr><th>a</th><th>b</th></tr><tr><td>1</td><td>2</td></tr></table>"
    )

    assert html_to_markdown(body) == (
        "> quoted\n> > deeper\n```\nline 1\n  line 2\n```\n---\na | b\n1 | 2"
    )


def test_an_unclosed_pre_is_closed_at_the_end() -> None:
    assert html_to_markdown("<pre>code") == "```\ncode\n```"


def test_a_stray_pre_end_is_ignored() -> None:
    assert html_to_markdown("<div>a</pre></div>") == "a"


def test_whitespace_entities_and_blank_runs() -> None:
    body = "<div>a&nbsp;&nbsp;b\n c &amp; d</div><div><br></div><div><br></div><div><br></div>"

    assert html_to_markdown(body + "<div>e</div>") == "a b c & d\n\ne"


@pytest.mark.parametrize("body", ["", "<div><br></div>", "<img src='x'>", "   "])
def test_nothing_readable_is_empty(body: str) -> None:
    assert html_to_markdown(body) == ""


def test_plain_text_passes_through() -> None:
    assert html_to_markdown("just text") == "just text"


def test_self_closed_block_tags() -> None:
    assert html_to_markdown("one<div/>two<br/>three") == "one\ntwo\nthree"


# --- bounded on hostile input (#103 security review: M1, M2, M3) -------------------------------


def test_list_indent_stops_growing_at_max_nesting() -> None:
    """M1: deep nesting once repeated the full indent on every line (54 KB in, 36 MB out)."""
    depth = MAX_NESTING + 40
    out = html_to_markdown("<ul><li>x" * depth)

    lines = out.splitlines()
    assert len(lines) == depth
    assert lines[MAX_NESTING] == "  " * MAX_NESTING + "- x"
    assert lines[-1] == "  " * MAX_NESTING + "- x"


def test_quote_markers_stop_growing_at_max_nesting() -> None:
    out = html_to_markdown("<blockquote>q<br>" * (MAX_NESTING + 40))

    assert max(len(line) for line in out.splitlines()) == len("> " * MAX_NESTING + "q")


def test_max_nesting_is_eight() -> None:
    assert MAX_NESTING == 8


@pytest.mark.parametrize(
    "body",
    [
        "<ul><li>x" * 120_000,
        "<blockquote>q<br>" * 70_000,
        "<ul>" * 9 + "<li>x" * 250_000,
        "&lt;x" * 600_000,  # one long line of decoded entities
    ],
)
def test_the_output_is_capped_and_marked(body: str) -> None:
    out = html_to_markdown(body)

    assert out.endswith(f"\n\n{TRUNCATION_MARKER}")
    assert len(out) <= MAX_OUTPUT_CHARS + len(TRUNCATION_MARKER) + 2


def test_output_just_under_the_cap_is_not_marked() -> None:
    body = "x" * (MAX_OUTPUT_CHARS - 1)  # one line plus its newline fills the cap exactly

    out = html_to_markdown(body)

    assert out == body
    assert TRUNCATION_MARKER not in out


def test_output_one_over_the_cap_is_cut_and_marked() -> None:
    out = html_to_markdown("x" * MAX_OUTPUT_CHARS)

    assert out == "x" * (MAX_OUTPUT_CHARS - 1) + f"\n\n{TRUNCATION_MARKER}"


def test_nothing_after_the_cap_is_kept() -> None:
    out = html_to_markdown("x" * MAX_OUTPUT_CHARS + "<div>after</div><pre>code</pre><hr>")

    assert "after" not in out and "code" not in out and "---" not in out


def _work(body: str) -> int:
    converter = _convert(body)
    converter.markdown()
    return converter.work


# Each shape once took time quadratic in its size (M2: 1 MB of nested <a> took 94 s).
HOSTILE_SHAPES = {
    "nested links": '<a href="https://e.x">x',
    "nested emphasis": "<b><i>x",
    "open bold, stray italic ends": "<b>x</i>",
    "closing emphasis after a full stack": "<b>x</b><b>",
    "nested lists": "<ul><li>x",
    "nested quotes": "<blockquote>x<br>",
    "links across lines": '<a href="https://e.x">x<br>',
}


@pytest.mark.parametrize("shape", HOSTILE_SHAPES.values(), ids=HOSTILE_SHAPES.keys())
def test_work_grows_linearly_with_the_input(shape: str) -> None:
    """Timing-free: ``_Converter.work`` counts characters joined and stack entries scanned.

    The bound is per input character, so a quadratic converter fails it at this size by
    two orders of magnitude.
    """
    for repeat in (2_000, 8_000):
        body = shape * repeat

        assert _work(body) <= (MAX_OPEN_INLINE + 4) * len(body), repeat


def test_the_inline_stacks_are_bounded() -> None:
    converter = _convert('<a href="https://e.x">' * 500 + "<b>" * 500 + "x")

    assert converter.lines  # flushed at the end
    converter2 = _Converter()
    converter2.feed('<a href="https://e.x">' * 500 + "<b>" * 500 + "x")
    assert len(converter2.links) == MAX_OPEN_INLINE
    assert len(converter2.marks) == MAX_OPEN_INLINE
    assert MAX_OPEN_INLINE == 64


def test_a_link_left_open_does_not_reach_the_next_line() -> None:
    """The links stack is cleared at a line end, like the emphasis stack."""
    converter = _Converter()
    converter.feed('<div><a href="https://e.x">one</div><div>two</a></div>')

    assert converter.links == []
    assert html_to_markdown('<div><a href="https://e.x">one</div><div>two</a></div>') == (
        "one\ntwo"
    )


def test_deep_links_keep_their_text() -> None:
    body = '<a href="https://e.x">' * (MAX_OPEN_INLINE + 3) + "deep" + "</a>" * 80

    assert "deep" in html_to_markdown(body)


@pytest.mark.parametrize(
    ("ref", "expected"),
    [
        ("&#" + "9" * 5000 + ";", "�"),  # once ValueError: over Python's 4,300 digits
        ("&#" + "9" * 5000, "�"),  # no semicolon
        ("&#x" + "F" * 5000 + ";", "�"),
        ("&#X" + "0" * 16 + "41;", "�"),  # at the digit bound, even if zero-padded
        ("&#" + "0" * 15 + ";", "�"),  # 15 digits: html.unescape's own U+FFFD for 0
        ("&#" + "0" * 13 + "65;", "A"),  # 15 digits: below the bound, decoded
        ("&#65;&#x42;", "AB"),
    ],
)
def test_huge_character_references_become_the_replacement_character(
    ref: str, expected: str
) -> None:
    assert html_to_markdown(f"<div>{ref}</div>") == expected


def test_a_huge_character_reference_in_an_attribute_is_harmless() -> None:
    body = f'<a href="https://e.x/&#{"9" * 5000};">t</a>'

    assert html_to_markdown(body) == "[t](https://e.x/�)"


# --- link escapes and data: payloads (#103 review: aud F4, rev 1) -------------------------------


@pytest.mark.parametrize(
    ("anchor", "expected"),
    [
        ('<a href="https://e.x/a>b"></a>', "<https://e.x/a%3Eb>"),  # cannot close the autolink
        ('<a href="https://e.x/<x>">t</a>', "[t](https://e.x/%3Cx%3E)"),
        ('<a href="https://e.x">see [1] here</a>', "[see \\[1\\] here](https://e.x)"),
        ('<a href="https://e.x">a](javascript:x)</a>', "[a\\](javascript:x)](https://e.x)"),
    ],
)
def test_link_text_and_targets_cannot_close_their_own_markup(
    anchor: str, expected: str
) -> None:
    assert html_to_markdown(f"<div>{anchor}</div>") == expected


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ('a b" src="data:image/png;base64,QUJD+/=">c', 'a b" src="">c'),  # a leaked img tail
        ("x data:text/plain;charset=utf-8,hello y", "x  y"),
        ("DATA:IMAGE/PNG;BASE64,QUJD", ""),
        ("metadata:keep, data: alone, data:nope", "metadata:keep, data: alone, data:nope"),
    ],
)
def test_data_payloads_never_reach_the_output(text: str, expected: str) -> None:
    assert html_to_markdown(f"<div>{text}</div>") == expected
    assert html_to_markdown(f"<pre>{text}</pre>") == f"```\n{expected}\n```"
