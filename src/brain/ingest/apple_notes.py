"""Apple Notes reader for `brain ingest-apple-notes` (RM-36): the `osascript` call, parsed.

Path (b) of the new-source protocol: ``osascript -l JavaScript`` (a macOS
built-in) runs :data:`JXA_SCRIPT` against the Notes app, behind an injectable
:data:`NotesRunner` so tests never run it. This module reads and shapes;
:mod:`brain.cli_ingest_apple_notes` writes.

**No injection.** The script is a fixed constant and is never formatted. What a
run asks for (folder names, ``--since``, the limit) travels as one JSON argv
element that the script reads with ``JSON.parse(argv[0])``, so a folder named
``"); Application("Finder").delete(...`` is a string the script compares, never
code it runs. The element always starts with ``{``, so ``osascript`` cannot take
it for an option.

**Read only.** The script asks Notes for properties and nothing else: no
``make``, ``delete``, ``move`` or ``save``, and no StandardAdditions (so no
``doShellScript``). ``tests/test_apple_notes_reader.py`` pins that.

**What crosses stdout.** For each note in a named folder (matched by exact
name, in every account): its id, name, folder, creation and modification times,
and for the newest ``limit`` unlocked notes only, the HTML body with every
``<img>`` removed (attachments are out of scope, and inline image data would
dominate the output). A locked note (``passwordProtected``) is counted and its
body never requested. The newest-first sort and the cap run inside the script,
because Notes scripting is slow per note and a body is the slow part.

**Errors.** Every :class:`AppleNotesError` message is written here and quotes no
``osascript`` output; its stderr is discarded unread.
"""
from __future__ import annotations

import json
import os
import subprocess
import unicodedata
from collections.abc import Callable
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path
from typing import Any

from . import ExtractedDoc
from .capped_process import run_capped
from .github_docs import one_line
from .notes_html import html_to_markdown

SOURCE_KIND = "applenotes"
CONTENT_TYPE = "apple_note"
#: Metadata keys that change without the body changing; a re-run patches only these.
VOLATILE_KEYS = ("folder", "modified")
UNTITLED = "Untitled note"

#: One ``osascript`` call: argv in, stdout out; raises :class:`AppleNotesError`.
NotesRunner = Callable[[list[str]], str]

#: The ticket's bound on one run; ``--since`` and ``--max`` keep a run inside it.
OSASCRIPT_TIMEOUT_SECONDS = 120
#: The largest answer read (bodies have their images removed already).
OSASCRIPT_MAX_STDOUT_BYTES = 64 * 1024 * 1024
#: The binary, by absolute path: a ``PATH`` lookup would run whatever ``osascript``
#: comes first on it (#103 security review).
OSASCRIPT_PATH = "/usr/bin/osascript"
#: The largest body converted; a bigger note is skipped and counted. The
#: converter is linear and its output capped, so this bounds the work per note.
MAX_BODY_CHARS = 2_000_000
#: A Notes id is ``x-coredata://<store>/ICNote/p<n>``; anything far longer is not one.
_MAX_ID_CHARS = 512

#: The JXA program. A constant: never formatted, never concatenated with input.
JXA_SCRIPT = r"""
function run(argv) {
  var request = JSON.parse(argv[0]);
  var wanted = request.folders;
  var since = request.since === null ? null : new Date(request.since);
  var folders = Application("Notes").folders();
  var matched = {};
  var seen = {};
  var picked = [];
  var locked = 0;
  for (var f = 0; f < folders.length; f++) {
    var folderName = folders[f].name();
    if (wanted.indexOf(folderName) < 0) continue;
    matched[folderName] = true;
    var notes = folders[f].notes;
    var ids = notes.id();
    var names = notes.name();
    var created = notes.creationDate();
    var modified = notes.modificationDate();
    var isLocked = notes.passwordProtected();
    for (var i = 0; i < ids.length; i++) {
      if (seen[ids[i]]) continue;
      seen[ids[i]] = true;
      if (since !== null && modified[i] < since) continue;
      if (isLocked[i]) { locked++; continue; }
      picked.push({
        notes: notes, id: ids[i], name: names[i], folder: folderName,
        created: created[i].toISOString(), modified: modified[i].toISOString(),
        time: modified[i].getTime()
      });
    }
  }
  picked.sort(function (a, b) { return b.time - a.time; });
  var out = [];
  for (var k = 0; k < picked.length && k < request.limit; k++) {
    var p = picked[k];
    var body = p.notes.byId(p.id).body();
    out.push({
      id: p.id, name: p.name, folder: p.folder, created: p.created, modified: p.modified,
      body: body.replace(/<img\b[^>]*>/gi, "")
    });
  }
  var missing = wanted.filter(function (w) { return !matched[w]; });
  return JSON.stringify({notes: out, eligible: picked.length, locked: locked, missing: missing});
}
"""

_PERMISSION_HINT = (
    "if macOS asked for permission to control Notes, allow it in System Settings → "
    "Privacy & Security → Automation, then rerun"
)


class AppleNotesError(RuntimeError):
    """The ``osascript`` call failed or answered something unusable. Quotes no output."""


@dataclass(frozen=True)
class NotesRequest:
    """What one run asks the script for."""

    folders: tuple[str, ...]
    since: date | None
    limit: int

    def to_json(self) -> str:
        """The one argv element the script parses. ``--since`` is local midnight."""
        since = None if self.since is None else f"{self.since.isoformat()}T00:00:00"
        return json.dumps({"folders": list(self.folders), "since": since, "limit": self.limit})


