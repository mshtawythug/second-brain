"""Wiki-link parser for Markdown vault notes.

Pure parsing: input is a Markdown body string (frontmatter already stripped by
the caller), output is a list of :class:`ParsedLink` in document order. The
parser recognizes Obsidian-style ``[[wiki]]`` and ``![[embed]]`` markers plus
the brain-specific ``[[brain:<id>]]`` and ``[[<source>:<external>]]`` forms.

Skipped contexts (silently — these are not links):

- Fenced code blocks (`` ``` ... ``` `` or ``~~~ ... ~~~``).
- Inline code spans delimited by backticks.
- Indented code blocks (4+ leading spaces, with no preceding non-blank line).
- Wiki-link openings preceded by a backslash escape (``\\[[X]]``).
- Empty ``[[]]`` openers (no error, just ignored).

The parser is intentionally conservative: when in doubt about whether a
``[[...]]`` is "really" a link, it skips. Callers can rely on the result list
being safe to materialize into the ``links``/``unresolved_links`` tables
without producing false positives.
"""
import re
from dataclasses import dataclass
from typing import Literal

# Source kinds the parser recognizes in ``[[<source>:<external_id>]]`` form.
# ``brain`` is reserved for direct document-id lookup; other identifiers are
# routed to the ``sources`` table.
_SOURCE_KINDS: frozenset[str] = frozenset({"krisp", "slack", "gmail", "manual"})

# Bracket inside a wiki-link must not contain another ``]]``. Allowing nested
# brackets would conflict with Markdown reference-style links and is not part
# of the Obsidian format we conform to.
_WIKI_LINK_RE = re.compile(r"\[\[(?P<inner>[^\[\]]*?)\]\]")

_FENCE_RE = re.compile(r"^(?P<fence>`{3,}|~{3,})")


@dataclass(frozen=True)
class ParsedLink:
    """One parsed ``[[...]]`` occurrence from a Markdown body.

    Field semantics map 1:1 onto the spec's resolution table — see the table
    in ``docs/specs/2026-04-28-vault-model-design.md``. The parser does not
    resolve the link; it only classifies the surface syntax. Resolution
    against the DB is :func:`brain.vault.resolver.resolve_link`'s job.

    ``raw`` is the literal substring as it appeared in the document,
    including the surrounding ``[[ ]]`` (or ``![[ ]]`` for embeds). Useful
    for round-tripping into ``links.link_text`` so renderers can match the
    original wording exactly.
    """

    raw: str
    kind: Literal["wiki", "embed"]
    target_type: Literal["title", "doc-id", "source-external"]
    target_value: str
    target_source: str | None
    display_text: str | None
    heading: str | None


def parse_wiki_links(text: str) -> list[ParsedLink]:
    """Return every wiki-link found in ``text`` in document order.

    The returned list is positional — callers can rely on the order matching
    the order links appear in the body, which makes diagnostics ("link #3
    on line 47 is dangling") straightforward.

    Returns an empty list for any input that contains no parseable links
    (including the empty string and bodies consisting entirely of code).
    """
    return [link for link, _start, _end in iter_wiki_links_with_spans(text)]


def iter_wiki_links_with_spans(
    text: str,
) -> list[tuple[ParsedLink, int, int]]:
    """Same as :func:`parse_wiki_links`, but each entry carries source spans.

    Each tuple is ``(parsed, start, end)`` where ``text[start:end]`` is the
    exact byte range the link occupies in the original ``text``. Embeds
    (``![[...]]``) include the leading ``!`` in the span so callers that
    rewrite in place don't have to detect it again.

    Spans are non-overlapping and yielded in document order. The
    :func:`_strip_uncodelike_regions` pass that powers code-fence skipping
    is length-preserving, so offsets from the regex over the masked text map
    1:1 onto the original — that's what makes rewriting safe even when a
    later region of the document contains the same ``[[X]]`` inside a code
    fence (it's silently skipped, not rewritten).
    """
    parseable = _strip_uncodelike_regions(text)
    out: list[tuple[ParsedLink, int, int]] = []
    for match in _WIKI_LINK_RE.finditer(parseable):
        inner = match.group("inner")
        if not inner.strip():
            # ``[[]]`` and ``[[   ]]`` are silently ignored — Obsidian treats
            # them as user typos rather than markup.
            continue
        # Detect ``![[...]]`` embeds: look at the character immediately
        # before the opener.
        start = match.start()
        end = match.end()
        is_embed = start > 0 and parseable[start - 1] == "!"
        # Reject ``\[[X]]`` (backslash-escaped) — the ``\`` makes it literal text.
        # An odd number of backslashes immediately preceding the opener escapes
        # it; an even number means the user wrote ``\\`` (literal backslash)
        # before a real link.
        escape_index = start - 1
        if is_embed:
            escape_index -= 1
        backslash_run = 0
        while escape_index >= 0 and parseable[escape_index] == "\\":
            backslash_run += 1
            escape_index -= 1
        if backslash_run % 2 == 1:
            continue

        raw = ("![[" if is_embed else "[[") + inner + "]]"
        link = _classify(raw, inner, embed=is_embed)
        if link is not None:
            span_start = start - 1 if is_embed else start
            out.append((link, span_start, end))
    return out


