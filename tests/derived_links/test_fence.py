"""Tests for brain.vault.derived_links.fence.

Covers the four pure-string helpers (``extract_fence``, ``strip_fence``,
``replace_fence``) plus the one DB-backed renderer
(``render_fenced_section``). The renderer tests use the real
``test_db`` fixture (Postgres) and seed ``documents`` + ``derived_links``
rows directly so each scenario can pin the exact corpus shape it needs.
"""
import hashlib
import json
import uuid
from typing import Any

import psycopg
import pytest

from brain.vault.derived_links.fence import (
    FENCE_END_MARKER,
    FENCE_START_MARKER,
    extract_fence,
    render_fenced_section,
    replace_fence,
    strip_fence,
)

# --------------------------------------------------------------------------
# Pure-string helpers — no DB.
# --------------------------------------------------------------------------


class TestExtractFence:
    """Round-trip the fence extractor for the shapes the sync engine sees."""

    def test_body_without_fence_returns_body_and_none(self) -> None:
        body = "# Hello\n\nSome content here.\n"
        body_without, fence = extract_fence(body)
        assert body_without == body
        assert fence is None

    def test_body_with_fence_splits_cleanly(self) -> None:
        fence = (
            f"{FENCE_START_MARKER}\n"
            f"## Related (auto-generated, do not edit)\n"
            f"- [[partner-stem|Partner Title]] *(shared_thread)*\n"
            f"{FENCE_END_MARKER}"
        )
        body = f"# Hello\n\nSome content.\n\n{fence}\n"
        body_without, extracted = extract_fence(body)
        assert extracted == fence
        # The body-without-fence retains everything up to the START marker
        # plus everything after the END marker.
        assert FENCE_START_MARKER not in body_without
        assert FENCE_END_MARKER not in body_without
        assert body_without.startswith("# Hello\n\nSome content.\n\n")

    def test_body_with_only_start_marker_returns_unchanged(self) -> None:
        # Malformed: START with no matching END. Treated as content; the
        # corruption stays visible to the user instead of us silently
        # eating the rest of the file.
        body = f"# Hello\n\n{FENCE_START_MARKER}\nstray content with no end\n"
        body_without, fence = extract_fence(body)
        assert body_without == body
        assert fence is None

    def test_multiple_start_markers_takes_first(self) -> None:
        # Two stacked fences (corruption from a buggy renderer) — only the
        # first START..END pair is treated as the fence; the second START
        # marker stays in body_without_fence as plain text. A subsequent
        # render will replace the first fence and leave the stray START in
        # place; the user will notice it in `git diff`.
        first_fence = (
            f"{FENCE_START_MARKER}\n"
            f"## Related (auto-generated, do not edit)\n"
            f"- [[a|A]] *(shared_thread)*\n"
            f"{FENCE_END_MARKER}"
        )
        body = (
            f"# Hello\n\n{first_fence}\n\n"
            f"{FENCE_START_MARKER}\nleftover\n{FENCE_END_MARKER}\n"
        )
        body_without, fence = extract_fence(body)
        assert fence == first_fence
        # The second (stray) START marker remains in body_without.
        assert body_without.count(FENCE_START_MARKER) == 1
        assert "leftover" in body_without