@dataclass(frozen=True)
class Note:
    """One note as the script returned it, validated."""

    id: str
    name: str
    folder: str
    body_html: str
    created: str
    modified: str


@dataclass(frozen=True)
class NotesBatch:
    """One run's answer. ``eligible`` counts every unlocked note in range, read or not."""

    notes: tuple[Note, ...]
    eligible: int
    locked: int
    missing: tuple[str, ...]
    malformed: int

    @property
    def remainder(self) -> int:
        """Eligible notes the cap left unread."""
        return max(0, self.eligible - len(self.notes) - self.malformed)


def osascript_argv(request: NotesRequest) -> list[str]:
    """The argv for one run: the fixed script, then the request as data."""
    return ["osascript", "-l", "JavaScript", "-e", JXA_SCRIPT, request.to_json()]


def osascript_runner(
    args: list[str],
    *,
    binary: str = OSASCRIPT_PATH,
    popen: Callable[..., subprocess.Popen[bytes]] = subprocess.Popen,
    timeout: float = OSASCRIPT_TIMEOUT_SECONDS,
    max_stdout_bytes: int = OSASCRIPT_MAX_STDOUT_BYTES,
) -> str:
    """The production :data:`NotesRunner`; ``args[0]`` names it, ``binary`` runs.

    ``binary`` (default :data:`OSASCRIPT_PATH`) and ``popen`` are test seams.
    """
    if not args:
        raise AppleNotesError("osascript runner invoked with no arguments")
    if not Path(binary).is_file():
        raise AppleNotesError(f"{binary} not found: brain ingest-apple-notes needs macOS")
    try:
        run = run_capped(
            [binary, *args[1:]],
            timeout=timeout,
            max_stdout_bytes=max_stdout_bytes,
            env=os.environ,
            popen=popen,
        )
    except OSError as exc:
        raise AppleNotesError(f"`osascript` could not run ({type(exc).__name__})") from None
    if run.oversized:
        raise AppleNotesError(
            f"`osascript` output exceeded {max_stdout_bytes} bytes; lower --max or narrow --since"
        )
    if run.timed_out:
        raise AppleNotesError(
            f"`osascript` timed out after {timeout:g}s; lower --max or narrow --since"
        )
    if run.returncode != 0:
        raise AppleNotesError(f"`osascript` failed (exit {run.returncode}); {_PERMISSION_HINT}")
    return run.stdout.decode("utf-8", errors="replace")


def read_notes(request: NotesRequest, *, runner: NotesRunner) -> NotesBatch:
    """Run the script once and validate its answer; at most ``request.limit`` notes."""
    try:
        answer = json.loads(runner(osascript_argv(request)))
    except (json.JSONDecodeError, RecursionError):  # RecursionError: nesting too deep
        raise AppleNotesError("could not parse the osascript output") from None
    if not isinstance(answer, dict):
        raise AppleNotesError("the osascript output was not a JSON object")
    raw_notes = answer.get("notes")
    missing = answer.get("missing")
    eligible = _count(answer.get("eligible"))
    locked = _count(answer.get("locked"))
    if (
        not isinstance(raw_notes, list)
        or not isinstance(missing, list)
        or not all(isinstance(name, str) for name in missing)
        or eligible is None
        or locked is None
    ):
        raise AppleNotesError("the osascript output was missing a field")
    returned = len(raw_notes)
    raw_notes = raw_notes[: request.limit]  # the script caps too; the cost bound is ours
    notes = tuple(note for raw in raw_notes if (note := _note(raw)) is not None)
    return NotesBatch(
        notes=notes,
        eligible=max(eligible, returned),
        locked=locked,
        missing=tuple(missing),
        malformed=len(raw_notes) - len(notes),
    )


def _count(value: Any) -> int | None:
    if isinstance(value, int) and not isinstance(value, bool) and value >= 0:
        return value
    return None


def _note(raw: Any) -> Note | None:
    """A :class:`Note`, or ``None`` for an entry missing what the key or the body needs."""
    if not isinstance(raw, dict):
        return None
    fields: list[str] = []
    for key in ("id", "name", "folder", "body", "created", "modified"):
        value = raw.get(key)
        if not isinstance(value, str):
            return None
        fields.append(value)
    note_id, name, folder, body, created, modified = fields
    if not _is_note_id(note_id) or not (_is_time(created) and _is_time(modified)):
        return None
    return Note(note_id, name, folder, body, created, modified)


def _is_note_id(value: str) -> bool:
    return 0 < len(value) <= _MAX_ID_CHARS and not any(
        unicodedata.category(ch).startswith("C") or ch.isspace() for ch in value
    )


def _is_time(value: str) -> bool:
    try:
        datetime.fromisoformat(value)
    except ValueError:
        return False
    return True


def build_document(note: Note) -> ExtractedDoc | None:
    """The note as a document; ``None`` when its body holds no readable text.

    ``metadata.date`` (promoted to ``sent_at``) is the creation time, which an
    edit never moves; the folder and the modification time are volatile
    (:data:`VOLATILE_KEYS`).
    """
    content = html_to_markdown(note.body_html)
    if not content:
        return None
    return ExtractedDoc(
        title=one_line(note.name) or UNTITLED,
        content=content,
        content_type=CONTENT_TYPE,
        source_path=None,
        metadata={
            "folder": one_line(note.folder),
            "created": note.created,
            "modified": note.modified,
            "date": note.created,
        },
    )
