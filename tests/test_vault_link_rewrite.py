"""Tests for ``brain.vault.link_rewrite``.

Covers the pure-logic core (:func:`rewrite_wiki_links`) and the I/O
wrapper (:func:`rewrite_vault_links`). Uses the real ``test_db`` fixture
so resolver semantics are exercised against actual Postgres rows — the
plan calls these "DB-fixture tests"; they are the canonical home for
checking the rewrite contract one behavior at a time.

The integration with ``brain vault sync`` is exercised in
``tests/test_vault_sync_link_rewrite.py``.
"""
import hashlib
import json
import uuid
from pathlib import Path
from typing import Any

import psycopg
import pytest

from brain.vault.frontmatter import dump_frontmatter, parse_frontmatter
from brain.vault.link_rewrite import (
    rewrite_vault_links,
    rewrite_wiki_links,
)

# ---------------------------------------------------------------------------
# Test seed helpers — kept tiny so each test reads as setup → exercise → verify.
# ---------------------------------------------------------------------------


def _seed_doc(
    conn: psycopg.Connection[Any],
    *,
    title: str,
    vault_path: str | None,
    aliases: list[str] | None = None,
    kind: str = "vault",
    source_kind: str | None = None,
    external_id: str | None = None,
    metadata: dict[str, Any] | None = None,
) -> str:
    """Insert one ``documents`` row (and matching ``sources`` row when given).

    Returns the new document id. Used to populate the corpus the
    rewriter resolves against. Each test seeds just the rows its
    assertion exercises so test bodies stay declarative.
    """
    source_id: str | None = None
    if source_kind is not None:
        ext = external_id or str(uuid.uuid4())
        src_row = conn.execute(
            "INSERT INTO sources (kind, external_id, metadata) "
            "VALUES (%s, %s, %s::jsonb) RETURNING id::text",
            (source_kind, ext, json.dumps({})),
        ).fetchone()
        assert src_row is not None
        source_id = str(src_row[0])

    salted_body = f"body for {title}\n<!-- {uuid.uuid4()} -->"
    content_hash = hashlib.sha256(salted_body.encode("utf-8")).hexdigest()
    meta = dict(metadata or {})
    if aliases:
        meta["aliases"] = aliases
    row = conn.execute(
        """
        INSERT INTO documents
            (source_id, title, content, content_hash, content_type,
             source_path, tags, metadata, vault_path, kind)
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s::jsonb, %s, %s)
        RETURNING id::text
        """,
        (
            source_id,
            title,
            salted_body,
            content_hash,
            "note",
            None,
            [],
            json.dumps(meta),
            vault_path,
            kind,
        ),
    ).fetchone()
    assert row is not None
    return str(row[0])


@pytest.fixture
def src_doc_id(test_db: psycopg.Connection) -> str:
    """A throwaway vault-tier document id used as ``document_id`` for the
    rewriter. The rewriter excludes the source from candidate matches, so
    we never want this id to coincide with a target.
    """
    return _seed_doc(
        test_db,
        title="Source Note Title-That-Will-Never-Match",
        vault_path="src-note.md",
    )


# ---------------------------------------------------------------------------
# rewrite_wiki_links — pure-string + DB-fixture cases.
# ---------------------------------------------------------------------------


