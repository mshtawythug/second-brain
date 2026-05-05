"""Tests for ``brain.vault.daily_index`` and the P4.1 home-page door wiring.

Two layers under test:

1. ``regenerate_daily_index(vault_path)`` — pure-function content writer.
   Idempotency, ordering, frontmatter preservation, and empty-folder
   behaviour are exercised directly without going through the CLI.
2. The Quartz transformer + ``brain daily`` integration — verified via
   static-source assertions on the TypeScript files (the .ts is a
   template file that runs inside the Quartz workspace, not from the
   brain repo) and a CLI smoke test that the regen helper is called on
   both the existing-note and fresh-note branches of ``brain daily``.

The Quartz transformer ordering check (``EmptyDoorFilter`` after
``CrawlLinks``) is asserted statically against ``quartz.config.ts``;
that's the canonical place the ordering contract lives, and putting
``EmptyDoorFilter`` before ``CrawlLinks`` would silently no-op rather
than crashing — a static check is the only line of defence.
"""
from __future__ import annotations

import re
from collections.abc import Callable
from pathlib import Path
from unittest.mock import patch

import psycopg
import pytest
from typer.testing import CliRunner

from brain import vault as vault_module
from brain.cli import app
from brain.vault.daily_index import regenerate_daily_index
from brain.vault.frontmatter import dump_frontmatter, parse_frontmatter

# Repo-root anchored path for the static .ts source assertions. The
# Quartz overrides are templates installed into a vault's `.quartz/`
# workspace at render time; they don't compile from the brain repo, so
# the assertions are textual rather than via TS imports.
_REPO_ROOT = Path(__file__).resolve().parent.parent
_TRANSFORMER_PATH = (
    _REPO_ROOT
    / "quartz_overrides"
    / "quartz"
    / "plugins"
    / "transformers"
    / "emptyDoorFilter.ts"
)
_QUARTZ_CONFIG_PATH = _REPO_ROOT / "quartz_overrides" / "quartz.config.ts"


# ---------------------------------------------------------------------------
# regenerate_daily_index — pure-function tests (no DB, no CLI)
# ---------------------------------------------------------------------------


def _make_daily(vault: Path, iso_date: str, body: str = "stub body\n") -> Path:
    """Create a ``daily/<YYYY>/<iso_date>.md`` file with minimal frontmatter."""
    year = iso_date.split("-", 1)[0]
    target = vault / "daily" / year / f"{iso_date}.md"
    target.parent.mkdir(parents=True, exist_ok=True)
    text = dump_frontmatter(
        {
            "title": iso_date,
            "tags": ["daily"],
            "kind": "vault",
            "content_type": "note",
        },
        body,
    )
    target.write_text(text, encoding="utf-8")
    return target


def test_regen_daily_index_idempotent_no_op(tmp_path: Path) -> None:
    """Two consecutive regens against the SAME daily set produce a
    byte-identical file on the second pass.

    First call writes the index. Second call sees `body == existing_body`
    and short-circuits before the atomic write — so neither the
    ``updated:`` timestamp nor the file mtime changes.
    """
    vault = tmp_path / "vault"
    _make_daily(vault, "2026-04-29")
    _make_daily(vault, "2026-04-30")
    _make_daily(vault, "2026-05-01")

    assert regenerate_daily_index(vault) is True
    index = vault / "daily" / "index.md"
    assert index.is_file()
    first = index.read_bytes()

    # Second invocation must be a no-op — body matches, regen returns
    # False, and the bytes on disk are identical (no `updated:` bump).
    assert regenerate_daily_index(vault) is False
    second = index.read_bytes()
    assert first == second


