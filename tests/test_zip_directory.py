"""The central-directory bound of RM-35 (sec-104 L3): find it as zipfile does, then walk it.

Every archive is synthetic and built here. Each crafted one is checked against
``zipfile`` itself where that matters: the guard must pick the same directory
``zipfile`` will read, or a crafted end record could steer the two apart.
"""
from __future__ import annotations

import io
import struct
import zipfile

import pytest

from brain.ingest import zip_directory as zd

pytestmark = pytest.mark.nodb

EOCD_SIG = b"PK\x05\x06"
LOCATOR_SIG = b"PK\x06\x07"
ZIP64_SIG = b"PK\x06\x06"


def _plain(count: int, *, comment: bytes = b"") -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        for i in range(count):
            zf.writestr(f"f{i}.md", b"x")
        zf.comment = comment
    return buf.getvalue()


def _zip64(count: int, monkeypatch: pytest.MonkeyPatch) -> bytes:
    """A real ZIP64 archive: zipfile writes ZIP64 end records past its count limit."""
    with monkeypatch.context() as patch:
        patch.setattr(zipfile, "ZIP_FILECOUNT_LIMIT", 1)
        data = _plain(count)
    assert data.rfind(LOCATOR_SIG) != -1
    return data


def _locate(data: bytes) -> zd.Directory:
    return zd.locate(io.BytesIO(data))


def _reason(data: bytes, limit: int = 100) -> str:
    fh = io.BytesIO(data)
    with pytest.raises(zd.ZipDirectoryError) as info:
        zd.count_entries(fh, zd.locate(fh), limit)
    return info.value.reason


def _zipfile_count(data: bytes) -> int:
    with zipfile.ZipFile(io.BytesIO(data)) as zf:
        return len(zf.infolist())


def _patch_eocd(data: bytes, *, entries: int | None = None, size: int | None = None) -> bytes:
    out = bytearray(data)
    at = out.rfind(EOCD_SIG)
    if entries is not None:
        out[at + 8 : at + 12] = struct.pack("<2H", entries, entries)
    if size is not None:
        out[at + 12 : at + 16] = struct.pack("<L", size)
    return bytes(out)


# --- finding the directory ---------------------------------------------------------------


def test_a_plain_archive_is_located_and_walked() -> None:
    data = _plain(3)

    directory = _locate(data)

    assert directory.declared_entries == 3
    assert zd.count_entries(io.BytesIO(data), directory, 3) == 3 == _zipfile_count(data)


def test_an_archive_with_a_comment_is_found_by_searching_the_tail() -> None:
    data = _plain(2, comment=b"synthetic comment")

    assert _locate(data).declared_entries == 2
    assert zd.count_entries(io.BytesIO(data), _locate(data), 2) == 2


@pytest.mark.parametrize("data", [b"", b"PK\x05\x06 short", b"no end record here" * 4])
def test_no_end_record_is_corrupt(data: bytes) -> None:
    with pytest.raises(zd.ZipDirectoryError) as info:
        _locate(data)

    assert info.value.reason == zd.CORRUPT


def test_a_size_larger_than_what_precedes_the_record_is_corrupt() -> None:
    data = _plain(2)

    with pytest.raises(zd.ZipDirectoryError) as info:
        _locate(_patch_eocd(data, size=len(data)))

    assert info.value.reason == zd.CORRUPT


def test_maximal_plain_values_without_a_locator_stand() -> None:
    """rev-104 L1: 0xFFFF entries with no locator is a plain archive at the format limit."""
    data = _patch_eocd(_plain(2), entries=0xFFFF)

    assert _locate(data).declared_entries == 0xFFFF
    assert zd.count_entries(io.BytesIO(data), _locate(data), 2) == 2 == _zipfile_count(data)


def test_a_zip64_archive_is_read_from_its_zip64_record(monkeypatch: pytest.MonkeyPatch) -> None:
    data = _zip64(4, monkeypatch)

    assert _locate(data).declared_entries == 4
    assert zd.count_entries(io.BytesIO(data), _locate(data), 4) == 4 == _zipfile_count(data)


