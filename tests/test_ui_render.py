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

import pytest

from brain.ui.render import render_markdown

#: Pure string-in, string-out: no connection, no fixtures, no filesystem. The
#: marker is what lets this file run without taking the machine-wide test-DB
#: lock — verified by running it against an unreachable database, not assumed
#: from the absence of a `test_db` argument.
pytestmark = pytest.mark.nodb


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
    # The anchor no longer ENDS at the href: `render.py` stamps
    # `data-brain-link-kind` after it. Only the trailing `>` is dropped from
    # each expectation — the closing quote stays inside the literal, so the
    # href value is still pinned exactly, which is what these assert.
    assert '<a href="https://example.com"' in render_markdown(
        "[ok](https://example.com)"
    )
    assert '<a href="notes/a.md"' in render_markdown("[rel](notes/a.md)")
    assert '<a href="#h"' in render_markdown("[anchor](#h)")


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


# ------------------------------------------- phase-0 paths phase 1 surfaced --
#
# These five statements are INHERITED debt, not phase-1 code: wikilink and
# scheme paths written in phase 0 that nothing exercised. Phase 1 grew
# `render.py` by 40 statements, which pushed the module from comfortably above
# its 95% tier to one statement below it — the same arithmetic either way, but
# an integer percentage displayed "95%" on both sides of the line.
#
# Covered rather than exempted because there is no argument these lines are
# unreachable; each is provoked below with input a user can type. "Nobody has
# covered it" is not a reason for an exemption.


@pytest.mark.parametrize("scheme", ["ftp://h/f", "tel:+15550100", "irc://h"])
def test_a_scheme_markdown_it_permits_but_we_do_not_is_blocked(scheme: str) -> None:
    """`render.py:148-149` — the SECOND BELT, and the only way to reach it.

    The module docstring calls `_render_link_open`'s scheme check a second belt
    over markdown-it's own `validateLink`. That is exactly right, and it makes
    the branch **unreachable for every scheme people test with**: `javascript:`,
    `vbscript:`, `file:` and non-image `data:` are rejected upstream before a
    link token exists, so the text stays literal and this code never runs. Every
    existing scheme test above asserts "no anchor" for that reason.

    `ftp:` / `tel:` / `irc:` are the reachable case — markdown-it permits them,
    `ALLOWED_SCHEMES` ({http, https, mailto}) does not. So an anchor IS created
    and this branch fires, which is what the belt exists for: a scheme upstream
    considers fine and this application does not.

    Asserted on both halves — href emptied AND the class applied — because
    either alone would leave a navigable link or an unmarked one.
    """
    html = render_markdown(f"[x]({scheme})")

    assert "link--blocked" in html
    assert 'href=""' in html
    assert scheme not in html


def test_mailto_is_allowed_so_the_block_is_not_indiscriminate() -> None:
    """Anti-vacuity for the three above: `mailto` is allowlisted and must pass.

    Without this, a scheme check that blocked EVERYTHING would satisfy every
    assertion in this file — the negative tests by blocking, and the http tests
    only by accident of their scheme.
    """
    html = render_markdown("[m](mailto:person-a@example.invalid)")

    assert "link--blocked" not in html
    assert 'href="mailto:person-a@example.invalid"' in html


def test_a_bare_relative_href_with_no_punctuation_is_allowed() -> None:
    """`render.py:74` — the fall-through when a href has no `:` and no `/?#`.

    `notes/a.md` and `#h` are covered above; both exit the loop early. A bare
    `notes` reaches neither branch and falls off the end of the scan, which is
    the statement that was uncovered. It is a real shape — a sibling-page link
    written without an extension.
    """
    assert '<a href="notes"' in render_markdown("[x](notes)")


@pytest.mark.parametrize(
    ("source", "why"),
    [
        ("[[]]", "empty target"),
        ("[[a[b]]", "nested opening bracket"),
        ("[[one\ntwo]]", "target spanning a newline"),
    ],
)
def test_malformed_wikilinks_degrade_to_literal_text(source: str, why: str) -> None:
    """`render.py:104` — the inner-content rejection.

    The rule declines rather than consuming, so malformed input renders as the
    text the author typed instead of swallowing the rest of the paragraph. Each
    case is a distinct clause of the same condition.
    """
    html = render_markdown(source, resolver=lambda t: "id1")

    assert "<a" not in html, f"{why} produced an anchor"
    assert "wikilink" not in html