class TestStripFence:
    """``strip_fence`` is idempotent and stable across fence-content changes."""

    def test_strip_no_fence_is_no_op(self) -> None:
        body = "# Hello\n\nSome content.\n"
        assert strip_fence(body) == body

    def test_strip_no_fence_no_trailing_newline_is_no_op(self) -> None:
        # Specifically: never add a newline to a body the renderer has never
        # touched. We don't want to perturb the user's authored shape.
        body = "no trailing newline"
        assert strip_fence(body) == body

    def test_strip_fence_removes_section(self) -> None:
        fence = f"{FENCE_START_MARKER}\nstuff\n{FENCE_END_MARKER}"
        body = f"# Hello\n\nbody text.\n\n{fence}\n"
        stripped = strip_fence(body)
        assert FENCE_START_MARKER not in stripped
        assert FENCE_END_MARKER not in stripped
        assert "stuff" not in stripped
        assert stripped.endswith("\n")

    def test_strip_is_idempotent(self) -> None:
        fence = f"{FENCE_START_MARKER}\nstuff\n{FENCE_END_MARKER}"
        body = f"# Hello\n\nbody text.\n\n{fence}\n"
        once = strip_fence(body)
        twice = strip_fence(once)
        assert once == twice

    def test_strip_is_stable_across_fence_content_changes(self) -> None:
        # The hash-stability guarantee: same base body + different fence →
        # byte-identical strip output. This is what keeps the re-embed
        # cascade from triggering on every relink.
        base = "# Hello\n\nbody text.\n\n"
        fence_a = f"{FENCE_START_MARKER}\nA\n{FENCE_END_MARKER}"
        fence_b = f"{FENCE_START_MARKER}\nB-different\n{FENCE_END_MARKER}"
        assert strip_fence(base + fence_a + "\n") == strip_fence(base + fence_b + "\n")

    def test_strip_collapses_only_trailing_whitespace(self) -> None:
        # Internal blank lines (legitimate content) survive; only the
        # whitespace between body and fence is collapsed.
        fence = f"{FENCE_START_MARKER}\nstuff\n{FENCE_END_MARKER}"
        body = f"# Title\n\nPara 1.\n\n\nPara 2.\n\n{fence}\n"
        stripped = strip_fence(body)
        assert "Para 1.\n\n\nPara 2." in stripped

    def test_strip_empty_body_after_fence_returns_empty(self) -> None:
        # All-fence file: nothing to keep.
        fence = f"{FENCE_START_MARKER}\nstuff\n{FENCE_END_MARKER}"
        assert strip_fence(fence) == ""
        assert strip_fence(fence + "\n") == ""


class TestReplaceFence:
    """``replace_fence`` appends-or-swaps and normalizes trailing whitespace."""

    def test_appends_when_absent(self) -> None:
        body = "# Hello\n\nbody text.\n"
        new = f"{FENCE_START_MARKER}\nfresh\n{FENCE_END_MARKER}"
        result = replace_fence(body, new)
        assert result == f"# Hello\n\nbody text.\n\n{new}\n"

    def test_appends_when_body_has_no_trailing_newline(self) -> None:
        body = "no trailing newline"
        new = f"{FENCE_START_MARKER}\nfresh\n{FENCE_END_MARKER}"
        result = replace_fence(body, new)
        assert result == f"no trailing newline\n\n{new}\n"

    def test_swaps_existing_fence(self) -> None:
        old = f"{FENCE_START_MARKER}\nold-content\n{FENCE_END_MARKER}"
        new = f"{FENCE_START_MARKER}\nnew-content\n{FENCE_END_MARKER}"
        body = f"# Hello\n\nbody.\n\n{old}\n"
        result = replace_fence(body, new)
        assert "old-content" not in result
        assert "new-content" in result
        # And only the new fence remains — no nested or duplicated fences.
        assert result.count(FENCE_START_MARKER) == 1
        assert result.count(FENCE_END_MARKER) == 1

    def test_normalizes_excessive_trailing_whitespace(self) -> None:
        # If a body somehow accumulated extra blank lines before its fence,
        # the result should still end with exactly one ``\n`` after the END
        # marker — no double-blank, no missing-newline.
        body = "# Hello\n\nbody.\n\n\n\n"
        new = f"{FENCE_START_MARKER}\nfresh\n{FENCE_END_MARKER}"
        result = replace_fence(body, new)
        assert result.endswith(f"{FENCE_END_MARKER}\n")
        # Exactly one blank line between body content and fence.
        assert "\n\n\n" + FENCE_START_MARKER not in result

    def test_empty_body_yields_just_the_fence(self) -> None:
        new = f"{FENCE_START_MARKER}\nfresh\n{FENCE_END_MARKER}"
        assert replace_fence("", new) == new + "\n"

    def test_whitespace_only_body_yields_just_the_fence(self) -> None:
        # All-whitespace input should not produce a double-newline before
        # the fence either.
        new = f"{FENCE_START_MARKER}\nfresh\n{FENCE_END_MARKER}"
        assert replace_fence("   \n\n  ", new) == new + "\n"


# --------------------------------------------------------------------------
# DB-backed renderer — real Postgres, seed documents + derived_links rows.
# --------------------------------------------------------------------------


