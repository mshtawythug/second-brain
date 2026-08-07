"""Archive packing, checksums, and hardened extraction (F3 §5.4, §7)."""
from __future__ import annotations

import hashlib
import io
import json
import tarfile
from pathlib import Path

import pytest

from brain.backup.archive import (
    extract_archive,
    read_manifest,
    sha256_file,
    verify_sidecar,
    write_archive,
    write_sidecar,
)
from brain.errors import BackupError
from tests.backup_fakes import repo_root_guard  # noqa: F401

SYNTHETIC = b"Larkspur quarterly review \xe2\x80\x94 synthetic corpus body.\n"


def _staging(tmp_path: Path, *, manifest: str = '{"schema": 1}') -> Path:
    """A minimal staging dir shaped like the real one (§5.4)."""
    staging = tmp_path / "staging"
    (staging / "db").mkdir(parents=True)
    (staging / "manifest.json").write_text(manifest, encoding="utf-8")
    (staging / "db" / "second_brain.dump").write_bytes(SYNTHETIC)
    (staging / "vault.tar").write_bytes(b"vault-tar-placeholder")
    return staging


def _hostile_archive(tmp_path: Path, member: tarfile.TarInfo, payload: bytes) -> Path:
    """Build a tar.gz containing exactly one attacker-shaped member."""
    archive = tmp_path / "hostile.tar.gz"
    with tarfile.open(archive, "w:gz") as tar:
        if member.isreg():
            member.size = len(payload)
            tar.addfile(member, io.BytesIO(payload))
        else:
            tar.addfile(member)
    return archive


def test_sha256_matches_hashlib(tmp_path: Path) -> None:
    target = tmp_path / "blob.bin"
    target.write_bytes(SYNTHETIC)

    assert sha256_file(target) == hashlib.sha256(SYNTHETIC).hexdigest()


def test_sha256_streams_files_larger_than_one_chunk(tmp_path: Path) -> None:
    """The 1 MiB chunked read must agree with a single-shot hash."""
    payload = SYNTHETIC * 40_000
    target = tmp_path / "big.bin"
    target.write_bytes(payload)

    assert sha256_file(target) == hashlib.sha256(payload).hexdigest()


def test_write_archive_packs_every_staged_member(tmp_path: Path) -> None:
    staging = _staging(tmp_path)
    dest = tmp_path / "brain-backup-20260725-141203.tar.gz"

    write_archive(staging, dest)

    with tarfile.open(dest, "r:gz") as tar:
        names = sorted(tar.getnames())
    assert "manifest.json" in names
    assert "db/second_brain.dump" in names
    assert "vault.tar" in names


def test_archive_is_written_with_owner_only_permissions(tmp_path: Path) -> None:
    """The archive holds the whole corpus in plaintext, so 0600 (§7)."""
    dest = tmp_path / "archive.tar.gz"

    write_archive(_staging(tmp_path), dest)

    assert (dest.stat().st_mode & 0o777) == 0o600


def test_archive_is_created_atomically(tmp_path: Path) -> None:
    """A writer that dies mid-stream leaves no plausible-looking .tar.gz."""
    staging = _staging(tmp_path)
    dest = tmp_path / "archive.tar.gz"

    def explode(_name: str) -> None:
        raise RuntimeError("disk full")

    with pytest.raises(RuntimeError):
        write_archive(staging, dest, _on_member=explode)

    assert not dest.exists()
    assert list(tmp_path.glob("*.partial")) == []
    assert list(tmp_path.glob("*.tmp")) == []


def test_sidecar_written_and_verified(tmp_path: Path) -> None:
    dest = tmp_path / "archive.tar.gz"
    write_archive(_staging(tmp_path), dest)

    sidecar = write_sidecar(dest)

    assert sidecar == dest.with_suffix(dest.suffix + ".sha256")
    # sha256sum format: "<hex>  <basename>\n"
    digest, name = sidecar.read_text(encoding="utf-8").strip().split("  ")
    assert digest == sha256_file(dest)
    assert name == dest.name
    assert verify_sidecar(dest) is True


