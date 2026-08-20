"""T18 — email threads render as real ``<details>``, never as escaped text.

THE DEFECT THIS EXISTS FOR (S2, filed as live on task #1). ``ingest/gmail.py``
assembles a thread with the newest message as a plain ``## H2`` and every older
message wrapped in ``<details><summary>…</summary>…</details>``. ``render.py``
builds its parser with ``html=False``, so those tags are *escaped* — the reader
sees the literal text ``<details>`` above every quoted message instead of a
collapsible section. Measured scope, from the T1 recon: 58 of 65 affected
documents, **89.1% of every ``email_thread``**, while plain ``email`` is clean.

WHY ``html=False`` STAYS, AND WHAT REPLACES IT. Turning raw HTML on would fix
the symptom by admitting arbitrary markup from every ingested source — the exact
property ``tests/test_ui_render.py`` pins and the whole XSS argument rests on.
Instead the renderer RECOGNISES the construct and RE-EMITS it structurally: a
block rule consumes the ``<details>`` wrapper into tokens, and the render rule
writes the elements itself. Nothing from the document is ever emitted as raw
HTML — the summary text is escaped by us, and the body goes through the ordinary
inline pipeline. That distinction is the safety argument, so it gets its own
tests below rather than being asserted in a comment.

HTML ENTITIES ARE OUT OF SCOPE, and that is measured rather than assumed: 18
corpus documents contain ``&nbsp;``/``&amp;``/``&lt;``, but markdown-it's
``entity`` rule is not gated by ``options.html``, so entities already decode
correctly. Only tags escape. Widening this task to entities would be fixing
something that is not broken.

THE FIXTURE IS SYNTHETIC. Real thread bodies are the single richest source of
PII in the corpus — names, addresses, quoted business content. Every address
here is ``@example.test`` (RFC 6761 reserved) and every name is invented.
"""
from __future__ import annotations

import pytest

from brain.ui.render import extract_headings, render_markdown

#: Opens no database connection — this module renders strings.
pytestmark = pytest.mark.nodb

#: Exactly the shape ``ingest/gmail.py::_format_thread_section`` emits, including
#: its ``html.escape(heading, quote=False)`` — which is why the addresses below
#: appear as ``&lt;…&gt;`` in the source. That escape now covers BOTH headings:
#: the ``<summary>`` of every collapsed message AND the newest message's ``## H2``
#: (defect #57, fixed; the H2 was raw, so CommonMark autolinked it and one
#: document spelled one address two ways). Reproduced rather than imported
#: because a test that calls the producer cannot notice the producer changing
#: shape; this is the contract as it exists on disk today.
#: OLDEST FIRST, NEWEST LAST — and that order was CORRECTED after being
#: VERIFIED BY RUNNING THE PRODUCER, not by reading it and not by trusting the
#: Quartz overlay.
#:
#: `to_extracted_thread` sorts ascending by `internalDate` and passes
#: `collapsed=(idx != last_idx)` (the `sections = [...]` comprehension in
#: `gmail.py`; cited by name because the line number has already moved once),
#: so the plain `## H2` is the LAST section, after every `<details>`. An earlier
#: revision of this fixture put the H2 first while its docstring claimed to be
#: "the contract as it exists on disk today"; it was not.
#: `quartz/static/emailThread.js` used to make the same claim in its header —
#: "a leading `<h2>`" — and two independent sources agreeing is exactly how a
#: false belief becomes credible. That comment has since been corrected at the
#: source, and the ordering is now held by an executable assertion
#: (`tests/test_gmail_thread.py::test_most_recent_message_not_collapsed`,
#: `last_h2 > last_details`) rather than by any prose. The overlay survived the
#: error by scanning to end-of-article; a FIXTURE cannot, because any
#: order-dependent assertion on it would be asserting against a shape the corpus
#: never produces.
#:
#: Established by calling `to_extracted_thread` on three synthetic messages and
#: reading the marker lines out of the result:
#:     <details> </details> <details> </details> ## 2026-03-02 09:15 — …
THREAD = """<details>
<summary>2026-03-07 08:00 — Dana Vendor &lt;dana@example.test&gt;</summary>

The oldest message in the thread.

</details>

<details>
<summary>2026-03-08 09:15 — Sam Buyer &lt;sam@example.test&gt;</summary>

An earlier message, collapsed by default.

</details>

## 2026-03-09 12:00 — Dana Vendor &lt;dana@example.test&gt;

The latest reply, always expanded.
"""

#: How many collapsed sections THREAD contains. Derived by counting the opening
#: tags in the fixture rather than restated, so editing the fixture cannot leave
#: the expectation behind — the roster failure this project has paid for twice.
COLLAPSED_SECTIONS = THREAD.count("<details>")


def test_the_fixture_declares_more_than_one_section() -> None:
    """Anti-vacuity for every count assertion below.

    A fixture that silently lost its ``<details>`` blocks would make "N elements
    rendered" true at N=0 for a renderer that emits nothing at all.
    """
    assert COLLAPSED_SECTIONS == 2


