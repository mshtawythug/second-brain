"""The child process that parses one receipt attachment (RM-45, sec-117 F3).

``brain ingest-gmail --with-attachments`` never parses a third party's PDF or image in
its own process. :class:`brain.ingest.attachment_isolation.ChildExtractor` runs this module
as ``python -P -s -m brain.ingest.attachment_worker <path> <max pages> <max chars> <max
rss>`` under ``RLIMIT_CPU`` (and ``RLIMIT_AS`` where the platform enforces it), a wall-clock
timeout, its own process group (killed on timeout, Ctrl-C and SIGTERM), stdin from
``/dev/null`` and stderr discarded. Here:

1. An **RSS watchdog** thread exits the process at once when its peak resident memory
   passes ``max rss`` (macOS refuses ``RLIMIT_AS``, measured; this is the bound there).
2. **fd 1 is pointed at stderr** while the parsers run, so nothing they print can reach
   the parent: the only thing on the real stdout is the one JSON object written last.
3. A PDF is opened by ``pypdf`` first and refused when encrypted or over ``max pages``;
   then the extractor registry runs (RM-32's PDF bounds, RM-33's image bounds, OCR).
4. **Every exception** becomes a status: the class names a status, OCR unavailable is
   ``no ocr``, anything else is ``unreadable``. Only the text (cut to ``max chars`` + 1, so
   the parent sees that it was cut), the page count and the OCR engine come back.

Nothing here logs; the parent never reads stderr.
"""
from __future__ import annotations

import json
import os
import resource
import sys
import threading
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any, TextIO

from . import ExtractedDoc
from .attachment_errors import (
    EncryptedAttachmentError,
    GmailAttachmentError,
    TooManyPagesError,
    UnreadableAttachmentError,
)

__all__ = ["check_pdf", "extract_in_process", "main", "run", "start_rss_watchdog"]

#: How often the watchdog reads the peak RSS.
WATCHDOG_INTERVAL_S = 0.05
#: The exit code when the watchdog stops the process (the parent treats any non-zero
#: exit as ``unreadable``).
RSS_EXIT_CODE = 86
#: ``ru_maxrss`` is bytes on macOS and kibibytes on Linux.
_RSS_UNIT = 1 if sys.platform == "darwin" else 1024


def check_pdf(path: Path, *, max_pages: int) -> None:
    """Refuse an encrypted PDF, or one over ``max_pages``, before the extractor reads it."""
    from pypdf import PdfReader

    try:
        reader = PdfReader(str(path))
        encrypted = reader.is_encrypted
        pages = 0 if encrypted else len(reader.pages)
    except Exception as exc:  # any parser failure: a hostile file chooses which
        raise UnreadableAttachmentError("PDF could not be opened") from exc
    if encrypted:
        raise EncryptedAttachmentError("PDF is encrypted")
    if pages > max_pages:
        raise TooManyPagesError("PDF over the page cap")


def extract_in_process(path: Path, *, max_pages: int) -> ExtractedDoc:
    """The page check, then the registry. Raises whatever the parsers raise."""
    from .dispatch import extract_path

    if path.suffix == ".pdf":
        check_pdf(path, max_pages=max_pages)
    return extract_path(path)


def _int_or_none(value: object) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def run(path: Path, *, max_pages: int, max_chars: int) -> dict[str, Any]:
    """Extract ``path``: ``{"content", "page_count", "ocr"}`` or ``{"error": <status>}``."""
    from .image import OcrUnavailableError

    try:
        doc = extract_in_process(path, max_pages=max_pages)
    except GmailAttachmentError as exc:
        return {"error": exc.status}
    except OcrUnavailableError:
        return {"error": "no ocr"}
    except Exception:  # sec-117 F1: the parsers' exception types are the file's choice
        return {"error": "unreadable"}
    ocr = doc.metadata.get("ocr")
    engine = ocr.get("engine") if isinstance(ocr, dict) else None
    return {
        "content": doc.content[: max_chars + 1],
        "page_count": _int_or_none(doc.metadata.get("page_count")),
        "ocr": engine if isinstance(engine, str) else None,
    }


def _peak_rss_bytes() -> int:
    return resource.getrusage(resource.RUSAGE_SELF).ru_maxrss * _RSS_UNIT


def start_rss_watchdog(
    max_rss_bytes: int,
    *,
    interval: float = WATCHDOG_INTERVAL_S,
    peak: Callable[[], int] = _peak_rss_bytes,
    exit_process: Callable[[int], object] = os._exit,
) -> threading.Thread:
    """A daemon thread that exits the process once the peak RSS passes ``max_rss_bytes``.

    ``peak`` and ``exit_process`` are test seams; in production ``os._exit`` does not return.
    """
    def watch() -> None:
        while peak() <= max_rss_bytes:
            time.sleep(interval)
        exit_process(RSS_EXIT_CODE)

    thread = threading.Thread(target=watch, name="rss-watchdog", daemon=True)
    thread.start()
    return thread


def _parse(argv: list[str]) -> tuple[Path, int, int, int] | None:
    if len(argv) != 4:
        return None
    try:
        return Path(argv[0]), int(argv[1]), int(argv[2]), int(argv[3])
    except ValueError:
        return None


def main(
    argv: list[str],
    *,
    out: TextIO | None = None,
    watchdog: Callable[[int], object] = start_rss_watchdog,
) -> int:
    """Run one extraction and write its JSON result; 2 on bad arguments.

    Without ``out`` (the real child), the result goes to the original stdout and fd 1
    is pointed at stderr first. Tests pass ``out`` and ``watchdog``, so no watchdog runs
    inside the test process.
    """
    parsed = _parse(argv)
    if parsed is None:
        return 2
    path, max_pages, max_chars, max_rss = parsed
    watchdog(max_rss)
    if out is None:  # pragma: no cover — the real child; tests pass ``out``
        out = os.fdopen(os.dup(1), "w", encoding="utf-8")
        os.dup2(2, 1)
    json.dump(run(path, max_pages=max_pages, max_chars=max_chars), out, ensure_ascii=False)
    out.flush()
    return 0


if __name__ == "__main__":  # pragma: no cover — the child process entry point
    raise SystemExit(main(sys.argv[1:]))
