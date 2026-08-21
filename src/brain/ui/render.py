"""Pure: note markdown → the HTML the inspector shows.

Rendering happens **on the server, in Python**, for four reasons (spec §4.3):
zero new dependencies (``markdown-it-py>=3.0`` is already declared and was
otherwise unused); XSS defence concentrated in one testable place; no extra
round trip, since the HTML rides along on the note fetch and the save response;
and ``[[wikilink]]`` needs a real parser rather than a regex over rendered HTML,
which would corrupt code blocks.

Three hardening measures, each covered by a test:

1. ``html=False``. **This is not the preset default** — verified on
   markdown-it-py 4.2.0, ``MarkdownIt("commonmark")`` alone renders a literal
   ``<script>`` tag straight through, because the CommonMark preset turns raw
   HTML *on*. The option is passed explicitly, and ``tests/test_ui_render.py``
   asserts the escaping rather than trusting a preset. (The F14 design document
   states the opposite; the document is wrong and the code follows the
   measurement.)
2. A ``link_open`` render rule that drops any href outside
   ``http`` / ``https`` / ``mailto`` / a same-origin relative path. markdown-it's
   own ``validateLink`` already rejects ``javascript:``, ``vbscript:`` and
   non-image ``data:``; this is a second, explicit allowlist so the guarantee
   survives an upstream change to that heuristic.
3. Wikilinks are a registered **inline rule**, not a post-hoc regex. Because
   markdown-it's ``backticks`` rule runs before ``link`` and fenced blocks never
   reach inline rules at all, ``[[Target]]`` inside code is left verbatim for
   free — the same correctness property ``vault.rename.collect_references``
   relies on.
"""
from __future__ import annotations

import re
from collections.abc import Callable
from dataclasses import dataclass
from html import unescape
from typing import Any

from markdown_it import MarkdownIt
from markdown_it.common.utils import escapeHtml
from markdown_it.token import Token
from markdown_it.utils import EnvType, OptionsDict
from mdit_py_plugins.tasklists import tasklists_plugin
from pygments import highlight
from pygments.formatters import HtmlFormatter
from pygments.lexers import get_lexer_by_name
from pygments.util import ClassNotFound

#: URL schemes a rendered link may use. Everything else — ``javascript:``,
#: ``data:``, ``vbscript:``, ``file:`` — is stripped, so the link text survives
#: but the navigation does not.
ALLOWED_SCHEMES: frozenset[str] = frozenset({"http", "https", "mailto"})

#: Wiki links that resolve to a real document get this class; the rest get
#: ``--unresolved`` so the stylesheet can show them as a dangling reference
#: rather than silently rendering them as ordinary text.
_LINK_CLASS = "wikilink"
_UNRESOLVED_CLASS = "wikilink wikilink--unresolved"

#: A resolver maps a wiki-link target to an opaque document id, or ``None``
#: when nothing in the corpus matches.
Resolver = Callable[[str], str | None]

#: The attribute the link-kind stylesheet keys off. Same ``data-brain-`` prefix
#: the Quartz overlay uses (``quartz_overrides/.../linkKindMark.ts``), so the
#: port can reuse that stylesheet's selectors verbatim.
LINK_KIND_ATTR = "data-brain-link-kind"

#: Prefixes, ported from ``linkKindMark.ts``. Both the relative and the
#: leading-slash forms appear in real bodies depending on how the link was
#: written, so between them the prefix list and :data:`_TAG_INFIX` cover every
#: shape.
#:
#: ``_TAG_PREFIXES`` deliberately omits the leading-slash ``/tags/`` that
#: ``linkKindMark.ts``'s ``TAG_PREFIXES`` carries: :data:`_TAG_INFIX` is exactly
#: that string, and a leading-slash URL matches it at offset 0. Listing it in
#: both places would be a branch no input can reach on its own.
_TAG_PREFIXES: tuple[str, ...] = ("tags/", "./tags/")
#: A host-qualified tag URL carries the tag segment mid-string, so the prefix
#: test alone would miss it — see :func:`_is_tag_url`. Doubles as the
#: leading-slash case, per the note above.
_TAG_INFIX = "/tags/"
_EXTERNAL_PREFIXES: tuple[str, ...] = ("http://", "https://", "mailto:")
_INGESTED_PREFIXES: tuple[str, ...] = ("_ingested/", "/_ingested/", "./_ingested/")

