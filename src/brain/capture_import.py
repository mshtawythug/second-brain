"""Turn a shared text file into a capture-inbox note (roadmap ticket RM-39).

An iOS Shortcut ("Save to Brain", ``docs/guides/mobile-capture.md``) writes what was
shared, plus an optional comment, as a ``.md`` file into an iCloud Drive folder. That
folder is a drop folder with ``mode = "capture"``. Both ``brain capture import`` and
``brain ingest-folder`` run it through :func:`brain.drop_folder.run` with
:func:`importer`, whose every call is :func:`import_file`. That is the one code path
(RM-39 AC6). The runner moves each file into ``imported/`` first, then calls this, and
deletes the ``imported/`` copy after any success (RM-39.D2).

**The file is untrusted.** It came through a third-party sync service, so:

- It is read without following a symlink. It must be a regular file of at most
  :data:`MAX_SHARE_BYTES` and strict UTF-8; NUL characters are dropped. Anything else
  is :class:`~brain.drop_folder.RejectedFileError`, and the file stays in ``imported/``.
- No YAML parser ever sees it. :func:`parse_share` reads an optional leading ``---``
  block line by line and honours exactly one key, ``sensitivity``. Every other key
  (``allow_secrets``, ``id``, ``tags``…) is dropped and never reaches the note. A share
  whose only text is inside the block keeps the whole text as its body, so it is never
  reported empty and deleted.
- Sensitivity fails closed and only escalates. An invalid level, or a block with no
  closing ``---`` within :data:`MAX_FRONTMATTER_LINES` lines and
  :data:`MAX_FRONTMATTER_BYTES` bytes, means ``confidential``. A file's ``normal`` never
  lowers a confidential folder.
- The secret guard does not run, the same as ``brain capture``: a share is text the
  owner chose to send.

**A capture folder must not overlap the vault** (rev-111 M1): one that is the vault,
lies inside it (``<vault>/capture``), or holds it would take the vault's own notes as
shares, re-create each and delete the original, every run. :func:`importer` refuses it
with :class:`~brain.drop_folder.DropFolderError`, by RM-37's rule
(:func:`brain.ingest.import_sync.is_within`, applied both ways).

**The note** is written the way ``brain capture`` writes one: a vault-tier note under
``capture/``, tagged ``inbox``, titled by
:func:`brain.capture.make_capture_title`. Its frontmatter carries
``capture_import: <share_key>``, which lands in ``documents.metadata``. A file whose key
a vault note already carries is ``unchanged``, so the same share seen twice is one note,
while two different files with one name are two.

**Content is never lost, and never written twice.** Before the note file is written,
every failure raises and the runner moves the file back for the next run. Once the note
file exists, the content is safe in the vault, so an indexing failure (the database,
the embedder) returns ``"written"``: the next vault sync indexes it. Raising there
instead would move the file back and author a second note on the retry.
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import stat
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import TYPE_CHECKING, Any

from .capture import make_capture_title
from .drop_folder import DropFolderError, IngestFn, MoveRaceError, RejectedFileError
from .errors import describe_failure
from .ingest.import_sync import is_within
from .sensitivity import CONFIDENTIAL, VALID_SENSITIVITY_LEVELS
from .vault.note_builder import index_vault_note, require_autocommit, write_vault_note

if TYPE_CHECKING:
    import psycopg

    from .config import Config
    from .ingest import Embedder

logger = logging.getLogger(__name__)

#: A share is a link and a comment; a pasted article fits too. Bigger is rejected.
MAX_SHARE_BYTES = 1 << 20
MAX_FRONTMATTER_LINES = 40
MAX_FRONTMATTER_BYTES = 4096
CAPTURE_FOLDER = "capture"
INBOX_TAG = "inbox"
#: The frontmatter (and ``documents.metadata``) key carrying :func:`share_key`.
KEY_FIELD = "capture_import"
_FENCE = "---"
_SENSITIVITY_LINE = re.compile(r"\s*sensitivity\s*:(.*)")
_QUOTES = "\"'"
VAULT_OVERLAP = (
    "a capture folder must not be the vault, be inside it, or contain it: "
    "it would take the vault's own notes as shares"
)


@dataclass(frozen=True)
class Share:
    """A parsed share: its body, and the level its frontmatter asked for (``None``: none)."""

    body: str
    sensitivity: str | None


@dataclass(frozen=True)
class CaptureContext:
    """What :func:`import_file` needs besides the file: config, embedder, the folder."""

    cfg: Config
    embedder: Embedder
    sensitivity: str  # the folder's level
    folder: Path


def overlaps_vault(folder: Path, vault: Path) -> bool:
    """Whether ``folder`` is the vault, lies inside it, or contains it (module docstring)."""
    return is_within(folder, vault) or is_within(vault, folder)


def escalate(folder_level: str, share_level: str | None) -> str:
    """The stricter of the two levels: a share can raise its folder's level, never lower it."""
    return CONFIDENTIAL if CONFIDENTIAL in (folder_level, share_level) else folder_level


def share_key(title: str, raw: bytes) -> str:
    """The idempotency key: SHA-256 of the original name, a NUL, and the file's bytes."""
    return hashlib.sha256(title.encode("utf-8") + b"\x00" + raw).hexdigest()


