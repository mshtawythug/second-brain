"""Synthetic Apple Notes answers for the RM-36 tests, and a fake ``osascript`` runner.

Every value is invented: no real note, folder, account or person. The fake runner
parses the request element exactly as the JXA script would (``JSON.parse(argv[0])``)
and answers from the notes it was built with, so tests exercise the real argv.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any

from brain.ingest.apple_notes import JXA_SCRIPT, AppleNotesError

FOLDER = "Reading"
OTHER_FOLDER = "Projects"
CREATED = "2026-09-01T08:00:00.000Z"
MODIFIED = "2026-10-20T09:30:00.000Z"


def note_id(n: int) -> str:
    """A Notes-shaped id for a synthetic store."""
    return f"x-coredata://00000000-0000-4000-8000-000000000000/ICNote/p{n}"


def note(
    n: int,
    *,
    name: str | None = None,
    folder: str = FOLDER,
    body: str | None = None,
    modified: str = MODIFIED,
) -> dict[str, Any]:
    """One note entry as the script emits it."""
    title = name if name is not None else f"Synthetic note {n}"
    return {
        "id": note_id(n),
        "name": title,
        "folder": folder,
        "created": CREATED,
        "modified": modified,
        "body": body if body is not None else f"<div><h1>{title}</h1></div><div>Body {n}.</div>",
    }


@dataclass
class FakeOsascript:
    """A :data:`NotesRunner` that answers like the script, from ``notes``.

    ``notes`` are eligible notes in the order the script would sort them (newest
    first); the fake applies the request's folder filter and limit, and records
    every argv. ``fail`` raises instead; ``raw`` answers with that text verbatim.
    """

    notes: list[dict[str, Any]] = field(default_factory=list)
    locked: int = 0
    folders: tuple[str, ...] = (FOLDER, OTHER_FOLDER)
    fail: AppleNotesError | None = None
    raw: str | None = None
    calls: list[list[str]] = field(default_factory=list)

    def __call__(self, argv: list[str]) -> str:
        self.calls.append(argv)
        assert argv[:5] == ["osascript", "-l", "JavaScript", "-e", JXA_SCRIPT]
        assert len(argv) == 6
        if self.fail is not None:
            raise self.fail
        if self.raw is not None:
            return self.raw
        request = json.loads(argv[5])
        wanted = request["folders"]
        eligible = [n for n in self.notes if n["folder"] in wanted]
        return json.dumps(
            {
                "notes": eligible[: request["limit"]],
                "eligible": len(eligible),
                "locked": self.locked,
                "missing": [w for w in wanted if w not in self.folders],
            }
        )

    def request(self, index: int = -1) -> dict[str, Any]:
        """The request element of a recorded call, parsed."""
        parsed: dict[str, Any] = json.loads(self.calls[index][5])
        return parsed