#: Runs of anything that is not an ASCII letter or digit collapse to one dash.
_NON_SLUG = re.compile(r"[^a-z0-9]+")

#: Anchor for a heading whose text slugifies to nothing at all (``## ???``).
_EMPTY_SLUG = "section"

#: Token types whose ``content`` is literal heading text. ``wikilink`` is one of
#: them because its content is the visible label, and a heading that says
#: ``## See [[Weekly Review|the review]]`` reads "See the review" in a TOC.
_TEXT_TOKEN_TYPES = frozenset({"text", "code_inline", "wikilink"})


@dataclass(frozen=True)
class Heading:
    """One entry in a document's table of contents.

    ``id`` is the anchor stamped onto the corresponding ``<h1>``…``<h6>``, so a
    TOC link is ``#{id}``. It is unique within the document — see
    :class:`_Slugger`.
    """

    level: int
    text: str
    id: str


def _slugify(text: str) -> str:
    """Deterministic ASCII anchor base for a heading's plain text.

    **No github-slugger parity is attempted.** Nothing outside ``brain ui``
    consumes these anchors — the wiki has its own slugger for its own emitted
    filenames — so the only requirements are determinism, uniqueness, and being
    safe inside an attribute. Stripping to ``[a-z0-9-]`` satisfies the third by
    construction rather than by escaping, which is why a heading full of markup
    or quotes cannot produce an injectable id.
    """
    return _NON_SLUG.sub("-", text.casefold()).strip("-") or _EMPTY_SLUG


class _Slugger:
    """Mints document-unique anchors, in document order.

    A plain per-base counter is not enough: ``## Notes``, ``## Notes 1``,
    ``## Notes`` would mint ``notes``, ``notes-1``, ``notes-1``. The used-id set
    is what makes the guarantee hold against authored text that happens to
    collide with a generated suffix.

    One instance per document. The render pass and :func:`extract_headings` mint
    the same ids only while they walk the same heading sequence — which is no
    longer something the two share by construction, since they parse with
    different envs. See :func:`extract_headings` for why it holds and for the
    test that holds it.
    """

    def __init__(self) -> None:
        self._counts: dict[str, int] = {}
        self._used: set[str] = set()

    def slug(self, text: str) -> str:
        base = _slugify(text)
        suffix = self._counts.get(base, 0)
        candidate = base if suffix == 0 else f"{base}-{suffix}"
        while candidate in self._used:
            suffix += 1
            candidate = f"{base}-{suffix}"
        self._counts[base] = suffix + 1
        self._used.add(candidate)
        return candidate


def _starts_with_any(url: str, prefixes: tuple[str, ...]) -> bool:
    """Case-insensitive prefix test — hand-edited bodies carry ``HTTPS://``."""
    lowered = url.lower()
    return any(lowered.startswith(prefix) for prefix in prefixes)


def _is_tag_url(url: str) -> bool:
    """True for ``tags/x``, ``./tags/x``, ``/tags/x`` and ``https://h/tags/x``.

    Mirrors ``linkKindMark.ts``'s ``isTagUrl`` — prefix OR infix — so one
    classifier has one meaning on both sides of the port.

    That agreement is recent and the port is the half that was right. This
    module took the infix reading from the overlay's *stated contract* ("starts
    with ``tags/`` … or CONTAINS ``/tags/``") while the overlay's own
    ``classifyLink`` still only prefix-matched, so ``https://host/tags/retro``
    was a ``tag`` here and an ``external`` there. ``35fc486`` closed that by
    adding ``TAG_INFIX``/``isTagUrl`` to the TypeScript, for the two reasons
    this port had already banked on: a host-qualified link to this site's own
    tag page IS a tag link, and under prefix-only the tag/external precedence is
    **inert** — the two prefix sets are disjoint, so no reordering could change
    any answer, and an ordering nothing can observe is an ordering no test can
    defend. The accepted cost, on both sides, is that a genuinely external
    ``https://elsewhere/tags/x`` is labelled ``tag``.
    """
    lowered = url.lower()
    return lowered.startswith(_TAG_PREFIXES) or _TAG_INFIX in lowered


