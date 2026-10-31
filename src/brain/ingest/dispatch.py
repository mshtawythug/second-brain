"""File-extractor registry: which extractor handles which file extension.

Moved out of ``brain.ingest`` (roadmap ticket RM-07) so that a new extractor
registers here instead of growing ``ingest/__init__.py``, which is over the
800-line ceiling. ``brain.ingest`` re-exports :func:`extract_path` and
:func:`supported_extensions`, so ``from brain.ingest import extract_path`` keeps
working.

**Adding an extractor.** Write ``ingest/<kind>.py`` with a
``(path: Path) -> ExtractedDoc`` function, then add an :class:`ExtractorSpec`
for it to :data:`BUILTIN_EXTRACTORS` below. That is the one way to add one.
:func:`register_extractor` is for code imported before dispatch runs (tests,
extension modules loaded at startup), never for the extractor module itself:
``load`` imports that module only after dispatch has found its spec. Register
at import time only; the registry is a plain dict that dispatch iterates. Two
fields keep the base CLI light and ``brain ingest-dir`` stable:

- ``load`` and ``wrap_errors`` are **loaders**, called only when a file of that
  type is extracted. A parser's exception types exist only once the parser is
  imported, so a tuple of them built at registration would import ``pypdf`` and
  ``docx`` on every ``brain`` invocation (about 0.4 s together). This is the one
  place the code departs from the ticket's sketch, which typed ``wrap_errors``
  as a tuple; ``tests/test_import_hygiene.py`` pins the laziness.
- ``in_directory_walks=False`` keeps an extractor out of ``brain ingest-dir``'s
  default walk (it then runs only for an explicit ``--ext`` or its own command),
  so adding, say, an image extractor does not silently change what an existing
  ``ingest-dir`` run picks up.
"""
from __future__ import annotations

import re
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, TypeAlias

from ..sensitivity import CONFIDENTIAL, DEFAULT_SENSITIVITY

if TYPE_CHECKING:
    from . import ExtractedDoc

ExtractorFn: TypeAlias = "Callable[[Path], ExtractedDoc]"

#: What ``Path.suffix`` can return and the registry can match: ``.md``, ``.m4a``.
_EXTENSION_RE = re.compile(r"\.[a-z0-9]+")


def _no_wrapped_errors() -> tuple[type[Exception], ...]:
    return ()


@dataclass(frozen=True)
class ExtractorSpec:
    """One extractor and the file extensions it handles."""

    #: Short label; a wrapped parser error reads ``malformed <NAME>: ...``.
    name: str
    #: Lowercase, each with its leading dot: ``(".md", ".markdown")``.
    extensions: tuple[str, ...]
    #: Returns the extractor; imports it lazily, so the base CLI stays light.
    load: Callable[[], ExtractorFn]
    #: Returns the parser errors re-raised as ``ValueError``; also lazy.
    wrap_errors: Callable[[], tuple[type[Exception], ...]] = _no_wrapped_errors
    #: False: only an explicit ``--ext`` or the extractor's own command uses it.
    in_directory_walks: bool = True
    #: The tier a file of this type gets when the caller names none (RM-33: a
    #: screenshot can show anything, so images are ``confidential``).
    default_sensitivity: str = DEFAULT_SENSITIVITY


def _validate_extensions(spec: ExtractorSpec, taken: dict[str, ExtractorSpec]) -> None:
    if not spec.extensions:
        raise ValueError(f"extractor {spec.name!r} declares no extensions")
    seen: set[str] = set()
    for ext in spec.extensions:
        # One plain suffix only: ``spec_for`` matches on ``Path.suffix``, so an
        # extension such as ``.tar.gz`` or ``.a/b`` would register and never fire.
        if _EXTENSION_RE.fullmatch(ext) is None:
            raise ValueError(
                f"extractor {spec.name!r}: extension {ext!r} must be one lowercase "
                f"suffix of ASCII letters and digits with a leading dot, like '.md'"
            )
        if ext in seen:
            raise ValueError(f"extractor {spec.name!r} lists {ext} twice")
        if ext in taken:
            raise ValueError(
                f"extension {ext} is already handled by extractor {taken[ext].name!r}"
            )
        seen.add(ext)


