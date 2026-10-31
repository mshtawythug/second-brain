"""Parse each receipt attachment in a child process under hard limits (RM-45, sec-117 F3).

The parent side of :mod:`brain.ingest.attachment_worker`. A third party chooses the PDF
or image, and its parse can be unbounded in pure Python (fifty pages sharing one content
stream: about 1.15 s and 23 MB of RSS per decoded MB per page, measured by sec-117) or
reach a C decoder (pdfium, Pillow). So :class:`ChildExtractor` runs the worker through
:func:`brain.ingest.capped_process.run_capped`:

- its own process group, killed at the wall-clock ``timeout_s``, on an oversized answer
  and on any exception in the parent (Ctrl-C; SIGTERM and SIGHUP become ``SystemExit``
  under the CLI's ``sigterm_as_system_exit``), so ``tesseract`` grandchildren go too;
- ``RLIMIT_CPU`` = ``cpu_s`` (SIGXCPU), and ``RLIMIT_AS`` = ``address_space_bytes`` where
  the platform enforces it (Linux; macOS refuses it, so the worker's own RSS watchdog
  at ``max_rss_bytes`` is the memory bound there, and a second one on Linux);
- stdin ``/dev/null``, stderr discarded unread, a minimal environment (the OCR knobs,
  ``PATH`` for ``tesseract``, the locale);
- stdout capped at what ``max_chars`` of JSON text can take.

Any limit hit, a non-zero exit, an oversized or malformed answer is a final
``unreadable``; the worker's own statuses (``encrypted``, ``too many pages``, ``no ocr``,
``unreadable``) come back as their exceptions. The interpreter runs with ``-P`` (nothing
from the working directory on ``sys.path``) and ``-s`` (no user site packages).
"""
from __future__ import annotations

import json
import os
import resource
import sys
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from . import ExtractedDoc
from .attachment_errors import UnreadableAttachmentError, error_for_status
from .capped_process import CappedRun, run_capped

__all__ = ["WORKER_MODULE", "ChildExtractor", "WorkerLimits", "stdout_cap", "worker_env"]

WORKER_MODULE = "brain.ingest.attachment_worker"
#: Exact names passed to the worker, plus every ``BRAIN_OCR*`` knob.
_ENV_NAMES = (
    "PATH", "HOME", "TMPDIR", "LANG", "LC_ALL", "LC_CTYPE", "TESSDATA_PREFIX", "PYTHONPATH",
)
_ENV_PREFIX = "BRAIN_OCR"
#: The JSON around the text, generously.
_STDOUT_SLACK_BYTES = 4096
#: The most bytes one character of ``ensure_ascii=False`` JSON can take (``\\u00XX``).
_MAX_BYTES_PER_CHAR = 6

Runner = Callable[..., CappedRun]


@dataclass(frozen=True)
class WorkerLimits:
    """The hard limits on one attachment's child process."""

    timeout_s: float = 180.0
    cpu_s: int = 60
    address_space_bytes: int = 2 * 1024**3
    max_rss_bytes: int = 1024**3


def worker_env(environ: Mapping[str, str]) -> dict[str, str]:
    """The worker's environment: the names in ``_ENV_NAMES`` and the OCR knobs only."""
    return {
        name: value
        for name, value in environ.items()
        if name in _ENV_NAMES or name.startswith(_ENV_PREFIX)
    }


def stdout_cap(max_chars: int) -> int:
    """The most stdout a worker returning ``max_chars`` + 1 characters can need."""
    return _MAX_BYTES_PER_CHAR * (max_chars + 1) + _STDOUT_SLACK_BYTES


def _parse(stdout: bytes) -> dict[str, Any]:
    try:
        parsed = json.loads(stdout)
    except (ValueError, RecursionError) as exc:
        raise UnreadableAttachmentError("worker answer is not JSON") from exc
    if not isinstance(parsed, dict):
        raise UnreadableAttachmentError("worker answer is not an object")
    return parsed


class ChildExtractor:
    """``(path) -> ExtractedDoc``, the extraction run in a limited child process."""

    def __init__(
        self,
        *,
        max_pages: int,
        max_chars: int,
        limits: WorkerLimits | None = None,
        run: Runner = run_capped,
        executable: str = sys.executable,
        environ: Mapping[str, str] | None = None,
    ) -> None:
        self._max_pages = max_pages
        self._max_chars = max_chars
        self._limits = limits or WorkerLimits()
        self._run = run
        self._executable = executable
        self._environ = os.environ if environ is None else environ

    def argv(self, path: Path) -> list[str]:
        return [
            self._executable, "-P", "-s", "-m", WORKER_MODULE,
            str(path), str(self._max_pages), str(self._max_chars),
            str(self._limits.max_rss_bytes),
        ]

    def __call__(self, path: Path) -> ExtractedDoc:
        try:
            result = self._run(
                self.argv(path),
                timeout=self._limits.timeout_s,
                max_stdout_bytes=stdout_cap(self._max_chars),
                env=worker_env(self._environ),
                resource_limits=[
                    (resource.RLIMIT_CPU, self._limits.cpu_s),
                    (resource.RLIMIT_AS, self._limits.address_space_bytes),
                ],
            )
        except OSError as exc:
            raise UnreadableAttachmentError("the worker could not start") from exc
        if result.timed_out or result.oversized or result.returncode != 0:
            raise UnreadableAttachmentError("the worker hit a limit or failed")
        parsed = _parse(result.stdout)
        if "error" in parsed:
            raise error_for_status(parsed["error"])
        content, pages, ocr = parsed.get("content"), parsed.get("page_count"), parsed.get("ocr")
        if not isinstance(content, str):
            raise UnreadableAttachmentError("worker answer has no text")
        metadata: dict[str, Any] = {}
        if isinstance(pages, int) and not isinstance(pages, bool):
            metadata["page_count"] = pages
        if isinstance(ocr, str):
            metadata["ocr"] = {"engine": ocr}
        return ExtractedDoc(
            title="", content=content, content_type="", source_path=None, metadata=metadata
        )
