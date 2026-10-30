"""The Ingest tab's inbox folder: which drop folder it is, its names, and their guard (RM-40).

**The inbox** (RM-40.D1) is the first usable ``[[folder]]`` in
``$BRAIN_HOME/drop-folders.toml`` with ``mode = "ingest"``, ``kind = "manual"`` and
``auto = false``: ``auto = false`` keeps the ``--configured`` LaunchAgent off it, and
``kind = "manual"`` keeps an ``auto = false`` screenshot or voice folder from becoming
browsable. It takes the folder's extensions that a browser may upload (RM-40.D3), so
images and audio, which need OCR or whisper, stay out as they do for upload.

**A request carries a name, never a path.** :func:`check_name` admits one plain file
name; :func:`resolve_inbox_file` then applies ``vault/paths.py``'s resolve-then-compare
rule against the inbox root and ``lstat``s the name, so a symlink (in or out of the
inbox), a directory, a FIFO or an iCloud-evicted file is refused. The job re-runs both
on the worker thread, and :func:`brain.drop_folder.run_one` opens with ``O_NOFOLLOW``
relative to the folder's fd, so a file swapped after the listing or after the submit
moves nothing (RM-40 AC2). ``lstat`` comes before the resolve, so a symlink loop is a
4xx and never a ``RuntimeError`` (#110 rev F1).

Reads only: nothing here moves or writes a file. The move is the job's
(``ingest_service.inbox_runner``), in the one module permitted to mutate.
"""
from __future__ import annotations

import os
import stat
import unicodedata
from dataclasses import dataclass
from itertools import islice
from pathlib import Path

from ..config import ConfigError
from ..drop_folder import SETTLE_S, SF_DATALESS, DropFolder, Lstat
from ..settings.drop_folders import CONFIG_NAME, load_drop_folders
from .errors import UiBadRequest, UiConflict, UiNotFound, UiPayloadTooLarge, UiUnsupportedMedia
from .upload_checks import upload_cap, uploadable_extensions

#: The source kind an inbox folder must have (RM-40.D1). Not the job kind, which is
#: ``ingest_service.INBOX_KIND``.
INBOX_SOURCE_KIND = "manual"
#: At most this many names are listed, newest first.
MAX_LISTED = 200
#: The listing reads at most this many directory entries, so a folder holding a huge
#: number of files cannot hold the worker for long (review round 1, L2).
MAX_SCANNED = 5000
#: ``NAME_MAX`` on APFS and ext4.
MAX_NAME_BYTES = 255


@dataclass(frozen=True)
class InboxFile:
    """One pickable file: its name and size. Never its path."""

    name: str
    size: int


@dataclass(frozen=True)
class InboxListing:
    """The listing; ``truncated`` when :data:`MAX_LISTED` or :data:`MAX_SCANNED` cut it."""

    files: tuple[InboxFile, ...]
    truncated: bool


@dataclass(frozen=True)
class CheckedFile:
    """What :func:`resolve_inbox_file` saw: the size, and the identity the job pins."""

    size: int
    identity: tuple[int, int]  # (st_dev, st_ino)


def find_inbox(brain_home: Path) -> DropFolder | None:
    """The inbox folder, or ``None``; a bad config file raises its ``ConfigError``."""
    for folder in load_drop_folders(brain_home).folders:
        if folder.mode == "ingest" and folder.kind == INBOX_SOURCE_KIND and not folder.auto:
            return folder
    return None


def require_inbox(brain_home: Path) -> DropFolder:
    """The inbox, or 404 ``inbox_unconfigured`` / 409 ``inbox_misconfigured``.

    The ``ConfigError``'s text is not passed on: the guide and ``brain doctor`` name the
    entry and key, and this answer needs only to say which of the two is wrong.
    """
    try:
        folder = find_inbox(brain_home)
    except ConfigError:
        raise UiConflict(
            f"{CONFIG_NAME} is not valid; run brain doctor", code="inbox_misconfigured"
        ) from None
    if folder is None:
        raise UiNotFound("no inbox folder is configured", code="inbox_unconfigured")
    return folder


def inbox_extensions(folder: DropFolder) -> frozenset[str]:
    """The folder's extensions that a browser may upload (RM-40.D3)."""
    return folder.extensions & frozenset(uploadable_extensions())


def check_name(raw: object, extensions: frozenset[str]) -> str:
    """``raw`` as one plain file name the inbox takes; 400 ``invalid_name`` or 415."""
    if not isinstance(raw, str) or not _plain_name(raw):
        raise UiBadRequest("name must be one file name in the inbox", code="invalid_name")
    if Path(raw).suffix.lower() not in extensions:
        allowed = ", ".join(sorted(extensions)) or "none"
        raise UiUnsupportedMedia(
            f"the inbox takes only these file types: {allowed}", code="unsupported_file"
        )
    return raw