class ExtractorRegistry:
    """Extension → :class:`ExtractorSpec`, in registration order."""

    def __init__(self, specs: Iterable[ExtractorSpec] = ()) -> None:
        self._by_extension: dict[str, ExtractorSpec] = {}
        for spec in specs:
            self.register(spec)

    def register(self, spec: ExtractorSpec) -> None:
        """Add ``spec``; ``ValueError`` on a duplicate or malformed extension.

        Validation runs before anything is stored, so a rejected spec
        registers none of its extensions.
        """
        _validate_extensions(spec, self._by_extension)
        for ext in spec.extensions:
            self._by_extension[ext] = spec

    def spec_for(self, path: Path) -> ExtractorSpec | None:
        return self._by_extension.get(Path(path).suffix.lower())

    def extract_path(self, path: Path) -> ExtractedDoc:
        """Dispatch to the correct extractor based on file extension.

        Raises ``ValueError`` for unsupported extensions or malformed input files;
        ``OSError`` for unreadable files. Backend-specific parser errors (e.g.
        :class:`pypdf.errors.PyPdfError`) are wrapped as ``ValueError`` so callers
        only need to handle a narrow set of exception types.
        """
        spec = self.spec_for(path)
        if spec is None:
            raise ValueError(f"unsupported file type: {Path(path).suffix.lower()}")
        extract = spec.load()
        wrapped = spec.wrap_errors()
        try:
            return extract(path)
        except UnicodeDecodeError as e:  # pragma: no cover - extractors use errors="replace"
            raise ValueError(f"could not decode file as UTF-8: {e}") from e
        except wrapped as e:
            raise ValueError(f"malformed {spec.name.upper()}: {e}") from e

    def supported_extensions(self, *, directory_walk: bool = False) -> list[str]:
        """Extensions :meth:`extract_path` handles, in registration order.

        ``directory_walk=True`` leaves out extractors registered with
        ``in_directory_walks=False``; ``brain ingest-dir`` asks for that list.
        """
        return [
            ext
            for ext, spec in self._by_extension.items()
            if spec.in_directory_walks or not directory_walk
        ]


def _load_text() -> ExtractorFn:
    from .text import extract_text

    return extract_text


def _load_markdown() -> ExtractorFn:
    from .markdown import extract_markdown

    return extract_markdown


def _load_pdf() -> ExtractorFn:
    from .pdf import extract_pdf

    return extract_pdf


def _pdf_errors() -> tuple[type[Exception], ...]:
    # pdfplumber, the fallback when pypdf reads no text, wraps every pdfminer failure in
    # its own PdfminerException (sec-117 F1): a hostile PDF raised it past this registry.
    from pdfplumber.utils.exceptions import PdfminerException
    from pypdf.errors import PyPdfError

    return (PyPdfError, PdfminerException)


def _load_docx() -> ExtractorFn:
    from .docx import extract_docx

    return extract_docx


def _docx_errors() -> tuple[type[Exception], ...]:
    from docx.opc.exceptions import PackageNotFoundError

    return (PackageNotFoundError,)


def _load_image() -> ExtractorFn:
    from .image import extract_image

    return extract_image


def _load_voice() -> ExtractorFn:
    from .voice import extract_voice

    return extract_voice


#: The built-in extractors, in the order ``supported_extensions()`` lists them.
#: ``image`` (RM-33) and ``voice`` (RM-38) stay out of directory walks: drop folders
#: and an explicit ``--ext`` reach them, so an existing ``brain ingest-dir`` run is
#: unchanged. Both are ``confidential`` by default: a screenshot can show anything, and
#: a voice memo is private by nature.
BUILTIN_EXTRACTORS: tuple[ExtractorSpec, ...] = (
    ExtractorSpec(name="text", extensions=(".txt",), load=_load_text),
    ExtractorSpec(name="markdown", extensions=(".md", ".markdown"), load=_load_markdown),
    ExtractorSpec(name="pdf", extensions=(".pdf",), load=_load_pdf, wrap_errors=_pdf_errors),
    ExtractorSpec(
        name="docx", extensions=(".docx",), load=_load_docx, wrap_errors=_docx_errors
    ),
    ExtractorSpec(
        name="image",
        extensions=(".png", ".jpg", ".jpeg"),
        load=_load_image,
        in_directory_walks=False,
        default_sensitivity=CONFIDENTIAL,
    ),
    ExtractorSpec(
        name="voice",
        extensions=(".m4a", ".mp3", ".wav"),
        load=_load_voice,
        in_directory_walks=False,
        default_sensitivity=CONFIDENTIAL,
    ),
)

_DEFAULT_REGISTRY = ExtractorRegistry(BUILTIN_EXTRACTORS)


def register_extractor(spec: ExtractorSpec) -> None:
    """Register ``spec`` process-wide; ``ValueError`` on a duplicate extension.

    Register at import time only, never while files are being dispatched.
    """
    _DEFAULT_REGISTRY.register(spec)


def extract_path(path: Path) -> ExtractedDoc:
    """Extract ``path`` with the extractor registered for its extension.

    The contract is :meth:`ExtractorRegistry.extract_path`'s.
    """
    return _DEFAULT_REGISTRY.extract_path(path)


def supported_extensions(*, directory_walk: bool = False) -> list[str]:
    """Return the file extensions that :func:`extract_path` can handle."""
    return _DEFAULT_REGISTRY.supported_extensions(directory_walk=directory_walk)


def default_sensitivity(path: Path) -> str:
    """The tier ``path`` gets when no ``--sensitivity`` is given: its extractor's default."""
    spec = spec_for(path)
    return spec.default_sensitivity if spec is not None else DEFAULT_SENSITIVITY


def spec_for(path: Path) -> ExtractorSpec | None:
    """The spec registered for ``path``'s extension, or ``None``."""
    return _DEFAULT_REGISTRY.spec_for(path)
