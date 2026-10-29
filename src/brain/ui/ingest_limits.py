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