def test_sidecar_mismatch_detected(tmp_path: Path) -> None:
    dest = tmp_path / "archive.tar.gz"
    write_archive(_staging(tmp_path), dest)
    write_sidecar(dest)

    # Flip one byte of the archive — a truncated copy to a USB stick.
    corrupted = bytearray(dest.read_bytes())
    corrupted[-1] ^= 0xFF
    dest.write_bytes(bytes(corrupted))

    assert verify_sidecar(dest) is False


def test_missing_sidecar_reports_none_not_false(tmp_path: Path) -> None:
    """A deleted sidecar is 'unknown', not 'corrupt' — restore warns and goes on."""
    dest = tmp_path / "archive.tar.gz"
    write_archive(_staging(tmp_path), dest)

    assert verify_sidecar(dest) is None


def test_read_manifest_parses_without_extracting(tmp_path: Path) -> None:
    from tests.test_backup_manifest import _manifest

    staging = _staging(tmp_path, manifest=json.dumps(_manifest().to_dict()))
    dest = tmp_path / "archive.tar.gz"
    write_archive(staging, dest)

    assert read_manifest(dest) == _manifest()


def test_read_manifest_rejects_archive_without_manifest(tmp_path: Path) -> None:
    staging = tmp_path / "empty-staging"
    (staging / "db").mkdir(parents=True)
    (staging / "db" / "second_brain.dump").write_bytes(SYNTHETIC)
    dest = tmp_path / "archive.tar.gz"
    write_archive(staging, dest)

    with pytest.raises(BackupError, match="manifest.json"):
        read_manifest(dest)


def test_extract_archive_roundtrips_content(tmp_path: Path) -> None:
    dest = tmp_path / "archive.tar.gz"
    write_archive(_staging(tmp_path), dest)
    target = tmp_path / "extracted"

    extract_archive(dest, target)

    assert (target / "db" / "second_brain.dump").read_bytes() == SYNTHETIC
    assert (target / "manifest.json").exists()


def test_extract_rejects_absolute_member(tmp_path: Path) -> None:
    archive = _hostile_archive(tmp_path, tarfile.TarInfo("/etc/passwd"), b"pwned")
    target = tmp_path / "out"

    with pytest.raises(BackupError, match="passwd"):
        extract_archive(archive, target)

    assert not (tmp_path / "etc").exists()


def test_extract_rejects_parent_traversal_member(tmp_path: Path) -> None:
    archive = _hostile_archive(
        tmp_path, tarfile.TarInfo("../../.ssh/authorized_keys"), b"pwned"
    )
    target = tmp_path / "out"

    with pytest.raises(BackupError, match="authorized_keys"):
        extract_archive(archive, target)

    assert not (tmp_path.parent / ".ssh").exists()


def test_extract_rejects_symlink_member(tmp_path: Path) -> None:
    member = tarfile.TarInfo("link")
    member.type = tarfile.SYMTYPE
    member.linkname = "/etc/passwd"
    archive = _hostile_archive(tmp_path, member, b"")
    target = tmp_path / "out"

    with pytest.raises(BackupError, match="link"):
        extract_archive(archive, target)

    assert not (target / "link").exists()


def test_extract_rejects_hardlink_member(tmp_path: Path) -> None:
    member = tarfile.TarInfo("hard")
    member.type = tarfile.LNKTYPE
    member.linkname = "manifest.json"
    archive = _hostile_archive(tmp_path, member, b"")
    target = tmp_path / "out"

    with pytest.raises(BackupError, match="hard"):
        extract_archive(archive, target)


def test_extract_rejects_device_member(tmp_path: Path) -> None:
    member = tarfile.TarInfo("dev/null")
    member.type = tarfile.CHRTYPE
    member.devmajor = 1
    member.devminor = 3
    archive = _hostile_archive(tmp_path, member, b"")
    target = tmp_path / "out"

    with pytest.raises(BackupError, match="dev/null"):
        extract_archive(archive, target)


