"""YAML frontmatter writer + reader for vault files."""
from typing import Any

import yaml

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
