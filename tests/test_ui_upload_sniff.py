"""Content sniffing and the zip-bomb guard (Ingest spec §7.4, RM-13).

The client's declared type is never trusted: the bytes must match the extension.
Every fixture is synthetic: the repo's own ``tests/fixtures/sample.*`` files, or bytes
built here from named constants.
"""
from __future__ import annotations

import zipfile
from pathlib import Path

import pytest

from brain.ui.errors import UiUnsupportedMedia
from brain.ui.ingest_limits import DOCX_MAX_EXPANDED_BYTES, DOCX_MAX_MEMBERS, DOCX_MAX_RATIO
from brain.ui.upload_checks import sniff

_DOCX_BODY = b"<w:document>synthetic</w:document>"


def _write(path: Path, data: bytes) -> Path:
    path.write_bytes(data)
    return path


def _docx(
    path: Path, members: dict[str, bytes], *, compression: int = zipfile.ZIP_DEFLATED
) -> Path:
    with zipfile.ZipFile(path, "w", compression=compression) as archive:
        for name, data in members.items():
            archive.writestr(name, data)
    return path


def _refused(path: Path, ext: str) -> UiUnsupportedMedia:
    with pytest.raises(UiUnsupportedMedia) as caught:
        sniff(path, ext)
    assert caught.value.code == "unsupported_file"
    return caught.value


@pytest.mark.parametrize("name", ["sample.pdf", "sample.docx", "sample.md", "sample.txt"])
def test_the_repo_fixtures_pass(fixtures_dir: Path, name: str) -> None:
    path = fixtures_dir / name
    sniff(path, path.suffix)


def test_markdown_extension_alias_is_sniffed_as_text(fixtures_dir: Path, tmp_path: Path) -> None:
    sniff(_write(tmp_path / "x.markdown", (fixtures_dir / "sample.md").read_bytes()), ".markdown")


def test_a_pdf_must_start_with_the_pdf_magic(tmp_path: Path) -> None:
    _refused(_write(tmp_path / "x.pdf", b"not a pdf at all"), ".pdf")
    _refused(_write(tmp_path / "y.pdf", b"PK\x03\x04 a zip"), ".pdf")
    _refused(_write(tmp_path / "z.pdf", b""), ".pdf")


def test_a_docx_must_be_a_zip(tmp_path: Path) -> None:
    _refused(_write(tmp_path / "x.docx", b"%PDF-1.7 a pdf"), ".docx")
    _refused(_write(tmp_path / "y.docx", b"PK\x03\x04 then garbage"), ".docx")


def test_a_docx_must_hold_word_document_xml(tmp_path: Path) -> None:
    _refused(_docx(tmp_path / "x.docx", {"other.xml": b"<x/>"}), ".docx")


def test_a_minimal_docx_passes(tmp_path: Path) -> None:
    sniff(_docx(tmp_path / "x.docx", {"word/document.xml": _DOCX_BODY}), ".docx")


def test_a_docx_whose_member_ratio_exceeds_the_bound_is_refused(tmp_path: Path) -> None:
    """Zeros deflate far past 100:1: the classic bomb shape."""
    bomb = b"\0" * (2 * 1024 * 1024)
    path = _docx(tmp_path / "x.docx", {"word/document.xml": _DOCX_BODY, "word/pad.bin": bomb})
    with zipfile.ZipFile(path) as archive:
        info = archive.getinfo("word/pad.bin")
    assert info.file_size > DOCX_MAX_RATIO * info.compress_size  # the fixture is a bomb

    _refused(path, ".docx")