def test_regen_daily_index_reverse_chronological_order(tmp_path: Path) -> None:
    """Bullet list must be sorted reverse-chronological by filename.

    Each bullet is a path-form-aliased wiki link of the shape
    ``- [[daily/<year>/<stem>|<stem>]]`` — matches the canonical form
    the link rewriter would produce post-sync, so the round-trip is
    idempotent.
    """
    vault = tmp_path / "vault"
    _make_daily(vault, "2026-04-29")
    _make_daily(vault, "2025-12-25")
    _make_daily(vault, "2026-05-04")

    assert regenerate_daily_index(vault) is True
    text = (vault / "daily" / "index.md").read_text(encoding="utf-8")
    _, body = parse_frontmatter(text)

    # Extract the display stem from each ``- [[<path>|<stem>]]`` bullet
    # in order of appearance.
    stems = re.findall(
        r"-\s+\[\[daily/\d{4}/\d{4}-\d{2}-\d{2}\|(\d{4}-\d{2}-\d{2})\]\]",
        body,
    )
    assert stems == ["2026-05-04", "2026-04-29", "2025-12-25"]


def test_regen_daily_index_empty_daily_folder_no_write(tmp_path: Path) -> None:
    """Empty ``daily/`` ⇒ no ``index.md`` is written.

    A pre-existing ``index.md`` would be left in place, but the canonical
    behaviour for a brand-new empty folder is to skip the write entirely
    so the home-page hide-if-empty transformer is the layer that
    reflects "no dailies" in the UI.
    """
    vault = tmp_path / "vault"
    (vault / "daily").mkdir(parents=True)
    assert regenerate_daily_index(vault) is False
    assert not (vault / "daily" / "index.md").exists()


def test_regen_daily_index_missing_daily_folder_returns_false(tmp_path: Path) -> None:
    """No ``daily/`` folder at all ⇒ regen returns False, no write."""
    vault = tmp_path / "vault"
    vault.mkdir()
    assert regenerate_daily_index(vault) is False
    assert not (vault / "daily" / "index.md").exists()


def test_regen_daily_index_excludes_index_self_reference(tmp_path: Path) -> None:
    """The auto-generated ``index.md`` itself must NOT be hoisted into the
    bullet list on subsequent runs (its name is excluded by stem regex,
    but the safety belt is the ``index.md`` filename guard)."""
    vault = tmp_path / "vault"
    _make_daily(vault, "2026-04-29")
    assert regenerate_daily_index(vault) is True
    # Second run sees `daily/index.md` on disk; must still produce only
    # the one ``- [[2026-04-29]]`` bullet, not two.
    assert regenerate_daily_index(vault) is False
    text = (vault / "daily" / "index.md").read_text(encoding="utf-8")
    _, body = parse_frontmatter(text)
    bullets = re.findall(r"-\s+\[\[", body)
    assert len(bullets) == 1


def test_regen_daily_index_skips_non_daily_filename(tmp_path: Path) -> None:
    """A markdown file under ``daily/`` whose stem is NOT ``YYYY-MM-DD``
    is ignored — the index lists the canonical date-shaped notes only."""
    vault = tmp_path / "vault"
    _make_daily(vault, "2026-04-29")
    rough = vault / "daily" / "2026" / "rough-thoughts.md"
    rough.write_text(
        dump_frontmatter({"title": "Rough thoughts"}, "stray\n"),
        encoding="utf-8",
    )
    assert regenerate_daily_index(vault) is True
    text = (vault / "daily" / "index.md").read_text(encoding="utf-8")
    _, body = parse_frontmatter(text)
    assert "[[daily/2026/2026-04-29|2026-04-29]]" in body
    assert "rough-thoughts" not in body


