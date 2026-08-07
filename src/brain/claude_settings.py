"""Non-destructive merge/removal of the brain Stop hook in Claude Code settings.json."""
from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .errors import SettingsFormatError

# Substrings that identify OUR Stop entry. Matching on a marker rather than on
# path equality means a user who relocated the script, or who inlined the
# plumbing command directly, is still recognized -- so a re-run reports "already
# present" instead of appending a second nudge.
_MARKERS: tuple[str, ...] = ("brain-capture-hook", "brain claude capture-hook")

#: Claude Code kills a hook that outruns this. Ten seconds is far beyond the
#: filesystem-only decision path, and bounds the damage if something wedges.
_DEFAULT_TIMEOUT_SECONDS: int = 10

_HOOKS_KEY = "hooks"
_STOP_KEY = "Stop"


@dataclass(frozen=True)
class SettingsMerge:
    """Result of a merge/remove: the new document plus what changed."""

    document: dict[str, Any]
    changed: bool
    action: str  # "added" | "updated" | "unchanged" | "removed" | "absent"


def read_settings(path: Path) -> dict[str, Any]:
    """Return the parsed settings document, or ``{}`` when missing/empty.

    Missing file -> ``{}``. Whitespace-only contents -> ``{}``. Invalid JSON, or
    valid JSON that is not an object, raises :class:`SettingsFormatError` -- we
    never silently discard a user's config, and a caller that cannot parse it
    must refuse to rewrite it.
    """
    try:
        raw = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return {}
    except OSError as exc:
        raise SettingsFormatError(f"{path} could not be read ({exc})") from exc

    if not raw.strip():
        return {}

    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise SettingsFormatError(
            f"{path} is not valid JSON ({exc.msg}: line {exc.lineno} column {exc.colno}) "
            "— refusing to rewrite it. Fix the file by hand, then re-run."
        ) from exc

    if not isinstance(parsed, dict):
        raise SettingsFormatError(
            f"{path} is a JSON {type(parsed).__name__}, expected an object "
            "— refusing to rewrite it."
        )
    return parsed


def _hooks_of(document: Mapping[str, Any]) -> dict[str, Any]:
    """The ``hooks`` object, refusing anything that is not one."""
    hooks = document.get(_HOOKS_KEY, {})
    if not isinstance(hooks, dict):
        raise SettingsFormatError(
            f"`{_HOOKS_KEY}` is a {type(hooks).__name__}, expected an object "
            "— refusing to rewrite it."
        )
    return hooks


def _stop_of(hooks: Mapping[str, Any]) -> list[Any]:
    """The ``hooks.Stop`` list, refusing anything that is not one."""
    stop = hooks.get(_STOP_KEY, [])
    if not isinstance(stop, list):
        raise SettingsFormatError(
            f"`{_HOOKS_KEY}.{_STOP_KEY}` is a {type(stop).__name__}, expected a list "
            "— refusing to rewrite it."
        )
    return stop


def _is_brain_entry(entry: Any) -> bool:
    """True when ``entry`` is a command hook naming one of our markers."""
    if not isinstance(entry, Mapping) or entry.get("type") != "command":
        return False
    command = entry.get("command")
    return isinstance(command, str) and any(marker in command for marker in _MARKERS)


def _entries_of(group: Any) -> list[Any]:
    """The ``hooks`` list inside one Stop group, or ``[]`` when malformed.

    A group we cannot interpret is carried through untouched rather than
    repaired -- guessing at a user's config is how a working hook gets silently
    disabled.
    """
    if not isinstance(group, Mapping):
        return []
    entries = group.get(_HOOKS_KEY)
    return entries if isinstance(entries, list) else []


