"""`brain ingest`'s extraction step, refusing a file it cannot take in one line (RM-33 review).

``extract_path`` raises ``OSError`` for a file it cannot open, ``ELOOP`` among them since
the image extractor opens with ``O_NOFOLLOW`` (a symlinked ``.png``), and an
:class:`~brain.ingest.image.ImageError` for an image it cannot read (and, since RM-38, a
:class:`~brain.ingest.voice.VoiceError` for a memo). Both used to escape
``brain ingest`` as a traceback. Here they are one red line and exit 1: the ``OSError`` by
class (:func:`~brain.errors.describe_failure`, never its message, which holds the path),
an ``ImageError`` or ``VoiceError`` by its fixed, content-free message. A leaf of its own because
``cli_ingest.py`` is over the 800-line ceiling.

0.4.0 QA F10 added two more. A file type no extractor reads is a usage error: exit 2, the
list of types ``brain ingest`` does read, and a pointer at ``brain ingest-code`` for source
code. The check is on the suffix, before extracting. A ``ValueError`` catch could not tell
it apart from a malformed PDF or DOCX, which the registry also raises as ``ValueError``.
That malformed file is now the same class-only ``cannot read`` line as an unreadable one,
with exit 1. The parser's message is not printed, because it can quote the file's bytes.
"""
from __future__ import annotations

from pathlib import Path

import typer

from .errors import describe_failure
from .ingest import ExtractedDoc
from .ingest.dispatch import extract_path, spec_for, supported_extensions
from .ingest.image import ImageError
from .ingest.text_safety import one_line
from .ingest.voice import VoiceError


def _refuse_unsupported(path: Path) -> None:
    """One line naming the readable types and ``ingest-code``, then exit 2 (QA F10)."""
    readable = ", ".join(supported_extensions())
    typer.secho(
        f"brain ingest does not read {one_line(path.suffix.lower()) or 'extensionless'} files "
        f"(it reads {readable}). For source code in a git work tree, use: "
        "brain ingest-code <repo-dir>",
        fg="red",
        err=True,
    )
    raise typer.Exit(code=2)


def extract_or_exit(path: Path) -> ExtractedDoc:
    """``extract_path(path)``, or a one-line refusal and exit 1 or 2 (module docstring)."""
    if spec_for(path) is None:
        _refuse_unsupported(path)
    try:
        return extract_path(path)
    except (ImageError, VoiceError) as exc:
        reason = f"{type(exc).__name__}: {exc}"
    except (OSError, ValueError) as exc:
        reason = describe_failure(exc)
    typer.secho(f"cannot read {one_line(path.name)}: {reason}", fg="red", err=True)
    raise typer.Exit(code=1)