def test_a_thread_renders_details_elements_not_escaped_text() -> None:
    """THE T18 test, and it FAILS ON TODAY'S CODE — which is the point.

    Asserts both directions. The element count alone would be satisfied by a
    renderer that emitted the elements *and also* left the escaped text behind;
    the absence assertion alone would be satisfied by one that dropped the
    construct entirely. Neither failure mode is hypothetical: "escape it" and
    "delete it" are the two obvious wrong fixes.
    """
    html = render_markdown(THREAD)

    assert html.count("<details") == COLLAPSED_SECTIONS, (
        f"expected {COLLAPSED_SECTIONS} <details> elements, found "
        f"{html.count('<details')}. With html=False and no structural rule the "
        f"count is 0 and the reader sees the literal tag text."
    )
    assert "&lt;details&gt;" not in html, (
        "the <details> wrapper is still being escaped into visible text — this "
        "is the defect verbatim"
    )
    assert html.count("<summary") == COLLAPSED_SECTIONS


def test_the_summary_keeps_the_address_the_reply_filter_matches_on() -> None:
    """The From address must survive into ``<summary>`` text.

    ``ingest/gmail.py`` escapes the heading precisely so the address stays
    VISIBLE and stays available as a substring — its comment says the filter
    reads ``summary.textContent``. A renderer that dropped or double-escaped it
    would take the address off the page and break the filter at the same time,
    and the filter's own test could not see the cause.
    """
    html = render_markdown(THREAD)

    assert "sam@example.test" in html
    assert "Sam Buyer" in html
    # Rendered as text, not as a tag: the source carried `&lt;`, so the output
    # must too. A bare `<sam@example.test>` would be parsed by the browser as an
    # unknown element and silently vanish from the rendered summary — the exact
    # bug gmail.py's escape was added to prevent.
    assert "&lt;sam@example.test&gt;" in html


def test_the_body_inside_a_section_is_rendered_as_markdown() -> None:
    """The inner text is markdown, not a preformatted lump.

    gmail.py puts blank lines around the body specifically so processors render
    it as markdown. If the block rule captured the body as opaque text, the
    section would collapse open onto an unformatted paragraph and nobody would
    notice until a message contained a list.
    """
    html = render_markdown(
        "<details>\n<summary>S</summary>\n\n"
        "A paragraph.\n\n- one\n- two\n\n</details>\n"
    )

    assert "<ul>" in html and html.count("<li>") == 2
    assert "<p>A paragraph.</p>" in html


# ------------------------------------------------------------------- safety --


def test_a_script_inside_a_section_body_renders_inert() -> None:
    """A3 — the XSS test the plan names.

    The whole justification for ruling (a) is that recognising ONE construct is
    not the same as admitting arbitrary HTML. This is what makes that a measured
    claim: the ``<details>`` wrapper is re-emitted, and a ``<script>`` sitting
    inside it is still escaped by the ordinary ``html=False`` pipeline.
    """
    html = render_markdown(
        "<details>\n<summary>S</summary>\n\n"
        "<script>alert(1)</script>\n\n</details>\n"
    )

    assert "<details" in html, "the recognised construct did not render"
    assert "<script>" not in html, (
        "a <script> inside a thread section reached the output as a live tag — "
        "recognising <details> must not become a general HTML pass-through"
    )
    assert "&lt;script&gt;" in html


def test_a_script_inside_the_summary_renders_inert() -> None:
    """The summary is a SEPARATE injection surface from the body.

    The body rides the normal inline pipeline; the summary text is placed by our
    own render rule, so its escaping is a different line of code and can fail
    independently. Asserting only the body case would leave the half we
    hand-wrote unproven.
    """
    html = render_markdown(
        "<details>\n<summary><script>alert(1)</script></summary>\n\n"
        "body\n\n</details>\n"
    )

    assert "<script>" not in html, (
        "a <script> in the summary reached the output as a live tag"
    )
    assert "<details" in html


def test_an_attribute_cannot_be_broken_out_of_via_the_summary() -> None:
    """The summary text must not be able to close the tag it sits in.

    A quote or an angle bracket in a From header is not exotic — display names
    routinely carry them — so this is a correctness case as much as a security
    one.
    """
    html = render_markdown(
        '<details>\n<summary>a" onclick="alert(1)</summary>\n\nbody\n\n</details>\n'
    )

    # WHAT THE PROPERTY ACTUALLY IS. `onclick` DOES appear in the safe output —
    # as escaped TEXT inside the element (`a&quot; onclick=&quot;alert(1)`),
    # which is correct and harmless. So "onclick is absent" is the WRONG
    # property and an assertion for it would fail on correct output. What must
    # never appear is the ATTRIBUTE form: `=` followed by a real quote. The safe
    # rendering has `onclick=&quot;`; only a raw pass-through yields `onclick="`.
    #
    # THE TWO ASSERTIONS THIS REPLACED WERE BOTH DEFECTIVE, which is why the
    # reasoning is written out rather than left to the reader:
    #   * `'onclick="alert(1)"' not in html` could NEVER FAIL — the payload ends
    #     without a closing quote, so that literal appears in neither the safe
    #     nor the unsafe rendering. A dead assertion on an XSS test.
    #   * `"onclick" not in html or "&quot;" in html` did the real work, but only
    #     because this fixture is the sole source of a `"` in the document. One
    #     more quote in any future fixture makes the right disjunct true
    #     unconditionally and the whole test goes vacuous — silently, on a
    #     security check.
    assert "<summary>" in html, (
        "the summary start tag is not bare, so the text broke out of the "
        "element and became attributes"
    )
    assert 'onclick="' not in html, (
        "an attribute-shaped onclick reached the output — the escaped form is "
        '`onclick=&quot;`, and only a raw pass-through produces `onclick="`'
    )


