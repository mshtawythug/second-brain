"""The Apple Notes reader (RM-36): the fixed JXA script, the runner, the answer's validation.

The real ``osascript`` is never run: runner tests use a throwaway script named
``osascript`` in ``tmp_path``, and parsing tests the synthetic ``FakeOsascript``.
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import time
from datetime import date
from pathlib import Path
from typing import Any

import pytest

from brain.ingest import apple_notes as an
from brain.ingest.apple_notes import (
    JXA_SCRIPT,
    AppleNotesError,
    Note,
    NotesRequest,
    build_document,
    osascript_argv,
    osascript_runner,
    read_notes,
)
from tests import apple_notes_fixtures as fx

REQUEST = NotesRequest(folders=(fx.FOLDER,), since=None, limit=200)

# Names a hostile or unlucky folder could carry: JS string breakouts, template
# literals, comment openers, a newline, an option-looking dash, non-ASCII.
HOSTILE_NAMES = (
    '"); Application("Finder").delete(Path("/")); ("',
    "'); doShellScript('touch /tmp/pwned'); ('",
    "${Application('Notes').notes()}",
    "`whoami`",
    "*/ throw 1; /*",
    "</script>\nline two",
    "-e",
    "--help",
    "Réésumé   \U0001f5d2",
    "\\",
)


# --- the request element: data, never code ----------------------------------------------


@pytest.mark.parametrize("name", HOSTILE_NAMES)
def test_a_hostile_folder_name_reaches_the_script_only_as_json_data(name: str) -> None:
    argv = osascript_argv(NotesRequest(folders=(name,), since=date(2026, 10, 1), limit=5))

    assert argv[:5] == ["osascript", "-l", "JavaScript", "-e", JXA_SCRIPT]
    assert len(argv) == 6
    assert argv[4] is JXA_SCRIPT  # the program is the constant, untouched by the input
    assert argv[5].startswith("{")  # never read as an osascript option
    assert json.loads(argv[5]) == {
        "folders": [name],
        "since": "2026-10-01T00:00:00",
        "limit": 5,
    }


def test_the_script_never_interpolates_and_reads_its_input_only_through_json_parse() -> None:
    assert JXA_SCRIPT.count("argv") == 2  # the parameter and JSON.parse(argv[0])
    assert "JSON.parse(argv[0])" in JXA_SCRIPT
    assert not re.search(r"\beval\b|Function\(|\$\{|`", JXA_SCRIPT)


@pytest.mark.parametrize(
    "verb",
    ["make", "delete", "move", "save", "duplicate", "close", "doShellScript",
     "includeStandardAdditions", "currentApplication", "set(", ".write", "openForAccess"],
)
def test_the_script_only_reads(verb: str) -> None:
    assert verb not in JXA_SCRIPT


def test_the_script_skips_locked_bodies_and_strips_images() -> None:
    assert "passwordProtected()" in JXA_SCRIPT
    assert "if (isLocked[i]) { locked++; continue; }" in JXA_SCRIPT
    assert 'body.replace(/<img\\b[^>]*>/gi, "")' in JXA_SCRIPT


def test_no_since_is_null() -> None:
    assert json.loads(REQUEST.to_json())["since"] is None


# --- the production runner --------------------------------------------------------------


def _script(tmp_path: Path, body: str) -> str:
    """A throwaway executable named ``osascript`` (never the real one)."""
    script = tmp_path / "osascript"
    script.write_text(f"#!/bin/sh\n{body}\n")
    script.chmod(0o755)
    return str(script)


def test_the_runner_passes_argv_untouched_and_returns_stdout(tmp_path: Path) -> None:
    tool = _script(tmp_path, 'for a in "$@"; do printf "[%s]" "$a"; done')
    argv = osascript_argv(NotesRequest(folders=(HOSTILE_NAMES[0],), since=None, limit=1))

    out = osascript_runner(argv, which=lambda _n: tool)

    assert out == "".join(f"[{a}]" for a in argv[1:])


def test_the_runner_gets_the_environment_no_stdin_and_its_own_session(tmp_path: Path) -> None:
    tool = _script(tmp_path, "printf ok")
    seen: dict[str, Any] = {}

    def popen(argv: list[str], **kwargs: Any) -> Any:
        seen.update(kwargs)
        return subprocess.Popen(argv, **kwargs)

    assert osascript_runner(["osascript"], which=lambda _n: tool, popen=popen) == "ok"
    assert seen["env"] == dict(os.environ)
    assert seen["stdin"] is subprocess.DEVNULL
    assert seen["stderr"] is subprocess.DEVNULL
    assert seen["start_new_session"] is True


def test_a_missing_osascript_names_macos() -> None:
    with pytest.raises(AppleNotesError) as info:
        osascript_runner(["osascript"], which=lambda _n: None)

    assert str(info.value) == "osascript not found on PATH: brain ingest-apple-notes needs macOS"


def test_the_runner_refuses_empty_args() -> None:
    with pytest.raises(AppleNotesError):
        osascript_runner([], which=lambda _n: "/usr/bin/osascript")


def test_a_failure_names_the_permission_and_never_its_stderr(tmp_path: Path) -> None:
    tool = _script(tmp_path, "echo 'Not authorized: private detail' >&2\necho partial\nexit 1")

    with pytest.raises(AppleNotesError) as info:
        osascript_runner(["osascript"], which=lambda _n: tool)

    assert str(info.value) == (
        "`osascript` failed (exit 1); if macOS asked for permission to control Notes, "
        "allow it in System Settings → Privacy & Security → Automation, then rerun"
    )
    assert "private detail" not in str(info.value) and "partial" not in str(info.value)
    assert info.value.__cause__ is None and info.value.__context__ is None


def test_a_hung_osascript_is_killed_at_the_timeout(tmp_path: Path) -> None:
    tool = _script(tmp_path, "sleep 30")
    started = time.monotonic()

    with pytest.raises(AppleNotesError) as info:
        osascript_runner(["osascript"], which=lambda _n: tool, timeout=0.3)

    assert str(info.value) == (
        "`osascript` timed out after 0.3s; lower --max or narrow --since"
    )
    assert time.monotonic() - started < 10


def test_the_default_timeout_is_the_tickets_120_seconds() -> None:
    assert an.OSASCRIPT_TIMEOUT_SECONDS == 120


def test_output_over_the_cap_is_refused(tmp_path: Path) -> None:
    tool = _script(tmp_path, "head -c 300000 /dev/zero\nsleep 30")

    with pytest.raises(AppleNotesError) as info:
        osascript_runner(["osascript"], which=lambda _n: tool, max_stdout_bytes=1000)

    assert str(info.value) == (
        "`osascript` output exceeded 1000 bytes; lower --max or narrow --since"
    )


def test_an_os_error_is_reported_by_class_only() -> None:
    def popen(*_a: Any, **_k: Any) -> Any:
        raise PermissionError(13, "denied /private/path")

    with pytest.raises(AppleNotesError) as info:
        osascript_runner(["osascript"], which=lambda _n: "/x/osascript", popen=popen)

    assert str(info.value) == "`osascript` could not run (PermissionError)"


# --- read_notes -------------------------------------------------------------------------


def test_read_notes_validates_and_keeps_the_scripts_order() -> None:
    fake = fx.FakeOsascript(notes=[fx.note(2), fx.note(1)], locked=3)

    batch = read_notes(REQUEST, runner=fake)

    assert [n.id for n in batch.notes] == [fx.note_id(2), fx.note_id(1)]
    assert batch.notes[0] == Note(
        fx.note_id(2), "Synthetic note 2", fx.FOLDER,
        "<div><h1>Synthetic note 2</h1></div><div>Body 2.</div>", fx.CREATED, fx.MODIFIED,
    )
    assert (batch.eligible, batch.locked, batch.missing, batch.malformed) == (2, 3, (), 0)
    assert batch.remainder == 0
    assert fake.request() == {"folders": [fx.FOLDER], "since": None, "limit": 200}


def test_the_remainder_is_what_the_limit_left_unread() -> None:
    fake = fx.FakeOsascript(notes=[fx.note(n) for n in range(5)])

    batch = read_notes(NotesRequest((fx.FOLDER,), None, limit=2), runner=fake)

    assert len(batch.notes) == 2
    assert batch.remainder == 3


def test_an_answer_over_the_limit_is_cut_and_still_counted() -> None:
    """A misbehaving script cannot raise the cost bound: the limit is applied here too."""
    raw = {"notes": [fx.note(n) for n in range(4)], "eligible": 1, "locked": 0, "missing": []}
    fake = fx.FakeOsascript(raw=json.dumps(raw))

    batch = read_notes(NotesRequest((fx.FOLDER,), None, limit=2), runner=fake)

    assert len(batch.notes) == 2
    assert batch.eligible == 4 and batch.remainder == 2


def test_missing_folders_are_reported() -> None:
    fake = fx.FakeOsascript(notes=[fx.note(1)])

    batch = read_notes(NotesRequest((fx.FOLDER, "Nowhere"), None, 10), runner=fake)

    assert batch.missing == ("Nowhere",)


@pytest.mark.parametrize(
    "bad",
    [
        {"id": 7},
        {"id": ""},
        {"id": "x-coredata://a b"},
        {"id": "x-coredata://a\u0007"},
        {"id": "x-coredata://a​"},
        {"id": "x" * 513},
        {"name": None},
        {"body": 1},
        {"folder": []},
        {"created": "yesterday"},
        {"modified": "2026-13-01"},
    ],
)
def test_a_malformed_note_is_counted_not_fatal(bad: dict[str, Any]) -> None:
    raw = {"notes": [fx.note(1), {**fx.note(2), **bad}, "not a note"], "eligible": 3,
           "locked": 0, "missing": []}

    batch = read_notes(REQUEST, runner=fx.FakeOsascript(raw=json.dumps(raw)))

    assert [n.id for n in batch.notes] == [fx.note_id(1)]
    assert batch.malformed == 2
    assert batch.remainder == 0


def test_an_id_at_the_length_limit_is_kept() -> None:
    raw = {"notes": [{**fx.note(1), "id": "x" * 512}], "eligible": 1, "locked": 0,
           "missing": []}

    assert len(read_notes(REQUEST, runner=fx.FakeOsascript(raw=json.dumps(raw))).notes) == 1


@pytest.mark.parametrize(
    ("raw", "message"),
    [
        ("not json", "could not parse the osascript output"),
        ("[" * 100000, "could not parse the osascript output"),
        ("[]", "the osascript output was not a JSON object"),
        ('{"eligible": 0, "locked": 0, "missing": []}', "the osascript output was missing a field"),
        ('{"notes": [], "locked": 0, "missing": []}', "the osascript output was missing a field"),
        ('{"notes": [], "eligible": 0, "missing": []}', "the osascript output was missing a field"),
        ('{"notes": [], "eligible": 0, "locked": 0}', "the osascript output was missing a field"),
        ('{"notes": {}, "eligible": 0, "locked": 0, "missing": []}',
         "the osascript output was missing a field"),
        ('{"notes": [], "eligible": 0, "locked": 0, "missing": [1]}',
         "the osascript output was missing a field"),
        ('{"notes": [], "eligible": true, "locked": 0, "missing": []}',
         "the osascript output was missing a field"),
        ('{"notes": [], "eligible": 0, "locked": -1, "missing": []}',
         "the osascript output was missing a field"),
    ],
)
def test_an_unusable_answer_is_refused_without_quoting_it(raw: str, message: str) -> None:
    with pytest.raises(AppleNotesError) as info:
        read_notes(REQUEST, runner=fx.FakeOsascript(raw=raw))

    assert str(info.value) == message
    assert info.value.__cause__ is None  # raised ``from None``: no traceback quotes the text
    assert info.value.__context__ is None or info.value.__suppress_context__


# --- build_document ---------------------------------------------------------------------


def _note(**overrides: str) -> Note:
    base = dict(
        id=fx.note_id(1), name="Recipe ideas", folder=fx.FOLDER,
        body_html="<div><h1>Recipe ideas</h1></div><ul><li>Soup</li></ul>",
        created=fx.CREATED, modified=fx.MODIFIED,
    )
    return Note(**{**base, **overrides})


def test_a_note_becomes_a_markdown_document_dated_by_its_creation() -> None:
    doc = build_document(_note())

    assert doc is not None
    assert doc.title == "Recipe ideas"
    assert doc.content == "# Recipe ideas\n- Soup"
    assert doc.content_type == "apple_note"
    assert doc.source_path is None
    assert doc.metadata == {
        "folder": fx.FOLDER, "created": fx.CREATED, "modified": fx.MODIFIED, "date": fx.CREATED,
    }
    assert set(an.VOLATILE_KEYS) <= set(doc.metadata)


def test_titles_and_folders_are_one_terminal_safe_line() -> None:
    doc = build_document(_note(name="Line\none\x1b[31m", folder="A\tB\x07"))

    assert doc is not None
    assert doc.title == "Line one[31m"
    assert doc.metadata["folder"] == "A B"


@pytest.mark.parametrize("name", ["", "   ", "\x1b"])
def test_a_nameless_note_is_untitled(name: str) -> None:
    doc = build_document(_note(name=name))

    assert doc is not None and doc.title == "Untitled note"


@pytest.mark.parametrize("body", ["", "<div><br></div>", '<img src="data:image/png;base64,AA">'])
def test_a_note_with_no_readable_text_is_no_document(body: str) -> None:
    assert build_document(_note(body_html=body)) is None
