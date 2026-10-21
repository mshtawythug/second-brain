"""Recover the complete items of a JSON array from a reply cut off mid-array.

A JSON-mode model reply that runs out of ``num_predict`` ends mid-value, so the
whole text fails ``json.loads`` even though every item before the cut is a
complete, well-formed JSON value. :func:`salvage_array_items` returns those
items and nothing past them. Pure and dependency-free: callers decide whether a
prefix is worth using (GraphRAG concept extraction does; a summary does not).
"""
from __future__ import annotations

import json
import re
from typing import Any

_DECODER = json.JSONDecoder()
_WHITESPACE = " \t\n\r"


def salvage_array_items(text: str, key: str) -> list[Any]:
    """The complete items of the array under ``"key"`` in a (possibly truncated) reply.

    Items are decoded one at a time from the opening ``[``; decoding stops at the
    closing ``]``, at the first item that does not decode (the cut-off one), or
    at a separator that is not a comma — never guessing past malformed input.
    Returns ``[]`` when ``key`` is not followed by an array.
    """
    match = re.search(rf'"{re.escape(key)}"\s*:\s*\[', text)
    if match is None:
        return []
    items: list[Any] = []
    pos = match.end()
    while True:
        try:
            # ``raw_decode`` refuses ``]`` and end-of-text as well as a cut-off
            # value, so an empty array, a closed one and a truncated one all
            # stop here.
            item, pos = _DECODER.raw_decode(text, _skip_whitespace(text, pos))
        except json.JSONDecodeError:
            return items
        items.append(item)
        pos = _skip_whitespace(text, pos)
        if pos >= len(text) or text[pos] != ",":
            return items
        pos += 1


def _skip_whitespace(text: str, pos: int) -> int:
    while pos < len(text) and text[pos] in _WHITESPACE:
        pos += 1
    return pos
