"""The Ingest tab's fixed limits (Ingest spec §4.5, §5.1, §7.3).

Module constants, **not config knobs, in v1**. They can be promoted to a feature
settings leaf (``brain/settings/``, RM-08) if a real need appears; never to
``config.py``, which is over the file-size ceiling.
"""
from __future__ import annotations

from datetime import timedelta

#: Jobs that may WAIT behind the one running job. A fifth submit gets 429.
#: A queue is for "drop three files", not for bulk import (``brain ingest-dir``).
MAX_QUEUED_JOBS = 4

#: Terminal jobs kept for polling: at most this many, for at most this long.
RETAIN_JOBS = 50
RETAIN_FOR = timedelta(hours=1)

#: Texts per embed call in the progress embedder. Small enough that a UI
#: search's embed can interleave at the Ollama runner between batches.
EMBED_BATCH_SIZE = 16

#: The paste body cap, in UTF-8 bytes of the decoded ``body`` string. This is
#: the limit the user is told about.
MAX_PASTE_BODY_BYTES = 2 * 1024 * 1024

#: The paste cap ON THE WIRE. JSON escaping can grow a body up to 6x (one
#: control character becomes ``\u00XX``), so a wire cap equal to the body cap
#: would refuse legitimate pastes heavy in newlines or quotes. 4 KiB covers the
#: other fields.
MAX_PASTE_WIRE_BYTES = 6 * MAX_PASTE_BODY_BYTES + 4 * 1024

#: The content types a paste may carry. ``krisp_action_items`` is excluded on
#: purpose: it needs a parent meeting id.
PASTE_CONTENT_TYPES: tuple[str, ...] = ("note", "transcript")
DEFAULT_PASTE_CONTENT_TYPE = "note"

#: How long shutdown waits for the running job before naming it as unknown.
STOP_TIMEOUT_S = 10.0

# --- File upload (RM-13, Ingest spec §7.3, §7.4) --------------------------------

#: The extensions a browser may upload. An EXPLICIT constant, not
#: ``ingest.dispatch.supported_extensions()``: once RM-33 registers image
#: extractors, the registry would admit images. A test asserts this is a subset
#: of the registry, and ``upload_checks.uploadable_extensions`` intersects at
#: runtime too. Adding a type here is a deliberate, reviewed act.
UPLOAD_EXTENSIONS: frozenset[str] = frozenset({".md", ".markdown", ".txt", ".pdf", ".docx"})

#: Per-file caps on the wire: a binary document, and a text or markdown file.
MAX_BINARY_UPLOAD_BYTES = 25 * 1024 * 1024
MAX_TEXT_UPLOAD_BYTES = 5 * 1024 * 1024
BINARY_UPLOAD_EXTENSIONS: frozenset[str] = frozenset({".pdf", ".docx"})

#: Bytes staged on disk across every upload not yet finished (unsubmitted, or
#: claimed by a job that has not settled). Over it: 429 ``staging_full``.
MAX_STAGED_BYTES = 64 * 1024 * 1024

#: ``POST /api/ingest/file`` carries a few short fields; its JSON is capped on the
#: wire like the paste's.
MAX_FILE_SUBMIT_WIRE_BYTES = 16 * 1024

#: Uploads held at once (unsubmitted, claimed by an unsettled job, or still
#: streaming). Over it: 429 ``too_many_uploads``. The byte budget alone would let
#: thousands of tiny files through, each costing a directory, a file and an inode.
MAX_STAGED_UPLOADS = 16

#: Every staged upload is charged against :data:`MAX_STAGED_BYTES` in whole
#: filesystem blocks, at least one: a zero-byte file still occupies a block and
#: an inode on disk.
STAGING_BLOCK_BYTES = 4096

#: An upload nobody submits is deleted after this long.
UPLOAD_TTL = timedelta(minutes=15)

#: The sanitized basename (stem plus extension) is at most this long, in
#: characters AND in UTF-8 bytes. The byte cap is what the filesystem enforces:
#: NAME_MAX is 255 BYTES on Linux and macOS, so 120 multibyte characters could
#: still fail ``open`` with ENAMETOOLONG. 200 leaves room for the extension.
MAX_FILENAME_CHARS = 120
MAX_FILENAME_BYTES = 200

#: A ``.docx`` is a zip. Checked from the central directory before extraction:
#: the members' declared total, the worst member's expansion ratio, and the
#: member count (a central directory of millions of entries is itself a bomb).
DOCX_MAX_EXPANDED_BYTES = 200 * 1024 * 1024
DOCX_MAX_RATIO = 100
DOCX_MAX_MEMBERS = 10_000

#: The central directory's declared size, read from the end record before the
#: zip is opened. zipfile reads exactly this many bytes of central directory and
#: parses every entry in them, WHATEVER the end record's entry count says, so a
#: count check alone can be bypassed by under-reporting it (sec-77 re-review: a
#: forged count of 1 still parsed 270,001 entries, ~156 MiB). Each entry is 46
#: bytes plus its name: 4 MiB fits DOCX_MAX_MEMBERS entries with ~370-byte names.
DOCX_MAX_CENTRAL_DIRECTORY_BYTES = 4 * 1024 * 1024

