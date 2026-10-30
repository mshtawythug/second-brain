"""The untrusted-export reader of RM-35 (ruling R5): folders, zips, and every guard.

Each refusal test is adversarial: a crafted archive or tree that a real Notion
export never produces. A refusal message must name no member, so every one is
checked for the crafted name too. All content is synthetic.
"""
from __future__ import annotations

import os
import struct
import warnings
import zipfile
from collections.abc import Callable, Iterator
from pathlib import Path, PurePosixPath
from typing import Any

import pytest

from brain.ingest import notion_archive as na
from brain.ingest.notion_archive import ExportLimits, NotionExportError, open_export
from brain.ingest.notion_export import ExportFile
from tests import notion_fixtures as fx

pytestmark = pytest.mark.nodb

PAGE = f"Ledger {fx.LEDGER_ID}.md"
PAGE_TEXT = "# Ledger\n\nSynthetic ledger body.\n"


def _paths(files: tuple[ExportFile, ...]) -> set[str]:
    return {str(f.path) for f in files}


def _tree_listing(root: Path) -> set[str]:
    return {str(p.relative_to(root)) for p in root.rglob("*")}


def _refused(path: Path, match: str, limits: ExportLimits | None = None) -> str:
    with pytest.raises(NotionExportError, match=match) as info, open_export(path, limits=limits):
        pass
    return str(info.value)


def _zip_with(path: Path, *infos: tuple[zipfile.ZipInfo | str, bytes]) -> Path:
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", UserWarning)  # zipfile warns on duplicate names
        with zipfile.ZipFile(path, "w", compression=zipfile.ZIP_DEFLATED) as zf:
            for info, data in infos:
                zf.writestr(info, data)
    return path


#: (signature, fixed length before the name, flag offset, uncompressed-size offset)
_HEADERS = ((b"PK\x03\x04", 30, 6, 22), (b"PK\x01\x02", 46, 8, 24))


def _patch_headers(path: Path, name: str, *, size: int | None = None, flag: int = 0) -> None:
    """Rewrite ``name``'s declared size, or OR a general-purpose flag, in both headers.

    ``zipfile`` writes its own sizes and clears caller flag bits, so a crafted
    header is made by editing the bytes after the archive is written.
    """
    data = bytearray(path.read_bytes())
    raw = name.encode()
    start = 0
    while (i := data.find(raw, start)) != -1:
        for sig, header_len, flag_at, size_at in _HEADERS:
            h = i - header_len
            if data[h : h + 4] != sig:
                continue
            data[h + flag_at] |= flag
            if size is not None:
                data[h + size_at : h + size_at + 4] = size.to_bytes(4, "little")
        start = i + 1
    path.write_bytes(bytes(data))


# --- folders --------------------------------------------------------------------------


def test_folder_lists_every_regular_file_and_reads_pages(tmp_path: Path) -> None:
    root = fx.write_tree(tmp_path / "export", fx.standard_export())

    with open_export(root) as export:
        assert _paths(export.files) == set(fx.standard_export())
        page = next(f for f in export.files if str(f.path) == fx.HANDBOOK_PATH)
        assert page.size == len(fx.HANDBOOK_MD.encode())
        assert export.read_text(page) == fx.HANDBOOK_MD
        assert export.skipped == {}


def test_folder_decodes_invalid_utf8_with_replacement(tmp_path: Path) -> None:
    root = fx.write_tree(tmp_path / "export", {PAGE: b"# Ledger \xff\n"})

    with open_export(root) as export:
        assert export.read_text(export.files[0]) == "# Ledger \ufffd\n"


def test_folder_skips_and_counts_links_and_never_descends_them(tmp_path: Path) -> None:
    outside = fx.write_tree(tmp_path / "outside", {PAGE: "# outside\n"})
    root = fx.write_tree(tmp_path / "export", {fx.HANDBOOK_PATH: fx.HANDBOOK_MD})
    (root / f"Linked {fx.BOOK_ONE_ID}.md").symlink_to(outside / PAGE)
    (root / f"Linked dir {fx.BOOK_TWO_ID}").symlink_to(outside, target_is_directory=True)
    os.mkfifo(root / f"Pipe {fx.ONBOARDING_ID}.md")

    with open_export(root) as export:
        assert _paths(export.files) == {fx.HANDBOOK_PATH}
        assert export.skipped == {na.SKIP_LINK: 3}


