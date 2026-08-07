"""Non-destructive merge/removal of the brain Stop hook in Claude Code settings.

``~/.claude/settings.json`` is the user's own config, so the contract under test
is conservative: merge builds new objects at every level and never mutates the
caller's document; an unexpected shape is a refusal, never a guess.

Pure logic — no database, no CLI, and every path is rooted at ``tmp_path`` so
the developer's real ``~/.claude/settings.json`` is never touched.
"""
from __future__ import annotations

import copy
import json
from pathlib import Path
from typing import Any

import pytest

from brain.claude_settings import (
    merge_stop_hook,
    read_settings,
    remove_stop_hook,
    serialize,
)
from brain.errors import SettingsFormatError

COMMAND = "/tmp/synthetic-home/.claude/hooks/brain-capture-hook.sh"
FOREIGN_COMMAND = "/tmp/synthetic-home/.claude/hooks/other-tool.sh"


def _write(path: Path, text: str) -> Path:
    path.write_text(text, encoding="utf-8")
    return path


def _brain_group(command: str = COMMAND, timeout: int = 10) -> dict[str, Any]:
    return {"hooks": [{"type": "command", "command": command, "timeout": timeout}]}


def _foreign_group() -> dict[str, Any]:
    return {"hooks": [{"type": "command", "command": FOREIGN_COMMAND, "timeout": 5}]}


def _stop_groups(document: dict[str, Any]) -> list[Any]:
    return list(document["hooks"]["Stop"])


# ---------------------------------------------------------------------------
# read_settings
# ---------------------------------------------------------------------------


def test_missing_file_yields_empty_document(tmp_path: Path) -> None:
    assert read_settings(tmp_path / "settings.json") == {}


@pytest.mark.parametrize("body", ["", "   ", "\n\n", "\t\n  "])
def test_whitespace_only_file_yields_empty_document(tmp_path: Path, body: str) -> None:
    path = _write(tmp_path / "settings.json", body)

    assert read_settings(path) == {}


def test_malformed_json_raises_and_leaves_file_untouched(tmp_path: Path) -> None:
    body = '{\n  "env": {},\n  "statusLine": "x"\n  "editorMode": "vim"\n}'
    path = _write(tmp_path / "settings.json", body)

    with pytest.raises(SettingsFormatError) as excinfo:
        read_settings(path)

    assert str(path) in str(excinfo.value)
    assert path.read_text(encoding="utf-8") == body


@pytest.mark.parametrize("body", ["[]", '"a string"', "42", "null", "true"])
def test_non_object_json_raises(tmp_path: Path, body: str) -> None:
    path = _write(tmp_path / "settings.json", body)

    with pytest.raises(SettingsFormatError):
        read_settings(path)

    assert path.read_text(encoding="utf-8") == body


def test_valid_document_round_trips(tmp_path: Path) -> None:
    document = {"env": {"FOO": "bar"}, "editorMode": "vim"}
    path = _write(tmp_path / "settings.json", json.dumps(document))

    assert read_settings(path) == document


def test_unreadable_path_raises_rather_than_yielding_empty(tmp_path: Path) -> None:
    """An unreadable settings file must refuse, not silently look like `{}`.

    Treating it as empty would rewrite the user's config from scratch. A
    directory in its place is the portable way to force the read to fail.
    """
    path = tmp_path / "settings.json"
    path.mkdir()

    with pytest.raises(SettingsFormatError):
        read_settings(path)


# ---------------------------------------------------------------------------
# merge_stop_hook — shape refusals
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("hooks", [[], "a string", 42, None])
def test_hooks_not_object_raises(hooks: Any) -> None:
    with pytest.raises(SettingsFormatError) as excinfo:
        merge_stop_hook({"hooks": hooks}, command=COMMAND)

    assert "hooks" in str(excinfo.value)


@pytest.mark.parametrize("stop", [{}, "a string", 42])
def test_stop_not_list_raises(stop: Any) -> None:
    with pytest.raises(SettingsFormatError) as excinfo:
        merge_stop_hook({"hooks": {"Stop": stop}}, command=COMMAND)

    assert "Stop" in str(excinfo.value)


@pytest.mark.parametrize("stop", [{}, "a string"])
def test_remove_also_refuses_a_bad_stop_shape(stop: Any) -> None:
    """Removal guesses no more than the merge does."""
    with pytest.raises(SettingsFormatError):
        remove_stop_hook({"hooks": {"Stop": stop}})


# ---------------------------------------------------------------------------
# merge_stop_hook — the happy paths
# ---------------------------------------------------------------------------


def test_merge_into_document_without_hooks_key() -> None:
    """The live shape on a fresh machine: many keys, no `hooks` at all."""
    original = {
        "env": {"BRAIN_EMBEDDER": "arctic"},
        "attribution": False,
        "statusLine": {"type": "command", "command": "synthetic"},
        "enabledPlugins": ["synthetic-plugin"],
        "editorMode": "vim",
    }

    result = merge_stop_hook(original, command=COMMAND)

    assert result.action == "added"
    assert result.changed is True
    for key, value in original.items():
        assert result.document[key] == value
    assert _stop_groups(result.document) == [_brain_group()]