def classify_link_kind(url: str) -> str:
    """Bucket a link target for the stylesheet. Port of ``classifyLink``.

    **The order is the contract**, not the buckets. An absolute
    ``https://…/tags/x`` is both a tag URL and an external URL, and the overlay
    resolves that by testing ``tag`` first — flip the two and every absolute tag
    link loses its tag treatment. ``ingested`` is a refinement of ``wiki`` —
    both are vault-internal — so it likewise has to precede the fallback rather
    than sit beside it.

    The overlay has a fifth kind, ``derived``, ahead of all of these: a link
    inside a Phase D evidence fence. It is **absent here, not forgotten**. The
    fence is stripped out of ``documents.content`` by ``vault.sync``
    (``_normalized_body`` → ``strip_fence``) before anything reaches this
    module, so no derived link exists on this path to classify; a branch for it
    would be one that cannot change behaviour. If the fence is ever surfaced in
    the inspector, its test belongs above the ``tag`` check.
    """
    if _is_tag_url(url):
        return "tag"
    if _starts_with_any(url, _EXTERNAL_PREFIXES):
        return "external"
    if _starts_with_any(url, _INGESTED_PREFIXES):
        return "ingested"
    return "wiki"


def _scheme_is_allowed(href: str) -> bool:
    """True when ``href`` is relative, or carries an allowlisted scheme.

    A relative path has no ``:`` before its first ``/``, ``?`` or ``#``. Testing
    it that way (rather than with a naive ``":" in href``) keeps
    ``notes/2026-07-26.md`` and ``#heading`` working while still catching
    ``javascript:alert(1)``.
    """
    for i, ch in enumerate(href):
        if ch == ":":
            return href[:i].lower() in ALLOWED_SCHEMES
        if ch in "/?#":
            return True
    return True


def _wikilink_inline_rule(state: Any, silent: bool) -> bool:
    """Consume ``[[Target]]`` / ``[[Target|Alias]]`` into a ``wikilink`` token.

    Returns ``False`` — meaning "not mine, try the next rule" — for anything
    unterminated, multi-line, or containing a nested bracket, so malformed input
    degrades to literal text instead of swallowing the rest of the paragraph.

    **``![[Embed]]`` is declined on purpose.** Obsidian's embed syntax is not
    supported, and before this the rule matched the ``[[…]]`` part and left the
    ``!`` behind — producing a literal ``!`` followed by a LIVE
    unresolved-wikilink anchor. That is wrong output, not absent output, and the
    difference matters: an unsupported construct rendering literally is
    defensible, one rendering as a broken link plus a stray character is a bug.
    Declining here makes the whole thing render as the text the author typed,
    which is the honest degradation until embeds are actually implemented.
    """
    src: str = state.src
    pos: int = state.pos
    if not src.startswith("[[", pos):
        return False
    if pos > 0 and src[pos - 1] == "!":
        return False
    end = src.find("]]", pos + 2)
    if end < 0:
        return False
    inner = src[pos + 2 : end]
    if not inner or "\n" in inner or "[" in inner or "]" in inner:
        return False

    target, _, alias = inner.partition("|")
    target = target.strip()
    if not target:
        return False
    label = alias.strip() or target

    if not silent:
        token = state.push("wikilink", "", 0)
        token.content = label
        token.meta = {"target": target}
    state.pos = end + 2
    return True


def _render_wikilink(
    self: Any, tokens: list[Token], idx: int, options: OptionsDict, env: EnvType
) -> str:
    """Render one ``wikilink`` token, escaping both the label and the href."""
    token = tokens[idx]
    target = str(token.meta.get("target", ""))
    resolver = env.get("wikilink_resolver")
    doc_id = resolver(target) if resolver is not None else None

    # ``escapeHtml`` is a module-level function in markdown-it-py 4.x, NOT a
    # method on the renderer (verified: RendererHTML has no such attribute).
    label = escapeHtml(token.content)
    # A wiki link has no href to classify, so the TARGET is what gets bucketed —
    # `[[tags/retro]]` is a tag link however it was aliased. The value is one of
    # four literals, never caller text, so the attribute cannot be broken out of.
    kind = f'{LINK_KIND_ATTR}="{classify_link_kind(target)}"'
    if doc_id is None:
        return (
            f'<a class="{_UNRESOLVED_CLASS}" {kind} '
            f'title="no note matches this link">{label}</a>'
        )
    href = escapeHtml(f"?id={doc_id}")
    return f'<a class="{_LINK_CLASS}" {kind} href="{href}">{label}</a>'