def test_folder_skips_and_counts_oversized_pages_but_lists_large_attachments(
    tmp_path: Path,
) -> None:
    root = fx.write_tree(
        tmp_path / "export",
        {PAGE: "x" * 101, f"Small {fx.BOOK_ONE_ID}.md": "x" * 100, "big.png": b"y" * 500},
    )

    with open_export(root, limits=ExportLimits(max_file_bytes=100)) as export:
        assert _paths(export.files) == {f"Small {fx.BOOK_ONE_ID}.md", "big.png"}
        assert export.skipped == {na.SKIP_TOO_LARGE: 1}


def test_folder_with_too_many_files_is_refused(tmp_path: Path) -> None:
    root = fx.write_tree(tmp_path / "export", {f"f{i}.png": b"x" for i in range(4)})

    with open_export(root, limits=ExportLimits(max_files=4)) as export:
        assert len(export.files) == 4
    _refused(root, "more files than the cap", ExportLimits(max_files=3))


def test_folder_over_the_read_cap_is_refused_before_any_read(tmp_path: Path) -> None:
    root = fx.write_tree(
        tmp_path / "export",
        {PAGE: "x" * 60, f"B {fx.BOOK_ONE_ID}.csv": "x" * 50, "big.png": b"y" * 900},
    )

    with open_export(root, limits=ExportLimits(max_read_bytes=110)):
        pass  # attachments never count: they are never read
    _refused(root, "pass the read cap", ExportLimits(max_read_bytes=109))


def test_folder_files_that_grow_after_listing_are_refused(tmp_path: Path) -> None:
    root = fx.write_tree(tmp_path / "export", {PAGE: "x", f"B {fx.BOOK_ONE_ID}.md": "x"})
    limits = ExportLimits(max_file_bytes=50, max_read_bytes=60)

    with open_export(root, limits=limits) as export:
        (root / PAGE).write_text("x" * 51)
        with pytest.raises(NotionExportError, match="grew past the size cap"):
            export.read_text(next(f for f in export.files if str(f.path) == PAGE))
    with open_export(root.parent / "export", limits=ExportLimits(max_read_bytes=60)) as export:
        (root / PAGE).write_text("x" * 40)
        (root / f"B {fx.BOOK_ONE_ID}.md").write_text("x" * 40)
        first, second = sorted(export.files, key=lambda f: str(f.path))
        export.read_text(first)
        with pytest.raises(NotionExportError, match="pass the read cap"):
            export.read_text(second)


def test_folder_file_swapped_for_a_link_after_listing_is_refused(tmp_path: Path) -> None:
    outside = fx.write_tree(tmp_path / "outside", {PAGE: "# outside secret\n"})
    inside_target = fx.write_tree(tmp_path / "export" / "inner", {"t.md": "inside"})
    root = fx.write_tree(tmp_path / "export", {PAGE: PAGE_TEXT, f"B {fx.BOOK_ONE_ID}.md": "x"})

    with open_export(root) as export:
        files = {str(f.path): f for f in export.files}
        (root / PAGE).unlink()
        (root / PAGE).symlink_to(outside / PAGE)
        with pytest.raises(NotionExportError, match="resolves outside") as info:
            export.read_text(files[PAGE])
        assert "outside secret" not in str(info.value)
        (root / f"B {fx.BOOK_ONE_ID}.md").unlink()
        (root / f"B {fx.BOOK_ONE_ID}.md").symlink_to(inside_target / "t.md")
        with pytest.raises(NotionExportError, match="could not be read as the file"):
            export.read_text(files[f"B {fx.BOOK_ONE_ID}.md"])


def test_folder_directory_swapped_for_a_link_after_listing_is_refused(tmp_path: Path) -> None:
    outside = fx.write_tree(tmp_path / "outside", {PAGE: "# outside\n"})
    root = fx.write_tree(tmp_path / "export", {f"Sub/{PAGE}": PAGE_TEXT})

    with open_export(root) as export:
        (root / "Sub" / PAGE).unlink()
        (root / "Sub").rmdir()
        (root / "Sub").symlink_to(outside, target_is_directory=True)
        with pytest.raises(NotionExportError, match="resolves outside"):
            export.read_text(export.files[0])