def test_extract_rejects_member_escaping_via_nested_path(tmp_path: Path) -> None:
    """A path that only escapes once resolved is still refused."""
    archive = _hostile_archive(tmp_path, tarfile.TarInfo("safe/../../escape"), b"x")
    target = tmp_path / "out"

    with pytest.raises(BackupError):
        extract_archive(archive, target)

    assert not (tmp_path.parent / "escape").exists()


def test_read_manifest_rejects_a_non_tar_file(tmp_path: Path) -> None:
    """A truncated download is not a tar at all — say so, don't traceback."""
    bogus = tmp_path / "brain-backup-broken.tar.gz"
    bogus.write_bytes(b"this is definitely not a gzip stream")

    with pytest.raises(BackupError, match="not a readable tar.gz"):
        read_manifest(bogus)


def test_read_manifest_rejects_invalid_json(tmp_path: Path) -> None:
    staging = _staging(tmp_path, manifest="{not valid json")
    dest = tmp_path / "archive.tar.gz"
    write_archive(staging, dest)

    with pytest.raises(BackupError, match="not valid JSON"):
        read_manifest(dest)


def test_read_manifest_rejects_a_directory_named_manifest_json(tmp_path: Path) -> None:
    """`extractfile` yields None for a non-regular member — a clear error, not None."""
    staging = tmp_path / "staging"
    (staging / "manifest.json").mkdir(parents=True)
    dest = tmp_path / "archive.tar.gz"
    write_archive(staging, dest)

    with pytest.raises(BackupError, match="not a regular file"):
        read_manifest(dest)


def test_extract_archive_rejects_a_non_tar_file(tmp_path: Path) -> None:
    bogus = tmp_path / "broken.tar.gz"
    bogus.write_bytes(b"not a gzip stream either")

    with pytest.raises(BackupError, match="not a readable tar.gz"):
        extract_archive(bogus, tmp_path / "out")


# ---------------------------------------------------------------------------
# Regression: the test doubles must never write inside the checkout.
# ---------------------------------------------------------------------------


def test_copy_into_container_writes_nothing_on_the_host(tmp_path: Path) -> None:
    """`docker cp <host> <container>:/path` has no host side (the 2026-07-26 bug).

    Treating the `<container>:/path` destination as a local path is what created
    a directory literally named `second-brain-postgres:` in the repo root.
    """
    from tests.backup_fakes import materialise_copy_out

    argv = [
        "docker",
        "cp",
        str(tmp_path / "local.dump"),
        "second-brain-postgres:/tmp/brain-restore-abc.dump",
    ]

    assert materialise_copy_out(argv, b"payload") is False
    assert not (Path.cwd() / "second-brain-postgres:").exists()
    assert list(tmp_path.iterdir()) == []


def test_copy_out_of_container_writes_to_the_host_path(tmp_path: Path) -> None:
    from tests.backup_fakes import materialise_copy_out

    destination = tmp_path / "db" / "second_brain.dump"
    argv = ["docker", "cp", "second-brain-postgres:/tmp/x.dump", str(destination)]

    assert materialise_copy_out(argv, b"payload") is True
    assert destination.read_bytes() == b"payload"


def test_double_refuses_to_write_inside_the_checkout() -> None:
    """The guard itself must fire — verified, not assumed."""
    from tests.backup_fakes import REPO_ROOT, SandboxEscape, materialise_copy_out

    argv = [
        "docker",
        "cp",
        "second-brain-postgres:/tmp/x.dump",
        str(REPO_ROOT / "escaped.dump"),
    ]

    with pytest.raises(SandboxEscape, match="inside the checkout"):
        materialise_copy_out(argv, b"payload")

    assert not (REPO_ROOT / "escaped.dump").exists()
