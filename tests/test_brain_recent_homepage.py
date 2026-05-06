"""Tests for ``brain.wiki.build_homepage`` (P4.7 home-page recent rail).

Three layers under test:

1. ``regenerate_recent_partial`` — writes/skips ``<vault>/_partials/recent.md``
   from a synthetic doc list. Pure-function: no DB, no fence parsing.
2. ``regenerate_recent_fence`` — replaces the ``<!-- BRAIN_RECENT_START -->`` /
   ``<!-- BRAIN_RECENT_END -->`` markers in ``<vault>/index.md`` with the
   rendered bullets. Tests cover present-marker / missing-marker /
   idempotent-rewrite paths.
3. ``refresh_homepage`` — DB-backed integration. Uses the project's
   ``test_db`` fixture + the ``seed_doc`` factory to land real rows, then
   asserts the partial + fence reflect them with the correct ordering /
   draft skip / 12-doc cap.

Match the conventions of ``tests/test_daily_index.py`` (P4.1): pure helpers
exercised first against ``tmp_path``, then DB-backed integration with the
shared fixtures from ``tests/conftest.py``.
"""
from __future__ import annotations

import datetime
import logging
from collections.abc import Callable
from pathlib import Path
from typing import Any

import psycopg
import pytest

from brain.vault.frontmatter import dump_frontmatter, parse_frontmatter
from brain.wiki.build_homepage import (
    FENCE_END_MARKER,
    FENCE_START_MARKER,
    RECENT_LIMIT,
    RecentDoc,
    _fetch_recent_docs,
    _format_relative_date,
    _render_bullets,
    _replace_fence,
    _safe_alias,
    _strip_md_extension,
    refresh_homepage,
    regenerate_recent_fence,
    regenerate_recent_partial,
)

# --------------------------------------------------------------------------
# Helpers
# --------------------------------------------------------------------------


def _doc(
    *,
    title: str = "Stub doc",
    source_kind: str | None = "manual",
    days_ago: int = 0,
    vault_path: str = "_ingested/manual/stub.md",
) -> RecentDoc:
    """Build a ``RecentDoc`` with a date offset N days from today.

    Defaults match the simplest seed; tests override per-case.
    """
    today = datetime.datetime.now().astimezone()
    return RecentDoc(
        title=title,
        source_kind=source_kind,
        ingested_at=today - datetime.timedelta(days=days_ago),
        vault_path=vault_path,
    )


def _make_index(
    vault: Path, *, with_fence: bool = True, body_extra: str = ""
) -> Path:
    """Create a minimal home note at ``<vault>/index.md``."""
    vault.mkdir(parents=True, exist_ok=True)
    fields = {
        "id": "00000000-0000-0000-0000-000000000001",
        "title": "Second Brain",
        "kind": "vault",
        "content_type": "note",
    }
    if with_fence:
        body = (
            "Welcome.\n\n## Recently captured\n\n"
            f"{FENCE_START_MARKER}\n"
            "- (placeholder)\n"
            f"{FENCE_END_MARKER}\n\n"
            f"{body_extra}"
        )
    else:
        body = "Welcome — no fence here.\n\n" + body_extra
    target = vault / "index.md"
    target.write_text(dump_frontmatter(fields, body), encoding="utf-8")
    return target


# --------------------------------------------------------------------------
# Pure helpers — no DB, no FS.
# --------------------------------------------------------------------------


def test_safe_alias_strips_brackets() -> None:
    """Brackets in titles would break Quartz wiki-link parsing."""
    assert _safe_alias("Re: [External] Re: foo") == "Re: (External) Re: foo"
    assert _safe_alias("plain title") == "plain title"


def test_strip_md_extension_removes_trailing_md() -> None:
    """Vault-paths are stored with ``.md``; wiki-link targets must not have it."""
    assert _strip_md_extension("_ingested/gmail/foo.md") == "_ingested/gmail/foo"
    assert _strip_md_extension("hubs/company-id.md") == "hubs/company-id"
    # Already stripped (defensive) — leave untouched.
    assert _strip_md_extension("hubs/company-id") == "hubs/company-id"


