"""Which tracked files `brain ingest-code` keeps, how each is read, and its document (RM-43).

Pure selection and reading: no ``git`` (that is :mod:`.code_repo`), no database.
Every exclusion the ticket names lives here, each with a named reason the CLI
counts:

- by path: a vendored or build directory (:data:`VENDORED_DIRS`), a lockfile
  (:data:`LOCKFILE_NAMES` or ``*.lock``), a minified name (``*.min.*``, ``*.map``),
  or an extension outside ``--ext``;
- by reading: not a regular file (a symlink, a submodule, gone from the work
  tree), a real location outside the work tree, over :data:`MAX_FILE_BYTES`,
  binary (a NUL byte, or not UTF-8), minified by its lines, or empty.

The file is opened with ``O_NOFOLLOW`` and checked with ``fstat`` after the
open, so a symlink swapped in after the listing is not followed, and the read is
capped at the size limit plus one byte. The 200 KB cap also keeps every file
well clear of tiktoken's backtracking ``ValueError`` on very long whitespace
runs, which ``count_tokens`` would otherwise raise.
"""
from __future__ import annotations

import os
import stat
from collections import Counter
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

from . import ExtractedDoc
from .chunk_dispatch import CODE_CONTENT_TYPE
from .code_chunker import language_of

#: Larger files are skipped (the ticket's 200 KB).
MAX_FILE_BYTES = 200 * 1024
#: A directory with one of these names, anywhere in a path, is vendored or built.
VENDORED_DIRS = frozenset({"node_modules", ".venv", "vendor", "dist", "build", "third_party"})
#: Lockfiles by name (casefolded); any ``*.lock`` is one too.
LOCKFILE_NAMES = frozenset(
    {
        "package-lock.json", "npm-shrinkwrap.json", "yarn.lock", "pnpm-lock.yaml",
        "poetry.lock", "pipfile.lock", "uv.lock", "cargo.lock", "gemfile.lock",
        "composer.lock", "go.sum", "flake.lock", "mix.lock", "podfile.lock",
        "packages.lock.json", "bun.lockb",
    }
)
#: Minified by its lines: the longest is over this, AND the mean is over the next.
MINIFIED_LONGEST_LINE = 1000
MINIFIED_MEAN_LINE = 200

SKIP_VENDORED = "vendored"
SKIP_LOCKFILE = "lockfile"
SKIP_MINIFIED = "minified"
SKIP_EXTENSION = "extension"
SKIP_NOT_REGULAR = "not_regular"
SKIP_OUTSIDE = "outside"
SKIP_TOO_LARGE = "too_large"
SKIP_BINARY = "binary"
SKIP_EMPTY = "empty"

#: The report's wording per reason, in the order it prints them.
SKIP_TEXT: dict[str, str] = {
    SKIP_VENDORED: "in a vendored or build directory",
    SKIP_LOCKFILE: "lockfile(s)",
    SKIP_MINIFIED: "minified file(s)",
    SKIP_EXTENSION: "outside --ext",
    SKIP_NOT_REGULAR: "not a regular file (a symlink, a submodule, or missing)",
    SKIP_OUTSIDE: "resolving outside the work tree",
    SKIP_TOO_LARGE: f"over {MAX_FILE_BYTES // 1024} KB",
    SKIP_BINARY: "binary or not UTF-8",
    SKIP_EMPTY: "empty",
}


@dataclass(frozen=True)
class CodeFile:
    """One file that passed every check: its repo-relative POSIX path and its text."""

    path: str
    text: str


def normalize_exts(exts: Iterable[str]) -> frozenset[str] | None:
    """``--ext`` values as bare lowercase suffixes (``.PY`` → ``py``); ``None`` when none given.

    Raises ``ValueError`` on a blank value or one containing a path separator.
    """
    normalized: set[str] = set()
    for raw in exts:
        ext = raw.strip().lstrip(".").casefold()
        if not ext or "/" in ext:
            raise ValueError("--ext takes a file extension such as py or .ts")
        normalized.add(ext)
    return frozenset(normalized) or None