class TestRewriteWikiLinksTitleForms:
    """Each surface form the wiki-link parser produces gets its own case."""

    def test_title_form_rewrites_to_path_with_title_display(
        self, test_db: psycopg.Connection, src_doc_id: str
    ) -> None:
        _seed_doc(
            test_db,
            title="company-mc <> COMPANY_REDACTED - Recap",
            vault_path="_ingested/gmail/company-mc-company-ko-recap.md",
        )
        body = "See [[company-mc <> COMPANY_REDACTED - Recap]] for context."
        new_body, count = rewrite_wiki_links(
            body, document_id=src_doc_id, conn=test_db
        )
        assert count == 1
        assert (
            "[[_ingested/gmail/company-mc-company-ko-recap"
            "|company-mc <> COMPANY_REDACTED - Recap]]"
        ) in new_body
        assert "[[company-mc <> COMPANY_REDACTED - Recap]]" not in new_body

    def test_title_with_explicit_alias_preserves_alias(
        self, test_db: psycopg.Connection, src_doc_id: str
    ) -> None:
        _seed_doc(
            test_db,
            title="company-mc Recap",
            vault_path="_ingested/gmail/company-mc.md",
        )
        body = "See [[company-mc Recap|the recap]]."
        new_body, count = rewrite_wiki_links(
            body, document_id=src_doc_id, conn=test_db
        )
        assert count == 1
        assert "[[_ingested/gmail/company-mc|the recap]]" in new_body

    def test_title_with_heading_anchor_preserves_heading(
        self, test_db: psycopg.Connection, src_doc_id: str
    ) -> None:
        _seed_doc(
            test_db,
            title="company-mc Recap",
            vault_path="_ingested/gmail/company-mc.md",
        )
        body = "See [[company-mc Recap#Action Items]]."
        new_body, count = rewrite_wiki_links(
            body, document_id=src_doc_id, conn=test_db
        )
        assert count == 1
        assert (
            "[[_ingested/gmail/company-mc#Action Items|company-mc Recap]]"
        ) in new_body


class TestRewriteWikiLinksBrainIdForms:
    """``[[brain:<id>]]`` and ``[[brain:<id>|alias]]``."""

    def test_brain_id_no_display_uses_resolved_title(
        self, test_db: psycopg.Connection, src_doc_id: str
    ) -> None:
        target_id = _seed_doc(
            test_db,
            title="person-x Q1 Review",
            vault_path="_ingested/krisp/person-a-q1.md",
        )
        prefix = target_id[:8]
        body = f"Backlink: [[brain:{prefix}]]."
        new_body, count = rewrite_wiki_links(
            body, document_id=src_doc_id, conn=test_db
        )
        assert count == 1
        assert "[[_ingested/krisp/person-a-q1|person-x Q1 Review]]" in new_body

    def test_brain_id_with_alias_uses_alias(
        self, test_db: psycopg.Connection, src_doc_id: str
    ) -> None:
        target_id = _seed_doc(
            test_db,
            title="person-x Q1 Review",
            vault_path="_ingested/krisp/person-a-q1.md",
        )
        prefix = target_id[:8]
        body = f"Backlink: [[brain:{prefix}|the Q1 call]]."
        new_body, count = rewrite_wiki_links(
            body, document_id=src_doc_id, conn=test_db
        )
        assert count == 1
        assert "[[_ingested/krisp/person-a-q1|the Q1 call]]" in new_body


class TestRewriteWikiLinksSourceExternalForms:
    """``[[<source>:<external_id>|alias]]``."""

    def test_gmail_source_external_with_alias(
        self, test_db: psycopg.Connection, src_doc_id: str
    ) -> None:
        _seed_doc(
            test_db,
            title="Gmail Thread Subject",
            vault_path="_ingested/gmail/abc123.md",
            kind="ingested",
            source_kind="gmail",
            external_id="abc123",
        )
        body = "Email: [[gmail:abc123|the merger thread]]."
        new_body, count = rewrite_wiki_links(
            body, document_id=src_doc_id, conn=test_db
        )
        assert count == 1
        assert "[[_ingested/gmail/abc123|the merger thread]]" in new_body


