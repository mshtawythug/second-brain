"""Tests for brain.vault.derived_links.fence.

Covers the four pure-string helpers (``extract_fence``, ``strip_fence``,
``replace_fence``), the DB-backed renderer (``render_fenced_section``),
and the file-rewriting renderer (``rewrite_derived_fences``).

The DB-backed tests use the real ``test_db`` fixture (Postgres) and seed
``documents`` + ``derived_links`` rows directly so each scenario can pin
the exact corpus shape it needs. The file-rewriter tests additionally
seed an ``_ingested/`` mirror under ``tmp_path`` so the disk-write path
is exercised end-to-end.
"""
import hashlib
import json
import uuid
from pathlib import Path
from typing import Any

import psycopg
import pytest

from brain.vault.derived_links.fence import (
    FENCE_END_MARKER,
    FENCE_START_MARKER,
    extract_fence,
    render_fenced_section,
    replace_fence,
    rewrite_derived_fences,
    strip_fence,
)
from brain.vault.frontmatter import dump_frontmatter, parse_frontmatter

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
        #
        # Within-tier sort uses R3 (same_day_participant, weight 0.7) for
        # the lower tier rather than R2 (shared_participant) — R2 is
        # filtered out of the fence renderer entirely (high-recall hairball
        # mitigation), so a within-R2-tier sort assertion would always
        # short-circuit to None.
        center_id = _seed_partner(
            test_db,
            title="Center",
            vault_path="_ingested/krisp/2026-04-15-ccc-center.md",
            metadata={"date": "2026-04-15"},
        )
        # R3 (weight 0.7), older date — should be LAST in the R3 tier.
        old_r3 = _seed_partner(
            test_db,
            title="Old R3",
            vault_path="_ingested/krisp/2026-01-01-aaa-old-r3.md",
            metadata={"date": "2026-01-01"},
        )
        # R3 (weight 0.7), newer date — should beat ``old_r3`` within tier.
        new_r3 = _seed_partner(
            test_db,
            title="New R3",
            vault_path="_ingested/krisp/2026-03-01-bbb-new-r3.md",
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
            test_db, a_id=center_id, b_id=old_r3,
            rule="same_day_participant", weight=0.7,
        )
        _insert_derived_link(
            test_db, a_id=center_id, b_id=new_r3,
            rule="same_day_participant", weight=0.7,
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
        idx_new_r3 = rendered.index("New R3")
        idx_old_r3 = rendered.index("Old R3")
        # R1 (weight 1.0) before any R3 (weight 0.7).
        assert idx_r1 < idx_new_r3
        assert idx_r1 < idx_old_r3
        # Within R3 tier: newer date beats older.
        assert idx_new_r3 < idx_old_r3

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
        # Use R3 (kept by FENCE_RULES) instead of R2 (filtered) so the
        # null-vault-path skip is what excludes ``no_path``, not the rule
        # filter.
        _insert_derived_link(
            test_db, a_id=center_id, b_id=no_path,
            rule="same_day_participant", weight=0.7,
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
        # R3 (kept by FENCE_RULES) so both rows make it past the rule
        # filter and the within-tier date-sort is what we actually verify.
        _insert_derived_link(
            test_db, a_id=center_id, b_id=dated,
            rule="same_day_participant", weight=0.7,
        )
        _insert_derived_link(
            test_db, a_id=center_id, b_id=undated,
            rule="same_day_participant", weight=0.7,
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
        # R3 (kept by FENCE_RULES); R2 would be filtered out and the
        # endpoint-symmetry assertion would short-circuit to None.
        _insert_derived_link(
            test_db, a_id=a_id, b_id=b_id,
            rule="same_day_participant", weight=0.7,
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
        # R3 (kept by FENCE_RULES) so both rows reach the sort step.
        _insert_derived_link(
            test_db, a_id=center_id, b_id=garbage,
            rule="same_day_participant", weight=0.7,
        )
        _insert_derived_link(
            test_db, a_id=center_id, b_id=dated,
            rule="same_day_participant", weight=0.7,
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
    # R3 (kept by FENCE_RULES) so the date-handling branches under test
    # actually run; R2 would short-circuit to None at the SQL filter.
    _insert_derived_link(
        test_db, a_id=center_id, b_id=partner,
        rule="same_day_participant", weight=0.7,
    )
    rendered = render_fenced_section(test_db, center_id)
    assert rendered is not None
    assert "Partner" in rendered


# --------------------------------------------------------------------------
# Hotfix D.6.1 — R2 filter in the fence renderer (graph hairball mitigation).
# --------------------------------------------------------------------------


class TestFenceRulesFilter:
    """``render_fenced_section`` filters R2 (``shared_participant``) at the SQL.

    R2 is high-recall / low-precision — at corpus scale the user is a
    participant in nearly every doc, so almost every doc shares a
    participant with almost every other doc. Materializing all of those
    edges as wiki-links breaks Quartz's force-directed graph layout
    (oscillating hairball). The filter is render-side only: R2 rows
    remain in the ``derived_links`` table and surface in
    ``brain backlinks`` / ``brain graph`` / MCP — only the Quartz fence
    is narrowed to R1 + R3.
    """

    def test_render_excludes_shared_participant(
        self, test_db: psycopg.Connection
    ) -> None:
        # Seed one R1, one R3, and five R2 edges. The rendered fence
        # should contain only the R1 and R3 partners — NO ``shared_participant``
        # bullets at all.
        center_id = _seed_partner(
            test_db,
            title="Center",
            vault_path="_ingested/krisp/center.md",
            metadata={"date": "2026-04-15"},
        )
        r1_partner = _seed_partner(
            test_db,
            title="R1 Partner",
            vault_path="_ingested/gmail/r1.md",
            metadata={"date": "2026-04-15"},
            source_kind="gmail",
        )
        r3_partner = _seed_partner(
            test_db,
            title="R3 Partner",
            vault_path="_ingested/krisp/r3.md",
            metadata={"date": "2026-04-15"},
        )
        _insert_derived_link(
            test_db, a_id=center_id, b_id=r1_partner,
            rule="shared_thread", weight=1.0,
        )
        _insert_derived_link(
            test_db, a_id=center_id, b_id=r3_partner,
            rule="same_day_participant", weight=0.7,
        )
        # Five R2 partners, all with valid vault_paths and dates so the
        # only reason they're absent is the rule filter.
        for i in range(5):
            r2_partner = _seed_partner(
                test_db,
                title=f"R2 Partner {i}",
                vault_path=f"_ingested/krisp/r2-{i}.md",
                metadata={"date": "2026-04-15"},
            )
            _insert_derived_link(
                test_db, a_id=center_id, b_id=r2_partner,
                rule="shared_participant", weight=0.4,
            )

        rendered = render_fenced_section(test_db, center_id)
        assert rendered is not None

        # R1 and R3 are present.
        assert "R1 Partner" in rendered
        assert "R3 Partner" in rendered
        # No R2 partners and no ``*(shared_participant)*`` rule annotation.
        assert "R2 Partner" not in rendered
        assert "shared_participant" not in rendered

        # Bullet count: 2 (R1 + R3). Header + 2 bullets + start + end =
        # 4 lines total inside the fence.
        bullet_count = sum(
            1 for line in rendered.splitlines()
            if line.startswith("- [[")
        )
        assert bullet_count == 2

    def test_render_returns_none_when_only_r2_edges(
        self, test_db: psycopg.Connection
    ) -> None:
        # A doc whose ONLY derived edges are R2 produces an empty
        # bullet list under the filter → returns None so the caller
        # strips the fence rather than emitting an empty section.
        # This is the hairball mitigation in action: an "everyone is
        # a participant" doc gets no Quartz-visible Related section,
        # but the underlying R2 edges remain in the DB for backlinks /
        # graph / MCP queries.
        center_id = _seed_partner(
            test_db,
            title="Center",
            vault_path="_ingested/krisp/center.md",
            metadata={"date": "2026-04-15"},
        )
        for i in range(3):
            r2_partner = _seed_partner(
                test_db,
                title=f"R2 Partner {i}",
                vault_path=f"_ingested/krisp/r2-only-{i}.md",
                metadata={"date": "2026-04-15"},
            )
            _insert_derived_link(
                test_db, a_id=center_id, b_id=r2_partner,
                rule="shared_participant", weight=0.4,
            )

        # Sanity: the rows ARE in the DB (filter is render-side only).
        row_count = test_db.execute(
            "SELECT count(*) FROM derived_links "
            "WHERE src_document_id = %s OR dst_document_id = %s",
            (center_id, center_id),
        ).fetchone()
        assert row_count is not None
        assert int(row_count[0]) == 3

        # But the renderer skips them.
        assert render_fenced_section(test_db, center_id) is None

    def test_fence_rules_constant_excludes_shared_participant(self) -> None:
        # Lock in the public contract: ``FENCE_RULES`` is the single
        # source of truth for what's surfaced in the Quartz fence, and
        # ``shared_participant`` MUST NOT be in it. If a future change
        # adds R2 back, this test fires with a clear hint about why
        # the filter exists (Quartz hairball mitigation, see fence.py
        # docstring for the rationale).
        from brain.vault.derived_links.fence import FENCE_RULES
        assert "shared_participant" not in FENCE_RULES
        # R1 and R3 are in.
        assert "shared_thread" in FENCE_RULES
        assert "same_day_participant" in FENCE_RULES


# --------------------------------------------------------------------------
# File-rewriting renderer — Task D.4.
# --------------------------------------------------------------------------


def _write_vault_file(
    vault_path: Path, relative: str, fields: dict[str, Any], body: str
) -> Path:
    """Write a vault file at ``vault_path/relative`` with frontmatter+body.

    Returns the absolute path. Mirrors the export pipeline's output shape
    so the rewriter sees realistic input.
    """
    target = vault_path / relative
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(dump_frontmatter(fields, body), encoding="utf-8")
    return target


def _seed_ingested_doc_with_file(
    conn: psycopg.Connection[Any],
    vault_path: Path,
    *,
    title: str,
    relative_vault_path: str,
    metadata: dict[str, Any] | None = None,
    body: str = "authored body content\n",
    source_kind: str = "krisp",
) -> tuple[str, Path]:
    """Seed an ingested-tier doc + write its mirror file.

    Sets ``documents.kind = 'ingested'`` and ``documents.vault_path`` to
    ``relative_vault_path`` so the rewriter classifies and locates the
    file the same way it would in production. Returns ``(doc_id, abs_path)``.
    """
    metadata = metadata or {}
    src_row = conn.execute(
        "INSERT INTO sources (kind, external_id, metadata) "
        "VALUES (%s, %s, %s::jsonb) RETURNING id",
        (source_kind, str(uuid.uuid4()), json.dumps({})),
    ).fetchone()
    assert src_row is not None
    salted = f"{body}<!-- {uuid.uuid4()} -->"
    content_hash = hashlib.sha256(salted.encode("utf-8")).hexdigest()
    doc_row = conn.execute(
        """
        INSERT INTO documents
            (source_id, title, content, content_hash, content_type,
             source_path, tags, metadata, kind, vault_path)
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s::jsonb, %s, %s)
        RETURNING id::text
        """,
        (
            src_row[0],
            title,
            salted,
            content_hash,
            "transcript",
            None,
            [],
            json.dumps(metadata),
            "ingested",
            relative_vault_path,
        ),
    ).fetchone()
    assert doc_row is not None
    doc_id = str(doc_row[0])

    target = _write_vault_file(
        vault_path,
        relative_vault_path,
        {"id": doc_id, "title": title, "kind": "ingested"},
        body,
    )
    return doc_id, target


def _seed_partner_with_path(
    conn: psycopg.Connection[Any],
    *,
    title: str,
    vault_path: str | None,
    metadata: dict[str, Any] | None = None,
) -> str:
    """Seed a partner doc (no file written) with the given vault_path column.

    The renderer queries ``documents.vault_path`` for the partner's
    filename stem; partners don't need actual files on disk for the
    bullet-rendering path to be exercised.
    """
    return _seed_partner(
        conn,
        title=title,
        vault_path=vault_path,
        metadata=metadata,
    )


class TestRewriteDerivedFences:
    """End-to-end: doc id set → fence regeneration in ``_ingested/`` files.

    Q3=a (vault-tier skipped), Q4=b (always write — no byte-identical
    skip), atomic temp+rename writes.
    """

    def test_empty_doc_ids_returns_zero_no_filesystem_touch(
        self, test_db: psycopg.Connection, tmp_path: Path
    ) -> None:
        # Empty input → zero writes, no DB query, no FS scan.
        vault = tmp_path / "vault"
        vault.mkdir()
        # A pre-existing file should not be touched.
        sentinel = vault / "_ingested" / "krisp" / "untouched.md"
        sentinel.parent.mkdir(parents=True)
        sentinel.write_text("untouched\n", encoding="utf-8")
        mtime_before = sentinel.stat().st_mtime_ns

        written = rewrite_derived_fences(test_db, set(), vault_path=vault)

        assert written == 0
        assert sentinel.read_text(encoding="utf-8") == "untouched\n"
        assert sentinel.stat().st_mtime_ns == mtime_before

    def test_touched_file_gets_correct_fence(
        self, test_db: psycopg.Connection, tmp_path: Path
    ) -> None:
        # Seed: an ingested doc on disk, a partner with a vault_path so
        # its filename stem is used as the wiki-link target, and one
        # derived_links row connecting them. After the rewrite, the file
        # has the fence with the correct bullet.
        vault = tmp_path / "vault"
        center_id, target = _seed_ingested_doc_with_file(
            test_db, vault,
            title="Center",
            relative_vault_path="_ingested/krisp/2026-04-15-aaa-center.md",
            metadata={"date": "2026-04-15"},
        )
        partner_id = _seed_partner_with_path(
            test_db,
            title="Partner Title",
            vault_path="_ingested/gmail/2026-04-15-bbb-partner.md",
            metadata={"date": "Wed, 15 Apr 2026 10:00:00 -0700"},
        )
        _insert_derived_link(
            test_db, a_id=center_id, b_id=partner_id,
            rule="shared_thread", weight=1.0,
        )

        written = rewrite_derived_fences(
            test_db, {center_id, partner_id}, vault_path=vault
        )

        # Only the center has a file on disk; partner's vault_path points
        # at a path that doesn't exist on disk → silently skipped.
        assert written == 1
        text = target.read_text(encoding="utf-8")
        # Frontmatter survives unchanged.
        fm, body = parse_frontmatter(text)
        assert fm["id"] == center_id
        # Fence + bullet shape per Q1+Q2a+Q5b.
        assert FENCE_START_MARKER in body
        assert FENCE_END_MARKER in body
        assert "[[2026-04-15-bbb-partner|Partner Title]] *(shared_thread)*" in body

    def test_q4b_always_rewrites_even_when_byte_identical(
        self, test_db: psycopg.Connection, tmp_path: Path
    ) -> None:
        # Q4=b: even when the rendered fence is byte-identical to what's
        # already on disk, the rewriter must produce a write. We assert by
        # running the rewriter twice in a row and checking the count is
        # the SAME on the second pass (no skip), and that the file's
        # mtime advanced between the two passes.
        vault = tmp_path / "vault"
        center_id, target = _seed_ingested_doc_with_file(
            test_db, vault,
            title="Center",
            relative_vault_path="_ingested/krisp/center.md",
            metadata={"date": "2026-04-15"},
        )
        partner_id = _seed_partner_with_path(
            test_db,
            title="Partner",
            vault_path="_ingested/gmail/partner.md",
            metadata={"date": "2026-04-14"},
        )
        _insert_derived_link(
            test_db, a_id=center_id, b_id=partner_id,
            rule="shared_thread", weight=1.0,
        )

        first = rewrite_derived_fences(
            test_db, {center_id}, vault_path=vault
        )
        first_text = target.read_text(encoding="utf-8")
        first_mtime = target.stat().st_mtime_ns

        second = rewrite_derived_fences(
            test_db, {center_id}, vault_path=vault
        )
        second_text = target.read_text(encoding="utf-8")
        second_mtime = target.stat().st_mtime_ns

        # Same write count both passes — Q4=b: no byte-identical skip.
        assert first == 1
        assert second == 1
        # Content is byte-identical (same fence content from the same DB
        # state) — but the file was rewritten anyway, so the mtime
        # advanced. (POSIX mtime is nanosecond-precision; even a
        # millisecond apart will differ.)
        assert first_text == second_text
        assert second_mtime >= first_mtime  # at least non-decreasing

    def test_only_affected_files_are_updated(
        self, test_db: psycopg.Connection, tmp_path: Path
    ) -> None:
        # Two ingested files exist; only one is in the affected_ids set.
        # The other file's content + mtime must not change.
        vault = tmp_path / "vault"
        affected_id, affected_target = _seed_ingested_doc_with_file(
            test_db, vault,
            title="Affected",
            relative_vault_path="_ingested/krisp/affected.md",
            metadata={"date": "2026-04-15"},
        )
        bystander_id, bystander_target = _seed_ingested_doc_with_file(
            test_db, vault,
            title="Bystander",
            relative_vault_path="_ingested/krisp/bystander.md",
            metadata={"date": "2026-04-15"},
        )
        partner = _seed_partner_with_path(
            test_db,
            title="Partner",
            vault_path="_ingested/gmail/partner.md",
            metadata={"date": "2026-04-14"},
        )
        # Only the affected doc has a derived edge.
        _insert_derived_link(
            test_db, a_id=affected_id, b_id=partner,
            rule="shared_thread", weight=1.0,
        )

        bystander_before = bystander_target.read_text(encoding="utf-8")
        bystander_mtime_before = bystander_target.stat().st_mtime_ns

        written = rewrite_derived_fences(
            test_db, {affected_id}, vault_path=vault
        )

        assert written == 1
        # Affected file picked up the fence.
        assert FENCE_START_MARKER in affected_target.read_text(encoding="utf-8")
        # Bystander untouched: same bytes, same mtime.
        assert bystander_target.read_text(encoding="utf-8") == bystander_before
        assert bystander_target.stat().st_mtime_ns == bystander_mtime_before
        # Bystander wasn't even queried (no fence either way), and
        # ``bystander_id`` wasn't in the affected set anyway.
        _ = bystander_id  # silence "unused" — kept for symmetry/readability

    def test_vault_tier_files_skipped_silently(
        self, test_db: psycopg.Connection, tmp_path: Path
    ) -> None:
        # Q3=a: user-authored vault-tier notes never get a fence. Even if
        # a vault-tier doc somehow shows up in affected_ids, the rewriter
        # silently skips it — no write, no error.
        vault = tmp_path / "vault"
        # Seed a vault-tier doc with a file on disk.
        vault_doc_id = str(uuid.uuid4())
        target = _write_vault_file(
            vault, "notes/my-note.md",
            {"id": vault_doc_id, "title": "My Note", "kind": "vault"},
            "user-authored content\n",
        )
        salted = f"vault body\n<!-- {uuid.uuid4()} -->"
        test_db.execute(
            """
            INSERT INTO documents
                (id, source_id, title, content, content_hash, content_type,
                 source_path, tags, metadata, kind, vault_path)
            VALUES (%s, NULL, %s, %s, %s, %s, %s, %s, %s::jsonb, %s, %s)
            """,
            (
                vault_doc_id,
                "My Note",
                salted,
                hashlib.sha256(salted.encode()).hexdigest(),
                "note",
                None,
                [],
                json.dumps({}),
                "vault",
                "notes/my-note.md",
            ),
        )
        before = target.read_text(encoding="utf-8")
        mtime_before = target.stat().st_mtime_ns

        written = rewrite_derived_fences(
            test_db, {vault_doc_id}, vault_path=vault
        )

        assert written == 0
        # File untouched.
        assert target.read_text(encoding="utf-8") == before
        assert target.stat().st_mtime_ns == mtime_before

    def test_ingested_doc_with_null_vault_path_is_skipped(
        self, test_db: psycopg.Connection, tmp_path: Path
    ) -> None:
        # An ingested doc that's never been exported has ``vault_path`` =
        # NULL on the row. The rewriter has no path to write to → skip
        # silently. (Distinct from "vault_path is set but file missing":
        # this branch is the row-level check, not the FS check.)
        vault = tmp_path / "vault"
        vault.mkdir()
        src_row = test_db.execute(
            "INSERT INTO sources (kind, external_id, metadata) "
            "VALUES (%s, %s, %s::jsonb) RETURNING id",
            ("krisp", "null-vp", json.dumps({})),
        ).fetchone()
        assert src_row is not None
        salted = f"body\n<!-- {uuid.uuid4()} -->"
        doc_row = test_db.execute(
            """
            INSERT INTO documents
                (source_id, title, content, content_hash, content_type,
                 source_path, tags, metadata, kind, vault_path)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s::jsonb, %s, NULL)
            RETURNING id::text
            """,
            (
                src_row[0],
                "Never Exported",
                salted,
                hashlib.sha256(salted.encode()).hexdigest(),
                "transcript",
                None,
                [],
                json.dumps({}),
                "ingested",
            ),
        ).fetchone()
        assert doc_row is not None
        doc_id = str(doc_row[0])

        written = rewrite_derived_fences(
            test_db, {doc_id}, vault_path=vault
        )

        assert written == 0

    def test_missing_ingested_mirror_is_skipped(
        self, test_db: psycopg.Connection, tmp_path: Path
    ) -> None:
        # Doc has ``vault_path`` set but the file isn't on disk yet
        # (export will produce one on the next pass). The rewriter logs
        # at debug level and moves on — no count, no error.
        vault = tmp_path / "vault"
        vault.mkdir()
        # Seed an ingested doc with a non-existent vault_path.
        src_row = test_db.execute(
            "INSERT INTO sources (kind, external_id, metadata) "
            "VALUES (%s, %s, %s::jsonb) RETURNING id",
            ("krisp", "missing-mirror", json.dumps({})),
        ).fetchone()
        assert src_row is not None
        salted = f"body\n<!-- {uuid.uuid4()} -->"
        doc_row = test_db.execute(
            """
            INSERT INTO documents
                (source_id, title, content, content_hash, content_type,
                 source_path, tags, metadata, kind, vault_path)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s::jsonb, %s, %s)
            RETURNING id::text
            """,
            (
                src_row[0],
                "Missing Mirror",
                salted,
                hashlib.sha256(salted.encode()).hexdigest(),
                "transcript",
                None,
                [],
                json.dumps({}),
                "ingested",
                "_ingested/krisp/never-exported.md",
            ),
        ).fetchone()
        assert doc_row is not None
        doc_id = str(doc_row[0])

        written = rewrite_derived_fences(
            test_db, {doc_id}, vault_path=vault
        )

        assert written == 0
        # No file was created — the rewriter only writes existing files.
        assert not (vault / "_ingested" / "krisp" / "never-exported.md").exists()

    def test_doc_with_zero_edges_has_fence_removed(
        self, test_db: psycopg.Connection, tmp_path: Path
    ) -> None:
        # The "lost partner" path: a file currently has a fence on disk
        # (from a previous relink). After all its derived edges have been
        # deleted, the rewriter must STRIP the stale fence — otherwise
        # the file keeps pointing at partners that no longer exist as
        # derived edges.
        vault = tmp_path / "vault"
        # Manually seed a doc whose file already has a fence.
        doc_id, target = _seed_ingested_doc_with_file(
            test_db, vault,
            title="Lonely Doc",
            relative_vault_path="_ingested/krisp/lonely.md",
            metadata={"date": "2026-04-15"},
        )
        # Re-write the file with a stale fence in the body. (Simulates
        # the post-D.4 state of a file whose edges were since deleted.)
        existing_fence = (
            f"{FENCE_START_MARKER}\n"
            f"## Related (auto-generated, do not edit)\n"
            f"- [[stale-stem|Stale Partner]] *(shared_thread)*\n"
            f"{FENCE_END_MARKER}"
        )
        target.write_text(
            dump_frontmatter(
                {"id": doc_id, "title": "Lonely Doc", "kind": "ingested"},
                f"the authored body\n\n{existing_fence}\n",
            ),
            encoding="utf-8",
        )

        # No derived_links rows for ``doc_id`` → render_fenced_section
        # returns None → rewriter calls strip_fence on the body.
        written = rewrite_derived_fences(
            test_db, {doc_id}, vault_path=vault
        )

        assert written == 1
        text = target.read_text(encoding="utf-8")
        assert FENCE_START_MARKER not in text
        assert FENCE_END_MARKER not in text
        assert "Stale Partner" not in text
        # Authored body content survived intact.
        assert "the authored body" in text

    def test_no_existing_fence_no_edges_is_idempotent_write(
        self, test_db: psycopg.Connection, tmp_path: Path
    ) -> None:
        # Q4=b: even when there's nothing to add (no edges) and no fence
        # to strip (file never had one), the rewriter still writes the
        # file. This is the "predictable mtimes" trade-off the user
        # explicitly chose. Asserting the count and that the body bytes
        # are unchanged after the write.
        vault = tmp_path / "vault"
        doc_id, target = _seed_ingested_doc_with_file(
            test_db, vault,
            title="No Edges No Fence",
            relative_vault_path="_ingested/krisp/no-edges.md",
            metadata={"date": "2026-04-15"},
        )
        before = target.read_text(encoding="utf-8")

        written = rewrite_derived_fences(
            test_db, {doc_id}, vault_path=vault
        )

        # Q4=b: write happens regardless.
        assert written == 1
        # Frontmatter + body round-trip identically (strip_fence is a
        # no-op on a fence-less body).
        after = target.read_text(encoding="utf-8")
        fm_before, body_before = parse_frontmatter(before)
        fm_after, body_after = parse_frontmatter(after)
        assert fm_before == fm_after
        assert body_before.rstrip() == body_after.rstrip()

    def test_atomic_write_no_temp_files_left_behind(
        self, test_db: psycopg.Connection, tmp_path: Path
    ) -> None:
        # The rewriter writes via a sibling ``.tmp`` then ``os.replace``.
        # On a successful pass there should be NO ``*.tmp`` files left in
        # the vault — the rename moves the temp into place.
        vault = tmp_path / "vault"
        center_id, _target = _seed_ingested_doc_with_file(
            test_db, vault,
            title="Center",
            relative_vault_path="_ingested/krisp/atomic.md",
            metadata={"date": "2026-04-15"},
        )
        partner = _seed_partner_with_path(
            test_db,
            title="Partner",
            vault_path="_ingested/gmail/partner.md",
            metadata={"date": "2026-04-14"},
        )
        _insert_derived_link(
            test_db, a_id=center_id, b_id=partner,
            rule="shared_thread", weight=1.0,
        )

        rewrite_derived_fences(
            test_db, {center_id}, vault_path=vault
        )

        leftover_tmp = list(vault.rglob("*.tmp"))
        assert leftover_tmp == [], f"unexpected tempfiles: {leftover_tmp!r}"

    def test_malformed_frontmatter_logged_and_skipped(
        self, test_db: psycopg.Connection, tmp_path: Path,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        # Defensive: a corrupt _ingested/ file with malformed YAML
        # frontmatter must not crash the rewriter — we log a warning and
        # move on. The file's bytes stay as-is so the user can fix it.
        vault = tmp_path / "vault"
        # Seed an ingested doc row pointing at a real file with bad YAML.
        doc_id, target = _seed_ingested_doc_with_file(
            test_db, vault,
            title="Broken",
            relative_vault_path="_ingested/krisp/broken.md",
            metadata={"date": "2026-04-15"},
        )
        # Stomp the file with deliberately malformed frontmatter.
        target.write_text(
            "---\nid: [unclosed\n---\n\nbody\n",
            encoding="utf-8",
        )
        before = target.read_text(encoding="utf-8")

        with caplog.at_level("WARNING", logger="brain.vault.derived_links.fence"):
            written = rewrite_derived_fences(
                test_db, {doc_id}, vault_path=vault
            )

        assert written == 0
        # File untouched.
        assert target.read_text(encoding="utf-8") == before
        # We logged something at WARNING level mentioning the file.
        assert any(
            "broken.md" in record.message
            for record in caplog.records
            if record.levelname == "WARNING"
        )

    def test_partner_id_in_input_drives_fence_for_partner_too(
        self, test_db: psycopg.Connection, tmp_path: Path
    ) -> None:
        # A partner that's in ``affected_ids`` AND has its own ingested
        # mirror file gets ITS fence rewritten. This is the symmetry the
        # affected-ids contract from D.3 enables: the renderer iterates
        # the full affected set, so both endpoints of an edge get their
        # "Related" sections regenerated in one pass.
        vault = tmp_path / "vault"
        a_id, a_target = _seed_ingested_doc_with_file(
            test_db, vault,
            title="Alpha",
            relative_vault_path="_ingested/krisp/alpha.md",
            metadata={"date": "2026-04-15"},
        )
        b_id, b_target = _seed_ingested_doc_with_file(
            test_db, vault,
            title="Bravo",
            relative_vault_path="_ingested/krisp/bravo.md",
            metadata={"date": "2026-04-15"},
        )
        # R3 (same_day_participant) — kept by FENCE_RULES. Using R2 here
        # would have the rule filter drop the edge and both files would
        # get an empty fence (one strip-fence write each = 2, but the
        # round-trip assertions about the bullet content would break).
        _insert_derived_link(
            test_db, a_id=a_id, b_id=b_id,
            rule="same_day_participant", weight=0.7,
        )

        written = rewrite_derived_fences(
            test_db, {a_id, b_id}, vault_path=vault
        )

        assert written == 2
        # Each side's fence points at the OTHER side.
        a_text = a_target.read_text(encoding="utf-8")
        b_text = b_target.read_text(encoding="utf-8")
        assert "[[bravo|Bravo]]" in a_text
        assert "[[alpha|Alpha]]" in b_text
