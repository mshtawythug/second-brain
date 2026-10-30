"""The untrusted-export reader of RM-35 (ruling R5): folders, zips, and every guard.

Each refusal test is adversarial: a crafted archive or tree that a real Notion
export never produces. A refusal message must name no member, so every one is
checked for the crafted name too. All content is synthetic.
"""
from __future__ import annotations

import os
import warnings
import zipfile
from pathlib import Path, PurePosixPath

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
        with pytest.raises(NotionExportError, match="could not be opened"):
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
