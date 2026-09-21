"""D2: the render-time strip of a leading heading that only repeats the title.

Every note ``brain note new`` and ``brain daily`` write opens with its own
``# Title`` — ``vault.templates.NOTE_TEMPLATE`` and ``DAILY_TEMPLATE`` both do —
and the inspector renders the title as a heading of its own on top of that. So
this was not an edge case: essentially the whole authored vault showed its title
twice.

The fix lives in ``brain.ui.notes_service`` rather than in the front end, so
``brain-mcp`` and any later client inherit it, and so the ``<h1>`` js/inspector.js emits
can stay — a note whose body has no heading still needs one.

Two properties matter. **Only the first is tested here**, and the correction is
worth making explicitly: this module previously claimed "both are tested here",
which was false, in a file whose neighbouring discipline is that a comment
asserting a property is not the property. That is the same error one level up —
a docstring asserting coverage instead of coverage.

1. **It strips exactly the redundant case and nothing else.** That is this file,
   below: pure, no DB, no HTTP.
2. **It never touches the STORED body** — a round trip through the editor would
   otherwise delete the user's own heading for real. That property needs a real
   file on disk and a real route, so it lives in
   ``tests/test_ui_routes.py::test_rendering_never_rewrites_the_file_on_disk``,
   with ``::test_body_hash_covers_the_unstripped_body`` beside it pinning the
   related invariant that ``body_hash`` hashes the UNSTRIPPED body.

The tests were not moved here to make the claim true, deliberately: dragging a
Postgres fixture and a ``TestClient`` into this module to satisfy a sentence
would trade a pure, millisecond-fast unit file for a slower integration one and
make the DB opt-out in ``conftest`` harder to reason about. The claim was
corrected to match where the coverage actually is.
"""
from __future__ import annotations

import pytest

from brain.ui.notes_service import strip_redundant_title_heading
from brain.vault.templates import DAILY_TEMPLATE, NOTE_TEMPLATE

#: Opens NO database connection — this module reads files off disk and
#: parses them. The marker lets the session skip the schema reset and, more
#: importantly, the MACHINE-WIDE advisory lock; see
#: ``conftest._session_touches_the_database``.
pytestmark = pytest.mark.nodb

TITLE = "Quarterly Vendor Review"


def test_a_leading_heading_matching_the_title_is_dropped() -> None:
    body = f"# {TITLE}\n\nThree unresolved threads.\n"
    assert strip_redundant_title_heading(body, TITLE) == "\nThree unresolved threads.\n"


def test_blank_lines_before_the_heading_do_not_hide_it() -> None:
    body = f"\n\n# {TITLE}\n\nBody.\n"
    assert "# " not in strip_redundant_title_heading(body, TITLE)


@pytest.mark.parametrize(
    "heading",
    [
        f"#  {TITLE}",            # extra spaces after the marker
        f"# {TITLE}   ",          # trailing spaces
        f"# {TITLE} ###",         # CommonMark closing sequence
        f"## {TITLE}",            # a lower level is still a duplicate
        f"   # {TITLE}",          # up to three leading spaces is still ATX
        f"# {TITLE.upper()}",     # case-insensitive
        f"# {TITLE.replace(' ', '   ')}",   # collapsed internal whitespace
    ],
)
def test_equivalent_spellings_of_the_same_heading_are_dropped(heading: str) -> None:
    assert strip_redundant_title_heading(f"{heading}\n\nBody.\n", TITLE) == "\nBody.\n"


@pytest.mark.parametrize(
    "body",
    [
        "Three unresolved threads.\n",                     # no heading at all
        f"Intro paragraph.\n\n# {TITLE}\n\nBody.\n",       # heading is not first
        "# Vendor scoring\n\nBody.\n",                     # different text
        f"#{TITLE}\n\nBody.\n",                            # no space: not a heading
        f"{TITLE}\n=====\n\nBody.\n",                      # setext: out of scope
        f"```\n# {TITLE}\n```\n",                          # inside a fence
        f"####### {TITLE}\n\nBody.\n",                     # seven #: not a heading
        f"# {TITLE} extra\n\nBody.\n",                     # superset, not a match
    ],
)
def test_everything_else_is_returned_verbatim(body: str) -> None:
    assert strip_redundant_title_heading(body, TITLE) == body


def test_a_hash_that_is_part_of_the_title_is_not_read_as_a_closing_run() -> None:
    """``# C#`` closes nothing — the run must be preceded by whitespace."""
    assert strip_redundant_title_heading("# C#\n\nBody.\n", "C#") == "\nBody.\n"
    assert strip_redundant_title_heading("# C#\n\nBody.\n", "C") == "# C#\n\nBody.\n"


@pytest.mark.parametrize("body", ["", "   \n\n  \n"])
def test_empty_and_whitespace_only_bodies_are_safe(body: str) -> None:
    assert strip_redundant_title_heading(body, TITLE) == body


@pytest.mark.parametrize("body", ["#\n\nBody.\n", "###\n\nBody.\n", "# ###\n\nBody.\n"])
def test_a_heading_with_no_text_is_never_stripped(body: str) -> None:
    """The empty-heading branch: a bare ``#`` matches the ATX pattern but has no
    text, so it can never equal a title and must be left alone.

    ``# ###`` is the interesting one — the closing-sequence strip reduces it to
    the empty string, which must NOT then be treated as matching an empty title.
    """
    assert strip_redundant_title_heading(body, TITLE) == body


def test_a_missing_title_strips_nothing() -> None:
    body = f"# {TITLE}\n"
    assert strip_redundant_title_heading(body, None) == body
    assert strip_redundant_title_heading(body, "") == body


def test_the_shipped_note_template_is_the_case_this_exists_for() -> None:
    """Guard the premise, not just the code.

    If ``NOTE_TEMPLATE`` ever stops opening with the title, this fix is solving
    a problem that no longer exists and someone should find out here.
    """
    body = NOTE_TEMPLATE.split("---\n")[-1]
    rendered = body.replace("{{title}}", TITLE)
    assert rendered.lstrip().startswith(f"# {TITLE}")
    assert f"# {TITLE}" not in strip_redundant_title_heading(rendered, TITLE)


def test_the_shipped_daily_template_is_covered_too() -> None:
    date = "2026-03-09"
    body = DAILY_TEMPLATE.split("---\n")[-1].replace("{{date}}", date)
    stripped = strip_redundant_title_heading(body, date)
    assert f"# {date}" not in stripped
    # The section headings below it are NOT the title and must survive.
    assert "## Notes" in stripped
    assert "## Tasks" in stripped
    assert "## Reflection" in stripped