def test_format_relative_date_buckets() -> None:
    """All four buckets — today, Nd, Nw, "Mon D" — render as documented."""
    today = datetime.date(2026, 5, 4)

    def at(days: int) -> datetime.datetime:
        d = today - datetime.timedelta(days=days)
        return datetime.datetime(d.year, d.month, d.day, 12, 0, 0)

    assert _format_relative_date(at(0), today=today) == "today"
    assert _format_relative_date(at(1), today=today) == "1d ago"
    assert _format_relative_date(at(6), today=today) == "6d ago"
    assert _format_relative_date(at(7), today=today) == "1w ago"
    assert _format_relative_date(at(34), today=today) == "4w ago"
    # 35+ days ago → "Mon D"
    older = at(60)
    rendered = _format_relative_date(older, today=today)
    assert rendered.split()[0] in {"Jan", "Feb", "Mar"}
    # Future date (clock skew) buckets as today.
    future = datetime.datetime(today.year + 1, today.month, today.day, 12, 0, 0)
    assert _format_relative_date(future, today=today) == "today"


def test_render_bullets_empty_corpus_emits_placeholder() -> None:
    """Empty docs list renders an italic placeholder, not a blank string."""
    out = _render_bullets([])
    assert out.endswith("\n")
    assert "*" in out
    assert "ingest" in out.lower()


def test_render_bullets_uses_correct_icons() -> None:
    """Each source kind picks the documented glyph; unknown → vault default."""
    docs = [
        _doc(title="g", source_kind="gmail", vault_path="_ingested/gmail/g.md"),
        _doc(title="k", source_kind="krisp", vault_path="_ingested/krisp/k.md"),
        _doc(title="s", source_kind="slack", vault_path="_ingested/slack/s.md"),
        _doc(title="m", source_kind="manual", vault_path="_ingested/manual/m.md"),
        _doc(title="v", source_kind=None, vault_path="hubs/v.md"),
        _doc(title="u", source_kind="unknown_src", vault_path="hubs/u.md"),
    ]
    out = _render_bullets(docs)
    assert "📧 [[_ingested/gmail/g|g]]" in out
    assert "🎙️ [[_ingested/krisp/k|k]]" in out
    assert "💬 [[_ingested/slack/s|s]]" in out
    assert "✍️ [[_ingested/manual/m|m]]" in out
    # source_kind=None and unknown both fall back to 🌱 (vault default).
    assert "🌱 [[hubs/v|v]]" in out
    assert "🌱 [[hubs/u|u]]" in out


def test_render_bullets_strips_brackets_from_alias() -> None:
    """Bracketed Gmail subjects must not break the wiki-link alias."""
    docs = [_doc(title="Re: [External] foo", vault_path="_ingested/gmail/x.md")]
    out = _render_bullets(docs)
    assert "[[_ingested/gmail/x|Re: (External) foo]]" in out


# --------------------------------------------------------------------------
# _replace_fence — pure-string contract.
# --------------------------------------------------------------------------


def test_replace_fence_swaps_inner_when_markers_present() -> None:
    body = (
        "intro\n\n"
        f"{FENCE_START_MARKER}\nold inner\n{FENCE_END_MARKER}\n\nouter\n"
    )
    rewritten = _replace_fence(body, "new inner\n")
    assert rewritten is not None
    assert "old inner" not in rewritten
    assert "new inner" in rewritten
    # Markers themselves are preserved verbatim.
    assert FENCE_START_MARKER in rewritten
    assert FENCE_END_MARKER in rewritten
    # Outer content survives both before and after.
    assert rewritten.startswith("intro\n\n")
    assert rewritten.endswith("outer\n")


def test_replace_fence_returns_none_when_markers_missing() -> None:
    assert _replace_fence("no fence here\n", "x\n") is None
    # END but no START → treated as missing fence.
    assert _replace_fence(f"only {FENCE_END_MARKER}\n", "x\n") is None
    # START but no END → also missing fence.
    assert _replace_fence(f"only {FENCE_START_MARKER}\n", "x\n") is None


# --------------------------------------------------------------------------
# regenerate_recent_partial — file write contract.
# --------------------------------------------------------------------------