def test_a_wikilink_with_a_whitespace_only_target_is_not_a_link() -> None:
    """`render.py:109` — target empty AFTER stripping, with an alias present.

    Distinct from the empty-inner case above: `[[   |Alias]]` has non-empty
    inner content, so it passes the `:104` check and is rejected one line later.
    A link whose target is whitespace would resolve to nothing while looking
    like a real reference.
    """
    html = render_markdown("[[   |Alias]]", resolver=lambda t: "id1")

    assert "<a" not in html
    assert "Alias" in html


# --------------------------------------------------------- phase 1: tables --
#
# 466 corpus documents contain a GFM table (460 ingested, 6 vault), counted on
# the live corpus rather than estimated. Before `md.enable("table")` every one
# rendered as literal pipe characters in a paragraph.

TABLE = (
    "| Item | Owner |\n"
    "| --- | --- |\n"
    "| Ship the thing | Person A |\n"
)


def test_a_table_renders_as_a_table() -> None:
    html = render_markdown(TABLE)
    assert "<table>" in html
    assert "<th>Item</th>" in html
    assert "<td>Ship the thing</td>" in html


def test_table_alignment_becomes_a_class_and_never_an_inline_style() -> None:
    """Alignment must survive the CSP, which means classes — not inline styles.

    markdown-it emits ``style="text-align:center"`` for a ``:---:`` column. The
    app serves ``style-src 'self'`` with no ``'unsafe-inline'``
    (``security.py:65``), and that directive covers style ATTRIBUTES as well as
    ``<style>`` elements — so the browser drops the declaration and every
    aligned column silently renders left-aligned. ``render.py`` therefore
    rewrites those styles into classes.

    Both halves are asserted. The class alone would pass while a leftover
    ``style`` attribute still tripped a CSP console error on every table; the
    absence alone would pass if alignment were simply discarded.

    Found by writing this test against the assumption that markdown-it emitted
    ``align="center"``. It does not, and the CSS written for that attribute was
    dead on arrival.
    """
    html = render_markdown(
        "| L | C | R |\n| :--- | :---: | ---: |\n| a | b | c |\n"
    )
    assert "cell--center" in html
    assert "cell--right" in html
    # `cell--left` is asserted too, and it is NOT redundant. Deleting its entry
    # from `_ALIGN_CLASSES` leaves center, right and the no-`style=` clause all
    # passing while explicit `:---` left alignment is silently dropped — the
    # attribute is stripped whether or not the value mapped, so the CSP half of
    # this test structurally cannot catch it. A check that cannot fail for the
    # thing it names is the subject of this phase's own §4.1.
    assert "cell--left" in html
    assert "style=" not in html, (
        "a table cell still carries an inline style; the CSP forbids it, so "
        "whatever it was meant to do will not happen in the browser"
    )


def test_html_inside_a_table_cell_is_escaped() -> None:
    """A3: every new construct needs its own XSS test.

    Cell contents run through the inline pipeline, so `html: False` should cover
    them — but "should" is the word this file exists to replace.
    """
    html = render_markdown(
        "| Col |\n| --- |\n| <script>alert(1)</script> |\n"
    )
    assert "<script>" not in html
    assert "&lt;script&gt;" in html


def test_a_javascript_link_inside_a_table_cell_produces_no_anchor() -> None:
    """Enabling a block construct must not open a route around the link defence.

    Asserted as "no anchor", matching the existing scheme tests above, rather
    than as "carries ``link--blocked``". The first draft asserted the latter and
    failed — because markdown-it's own ``validateLink`` rejects ``javascript:``
    before a link token is ever created, so the text stays literal and
    ``_render_link_open`` is never reached. That render rule is the SECOND belt
    the module docstring describes, kept so the guarantee survives an upstream
    change to that heuristic; asserting on it here would have been a test of an
    unreachable path dressed as a test of the defence.

    The safety property is that no clickable ``javascript:`` link exists, and
    that is what this checks — inside a table cell, which is the new route.
    """
    html = render_markdown(
        "| Link |\n| --- |\n| [x](javascript:alert(1)) |\n"
    )
    assert "<a" not in html


