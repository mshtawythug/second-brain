"""Tests for brain.wiki.slug — slugify_file_path / slugify_source_path.

Static parity: Python output must match Quartz's slugifyFilePath for a
known corpus of vault-relative paths.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from brain.wiki.slug import slugify_file_path, slugify_source_path


class TestSlugifyFilePath:
    """Static parity: Python slugify must match Quartz's slugifyFilePath."""

    # (input_path, expected_slug)
    CASES: list[tuple[str, str]] = [
        # Basic .md stripping
        ("foo/bar.md", "foo/bar"),
        ("foo/index.md", "foo/index"),
        ("index.md", "index"),
        ("simple.md", "simple"),
        # Deep nesting
        ("foo/bar/baz.md", "foo/bar/baz"),
        # Space → hyphen
        ("my note.md", "my-note"),
        ("folder/my note.md", "folder/my-note"),
        # _index → index
        ("foo/_index.md", "foo/index"),
        ("_index.md", "index"),
        # & → -and- (space around & becomes hyphen first)
        ("foo/a & b.md", "foo/a--and--b"),
        # Already slugged
        ("already-slugged.md", "already-slugged"),
        # % → -percent
        ("100percent.md", "100percent"),
        # Leading slash stripped
        ("/leading.md", "leading"),
        # Deep path with leading slash
        ("/dir/sub/file.md", "dir/sub/file"),
        # Non-.md extension preserved
        ("README.txt", "README.txt"),
        # .html extension stripped
        ("page.html", "page"),
    ]

    def test_slugify_cases(self) -> None:
        for fp, expected in self.CASES:
            result = slugify_file_path(fp)
            assert result == expected, (
                f"slugify_file_path({fp!r}) = {result!r}, expected {expected!r}"
            )

    def test_slugify_source_path_basic(self, tmp_path: Path) -> None:
        vault = tmp_path / "vault"
        vault.mkdir()
        (vault / "notes").mkdir()
        source = vault / "notes" / "my note.md"
        source.write_bytes(b"hi")
        assert slugify_source_path(source, vault) == "notes/my-note"

    def test_slugify_source_path_root_file(self, tmp_path: Path) -> None:
        vault = tmp_path / "vault"
        vault.mkdir()
        source = vault / "index.md"
        source.write_bytes(b"hi")
        assert slugify_source_path(source, vault) == "index"

    def test_slugify_source_path_outside_vault_raises(self, tmp_path: Path) -> None:
        vault = tmp_path / "vault"
        vault.mkdir()
        outside = tmp_path / "other.md"
        outside.write_bytes(b"hi")
        with pytest.raises(ValueError, match="not inside vault_root"):
            slugify_source_path(outside, vault)

    def test_slug_collision_different_filenames(self, tmp_path: Path) -> None:
        """Both 'a b.md' and 'a-b.md' produce slug 'a-b' — key rename-guard case."""
        vault = tmp_path / "vault"
        vault.mkdir()

        spaced = vault / "a b.md"
        spaced.write_bytes(b"content")
        hyphenated = vault / "a-b.md"
        hyphenated.write_bytes(b"content")

        assert slugify_source_path(spaced, vault) == "a-b"
        assert slugify_source_path(hyphenated, vault) == "a-b"
        # Paths are different despite identical slug
        assert spaced.relative_to(vault).as_posix() != hyphenated.relative_to(vault).as_posix()
