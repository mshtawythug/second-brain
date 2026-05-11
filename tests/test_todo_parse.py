"""Unit tests for :func:`brain.todo.parse_action_items` (Wave Q1-D 2.7)."""
from __future__ import annotations

from brain.todo import ActionItem, parse_action_items


def test_parse_action_items_open_and_done() -> None:
    body = "- [ ] write the report\n- [x] file the expense\n"
    items = parse_action_items(body)
    assert items == [
        ActionItem(state="open", text="write the report"),
        ActionItem(state="done", text="file the expense"),
    ]


def test_parse_action_items_uppercase_x_is_done() -> None:
    items = parse_action_items("- [X] DONE\n")
    assert items == [ActionItem(state="done", text="DONE")]


def test_parse_action_items_with_leading_whitespace() -> None:
    body = "  - [ ] indented item\n    * [x] also indented and star bullet\n"
    items = parse_action_items(body)
    assert len(items) == 2
    assert items[0] == ActionItem(state="open", text="indented item")
    assert items[1] == ActionItem(state="done", text="also indented and star bullet")


def test_parse_action_items_ignores_non_task_lines() -> None:
    body = (
        "## Action items\n"
        "\n"
        "- [ ] real task\n"
        "Some prose paragraph that should be ignored.\n"
        "- not-a-checkbox\n"
        "[just brackets in text]\n"
    )
    items = parse_action_items(body)
    assert items == [ActionItem(state="open", text="real task")]


def test_parse_action_items_empty_body_returns_empty() -> None:
    assert parse_action_items("") == []


def test_parse_action_items_mixed_open_and_done_preserves_order() -> None:
    body = "- [ ] a\n- [x] b\n- [ ] c\n"
    items = parse_action_items(body)
    assert [i.state for i in items] == ["open", "done", "open"]
    assert [i.text for i in items] == ["a", "b", "c"]


def test_parse_action_items_strips_trailing_whitespace() -> None:
    items = parse_action_items("- [ ] item with trailing   \n")
    assert items == [ActionItem(state="open", text="item with trailing")]
