"""The pure helpers inside ``brain.ui.notes_service``.

No database, no HTTP, no filesystem beyond a ``tmp_path`` — every function here
takes values and returns values, so these are millisecond unit tests and the
module carries ``nodb``.

Scope is deliberately narrow. ``strip_redundant_title_heading`` is NOT tested
here: it already has a dedicated file in ``tests/test_ui_heading_strip.py``,
and duplicating it would create two places to update and a false impression of
where that coverage lives. What is here is the set of helper branches nothing
else reaches — the degenerate inputs each function promises to survive.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from brain.ui.notes_service import _merge_frontmatter, _split_file, _wikilink_targets
from brain.ui.schemas import NotePatch

pytestmark = pytest.mark.nodb


def _patch(**overrides: object) -> NotePatch:
    """A ``NotePatch`` with everything unset except what a test names."""
    fields: dict[str, object] = {
        "body_hash": "sha256:whatever",
        "body": None,
        "title": None,
        "tags": None,
        "content_type": None,
    }
    fields.update(overrides)
    return NotePatch(**fields)  # type: ignore[arg-type]


# ------------------------------------------------------------- _split_file --


def test_a_missing_file_reads_as_empty_rather_than_raising(tmp_path: Path) -> None:
    """The vault is edited by a watcher, ``brain-mcp`` and the CLI concurrently.

    A row can name a ``vault_path`` whose file was deleted or moved between the
    row being read and the file being opened. Raising here would turn that race
    into a 500 on a note the user can still see listed; returning the empty pair
    lets the caller fall back to the row's own content.
    """
    assert _split_file(tmp_path / "does-not-exist.md") == ({}, "")


def test_a_real_file_is_split_into_frontmatter_and_body(tmp_path: Path) -> None:
    """The success path, so the empty-pair case above cannot be vacuous.

    A ``_split_file`` that returned ``({}, "")`` unconditionally would satisfy
    the test above perfectly.
    """
    note = tmp_path / "note.md"
    note.write_text("---\ntitle: A Note\n---\n\nBody text.\n", encoding="utf-8")

    frontmatter, body = _split_file(note)

    assert frontmatter["title"] == "A Note"
    assert "Body text." in body


# -------------------------------------------------------- _wikilink_targets --


def test_an_unterminated_wikilink_stops_the_scan_instead_of_looping(
) -> None:
    """``[[`` with no ``]]`` must terminate.

    The scanner advances its cursor past each closing ``]]``; with no closing
    delimiter there is nothing to advance past, so the ``break`` is what stops
    it. Without that branch the loop would find the same unterminated ``[[``
    forever — a hung request rather than a wrong answer, from a note a user can
    create by typing two brackets and saving.
    """
    assert _wikilink_targets("See [[Unclosed and then nothing") == []


def test_a_valid_link_before_an_unterminated_one_is_still_collected() -> None:
    """The partial case: everything up to the broken link must survive.

    Pins that the ``break`` ends the SCAN rather than discarding the results —
    a `return []` in its place would pass the test above and lose real links.
    """
    assert _wikilink_targets("[[First]] then [[broken") == ["First"]


@pytest.mark.parametrize(
    ("body", "expected"),
    [
        ("[[Target]]", ["Target"]),
        ("[[folder/slug|Display Text]]", ["folder/slug"]),  # alias form
        ("[[  Padded  ]]", ["Padded"]),
        ("[[]]", []),                                        # empty target
        ("[[line\nbreak]]", []),                             # never spans lines
        ("no links here", []),
    ],
)
def test_wikilink_scanning_shapes(body: str, expected: list[str]) -> None:
    assert _wikilink_targets(body) == expected


# -------------------------------------------------------- _merge_frontmatter --


def test_each_patched_field_lands_under_its_frontmatter_key() -> None:
    """``content_type`` is the trap: it is stored under ``type``, not its own name.

    Parametrising over the three fields would hide exactly that, since the test
    would have to name the mapping it is checking. Asserting the three together
    against one merge keeps the key names literal.
    """
    merged = _merge_frontmatter(
        {"id": "kept", "title": "Old", "tags": ["old"], "type": "note"},
        _patch(title="New", tags=["new"], content_type="transcript"),
    )

    assert merged["title"] == "New"
    assert merged["tags"] == ["new"]
    assert merged["type"] == "transcript"


def test_unpatched_fields_and_unknown_keys_are_left_alone() -> None:
    """A save must not strip frontmatter the UI does not model.

    ``id`` / ``kind`` / ``source`` / ``vault_path`` are owned by the export
    layer, and a user's own keys are theirs. An empty patch must be a no-op on
    every one of them.
    """
    existing = {
        "id": "abc",
        "kind": "vault",
        "title": "Untouched",
        "tags": ["keep"],
        "type": "note",
        "user_key": "user value",
    }

    assert _merge_frontmatter(existing, _patch()) == existing


def test_merging_does_not_mutate_the_caller_s_dict() -> None:
    """The existing dict is read from the file the caller may still write back.

    Mutating it in place would make the merge invisible to a caller that kept
    its own reference — and this returns a new dict precisely so the on-disk
    key ORDER can be preserved without the two copies drifting.
    """
    existing = {"title": "Original", "tags": ["first"]}

    merged = _merge_frontmatter(existing, _patch(title="Changed"))

    assert existing == {"title": "Original", "tags": ["first"]}
    assert merged["title"] == "Changed"
    assert merged is not existing