class TestRewriteWikiLinksEmbedAndContextHandling:
    """Embeds + skips for code blocks / inline code / non-resolvable links."""

    def test_embed_form_rewrites_with_bang_prefix(
        self, test_db: psycopg.Connection, src_doc_id: str
    ) -> None:
        _seed_doc(
            test_db,
            title="company-mc Recap",
            vault_path="_ingested/gmail/company-mc.md",
        )
        body = "Embedded: ![[company-mc Recap]]"
        new_body, count = rewrite_wiki_links(
            body, document_id=src_doc_id, conn=test_db
        )
        assert count == 1
        assert "![[_ingested/gmail/company-mc|company-mc Recap]]" in new_body

    def test_link_inside_fenced_code_block_is_left_alone(
        self, test_db: psycopg.Connection, src_doc_id: str
    ) -> None:
        _seed_doc(
            test_db,
            title="company-mc Recap",
            vault_path="_ingested/gmail/company-mc.md",
        )
        body = "Code:\n```\nliteral [[company-mc Recap]] in fence\n```\n"
        new_body, count = rewrite_wiki_links(
            body, document_id=src_doc_id, conn=test_db
        )
        assert count == 0
        assert new_body == body

    def test_link_inside_inline_code_is_left_alone(
        self, test_db: psycopg.Connection, src_doc_id: str
    ) -> None:
        _seed_doc(
            test_db,
            title="company-mc Recap",
            vault_path="_ingested/gmail/company-mc.md",
        )
        body = "Reference: `[[company-mc Recap]]` in inline code."
        new_body, count = rewrite_wiki_links(
            body, document_id=src_doc_id, conn=test_db
        )
        assert count == 0
        assert new_body == body

    def test_unresolved_link_left_unchanged(
        self, test_db: psycopg.Connection, src_doc_id: str
    ) -> None:
        body = "Dangling: [[Nonexistent Title]]."
        new_body, count = rewrite_wiki_links(
            body, document_id=src_doc_id, conn=test_db
        )
        assert count == 0
        assert new_body == body

    def test_target_with_null_vault_path_left_unchanged(
        self, test_db: psycopg.Connection, src_doc_id: str
    ) -> None:
        _seed_doc(test_db, title="Free Floating", vault_path=None)
        body = "Floating: [[Free Floating]]."
        new_body, count = rewrite_wiki_links(
            body, document_id=src_doc_id, conn=test_db
        )
        assert count == 0
        assert new_body == body

    def test_self_reference_is_left_alone(
        self, test_db: psycopg.Connection
    ) -> None:
        # A note that contains its own title in a wiki-link should resolve
        # to nothing (the rewriter passes ``exclude_doc_id``); the link
        # is left as-is.
        self_id = _seed_doc(
            test_db, title="Loner Note", vault_path="loner-note.md"
        )
        body = "Talking about [[Loner Note]] again."
        new_body, count = rewrite_wiki_links(
            body, document_id=self_id, conn=test_db
        )
        assert count == 0
        assert new_body == body

    def test_already_path_form_is_idempotent_fast_path(
        self, test_db: psycopg.Connection, src_doc_id: str
    ) -> None:
        _seed_doc(
            test_db,
            title="company-mc Recap",
            vault_path="_ingested/gmail/company-mc.md",
        )
        # Body already in the canonical post-rewrite shape.
        body = "Stable: [[_ingested/gmail/company-mc|company-mc Recap]]."
        new_body, count = rewrite_wiki_links(
            body, document_id=src_doc_id, conn=test_db
        )
        assert count == 0
        assert new_body == body