def test_a_locator_is_followed_even_when_the_plain_record_has_no_sentinel(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """sec-104 (a): the plain record claims 1 entry and 100 bytes; zipfile follows the locator."""
    data = _patch_eocd(_zip64(4, monkeypatch), entries=1, size=100)

    assert _locate(data).declared_entries == 4
    assert _zipfile_count(data) == 4
    assert _reason(data, limit=3) == zd.TOO_MANY


def _with_extensible_data(data: bytes, extra: bytes) -> bytes:
    """Insert ZIP64 extensible data between the ZIP64 record and its locator."""
    out = bytearray(data)
    record_at = out.rfind(ZIP64_SIG)
    end = record_at + 56
    size = struct.unpack_from("<Q", out, record_at + 4)[0]
    struct.pack_into("<Q", out, record_at + 4, size + len(extra))
    return bytes(out[:end]) + extra + bytes(out[end:])


def test_the_zip64_record_is_read_at_the_locator_s_offset_first(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """sec-104 (c): with extensible data, the bytes before the locator are not the record."""
    data = _with_extensible_data(_zip64(4, monkeypatch), b"\x00" * 24)

    assert _zipfile_count(data) == 4
    assert _locate(data).declared_entries == 4
    assert _reason(data, limit=3) == zd.TOO_MANY


def test_prepended_data_falls_back_to_the_record_before_the_locator(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    data = b"#!synthetic stub\n" * 4 + _zip64(3, monkeypatch)

    assert _zipfile_count(data) == 3
    assert zd.count_entries(io.BytesIO(data), _locate(data), 3) == 3


def _patch_locator(data: bytes, *, disk: int | None = None, reloff: int | None = None,
                   disks: int | None = None) -> bytes:
    out = bytearray(data)
    at = out.rfind(LOCATOR_SIG)
    if disk is not None:
        struct.pack_into("<L", out, at + 4, disk)
    if reloff is not None:
        struct.pack_into("<Q", out, at + 8, reloff)
    if disks is not None:
        struct.pack_into("<L", out, at + 16, disks)
    return bytes(out)


@pytest.mark.parametrize(
    "patch",
    [{"disk": 1}, {"disks": 2}, {"reloff": 1 << 40}],
    ids=["disk", "disks", "offset-past-the-locator"],
)
def test_a_bad_locator_is_corrupt(monkeypatch: pytest.MonkeyPatch, patch: dict[str, int]) -> None:
    data = _patch_locator(_zip64(3, monkeypatch), **patch)

    with pytest.raises(zd.ZipDirectoryError) as info:
        _locate(data)

    assert info.value.reason == zd.CORRUPT


def test_a_locator_pointing_at_no_record_is_corrupt(monkeypatch: pytest.MonkeyPatch) -> None:
    data = bytearray(_zip64(3, monkeypatch))
    data[data.rfind(ZIP64_SIG) + 3] ^= 0xFF

    with pytest.raises(zd.ZipDirectoryError):
        _locate(bytes(data))


@pytest.mark.parametrize("field", ["dir-size", "record-size"])
def test_a_zip64_record_that_disagrees_with_its_locator_is_corrupt(
    monkeypatch: pytest.MonkeyPatch, field: str
) -> None:
    data = bytearray(_zip64(3, monkeypatch))
    at = data.rfind(ZIP64_SIG)
    offset = at + 40 if field == "dir-size" else at + 4
    struct.pack_into("<Q", data, offset, struct.unpack_from("<Q", data, offset)[0] + 1)

    with pytest.raises(zd.ZipDirectoryError):
        _locate(bytes(data))


def test_a_locator_at_the_start_of_the_file_has_no_room_for_its_record() -> None:
    locator = struct.pack("<4sLQL", LOCATOR_SIG, 0, 0, 1)
    eocd = struct.pack("<4s4H2LH", EOCD_SIG, 0, 0, 0xFFFF, 0xFFFF, 0, 0, 0)

    with pytest.raises(zd.ZipDirectoryError):
        _locate(locator + eocd)


# --- walking it --------------------------------------------------------------------------


def test_the_walk_ignores_the_declared_count() -> None:
    """sec-104 (b): the record claims 1 entry over a real directory of 5."""
    data = _patch_eocd(_plain(5), entries=1)

    assert _zipfile_count(data) == 5  # zipfile ignores the count too
    assert _reason(data, limit=4) == zd.TOO_MANY
    assert zd.count_entries(io.BytesIO(data), _locate(data), 5) == 5


def test_a_header_without_its_signature_is_corrupt() -> None:
    data = bytearray(_plain(2))
    data[data.find(b"PK\x01\x02") + 3] ^= 0xFF

    assert _reason(bytes(data)) == zd.CORRUPT


def test_a_directory_that_ends_mid_header_is_corrupt() -> None:
    data = _plain(2)
    directory = _locate(data)
    longer = zd.Directory(directory.start, directory.size + 10, directory.declared_entries)

    with pytest.raises(zd.ZipDirectoryError) as info:
        zd.count_entries(io.BytesIO(data), longer, 100)

    assert info.value.reason == zd.CORRUPT


def test_the_walk_reads_headers_one_at_a_time() -> None:
    """Constant memory: no read is larger than one 46-byte header (or the end-record tail)."""
    data = _plain(50)
    reads: list[int] = []

    class Recording(io.BytesIO):
        def read(self, size: int | None = -1) -> bytes:
            chunk = super().read(size)
            reads.append(len(chunk))
            return chunk

    fh = Recording(data)
    directory = zd.locate(fh)
    reads.clear()
    zd.count_entries(fh, directory, 50)

    assert reads and max(reads) == 46


def test_a_header_cut_short_after_its_signature_is_corrupt() -> None:
    data = _plain(1)
    directory = _locate(data)
    cut = data[: directory.start + 20]  # the header's signature, then nothing

    with pytest.raises(zd.ZipDirectoryError) as info:
        zd.count_entries(io.BytesIO(cut), directory, 100)

    assert info.value.reason == zd.CORRUPT