def test_a_docx_whose_expanded_total_exceeds_the_bound_is_refused(tmp_path: Path) -> None:
    """Stored (ratio 1:1) members cannot exceed the ratio, only the total.

    The total is read from the central directory, so the declared sizes are what is
    checked: a forged ``file_size`` is the attack, and it is refused before any
    member is read.
    """
    path = _docx(
        tmp_path / "x.docx", {"word/document.xml": _DOCX_BODY}, compression=zipfile.ZIP_STORED
    )
    with zipfile.ZipFile(path) as archive:
        info = archive.getinfo("word/document.xml")
    raw = bytearray(path.read_bytes())
    # Forge the central directory's uncompressed size (offset 24 in the entry).
    cd = raw.rfind(b"PK\x01\x02")
    forged = (DOCX_MAX_EXPANDED_BYTES + 1).to_bytes(4, "little")
    raw[cd + 24 : cd + 28] = forged
    raw[cd + 20 : cd + 24] = (DOCX_MAX_EXPANDED_BYTES + 1).to_bytes(4, "little")
    path.write_bytes(bytes(raw))
    assert info.file_size == len(_DOCX_BODY)

    _refused(path, ".docx")


def test_a_docx_with_too_many_members_is_refused(tmp_path: Path) -> None:
    members = {"word/document.xml": _DOCX_BODY}
    members.update({f"m/{i}.xml": b"" for i in range(DOCX_MAX_MEMBERS)})

    _refused(_docx(tmp_path / "x.docx", members, compression=zipfile.ZIP_STORED), ".docx")


@pytest.mark.parametrize("ext", [".md", ".markdown", ".txt"])
def test_text_must_be_strict_utf8(tmp_path: Path, ext: str) -> None:
    _refused(_write(tmp_path / f"x{ext}", b"caf\xe9 latin-1"), ext)


@pytest.mark.parametrize("ext", [".md", ".markdown", ".txt"])
def test_text_must_not_contain_nul(tmp_path: Path, ext: str) -> None:
    _refused(_write(tmp_path / f"x{ext}", b"text\x00with a nul"), ext)


def test_a_pdf_renamed_to_txt_is_refused(fixtures_dir: Path, tmp_path: Path) -> None:
    _refused(_write(tmp_path / "x.txt", (fixtures_dir / "sample.pdf").read_bytes()), ".txt")


def test_utf8_text_and_an_empty_text_file_pass(tmp_path: Path) -> None:
    sniff(_write(tmp_path / "x.txt", "résumé — synthetic\n".encode()), ".txt")
    sniff(_write(tmp_path / "y.txt", b""), ".txt")
    sniff(_write(tmp_path / "z.txt", b"   \n\t\n"), ".txt")


def test_an_unknown_extension_is_refused(tmp_path: Path) -> None:
    _refused(_write(tmp_path / "x.exe", b"MZ"), ".exe")


def test_a_declared_member_count_over_the_bound_is_refused_from_the_end_record(
    tmp_path: Path,
) -> None:
    """The end record claims 60,000 entries and the central directory is garbage: the
    refusal names the count, which proves it came BEFORE zipfile parsed anything (a
    ZipFile open would have failed first, as a damaged zip) (sec-77 #5)."""
    end_record = (
        b"PK\x05\x06" + (0).to_bytes(2, "little") * 2
        + (60_000).to_bytes(2, "little") * 2
        + (1).to_bytes(4, "little") + (4).to_bytes(4, "little") + (0).to_bytes(2, "little")
    )
    path = _write(tmp_path / "x.docx", b"PK\x03\x04" + b"\0" * 100 + end_record)

    error = _refused(path, ".docx")

    assert error.message == "the .docx holds too many parts to unpack safely"


def test_a_zip_with_no_end_record_is_refused_as_damaged(tmp_path: Path) -> None:
    error = _refused(_write(tmp_path / "x.docx", b"PK\x03\x04" + b"\0" * 200), ".docx")

    assert error.message == "the file is not a .docx (a damaged zip)"


def test_the_zip64_marker_is_refused(tmp_path: Path) -> None:
    """0xFFFF entries means zip64 or more than 65,534 members: no .docx under the cap."""
    end_record = b"PK\x05\x06" + b"\0" * 6 + b"\xff\xff" + b"\0" * 10
    error = _refused(_write(tmp_path / "x.docx", b"PK\x03\x04" + end_record), ".docx")

    assert error.message == "the .docx holds too many parts to unpack safely"