# ------------------------------------------------- the newest message stays --


def test_the_newest_message_stays_a_heading_and_stays_in_the_toc() -> None:
    """T5 non-disturbance, and the reason the newest section is not a <details>.

    ``gmail.py`` emits the newest message as a plain ``## H2`` and only OLDER
    messages as ``<details>``, so "newest expanded" is structural — it is not
    collapsed because it was never wrapped. Converting that H2 into a
    ``<details open>`` to make the thread uniform would delete a heading from
    ``extract_headings``, breaking T5's anchors and the TOC that reads them.

    (The plan's acceptance line says "newest is `open`". Taken literally that
    describes a state the corpus cannot produce; see the task notes.)

    THE ANGLE BRACKETS ARE STILL NOT ASSERTED HERE, and the reason has changed.
    They used to be absent because ``gmail.py`` emitted the newest message's H2
    UNESCAPED, so CommonMark read ``<dana@example.test>`` as an email autolink
    and stripped the brackets from both the rendered heading and
    ``extract_headings`` — defect #57, a producer asymmetry in which the same
    address rendered two ways in one document. #57 is now FIXED: the producer
    escapes both headings, the fixture above carries ``&lt;…&gt;``, and the
    brackets survive into ``extract_headings``.

    They stay unasserted because this test is about T5 non-disturbance — one
    heading, in the TOC, with an anchor — and the bracket form is
    :mod:`tests.test_gmail_thread`'s contract to hold, at the producer, where a
    regression would actually originate. Asserting it in two places would mean
    two places to update and only one of them named in the failure.

    The anchor id is unchanged by the escape, which was MEASURED rather than
    assumed: both the raw and the escaped heading slug to
    ``2026-03-09-12-00-dana-vendor-dana-example-test``, because the slugger
    drops the punctuation that the two forms differ in.
    """
    html = render_markdown(THREAD)
    headings = extract_headings(THREAD)

    assert "<h2" in html
    assert len(headings) == 1, (
        f"expected exactly the newest message's heading, got "
        f"{[h.text for h in headings]} — a <details> section leaked a heading "
        "into the TOC, or the newest one was dropped from it"
    )
    # The identity of the heading, minus the bracket form #57 removes.
    assert headings[0].text.startswith("2026-03-09 12:00 — Dana Vendor")
    assert "dana@example.test" in headings[0].text, (
        "the sender address left the TOC entry entirely"
    )
    assert headings[0].id, "the heading lost its anchor"


def test_older_sections_are_closed() -> None:
    """Collapsed by default is the feature; an `open` attribute would defeat it."""
    html = render_markdown(THREAD)

    assert "<details open" not in html
    assert html.count("<details") == COLLAPSED_SECTIONS


# --------------------------------------------------------- malformed input --


def test_an_unterminated_details_degrades_to_text_rather_than_swallowing_the_page(
) -> None:
    """A missing ``</details>`` must not consume the rest of the document.

    The wikilink rule takes the same position for the same reason: an
    unterminated construct declines and renders literally, because swallowing
    the remainder of the note is a far worse failure than showing a stray tag.
    """
    html = render_markdown("<details>\n<summary>S</summary>\n\nbody\n\nAfter.\n")

    assert "After." in html
    assert "<details" not in html, (
        "an unterminated <details> was rendered as an element, so everything "
        "after it is inside a section that never closes"
    )


def test_a_details_without_a_summary_declines() -> None:
    """No summary means no section label, and an unlabelled collapsed block
    hides content behind a control that says nothing about what it hides."""
    html = render_markdown("<details>\n\nbody\n\n</details>\n")

    assert "<details" not in html, (
        "a summary-less <details> was consumed into an element, so its content "
        "sits behind a control with no label saying what it hides"
    )
    # ABSENCE NEEDS PRESENCE. Without the two below, an implementation that
    # returned "" — or dropped the whole construct — satisfies the assertion
    # above and this test proves nothing. Declining must leave the document
    # INTACT, not merely un-elemented.
    assert "<p>body</p>" in html, (
        "the body did not survive the decline — the construct was dropped "
        "rather than rendered literally"
    )
    assert "&lt;details&gt;" in html, (
        "the declined marker is not visible as escaped text; silently removing "
        "it hides from the reader that the document contains it"
    )