def test_regen_daily_index_preserves_id_and_created(tmp_path: Path) -> None:
    """Re-running over an existing ``index.md`` keeps ``id`` and ``created``
    stable — only ``updated`` and the body churn when the daily set
    changes.
    """
    vault = tmp_path / "vault"
    _make_daily(vault, "2026-04-29")

    # First run: capture id + created.
    assert regenerate_daily_index(vault) is True
    index_path = vault / "daily" / "index.md"
    fields1, _ = parse_frontmatter(index_path.read_text(encoding="utf-8"))
    first_id = fields1["id"]
    first_created = fields1["created"]
    assert isinstance(first_id, str) and first_id
    assert isinstance(first_created, str) and first_created

    # Add a daily so the body changes — forces a rewrite.
    _make_daily(vault, "2026-05-04")
    assert regenerate_daily_index(vault) is True

    fields2, _ = parse_frontmatter(index_path.read_text(encoding="utf-8"))
    assert fields2["id"] == first_id
    assert fields2["created"] == first_created
    # `updated` keys must exist; we don't pin its exact value (clock).
    assert "updated" in fields2


def test_regen_daily_index_frontmatter_marks_autogenerated(tmp_path: Path) -> None:
    """The autogenerated marker is the user-facing signal "do not hand-edit
    this file"; the test pins it so a future refactor can't drop it
    silently."""
    vault = tmp_path / "vault"
    _make_daily(vault, "2026-04-29")
    assert regenerate_daily_index(vault) is True
    text = (vault / "daily" / "index.md").read_text(encoding="utf-8")
    fields, _ = parse_frontmatter(text)
    assert fields.get("autogenerated") is True
    assert fields.get("title") == "Daily notes"
    assert fields.get("tags") == ["daily", "index"]


def test_regen_daily_index_recursive_year_folder(tmp_path: Path) -> None:
    """A daily nested under ``daily/<YYYY>/`` is found by the recursive walk."""
    vault = tmp_path / "vault"
    _make_daily(vault, "2026-04-29")  # nested under daily/2026/
    flat = vault / "daily" / "2025-12-25.md"
    flat.write_text(
        dump_frontmatter({"title": "2025-12-25"}, "flat\n"),
        encoding="utf-8",
    )
    assert regenerate_daily_index(vault) is True
    text = (vault / "daily" / "index.md").read_text(encoding="utf-8")
    _, body = parse_frontmatter(text)
    # Year-folded daily — path-form alias.
    assert "[[daily/2026/2026-04-29|2026-04-29]]" in body
    # Flat (no year folder) — path is just ``daily/<stem>``.
    assert "[[daily/2025-12-25|2025-12-25]]" in body


# ---------------------------------------------------------------------------
# emptyDoorFilter.ts — static-source assertions
# ---------------------------------------------------------------------------


def test_emptyDoorFilter_options_shape() -> None:
    """The transformer must expose ``folders: string[]`` and
    ``pageSlugs: string[]`` options. The default config protects only
    ``daily/`` and the ``index`` slug — both are wired by
    ``quartz.config.ts`` so a regression there silently disables the
    feature."""
    src = _TRANSFORMER_PATH.read_text(encoding="utf-8")
    assert "folders: string[]" in src
    assert "pageSlugs: string[]" in src
    # Default option values — strict regex so a typo (e.g. "daily" vs
    # "Daily") is caught.
    assert re.search(
        r"const\s+defaultOptions[^=]*=\s*\{[^}]*folders:\s*\[\s*\"daily\"\s*\]",
        src,
        flags=re.DOTALL,
    )
    assert re.search(
        r"pageSlugs:\s*\[\s*\"index\"\s*\]",
        src,
    )


def test_emptyDoorFilter_walks_hast_tree() -> None:
    """Sanity check: the implementation walks the rehype hast tree
    (`tagName === "li"`, `tagName === "a"`, `properties.href`)."""
    src = _TRANSFORMER_PATH.read_text(encoding="utf-8")
    assert 'tagName === "li"' in src
    assert 'tagName === "a"' in src
    assert '"href"' in src or "['href']" in src
    # Recursive `<li>` filter signature — distinguishes this transformer's
    # contract (drop only matching list items, walk into others).
    assert "stripEmptyDoorListItems" in src