class TestRewriteWikiLinksMixedAndMultiple:
    """Whole-body cases — multiple links of mixed shapes."""

    def test_multiple_refs_same_target_preserve_individual_displays(
        self, test_db: psycopg.Connection, src_doc_id: str
    ) -> None:
        _seed_doc(
            test_db,
            title="company-mc Recap",
            vault_path="_ingested/gmail/company-mc.md",
        )
        body = (
            "First: [[company-mc Recap|the recap]].\n"
            "Second: [[company-mc Recap|that call]].\n"
        )
        new_body, count = rewrite_wiki_links(
            body, document_id=src_doc_id, conn=test_db
        )
        assert count == 2
        assert "[[_ingested/gmail/company-mc|the recap]]" in new_body
        assert "[[_ingested/gmail/company-mc|that call]]" in new_body

    def test_mix_resolved_and_unresolved_only_rewrites_resolved(
        self, test_db: psycopg.Connection, src_doc_id: str
    ) -> None:
        _seed_doc(
            test_db,
            title="company-mc Recap",
            vault_path="_ingested/gmail/company-mc.md",
        )
        body = (
            "Resolved: [[company-mc Recap]].\n"
            "Dangling: [[Nonexistent Title]]."
        )
        new_body, count = rewrite_wiki_links(
            body, document_id=src_doc_id, conn=test_db
        )
        assert count == 1
        assert "[[_ingested/gmail/company-mc|company-mc Recap]]" in new_body
        # Unresolved survives untouched.
        assert "[[Nonexistent Title]]" in new_body

    def test_company_ko_session_regression(
        self, test_db: psycopg.Connection, src_doc_id: str
    ) -> None:
        """Reproduce the exact bug from the user's `company-ko.md` session.

        Seven links: four ``[[Title]]`` forms and three
        ``[[brain:<id>|alias]]`` forms. Every one must rewrite to a
        vault-root-relative path with the correct display, and every one
        must resolve in the brain DB after the rewrite.
        """
        # Four title-form targets.
        _seed_doc(
            test_db,
            title="company-mc <> COMPANY_REDACTED - Recap",
            vault_path=(
                "_ingested/gmail/Mon, 20 Ap-19dacef6-re-company-mc-company-ko-recap.md"
            ),
        )
        _seed_doc(
            test_db,
            title="company-mc — Pricing",
            vault_path="_ingested/gmail/company-mc-pricing.md",
        )
        _seed_doc(
            test_db,
            title="company-mc Sales Cadence",
            vault_path="_ingested/gmail/company-mc-cadence.md",
        )
        _seed_doc(
            test_db,
            title="COMPANY_REDACTED Roadmap",
            vault_path="company-ko-roadmap.md",
        )
        # Three brain-id-form targets — keep the prefix unique so the
        # resolver returns a single match.
        b1 = _seed_doc(
            test_db,
            title="person-x 1:1 — March",
            vault_path="_ingested/krisp/person-a-march.md",
        )
        b2 = _seed_doc(
            test_db,
            title="Account Plan — company-mc",
            vault_path="account-plan-company-mc.md",
        )
        b3 = _seed_doc(
            test_db,
            title="2026-Q2 OKRs",
            vault_path="2026-q2-okrs.md",
        )

        body = (
            "Recap: [[company-mc <> COMPANY_REDACTED - Recap]].\n"
            "Pricing: [[company-mc — Pricing]].\n"
            "Cadence: [[company-mc Sales Cadence]].\n"
            "Roadmap: [[COMPANY_REDACTED Roadmap]].\n"
            f"person-x: [[brain:{b1[:8]}|the March 1:1]].\n"
            f"Plan: [[brain:{b2[:8]}|the account plan]].\n"
            f"OKRs: [[brain:{b3[:8]}|Q2 OKRs]].\n"
        )
        new_body, count = rewrite_wiki_links(
            body, document_id=src_doc_id, conn=test_db
        )

        assert count == 7
        # Every replacement is a vault-root-relative path with display.
        assert (
            "[[_ingested/gmail/Mon, 20 Ap-19dacef6-re-company-mc-company-ko-recap"
            "|company-mc <> COMPANY_REDACTED - Recap]]"
        ) in new_body
        assert "[[_ingested/gmail/company-mc-pricing|company-mc — Pricing]]" in new_body
        assert (
            "[[_ingested/gmail/company-mc-cadence|company-mc Sales Cadence]]"
        ) in new_body
        assert "[[company-ko-roadmap|COMPANY_REDACTED Roadmap]]" in new_body
        assert "[[_ingested/krisp/person-a-march|the March 1:1]]" in new_body
        assert "[[account-plan-company-mc|the account plan]]" in new_body
        assert "[[2026-q2-okrs|Q2 OKRs]]" in new_body

        # And every rewritten link must round-trip through the resolver
        # (path-form must still resolve so brain-side `links` materialization
        # stays consistent).
        from brain.vault.links import parse_wiki_links
        from brain.vault.resolver import resolve_link

        for parsed in parse_wiki_links(new_body):
            target = resolve_link(test_db, parsed, exclude_doc_id=src_doc_id)
            assert target is not None, parsed.raw


class TestRewriteWikiLinksIdempotency:
    """Two passes over the same body should yield byte-identical output."""

    def test_second_pass_is_noop(
        self, test_db: psycopg.Connection, src_doc_id: str
    ) -> None:
        _seed_doc(
            test_db,
            title="company-mc Recap",
            vault_path="_ingested/gmail/company-mc.md",
        )
        body = "[[company-mc Recap]] and [[company-mc Recap|other]]"
        first, count_first = rewrite_wiki_links(
            body, document_id=src_doc_id, conn=test_db
        )
        assert count_first == 2
        second, count_second = rewrite_wiki_links(
            first, document_id=src_doc_id, conn=test_db
        )
        assert count_second == 0
        assert second == first