def test_only_pages_and_tables_are_ever_read(tmp_path: Path) -> None:
    root = fx.write_tree(tmp_path / "export", {"image.png": fx.PNG_BYTES})

    with open_export(root) as export, pytest.raises(ValueError, match="only pages and tables"):
        export.read_text(export.files[0])


@pytest.mark.parametrize("name", ["notes.txt", "export.ZIPX", "missing.zip"])
def test_a_path_that_is_not_a_folder_or_zip_is_refused(tmp_path: Path, name: str) -> None:
    path = tmp_path / name
    if name != "missing.zip":
        path.write_text("not an export")

    _refused(path, "neither a folder nor a .zip")


def _listed_then(root: Path, swap: Callable[[Path], None]) -> NotionExportError:
    """List ``root``, apply ``swap`` to ``PAGE``'s path, and return the read's refusal."""
    with open_export(root) as export:
        page = next(f for f in export.files if f.path.name == PAGE)
        swap(root.joinpath(*page.path.parts))
        with pytest.raises(NotionExportError) as info:
            export.read_text(page)
    return info.value


def test_folder_skips_a_file_with_more_than_one_hard_link(tmp_path: Path) -> None:
    outside = fx.write_tree(tmp_path / "outside", {"secret.md": "# outside secret\n"})
    root = fx.write_tree(tmp_path / "export", {fx.HANDBOOK_PATH: fx.HANDBOOK_MD})
    os.link(outside / "secret.md", root / PAGE)

    with open_export(root) as export:
        assert _paths(export.files) == {fx.HANDBOOK_PATH}
        assert export.skipped == {na.SKIP_LINK: 1}


def test_folder_entry_that_vanishes_while_listed_is_skipped_unnamed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = fx.write_tree(tmp_path / "export", {fx.HANDBOOK_PATH: fx.HANDBOOK_MD})
    real_walk = os.walk

    def walk_with_a_ghost(top: Any, **kwargs: Any) -> Iterator[tuple[str, list[str], list[str]]]:
        for dirpath, dirnames, filenames in real_walk(top, **kwargs):
            yield dirpath, dirnames, [*filenames, f"Ghost {fx.LEDGER_ID}.md"]

    monkeypatch.setattr(na.os, "walk", walk_with_a_ghost)

    with open_export(root) as export:
        assert _paths(export.files) == {fx.HANDBOOK_PATH}
        assert export.skipped == {na.SKIP_UNREADABLE: 1}


def _replace_with_fifo(path: Path) -> None:
    path.unlink()
    os.mkfifo(path)


def _replace_with_dir(path: Path) -> None:
    path.unlink()
    path.mkdir()


def _replace_with_new_file(path: Path) -> None:
    """Rename another file over it: two inodes exist at once, so they always differ.

    An unlink-then-create may reuse the freed inode number (ext4 does), which the
    identity check cannot see and need not: that file is the export's own.
    """
    other = path.with_name("other.tmp")
    other.write_text("# a different file\n")
    os.replace(other, path)


@pytest.mark.parametrize(
    "swap", [_replace_with_fifo, _replace_with_dir, _replace_with_new_file],
    ids=["fifo", "directory", "new-inode"],
)
def test_folder_file_replaced_after_listing_is_refused(
    tmp_path: Path, swap: Callable[[Path], None]
) -> None:
    root = fx.write_tree(tmp_path / "export", {PAGE: PAGE_TEXT})

    refusal = _listed_then(root, swap)

    assert "could not be read as the file that was listed" in str(refusal)


def test_folder_file_replaced_by_a_hard_link_after_listing_is_refused(tmp_path: Path) -> None:
    outside = fx.write_tree(tmp_path / "outside", {"secret.md": "# outside secret\n"})
    root = fx.write_tree(tmp_path / "export", {PAGE: PAGE_TEXT})

    def hard_link(path: Path) -> None:
        path.unlink()
        os.link(outside / "secret.md", path)

    refusal = _listed_then(root, hard_link)

    assert "could not be read as the file" in str(refusal)
    assert "secret" not in str(refusal)


def test_folder_parent_swapped_for_an_inside_link_after_listing_is_refused(
    tmp_path: Path,
) -> None:
    root = fx.write_tree(
        tmp_path / "export", {f"Sub/{PAGE}": PAGE_TEXT, f"Other/{PAGE}": "# other page\n"}
    )

    def swap_parent(path: Path) -> None:
        path.unlink()
        path.parent.rmdir()
        path.parent.symlink_to(root / "Other", target_is_directory=True)

    with open_export(root) as export:
        page = next(f for f in export.files if str(f.path) == f"Sub/{PAGE}")
        swap_parent(root / "Sub" / PAGE)
        with pytest.raises(NotionExportError, match="could not be read as the file"):
            export.read_text(page)


