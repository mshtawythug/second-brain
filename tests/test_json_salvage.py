"""Tests for :mod:`brain.json_salvage` — recovering complete array items from a
JSON reply that was cut off mid-array. All values are synthetic (no PII)."""
from __future__ import annotations

from brain.json_salvage import salvage_array_items


def test_salvage_returns_complete_items_before_a_mid_string_cut() -> None:
    text = (
        '{"entities": [{"name": "Zorvex", "type": "tool"}, '
        '{"name": "Lumkai Corp", "type": "org"}, {"name": "Fen'
    )
    assert salvage_array_items(text, "entities") == [
        {"name": "Zorvex", "type": "tool"},
        {"name": "Lumkai Corp", "type": "org"},
    ]


def test_salvage_stops_at_a_cut_between_items() -> None:
    text = '{"entities": [{"name": "Zorvex", "type": "tool"}, '
    assert salvage_array_items(text, "entities") == [{"name": "Zorvex", "type": "tool"}]


def test_salvage_tolerates_pretty_printed_whitespace() -> None:
    text = '{\n  "entities" :\n  [\n    {"name": "Zorvex",\n     "type": "tool"}\n    ,\n    {"na'
    assert salvage_array_items(text, "entities") == [{"name": "Zorvex", "type": "tool"}]


def test_salvage_of_a_closed_array_returns_every_item() -> None:
    text = '{"entities": ["a", 2, {"b": [3]}]}'
    assert salvage_array_items(text, "entities") == ["a", 2, {"b": [3]}]


def test_salvage_of_an_empty_array_is_empty() -> None:
    assert salvage_array_items('{"entities": []}', "entities") == []


def test_salvage_cut_inside_the_first_item_is_empty() -> None:
    assert salvage_array_items('{"entities": [{"name": "Zor', "entities") == []


def test_salvage_missing_key_is_empty() -> None:
    assert salvage_array_items('{"other": [{"name": "Zorvex"}]}', "entities") == []


def test_salvage_key_not_followed_by_an_array_is_empty() -> None:
    assert salvage_array_items('{"entities": "oops"}', "entities") == []


def test_salvage_skips_the_key_appearing_earlier_as_a_value() -> None:
    # "entities" as a string VALUE before the real key is not the array's key.
    text = '{"note": "entities", "entities": [{"name": "Zorvex"}, {"na'
    assert salvage_array_items(text, "entities") == [{"name": "Zorvex"}]


def test_salvage_stops_at_a_malformed_separator() -> None:
    # Two items with no comma between them: stop rather than guess.
    text = '{"entities": [{"name": "A"} {"name": "B"}]}'
    assert salvage_array_items(text, "entities") == [{"name": "A"}]