def test_partial_writes_rendered_bullets(tmp_path: Path) -> None:
    docs = [
        _doc(title="A", source_kind="gmail", vault_path="_ingested/gmail/a.md"),
        _doc(title="B", source_kind="krisp", vault_path="_ingested/krisp/b.md"),
    ]
    changed = regenerate_recent_partial(tmp_path, docs=docs)
    assert changed is True
    out = (tmp_path / "_partials" / "recent.md").read_text(encoding="utf-8")
    assert "📧 [[_ingested/gmail/a|A]]" in out
    assert "🎙️ [[_ingested/krisp/b|B]]" in out


def test_partial_idempotent_byte_stable(tmp_path: Path) -> None:
    """Two consecutive regens with the same input write byte-identical files
    and the second call returns False (no-op preserves mtime)."""
    docs = [
        _doc(title="A", source_kind="gmail", vault_path="_ingested/gmail/a.md"),
    ]
    assert regenerate_recent_partial(tmp_path, docs=docs) is True
    first = (tmp_path / "_partials" / "recent.md").read_bytes()
    # Second call: content identical → write skipped.
    assert regenerate_recent_partial(tmp_path, docs=docs) is False
    second = (tmp_path / "_partials" / "recent.md").read_bytes()
    assert first == second


def test_partial_creates_parent_dir(tmp_path: Path) -> None:
    """First-ever call on a fresh vault must mkdir _partials/ on demand."""
    assert not (tmp_path / "_partials").exists()
    docs = [_doc()]
    assert regenerate_recent_partial(tmp_path, docs=docs) is True
    assert (tmp_path / "_partials").is_dir()
    assert (tmp_path / "_partials" / "recent.md").is_file()


def test_partial_empty_corpus_writes_placeholder(tmp_path: Path) -> None:
    """An empty doc list still writes the partial — placeholder body."""
    assert regenerate_recent_partial(tmp_path, docs=[]) is True
    body = (tmp_path / "_partials" / "recent.md").read_text(encoding="utf-8")
    assert "ingest" in body.lower()


# --------------------------------------------------------------------------
# regenerate_recent_fence — home-note rewrite contract.
# --------------------------------------------------------------------------


def test_fence_replaces_between_markers(tmp_path: Path) -> None:
    _make_index(tmp_path, with_fence=True)
    docs = [
        _doc(title="A", source_kind="gmail", vault_path="_ingested/gmail/a.md"),
    ]
    assert regenerate_recent_fence(tmp_path, docs=docs) is True
    text = (tmp_path / "index.md").read_text(encoding="utf-8")
    fields, body = parse_frontmatter(text)
    # Fence rewritten — placeholder gone, new bullet present, markers preserved.
    assert "(placeholder)" not in body
    assert "📧 [[_ingested/gmail/a|A]]" in body
    assert FENCE_START_MARKER in body
    assert FENCE_END_MARKER in body
    # Frontmatter survived intact (id + title preserved).
    assert fields["id"] == "00000000-0000-0000-0000-000000000001"
    assert fields["title"] == "Second Brain"


