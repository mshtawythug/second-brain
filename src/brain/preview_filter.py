"""Clean the `brain search --pick` preview: nothing that can drive a terminal gets through (RM-52).

``brain show`` prints a document raw, and real ``fzf`` passes some sequences straight to the
terminal for images: iTerm2 ``OSC 1337``, tmux-wrapped sequences, sixel, kitty, and ``OSC 8`` links
(sec-121 F1, measured on 0.72.0). The first row previews on open, so a hostile email would need no
keystroke. ``fzf``'s preview command pipes through this module, ``python -I -m
brain.preview_filter``, and so never sees those bytes.

This is a filter and not ``tr -d '\\033'`` because C1 controls are two bytes in UTF-8, and a
byte-level tool cannot delete them without cutting other characters (U+00DB ends in ``0x9B``).

Kept deliberately light, with stdlib imports only: it starts once per preview.
"""
from __future__ import annotations

import re
import signal
import sys

#: 7-bit sequences with their payloads: CSI; OSC to BEL or ST; DCS, SOS, PM, APC to ST; then any
#: other escape. An unterminated string falls to the last branch, which takes only its introducer,
#: so the text after it survives as inert words. Every string stops at the next ESC, which is what
#: keeps this linear. Allowing tmux's doubled ESC inside a string made it quadratic (sec-121b N1),
#: so that wrapper leaves the inert word ``tmux;`` behind; its inner sequence still goes.
#: C1 forms are not here: :data:`_DROP` removes that one character, and a smart quote mis-decoded
#: as Latin-1 (U+009D) never eats the words after it (sec-121 F4).
_SEQUENCE_RE = re.compile(
    r"\x1b\[[0-?]*[ -/]*[@-~]?"
    r"|\x1b\][^\x07\x1b\x9c]*(?:\x07|\x1b\\|\x9c)"
    r"|\x1b[PX^_][^\x1b\x9c]*(?:\x1b\\|\x9c)"
    r"|\x1b[ -/]*[0-~]?"
)
_BIDI_CONTROLS = "\u202a\u202b\u202c\u202d\u202e\u2066\u2067\u2068\u2069"
#: C0 but for tab and newline, DEL, C1, the bidi overrides and the tag characters (invisible text)
#: go; Unicode line separators become newlines, so a pane line stays a line. ZWJ and ZWNJ stay,
#: because real text needs them. One cost: a subdivision flag such as England's shows as a plain
#: black flag.
_DROP: dict[int, str | None] = {
    **{code: None for code in range(0x20) if code not in (0x09, 0x0a)},
    **{code: None for code in range(0x7f, 0xa0)},
    **{ord(char): None for char in _BIDI_CONTROLS},
    **{code: None for code in range(0xe0000, 0xe0080)},
    0x2028: "\n",
    0x2029: "\n",
}


def strip_sequences(text: str) -> str:
    """``text`` without any 7-bit escape sequence, payload included."""
    return _SEQUENCE_RE.sub("", text)


def clean_line(text: str) -> str:
    """``text`` with no sequence, no control but tab and newline, and no bidi override."""
    return strip_sequences(text).translate(_DROP)


def main() -> None:
    """Copy stdin to stdout, line by line, cleaned. Invalid UTF-8 becomes U+FFFD, not a C1 byte."""
    # fzf closes the pane's pipe when the cursor moves on; go quietly when it does.
    signal.signal(signal.SIGPIPE, signal.SIG_DFL)
    for raw in sys.stdin.buffer:
        cleaned = clean_line(raw.decode("utf-8", "replace"))
        sys.stdout.buffer.write(cleaned.encode("utf-8"))
    sys.stdout.buffer.flush()


if __name__ == "__main__":
    main()