def test_a_safe_link_inside_a_table_cell_still_renders() -> None:
    """Anti-vacuity for the test above: links must work in cells at all.

    Without this, a table implementation that never parsed ANY link in a cell
    would satisfy "no anchor for javascript:" perfectly.
    """
    html = render_markdown(
        "| Link |\n| --- |\n| [ok](https://example.com) |\n"
    )
    assert '<a href="https://example.com"' in html


def test_a_wikilink_inside_a_table_cell_still_resolves() -> None:
    """Wikilinks are a CUSTOM inline rule inserted before `link`.

    Nothing guarantees a rule registered that way fires inside a construct
    enabled later; the table rule tokenizes cell contents separately, so this is
    the integration most likely to be quietly missing.
    """
    html = render_markdown(
        "| Ref |\n| --- |\n| [[Q3 Planning Sync]] |\n",
        resolver=lambda t: "abc123" if t == "Q3 Planning Sync" else None,
    )
    assert 'href="?id=abc123"' in html
    assert "wikilink" in html


def test_a_pipe_table_is_not_produced_without_a_delimiter_row() -> None:
    """Guards the enable itself against over-reach.

    A line of pipes with no `---` row is not a table in GFM, and prose that
    happens to contain pipes must not become one.
    """
    html = render_markdown("a | b | c\n")
    assert "<table>" not in html


# ------------------------------------------------ phase 1: embed mis-render --


def test_an_embed_renders_literally_rather_than_as_a_broken_link() -> None:
    """A DEFECT fixed, not a feature added.

    ``![[Some Note]]`` used to emit a literal ``!`` followed by a live
    unresolved-wikilink anchor — the ``[[…]]`` matched and the ``!`` was left
    behind. Wrong output, not absent output. An unsupported construct rendering
    literally is defensible; one rendering as a broken link plus a stray
    character is a bug, and it is the kind a reader reports as "the wiki is
    broken" rather than as "embeds are unsupported".

    Zero corpus documents use embeds, which is exactly the condition that makes
    this cheap and safe to fix: no corpus to regress, no styling to design, and
    the first person to write one now gets their own text back instead of
    garbage.
    """
    html = render_markdown("![[Some Note]]", resolver=lambda t: "abc123")
    assert "<a" not in html
    assert "wikilink" not in html
    assert "![[Some Note]]" in html


def test_an_embed_of_an_unresolvable_target_is_also_literal() -> None:
    """The unresolved path was the uglier half — a `link--unresolved` anchor."""
    html = render_markdown("![[Nothing Here]]", resolver=lambda t: None)
    assert "<a" not in html
    assert "![[Nothing Here]]" in html


def test_a_bang_that_is_not_adjacent_still_leaves_the_wikilink_alone() -> None:
    """Over-reach check: only an IMMEDIATELY preceding `!` suppresses the link.

    Without this, a rule that declined whenever a `!` appeared anywhere earlier
    in the paragraph would pass the two tests above and silently kill ordinary
    wiki links in any sentence containing an exclamation mark.
    """
    html = render_markdown("! [[Some Note]]", resolver=lambda t: "abc123")
    assert 'href="?id=abc123"' in html


def test_a_real_markdown_image_is_unaffected() -> None:
    """The `!` handling must not disturb `![alt](url)`."""
    html = render_markdown("![alt](https://example.com/i.png)")
    assert "<img" in html
    assert 'alt="alt"' in html


# -------------------------------------------------- phase 1: task lists --
#
# 180 documents, 6,745 items (159 ingested / 21 vault), counted with the
# tokenizer. The only phase 1 item needing a package beyond markdown-it-py.