def _rebuild(
    document: Mapping[str, Any], hooks: Mapping[str, Any], stop: list[Any]
) -> dict[str, Any]:
    """Reassemble bottom-up with new objects at every level.

    Every unrelated key -- ``env``, ``statusLine``, ``PreToolUse``, other Stop
    groups -- is carried through by construction, so the caller's document is
    never mutated and nothing needs an explicit allowlist. An empty ``Stop`` or
    an empty ``hooks`` is dropped rather than left as rubble.
    """
    if stop:
        new_hooks: dict[str, Any] = {**hooks, _STOP_KEY: stop}
    else:
        new_hooks = {key: value for key, value in hooks.items() if key != _STOP_KEY}

    if new_hooks:
        return {**document, _HOOKS_KEY: new_hooks}
    return {key: value for key, value in document.items() if key != _HOOKS_KEY}


def merge_stop_hook(
    document: Mapping[str, Any],
    *,
    command: str,
    timeout_seconds: int = _DEFAULT_TIMEOUT_SECONDS,
) -> SettingsMerge:
    """Return a NEW document with the brain Stop hook present exactly once.

    An existing brain entry -- found by marker substring, so a relocated script
    or an inlined command both count -- has its ``command`` and ``timeout``
    corrected in place; every sibling key on that entry survives. When no entry
    exists a new group is appended at the END of ``Stop``, so any pre-existing
    user hook keeps running first. ``matcher`` is omitted: ``Stop`` has no
    matcher semantics, and emitting ``""`` invites confusion.
    """
    hooks = _hooks_of(document)
    stop = _stop_of(hooks)

    new_stop: list[Any] = []
    found = False
    changed = False

    for group in stop:
        entries = _entries_of(group)
        if not any(_is_brain_entry(entry) for entry in entries):
            new_stop.append(group)
            continue

        new_entries: list[Any] = []
        for entry in entries:
            if not _is_brain_entry(entry):
                new_entries.append(entry)
                continue
            if found:
                # A hand-edited file can list the hook twice, which means two
                # nudges per session. Keep the first, drop the rest.
                changed = True
                continue
            found = True
            if entry.get("command") == command and entry.get("timeout") == timeout_seconds:
                new_entries.append(entry)
                continue
            changed = True
            new_entries.append({**entry, "command": command, "timeout": timeout_seconds})

        # A group emptied by de-duplication is dropped rather than left as an
        # empty shell.
        if new_entries:
            new_stop.append({**group, _HOOKS_KEY: new_entries})

    if not found:
        new_stop.append(
            {
                _HOOKS_KEY: [
                    {"type": "command", "command": command, "timeout": timeout_seconds}
                ]
            }
        )
        return SettingsMerge(
            document=_rebuild(document, hooks, new_stop), changed=True, action="added"
        )

    # Rebuild on every branch, so the result is always a distinct object graph
    # regardless of which one produced it.
    return SettingsMerge(
        document=_rebuild(document, hooks, new_stop),
        changed=changed,
        action="updated" if changed else "unchanged",
    )


def remove_stop_hook(document: Mapping[str, Any]) -> SettingsMerge:
    """Return a NEW document with every brain Stop hook entry removed.

    Leaves no rubble: a group whose entry list empties is dropped, ``Stop`` is
    dropped when its list empties, and ``hooks`` is dropped when that object
    empties. A co-located foreign entry, a foreign group, and every other hook
    event survive verbatim.
    """
    hooks = _hooks_of(document)
    stop = _stop_of(hooks)

    new_stop: list[Any] = []
    removed = False

    for group in stop:
        entries = _entries_of(group)
        if not any(_is_brain_entry(entry) for entry in entries):
            new_stop.append(group)
            continue

        removed = True
        kept = [entry for entry in entries if not _is_brain_entry(entry)]
        if kept:
            new_stop.append({**group, _HOOKS_KEY: kept})

    if not removed:
        return SettingsMerge(document=dict(document), changed=False, action="absent")
    return SettingsMerge(
        document=_rebuild(document, hooks, new_stop), changed=True, action="removed"
    )


def serialize(document: Mapping[str, Any]) -> str:
    """``json.dumps(indent=2)`` + trailing newline — the Claude Code house style."""
    return json.dumps(document, indent=2, ensure_ascii=False) + "\n"
