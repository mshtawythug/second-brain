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

import base64
from datetime import datetime

import pytest

from brain.ingest.gmail import to_extracted_thread
from brain.ui.render import (
    EMAIL_THREAD_CONTENT_TYPE,
    extract_headings,
    render_markdown,
)

#: Opens no database connection — this module renders strings.
pytestmark = pytest.mark.nodb


def render_thread(text: str) -> str:
    """Render ``text`` as the body of a document the gmail assembler produced.

    Every assertion in this module is about the thread construct, so every one
    of them has to declare the marker that switches the construct on. Declared
    once, here, rather than repeated at ~10 call sites: the point of the marker
    is that recognition is a property of the DOCUMENT, and a helper named for
    the document type says that where a keyword argument repeated ten times
    would just be noise.
    """
    return render_markdown(text, content_type=EMAIL_THREAD_CONTENT_TYPE)

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
    html = render_thread(THREAD)

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
    html = render_thread(THREAD)

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
    html = render_thread(
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
    html = render_thread(
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
    html = render_thread(
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
    html = render_thread(
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
    html = render_thread(THREAD)
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
    html = render_thread(THREAD)

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
    html = render_thread("<details>\n<summary>S</summary>\n\nbody\n\nAfter.\n")

    assert "After." in html
    assert "<details" not in html, (
        "an unterminated <details> was rendered as an element, so everything "
        "after it is inside a section that never closes"
    )


def test_a_details_without_a_summary_declines() -> None:
    """No summary means no section label, and an unlabelled collapsed block
    hides content behind a control that says nothing about what it hides."""
    html = render_thread("<details>\n\nbody\n\n</details>\n")

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


# ---------------------------------------------- whose markup is this, though --
#
# The rule above recognises a construct. Nothing in it used to ask WHOSE
# construct it was, so ANY document containing `<details>` + `<summary>` on
# their own lines got the thread treatment — including a hand-authored vault
# note, on which `js/thread.js` then mounted the email-specific "Only my
# replies" checkbox (it fires on `details.thread-message` alone; the
# `THREAD_HEADING_RE` gate beside it only covers the *newest-message* wrap).
#
# The marker is `documents.content_type == "email_thread"`, which
# `to_extracted_thread` stamps and nothing a user types can produce. The
# alternative — keying on the `YYYY-MM-DD HH:MM — sender` summary shape — is a
# shape a person can type, so it would narrow the false-positive rather than
# remove it.


def _thread_messages() -> list[dict[str, object]]:
    """Two synthetic Gmail messages in one thread.

    Built for the producer rather than for the renderer: the point of the test
    below is to render what ``to_extracted_thread`` ACTUALLY emits, so the
    marker cannot be verified against an assumption about the producer's shape.

    No PII — ``@example.test`` is RFC 6761 reserved and both names are invented.
    """
    def message(msg_id: str, when: str, sender: str, body: str) -> dict[str, object]:
        return {
            "id": msg_id,
            "threadId": "thread-synthetic",
            "internalDate": str(int(datetime.fromisoformat(when).timestamp() * 1000)),
            "labelIds": [],
            "payload": {
                "mimeType": "text/plain",
                "headers": [
                    {"name": "Subject", "value": "quarterly widget order"},
                    {"name": "From", "value": sender},
                    {"name": "To", "value": "Sam Buyer <sam@example.test>"},
                    {"name": "Date", "value": when},
                ],
                "body": {
                    "data": base64.urlsafe_b64encode(body.encode()).decode().rstrip("=")
                },
            },
        }

    return [
        message("m1", "2026-03-07 08:00:00+00:00",
                "Dana Vendor <dana@example.test>", "The oldest message."),
        message("m2", "2026-03-09 12:00:00+00:00",
                "Sam Buyer <sam@example.test>", "The latest reply."),
    ]


def test_the_marker_is_the_one_the_gmail_assembler_actually_stamps() -> None:
    """Drift guard across the producer/renderer boundary.

    ``EMAIL_THREAD_CONTENT_TYPE`` is a second spelling of a value that
    originates in ``ingest/gmail.py``. If the producer ever stamps something
    else, the renderer would silently stop recognising every real thread — the
    failure would be a corpus-wide rendering regression with no red test, which
    is exactly the class of defect the narrowing is supposed to prevent, not to
    introduce.
    """
    doc = to_extracted_thread(_thread_messages())

    assert doc.content_type == EMAIL_THREAD_CONTENT_TYPE, (
        "brain.ui.render.EMAIL_THREAD_CONTENT_TYPE no longer matches what "
        "brain.ingest.gmail.to_extracted_thread stamps, so no real email "
        "thread will be recognised by the renderer any more"
    )


def test_a_real_assembled_thread_still_renders_as_sections() -> None:
    """DIRECTION 1 — the narrowing must not break what it was protecting.

    Renders the producer's own output, not the hand-written ``THREAD`` fixture,
    so "real Gmail threads keep working" is checked against ``gmail.py`` as it
    exists on disk rather than against this module's copy of its shape.
    """
    doc = to_extracted_thread(_thread_messages())
    assert "<details>" in doc.content, (
        "the fixture stopped producing a collapsed section, so the assertions "
        "below would pass over markup that contains nothing to recognise"
    )

    html = render_markdown(doc.content, content_type=doc.content_type)

    assert "<details class=\"thread-message\">" in html, (
        "a thread straight out of to_extracted_thread no longer renders as a "
        "section — the narrowing broke the case it exists to serve"
    )
    assert "&lt;details&gt;" not in html


def test_a_hand_authored_note_in_the_same_shape_gets_no_thread_markup() -> None:
    """DIRECTION 2 — the defect itself.

    Byte-identical markup to :data:`THREAD`, differing ONLY in the document's
    ``content_type``. Any recognition rule that reads the body alone passes
    direction 1 and fails here, which is why one test could never have covered
    both.

    ``thread-message`` is the specific class asserted rather than ``<details``,
    because that class is what ``js/thread.js`` queries to decide whether to
    mount the email-only "Only my replies" control. Asserting the element alone
    would still pass if the class were emitted on a vault note.
    """
    html = render_markdown(THREAD, content_type="note")

    assert "thread-message" not in html, (
        "a hand-authored note was given the email-thread class, so the UI "
        "mounts an email-only reply filter on ordinary vault content"
    )
    # ABSENCE NEEDS PRESENCE. Without this, a renderer that returned "" — or
    # dropped the construct — satisfies the assertion above while destroying
    # the note. Declining must leave the document INTACT and visible, which is
    # the same standard `test_a_details_without_a_summary_declines` holds.
    assert "&lt;details&gt;" in html, (
        "the unrecognised marker vanished instead of rendering as text — the "
        "user's own markup was silently deleted"
    )
    assert "The oldest message in the thread." in html
    assert "<h2" in html, "the note's ordinary heading did not survive"


def test_an_unmarked_document_is_the_default() -> None:
    """Fail-closed: a caller that says nothing gets no thread recognition.

    ``notes_service`` is the only production caller and it always passes the
    row's ``content_type``. The default still matters: the next caller added
    without reading this file gets the SAFE behaviour, and a marker that had to
    be remembered in order to be respected is the roster failure this project
    has paid for repeatedly.
    """
    assert "thread-message" not in render_markdown(THREAD)
