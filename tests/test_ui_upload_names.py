"""The upload filename sanitizer and the extension allow-list (Ingest spec §7.4, RM-13).

The allow-list is the ticket's explicit constant, ``UPLOAD_EXTENSIONS``, and it is
asserted to be a SUBSET of the extractor registry rather than restated from it: once
RM-33 registers image extractors, images must stay non-uploadable until a ticket adds
them to the constant on purpose.

No PII: every name here is synthetic.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from brain.ingest.dispatch import ExtractorRegistry, ExtractorSpec, supported_extensions
from brain.ui.errors import UiBadRequest, UiUnsupportedMedia
from brain.ui.ingest_limits import (
    MAX_BINARY_UPLOAD_BYTES,
    MAX_FILENAME_CHARS,
    MAX_TEXT_UPLOAD_BYTES,
    UPLOAD_EXTENSIONS,
)
from brain.ui.upload_checks import (
    SafeName,
    sanitize_filename,
    upload_cap,
    upload_kind,
    uploadable_extensions,
)


def test_the_allow_list_is_exactly_the_tickets_five_extensions() -> None:
    assert frozenset({".md", ".markdown", ".txt", ".pdf", ".docx"}) == UPLOAD_EXTENSIONS


def test_the_allow_list_is_a_subset_of_the_extractor_registry() -> None:
    assert set(supported_extensions()) >= UPLOAD_EXTENSIONS


def test_uploadable_extensions_is_the_allow_list_in_registry_order() -> None:
    assert uploadable_extensions() == [e for e in supported_extensions() if e in UPLOAD_EXTENSIONS]


def test_an_extension_the_registry_lacks_is_never_uploadable() -> None:
    """Intersected at runtime too, so the config never offers a dead extension."""
    registry = ExtractorRegistry(
        [ExtractorSpec(name="text", extensions=(".txt",), load=lambda: lambda p: None)]  # type: ignore[arg-type,return-value]
    )

    assert uploadable_extensions(registry) == [".txt"]


def test_a_registered_extension_outside_the_allow_list_is_not_uploadable() -> None:
    """The RM-33 case: a new extractor does not make its type uploadable."""
    registry = ExtractorRegistry(
        [
            ExtractorSpec(name="text", extensions=(".txt",), load=lambda: lambda p: None),  # type: ignore[arg-type,return-value]
            ExtractorSpec(name="image", extensions=(".png",), load=lambda: lambda p: None),  # type: ignore[arg-type,return-value]
        ]
    )

    assert uploadable_extensions(registry) == [".txt"]
    with pytest.raises(UiUnsupportedMedia):
        sanitize_filename("shot.png", registry)


@pytest.mark.parametrize(
    ("header", "expected"),
    [
        ("notes.md", SafeName("notes", ".md")),
        ("Notes.MD", SafeName("Notes", ".md")),
        ("report.markdown", SafeName("report", ".markdown")),
        ("plain.txt", SafeName("plain", ".txt")),
        ("paper.pdf", SafeName("paper", ".pdf")),
        ("memo.docx", SafeName("memo", ".docx")),
        # Only the final component survives: traversal and absolute paths are inert.
        ("../../x.pdf", SafeName("x", ".pdf")),
        ("..%2F..%2Fx.pdf", SafeName("x", ".pdf")),
        ("..\\x.pdf", SafeName("x", ".pdf")),
        ("..%5C..%5Cx.pdf", SafeName("x", ".pdf")),
        ("/etc/passwd.txt", SafeName("passwd", ".txt")),
        ("C:\\Users\\someone\\file.txt", SafeName("file", ".txt")),
        # Percent-encoded UTF-8 is decoded.
        ("r%C3%A9sum%C3%A9.pdf", SafeName("résumé", ".pdf")),
        ("two%20words.md", SafeName("two words", ".md")),
        # Control characters are removed, NUL included; so is a bidi override,
        # which can make "evil‮fdp.txt" render as "eviltxt.pdf".
        ("a%00b.txt", SafeName("ab", ".txt")),
        ("tab%09name.md", SafeName("tabname", ".md")),
        ("evil%E2%80%AEfdp.txt", SafeName("evilfdp", ".txt")),
        # Leading dots go, so no hidden file and no ".." component.
        (".hidden.md", SafeName("hidden", ".md")),
        ("...x.txt", SafeName("x", ".txt")),
        ("  padded.md  ", SafeName("padded", ".md")),
    ],
)
def test_the_sanitizer_table(header: str, expected: SafeName) -> None:
    assert sanitize_filename(header) == expected


def test_a_long_name_is_capped_and_keeps_its_extension() -> None:
    name = sanitize_filename("a" * 300 + ".pdf")

    assert name.ext == ".pdf"
    assert len(name.basename) == MAX_FILENAME_CHARS
    assert name.basename == "a" * (MAX_FILENAME_CHARS - 4) + ".pdf"


@pytest.mark.parametrize(
    "header",
    ["", "   ", ".", "..", "...", "%00", "%2F", "dir/", "a/..", "%FF%FE.txt", "café.txt"],
)
def test_empty_dot_only_or_undecodable_names_are_bad_filename(header: str) -> None:
    """``café.txt`` raw (not percent-encoded) is refused: the header must be ASCII."""
    with pytest.raises(UiBadRequest) as caught:
        sanitize_filename(header)

    assert caught.value.code == "bad_filename"


@pytest.mark.parametrize(
    "header", ["archive.zip", "binary.exe", "noextension", "image.png", "script.md.exe", ".pdf"]
)
def test_an_extension_outside_the_allow_list_is_unsupported_file(header: str) -> None:
    with pytest.raises(UiUnsupportedMedia) as caught:
        sanitize_filename(header)

    assert caught.value.code == "unsupported_file"


def test_a_sanitized_basename_never_has_a_separator_or_a_leading_dot() -> None:
    for header in ["../../x.pdf", "..\\x.pdf", "/etc/passwd.txt", "a%2F..%2F.b.md"]:
        name = sanitize_filename(header)
        assert "/" not in name.basename and "\\" not in name.basename
        assert not name.basename.startswith(".")
        assert Path(name.basename).name == name.basename


def test_caps_are_per_extension() -> None:
    assert upload_cap(".pdf") == MAX_BINARY_UPLOAD_BYTES == 25 * 1024 * 1024
    assert upload_cap(".docx") == MAX_BINARY_UPLOAD_BYTES
    for ext in (".md", ".markdown", ".txt"):
        assert upload_cap(ext) == MAX_TEXT_UPLOAD_BYTES == 5 * 1024 * 1024


def test_every_uploadable_extension_has_a_cap() -> None:
    for ext in UPLOAD_EXTENSIONS:
        assert upload_cap(ext) > 0


def test_the_kind_is_the_extractors_registry_name() -> None:
    assert [upload_kind(e) for e in (".md", ".markdown", ".txt", ".pdf", ".docx")] == [
        "markdown", "markdown", "text", "pdf", "docx",
    ]
