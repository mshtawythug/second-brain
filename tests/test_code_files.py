"""Unit tests for :mod:`brain.ingest.code_files` (RM-43): the exclusions, the read, the doc.

Pure, no database and no ``git``: every file is written into ``tmp_path``.
"""
from __future__ import annotations

import os
from pathlib import Path

import pytest

from brain.ingest import code_files as cf
from brain.ingest.code_files import CodeFile

pytestmark = pytest.mark.nodb


# --- normalize_exts ---------------------------------------------------------------------


def test_extensions_are_normalized_to_bare_lowercase_suffixes() -> None:
    assert cf.normalize_exts([".PY", "ts", " .Go "]) == frozenset({"py", "ts", "go"})


def test_no_extension_means_no_filter() -> None:
    assert cf.normalize_exts([]) is None


@pytest.mark.parametrize("bad", ["", " . ", "src/py"])
def test_a_blank_or_path_like_extension_is_refused(bad: str) -> None:
    with pytest.raises(ValueError, match="--ext takes a file extension"):
        cf.normalize_exts([bad])


# --- path_skip_reason --------------------------------------------------------------------


@pytest.mark.parametrize(
    ("path", "reason"),
    [
        ("node_modules/pkg/index.js", cf.SKIP_VENDORED),
        ("a/.venv/lib/site.py", cf.SKIP_VENDORED),
        ("vendor/x.go", cf.SKIP_VENDORED),
        ("web/dist/app.js", cf.SKIP_VENDORED),
        ("build/out.py", cf.SKIP_VENDORED),
        ("third_party/lib.c", cf.SKIP_VENDORED),
        ("Vendor/x.go", cf.SKIP_VENDORED),  # case-insensitive filesystems
        ("package-lock.json", cf.SKIP_LOCKFILE),
        ("sub/Cargo.lock", cf.SKIP_LOCKFILE),
        ("go.sum", cf.SKIP_LOCKFILE),
        ("deps/custom.lock", cf.SKIP_LOCKFILE),
        ("web/app.min.js", cf.SKIP_MINIFIED),
        ("web/style.MIN.css", cf.SKIP_MINIFIED),
        ("web/app.js.map", cf.SKIP_MINIFIED),
    ],
)
def test_path_level_exclusions(path: str, reason: str) -> None:
    assert cf.path_skip_reason(path, None) == reason


@pytest.mark.parametrize(
    "path", ["src/widget.py", "build.py", "vendored/x.py", "docs/vendor.md", "Makefile"]
)
def test_ordinary_paths_are_read(path: str) -> None:
    """Only a DIRECTORY component names a vendored dir; a file named like one is kept."""
    assert cf.path_skip_reason(path, None) is None


def test_ext_keeps_only_the_named_suffixes() -> None:
    exts = frozenset({"py"})

    assert cf.path_skip_reason("src/a.py", exts) is None
    assert cf.path_skip_reason("src/a.PY", exts) is None
    assert cf.path_skip_reason("src/a.ts", exts) == cf.SKIP_EXTENSION
    assert cf.path_skip_reason("Makefile", exts) == cf.SKIP_EXTENSION


def test_select_candidates_sorts_and_counts_reasons() -> None:
    keep, skipped = cf.select_candidates(
        ["src/b.py", "node_modules/x.js", "src/a.py", "yarn.lock", "vendor/y.go"], exts=None
    )

    assert keep == ["src/a.py", "src/b.py"]
    assert skipped == {cf.SKIP_VENDORED: 2, cf.SKIP_LOCKFILE: 1}


# --- read_code_file ----------------------------------------------------------------------


def _root(tmp_path: Path) -> Path:
    root = tmp_path / "repo"
    root.mkdir()
    return root.resolve()


def _put(root: Path, rel: str, data: bytes) -> None:
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)


def test_a_text_file_is_read(tmp_path: Path) -> None:
    root = _root(tmp_path)
    _put(root, "src/a.py", b"x = 1\n")

    assert cf.read_code_file(root, "src/a.py") == CodeFile(path="src/a.py", text="x = 1\n")