def test_merge_preserves_existing_stop_and_other_events() -> None:
    original = {
        "hooks": {
            "PostToolUse": [
                {"matcher": "Write", "hooks": [{"type": "command", "command": "x"}]}
            ],
            "Stop": [_foreign_group()],
        }
    }

    result = merge_stop_hook(original, command=COMMAND)

    assert result.action == "added"
    assert result.document["hooks"]["PostToolUse"] == original["hooks"]["PostToolUse"]
    # Ours is appended LAST so a pre-existing hook keeps running first.
    assert _stop_groups(result.document) == [_foreign_group(), _brain_group()]


def test_merge_is_idempotent() -> None:
    once = merge_stop_hook({}, command=COMMAND)

    twice = merge_stop_hook(once.document, command=COMMAND)

    assert twice.action == "unchanged"
    assert twice.changed is False
    assert twice.document == once.document


def test_merge_updates_stale_command_in_place() -> None:
    """A relocated script is corrected, never duplicated."""
    stale = "/tmp/old-home/.claude/hooks/brain-capture-hook.sh"
    original = {"hooks": {"Stop": [_brain_group(command=stale, timeout=99)]}}

    result = merge_stop_hook(original, command=COMMAND)

    assert result.action == "updated"
    assert result.changed is True
    groups = _stop_groups(result.document)
    assert len(groups) == 1
    assert groups[0]["hooks"][0]["command"] == COMMAND
    assert groups[0]["hooks"][0]["timeout"] == 10


def test_merge_recognizes_inlined_command() -> None:
    """A user who inlined the plumbing command is recognized, not duplicated."""
    original = {"hooks": {"Stop": [_brain_group(command="brain claude capture-hook")]}}

    result = merge_stop_hook(original, command=COMMAND)

    assert result.action == "updated"
    assert len(_stop_groups(result.document)) == 1


def test_merge_preserves_sibling_keys_in_our_entry() -> None:
    """Only `command` and `timeout` are corrected; the user's extras survive."""
    group = {
        "matcher": "*",
        "hooks": [
            {
                "type": "command",
                "command": "brain claude capture-hook",
                "timeout": 3,
                "customField": "keep me",
            }
        ],
    }

    result = merge_stop_hook({"hooks": {"Stop": [group]}}, command=COMMAND)

    entry = _stop_groups(result.document)[0]["hooks"][0]
    assert entry["customField"] == "keep me"
    assert _stop_groups(result.document)[0]["matcher"] == "*"
    assert entry["command"] == COMMAND
    assert entry["timeout"] == 10


def test_merge_collapses_duplicate_brain_entries() -> None:
    """"Present exactly once" holds even when the document already had two.

    A hand-edited settings.json can end up with the hook listed twice, which
    means two nudges per session. Merging must converge on one.
    """
    original = {
        "hooks": {
            "Stop": [
                _brain_group(command="/tmp/old-a/brain-capture-hook.sh"),
                _foreign_group(),
                _brain_group(command="/tmp/old-b/brain-capture-hook.sh"),
            ]
        }
    }

    result = merge_stop_hook(original, command=COMMAND)

    brain_entries = [
        entry
        for group in _stop_groups(result.document)
        for entry in group.get("hooks", [])
        if "brain-capture-hook" in str(entry.get("command", ""))
    ]
    assert len(brain_entries) == 1
    assert brain_entries[0]["command"] == COMMAND
    # The user's own hook is untouched and still present.
    assert _foreign_group() in _stop_groups(result.document)


def test_merge_collapsing_duplicates_is_idempotent() -> None:
    original = {
        "hooks": {
            "Stop": [
                _brain_group(command="/tmp/old-a/brain-capture-hook.sh"),
                _brain_group(command="/tmp/old-b/brain-capture-hook.sh"),
            ]
        }
    }

    once = merge_stop_hook(original, command=COMMAND)
    twice = merge_stop_hook(once.document, command=COMMAND)

    assert once.action == "updated"
    assert twice.action == "unchanged"
    assert twice.document == once.document


def test_merge_keeps_a_sibling_entry_inside_our_group() -> None:
    """A foreign entry co-located with ours is carried through untouched."""
    group = {
        "hooks": [
            {"type": "command", "command": FOREIGN_COMMAND},
            {"type": "command", "command": "brain claude capture-hook"},
        ]
    }

    result = merge_stop_hook({"hooks": {"Stop": [group]}}, command=COMMAND)

    entries = _stop_groups(result.document)[0]["hooks"]
    assert entries[0] == {"type": "command", "command": FOREIGN_COMMAND}
    assert entries[1]["command"] == COMMAND