def test_folder_read_error_is_refused_unnamed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = fx.write_tree(tmp_path / "export", {PAGE: PAGE_TEXT})
    real_fdopen = os.fdopen

    class FailingRead:
        def __init__(self, fd: int, mode: str, **kwargs: Any) -> None:
            self._fh = real_fdopen(fd, mode, **kwargs)

        def __enter__(self) -> FailingRead:
            return self

        def __exit__(self, *exc: object) -> None:
            self._fh.close()

        def read(self, _n: int) -> bytes:
            raise OSError(5, f"I/O error on {PAGE}")

    monkeypatch.setattr(na.os, "fdopen", FailingRead)

    with open_export(root) as export, pytest.raises(NotionExportError) as info:
        export.read_text(export.files[0])

    assert "could not be read as the file" in str(info.value)
    assert PAGE not in str(info.value)


# --- zips -----------------------------------------------------------------------------


def test_zip_lists_members_and_reads_them_without_extracting(tmp_path: Path) -> None:
    archive = fx.write_zip(tmp_path / "in" / "export.zip", fx.standard_export(), prefix="Export/")
    before = _tree_listing(tmp_path)

    with open_export(archive) as export:
        assert _paths(export.files) == {f"Export/{p}" for p in fx.standard_export()}
        page = next(f for f in export.files if f.path.name == PurePosixPath(fx.HANDBOOK_PATH).name)
        assert export.read_text(page) == fx.HANDBOOK_MD
        assert export.skipped == {}

    assert _tree_listing(tmp_path) == before


def test_zip_directory_entries_are_not_files(tmp_path: Path) -> None:
    archive = _zip_with(tmp_path / "e.zip", (zipfile.ZipInfo("Sub/"), b""), (f"Sub/{PAGE}", b"x"))

    with open_export(archive) as export:
        assert _paths(export.files) == {f"Sub/{PAGE}"}


def test_upper_case_zip_suffix_is_accepted(tmp_path: Path) -> None:
    archive = fx.write_zip(tmp_path / "EXPORT.ZIP", {PAGE: PAGE_TEXT})

    with open_export(archive) as export:
        assert export.read_text(export.files[0]) == PAGE_TEXT


@pytest.mark.parametrize(
    "name",
    [
        "/etc/notion.md",
        "../escape.md",
        "Sub/../../escape.md",
        "./here.md",
        "Sub//double.md",
        "C:/windows.md",
        "c:\\windows.md",
        "Sub\\..\\escape.md",
        "..\\escape.md",
    ],
)
def test_zip_with_a_traversing_member_name_is_refused(tmp_path: Path, name: str) -> None:
    archive = _zip_with(tmp_path / "e.zip", (PAGE, b"fine"), (name, b"crafted"))

    message = _refused(archive, "absolute or leaves the archive")

    assert name not in message and "escape" not in message


@pytest.mark.parametrize("name", ["Q3: plan.md", "a\\b.md", "C:notes.md", "...md"])
def test_zip_names_that_cannot_leave_the_root_are_kept(tmp_path: Path, name: str) -> None:
    archive = _zip_with(tmp_path / "e.zip", (name, b"x"))

    with open_export(archive) as export:
        assert _paths(export.files) == {name}


def test_zip_with_a_symlink_member_is_refused(tmp_path: Path) -> None:
    link = zipfile.ZipInfo(f"Linked {fx.BOOK_ONE_ID}.md")
    link.external_attr = (0o120777 << 16)
    archive = _zip_with(tmp_path / "e.zip", (PAGE, b"fine"), (link, b"../../outside/secret"))

    message = _refused(archive, "symbolic-link member")

    assert "outside" not in message


def test_zip_regular_file_mode_is_accepted(tmp_path: Path) -> None:
    regular = zipfile.ZipInfo(PAGE)
    regular.external_attr = (0o100644 << 16)
    archive = _zip_with(tmp_path / "e.zip", (regular, PAGE_TEXT.encode()))

    with open_export(archive) as export:
        assert export.read_text(export.files[0]) == PAGE_TEXT