def test_a_utf8_bom_is_stripped(tmp_path: Path) -> None:
    root = _root(tmp_path)
    _put(root, "a.py", b"\xef\xbb\xbfx = 1\n")

    read = cf.read_code_file(root, "a.py")

    assert isinstance(read, CodeFile)
    assert read.text == "x = 1\n"


def test_the_size_cap_is_inclusive(tmp_path: Path) -> None:
    root = _root(tmp_path)
    line = b"value = 1  # synthetic\n"
    body = line * (cf.MAX_FILE_BYTES // len(line))
    body += b"#" * (cf.MAX_FILE_BYTES - len(body))  # short lines, so not "minified"
    assert len(body) == cf.MAX_FILE_BYTES
    _put(root, "at.py", body)
    _put(root, "over.py", body + b"\n")

    assert isinstance(cf.read_code_file(root, "at.py"), CodeFile)
    assert cf.read_code_file(root, "over.py") == cf.SKIP_TOO_LARGE


def test_a_file_that_grows_after_the_fstat_is_still_capped(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The read is bounded too, so the cap does not depend on ``fstat`` alone."""
    root = _root(tmp_path)
    _put(root, "grows.py", b"a" * (cf.MAX_FILE_BYTES + 5))
    real_fstat = os.fstat

    def small_fstat(fd: int) -> os.stat_result:
        info = real_fstat(fd)
        return os.stat_result((info.st_mode, *info[1:6], 0, *info[7:]))

    monkeypatch.setattr("brain.ingest.code_files.os.fstat", small_fstat)

    assert cf.read_code_file(root, "grows.py") == cf.SKIP_TOO_LARGE


def test_the_fstat_size_alone_refuses_a_file_before_it_is_read(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The pre-read check: an over-cap ``fstat`` size is refused without reading the file."""
    root = _root(tmp_path)
    _put(root, "small.py", b"x = 1\n")
    real_fstat = os.fstat

    def large_fstat(fd: int) -> os.stat_result:
        info = real_fstat(fd)
        return os.stat_result((*info[:6], cf.MAX_FILE_BYTES + 1, *info[7:]))

    monkeypatch.setattr("brain.ingest.code_files.os.fstat", large_fstat)

    assert cf.read_code_file(root, "small.py") == cf.SKIP_TOO_LARGE


@pytest.mark.parametrize(
    "data",
    [b"abc\x00def", "caf\xe9\n".encode("latin-1"), b"\xff\xfe"],
    ids=["nul", "latin1", "invalid-utf8"],
)
def test_binary_or_non_utf8_is_skipped(tmp_path: Path, data: bytes) -> None:
    root = _root(tmp_path)
    _put(root, "f.dat", data)

    assert cf.read_code_file(root, "f.dat") == cf.SKIP_BINARY


@pytest.mark.parametrize("data", [b"", b"  \n\t\n"])
def test_empty_or_blank_is_skipped(tmp_path: Path, data: bytes) -> None:
    root = _root(tmp_path)
    _put(root, "e.py", data)

    assert cf.read_code_file(root, "e.py") == cf.SKIP_EMPTY


def test_a_symlink_is_not_followed(tmp_path: Path) -> None:
    root = _root(tmp_path)
    _put(root, "real.py", b"x = 1\n")
    (root / "link.py").symlink_to("real.py")

    assert cf.read_code_file(root, "link.py") == cf.SKIP_NOT_REGULAR


def test_a_file_symlink_out_of_the_tree_is_not_regular_and_not_followed(
    tmp_path: Path,
) -> None:
    """A tracked symlink is refused as a symlink, before where it points is looked at."""
    root = _root(tmp_path)
    outside = tmp_path / "outside.py"
    outside.write_text("x = 1\n")
    (root / "out.py").symlink_to(outside)

    assert cf.read_code_file(root, "out.py") == cf.SKIP_NOT_REGULAR


def test_a_directory_and_a_missing_file_are_not_regular(tmp_path: Path) -> None:
    root = _root(tmp_path)
    (root / "submodule").mkdir()

    assert cf.read_code_file(root, "submodule") == cf.SKIP_NOT_REGULAR
    assert cf.read_code_file(root, "gone.py") == cf.SKIP_NOT_REGULAR


def test_a_directory_symlink_out_of_the_tree_is_refused(tmp_path: Path) -> None:
    root = _root(tmp_path)
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "secret.py").write_text("x = 1\n")
    (root / "escape").symlink_to(outside, target_is_directory=True)

    assert cf.read_code_file(root, "escape/secret.py") == cf.SKIP_OUTSIDE


def test_a_fifo_swapped_in_is_not_read(tmp_path: Path) -> None:
    """``O_NONBLOCK`` plus the post-open ``fstat``: a FIFO never blocks the run."""
    root = _root(tmp_path)
    os.mkfifo(root / "pipe.py")

    assert cf.read_code_file(root, "pipe.py") == cf.SKIP_NOT_REGULAR


def test_read_capped_refuses_a_non_regular_target_after_the_open(tmp_path: Path) -> None:
    """The post-open check: ``lstat`` said regular, but the opened object is not."""
    os.mkfifo(tmp_path / "pipe")

    assert cf._read_capped(tmp_path / "pipe") == cf.SKIP_NOT_REGULAR


def test_read_capped_bounds_the_read_itself(tmp_path: Path) -> None:
    path = tmp_path / "big"
    path.write_bytes(b"a" * cf.MAX_FILE_BYTES)

    assert cf._read_capped(path) == b"a" * cf.MAX_FILE_BYTES


# --- is_minified -------------------------------------------------------------------------


def test_minified_needs_a_long_longest_line_and_a_long_mean() -> None:
    long_line = "x" * (cf.MINIFIED_LONGEST_LINE + 1)

    assert cf.is_minified(long_line + "\n") is True
    # One long literal among many short lines: the mean stays low, so it is kept.
    assert cf.is_minified(long_line + "\n" + "y = 1\n" * 50) is False
    # Long-ish lines that never pass the longest bound are kept too.
    assert cf.is_minified(("z" * cf.MINIFIED_LONGEST_LINE + "\n") * 3) is False


def test_a_minified_body_is_skipped_on_read(tmp_path: Path) -> None:
    root = _root(tmp_path)
    _put(root, "bundle.js", ("v" * 1500 + "\n").encode())

    assert cf.read_code_file(root, "bundle.js") == cf.SKIP_MINIFIED


# --- build_code_doc ----------------------------------------------------------------------


def test_the_document_shape(tmp_path: Path) -> None:
    root = _root(tmp_path)

    doc = cf.build_code_doc(repo="sample", root=root, file=CodeFile("src/a.py", "x = 1\n"))

    assert doc.title == "sample/src/a.py"
    assert doc.content == "x = 1\n"
    assert doc.content_type == "code"
    assert doc.source_path == str(root / "src/a.py")
    assert doc.metadata == {"path": "src/a.py", "repo": "sample", "language": "python"}


def test_an_unknown_language_is_omitted(tmp_path: Path) -> None:
    doc = cf.build_code_doc(repo="r", root=_root(tmp_path), file=CodeFile("README.md", "# x\n"))

    assert doc.metadata == {"path": "README.md", "repo": "r"}


def test_every_reason_has_report_text() -> None:
    reasons = {
        cf.SKIP_VENDORED, cf.SKIP_LOCKFILE, cf.SKIP_MINIFIED, cf.SKIP_EXTENSION,
        cf.SKIP_NOT_REGULAR, cf.SKIP_OUTSIDE, cf.SKIP_TOO_LARGE, cf.SKIP_BINARY, cf.SKIP_EMPTY,
    }

    assert set(cf.SKIP_TEXT) == reasons
