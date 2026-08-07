"""Shared path / wiki-link helpers used by the vault renderers.

Both helpers are pure (no I/O, no DB) and small. Three rendering surfaces
hand-rolled the same logic before this module existed:

- :mod:`brain.wiki.build_homepage` — recent-rail bullet renderer
- :mod:`brain.wiki.build_people` — People Hub per-person + index pages
- :mod:`brain.vault.daily_index` — daily-notes index renderer

Centralizing here means a future change (say, switching to ``.markdown``
extension support) flips one place rather than three.

:func:`assert_within_vault` joined them in F8 as the pure, framework-free
form of the path-traversal guard the authoring commands hand-rolled. It
raises :class:`~brain.errors.VaultPathEscape` rather than
``typer.BadParameter`` so library callers (:func:`brain.vault.rename.plan_rename`,
the MCP server, ``brain ui``) are guarded by the same implementation the CLI
uses.
"""
from pathlib import Path, PurePosixPath

from ..errors import VaultPathEscape


def strip_md_extension(path: str) -> str:
    """Return ``path`` with a trailing ``.md`` removed, POSIX-style.

    ``documents.vault_path`` and the equivalent on-disk relative paths are
    stored as forward-slash POSIX strings; we keep the same shape so wiki
    links round-trip on Windows hosts too. Inputs without a trailing
    ``.md`` are returned unchanged (after the POSIX normalization), so
    callers can pipe path-form strings through unconditionally.

    Used by every renderer that emits ``[[<vault-path-no-md>|<title>]]``
    so wiki-links match what
    :func:`brain.vault.resolver._resolve_by_vault_path` looks for. Empty
    input yields ``'.'`` (because ``PurePosixPath('').as_posix() == '.'``);
    the renderers guard for that separately if they care, and the
    contract is pinned by ``tests/test_vault_paths.py::test_empty_input``.
    """
    posix = PurePosixPath(path).as_posix()
    if posix.endswith(".md"):
        return posix[:-3]
    return posix


def safe_wikilink_alias(title: str) -> str:
    """Strip ``[`` / ``]`` from a wiki-link alias slot.

    Quartz's wiki-link regex defines the alias slot as ``[^\\[\\]\\#]``;
    any ``[`` or ``]`` inside an alias makes the whole ``[[...]]`` fail
    to match and emit as raw text. Doc titles in the brain corpus
    routinely contain bracketed prefixes — ``Re: [External] Re: ...``
    from forwarded Gmail, ``[2026-04] Plenty sync notes`` from manual
    notes — so we swap brackets for parens in the alias slot only. The
    wiki-link target is :data:`documents.vault_path`, which never
    contains these characters by construction.

    Returns the title unchanged when no replacement is needed (cheap
    early-out for the common case).
    """
    if "[" not in title and "]" not in title:
        return title
    return title.replace("[", "(").replace("]", ")")


def assert_within_vault(target: Path, vault_root: Path) -> None:
    """Raise :class:`~brain.errors.VaultPathEscape` if ``target`` escapes the vault.

    Both sides are ``.resolve()``d before comparison, so symlinks are
    followed on the target **and** on the vault root: a vault symlinked
    into iCloud still validates (no false positive), while a symlink
    pointing out of the vault is rejected.

    ``target`` does not have to exist — ``Path.resolve()`` is non-strict on
    3.6+, so this is usable as a pre-write guard on a path that is about to
    be created (``brain note new --folder``, ``brain note move``).

    This is the pure form of the guard; the Typer-flavoured wrapper in
    :mod:`brain.cli_note` catches this exception and re-raises
    ``typer.BadParameter`` so the CLI keeps its usage-error exit code.
    """
    try:
        target.resolve().relative_to(vault_root.resolve())
    except ValueError as e:
        raise VaultPathEscape(
            f"path must stay within the vault; "
            f"{target} resolves outside {vault_root}"
        ) from e