def test_emptyDoorFilter_uses_ctx_argv_directory() -> None:
    """The transformer reads the vault root via Quartz's
    ``ctx.argv.directory`` once per build. If a future Quartz version
    renames this field the failure here will pin the breakage to this
    spot rather than a silent mis-classification at runtime."""
    src = _TRANSFORMER_PATH.read_text(encoding="utf-8")
    assert "ctx.argv.directory" in src


def test_emptyDoorFilter_registered_after_CrawlLinks() -> None:
    """``Plugin.EmptyDoorFilter()`` MUST appear AFTER ``Plugin.CrawlLinks``
    in the transformer list — otherwise it walks unresolved markdown URLs
    instead of rehype-resolved ``<a href>`` strings and silently no-ops.

    Static check on ``quartz.config.ts``: substring index of
    ``Plugin.CrawlLinks(`` must come before ``Plugin.EmptyDoorFilter(``.
    """
    src = _QUARTZ_CONFIG_PATH.read_text(encoding="utf-8")
    crawl_idx = src.find("Plugin.CrawlLinks(")
    empty_idx = src.find("Plugin.EmptyDoorFilter(")
    assert crawl_idx != -1, "Plugin.CrawlLinks not registered"
    assert empty_idx != -1, "Plugin.EmptyDoorFilter not registered"
    assert crawl_idx < empty_idx, (
        f"EmptyDoorFilter registered before CrawlLinks "
        f"(crawl={crawl_idx}, empty={empty_idx}) — would silently no-op."
    )


def test_emptyDoorFilter_exported_from_transformer_barrel() -> None:
    """The barrel re-export ``index.ts`` must surface ``EmptyDoorFilter``
    under ``Plugin.*`` so ``quartz.config.ts`` can register it."""
    barrel = (
        _REPO_ROOT
        / "quartz_overrides"
        / "quartz"
        / "plugins"
        / "transformers"
        / "index.ts"
    ).read_text(encoding="utf-8")
    assert 'export { EmptyDoorFilter } from "./emptyDoorFilter"' in barrel


# ---------------------------------------------------------------------------
# brain daily — CLI integration: regen helper called on both branches
# ---------------------------------------------------------------------------


