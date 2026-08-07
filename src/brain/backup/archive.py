"""Pack, checksum, and safely unpack a backup archive."""
from __future__ import annotations

import hashlib
import json
import os
import tarfile
from collections.abc import Callable, Iterator
from pathlib import Path

from ..errors import BackupError
from .manifest import BackupManifest

#: Streaming hash chunk. Matches the `hashlib.sha256` idiom already used at
#: `ingest/__init__.py` and `vault/frontmatter.py`.
_CHUNK_BYTES = 1024 * 1024

#: gzip level 6 — the dump is already internally compressed by `pg_dump -Fc`,
#: so a higher level costs minutes and buys almost nothing.
_COMPRESS_LEVEL = 6

#: The archive holds the entire corpus in plaintext (§7), so it is owner-only.
_ARCHIVE_MODE = 0o600

MANIFEST_MEMBER = "manifest.json"
DUMP_MEMBER = "db/second_brain.dump"
VAULT_MEMBER = "vault.tar"


def sha256_file(path: Path) -> str:
    """Streaming SHA-256 of a file, in 1 MiB chunks."""
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(_CHUNK_BYTES), b""):
            digest.update(block)
    return digest.hexdigest()


def sidecar_path(archive: Path) -> Path:
    """``<archive>.sha256`` — the whole-archive checksum file."""
    return archive.with_suffix(archive.suffix + ".sha256")


def write_sidecar(archive: Path) -> Path:
    """Write ``<archive>.sha256`` in ``sha256sum`` format and return its path.

    The manifest cannot carry its own archive's hash (circular), which is
    exactly why this exists: it catches truncation from a copy to a USB stick
    or a cloud drive, which per-member hashes inside the tar cannot.
    """
    target = sidecar_path(archive)
    target.write_text(f"{sha256_file(archive)}  {archive.name}\n", encoding="utf-8")
    return target


def verify_sidecar(archive: Path) -> bool | None:
    """``True`` if the sidecar matches, ``False`` if not, ``None`` if absent.

    Three states rather than two: a *missing* sidecar means "unknown", and
    restore downgrades that to a warning and falls back to the per-member
    manifest checksums. A *mismatching* sidecar is fatal.
    """
    target = sidecar_path(archive)
    if not target.exists():
        return None
    recorded = target.read_text(encoding="utf-8").strip().split("  ")[0]
    return recorded == sha256_file(archive)


def write_archive(
    staging: Path,
    dest: Path,
    *,
    _on_member: Callable[[str], None] | None = None,
) -> Path:
    """Pack ``staging``'s contents into ``dest`` atomically.

    Written to a sibling ``.partial`` file and ``os.replace``d into position at
    the very end — the same atomic-rename discipline as ``vault/_atomic.py``.
    An interrupted backup therefore never leaves a truncated ``.tar.gz`` that
    looks usable: there is either a complete archive or nothing at all.

    ``_on_member`` is a test seam invoked once per member, used to simulate a
    mid-stream failure.
    """
    dest.parent.mkdir(parents=True, exist_ok=True)
    partial = dest.with_name(dest.name + ".partial")
    try:
        with tarfile.open(partial, "w:gz", compresslevel=_COMPRESS_LEVEL) as tar:
            for path in sorted(staging.rglob("*")):
                name = path.relative_to(staging).as_posix()
                if _on_member is not None:
                    _on_member(name)
                tar.add(path, arcname=name, recursive=False)
        partial.chmod(_ARCHIVE_MODE)
        os.replace(partial, dest)
    finally:
        partial.unlink(missing_ok=True)
    return dest


def read_manifest(archive: Path) -> BackupManifest:
    """Parse ``manifest.json`` out of the archive without unpacking it."""
    try:
        with tarfile.open(archive, "r:gz") as tar:
            try:
                member = tar.getmember(MANIFEST_MEMBER)
            except KeyError as exc:
                raise BackupError(
                    f"{archive.name} contains no {MANIFEST_MEMBER} — it was not "
                    "produced by `brain backup`."
                ) from exc
            handle = tar.extractfile(member)
            if handle is None:
                raise BackupError(
                    f"{archive.name}'s {MANIFEST_MEMBER} is not a regular file."
                )
            payload = json.loads(handle.read().decode("utf-8"))
    except tarfile.TarError as exc:
        raise BackupError(f"{archive.name} is not a readable tar.gz: {exc}") from exc
    except json.JSONDecodeError as exc:
        raise BackupError(
            f"{archive.name}'s {MANIFEST_MEMBER} is not valid JSON: {exc}"
        ) from exc
    return BackupManifest.from_dict(payload)


def _safe_members(tar: tarfile.TarFile, root: Path) -> Iterator[tarfile.TarInfo]:
    """Yield only members that provably land inside ``root``.

    Implemented explicitly rather than via ``tarfile``'s ``filter="data"``,
    which only landed in 3.11.4 while this package supports 3.11.0. Restore
    reads a file the user may have received from anywhere, so every hostile
    shape is refused by name: absolute paths, ``..`` components, symlinks,
    hardlinks, device/FIFO nodes, and anything whose resolved destination
    escapes the root.
    """
    resolved_root = root.resolve()
    for member in tar.getmembers():
        name = member.name
        if name.startswith("/") or Path(name).is_absolute():
            raise BackupError(
                f"refusing to extract absolute path from archive: {name!r}"
            )
        if ".." in Path(name).parts:
            raise BackupError(
                f"refusing to extract path with a '..' component from archive: "
                f"{name!r}"
            )
        if member.issym() or member.islnk():
            raise BackupError(
                f"refusing to extract link member from archive: {name!r} "
                f"-> {member.linkname!r}"
            )
        if member.isdev() or member.isfifo():
            raise BackupError(
                f"refusing to extract device/FIFO member from archive: {name!r}"
            )
        destination = (resolved_root / name).resolve()
        if destination != resolved_root and resolved_root not in destination.parents:
            raise BackupError(
                f"refusing to extract member escaping the destination: {name!r}"
            )
        yield member


def extract_archive(archive: Path, dest: Path) -> Path:
    """Unpack ``archive`` into ``dest`` through the hardened member filter."""
    dest.mkdir(parents=True, exist_ok=True)
    try:
        with tarfile.open(archive, "r:gz") as tar:
            # Materialise the whole vetted list first: `_safe_members` raises
            # before the first write when any member is hostile, so a rejected
            # archive never lands even partially on disk.
            members = list(_safe_members(tar, dest))
            tar.extractall(dest, members=members)  # noqa: S202 — vetted above
    except tarfile.TarError as exc:
        raise BackupError(f"{archive.name} is not a readable tar.gz: {exc}") from exc
    return dest
