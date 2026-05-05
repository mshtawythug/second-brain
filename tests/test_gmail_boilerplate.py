"""Tests for ``brain.ingest.gmail.strip_boilerplate`` and ``BOILERPLATE_PATTERNS``.

Pure-function tests — no DB, no fixtures beyond the standard library. The
strip_boilerplate function:

  1. Collapses runs of ≥2 consecutive identical lines longer than 40 chars to
     one occurrence.
  2. Strips each compiled regex in ``brain.config.BOILERPLATE_PATTERNS``
     (applied with re.MULTILINE | re.IGNORECASE; individual patterns may opt
     into DOTALL via ``(?s)``).
  3. Leaves quoted-reply markers untouched — threading is Phase 2's problem.
"""
import re

from brain.config import BOILERPLATE_PATTERNS
from brain.ingest.gmail import strip_boilerplate

# --- Run collapse ---------------------------------------------------------


def test_collapse_collapses_repeated_long_lines() -> None:
    """Seven copies of a 60-char line collapse down to a single occurrence."""
    long_line = "This is a confidentiality footer that is sixty chars long ok"
    assert len(long_line) > 40
    body = "Hello there\n" + ("\n".join([long_line] * 7)) + "\n"
    out = strip_boilerplate(body)
    assert out.count(long_line) == 1
    assert "Hello there" in out


def test_collapse_leaves_short_repeated_lines_alone() -> None:
    """Short repeated lines (≤40 chars) remain repeated — they're often legit."""
    short_line = "Thanks!"
    body = "\n".join([short_line] * 5)
    out = strip_boilerplate(body)
    assert out.count(short_line) == 5


def test_collapse_handles_runs_at_eof() -> None:
    """A run of repeated long lines at the end of the body still collapses."""
    long_line = "A" * 60
    body = "Header line\n" + ("\n".join([long_line] * 4))
    out = strip_boilerplate(body)
    assert out.count(long_line) == 1
    assert "Header line" in out


def test_collapse_two_distinct_runs_collapse_independently() -> None:
    """Two separate runs of different long lines each collapse to one copy."""
    a = "A" * 60
    b = "B" * 60
    body = "\n".join([a, a, a, "intermission", b, b, b])
    out = strip_boilerplate(body)
    assert out.count(a) == 1
    assert out.count(b) == 1
    assert "intermission" in out


# --- Pattern stripping ----------------------------------------------------


def test_strip_sent_from_iphone() -> None:
    """``Sent from my iPhone`` footer is removed; preceding body is preserved."""
    body = "Hey, see you Monday.\n\nSent from my iPhone"
    out = strip_boilerplate(body)
    assert "Sent from my iPhone" not in out
    assert "Hey, see you Monday." in out


def test_strip_sent_from_iphone_case_insensitive() -> None:
    """Case-insensitive match — ``SENT FROM MY IPHONE`` is also stripped."""
    body = "Body content\n\nSENT FROM MY IPHONE"
    out = strip_boilerplate(body)
    assert "SENT FROM MY IPHONE" not in out


def test_strip_sent_from_my_ipad_with_period() -> None:
    """The trailing period is optional in the pattern — variant should match too."""
    body = "Cheers,\nAli\n\nSent from my iPad."
    out = strip_boilerplate(body)
    assert "Sent from my iPad" not in out
    assert "Cheers," in out


def test_strip_get_outlook_footer() -> None:
    """``Get Outlook for Android <https://...>`` footer is removed."""
    body = "Body content\n\nGet Outlook for Android <https://aka.ms/foo>"
    out = strip_boilerplate(body)
    assert "Get Outlook" not in out
    assert "Body content" in out


def test_strip_confidentiality_notice() -> None:
    """A ``CONFIDENTIALITY NOTICE:`` block is stripped up to the next blank line."""
    body = (
        "Real message body here.\n"
        "\n"
        "CONFIDENTIALITY NOTICE: This message is confidential.\n"
        "If you received it in error, delete it.\n"
        "\n"
        "Trailing legitimate paragraph."
    )
    out = strip_boilerplate(body)
    assert "CONFIDENTIALITY NOTICE" not in out
    assert "delete it" not in out
    # Body before AND after the notice is preserved.
    assert "Real message body here." in out
    assert "Trailing legitimate paragraph." in out


def test_strip_company_ko_footer() -> None:
    """The recurring COMPANY_REDACTED/COMPANY_REDACTED-style confidentiality footer is removed.

    Real-world sample (paraphrased from the live COMPANY_REDACTED thread): a multi-line
    paragraph starting with ``This email and any attachments are confidential``
    which terminates at a blank line.
    """
    body = (
        "Thanks for the introduction. Looking forward to chatting.\n"
        "\n"
        "This email and any attachments are confidential and intended solely "
        "for the addressee. If you are not the intended recipient, please "
        "notify the sender immediately and delete this message from your "
        "system.\n"
        "\n"
        "Best,\nAli"
    )
    out = strip_boilerplate(body)
    assert "This email and any attachments are confidential" not in out
    assert "Looking forward to chatting." in out
    assert "Best," in out


def test_strip_preserves_quoted_replies() -> None:
    """Quoted reply markers below the most recent message are left intact."""
    body = (
        "Thanks for the update.\n"
        "\n"
        "On Mon, 1 May 2026, Foo wrote:\n"
        "> Bar\n"
        "> Baz"
    )
    out = strip_boilerplate(body)
    assert "On Mon, 1 May 2026, Foo wrote:" in out
    assert "> Bar" in out
    assert "> Baz" in out


def test_strip_idempotent() -> None:
    """``strip_boilerplate`` is idempotent — running it twice == once."""
    repeated_long = "This is a long boilerplate line that repeats and is over forty chars\n"
    body = (
        "Hello there\n\n"
        + (repeated_long * 5)
        + "\n"
        + "CONFIDENTIALITY NOTICE: foo\nbar baz\n"
        + "\n"
        + "Sent from my iPhone"
    )
    once = strip_boilerplate(body)
    twice = strip_boilerplate(once)
    assert once == twice


def test_strip_handles_empty_body() -> None:
    """Empty input round-trips to empty string without raising."""
    assert strip_boilerplate("") == ""


def test_strip_preserves_legitimate_body_when_no_boilerplate() -> None:
    """Bodies without any boilerplate or repeated long lines are untouched
    (modulo trailing-whitespace normalization from the final ``.strip()``)."""
    body = "Hello.\n\nThis is a regular email body.\nIt has multiple lines."
    out = strip_boilerplate(body)
    assert out == body


def test_pattern_compilation() -> None:
    """Every entry in ``BOILERPLATE_PATTERNS`` must compile without error."""
    for pattern in BOILERPLATE_PATTERNS:
        re.compile(pattern, re.MULTILINE | re.IGNORECASE)