def test_brain_daily_calls_refresh_on_fresh_note(
    test_db: psycopg.Connection,
    tmp_path: Path,
    fake_embedder: object,
    patch_embedder: Callable[[object], None],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``brain daily`` on a fresh date triggers ``_refresh_daily_index``
    so the index file lands on disk alongside today's note."""
    patch_embedder(fake_embedder)
    vault = tmp_path / "vault"
    vault_module.init_vault(vault)
    monkeypatch.setenv("BRAIN_VAULT_PATH", str(vault))

    result = CliRunner().invoke(
        app, ["daily", "--date", "2026-04-29", "--no-edit"]
    )
    assert result.exit_code == 0, result.output

    # Today's daily and the autogen index both exist.
    assert (vault / "daily" / "2026" / "2026-04-29.md").is_file()
    index = vault / "daily" / "index.md"
    assert index.is_file(), "P4.1: daily index should be auto-generated"
    fields, body = parse_frontmatter(index.read_text(encoding="utf-8"))
    assert fields.get("autogenerated") is True
    # The regen emits the canonical path-form alias directly so the
    # post-sync body matches what regen would produce, keeping
    # consecutive ``brain daily`` invocations idempotent.
    assert "[[daily/2026/2026-04-29|2026-04-29]]" in body


def test_brain_daily_calls_refresh_on_existing_note(
    test_db: psycopg.Connection,
    tmp_path: Path,
    fake_embedder: object,
    patch_embedder: Callable[[object], None],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The repair use case: user deletes ``daily/index.md`` and re-runs
    ``brain daily`` on a date that already exists. The "existing note"
    branch must call the refresh helper so the index regenerates.
    Without this, ``brain daily`` would only ever write the index on
    fresh-note days."""
    patch_embedder(fake_embedder)
    vault = tmp_path / "vault"
    vault_module.init_vault(vault)
    monkeypatch.setenv("BRAIN_VAULT_PATH", str(vault))
    runner = CliRunner()

    # Day 1: fresh note — populates index.md too.
    runner.invoke(app, ["daily", "--date", "2026-04-29", "--no-edit"])
    index = vault / "daily" / "index.md"
    assert index.is_file()
    # Simulate the user (or a migration) deleting the index.
    index.unlink()
    assert not index.exists()

    # Day 2: re-run for the SAME date — exercises the existing-note branch.
    result = runner.invoke(app, ["daily", "--date", "2026-04-29", "--no-edit"])
    assert result.exit_code == 0, result.output
    assert "opened" in result.output
    assert "(existing)" in result.output
    # The repair: index.md is back, regenerated from the dailies on disk.
    assert index.is_file(), (
        "P4.1: re-running brain daily on an existing date must repair "
        "a missing daily/index.md (existing-note branch path)"
    )


def test_brain_daily_index_byte_stable_across_runs(
    test_db: psycopg.Connection,
    tmp_path: Path,
    fake_embedder: object,
    patch_embedder: Callable[[object], None],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """End-to-end idempotency: ``brain daily`` invoked twice on the same
    date produces a byte-identical ``daily/index.md`` on the second
    pass.

    Regression for the bare-stem-vs-path-form bug: an earlier version
    of the regen wrote ``[[<stem>]]`` bullets, and the post-write
    sync's link rewriter mutated them to
    ``[[daily/<year>/<stem>|<stem>]]``. The next ``brain daily`` then
    saw a body mismatch (regen wanted bare stem, disk had path form),
    rewrote the index, bumped ``updated:``, and triggered a watcher
    rebuild on every invocation. The fix emits the path-form alias
    directly so the round-trip is stable from the second run onwards.
    """
    patch_embedder(fake_embedder)
    vault = tmp_path / "vault"
    vault_module.init_vault(vault)
    monkeypatch.setenv("BRAIN_VAULT_PATH", str(vault))
    runner = CliRunner()

    runner.invoke(app, ["daily", "--date", "2026-04-29", "--no-edit"])
    index = vault / "daily" / "index.md"
    assert index.is_file()
    first = index.read_bytes()

    runner.invoke(app, ["daily", "--date", "2026-04-29", "--no-edit"])
    second = index.read_bytes()
    assert first == second, (
        "P4.1: brain daily must be byte-idempotent across invocations "
        "for the same date — sync's link rewriter and the regen's link "
        "form must agree on the canonical path-form shape."
    )


def test_brain_daily_invokes_regenerate_helper(
    test_db: psycopg.Connection,
    tmp_path: Path,
    fake_embedder: object,
    patch_embedder: Callable[[object], None],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Wraps ``regenerate_daily_index`` to count invocations from the CLI
    end-to-end. Asserts the helper fires on BOTH the fresh-note and the
    existing-note code paths.

    ``unittest.mock.patch`` is a standard test double (not monkey-patching
    in the CLAUDE.md sense — auto-cleanup, no module reopening).
    """
    patch_embedder(fake_embedder)
    vault = tmp_path / "vault"
    vault_module.init_vault(vault)
    monkeypatch.setenv("BRAIN_VAULT_PATH", str(vault))
    runner = CliRunner()

    with patch(
        "brain.cli.regenerate_daily_index", wraps=regenerate_daily_index
    ) as spy:
        # Fresh-note branch — counts as 1 invocation.
        result1 = runner.invoke(
            app, ["daily", "--date", "2026-04-29", "--no-edit"]
        )
        assert result1.exit_code == 0, result1.output
        assert spy.call_count == 1

        # Existing-note branch — counts as 2nd invocation.
        result2 = runner.invoke(
            app, ["daily", "--date", "2026-04-29", "--no-edit"]
        )
        assert result2.exit_code == 0, result2.output
        assert spy.call_count == 2
