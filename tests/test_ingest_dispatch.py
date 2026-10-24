"""Tests for the file-extractor registry, :mod:`brain.ingest.dispatch` (RM-07).

The built-in table is pinned literally rather than derived from the registry: the
point of the pin is that moving the dispatcher out of ``ingest/__init__.py``
changed nothing for any existing file type, and a pin derived from the code under
test could not fail.
"""
from __future__ import annotations

from pathlib import Path

import pytest

import brain.ingest as ingest_pkg
from brain.ingest import ExtractedDoc, dispatch
from brain.ingest.dispatch import (
    BUILTIN_EXTRACTORS,
    ExtractorRegistry,
    ExtractorSpec,
)
from brain.ingest.docx import extract_docx
from brain.ingest.markdown import extract_markdown
from brain.ingest.pdf import extract_pdf
from brain.ingest.text import extract_text

# The dispatch table as it was in ``ingest/__init__.py`` before RM-07, in order.
PRE_RM07_EXTENSIONS = [".txt", ".md", ".markdown", ".pdf", ".docx"]
PRE_RM07_EXTRACTOR_FOR = {
    ".txt": ("text", extract_text),
    ".md": ("markdown", extract_markdown),
    ".markdown": ("markdown", extract_markdown),
    ".pdf": ("pdf", extract_pdf),
    ".docx": ("docx", extract_docx),
}

PROBE_EXTENSION = ".probe"
PROBE_TITLE = "probe-title"
PROBE_CONTENT_TYPE = "probe"


def _probe_extract(path: Path) -> ExtractedDoc:
    return ExtractedDoc(
        title=PROBE_TITLE,
        content=path.read_text(encoding="utf-8"),
        content_type=PROBE_CONTENT_TYPE,
        source_path=str(path),
        metadata={},
    )


def _probe_spec(*, in_directory_walks: bool = True) -> ExtractorSpec:
    return ExtractorSpec(
        name="probe",
        extensions=(PROBE_EXTENSION,),
        load=lambda: _probe_extract,
        in_directory_walks=in_directory_walks,
    )


def _fresh_registry() -> ExtractorRegistry:
    return ExtractorRegistry(BUILTIN_EXTRACTORS)


# --- No behaviour change for the built-ins --------------------------------------


def test_builtin_dispatch_table_unchanged() -> None:
    # Arrange
    registry = _fresh_registry()

    # Act
    listed = registry.supported_extensions()
    walked = registry.supported_extensions(directory_walk=True)
    chosen = {
        ext: (spec.name, spec.load())
        for ext in listed
        if (spec := registry.spec_for(Path(f"x{ext}"))) is not None
    }

    # Assert
    assert listed == PRE_RM07_EXTENSIONS
    assert walked == PRE_RM07_EXTENSIONS
    assert chosen == PRE_RM07_EXTRACTOR_FOR


def test_module_functions_match_the_pre_rm07_table() -> None:
    assert dispatch.supported_extensions() == PRE_RM07_EXTENSIONS
    assert dispatch.supported_extensions(directory_walk=True) == PRE_RM07_EXTENSIONS


def test_package_reexports_are_the_dispatch_functions() -> None:
    assert ingest_pkg.extract_path is dispatch.extract_path
    assert ingest_pkg.supported_extensions is dispatch.supported_extensions
    assert not hasattr(ingest_pkg, "_EXTRACTORS")


@pytest.mark.parametrize(
    ("fixture_name", "direct"),
    [
        ("sample.txt", extract_text),
        ("sample.md", extract_markdown),
        ("sample.pdf", extract_pdf),
        ("sample.docx", extract_docx),
    ],
)
def test_builtin_extract_matches_direct_extractor(
    fixtures_dir: Path, fixture_name: str, direct: object
) -> None:
    # Arrange
    path = fixtures_dir / fixture_name
    assert callable(direct)

    # Act
    via_dispatch = dispatch.extract_path(path)

    # Assert
    assert via_dispatch == direct(path)


def test_extension_match_is_case_insensitive(tmp_path: Path) -> None:
    path = tmp_path / "NOTE.MD"
    path.write_text("# Heading\n\nbody\n", encoding="utf-8")
    assert dispatch.extract_path(path).content_type == "markdown"


def test_unsupported_extension_raises_value_error(tmp_path: Path) -> None:
    path = tmp_path / "weird.xyz"
    path.write_text("irrelevant", encoding="utf-8")
    with pytest.raises(ValueError, match=r"^unsupported file type: \.xyz$"):
        dispatch.extract_path(path)


def test_malformed_pdf_wrapped_as_value_error(tmp_path: Path) -> None:
    bad = tmp_path / "bad.pdf"
    bad.write_bytes(b"%PDF-1.4 not really a pdf")
    with pytest.raises(ValueError, match=r"^malformed PDF: "):
        dispatch.extract_path(bad)


def test_malformed_docx_wrapped_as_value_error(tmp_path: Path) -> None:
    bad = tmp_path / "bad.docx"
    bad.write_bytes(b"not a real docx")
    with pytest.raises(ValueError, match=r"^malformed DOCX: "):
        dispatch.extract_path(bad)


# --- AC2: duplicates and malformed specs ----------------------------------------


def test_register_duplicate_extension_raises() -> None:
    # Arrange
    registry = _fresh_registry()
    clash = ExtractorSpec(name="other-md", extensions=(".md",), load=lambda: _probe_extract)

    # Act / Assert
    with pytest.raises(ValueError, match=r"\.md"):
        registry.register(clash)
    assert registry.supported_extensions() == PRE_RM07_EXTENSIONS


