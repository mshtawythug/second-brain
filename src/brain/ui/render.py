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

from collections.abc import Callable
from typing import Any

from markdown_it import MarkdownIt
from markdown_it.common.utils import escapeHtml
from markdown_it.token import Token
from markdown_it.utils import EnvType, OptionsDict

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
    """
    src: str = state.src
    pos: int = state.pos
    if not src.startswith("[[", pos):
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
    if doc_id is None:
        return (
            f'<a class="{_UNRESOLVED_CLASS}" '
            f'title="no note matches this link">{label}</a>'
        )
    href = escapeHtml(f"?id={doc_id}")
    return f'<a class="{_LINK_CLASS}" href="{href}">{label}</a>'


def _render_link_open(
    self: Any, tokens: list[Token], idx: int, options: OptionsDict, env: EnvType
) -> str:
    """Drop any href whose scheme is not allowlisted, then render normally."""
    token = tokens[idx]
    href = token.attrGet("href")
    if href is not None and not _scheme_is_allowed(str(href)):
        token.attrSet("href", "")
        token.attrSet("class", "link--blocked")
    return str(self.renderToken(tokens, idx, options, env))


def build_renderer() -> MarkdownIt:
    """Construct the configured parser.

    Kept as a function (rather than a module-level singleton) because
    ``MarkdownIt`` instances carry mutable rule state; one per call is cheap and
    removes any chance of cross-request contamination.
    """
    md = MarkdownIt("commonmark", {"html": False, "linkify": False})
    md.inline.ruler.before("link", "wikilink", _wikilink_inline_rule)
    md.add_render_rule("wikilink", _render_wikilink)
    md.add_render_rule("link_open", _render_link_open)
    return md


def render_markdown(text: str | None, *, resolver: Resolver | None = None) -> str:
    """Render ``text`` to sanitized HTML.

    ``resolver`` maps a wiki-link target to a document id; when it is ``None``
    every wiki link renders unresolved, which is the correct degradation for a
    caller with no database handy (the pure tests).

    An empty or ``None`` body returns ``""`` rather than raising — a
    freshly-created note legitimately has no content yet.
    """
    if not text:
        return ""
    md = build_renderer()
    env: dict[str, Any] = {"wikilink_resolver": resolver}
    return str(md.render(text, env))