def _render_link_open(
    self: Any, tokens: list[Token], idx: int, options: OptionsDict, env: EnvType
) -> str:
    """Drop any href whose scheme is not allowlisted, then render normally.

    A blocked link is deliberately left **unstamped**: its href has just been
    emptied, so classifying it would bucket every rejected scheme as ``wiki`` —
    dressing up the one link on the page that is not navigable as the most
    ordinary kind there is. ``link--blocked`` already says what it is.
    """
    token = tokens[idx]
    href = token.attrGet("href")
    if href is not None and not _scheme_is_allowed(str(href)):
        token.attrSet("href", "")
        token.attrSet("class", "link--blocked")
    else:
        token.attrSet(LINK_KIND_ATTR, classify_link_kind(str(href or "")))
    return str(self.renderToken(tokens, idx, options, env))


def _heading_text(inline: Token | None) -> str:
    """Plain text of a heading, with inline markup flattened away.

    markdown-it's inline children are a FLAT list, so the text inside a link or
    emphasis is reached without recursion; the ``_open``/``_close`` tokens carry
    no content and drop out on their own.
    """
    if inline is None or inline.type != "inline":
        return ""
    children = inline.children
    if not children:
        return inline.content.strip()
    return "".join(
        child.content for child in children if child.type in _TEXT_TOKEN_TYPES
    ).strip()


def _document_slugger(env: EnvType) -> _Slugger:
    """The per-document slugger, created on first use.

    Lives in ``env`` rather than on the renderer so that two concurrent renders
    cannot share a counter — the same reason :func:`build_renderer` is a
    function and not a module-level singleton.
    """
    slugger = env.get("heading_slugger")
    if not isinstance(slugger, _Slugger):
        slugger = _Slugger()
        env["heading_slugger"] = slugger
    return slugger


def _render_heading_open(
    self: Any, tokens: list[Token], idx: int, options: OptionsDict, env: EnvType
) -> str:
    """Stamp the anchor :func:`extract_headings` will point a TOC at."""
    token = tokens[idx]
    inline = tokens[idx + 1] if idx + 1 < len(tokens) else None
    token.attrSet("id", _document_slugger(env).slug(_heading_text(inline)))
    return str(self.renderToken(tokens, idx, options, env))


def _highlight_code(code: str, lang: str, _attrs: str) -> str:
    """Tokenise a fenced block with Pygments. Returns ``""`` to decline.

    **Class-based, never inline styles.** ``HtmlFormatter(noclasses=True)``
    emits ``style="color: #..."`` on every single token, and the app serves
    ``style-src 'self'`` with no ``'unsafe-inline'`` — so the browser would drop
    all of it and the code would render unhighlighted, with nothing connecting
    the two. That is the same defect that made table alignment silently fail,
    at hundreds of tokens per block instead of one attribute per cell.

    ``nowrap=True`` is equally deliberate: the default formatter returns
    ``<div class="highlight"><pre>…``, and markdown-it only substitutes a
    highlighter's output for the whole block when it starts with ``<pre``.
    Anything else is inserted *inside* the ``<pre><code>`` the fence renderer
    builds — so the default would nest a div inside a code element. ``nowrap``
    returns bare spans, which is exactly what belongs there, and it preserves
    the ``class="language-x"`` the fence renderer already emits.

    Declining (``""``) rather than raising on an unknown language lets
    markdown-it fall back to its own escaping path, which is the correct
    degradation for `````mermaid``, a typo'd language name, and an untagged
    fence alike.

    There is deliberately no ``if not lang`` early return. It reads like a
    guard and is not one: ``get_lexer_by_name("")`` raises ``ClassNotFound``
    like any other unknown alias, so the branch below already handles an
    untagged fence identically. Measured both ways — removing it changed no
    test — and the round trip it would save is **0.2 microseconds** per fence.
    A condition that cannot change behaviour is the shape this codebase has
    spent real effort deleting.
    """
    try:
        lexer = get_lexer_by_name(lang, stripnl=False)
    except ClassNotFound:
        return ""
    return str(highlight(code, lexer, HtmlFormatter(nowrap=True)))


#: markdown-it emits column alignment as an INLINE STYLE
#: (``style="text-align:center"``). The app serves ``style-src 'self'`` with no
#: ``'unsafe-inline'`` (``security.py:65``), and that directive governs style
#: ATTRIBUTES as well as ``<style>`` elements — so the browser drops those
#: declarations and every aligned column silently renders left-aligned. Rewriting
#: them to classes keeps the alignment AND keeps the CSP strict; the stylesheet
#: carries the matching rules.
_ALIGN_CLASSES = {
    "text-align:center": "cell--center",
    "text-align:right": "cell--right",
    "text-align:left": "cell--left",
}