def _classify(raw: str, inner: str, *, embed: bool) -> ParsedLink | None:
    """Turn ``inner`` (the text between ``[[`` and ``]]``) into a ParsedLink.

    Splits on the first ``|`` for the optional display alias, then on ``#``
    for an optional heading, then on ``:`` to detect explicit prefixes
    (``brain:`` / ``<source>:``). Returns ``None`` only on degenerate inputs
    that survived the empty-check above (e.g. a lone ``|`` with no target);
    the caller drops them silently.
    """
    target_part, _, display = inner.partition("|")
    target_part = target_part.strip()
    display_text = display.strip() if display else None
    if not target_part:
        return None

    target_value, _, heading_part = target_part.partition("#")
    target_value = target_value.strip()
    heading = heading_part.strip() if heading_part else None
    if not target_value:
        return None

    # Explicit ``<prefix>:<rest>`` form. Only a small set of known prefixes
    # win; arbitrary ``foo:bar`` is treated as a title (Obsidian allows
    # colons in note titles, so we don't claim them by default).
    prefix, sep, rest = target_value.partition(":")
    prefix = prefix.strip()
    rest = rest.strip()
    kind: Literal["wiki", "embed"] = "embed" if embed else "wiki"
    if sep and rest:
        if prefix == "brain":
            return ParsedLink(
                raw=raw,
                kind=kind,
                target_type="doc-id",
                target_value=rest,
                target_source=None,
                display_text=display_text,
                heading=heading,
            )
        if prefix in _SOURCE_KINDS:
            return ParsedLink(
                raw=raw,
                kind=kind,
                target_type="source-external",
                target_value=rest,
                target_source=prefix,
                display_text=display_text,
                heading=heading,
            )

    return ParsedLink(
        raw=raw,
        kind=kind,
        target_type="title",
        target_value=target_value,
        target_source=None,
        display_text=display_text,
        heading=heading,
    )


def _strip_uncodelike_regions(text: str) -> str:
    """Replace fenced/inline/indented code regions with spaces, length-preserving.

    Length-preserving so positional information (``match.start()``) over the
    returned string still maps onto the original — useful for future
    line/column diagnostics, even though Phase 2 doesn't surface them yet.
    All other characters are left intact, so the regex scan only sees text
    that's actually eligible to contain a link.
    """
    out_chars: list[str] = []
    in_fence = False
    fence_marker = ""
    lines = text.splitlines(keepends=True)
    for line in lines:
        stripped = line.lstrip()
        if in_fence:
            if stripped.startswith(fence_marker):
                in_fence = False
                fence_marker = ""
            out_chars.append(_blank_keep_newline(line))
            continue
        match = _FENCE_RE.match(stripped)
        if match:
            in_fence = True
            fence_marker = match.group("fence")[0] * 3
            out_chars.append(_blank_keep_newline(line))
            continue
        # Indented code block (4+ leading spaces or a tab) — only when the
        # block is structurally a code block; for our purposes, treating any
        # 4+-space indent as code is conservative-but-safe (a real list item
        # at 4 spaces is rare in this corpus and a false negative on a link
        # there is acceptable).
        if line.startswith("    ") or line.startswith("\t"):
            out_chars.append(_blank_keep_newline(line))
            continue
        out_chars.append(_strip_inline_code(line))
    return "".join(out_chars)


def _blank_keep_newline(line: str) -> str:
    """Replace every non-newline character in ``line`` with a space.

    Keeps offsets stable for downstream regex matching while ensuring no
    bracket-like characters survive to be mistaken for a link.
    """
    return "".join(" " if ch != "\n" else "\n" for ch in line)


def _strip_inline_code(line: str) -> str:
    """Replace inline code spans (``` `…` ```) in ``line`` with spaces.

    Only single-backtick spans are handled — Markdown allows multi-backtick
    spans to escape inner backticks, but in practice vault notes don't mix
    those with wiki-links, and the cost of a false negative (a missed link)
    is much lower than a false positive (materializing a code-fragment as a
    link).
    """
    out: list[str] = []
    in_code = False
    for ch in line:
        if ch == "`":
            out.append(" ")
            in_code = not in_code
            continue
        if in_code and ch != "\n":
            out.append(" ")
        else:
            out.append(ch)
    # If the line ends with an unmatched opening backtick, treat the rest as
    # text — this branch is naturally handled by the loop above (``in_code``
    # leaks to the next line, but ``_strip_uncodelike_regions`` resets state
    # per-line, so an unbalanced inline opener is forgiving).
    return "".join(out)