def _seed_partner(
    conn: psycopg.Connection[Any],
    *,
    title: str,
    vault_path: str | None,
    metadata: dict[str, Any] | None = None,
    source_kind: str = "krisp",
) -> str:
    """Insert a sources + documents pair, return the new document id.

    The renderer reads ``documents.title``, ``documents.vault_path``, and
    ``documents.metadata->>'date'`` (via the metadata blob); each test
    fills in just the fields its assertion exercises. Content is salted
    with a random UUID so the global ``content_hash`` UNIQUE constraint
    never collides between fixtures.
    """
    src_row = conn.execute(
        "INSERT INTO sources (kind, external_id, metadata) "
        "VALUES (%s, %s, %s::jsonb) RETURNING id",
        (source_kind, str(uuid.uuid4()), json.dumps({})),
    ).fetchone()
    assert src_row is not None
    source_id = src_row[0]

    salted = f"body for {title}\n<!-- {uuid.uuid4()} -->"
    content_hash = hashlib.sha256(salted.encode("utf-8")).hexdigest()
    doc_row = conn.execute(
        """
        INSERT INTO documents
            (source_id, title, content, content_hash, content_type,
             source_path, tags, metadata, vault_path)
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s::jsonb, %s)
        RETURNING id::text
        """,
        (
            source_id,
            title,
            salted,
            content_hash,
            "transcript",
            None,
            [],
            json.dumps(metadata or {}),
            vault_path,
        ),
    ).fetchone()
    assert doc_row is not None
    return str(doc_row[0])


def _insert_derived_link(
    conn: psycopg.Connection[Any],
    *,
    a_id: str,
    b_id: str,
    rule: str,
    weight: float,
) -> None:
    """Insert one ``derived_links`` row with canonical ``(LEAST, GREATEST)`` ordering.

    Mirrors the pass-runner's ordering so ``UNIQUE (src, dst, rule)`` doesn't
    fire on test fixtures that exercise both directions.
    """
    src, dst = (a_id, b_id) if a_id < b_id else (b_id, a_id)
    conn.execute(
        """
        INSERT INTO derived_links
            (src_document_id, dst_document_id, rule, evidence, weight)
        VALUES (%s, %s, %s, %s::jsonb, %s)
        """,
        (src, dst, rule, json.dumps({}), weight),
    )