def _render_cell_open(
    self: Any,
    tokens: list[Token],
    idx: int,
    options: OptionsDict,
    env: EnvType,
) -> str:
    """Translate a table cell's inline alignment style into a class."""
    token = tokens[idx]
    style = token.attrGet("style")
    if style is not None:
        cls = _ALIGN_CLASSES.get(str(style).replace(" ", ""))
        # Drop the style attribute either way: an unrecognised value is still an
        # inline style the CSP will reject, and leaving it would emit markup
        # whose only effect is a console error.
        token.attrs = {k: v for k, v in token.attrs.items() if k != "style"}
        if cls is not None:
            token.attrJoin("class", cls)
    return str(self.renderToken(tokens, idx, options, env))


# --------------------------------------------------------- email threads ---
#
# T18, decided option (a): RECOGNISE AND RE-EMIT, never pass through.
#
# `brain.ingest.gmail._format_thread_section` wraps every message except the
# most recent in `<details><summary>…`, because Quartz and CommonMark both treat
# raw HTML as opaque pass-through. `html=False` here does not, so those lines
# rendered as the literal text `&lt;details&gt;` — visible markup in **58
# corpus documents**, 89.1% of `email_thread` (docs/audits/2026-08-13-phase2-recon.md).
#
# THE SAFETY ARGUMENT, which is why `html=False` stays exactly as it is:
# nothing below admits HTML from the source. These rules match two specific
# tags and then GENERATE elements of their own, with NO attributes, ever. An
# `<details onclick=…>` in a document does not match (the pattern is anchored
# and admits no attributes), so it stays escaped text like any other tag. The
# difference between "generated" and "passed through" is the whole of A3.
#
# ENTITIES ARE OUT OF SCOPE and that was verified rather than assumed:
# markdown-it's entity rule is not gated by `options.html`, so `&lt;` already
# decodes today. See the recon audit.
_THREAD_OPEN_RE = re.compile(r"^<details>[ \t]*$")
_THREAD_CLOSE_RE = re.compile(r"^</details>[ \t]*$")
_THREAD_SUMMARY_RE = re.compile(r"^<summary>(?P<text>.*)</summary>[ \t]*$")

#: The ``documents.content_type`` that ``ingest/gmail.py::to_extracted_thread``
#: stamps on an assembled thread, and the ONLY thing that switches the rules
#: below on.
#:
#: WHY A CONTENT TYPE AND NOT A SHAPE. The three patterns above describe markup
#: any person can type: ``<details>`` and ``<summary>`` are ordinary HTML and a
#: hand-authored vault note written that way was, until this marker existed,
#: re-emitted as ``details.thread-message`` — the class ``js/thread.js`` queries
#: to decide whether to mount the email-only "Only my replies" checkbox. So a
#: note about anything at all grew a control offering to filter it by the
#: reader's own email address.
#:
#: Narrowing the *shape* instead — requiring the ``YYYY-MM-DD HH:MM — sender``
#: summary the assembler emits, which is what ``js/thread.js``'s
#: ``THREAD_HEADING_RE`` does for the newest-message wrap — would have made the
#: false positive rarer without making it impossible: that is still a shape a
#: person can type, and a recognition rule that a user can trip by writing
#: prose is the defect, not the frequency of it.
#:
#: ``content_type`` is not that. It is stamped by the ingest pipeline from the
#: assembler's own ``ExtractedDoc``; no editor surface writes it as a side
#: effect of typing a body. ``tests/test_ui_render_email_thread.py`` asserts
#: this constant against ``to_extracted_thread``'s real output, so a producer
#: rename is a red test rather than a corpus-wide silent un-rendering.
EMAIL_THREAD_CONTENT_TYPE = "email_thread"

#: Where the per-render answer to "is this document an email thread?" is
#: carried. On the parser env, beside :data:`_THREAD_DEPTH`, because that is the
#: only channel a markdown-it block rule has to its caller.
_THREAD_ENABLED = "thread_sections_enabled"

#: Where the open/close balance is tracked, per render, on the parser env.
_THREAD_DEPTH = "thread_details_depth"