# ---------------------------------------------------------------------------
# rewrite_vault_links — I/O wrapper.
# ---------------------------------------------------------------------------


class TestRewriteVaultLinksIO:
    """File-level wrapper: reads, rewrites, writes back atomically."""

    def test_writes_back_when_links_changed(
        self,
        test_db: psycopg.Connection,
        src_doc_id: str,
        tmp_path: Path,
    ) -> None:
        _seed_doc(
            test_db,
            title="company-mc Recap",
            vault_path="_ingested/gmail/company-mc.md",
        )
        note = tmp_path / "k.md"
        note.write_text(
            dump_frontmatter(
                {"id": src_doc_id, "title": "Source"},
                "Body: [[company-mc Recap]]\n",
            )
        )
        changed = rewrite_vault_links(
            note,
            document_id=src_doc_id,
            conn=test_db,
        )
        assert changed is True
        text = note.read_text()
        # Frontmatter preserved verbatim.
        fm, body = parse_frontmatter(text)
        assert fm["id"] == src_doc_id
        assert fm["title"] == "Source"
        assert "[[_ingested/gmail/company-mc|company-mc Recap]]" in body

    def test_no_change_when_already_canonical(
        self,
        test_db: psycopg.Connection,
        src_doc_id: str,
        tmp_path: Path,
    ) -> None:
        _seed_doc(
            test_db,
            title="company-mc Recap",
            vault_path="_ingested/gmail/company-mc.md",
        )
        note = tmp_path / "k.md"
        original = dump_frontmatter(
            {"id": src_doc_id, "title": "Source"},
            "Body: [[_ingested/gmail/company-mc|company-mc Recap]]\n",
        )
        note.write_text(original)
        changed = rewrite_vault_links(
            note,
            document_id=src_doc_id,
            conn=test_db,
        )
        assert changed is False
        # Bytes on disk are unchanged.
        assert note.read_text() == original

    def test_unreadable_file_returns_false_without_raising(
        self,
        test_db: psycopg.Connection,
        src_doc_id: str,
        tmp_path: Path,
    ) -> None:
        # Pointing at a nonexistent path triggers the OSError branch — the
        # rewriter must swallow + return False so a sync run keeps going.
        missing = tmp_path / "does-not-exist.md"
        changed = rewrite_vault_links(
            missing,
            document_id=src_doc_id,
            conn=test_db,
        )
        assert changed is False

    def test_malformed_frontmatter_returns_false_without_raising(
        self,
        test_db: psycopg.Connection,
        src_doc_id: str,
        tmp_path: Path,
    ) -> None:
        note = tmp_path / "broken.md"
        # Closing fence with malformed YAML inside.
        note.write_text("---\n: not valid : yaml :\n---\n\nbody\n")
        changed = rewrite_vault_links(
            note,
            document_id=src_doc_id,
            conn=test_db,
        )
        assert changed is False

    def test_no_links_in_body_returns_false(
        self,
        test_db: psycopg.Connection,
        src_doc_id: str,
        tmp_path: Path,
    ) -> None:
        note = tmp_path / "plain.md"
        original = dump_frontmatter(
            {"id": src_doc_id, "title": "Plain"},
            "Just plain prose, no links.\n",
        )
        note.write_text(original)
        changed = rewrite_vault_links(
            note,
            document_id=src_doc_id,
            conn=test_db,
        )
        assert changed is False
        assert note.read_text() == original


# ---------------------------------------------------------------------------
# Resolver path-form regression — ensures the brain DB still resolves
# rewritten links so `brain links` / `brain backlinks` keep working after a
# rewrite pass.
# ---------------------------------------------------------------------------