def test_duplicate_within_one_spec_raises() -> None:
    registry = ExtractorRegistry()
    spec = ExtractorSpec(name="twice", extensions=(".a", ".a"), load=lambda: _probe_extract)
    with pytest.raises(ValueError, match=r"\.a"):
        registry.register(spec)
    assert registry.supported_extensions() == []


def test_rejected_spec_registers_none_of_its_extensions() -> None:
    # Arrange: ``.new`` is fine, ``.txt`` clashes; neither may land.
    registry = _fresh_registry()
    spec = ExtractorSpec(name="half", extensions=(".new", ".txt"), load=lambda: _probe_extract)

    # Act
    with pytest.raises(ValueError):
        registry.register(spec)

    # Assert
    assert ".new" not in registry.supported_extensions()


@pytest.mark.parametrize("bad", ["md", ".MD", ".", "", ". md"])
def test_register_rejects_malformed_extension(bad: str) -> None:
    registry = ExtractorRegistry()
    spec = ExtractorSpec(name="bad", extensions=(bad,), load=lambda: _probe_extract)
    with pytest.raises(ValueError, match="extension"):
        registry.register(spec)


def test_register_rejects_empty_extensions() -> None:
    registry = ExtractorRegistry()
    spec = ExtractorSpec(name="none", extensions=(), load=lambda: _probe_extract)
    with pytest.raises(ValueError, match="no extensions"):
        registry.register(spec)


# --- AC3: a runtime extractor is dispatched, listed, and walk-gated -------------


def test_registered_extractor_is_dispatched_and_listed(tmp_path: Path) -> None:
    # Arrange
    registry = _fresh_registry()
    registry.register(_probe_spec())
    path = tmp_path / f"a{PROBE_EXTENSION}"
    path.write_text("probe body", encoding="utf-8")

    # Act
    doc = registry.extract_path(path)

    # Assert
    assert (doc.title, doc.content, doc.content_type) == (
        PROBE_TITLE,
        "probe body",
        PROBE_CONTENT_TYPE,
    )
    assert registry.supported_extensions() == [*PRE_RM07_EXTENSIONS, PROBE_EXTENSION]
    assert PROBE_EXTENSION in registry.supported_extensions(directory_walk=True)


def test_non_walk_extractor_absent_from_directory_walk() -> None:
    # Arrange
    registry = _fresh_registry()

    # Act
    registry.register(_probe_spec(in_directory_walks=False))

    # Assert
    assert PROBE_EXTENSION in registry.supported_extensions()
    assert registry.supported_extensions(directory_walk=True) == PRE_RM07_EXTENSIONS


def test_module_functions_use_the_default_registry(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Arrange: a fresh default registry, restored by monkeypatch, so the
    # process-wide one is never mutated by this test.
    monkeypatch.setattr(dispatch, "_DEFAULT_REGISTRY", _fresh_registry())
    path = tmp_path / f"b{PROBE_EXTENSION}"
    path.write_text("via module", encoding="utf-8")

    # Act
    dispatch.register_extractor(_probe_spec(in_directory_walks=False))

    # Assert
    assert ingest_pkg.extract_path(path).content == "via module"
    assert PROBE_EXTENSION in ingest_pkg.supported_extensions()
    assert PROBE_EXTENSION not in ingest_pkg.supported_extensions(directory_walk=True)


def test_wrap_errors_maps_listed_errors_to_value_error(tmp_path: Path) -> None:
    # Arrange
    class ProbeParseError(Exception):
        pass

    def _raise(path: Path) -> ExtractedDoc:
        raise ProbeParseError("bad bytes")

    registry = ExtractorRegistry()
    registry.register(
        ExtractorSpec(
            name="probe",
            extensions=(PROBE_EXTENSION,),
            load=lambda: _raise,
            wrap_errors=lambda: (ProbeParseError,),
        )
    )
    path = tmp_path / f"c{PROBE_EXTENSION}"
    path.write_text("x", encoding="utf-8")

    # Act / Assert
    with pytest.raises(ValueError, match=r"^malformed PROBE: bad bytes$") as info:
        registry.extract_path(path)
    assert isinstance(info.value.__cause__, ProbeParseError)


def test_unlisted_errors_propagate_unwrapped(tmp_path: Path) -> None:
    def _raise(path: Path) -> ExtractedDoc:
        raise OSError("unreadable")

    registry = ExtractorRegistry()
    registry.register(
        ExtractorSpec(name="probe", extensions=(PROBE_EXTENSION,), load=lambda: _raise)
    )
    path = tmp_path / f"d{PROBE_EXTENSION}"
    path.write_text("x", encoding="utf-8")
    with pytest.raises(OSError, match="unreadable"):
        registry.extract_path(path)


def test_loaders_run_only_when_their_extractor_runs(tmp_path: Path) -> None:
    # Arrange: registration and listing must not call ``load`` or ``wrap_errors``,
    # which is what keeps pypdf / docx out of the base import.
    calls: list[str] = []

    def _load() -> dispatch.ExtractorFn:
        calls.append("load")
        return _probe_extract

    def _wrap() -> tuple[type[Exception], ...]:
        calls.append("wrap")
        return ()

    registry = ExtractorRegistry()
    registry.register(
        ExtractorSpec(
            name="probe",
            extensions=(PROBE_EXTENSION,),
            load=_load,
            wrap_errors=_wrap,
        )
    )
    registry.supported_extensions()
    assert calls == []

    # Act
    path = tmp_path / f"e{PROBE_EXTENSION}"
    path.write_text("x", encoding="utf-8")
    registry.extract_path(path)

    # Assert
    assert sorted(calls) == ["load", "wrap"]