def _thread_line(state: Any, line_no: int) -> str:
    """The raw source of one block line, without its indent."""
    return str(
        state.src[
            state.bMarks[line_no] + state.tShift[line_no]: state.eMarks[line_no]
        ]
    )


def _thread_block_is_complete(state: Any, start_line: int, end_line: int) -> bool:
    """Is this ``<details>`` a whole, well-formed thread section?

    VALIDATED BEFORE ANYTHING IS CONSUMED, and that ordering is the point. An
    earlier version of this rule opened an element the moment it saw
    ``<details>``; a document whose closing tag was missing then rendered an
    element that never closed, and **every following paragraph fell inside a
    collapsed section** — the rest of the note simply disappeared behind a
    twisty. Declining here instead leaves the line to the paragraph rule, which
    escapes it, so a malformed section degrades to visible text: the reader sees
    something odd rather than losing the document.

    Requires, in order: a ``<summary>`` on the next non-blank line, and a
    ``</details>`` before either EOF or a second ``<details>``. Thread sections
    are emitted flat by the assembler and never nest, so an inner ``<details>``
    means the markup is not what this rule is for.
    """
    saw_summary = False
    for line_no in range(start_line + 1, end_line):
        line = _thread_line(state, line_no)
        if not saw_summary:
            if not line.strip():
                continue
            if _THREAD_SUMMARY_RE.match(line) is None:
                return False
            saw_summary = True
            continue
        if _THREAD_OPEN_RE.match(line):
            return False
        if _THREAD_CLOSE_RE.match(line):
            return True
    return False


def _thread_html_rule(state: Any, start_line: int, end_line: int, silent: bool) -> bool:
    """Turn the assembler's three marker lines into real block tokens.

    ONE LINE AT A TIME, rather than scanning for the matching close. The
    assembler separates the summary and the body with blank lines precisely so
    markdown processors parse the body as markdown, and handling each marker
    independently preserves that: everything between the markers goes through
    the normal block pipeline and keeps its emphasis, lists and links.

    THE DEPTH COUNTER IS A SAFETY GUARD, not bookkeeping. A document containing
    a bare `</details>` with no opener must not emit a closing tag: it would
    close whichever element the note body is rendered inside and let the rest of
    the document escape its container. Refusing to close what we did not open
    leaves the stray marker to be escaped as ordinary text, which is both safe
    and honest about what the document contains.
    """
    # WHOSE MARKUP IS THIS. Asked FIRST, before a single pattern is matched,
    # so an unmarked document costs one dict lookup and takes the ordinary
    # `html=False` path — its `<details>` escapes to visible text like any
    # other tag it contains. See EMAIL_THREAD_CONTENT_TYPE for why the question
    # is about the document rather than about the line.
    if not state.env.get(_THREAD_ENABLED):
        return False

    line_start = state.bMarks[start_line] + state.tShift[start_line]
    line = state.src[line_start:state.eMarks[start_line]]

    depth = state.env.get(_THREAD_DEPTH, 0)

    if _THREAD_OPEN_RE.match(line):
        if not _thread_block_is_complete(state, start_line, end_line):
            return False                    # malformed: degrade to escaped text
        if not silent:
            token = state.push("thread_details_open", "details", 1)
            token.map = [start_line, start_line + 1]
            token.block = True
            state.env[_THREAD_DEPTH] = depth + 1
        state.line = start_line + 1
        return True

    if _THREAD_CLOSE_RE.match(line):
        if depth <= 0:
            return False                    # unbalanced: leave it to be escaped
        if not silent:
            token = state.push("thread_details_close", "details", -1)
            token.map = [start_line, start_line + 1]
            token.block = True
            state.env[_THREAD_DEPTH] = depth - 1
        state.line = start_line + 1
        return True

    summary = _THREAD_SUMMARY_RE.match(line)
    if summary is not None and depth > 0:
        if not silent:
            open_token = state.push("thread_summary_open", "summary", 1)
            open_token.map = [start_line, start_line + 1]
            open_token.block = True

            # EXACTLY ONE LEVEL OF ESCAPING, and getting this wrong is visible.
            # The assembler already escaped the heading (`gmail.py`'s
            # `_format_thread_section`, the `escaped_heading` binding — named
            # rather than cited by line, because that line has already moved
            # once) so the `Name <addr>` form would survive Quartz's
            # pass-through. Emitting
            # that text unchanged through the renderer's own escaper yields
            # `&amp;lt;` and the reader sees the entity spelled out. So it is
            # unescaped once here and escaped once on the way out.
            #
            # ITS OWN TOKEN TYPE, NOT AN `inline` ONE, and that is a bug fix
            # rather than a preference. Pushing `inline` with pre-built children
            # emitted the label TWICE (`<summary>SS</summary>`): markdown-it's
            # core `inline` rule parses `token.content` and APPENDS the result
            # to whatever children the token already carries, so the label was
            # rendered once from the child supplied here and once from the
            # parse. A dedicated token renders exactly what it holds.
            #
            # Staying off the inline pipeline is also correct on its own terms:
            # unescaping produces `<addr>`, which CommonMark's autolink rule
            # would turn into a mailto link, and a summary is a label rather
            # than a place to discover new syntax.
            label = state.push("thread_summary_text", "", 0)
            label.content = unescape(summary.group("text"))
            label.map = [start_line, start_line + 1]

            close_token = state.push("thread_summary_close", "summary", -1)
            close_token.block = True
        state.line = start_line + 1
        return True

    return False