class TestRewriteWikiLinksDefensiveBranches:
    """Coverage for the defensive error / edge-case branches.

    These exercise paths a happy production run never sees but that the
    rewriter must handle without raising (and without losing a sync).
    """

    def test_resolver_psycopg_error_is_swallowed_per_link(
        self,
        test_db: psycopg.Connection,
        src_doc_id: str,
        mocker,
    ) -> None:
        # Patch the resolve_link the rewriter imports so it raises on every
        # call. The rewriter must continue past the failure and return the
        # body unchanged with zero replacements.
        mocker.patch(
            "brain.vault.link_rewrite.resolve_link",
            side_effect=psycopg.errors.OperationalError("boom"),
        )
        body = "Link: [[company-mc Recap]]"
        new_body, count = rewrite_wiki_links(
            body, document_id=src_doc_id, conn=test_db
        )
        assert count == 0
        assert new_body == body

    def test_target_row_vanished_skipped(
        self,
        test_db: psycopg.Connection,
        src_doc_id: str,
        mocker,
    ) -> None:
        # Resolver succeeds, but the SELECT for vault_path/title returns
        # nothing (race: row deleted after resolve). The rewriter leaves
        # the link untouched.
        from brain.vault.resolver import ResolvedTarget

        mocker.patch(
            "brain.vault.link_rewrite.resolve_link",
            return_value=ResolvedTarget(
                document_id="00000000-0000-0000-0000-000000000000",
                kind="vault",
            ),
        )
        body = "Link: [[Anything]]"
        new_body, count = rewrite_wiki_links(
            body, document_id=src_doc_id, conn=test_db
        )
        assert count == 0
        assert new_body == body

    def test_strip_md_extension_passthrough_for_no_suffix(
        self, test_db: psycopg.Connection, src_doc_id: str
    ) -> None:
        # vault_path stored without a trailing `.md` (corrupted row /
        # legacy data) — the rewriter falls back to using the path verbatim
        # rather than crashing. The link is still rewritten.
        _seed_doc(
            test_db,
            title="No Extension",
            vault_path="legacy-path",
        )
        body = "Ref: [[No Extension]]"
        new_body, count = rewrite_wiki_links(
            body, document_id=src_doc_id, conn=test_db
        )
        assert count == 1
        assert "[[legacy-path|No Extension]]" in new_body


class TestRewriteVaultLinksDefensiveBranches:
    """The I/O wrapper's psycopg + OSError branches."""

    def test_db_error_during_rewrite_returns_false(
        self,
        test_db: psycopg.Connection,
        src_doc_id: str,
        tmp_path: Path,
        mocker,
    ) -> None:
        note = tmp_path / "k.md"
        note.write_text(
            dump_frontmatter(
                {"id": src_doc_id, "title": "Source"},
                "Body: [[company-mc Recap]]\n",
            )
        )
        # Force the inner pure helper to raise a psycopg error.
        mocker.patch(
            "brain.vault.link_rewrite.rewrite_wiki_links",
            side_effect=psycopg.errors.OperationalError("boom"),
        )
        changed = rewrite_vault_links(
            note,
            document_id=src_doc_id,
            conn=test_db,
        )
        assert changed is False

    def test_oserror_on_write_returns_false(
        self,
        test_db: psycopg.Connection,
        src_doc_id: str,
        tmp_path: Path,
        mocker,
    ) -> None:
        _seed_doc(
            test_db,
            title="company-mc Recap",
            vault_path="_ingested/gmail/company-mc.md",
        )
        note = tmp_path / "k.md"
        note.write_text(
            dump_frontmatter(
                {"id": src_doc_id, "title": "Source"},
                "Body: [[company-mc Recap]]\n",
            )
        )
        # Force the atomic write to blow up.
        mocker.patch(
            "brain.vault.link_rewrite.atomic_write_text",
            side_effect=OSError("disk full"),
        )
        changed = rewrite_vault_links(
            note,
            document_id=src_doc_id,
            conn=test_db,
        )
        assert changed is False


class TestPathFormResolverRegression:
    def test_path_form_resolves_via_vault_path(
        self, test_db: psycopg.Connection, src_doc_id: str
    ) -> None:
        from brain.vault.links import parse_wiki_links
        from brain.vault.resolver import resolve_link

        target_id = _seed_doc(
            test_db,
            title="company-mc Recap",
            vault_path="_ingested/gmail/company-mc.md",
        )
        # Path-form link, as produced by the rewriter.
        parsed = parse_wiki_links("[[_ingested/gmail/company-mc|company-mc Recap]]")
        assert len(parsed) == 1
        target = resolve_link(test_db, parsed[0], exclude_doc_id=src_doc_id)
        assert target is not None
        assert target.document_id == target_id