class TestRenderFencedSection:
    """End-to-end: derived_links + documents → rendered fenced markdown."""

    def test_returns_none_when_no_edges(
        self, test_db: psycopg.Connection
    ) -> None:
        center_id = _seed_partner(
            test_db,
            title="Center Doc",
            vault_path="_ingested/krisp/2026-04-01-abc123-center.md",
            metadata={"date": "2026-04-01"},
        )
        assert render_fenced_section(test_db, center_id) is None

    def test_single_edge_renders_one_bullet(
        self, test_db: psycopg.Connection
    ) -> None:
        center_id = _seed_partner(
            test_db,
            title="Center",
            vault_path="_ingested/krisp/2026-04-01-aaa-center.md",
            metadata={"date": "2026-04-01"},
        )
        partner_id = _seed_partner(
            test_db,
            title="Partner Title",
            vault_path="_ingested/gmail/2026-04-02-bbb-partner.md",
            metadata={"date": "Wed, 02 Apr 2026 12:00:00 -0700"},
            source_kind="gmail",
        )
        _insert_derived_link(
            test_db,
            a_id=center_id,
            b_id=partner_id,
            rule="shared_thread",
            weight=1.0,
        )

        rendered = render_fenced_section(test_db, center_id)
        assert rendered is not None
        # Markers and section heading present.
        assert rendered.startswith(FENCE_START_MARKER)
        assert rendered.endswith(FENCE_END_MARKER)
        assert "## Related (auto-generated, do not edit)" in rendered
        # Bullet uses the partner's filename stem (not its document id) and
        # the partner's title as the alias.
        assert "[[2026-04-02-bbb-partner|Partner Title]]" in rendered
        assert "*(shared_thread)*" in rendered

    def test_sort_order_weight_desc_then_date_desc(
        self, test_db: psycopg.Connection
    ) -> None:
        # Q1 decision: primary sort = rule weight DESC; secondary = partner
        # date DESC (NOT title-ASC). Three partners pinned to deterministic
        # weights and dates so the assertion checks both axes.
        center_id = _seed_partner(
            test_db,
            title="Center",
            vault_path="_ingested/krisp/2026-04-15-ccc-center.md",
            metadata={"date": "2026-04-15"},
        )
        # weight=0.4 (R2), older date — should be LAST.
        old_r2 = _seed_partner(
            test_db,
            title="Old R2",
            vault_path="_ingested/krisp/2026-01-01-aaa-old-r2.md",
            metadata={"date": "2026-01-01"},
        )
        # weight=0.4 (R2), newer date — should beat ``old_r2`` within tier.
        new_r2 = _seed_partner(
            test_db,
            title="New R2",
            vault_path="_ingested/krisp/2026-03-01-bbb-new-r2.md",
            metadata={"date": "2026-03-01"},
        )
        # weight=1.0 (R1) — highest weight tier always sorts first.
        r1 = _seed_partner(
            test_db,
            title="R1 Partner",
            vault_path="_ingested/gmail/2026-02-01-ddd-r1.md",
            metadata={"date": "2026-02-01"},
            source_kind="gmail",
        )
        _insert_derived_link(
            test_db, a_id=center_id, b_id=old_r2,
            rule="shared_participant", weight=0.4,
        )
        _insert_derived_link(
            test_db, a_id=center_id, b_id=new_r2,
            rule="shared_participant", weight=0.4,
        )
        _insert_derived_link(
            test_db, a_id=center_id, b_id=r1,
            rule="shared_thread", weight=1.0,
        )

        rendered = render_fenced_section(test_db, center_id)
        assert rendered is not None
        # Find the index of each partner's bullet — the rendered string is
        # newline-delimited with bullets in sort order.
        idx_r1 = rendered.index("R1 Partner")
        idx_new_r2 = rendered.index("New R2")
        idx_old_r2 = rendered.index("Old R2")
        # R1 (weight 1.0) before any R2 (weight 0.4).
        assert idx_r1 < idx_new_r2
        assert idx_r1 < idx_old_r2
        # Within R2 tier: newer date beats older.
        assert idx_new_r2 < idx_old_r2

    def test_partner_with_null_vault_path_is_skipped(
        self, test_db: psycopg.Connection
    ) -> None:
        # A partner that hasn't been exported to disk yet has no usable
        # link target; the bullet would be ``[[|Title]]`` and wouldn't
        # resolve in Quartz, so the renderer drops it silently.
        center_id = _seed_partner(
            test_db,
            title="Center",
            vault_path="_ingested/krisp/2026-04-15-eee-center.md",
            metadata={"date": "2026-04-15"},
        )
        no_path = _seed_partner(
            test_db,
            title="No Path",
            vault_path=None,
            metadata={"date": "2026-04-10"},
        )
        with_path = _seed_partner(
            test_db,
            title="With Path",
            vault_path="_ingested/gmail/2026-04-12-fff-with-path.md",
            metadata={"date": "2026-04-12"},
            source_kind="gmail",
        )
        _insert_derived_link(
            test_db, a_id=center_id, b_id=no_path,
            rule="shared_participant", weight=0.4,
        )
        _insert_derived_link(
            test_db, a_id=center_id, b_id=with_path,
            rule="shared_thread", weight=1.0,
        )

        rendered = render_fenced_section(test_db, center_id)
        assert rendered is not None
        assert "With Path" in rendered
        assert "No Path" not in rendered

    def test_returns_none_when_only_partners_have_no_vault_path(
        self, test_db: psycopg.Connection
    ) -> None:
        # Edge case: every partner is unexported → nothing to render →
        # caller should remove (not append) the fence. Returning None is
        # how the renderer signals that.
        center_id = _seed_partner(
            test_db,
            title="Center",
            vault_path="_ingested/krisp/2026-04-15-ggg-center.md",
            metadata={"date": "2026-04-15"},
        )
        partner = _seed_partner(
            test_db,
            title="Unexported",
            vault_path=None,
            metadata={"date": "2026-04-10"},
        )
        _insert_derived_link(
            test_db, a_id=center_id, b_id=partner,
            rule="shared_thread", weight=1.0,
        )
        assert render_fenced_section(test_db, center_id) is None

    def test_partner_with_missing_date_sorts_after_dated_within_tier(
        self, test_db: psycopg.Connection
    ) -> None:
        # Q1 decision: partners with no parseable date sort AFTER partners
        # with one, within the same weight tier. (Weight tiers still
        # dominate.)
        center_id = _seed_partner(
            test_db,
            title="Center",
            vault_path="_ingested/krisp/2026-04-15-hhh-center.md",
            metadata={"date": "2026-04-15"},
        )
        dated = _seed_partner(
            test_db,
            title="Dated Partner",
            vault_path="_ingested/krisp/2026-04-10-iii-dated.md",
            metadata={"date": "2026-04-10"},
        )
        undated = _seed_partner(
            test_db,
            title="Undated Partner",
            vault_path="_ingested/krisp/local-jjj-undated.md",
            metadata={},
        )
        _insert_derived_link(
            test_db, a_id=center_id, b_id=dated,
            rule="shared_participant", weight=0.4,
        )
        _insert_derived_link(
            test_db, a_id=center_id, b_id=undated,
            rule="shared_participant", weight=0.4,
        )

        rendered = render_fenced_section(test_db, center_id)
        assert rendered is not None
        assert rendered.index("Dated Partner") < rendered.index("Undated Partner")

    def test_finds_partner_via_either_endpoint(
        self, test_db: psycopg.Connection
    ) -> None:
        # ``derived_links`` rows store ``(LEAST(a,b), GREATEST(a,b))`` —
        # the renderer must surface the partner whether ``doc_id`` is the
        # SRC or the DST end of the edge.
        a_id = _seed_partner(
            test_db,
            title="Alpha",
            vault_path="_ingested/krisp/2026-04-01-aaa-alpha.md",
            metadata={"date": "2026-04-01"},
        )
        b_id = _seed_partner(
            test_db,
            title="Bravo",
            vault_path="_ingested/krisp/2026-04-02-bbb-bravo.md",
            metadata={"date": "2026-04-02"},
        )
        _insert_derived_link(
            test_db, a_id=a_id, b_id=b_id,
            rule="shared_participant", weight=0.4,
        )

        from_a = render_fenced_section(test_db, a_id)
        from_b = render_fenced_section(test_db, b_id)
        assert from_a is not None and "Bravo" in from_a
        assert from_b is not None and "Alpha" in from_b

    def test_unparseable_date_string_sorts_with_undated(
        self, test_db: psycopg.Connection
    ) -> None:
        # Defensive: a metadata.date that's neither ISO nor RFC 5322 must
        # not crash the sort — it falls through to the undated bucket.
        center_id = _seed_partner(
            test_db,
            title="Center",
            vault_path="_ingested/krisp/2026-04-15-kkk-center.md",
            metadata={"date": "2026-04-15"},
        )
        garbage = _seed_partner(
            test_db,
            title="Garbage Date",
            vault_path="_ingested/krisp/2026-04-10-lll-garbage.md",
            metadata={"date": "not-a-real-date"},
        )
        dated = _seed_partner(
            test_db,
            title="Real Date",
            vault_path="_ingested/krisp/2026-04-05-mmm-real.md",
            metadata={"date": "2026-04-05"},
        )
        _insert_derived_link(
            test_db, a_id=center_id, b_id=garbage,
            rule="shared_participant", weight=0.4,
        )
        _insert_derived_link(
            test_db, a_id=center_id, b_id=dated,
            rule="shared_participant", weight=0.4,
        )

        rendered = render_fenced_section(test_db, center_id)
        assert rendered is not None
        assert rendered.index("Real Date") < rendered.index("Garbage Date")


@pytest.mark.parametrize(
    "raw_date",
    [
        None,
        "",
        "   ",
        123,  # non-string metadata.date
    ],
)
def test_render_handles_missing_or_non_string_date_keys(
    test_db: psycopg.Connection, raw_date: Any
) -> None:
    """Defensive: a metadata blob with no usable date key must not crash."""
    center_id = _seed_partner(
        test_db,
        title="Center",
        vault_path="_ingested/krisp/center.md",
        metadata={"date": "2026-04-15"},
    )
    metadata: dict[str, Any] = {} if raw_date is None else {"date": raw_date}
    partner = _seed_partner(
        test_db,
        title="Partner",
        vault_path="_ingested/krisp/partner.md",
        metadata=metadata,
    )
    _insert_derived_link(
        test_db, a_id=center_id, b_id=partner,
        rule="shared_participant", weight=0.4,
    )
    rendered = render_fenced_section(test_db, center_id)
    assert rendered is not None
    assert "Partner" in rendered