def _render_thread_details_open(*_args: Any, **_kwargs: Any) -> str:
    return "<details class=\"thread-message\">\n"


def _render_thread_details_close(*_args: Any, **_kwargs: Any) -> str:
    return "</details>\n"


def _render_thread_summary_open(*_args: Any, **_kwargs: Any) -> str:
    return "<summary>"


def _render_thread_summary_text(
    self: Any, tokens: list[Token], idx: int, options: OptionsDict, env: EnvType
) -> str:
    """The summary label, escaped exactly once.

    ``escapeHtml`` is the same escaper every text token goes through, so the
    label is no more trusted than ordinary prose — a ``<script>`` typed into a
    summary comes out inert, which is asserted rather than assumed.
    """
    return str(escapeHtml(tokens[idx].content))


def _render_thread_summary_close(*_args: Any, **_kwargs: Any) -> str:
    return "</summary>\n"


def build_renderer() -> MarkdownIt:
    """Construct the configured parser.

    Kept as a function (rather than a module-level singleton) because
    ``MarkdownIt`` instances carry mutable rule state; one per call is cheap and
    removes any chance of cross-request contamination.
    """
    md = MarkdownIt(
        "commonmark",
        {"html": False, "linkify": False, "highlight": _highlight_code},
    )
    # Phase 1 parity. Both rules ship WITH markdown-it-py — `enable` only turns
    # on grammar that is already present, so neither adds a dependency.
    #
    # Measured against the live corpus rather than estimated:
    #   table          466 documents (460 ingested, 6 vault)
    #   strikethrough   31 documents (all ingested)
    #
    # `html: False` above still holds, so neither construct opens an HTML hole:
    # a table cell's contents go through the same inline pipeline (and the same
    # `link_open` scheme check) as any other text.
    md.enable("table")
    md.enable("strikethrough")
    # Task lists: 180 corpus documents, 6,745 items (159 ingested / 21 vault),
    # counted with the tokenizer rather than a regex — see the phase 1 tests.
    # The ONLY item here that needs a package beyond markdown-it-py itself.
    #
    # Left at the default `enabled=False`, which renders `disabled="disabled"`
    # checkboxes. That is correct rather than lazy: the inspector is a READ
    # surface, and an interactive checkbox would offer a state change that
    # nothing persists — a click that silently does nothing is worse than a
    # control that is visibly inert.
    md.use(tasklists_plugin)
    md.inline.ruler.before("link", "wikilink", _wikilink_inline_rule)
    md.add_render_rule("wikilink", _render_wikilink)
    md.add_render_rule("link_open", _render_link_open)
    md.add_render_rule("heading_open", _render_heading_open)
    md.add_render_rule("th_open", _render_cell_open)
    md.add_render_rule("td_open", _render_cell_open)
    # T18. BEFORE `paragraph`, which would otherwise claim these lines and
    # render them as escaped text — which is exactly the shipped defect.
    md.block.ruler.before("paragraph", "thread_html", _thread_html_rule)
    md.add_render_rule("thread_details_open", _render_thread_details_open)
    md.add_render_rule("thread_details_close", _render_thread_details_close)
    md.add_render_rule("thread_summary_open", _render_thread_summary_open)
    md.add_render_rule("thread_summary_text", _render_thread_summary_text)
    md.add_render_rule("thread_summary_close", _render_thread_summary_close)
    return md


