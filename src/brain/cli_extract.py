"""`brain ingest`'s extraction step, refusing an unreadable file in one line (RM-33 review).

``extract_path`` raises ``OSError`` for a file it cannot open, ``ELOOP`` among them since
the image extractor opens with ``O_NOFOLLOW`` (a symlinked ``.png``), and an
:class:`~brain.ingest.image.ImageError` for an image it cannot read. Both used to escape
``brain ingest`` as a traceback. Here they are one red line and exit 1: the ``OSError`` by
class (:func:`~brain.errors.describe_failure`, never its message, which holds the path),
an ``ImageError`` by its fixed, content-free message. A leaf of its own because
``cli_ingest.py`` is over the 800-line ceiling.
"""
from __future__ import annotations

from pathlib import Path

import typer

from .errors import describe_failure
from .ingest import ExtractedDoc
from .ingest.dispatch import extract_path
from .ingest.image import ImageError
from .ingest.text_safety import one_line


def extract_or_exit(path: Path) -> ExtractedDoc:
    """``extract_path(path)``, or a one-line refusal and exit 1 (module docstring)."""
    try:
        return extract_path(path)
    except ImageError as exc:
        reason = f"{type(exc).__name__}: {exc}"
    except OSError as exc:
        reason = describe_failure(exc)
    typer.secho(f"cannot read {one_line(path.name)}: {reason}", fg="red", err=True)
    raise typer.Exit(code=1)
