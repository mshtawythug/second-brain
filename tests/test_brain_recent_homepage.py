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
from brain.vault.paths import safe_wikilink_alias, strip_md_extension
from brain.wiki.build_homepage import (
    FENCE_END_MARKER,
    FENCE_START_MARKER,
    RECENT_LIMIT,
    RecentDoc,
    _fetch_recent_docs,
    _format_absolute_date,
    _format_relative_date,
    _render_bullets,
    _replace_fence,
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
    """Build a ``RecentDoc`` with a ``display_date`` offset N days from today.

    Defaults match the simplest seed; tests override per-case.
    """
    today = datetime.datetime.now().astimezone()
    return RecentDoc(
        title=title,
        source_kind=source_kind,
        display_date=today - datetime.timedelta(days=days_ago),
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


def test_safe_wikilink_alias_strips_brackets() -> None:
    """Brackets in titles would break Quartz wiki-link parsing."""
    assert safe_wikilink_alias("Re: [External] Re: foo") == "Re: (External) Re: foo"
    assert safe_wikilink_alias("plain title") == "plain title"


def test_strip_md_extension_removes_trailing_md() -> None:
    """Vault-paths are stored with ``.md``; wiki-link targets must not have it."""
    assert strip_md_extension("_ingested/gmail/foo.md") == "_ingested/gmail/foo"
    assert strip_md_extension("hubs/company-id.md") == "hubs/company-id"
    # Already stripped (defensive) — leave untouched.
    assert strip_md_extension("hubs/company-id") == "hubs/company-id"


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


def test_format_relative_date_boundary_table() -> None:
    """Pin every bucket boundary so the Python side can't drift.

    The client-side mirror (``static/relativeDate.js``) is exercised by the
    e2e build; this table locks the source-of-truth buckets the JS copies:
    0 → today, 1/6 → "Nd ago", 7/13/34 → "Nw ago", 35 → absolute, and a
    future date → today.
    """
    today = datetime.date(2026, 6, 23)

    def at(days: int) -> datetime.datetime:
        d = today - datetime.timedelta(days=days)
        return datetime.datetime(d.year, d.month, d.day, 12, 0, 0)

    cases = {
        0: "today",
        1: "1d ago",
        6: "6d ago",
        7: "1w ago",
        13: "1w ago",
        34: "4w ago",
    }
    for days, expected in cases.items():
        assert _format_relative_date(at(days), today=today) == expected, days
    # 35 days → first day that rolls into the absolute "Mon D" branch.
    assert _format_relative_date(at(35), today=today) == _format_absolute_date(at(35))
    # Future date buckets as today.
    future = datetime.datetime(today.year + 1, 1, 1, 12, 0, 0)
    assert _format_relative_date(future, today=today) == "today"


def test_format_absolute_date_no_leading_zero() -> None:
    """``_format_absolute_date`` renders a portable ``"Mon D"`` (no zero-pad)."""
    when = datetime.datetime(2026, 6, 5, 9, 30, 0)
    assert _format_absolute_date(when) == "Jun 5"
    # Two-digit day stays two digits.
    assert _format_absolute_date(datetime.datetime(2026, 1, 10, 0, 0, 0)) == "Jan 10"
    # The >= 35-day relative branch must agree with the absolute helper.
    today = datetime.date(2026, 6, 23)
    old = datetime.datetime(2026, 1, 10, 12, 0, 0)
    assert _format_relative_date(old, today=today) == "Jan 10"


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


def test_render_bullets_emits_machine_readable_date_span() -> None:
    """Each bullet ends with a ``.brain-rel-date`` span, not a baked string.

    The span carries the ISO ``data-date`` (machine-readable source of
    truth, recomputed client-side) and a NON-decaying absolute fallback
    ("Jun 10") as its visible text. This is the fix for the decaying-string
    bug: nothing relative ("today" / "1d ago") may be baked into the body.
    """
    ingested = datetime.datetime(2026, 6, 10, 14, 30, 0, tzinfo=datetime.UTC)
    docs = [
        RecentDoc(
            title="Span doc",
            source_kind="manual",
            display_date=ingested,
            vault_path="_ingested/manual/span.md",
        )
    ]
    out = _render_bullets(docs)
    # The span class hook + machine-readable ISO attribute are present.
    assert 'class="brain-rel-date"' in out
    assert f'data-date="{ingested.isoformat()}"' in out
    # The inner text is the absolute fallback, not a relative literal.
    expected_abs = _format_absolute_date(ingested)
    assert f">{expected_abs}</span>" in out
    # Full span shape, exact.
    assert (
        f'<span class="brain-rel-date" data-date="{ingested.isoformat()}">'
        f"{expected_abs}</span>"
    ) in out


def test_render_bullets_bakes_no_decaying_relative_literal() -> None:
    """No decaying relative phrase is baked into the rendered bullet.

    Regression for the home-rail decay bug: a doc rendered N days before a
    build used to read "Nd ago" forever. The body must now carry only the
    absolute date — the relative phrase is computed client-side.
    """
    # Use a "1 day ago" doc — the old code would have baked literally "1d ago".
    ingested = datetime.datetime.now().astimezone() - datetime.timedelta(days=1)
    docs = [
        RecentDoc(
            title="Yesterday doc",
            source_kind="manual",
            display_date=ingested,
            vault_path="_ingested/manual/yest.md",
        )
    ]
    out = _render_bullets(docs)
    assert "1d ago" not in out
    assert "today" not in out
    assert "ago" not in out
    # But the machine-readable date IS present for the client to recompute.
    assert 'class="brain-rel-date"' in out


def test_quartz_config_registers_relative_date_plugin() -> None:
    """``Plugin.RelativeDate()`` must be wired into the transformers list."""
    cfg_path = (
        Path(__file__).resolve().parent.parent
        / "src" / "brain" / "quartz_overrides"
        / "quartz.config.ts"
    )
    src = cfg_path.read_text(encoding="utf-8")
    assert "Plugin.RelativeDate()" in src, (
        "the live relative-date transformer must be registered in "
        "quartz.config.ts so /static/relativeDate.js is injected"
    )


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


def test_fetch_recent_docs_orders_by_display_date_desc(
    test_db: psycopg.Connection, seed_doc: Callable[..., str]
) -> None:
    """Newest first by display date. Manual seeds have NULL ``sent_at``, so
    ``doc_date`` falls back to ``ingested_at`` and ingest order == display
    order here."""
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


def test_fetch_recent_docs_excludes_home_note(
    test_db: psycopg.Connection, seed_doc: Callable[..., str]
) -> None:
    """The home note (``vault_path='index.md'``) must not list itself.

    Regression: the rail lives inside ``index.md`` and the pipeline re-stamps
    that row's ``ingested_at`` on every derived-page regen, so without the
    explicit exclusion the home note would sit permanently at the top of its
    own rail.
    """
    home_id = seed_doc(title="Second Brain", content="home")
    keep_id = seed_doc(title="real doc", content="r")
    test_db.execute(
        "UPDATE documents SET vault_path = 'index.md' WHERE id = %s", (home_id,)
    )
    test_db.execute(
        "UPDATE documents SET vault_path = '_ingested/manual/real-doc.md' "
        "WHERE id = %s",
        (keep_id,),
    )
    docs = _fetch_recent_docs(test_db, limit=10)
    paths = [d.vault_path for d in docs]
    titles = [d.title for d in docs]
    assert "index.md" not in paths
    assert "Second Brain" not in titles
    assert "real doc" in titles


def test_fetch_recent_docs_excludes_people_hub_pages(
    test_db: psycopg.Connection, seed_doc: Callable[..., str]
) -> None:
    """People-Hub auto-pages (``people/*``) must not flood the rail.

    Regression: ``emit_people_pages`` writes every page under ``people/`` and
    re-stamps ``ingested_at = now()`` on each regen. Without the
    ``NOT LIKE 'people/%'`` exclusion, a Krisp/Slack batch + People-Hub regen
    would make the 12 newest rows ALL auto-generated person/index pages.
    """
    person_id = seed_doc(title="Pat Roster", content="p")
    index_id = seed_doc(title="People", content="i")
    keep_id = seed_doc(title="genuine note", content="g")
    test_db.execute(
        "UPDATE documents SET vault_path = 'people/pat-roster.md' WHERE id = %s",
        (person_id,),
    )
    test_db.execute(
        "UPDATE documents SET vault_path = 'people/index.md' WHERE id = %s",
        (index_id,),
    )
    test_db.execute(
        "UPDATE documents SET vault_path = '_ingested/manual/genuine-note.md' "
        "WHERE id = %s",
        (keep_id,),
    )
    docs = _fetch_recent_docs(test_db, limit=10)
    paths = [d.vault_path for d in docs]
    titles = [d.title for d in docs]
    assert not any(p.startswith("people/") for p in paths)
    assert "Pat Roster" not in titles
    assert "People" not in titles
    assert "genuine note" in titles


def test_fetch_recent_docs_ranks_and_renders_by_event_date(
    test_db: psycopg.Connection, seed_doc: Callable[..., str]
) -> None:
    """A doc whose event date (``doc_date``) predates its ``ingested_at`` ranks
    and renders by the EVENT date, not the processing timestamp.

    Regression for the bulk-bump bug: a Krisp meeting held 2026-06-11 but
    ingested today must render its span ``data-date`` = the meeting date, not
    ``ingested_at``. ``doc_date`` is the generated ``COALESCE(sent_at,
    ingested_at)`` column, so we set ``sent_at`` to drive it.
    """
    # Krisp-style doc: event date well before ingest. ``sent_at`` feeds the
    # generated ``doc_date`` column.
    event_dt = datetime.datetime(2026, 6, 11, 9, 0, 0, tzinfo=datetime.UTC)
    krisp_id = seed_doc(title="standup meeting", content="notes")
    # Plain doc ingested before the Krisp event but with no event date — it
    # must rank BELOW the Krisp doc once we order by event date.
    older_id = seed_doc(title="older note", content="o")
    test_db.execute(
        "UPDATE documents SET vault_path = '_ingested/krisp/standup.md', "
        "ingested_at = now(), sent_at = %s WHERE id = %s",
        (event_dt, krisp_id),
    )
    test_db.execute(
        "UPDATE documents SET vault_path = '_ingested/manual/older.md', "
        "ingested_at = %s WHERE id = %s",
        (datetime.datetime(2026, 6, 1, 0, 0, 0, tzinfo=datetime.UTC), older_id),
    )

    docs = _fetch_recent_docs(test_db, limit=10)
    by_title = {d.title: d for d in docs}
    assert "standup meeting" in by_title
    # display_date carries the EVENT date, not the (today) ingest time.
    assert by_title["standup meeting"].display_date == event_dt

    # The rendered span's data-date is the event date, NOT ingested_at.
    out = _render_bullets([by_title["standup meeting"]])
    assert f'data-date="{event_dt.isoformat()}"' in out
    assert f">{_format_absolute_date(event_dt)}</span>" in out

    # Ranking: event-dated Krisp doc (2026-06-11) outranks the older note
    # (ingested 2026-06-01), proving the ORDER BY uses the event date.
    ordered_titles = [d.title for d in docs]
    assert ordered_titles.index("standup meeting") < ordered_titles.index(
        "older note"
    )


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
        / "src" / "brain" / "quartz_overrides"
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
    monkeypatch.setattr(bs, "_run_build", fake_run_build)

    monkeypatch.setenv("DATABASE_URL", "postgresql://x:x@localhost:5432/x")
    vault = tmp_path / "vault"
    vault.mkdir()
    quartz = vault / ".quartz"
    quartz.mkdir()
    (quartz / "quartz.config.ts").write_text("", encoding="utf-8")
    # bootstrap-cli.mjs must exist for _check_workspace to pass (Task 5: node-direct).
    (quartz / "quartz").mkdir()
    (quartz / "quartz" / "bootstrap-cli.mjs").write_text("// stub\n")
    monkeypatch.setenv("BRAIN_VAULT_PATH", str(vault))

    # Pass node_path explicitly so the test doesn't depend on node being on PATH.
    # _run_build is already patched above; node_path just bypasses shutil.which().
    bs.build_and_swap(vault, quartz_dir=quartz, node_path="node")

    assert len(refresh_calls) == 1
    # The cfg passed to refresh has vault_path == the resolved CLI vault.
    assert refresh_calls[0].vault_path == vault.resolve()