def parse_share(text: str) -> Share:
    """Split an optional leading ``---`` block from the body (module docstring)."""
    text = text.removeprefix("﻿").replace("\r\n", "\n").replace("\r", "\n")
    lines = text.split("\n")
    if lines[0].rstrip() != _FENCE:
        return Share(body=text.strip(), sensitivity=None)
    block: list[str] = []
    for line in lines[1 : MAX_FRONTMATTER_LINES + 1]:
        if line.rstrip() == _FENCE:
            if len("\n".join(block).encode("utf-8")) > MAX_FRONTMATTER_BYTES:
                break
            body = "\n".join(lines[len(block) + 2 :]).strip()
            if not body and any(_is_content(line) for line in block):
                body = text.strip()  # text only inside the block: never lose it
            return Share(body=body, sensitivity=_level(block))
        block.append(line)
    # No closing fence in bounds: the intent is unknown, so fail closed.
    return Share(body=text.strip(), sensitivity=CONFIDENTIAL)


def _is_content(line: str) -> bool:
    return bool(line.strip()) and _SENSITIVITY_LINE.fullmatch(line) is None


def _level(block: list[str]) -> str | None:
    """The block's ``sensitivity`` (the stricter of several); an invalid one is confidential."""
    level: str | None = None
    for line in block:
        match = _SENSITIVITY_LINE.fullmatch(line)
        if match is None:
            continue
        value = match.group(1).strip().strip(_QUOTES).lower()
        found = value if value in VALID_SENSITIVITY_LEVELS else CONFIDENTIAL
        level = escalate(found, level)
    return level


def importer(ctx: CaptureContext) -> IngestFn:
    """The drop-folder runner's ingest step for a capture folder: :func:`import_file`.

    Raises :class:`~brain.drop_folder.DropFolderError` when the folder overlaps the vault.
    """
    if overlaps_vault(ctx.folder, ctx.cfg.vault_path):
        raise DropFolderError(VAULT_OVERLAP)

    def ingest(conn: psycopg.Connection[Any], path: Path, *, title: str, timeout: float) -> str:
        return import_file(conn, path, title=title, timeout=timeout, ctx=ctx)

    return ingest


def import_file(
    conn: psycopg.Connection[Any],
    path: Path,
    *,
    title: str,
    timeout: float,
    ctx: CaptureContext,
) -> str:
    """Make the share at ``path`` an inbox note; return the runner's outcome.

    ``title`` is the file's original stem (it keys the share; the note's own title comes
    from its body). ``timeout`` is unused: the embedder carries its own. Returns
    ``"ingested"``, ``"unchanged"``, ``"empty"`` or ``"written"`` (module docstring).
    """
    del timeout
    raw = _read(path)
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise RejectedFileError("the share is not UTF-8 text") from exc
    share = parse_share(text.replace("\x00", ""))
    if not share.body:
        return "empty"
    key = share_key(title, raw)
    if _captured(conn, key):
        return "unchanged"
    cfg, vault = ctx.cfg, ctx.cfg.vault_path
    extra = {KEY_FIELD: key}
    sensitivity = escalate(ctx.sensitivity, share.sensitivity)
    if sensitivity == CONFIDENTIAL:
        extra["sensitivity"] = CONFIDENTIAL
    require_autocommit(conn, vault)
    target, document_id = write_vault_note(
        vault_path=vault,
        title=make_capture_title(share.body, today=date.today(), max_words=cfg.capture_title_words),
        body=f"{share.body}\n",
        tags=[INBOX_TAG],
        folder=CAPTURE_FOLDER,
        extra_frontmatter=extra,
    )
    try:
        index_vault_note(conn, cfg=cfg, vault_path=vault, target=target, embedder=ctx.embedder)
    except Exception as exc:  # noqa: BLE001 - the note is written: never retry (docstring)
        logger.warning(
            "capture %s was written to the vault but not indexed (%s); "
            "the next vault sync indexes it",
            document_id[:8],
            describe_failure(exc),
        )
        return "written"
    return "ingested"


def _read(path: Path) -> bytes:
    """The file's bytes: no symlink followed, no FIFO blocked on, at most the cap.

    The cap is enforced on what is read, not on ``st_size``, so a file that grows after
    being opened is still refused.
    """
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            raise MoveRaceError("not a regular file")
        chunks: list[bytes] = []
        size = 0
        while size <= MAX_SHARE_BYTES and (chunk := os.read(fd, MAX_SHARE_BYTES + 1 - size)):
            chunks.append(chunk)
            size += len(chunk)
    finally:
        os.close(fd)
    if size > MAX_SHARE_BYTES:
        raise RejectedFileError(f"the share is over {MAX_SHARE_BYTES} bytes")
    return b"".join(chunks)


def _captured(conn: psycopg.Connection[Any], key: str) -> bool:
    """Whether a vault note already carries ``key`` (the share was captured before)."""
    row = conn.execute(
        "SELECT 1 FROM documents WHERE kind = 'vault' AND metadata @> %s::jsonb LIMIT 1",
        (json.dumps({KEY_FIELD: key}),),
    ).fetchone()
    return row is not None