def render_markdown(
    text: str | None,
    *,
    resolver: Resolver | None = None,
    content_type: str | None = None,
) -> str:
    """Render ``text`` to sanitized HTML.

    ``resolver`` maps a wiki-link target to a document id; when it is ``None``
    every wiki link renders unresolved, which is the correct degradation for a
    caller with no database handy (the pure tests).

    ``content_type`` is the DOCUMENT's type — ``documents.content_type``, the
    value the ingest pipeline stamped — and it gates exactly one thing: the
    email-thread ``<details>``/``<summary>`` rules, which fire only for
    :data:`EMAIL_THREAD_CONTENT_TYPE`. Every other rule is content-agnostic and
    stays that way.

    The default is ``None``, which means NO thread recognition, and that
    direction is deliberate. A caller that forgets the argument gets a document
    rendered as ordinary markup — the outcome that is merely less pretty —
    rather than a vault note dressed up as somebody's inbox. ``notes_service``
    is the only production caller and it passes the row's type.

    An empty or ``None`` body returns ``""`` rather than raising — a
    freshly-created note legitimately has no content yet.
    """
    if not text:
        return ""
    md = build_renderer()
    env: dict[str, Any] = {
        "wikilink_resolver": resolver,
        _THREAD_ENABLED: content_type == EMAIL_THREAD_CONTENT_TYPE,
    }
    return str(md.render(text, env))


def extract_headings(text: str | None) -> list[Heading]:
    """The document's headings, with the anchors :func:`render_markdown` stamps.

    **Pass the same string to both.** ``notes_service.read_note`` renders
    ``strip_redundant_title_heading(body, title)``; a TOC built from the
    *unstripped* body would open with an entry pointing at an ``<h1>`` that the
    HTML does not contain (defect S4). Nothing is stripped here on purpose — the
    caller owns that decision, and owning it in one place is what keeps the two
    walks in agreement.

    **The two walks no longer parse with the same env, and this used to claim
    they did.** :func:`render_markdown` builds an env whose
    ``thread_sections_enabled`` comes from the document's ``content_type``;
    this function parses with a hardcoded ``{}``. For an
    :data:`EMAIL_THREAD_CONTENT_TYPE` document that means the render walk has
    the ``<details>``/``<summary>`` rules ON and this one has them OFF. Passing
    the same STRING to both — S4's guard, above — no longer makes them see the
    same tokens; it only makes them see the same INPUT.

    The ids agree anyway, for two reasons, and it is worth separating them
    because only the first is a property of the rule as written:

    1. ``_thread_html_rule`` matches three line shapes — ``<details>``,
       ``</details>``, ``<summary>…</summary>`` — and emits elements of its own.
       It never consumes a heading line and never produces one.
    2. Even if it did, it would not get the chance: ``build_renderer``
       registers it ``before("paragraph", …)``, and markdown-it's block ruler
       runs ``heading`` and ``lheading`` several rules EARLIER, so an ATX or
       setext heading is already claimed before ``thread_html`` is consulted.
       (Verified by reading the ruler back off the built parser, after a
       mutation that added a heading-consuming branch turned out to be inert.)

    Neither is guaranteed by anything structural, so the agreement is held by
    an executable assertion rather than by this paragraph:
    ``tests/test_ui_render_email_thread.py::
    test_the_render_walk_and_the_toc_walk_agree_on_a_thread_document`` renders a
    thread carrying headings inside its collapsed sections and compares the
    ``id`` attributes in the HTML against the ids minted here. It is the only
    test in the render suites that fails when the two walks diverge — measured,
    against both a rule that eats a heading and one that emits a heading.

    Within one walk the ids are minted from a fresh :class:`_Slugger` in
    document order, so they need no shared cache that could go stale.
    """
    if not text:
        return []
    tokens = build_renderer().parse(text, {})
    slugger = _Slugger()
    headings: list[Heading] = []
    for idx, token in enumerate(tokens):
        if token.type != "heading_open":
            continue
        inline = tokens[idx + 1] if idx + 1 < len(tokens) else None
        heading_text = _heading_text(inline)
        headings.append(
            Heading(
                level=int(token.tag[1:]),
                text=heading_text,
                id=slugger.slug(heading_text),
            )
        )
    return headings