def test_merge_skips_malformed_groups_without_raising() -> None:
    """Junk entries in a user's Stop list are carried through, not interpreted."""
    original = {"hooks": {"Stop": ["not a dict", {"hooks": "not a list"}, {"hooks": [7]}]}}

    result = merge_stop_hook(original, command=COMMAND)

    assert result.action == "added"
    assert _stop_groups(result.document)[:3] == original["hooks"]["Stop"]
    assert _stop_groups(result.document)[3] == _brain_group()


def test_merge_honors_custom_timeout() -> None:
    result = merge_stop_hook({}, command=COMMAND, timeout_seconds=30)

    assert _stop_groups(result.document)[0]["hooks"][0]["timeout"] == 30


def test_merge_omits_matcher_on_a_new_group() -> None:
    """`Stop` has no matcher semantics; emitting `""` only invites confusion."""
    result = merge_stop_hook({}, command=COMMAND)

    assert "matcher" not in _stop_groups(result.document)[0]


def test_merge_does_not_mutate_input() -> None:
    original = {
        "env": {"A": "1"},
        "hooks": {"Stop": [_foreign_group()], "PostToolUse": [{"hooks": []}]},
    }
    snapshot = copy.deepcopy(original)

    merge_stop_hook(original, command=COMMAND)

    assert original == snapshot


def test_merged_document_is_a_distinct_object_graph() -> None:
    """Mutating the result must not reach back into the caller's document."""
    original: dict[str, Any] = {"hooks": {"Stop": [_foreign_group()]}}

    result = merge_stop_hook(original, command=COMMAND)
    result.document["hooks"]["Stop"].append({"hooks": []})

    assert len(original["hooks"]["Stop"]) == 1


# ---------------------------------------------------------------------------
# remove_stop_hook
# ---------------------------------------------------------------------------


def test_remove_drops_entry_group_and_empty_keys() -> None:
    """A document that had only our hook is left with no rubble."""
    document = merge_stop_hook({"env": {"A": "1"}}, command=COMMAND).document

    result = remove_stop_hook(document)

    assert result.action == "removed"
    assert result.changed is True
    assert "hooks" not in result.document
    assert result.document["env"] == {"A": "1"}


def test_remove_keeps_a_foreign_stop_hook() -> None:
    document = merge_stop_hook(
        {"hooks": {"Stop": [_foreign_group()]}}, command=COMMAND
    ).document

    result = remove_stop_hook(document)

    assert result.action == "removed"
    assert _stop_groups(result.document) == [_foreign_group()]


def test_remove_keeps_other_hook_events() -> None:
    original = {"hooks": {"PostToolUse": [{"hooks": []}], "Stop": [_brain_group()]}}

    result = remove_stop_hook(original)

    assert "Stop" not in result.document["hooks"]
    assert result.document["hooks"]["PostToolUse"] == [{"hooks": []}]


def test_remove_keeps_a_sibling_entry_inside_our_group() -> None:
    """Only the brain entry is filtered; a co-located foreign entry survives."""
    group = {
        "hooks": [
            {"type": "command", "command": COMMAND, "timeout": 10},
            {"type": "command", "command": FOREIGN_COMMAND},
        ]
    }

    result = remove_stop_hook({"hooks": {"Stop": [group]}})

    assert _stop_groups(result.document) == [
        {"hooks": [{"type": "command", "command": FOREIGN_COMMAND}]}
    ]


@pytest.mark.parametrize(
    "document",
    [
        {},
        {"env": {}},
        {"hooks": {}},
        {"hooks": {"Stop": []}},
        {"hooks": {"Stop": [_foreign_group()]}},
    ],
)
def test_remove_when_absent_reports_absent(document: dict[str, Any]) -> None:
    result = remove_stop_hook(document)

    assert result.action == "absent"
    assert result.changed is False


def test_remove_does_not_mutate_input() -> None:
    original = merge_stop_hook(
        {"hooks": {"Stop": [_foreign_group()]}}, command=COMMAND
    ).document
    snapshot = copy.deepcopy(original)

    remove_stop_hook(original)

    assert original == snapshot


def test_remove_is_idempotent() -> None:
    document = merge_stop_hook({}, command=COMMAND).document

    once = remove_stop_hook(document)
    twice = remove_stop_hook(once.document)

    assert once.action == "removed"
    assert twice.action == "absent"


# ---------------------------------------------------------------------------
# serialize
# ---------------------------------------------------------------------------


def test_serialize_round_trips() -> None:
    document = merge_stop_hook({"env": {"A": "1"}}, command=COMMAND).document

    text = serialize(document)

    assert json.loads(text) == document
    assert text.endswith("\n")
    assert '\n  "env"' in text  # 2-space indent, the Claude Code house style.


def test_serialize_keeps_non_ascii_readable() -> None:
    text = serialize({"note": "café — ✓"})

    assert "café — ✓" in text