def test_a_task_list_renders_real_checkboxes() -> None:
    html = render_markdown("- [ ] open\n- [x] done\n")
    assert 'type="checkbox"' in html
    assert 'checked="checked"' in html
    assert "contains-task-list" in html


def test_an_ORDERED_task_list_is_converted_too() -> None:
    """The 12 documents that would otherwise have been missed.

    GFM task lists are conventionally bullet-only, and my first corpus count
    used a bullet-only regex — which is exactly why it read 168 instead of 180.
    Whether the plugin handles ``1. [ ]`` decided whether phase 1's exit
    criterion covered 168 documents or all 180, so it was checked by execution
    before any of this was written. It does: the list becomes an ``<ol>`` whose
    items carry checkboxes.

    Pinned because a plugin release that quietly dropped ordered support would
    silently un-render 12 documents, and nothing else here would notice.
    """
    html = render_markdown("1. [ ] open\n2. [x] done\n")
    assert "<ol" in html
    assert 'type="checkbox"' in html
    assert 'checked="checked"' in html


def test_checkboxes_are_disabled_because_nothing_persists_a_click() -> None:
    """The inspector is a read surface.

    An interactive checkbox would offer a state change no code saves — a click
    that silently does nothing, which is worse than a control that is visibly
    inert.
    """
    html = render_markdown("- [ ] open\n")
    assert "disabled" in html


def test_task_lists_emit_no_inline_styles() -> None:
    """Third construct, same CSP question, asked again rather than assumed.

    Table alignment failed this exact check, so it is now asked of every new
    construct instead of being inferred from one library behaving.
    """
    html = render_markdown("- [ ] a\n- [x] b\n")
    assert "style=" not in html
    # Anti-vacuity, matching the partner clause in the highlighting test. Without
    # the plugin this input renders `<ul><li>[ ] a</li>` — no `style=`, so the
    # assertion above holds with task lists ENTIRELY ABSENT and answers nothing.
    assert 'type="checkbox"' in html, (
        "no checkbox was rendered, so the inline-style check above proves "
        "nothing about task lists"
    )


def test_a_checkbox_marker_inside_a_code_fence_is_not_converted() -> None:
    """The analogue of the signed-URL case, for this construct.

    Fenced content never reaches inline rules, so this should hold — and the
    corpus count relies on it, since it was measured with a fence-aware
    tokenizer. A regression here would both mis-render code samples AND
    invalidate the 180 figure.
    """
    html = render_markdown("```\n- [ ] not a task\n```")
    assert 'type="checkbox"' not in html
    assert "- [ ] not a task" in html


def test_an_ordinary_list_is_untouched() -> None:
    """Over-reach check: only `[ ]`/`[x]` at item start become checkboxes."""
    html = render_markdown("- plain item\n- [not a box] item\n")
    assert 'type="checkbox"' not in html
    assert "contains-task-list" not in html


# ----------------------------------------------- phase 1: syntax highlight --
#
# 212 corpus documents carry a language-tagged fence (211 ingested, 1 vault).
# Pygments was already installed transitively via `rich`; phase 1 declares it
# and wires it in. The palette is deliberately NOT decided here — phase 3 owns
# the token system, so the stylesheet ships neutral distinctions only.


def test_a_language_tagged_fence_is_tokenised() -> None:
    html = render_markdown("```python\ndef f():\n    pass\n```")
    assert '<span class="k">def</span>' in html
    assert '<span class="k">pass</span>' in html


def test_highlighting_never_emits_inline_styles() -> None:
    """The CSP trap, asserted directly — the higher-volume twin of the table bug.

    ``HtmlFormatter(noclasses=True)`` puts ``style="color: #..."`` on EVERY
    token. ``style-src 'self'`` (``security.py:65``) covers style attributes, so
    the browser would drop all of it and the block would render unhighlighted,
    with nothing linking the missing colour to the security policy. Verified by
    execution before it was written: ``noclasses=True`` does produce
    ``style=``; the configured formatter does not.

    Asserted on the RENDERED output rather than on the formatter's arguments,
    for the reason this module's docstring gives — an assertion about options
    survives a swap of the thing being configured.
    """
    html = render_markdown("```python\nx = 1\n```")
    assert "style=" not in html
    assert 'class="' in html, "nothing was tokenised, so the check above is vacuous"