def test_zip_with_duplicate_member_names_is_refused(tmp_path: Path) -> None:
    archive = _zip_with(tmp_path / "e.zip", (PAGE, b"one"), (PAGE, b"two"))

    _refused(archive, "two members with the same name")


def test_zip_with_an_encrypted_member_is_refused(tmp_path: Path) -> None:
    archive = _zip_with(tmp_path / "e.zip", (PAGE, b"ciphertext"))
    _patch_headers(archive, PAGE, flag=0x1)

    _refused(archive, "encrypted member")


def test_zip_with_too_many_members_is_refused(tmp_path: Path) -> None:
    archive = _zip_with(tmp_path / "e.zip", *[(f"f{i}.png", b"x") for i in range(4)])

    with open_export(archive, limits=ExportLimits(max_files=4)) as export:
        assert len(export.files) == 4
    _refused(archive, "more files than the cap", ExportLimits(max_files=3))


def test_zip_bomb_over_the_read_cap_is_refused_before_decompressing(tmp_path: Path) -> None:
    archive = _zip_with(
        tmp_path / "e.zip", (PAGE, b"\0" * 2_000_000), (f"B {fx.BOOK_ONE_ID}.md", b"\0" * 10)
    )
    assert archive.stat().st_size < 10_000  # a real bomb: tiny on disk

    _refused(archive, "pass the read cap", ExportLimits(max_read_bytes=1_000_000))


def test_zip_oversized_page_is_skipped_and_an_attachment_never_decompressed(
    tmp_path: Path,
) -> None:
    archive = _zip_with(
        tmp_path / "e.zip", (PAGE, b"x" * 101), ("bomb.png", b"\0" * 5_000_000), ("ok.md", b"x")
    )

    with open_export(
        archive, limits=ExportLimits(max_file_bytes=100, max_read_bytes=1_000)
    ) as export:
        assert _paths(export.files) == {"bomb.png", "ok.md"}
        assert export.skipped == {na.SKIP_TOO_LARGE: 1}


def test_zip_member_that_understates_its_size_is_refused_as_corrupt(tmp_path: Path) -> None:
    archive = _zip_with(tmp_path / "e.zip", (PAGE, b"z" * 5_000))
    _patch_headers(archive, PAGE, size=10)

    with open_export(archive, limits=ExportLimits(max_file_bytes=100)) as export:
        assert export.files[0].size == 10
        with pytest.raises(NotionExportError, match="member is corrupt"):
            export.read_text(export.files[0])


def test_zip_member_with_corrupt_data_is_refused(tmp_path: Path) -> None:
    archive = _zip_with(tmp_path / "e.zip", (PAGE, os.urandom(64) + PAGE_TEXT.encode() * 40))
    data = bytearray(archive.read_bytes())
    offset = data.find(b"PK\x03\x04") + 30 + len(PAGE) + 5
    data[offset : offset + 20] = bytes(b ^ 0xFF for b in data[offset : offset + 20])
    archive.write_bytes(bytes(data))

    with open_export(archive) as export, pytest.raises(NotionExportError, match="corrupt"):
        export.read_text(export.files[0])


@pytest.mark.parametrize("content", [b"", b"PK\x05\x06 truncated", b"not a zip at all"])
def test_a_file_named_zip_that_is_not_one_is_refused(tmp_path: Path, content: bytes) -> None:
    path = tmp_path / "e.zip"
    path.write_bytes(content)

    _refused(path, "not a readable zip")


# --- the central directory is bounded before zipfile parses it (sec-104 L3) ----------


def _never(_path: Path) -> zipfile.ZipFile:
    raise AssertionError("zipfile parsed an archive the directory guard should have refused")


def _refused_unparsed(path: Path, match: str, limits: ExportLimits | None = None) -> None:
    """Refused with ``match``, and ``zipfile`` was never asked to parse the archive."""
    with pytest.raises(NotionExportError, match=match), open_export(
        path, limits=limits, open_zip=_never
    ):
        pass


def _many(path: Path, count: int) -> Path:
    return _zip_with(path, *[(f"f{i}.png", b"x") for i in range(count)])


def _eocd_offset(data: bytes) -> int:
    return data.rfind(b"PK\x05\x06")


