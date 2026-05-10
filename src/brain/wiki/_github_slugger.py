"""Minimal Python reimplementation of github-slugger heading-anchor semantics.

Used by ``fastpath_manifest.py`` to compute heading anchor strings that match
what Quartz produces via the ``github-slugger`` npm package (v2.x).

Algorithm (mirrors github-slugger source):
  1. NFC-normalise the text.
  2. Lowercase.
  3. Strip: Unicode general punctuation \\u2000-\\u206F, supplemental
     \\u2E00-\\u2E7F, and ASCII punctuation: ``\\'!"#$%&()*+,./:;<=>?@[]^`{|}~``.
  4. Replace whitespace runs with ``-``.
  5. Track duplicate headings within a document; suffix with ``-1``, ``-2``, etc.

Parity with TS is enforced by ``tests/wiki/test_fastpath_fingerprint_parity.py``.
"""
from __future__ import annotations

import re
import unicodedata

# ASCII characters stripped by github-slugger v2.
# Matches the regex in github-slugger/index.js:
#   /[\\u2000-\\u206F\\u2E00-\\u2E7F\\\\'!"#$%&()*+,./:;<=>?@[\\]^`{|}~]/g
_STRIP_ASCII: frozenset[str] = frozenset({
    "\\", "'", '"', "!", "#", "$", "%", "&", "(", ")", "*", "+",
    ",", ".", "/", ":", ";", "<", "=", ">", "?", "@", "[", "]",
    "^", "`", "{", "|", "}", "~",
})


def _normalize(text: str) -> str:
    """Apply NFC normalisation + github-slugger stripping to heading text."""
    text = unicodedata.normalize("NFC", text)
    text = text.lower().strip()
    result: list[str] = []
    for ch in text:
        cp = ord(ch)
        if ch in _STRIP_ASCII:
            continue
        if 0x2000 <= cp <= 0x206F or 0x2E00 <= cp <= 0x2E7F:
            continue
        result.append(ch)
    text = "".join(result)
    # Replace whitespace runs (including Unicode whitespace) with a single hyphen.
    text = re.sub(r"\s+", "-", text)
    return text


class Slugger:
    """Stateful github-slugger: tracks duplicates within one document.

    Matches the ``Slugger`` class from the github-slugger npm package (v2.x).
    Create one instance per document; call :meth:`slug` for each heading in
    document order; call :meth:`reset` before processing a new document.
    """

    def __init__(self) -> None:
        self._seen: dict[str, int] = {}

    def slug(self, text: str) -> str:
        """Return heading anchor for ``text``, with duplicate disambiguation.

        First occurrence of a heading → plain normalised string.
        Second → ``<base>-1``.  Third → ``<base>-2``.  Etc.
        """
        base = _normalize(text)
        if base not in self._seen:
            self._seen[base] = 0
            return base
        count = self._seen[base] + 1
        self._seen[base] = count
        return f"{base}-{count}"

    def reset(self) -> None:
        """Reset per-document state (call between documents)."""
        self._seen.clear()