def test_the_language_class_survives_highlighting() -> None:
    """`brain ui` already emitted `class="language-python"`; it must keep doing so.

    The highlighter returns bare spans (``nowrap=True``) precisely so
    markdown-it's own fence renderer still wraps them and keeps this class. The
    default formatter would return a ``<div class="highlight">`` that markdown-it
    nests INSIDE the ``<code>``, producing a div in a code element and losing
    nothing visibly — the kind of breakage that shows up much later.
    """
    html = render_markdown("```python\nx = 1\n```")
    assert 'class="language-python"' in html
    assert "<div" not in html


def test_html_inside_a_highlighted_fence_is_still_escaped() -> None:
    """A3: the new construct routes fence content through a THIRD-PARTY library.

    Pygments does its own escaping, so this checks that the escaping survives
    the handoff — markdown-it inserts a highlighter's return value RAW, without
    re-escaping it. If Pygments ever returned unescaped text, this is the only
    thing standing between a code block and script execution.
    """
    html = render_markdown('```python\nx = "<script>alert(1)</script>"\n```')
    assert "<script>" not in html
    assert "&lt;script&gt;" in html


def test_an_unknown_language_degrades_to_plain_escaped_code() -> None:
    """```mermaid``` and typos must not raise, and must stay escaped.

    The highlighter declines (returns "") rather than raising on
    ``ClassNotFound``, which hands the block back to markdown-it's own escaping
    path.
    """
    html = render_markdown("```notalanguage\n<script>x</script>\n```")
    assert "&lt;script&gt;" in html
    assert "<script>" not in html
    assert 'class="language-notalanguage"' in html


def test_a_fence_with_no_language_is_left_alone() -> None:
    """Pre-existing behaviour, pinned so highlighting cannot quietly change it.

    ``test_fenced_code_is_preserved_verbatim`` above covers the content; this
    adds that no tokenisation happens at all without a language tag.
    """
    html = render_markdown("```\ndef f():\n```")
    assert "<span" not in html
    assert "def f():" in html


# -------------------------------------------------- phase 1: strikethrough --
#
# 31 corpus documents, all ingested. Counted as `~~text~~` PAIRS rather than as
# occurrences of `~~`: a looser count returns 38, and the extra 7 are documents
# whose only double-tilde sits inside a CloudFront signed URL, where `~` is a
# legal character. Those never rendered as strikethrough and still do not — see
# the URL test below.


def test_strikethrough_renders() -> None:
    html = render_markdown("~~retired plan~~")
    assert "<s>retired plan</s>" in html


def test_html_inside_strikethrough_is_escaped() -> None:
    html = render_markdown("~~<script>alert(1)</script>~~")
    assert "<script>" not in html
    assert "&lt;script&gt;" in html


def test_a_signed_url_with_double_tildes_is_not_struck_through() -> None:
    """The corpus case that made the raw `~~` count misleading.

    Krisp "Download Recording" links are CloudFront signed URLs in which `~` is
    a legal character, so a signature can contain `~~`. Seven documents contain
    exactly that and no real strikethrough. Enabling the rule must not reach
    inside a link destination and mangle a URL into `<s>`, which would produce a
    broken download link that still LOOKS like a link.

    The signature here is synthetic, shaped like the real ones.
    """
    source = "[Download](<https://files.example.invalid/d?Signature=ab~~cd~~ef__>)"
    html = render_markdown(source)
    assert "<s>" not in html
    assert "ab~~cd~~ef__" in html


def test_a_lone_double_tilde_is_left_alone() -> None:
    """An unpaired `~~` is not strikethrough and must render literally."""
    html = render_markdown("a ~~ b")
    assert "<s>" not in html
    assert "~~" in html


def test_empty_body_returns_empty_string() -> None:
    assert render_markdown("") == ""
    assert render_markdown(None) == ""
