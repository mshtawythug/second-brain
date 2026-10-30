"""Synthetic Notion "Markdown & CSV" exports for the RM-35 tests.

Every name, id and body below is invented. An export is a mapping of
POSIX relative path to content (``str`` is UTF-8 encoded, ``bytes`` is written
as is); :func:`write_tree` lays it out under a folder and :func:`write_zip`
packs it into an archive, so one layout serves both inputs.
"""
from __future__ import annotations

import io
import struct
import zipfile
from collections.abc import Mapping
from pathlib import Path

HANDBOOK_ID = "0a1b2c3d4e5f60718293a4b5c6d7e8f9"
ONBOARDING_ID = "1b2c3d4e5f60718293a4b5c6d7e8f90a"
READING_DB_ID = "2c3d4e5f60718293a4b5c6d7e8f90a1b"
BOOK_ONE_ID = "3d4e5f60718293a4b5c6d7e8f90a1b2c"
BOOK_TWO_ID = "4e5f60718293a4b5c6d7e8f90a1b2c3d"
LEDGER_ID = "5f60718293a4b5c6d7e8f90a1b2c3d4e"

HANDBOOK_TITLE = "Team Handbook"
ONBOARDING_TITLE = "Onboarding Notes"
LEDGER_TITLE = "Synthetic Ledger"

HANDBOOK_MD = (
    f"# {HANDBOOK_TITLE}\n\n"
    "Welcome to the synthetic handbook. The meridian protocol is described below.\n\n"
    f"[Onboarding Notes]({HANDBOOK_TITLE}%20{HANDBOOK_ID}/"
    f"{ONBOARDING_TITLE}%20{ONBOARDING_ID}.md)\n"
)
ONBOARDING_MD = (
    f"# {ONBOARDING_TITLE}\n\n"
    "Day one covers the lantern checklist.\n\n"
    f"![diagram]({ONBOARDING_TITLE}%20{ONBOARDING_ID}/diagram.png)\n"
)
BOOK_ONE_MD = (
    "# Book One\n\n"
    "Created: October 1, 2026 3:04 PM\n"
    "Last edited time: October 20, 2026 4:12 PM\n"
    "Status: Reading\n\n"
    "Notes on the first synthetic book.\n"
)
BOOK_TWO_MD = (
    "# Book Two\n\n"
    "Created: September 2, 2026 9:00 AM\n"
    "Last edited time: September 5, 2026 10:30 AM\n"
    "Status: Done\n\n"
    "Notes on the second synthetic book.\n"
)
READING_CSV = (
    "Name,Status,Created\n"
    'Book One,Reading,"October 1, 2026 3:04 PM"\n'
    'Book Two,Done,"September 2, 2026 9:00 AM"\n'
)
PNG_BYTES = b"\x89PNG\r\n\x1a\nsynthetic"

HANDBOOK_PATH = f"{HANDBOOK_TITLE} {HANDBOOK_ID}.md"
ONBOARDING_PATH = f"{HANDBOOK_TITLE} {HANDBOOK_ID}/{ONBOARDING_TITLE} {ONBOARDING_ID}.md"
IMAGE_PATH = f"{HANDBOOK_TITLE} {HANDBOOK_ID}/{ONBOARDING_TITLE} {ONBOARDING_ID}/diagram.png"
READING_DIR = f"{HANDBOOK_TITLE} {HANDBOOK_ID}/Reading List {READING_DB_ID}"
READING_CSV_PATH = f"{READING_DIR}.csv"
READING_ALL_CSV_PATH = f"{READING_DIR}_all.csv"
BOOK_ONE_PATH = f"{READING_DIR}/Book One {BOOK_ONE_ID}.md"
BOOK_TWO_PATH = f"{READING_DIR}/Book Two {BOOK_TWO_ID}.md"
NO_ID_PATH = "Loose Note.md"

Export = Mapping[str, str | bytes]


def standard_export() -> dict[str, str | bytes]:
    """Two nested pages, a database with two row pages and both CSVs, an image, a no-id page."""
    return {
        HANDBOOK_PATH: HANDBOOK_MD,
        ONBOARDING_PATH: ONBOARDING_MD,
        IMAGE_PATH: PNG_BYTES,
        READING_CSV_PATH: READING_CSV,
        READING_ALL_CSV_PATH: READING_CSV,
        BOOK_ONE_PATH: BOOK_ONE_MD,
        BOOK_TWO_PATH: BOOK_TWO_MD,
        NO_ID_PATH: "# Loose Note\n\nNo id in this file name.\n",
    }


def _bytes(content: str | bytes) -> bytes:
    return content.encode("utf-8") if isinstance(content, str) else content


def write_tree(root: Path, files: Export) -> Path:
    """Write ``files`` under ``root`` (created) and return ``root``."""
    for rel, content in files.items():
        target = root / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(_bytes(content))
    root.mkdir(parents=True, exist_ok=True)
    return root


def write_zip(path: Path, files: Export, *, prefix: str = "") -> Path:
    """Pack ``files`` into a deflated zip at ``path``, each name under ``prefix``."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(path, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        for rel, content in files.items():
            zf.writestr(f"{prefix}{rel}", _bytes(content))
    return path


def old_zipfile_bypass(count: int) -> bytes:
    """sec-104's shape against a ``zipfile`` without the CVE-2025-8291 fix (synthetic).

    A ZIP64 record at the locator's offset claims a one-entry directory (the
    last header), zero padding sits just before the locator, and that last
    header's comment swallows the record, the padding and the locator. A fixed
    ``zipfile`` reads the record at the locator's offset (one entry); an unfixed
    one reads the padding, finds no record, keeps the plain end record and
    parses all ``count`` entries.
    """
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        for i in range(count):
            zf.writestr(f"f{i}.md", b"x")
    data = buf.getvalue()
    eocd_at = data.rfind(b"PK\x05\x06")
    cd_offset = struct.unpack_from("<L", data, eocd_at + 16)[0]
    directory = bytearray(data[cd_offset:eocd_at])
    pos = last = 0
    while pos < len(directory):
        last = pos
        name, extra, comment = struct.unpack_from("<3H", directory, pos + 28)
        pos += 46 + name + extra + comment
    last_at = cd_offset + last
    padding = b"\x00" * 56
    reloff = eocd_at  # the small record starts where the real directory ended
    record = struct.pack(
        "<4sQ2H2L4Q", b"PK\x06\x06", 44 + len(padding), 45, 45, 0, 0, 1, 1,
        reloff - last_at, last_at,
    )
    locator = struct.pack("<4sLQL", b"PK\x06\x07", 0, reloff, 1)
    swallowed = record + padding + locator
    struct.pack_into("<H", directory, last + 32, comment + len(swallowed))
    new_directory = bytes(directory) + swallowed
    eocd = struct.pack(
        "<4s4H2LH", b"PK\x05\x06", 0, 0, count, count, len(new_directory), cd_offset, 0
    )
    return data[:cd_offset] + new_directory + eocd