def _plain_name(name: str) -> bool:
    """No separator, NUL, leading dot (``.``/``..``/hidden) or control/format character,
    strict UTF-8, at most :data:`MAX_NAME_BYTES` bytes."""
    if not name or name.startswith(".") or "/" in name:
        return False
    # NUL, ESC, RLO, and a lone surrogate (category Cs: valid JSON, never a real name),
    # so the encode below cannot fail.
    if any(unicodedata.category(ch).startswith("C") for ch in name):
        return False
    return len(name.encode("utf-8")) <= MAX_NAME_BYTES


def resolve_inbox_file(
    folder: DropFolder, name: str, *, lstat: Lstat = os.lstat
) -> CheckedFile:
    """``<inbox>/<name>``'s size and identity, once it is shown to be a regular file in it.

    ``name`` has passed :func:`check_name`. ``lstat`` first, which never follows a
    symlink, so a link (a loop among them: ``Path.resolve`` raises ``RuntimeError`` on
    one on 3.11) is refused before anything is resolved (#110 rev F1). Then
    resolve-then-compare (``vault/paths.py``): both sides resolved, the candidate's
    parent must be the root itself, which catches a link swapped in between the two.
    Any other ``OSError`` (an inbox this process may not search: TCC's ``EPERM``) is
    409 ``inbox_unreadable``, never a 500. A file with a second hard link is refused
    (400 ``hard_linked_file``): that name could be a file from outside the inbox
    (defence in depth; review round 1, L4). The job pins the identity returned here.
    """
    try:
        root = folder.path.resolve(strict=True)
    except (RuntimeError, OSError):  # removed, or swapped for a link loop, since loaded
        raise _unreadable() from None
    candidate = root / name
    try:
        st = lstat(candidate)
    except FileNotFoundError:
        raise UiNotFound("no such file in the inbox", code="inbox_file_unknown") from None
    except OSError:
        raise _unreadable() from None
    if not stat.S_ISREG(st.st_mode):
        raise _not_regular()
    if st.st_nlink != 1:
        raise UiBadRequest(
            "the file has another hard link; copy it into the inbox instead",
            code="hard_linked_file",
        )
    try:
        parent = candidate.resolve().parent
    except (RuntimeError, OSError):  # swapped for a link loop after the lstat
        raise _not_regular() from None
    if parent != root:
        raise UiBadRequest("the name points outside the inbox", code="outside_inbox")
    if getattr(st, "st_flags", 0) & SF_DATALESS:
        raise UiConflict("the file is in iCloud, not on this disk; download it first",
                         code="file_not_on_disk")
    cap = upload_cap(Path(name).suffix.lower())
    if st.st_size > cap:
        raise UiPayloadTooLarge(f"the file is over {cap} bytes", code="inbox_file_too_large")
    return CheckedFile(size=int(st.st_size), identity=(int(st.st_dev), int(st.st_ino)))


def _not_regular() -> UiBadRequest:
    return UiBadRequest("not a regular file (a link, folder or pipe)", code="not_a_regular_file")


def _unreadable() -> UiConflict:
    return UiConflict("the inbox folder could not be read", code="inbox_unreadable")


def list_inbox(folder: DropFolder, *, now: float, lstat: Lstat = os.lstat) -> InboxListing:
    """The pickable files, newest first, capped at :data:`MAX_LISTED`.

    The same rule as RM-33's :func:`~brain.drop_folder.scan`, by ``lstat`` (no symlink,
    dot-file, sub-directory or dataless file; a file modified in the last ``SETTLE_S``
    seconds is still being written), with the inbox's extensions, plus the request
    rules: a name :func:`check_name` would refuse, or a file with another hard link, is
    never listed. Its own loop rather than ``scan``, so it can stop after
    :data:`MAX_SCANNED` entries without changing the LaunchAgent's scan. ``OSError``
    from the folder itself propagates.
    """
    takes = inbox_extensions(folder)
    until = now - SETTLE_S
    found: list[tuple[float, str, int]] = []
    with os.scandir(folder.path) as entries:
        batch = list(islice(entries, MAX_SCANNED + 1))
    for entry in batch[:MAX_SCANNED]:
        name = entry.name
        if not _plain_name(name) or Path(name).suffix.lower() not in takes:
            continue
        try:
            st = lstat(folder.path / name)
        except FileNotFoundError:
            continue
        if not _pickable(st) or st.st_mtime > until:
            continue
        found.append((st.st_mtime, name, int(st.st_size)))
    found.sort(reverse=True)
    files = tuple(InboxFile(name, size) for _, name, size in found[:MAX_LISTED])
    return InboxListing(files, truncated=len(batch) > MAX_SCANNED or len(found) > MAX_LISTED)


def _pickable(st: os.stat_result) -> bool:
    """A regular file with one link, on disk: what :func:`resolve_inbox_file` admits."""
    return (
        stat.S_ISREG(st.st_mode)
        and st.st_nlink == 1
        and not getattr(st, "st_flags", 0) & SF_DATALESS
    )