def path_skip_reason(path: str, exts: frozenset[str] | None) -> str | None:
    """Why ``path`` is excluded before it is read, or ``None`` to read it."""
    parts = PurePosixPath(path).parts
    if any(part.casefold() in VENDORED_DIRS for part in parts[:-1]):
        return SKIP_VENDORED
    name = parts[-1].casefold()
    if name in LOCKFILE_NAMES or name.endswith(".lock"):
        return SKIP_LOCKFILE
    if ".min." in name or name.endswith(".map"):
        return SKIP_MINIFIED
    if exts is not None and PurePosixPath(name).suffix.lstrip(".") not in exts:
        return SKIP_EXTENSION
    return None


def select_candidates(
    paths: Iterable[str], *, exts: frozenset[str] | None
) -> tuple[list[str], Counter[str]]:
    """The paths to read, sorted, and the count of each path-level skip reason."""
    keep: list[str] = []
    skipped: Counter[str] = Counter()
    for path in sorted(paths):
        reason = path_skip_reason(path, exts)
        if reason is None:
            keep.append(path)
        else:
            skipped[reason] += 1
    return keep, skipped


def read_code_file(root: Path, path: str, *, within: Path | None = None) -> CodeFile | str:
    """Read ``root / path`` as code, or return the skip reason.

    ``root`` is the work tree's resolved top level. ``within`` (default ``root``)
    is the resolved directory the owner named; a file that does not resolve
    inside it is :data:`SKIP_OUTSIDE`, whatever git reported (sec-113).
    """
    target = root / path
    bound = root if within is None else within
    try:
        if not stat.S_ISREG(target.lstat().st_mode):
            return SKIP_NOT_REGULAR
        if not target.resolve(strict=True).is_relative_to(bound):
            return SKIP_OUTSIDE  # a symlinked directory, or a path outside the named one
        data = _read_capped(target)
    except OSError:  # gone, a dangling or looping link, or swapped for a symlink
        return SKIP_NOT_REGULAR
    return data if isinstance(data, str) else _classify(path, data)


def _read_capped(target: Path) -> bytes | str:
    """The file's bytes, or a skip reason: not regular once open, or over the size cap.

    The size is checked twice: by ``fstat`` before reading, and by the read itself
    (capped at one byte past the limit), since a file can grow between the two.
    """
    fd = os.open(target, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(fd, "rb") as handle:
        info = os.fstat(handle.fileno())
        if not stat.S_ISREG(info.st_mode):
            return SKIP_NOT_REGULAR
        if info.st_size > MAX_FILE_BYTES:
            return SKIP_TOO_LARGE
        data = handle.read(MAX_FILE_BYTES + 1)
    return SKIP_TOO_LARGE if len(data) > MAX_FILE_BYTES else data


def _classify(path: str, data: bytes) -> CodeFile | str:
    if b"\0" in data:
        return SKIP_BINARY
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError:
        return SKIP_BINARY
    text = text.removeprefix("﻿")
    if not text.strip():
        return SKIP_EMPTY
    if is_minified(text):
        return SKIP_MINIFIED
    return CodeFile(path=path, text=text)


def is_minified(text: str) -> bool:
    """A longest line over :data:`MINIFIED_LONGEST_LINE` AND a mean over :data:`MINIFIED_MEAN_LINE`.

    Both, so one long string literal in an ordinary file does not drop it.
    """
    lines = text.splitlines()
    longest = max(len(line) for line in lines)
    return longest > MINIFIED_LONGEST_LINE and len(text) / len(lines) > MINIFIED_MEAN_LINE


def build_code_doc(*, repo: str, root: Path, file: CodeFile) -> ExtractedDoc:
    """The document for one file: titled ``<repo>/<path>``, keyed by its absolute path.

    ``metadata`` carries ``path`` and ``language``, the hints the code chunker
    reads (``chunk_dispatch.chunk_for``), and ``repo``.
    """
    metadata: dict[str, str] = {"path": file.path, "repo": repo}
    language = language_of(file.path, None)
    if language is not None:
        metadata["language"] = language
    return ExtractedDoc(
        title=f"{repo}/{file.path}",
        content=file.text,
        content_type=CODE_CONTENT_TYPE,
        source_path=str(root / file.path),
        metadata=metadata,
    )