def _patch_eocd(path: Path, *, entries: int | None = None, size: int | None = None) -> None:
    data = bytearray(path.read_bytes())
    at = _eocd_offset(data)
    if entries is not None:
        data[at + 8 : at + 12] = struct.pack("<2H", entries, entries)
    if size is not None:
        data[at + 12 : at + 16] = struct.pack("<L", size)
    path.write_bytes(bytes(data))


def _zip64(path: Path, count: int, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A real ZIP64 archive: zipfile writes the ZIP64 end records past its count limit."""
    with monkeypatch.context() as patch:
        patch.setattr(zipfile, "ZIP_FILECOUNT_LIMIT", 1)
        archive = _many(path, count)
    assert archive.read_bytes().rfind(b"PK\x06\x07") != -1
    return archive


def test_more_entries_than_the_cap_is_refused_before_parsing(tmp_path: Path) -> None:
    archive = _many(tmp_path / "e.zip", 4)

    with open_export(archive, limits=ExportLimits(max_files=4)) as export:
        assert len(export.files) == 4
    _refused_unparsed(archive, "more files than the cap", ExportLimits(max_files=3))


def test_a_declared_count_of_one_over_a_larger_directory_is_still_capped(
    tmp_path: Path,
) -> None:
    """sec-104 (b): zipfile ignores the count and parses the declared size."""
    archive = _many(tmp_path / "e.zip", 4)
    _patch_eocd(archive, entries=1)

    _refused_unparsed(archive, "more files than the cap", ExportLimits(max_files=3))


def test_a_zip64_locator_is_followed_whatever_the_plain_record_claims(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """sec-104 (a): the plain record claims 1 entry and 100 bytes, with no ZIP64 sentinel."""
    archive = _zip64(tmp_path / "e.zip", 4, monkeypatch)
    _patch_eocd(archive, entries=1, size=100)

    with open_export(archive, limits=ExportLimits(max_files=4)) as export:
        assert len(export.files) == 4  # what zipfile reads, through the locator
    _refused_unparsed(archive, "more files than the cap", ExportLimits(max_files=3))


def test_a_zip64_record_with_extensible_data_is_refused_unparsed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """sec-104 (c) and its residual: fixed and unfixed zipfile read different records here."""
    archive = _zip64(tmp_path / "e.zip", 4, monkeypatch)
    data = bytearray(archive.read_bytes())
    record_at = data.rfind(b"PK\x06\x06")
    size = struct.unpack_from("<Q", data, record_at + 4)[0]
    struct.pack_into("<Q", data, record_at + 4, size + 24)
    end = record_at + 56
    archive.write_bytes(bytes(data[:end]) + b"\x00" * 24 + bytes(data[end:]))

    _refused_unparsed(archive, "not a readable zip")


def test_the_old_zipfile_bypass_is_refused_unparsed(tmp_path: Path) -> None:
    """sec-104 residual: an unfixed zipfile would parse every entry this shape hides."""
    archive = tmp_path / "e.zip"
    archive.write_bytes(fx.old_zipfile_bypass(50))

    _refused_unparsed(archive, "not a readable zip", ExportLimits(max_files=10))


def test_a_zip64_archive_with_prepended_data_is_accepted(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A self-extracting stub reads alike on fixed and unfixed zipfile: kept."""
    archive = _zip64(tmp_path / "e.zip", 4, monkeypatch)
    archive.write_bytes(b"#!synthetic stub\n" * 4 + archive.read_bytes())

    with open_export(archive, limits=ExportLimits(max_files=4)) as export:
        assert len(export.files) == 4
    _refused_unparsed(archive, "more files than the cap", ExportLimits(max_files=3))


class _ManyInfos:
    """A parsed archive that holds more entries than the directory walk counted."""

    def __init__(self, count: int) -> None:
        self._infos = [zipfile.ZipInfo(f"f{i}.png") for i in range(count)]

    def infolist(self) -> list[zipfile.ZipInfo]:
        return self._infos

    def __enter__(self) -> _ManyInfos:
        return self

    def __exit__(self, *exc: object) -> None:
        return None


def test_the_parsed_entry_count_is_capped_as_a_backstop(tmp_path: Path) -> None:
    """If a zipfile reads another directory than the walk did, its own count is capped."""
    archive = _many(tmp_path / "e.zip", 1)  # the walk counts one entry

    with pytest.raises(NotionExportError, match="more files than the cap"), open_export(
        archive, limits=ExportLimits(max_files=3), open_zip=lambda _p: _ManyInfos(4)
    ):
        pass
    with open_export(
        archive, limits=ExportLimits(max_files=4), open_zip=lambda _p: _ManyInfos(4)
    ) as export:
        assert len(export.files) == 4


def test_a_directory_larger_than_its_cap_is_refused_before_walking(tmp_path: Path) -> None:
    archive = _zip_with(tmp_path / "e.zip", ("n" * (na._CD_BYTES_PER_ENTRY + 10) + ".md", b"x"))

    with open_export(archive, limits=ExportLimits(max_files=2)) as export:
        assert len(export.files) == 1
    _refused_unparsed(archive, "more files than the cap", ExportLimits(max_files=1))


def test_a_zip64_locator_pointing_at_garbage_is_refused_as_corrupt(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    archive = _zip64(tmp_path / "e.zip", 4, monkeypatch)
    data = bytearray(archive.read_bytes())
    data[data.rfind(b"PK\x06\x06") + 3] ^= 0xFF  # the ZIP64 record's signature
    archive.write_bytes(bytes(data))

    _refused_unparsed(archive, "not a readable zip")


def test_a_damaged_locator_leaves_a_plain_record_that_does_not_fit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    archive = _zip64(tmp_path / "e.zip", 4, monkeypatch)
    data = bytearray(archive.read_bytes())
    data[data.rfind(b"PK\x06\x07") + 3] ^= 0xFF
    archive.write_bytes(bytes(data))

    _refused_unparsed(archive, "not a readable zip")


def test_a_plain_archive_with_exactly_65535_entries_is_accepted(tmp_path: Path) -> None:
    """rev-104 L1: maximal plain values without a locator are the archive's own."""
    archive = _zip_with(tmp_path / "e.zip", *[(f"{i}.png", b"") for i in range(0xFFFF)])
    data = archive.read_bytes()
    assert data.rfind(b"PK\x06\x07") == -1  # zipfile wrote no ZIP64 records
    assert data[_eocd_offset(data) + 10 : _eocd_offset(data) + 12] == b"\xff\xff"

    with open_export(archive) as export:
        assert len(export.files) == 0xFFFF


def test_an_end_record_alone_is_an_empty_plain_archive(tmp_path: Path) -> None:
    path = tmp_path / "e.zip"
    path.write_bytes(struct.pack("<4s4H2LH", b"PK\x05\x06", 0, 0, 0xFFFF, 0xFFFF, 0, 0, 0))

    with open_export(path) as export:
        assert export.files == ()


def test_a_bounded_directory_that_zipfile_still_rejects_is_corrupt(tmp_path: Path) -> None:
    """The guard counts headers; zipfile's own failures still map to the fixed sentence."""
    archive = _zip_with(tmp_path / "e.zip", ("caf\u00e9.md", b"x"))  # a UTF-8-flagged name
    data = bytearray(archive.read_bytes())
    at = data.rfind("caf\u00e9".encode())  # the central-directory copy of the name
    data[at + 3 : at + 5] = b"\xff\xfe"  # no longer UTF-8
    archive.write_bytes(bytes(data))

    _refused(archive, "not a readable zip")


# --- review round 2 (rev-104) -----------------------------------------------------------


def test_a_corrupt_lzma_member_is_refused_as_corrupt(tmp_path: Path) -> None:
    archive = tmp_path / "e.zip"
    with zipfile.ZipFile(archive, "w", compression=zipfile.ZIP_LZMA) as zf:
        zf.writestr(PAGE, PAGE_TEXT * 40)
    data = bytearray(archive.read_bytes())
    at = data.find(PAGE.encode()) + len(PAGE) + 9  # past the name and LZMA's 4-byte prefix
    data[at : at + 24] = bytes(b ^ 0xFF for b in data[at : at + 24])
    archive.write_bytes(bytes(data))

    with open_export(archive) as export, pytest.raises(NotionExportError, match="corrupt"):
        export.read_text(export.files[0])


@pytest.mark.parametrize("zipped", [False, True], ids=["folder", "zip"])
def test_a_byte_order_mark_is_dropped(tmp_path: Path, zipped: bool) -> None:
    files = {PAGE: "\ufeff" + PAGE_TEXT}
    path = (
        fx.write_zip(tmp_path / "e.zip", files) if zipped else fx.write_tree(tmp_path / "e", files)
    )

    with open_export(path) as export:
        assert export.read_text(export.files[0]) == PAGE_TEXT
