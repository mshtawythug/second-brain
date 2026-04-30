"""YAML frontmatter writer + reader for vault files."""
import hashlib
from typing import Any

import yaml

from .derived_links.fence import strip_fence

_FENCE = "---"


def dump_frontmatter(fields: dict[str, Any], body: str) -> str:
    """Serialize a vault file: YAML frontmatter + a single blank line + body.

    Field ordering is preserved verbatim from ``fields`` (callers control the
    canonical order; this is what makes export output stable across runs).
    Output uses block style with Unicode preserved so titles like
    ``"person-x Q1 réview"`` round-trip cleanly.

    The body is written verbatim with a single newline separator after the
    closing ``---`` fence; callers are responsible for any trailing newline
    on the body itself.
    """
    yaml_body = yaml.safe_dump(
        fields,
        default_flow_style=False,
        sort_keys=False,
        allow_unicode=True,
    )
    return f"{_FENCE}\n{yaml_body}{_FENCE}\n\n{body}"


def parse_frontmatter(text: str) -> tuple[dict[str, Any], str]:
    """Split a vault file's text into ``(frontmatter_dict, body)``.

    A vault file *must* begin with ``---``; if not, returns ``({}, text)`` and
    treats the whole input as body. Inside the fences the YAML is parsed in
    safe mode (no arbitrary Python object construction). Returns the parsed
    YAML mapping (empty dict if YAML loads to ``None``) and everything after
    the closing fence, with the leading blank line stripped if present.

    Raises :class:`yaml.YAMLError` for malformed YAML so callers can decide
    whether to skip + warn or abort the run.
    """
    lines = text.splitlines(keepends=True)
    if not lines or lines[0].rstrip("\r\n") != _FENCE:
        return {}, text
    # Find the closing ``---`` — second fence on its own line.
    for idx in range(1, len(lines)):
        if lines[idx].rstrip("\r\n") == _FENCE:
            yaml_text = "".join(lines[1:idx])
            body_text = "".join(lines[idx + 1 :])
            # The writer adds an empty separator line after the closing fence;
            # peel exactly one of those off so re-parses see the same body
            # the caller wrote.
            if body_text.startswith("\n"):
                body_text = body_text[1:]
            elif body_text.startswith("\r\n"):
                body_text = body_text[2:]
            parsed = yaml.safe_load(yaml_text) if yaml_text.strip() else {}
            if parsed is None:
                parsed = {}
            if not isinstance(parsed, dict):
                # Frontmatter is contractually a mapping; anything else (a
                # bare list, scalar, etc.) is corrupt.
                raise ValueError(
                    "frontmatter must be a YAML mapping; "
                    f"got {type(parsed).__name__}"
                )
            return parsed, body_text
    # No closing fence — treat the whole file as body.
    return {}, text


def body_hash(text: str) -> str:
    """Return SHA-256 of the body of a vault file (frontmatter + fence stripped).

    The hash is what populates ``documents.content_hash`` for vault-tier
    rows. ``brain vault sync`` uses it to detect "real" content changes and
    skip a re-embed when only the frontmatter shifted (e.g. tag tweaks,
    ``updated:`` timestamp bumps).

    Normalizations before hashing — all chosen to preserve hash stability
    across editors and platforms:

    - The leading ``---\\n…\\n---\\n`` block (if present) is stripped via
      :func:`parse_frontmatter`. Inputs with no frontmatter hash the entire
      text.
    - The auto-generated derived-edges fence (everything between
      ``<!-- BRAIN_DERIVED_START -->`` and ``<!-- BRAIN_DERIVED_END -->``)
      is stripped via :func:`brain.vault.derived_links.fence.strip_fence`.
      The fence is recomputed every relink from ``derived_links`` rows; its
      content is therefore not authored body and must not influence the
      hash, otherwise every relink would trigger a re-embed cascade.
    - CRLF line endings collapse to LF so a Windows-saved copy and the same
      content on macOS produce the same hash.
    - Leading/trailing whitespace is stripped — the writer's trailing
      newline is not part of the canonical content. (Editors that append a
      trailing newline don't trigger spurious updates.)

    The hash matches the value the export module would write into the same
    document's ``content_hash`` column, which is what makes the
    export → sync round-trip a no-op.
    """
    # ``parse_frontmatter`` raises on malformed YAML — callers (the sync
    # engine) catch that separately and record an error. For the hash itself
    # we fall back to hashing the raw text when the file has no frontmatter
    # at all, but propagate yaml errors upward.
    _, body = parse_frontmatter(text)
    fence_stripped = strip_fence(body)
    normalized = fence_stripped.replace("\r\n", "\n").replace("\r", "\n").strip()
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()