def test_fence_no_op_when_markers_missing(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """Index without the fence: no rewrite, warning logged."""
    target = _make_index(tmp_path, with_fence=False)
    before = target.read_bytes()
    with caplog.at_level(logging.WARNING, logger="brain.wiki.build_homepage"):
        assert regenerate_recent_fence(tmp_path, docs=[_doc()]) is False
    # File untouched.
    assert target.read_bytes() == before
    # Warning surfaced — message mentions both markers.
    assert any(
        FENCE_START_MARKER in r.message and FENCE_END_MARKER in r.message
        for r in caplog.records
    )


def test_fence_no_op_when_index_missing(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """No home note at all → return False, no crash, warning logged."""
    with caplog.at_level(logging.WARNING, logger="brain.wiki.build_homepage"):
        assert regenerate_recent_fence(tmp_path, docs=[_doc()]) is False
    # Records mention "home note".
    assert any("home note" in r.message for r in caplog.records)


def test_fence_idempotent_byte_stable(tmp_path: Path) -> None:
    """Two regens against the same DB state ⇒ byte-identical home note."""
    target = _make_index(tmp_path, with_fence=True)
    docs = [_doc(title="A", vault_path="_ingested/manual/a.md")]
    assert regenerate_recent_fence(tmp_path, docs=docs) is True
    first = target.read_bytes()
    # Second call: rendered content unchanged → write skipped.
    assert regenerate_recent_fence(tmp_path, docs=docs) is False
    assert target.read_bytes() == first


# --------------------------------------------------------------------------
# DB-backed integration — uses test_db + seed_doc fixtures from conftest.
# --------------------------------------------------------------------------


def test_fetch_recent_docs_orders_by_ingested_at_desc(
    test_db: psycopg.Connection, seed_doc: Callable[..., str]
) -> None:
    """Newest first. Uses seeded ingest order = ingest time order in tests."""
    seed_doc(title="oldest", content="o")
    seed_doc(title="middle", content="m")
    seed_doc(title="newest", content="n")
    # Ensure all three landed and have a vault_path. The seed_doc fixture
    # uses ingest_document which sets vault_path via auto-mirror; if the
    # test fixture path doesn't, we materialize it ourselves.
    test_db.execute(
        "UPDATE documents SET vault_path = '_ingested/manual/' || "
        "regexp_replace(lower(title), '[^a-z0-9]+', '-', 'g') || '.md' "
        "WHERE vault_path IS NULL"
    )
    docs = _fetch_recent_docs(test_db, limit=10)
    titles = [d.title for d in docs]
    assert titles == ["newest", "middle", "oldest"]


def test_fetch_recent_docs_skips_drafts(
    test_db: psycopg.Connection, seed_doc: Callable[..., str]
) -> None:
    """``draft = TRUE`` rows are excluded at the SQL layer."""
    keep_id = seed_doc(title="keep", content="k")
    draft_id = seed_doc(title="hidden draft", content="d")
    test_db.execute(
        "UPDATE documents SET vault_path = '_ingested/manual/' || "
        "regexp_replace(lower(title), '[^a-z0-9]+', '-', 'g') || '.md' "
    )
    test_db.execute(
        "UPDATE documents SET draft = TRUE WHERE id = %s", (draft_id,)
    )
    docs = _fetch_recent_docs(test_db, limit=10)
    titles = [d.title for d in docs]
    assert "keep" in titles
    assert "hidden draft" not in titles
    # Sanity: keep_id row is the one we expect.
    assert any(d.title == "keep" for d in docs), keep_id


def test_fetch_recent_docs_skips_null_vault_path(
    test_db: psycopg.Connection, seed_doc: Callable[..., str]
) -> None:
    """Rows whose ``vault_path`` is NULL aren't browseable from the wiki."""
    visible_id = seed_doc(title="visible", content="v")
    hidden_id = seed_doc(title="no_path", content="h")
    test_db.execute(
        "UPDATE documents SET vault_path = '_ingested/manual/visible.md' "
        "WHERE id = %s", (visible_id,)
    )
    test_db.execute(
        "UPDATE documents SET vault_path = NULL WHERE id = %s", (hidden_id,)
    )
    docs = _fetch_recent_docs(test_db, limit=10)
    titles = [d.title for d in docs]
    assert "visible" in titles
    assert "no_path" not in titles


def test_fetch_recent_docs_caps_at_limit(
    test_db: psycopg.Connection, seed_doc: Callable[..., str]
) -> None:
    """Asking for 12 returns at most 12 even with 15 eligible rows."""
    for i in range(15):
        doc_id = seed_doc(title=f"doc_{i:02d}", content=f"body_{i}")
        test_db.execute(
            "UPDATE documents SET vault_path = %s WHERE id = %s",
            (f"_ingested/manual/doc_{i:02d}.md", doc_id),
        )
    docs = _fetch_recent_docs(test_db, limit=RECENT_LIMIT)
    assert len(docs) == RECENT_LIMIT


def test_refresh_homepage_writes_partial_and_fence(
    test_db: psycopg.Connection,
    seed_doc: Callable[..., str],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """End-to-end: seed DB, point cfg at tmp vault, refresh, verify both files."""
    # Seed three docs and stamp vault_paths so they're eligible.
    for name in ("alpha", "bravo", "charlie"):
        doc_id = seed_doc(title=name, content=f"{name}_body")
        test_db.execute(
            "UPDATE documents SET vault_path = %s WHERE id = %s",
            (f"_ingested/manual/{name}.md", doc_id),
        )

    vault = tmp_path / "vault"
    _make_index(vault, with_fence=True)

    # Build a Config pointing at the test DB + the throwaway vault.
    # ``test_db.info.dsn`` omits the password; pull the canonical URL from
    # conftest's session-level fixture so refresh_homepage can re-open the
    # connection with full credentials.
    from brain.config import Config
    from tests.conftest import TEST_DATABASE_URL

    cfg = Config(
        database_url=TEST_DATABASE_URL,
        vault_path=vault,
    )
    partial_changed, fence_changed = refresh_homepage(cfg)
    assert partial_changed is True
    assert fence_changed is True

    partial = (vault / "_partials" / "recent.md").read_text(encoding="utf-8")
    assert "alpha" in partial
    assert "bravo" in partial
    assert "charlie" in partial

    fields, body = parse_frontmatter(
        (vault / "index.md").read_text(encoding="utf-8")
    )
    assert "alpha" in body
    assert FENCE_START_MARKER in body
    assert FENCE_END_MARKER in body


def test_refresh_homepage_swallows_db_error(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """A bad DSN must NOT raise — refresh logs and returns (False, False).

    The build is the customer; the rail is a courtesy. A DB outage cannot
    fail a build.
    """
    from brain.config import Config

    vault = tmp_path / "vault"
    _make_index(vault, with_fence=True)
    cfg = Config(
        database_url="postgresql://no:no@localhost:1/no_such_db",
        vault_path=vault,
    )
    with caplog.at_level(logging.WARNING, logger="brain.wiki.build_homepage"):
        result = refresh_homepage(cfg)
    assert result == (False, False)
    assert any("DB query failed" in r.message for r in caplog.records)


# --------------------------------------------------------------------------
# Static contract: the partial markdown is what the build pipeline expects.
# --------------------------------------------------------------------------


def test_recent_limit_constant_is_twelve() -> None:
    """The plan pins 12; the constant must match. A surprise change here
    would silently skew the home page; lock it explicitly."""
    assert RECENT_LIMIT == 12


def test_quartz_config_ignores_partials_dir() -> None:
    """``_partials/`` MUST be in ``ignorePatterns`` — otherwise the partial
    becomes its own Quartz page (the home rail would render twice)."""
    cfg_path = (
        Path(__file__).resolve().parent.parent
        / "quartz_overrides"
        / "quartz.config.ts"
    )
    src = cfg_path.read_text(encoding="utf-8")
    # The pattern lives inside the ``ignorePatterns: [...]`` array; assert
    # by substring rather than parsing TypeScript.
    assert '"_partials"' in src, (
        "P4.7: _partials/ must be in quartz.config.ts ignorePatterns or the "
        "server-rendered recent.md will leak into the public site as a page."
    )


def test_build_and_swap_calls_refresh_homepage(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Wire test: ``build_and_swap`` invokes refresh_homepage before Quartz.

    This covers watcher/direct build callers, not only the CLI wrapper.
    """
    import brain.wiki.build_swap as bs

    refresh_calls: list[Any] = []

    def fake_refresh(cfg: Any) -> tuple[bool, bool]:
        refresh_calls.append(cfg)
        return (True, True)

    def fake_run_build(*args: Any, **kwargs: Any) -> None:
        build_dir = kwargs["build_dir"]
        build_dir.mkdir(parents=True)
        (build_dir / "index.html").write_text("", encoding="utf-8")

    monkeypatch.setattr(
        "brain.wiki.build_homepage.refresh_homepage", fake_refresh
    )
    monkeypatch.setattr(
        "brain.wiki.build_related.refresh_related",
        lambda _cfg: None,
    )
    monkeypatch.setattr(bs, "_probe_build_method", lambda *a, **kw: "output-flag")
    monkeypatch.setattr(bs, "_run_build", fake_run_build)

    monkeypatch.setenv("DATABASE_URL", "postgresql://x:x@localhost:5432/x")
    vault = tmp_path / "vault"
    vault.mkdir()
    quartz = vault / ".quartz"
    quartz.mkdir()
    (quartz / "quartz.config.ts").write_text("", encoding="utf-8")
    monkeypatch.setenv("BRAIN_VAULT_PATH", str(vault))

    bs.build_and_swap(vault, quartz_dir=quartz)

    assert len(refresh_calls) == 1
    # The cfg passed to refresh has vault_path == the resolved CLI vault.
    assert refresh_calls[0].vault_path == vault.resolve()
