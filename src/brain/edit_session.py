"""Editor-mode session for ``brain edit``: payload format, parsing, recovery flow.

Kept separate from :mod:`brain.cli` so the Typer routing layer stays thin and
from :mod:`brain.editor` so the subprocess-wrapper layer stays focused on the
``$VISUAL``/``$EDITOR``/``vi`` lookup.
"""
import json
import tempfile
from pathlib import Path
from typing import Any

from .editor import EditorError, make_temp_file, run_editor_on

EDITOR_SEPARATOR = "\n---\n"

__all__ = [
    "EDITOR_SEPARATOR",
    "EditorAbortedError",
    "EditorError",
    "EditorParseFailedError",
    "EditorUnchangedError",
    "build_payload",
    "parse_payload",
    "run_editor_session",
]


class EditorAbortedError(Exception):
    """Editor exited non-zero — treat as the user cancelling the edit."""


class EditorUnchangedError(Exception):
    """User did not modify the file — no DB write should follow."""


class EditorParseFailedError(Exception):
    """Two consecutive saves contained an unparseable payload.

    The :attr:`preserved_path` attribute points at a copy of the user's last
    draft saved outside the auto-cleanup of the temp file, so they can recover
    their work.
    """

    def __init__(self, message: str, preserved_path: Path) -> None:
        super().__init__(message)
        self.preserved_path = preserved_path


def build_payload(
    *,
    title: str,
    content_type: str,
    tags: list[str],
    metadata: dict[str, Any],
    body: str,
) -> str:
    """Render document fields into the JSON-header / body payload."""
    header = json.dumps(
        {
            "title": title,
            "content_type": content_type,
            "tags": list(tags),
            "metadata": dict(metadata),
        },
        indent=2,
        ensure_ascii=False,
        sort_keys=True,
    )
    return header + EDITOR_SEPARATOR + body


def _strip_header_comments(header_text: str) -> str:
    """Drop ``# ...`` recovery-comment lines before parsing JSON."""
    return "\n".join(
        line for line in header_text.splitlines() if not line.lstrip().startswith("#")
    )


def parse_payload(text: str) -> tuple[dict[str, Any], str]:
    """Split ``text`` into ``(header_dict, body)``.

    Raises :class:`ValueError` on malformed JSON, missing separator, or wrong
    header / tags / metadata types.
    """
    if EDITOR_SEPARATOR not in text:
        raise ValueError("missing '---' separator between JSON header and body")
    header_text, body = text.split(EDITOR_SEPARATOR, 1)
    cleaned = _strip_header_comments(header_text).strip()
    try:
        header = json.loads(cleaned)
    except json.JSONDecodeError as e:
        raise ValueError(f"invalid JSON header: {e.msg} (line {e.lineno})") from e
    if not isinstance(header, dict):
        raise ValueError("JSON header must be an object")
    if "tags" in header:
        tags = header["tags"]
        if not isinstance(tags, list) or not all(isinstance(t, str) for t in tags):
            raise ValueError("'tags' must be a JSON array of strings")
    if "metadata" in header and not isinstance(header["metadata"], dict):
        raise ValueError("'metadata' must be a JSON object")
    return header, body


def run_editor_session(
    initial_text: str, *, doc_id_label: str
) -> tuple[dict[str, Any], str]:
    """Open the user's editor on ``initial_text``; return parsed ``(header, body)``.

    Implements the malformed-JSON recovery flow: on a parse failure, the temp
    file is reopened with the parse error prepended as a ``# error: ...``
    comment. A second failure copies the user's draft to a stable temp path
    (``brain-edit-<id>.json``) and raises :class:`EditorParseFailedError` so
    the caller can surface where the work was preserved.

    Raises:
        EditorError: no ``$VISUAL`` / ``$EDITOR`` / ``vi`` available.
        EditorAbortedError: editor exited non-zero on either pass.
        EditorUnchangedError: file was byte-identical to ``initial_text`` after
            the first editor exit.
        EditorParseFailedError: two consecutive saves failed to parse.
    """
    temp_path = make_temp_file(initial_text)
    preserved_path: Path | None = None
    try:
        rc = run_editor_on(temp_path)
        if rc != 0:
            raise EditorAbortedError("editor exited non-zero")
        after_first = temp_path.read_text(encoding="utf-8")
        if after_first == initial_text:
            raise EditorUnchangedError("file unchanged")
        try:
            return parse_payload(after_first)
        except ValueError as e1:
            recovery_text = f"# error: {e1}\n{after_first}"
            temp_path.write_text(recovery_text, encoding="utf-8")
            rc = run_editor_on(temp_path)
            if rc != 0:
                raise EditorAbortedError("editor exited non-zero") from e1
            after_second = temp_path.read_text(encoding="utf-8")
            try:
                return parse_payload(after_second)
            except ValueError as e2:
                preserved_path = (
                    Path(tempfile.gettempdir())
                    / f"brain-edit-{doc_id_label}.json"
                )
                preserved_path.write_text(after_second, encoding="utf-8")
                raise EditorParseFailedError(str(e2), preserved_path) from e2
    finally:
        if preserved_path is None and temp_path.exists():
            temp_path.unlink()
