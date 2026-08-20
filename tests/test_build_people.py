"""Tests for ``brain.people`` — aggregation + People Hub page emission.

Phase A: ``aggregate_people`` (real-Postgres SELECT against seeded
``sources`` / ``documents`` / ``directory_entries`` rows; no chunker /
embedder in the loop).

Phase B: ``render_person_md`` / ``render_index_md`` (pure-string
golden-shape tests) and ``emit_people_pages`` (DB → tmp_path round-trip
with idempotency + cleanup contracts).
"""
import hashlib
import json
import uuid
from pathlib import Path
from typing import Any

import psycopg
import pytest

from brain.people import (
    DocRef,
    EmitReport,
    PersonRecord,
    aggregate_people,
    emit_people_pages,
    render_index_md,
    render_person_md,
)
from brain.vault.derived_links.directory import DirectoryStore
from brain.vault.frontmatter import parse_frontmatter

# --------------------------------------------------------------------------
# Helpers — direct SQL seeding mirrors tests/derived_links/test_pass_runner.py
# so the corpus shape matches a real ingest without booting the embedder.
# --------------------------------------------------------------------------


def _seed_doc(
    conn: psycopg.Connection[Any],
    *,
    source_kind: str,
    external_id: str,
    metadata: dict[str, Any],
    title: str,
    content: str = "body",
    content_type: str = "transcript",
    vault_path: str | None = None,
    draft: bool = False,
) -> str:
    """Insert a ``sources`` + ``documents`` pair, return the new document id.

    Each call salts ``content`` with a random suffix so the global
    ``content_hash`` UNIQUE constraint never collides between test docs.
    """
    src_row = conn.execute(
        "INSERT INTO sources (kind, external_id, metadata) "
        "VALUES (%s, %s, %s::jsonb) RETURNING id",
        (source_kind, external_id, json.dumps({})),
    ).fetchone()
    assert src_row is not None
    source_id = src_row[0]

    salted = f"{content}\n<!-- {uuid.uuid4()} -->"
    content_hash = hashlib.sha256(salted.encode("utf-8")).hexdigest()

    doc_row = conn.execute(
        """
        INSERT INTO documents
            (source_id, title, content, content_hash, content_type,
             source_path, tags, metadata, vault_path, draft)
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s::jsonb, %s, %s)
        RETURNING id::text
        """,
        (
            source_id,
            title,
            salted,
            content_hash,
            content_type,
            None,
            [],
            json.dumps(metadata),
            vault_path,
            draft,
        ),
    ).fetchone()
    assert doc_row is not None
    return str(doc_row[0])


def _record_by_name(
    records: list[PersonRecord], display_name: str
) -> PersonRecord:
    """Pluck a single record by display_name, asserting uniqueness."""
    matches = [r for r in records if r.display_name == display_name]
    assert len(matches) == 1, f"expected exactly one record for {display_name!r}, got {matches}"
    return matches[0]


# --------------------------------------------------------------------------
# A.1 — basic aggregation
# --------------------------------------------------------------------------


class TestSingleGmailDocTwoParticipants:
    """Single Gmail doc with two participants → both yield 1-doc PersonRecords."""

    def test_emits_record_per_participant_with_one_doc_each(
        self, test_db: psycopg.Connection[Any]
    ) -> None:
        store = DirectoryStore(test_db)
        store.upsert_pair(
            display_name="Alice", email="alice@example.com", source="gmail"
        )
        store.upsert_pair(
            display_name="Bob", email="bob@example.com", source="gmail"
        )
        _seed_doc(
            test_db,
            source_kind="gmail",
            external_id="msg-1",
            title="Lunch plans",
            metadata={
                "from": "Alice <alice@example.com>",
                "to": "Bob <bob@example.com>",
                "thread_id": "t-1",
                "date": "Wed, 15 Apr 2026 12:00:00 -0700",
            },
            vault_path="_ingested/gmail/2026-04-15-deadbeef-lunch-plans.md",
        )

        records = aggregate_people(
            test_db, owner_keys=frozenset(), min_docs=1
        )

        names = [r.display_name for r in records]
        assert names == ["alice", "bob"]

        alice = _record_by_name(records, "alice")
        assert alice.slug == "alice"
        assert alice.primary_email == "alice@example.com"
        assert alice.all_emails == ["alice@example.com"]
        assert len(alice.docs) == 1
        assert alice.docs[0].source_kind == "gmail"
        assert alice.docs[0].title == "Lunch plans"
        assert alice.docs[0].vault_target == (
            "_ingested/gmail/2026-04-15-deadbeef-lunch-plans"
        )
        assert alice.docs[0].date is not None
        assert alice.docs[0].date.year == 2026

        bob = _record_by_name(records, "bob")
        assert bob.slug == "bob"
        assert len(bob.docs) == 1


class TestKrispNameAndEmailKeysDedupe:
    """Krisp transcript whose `_participant_keys` lists both an email and
    its corresponding normalized name resolves to a single PersonRecord
    holding the doc once (regression: name-only key still resolves via
    DirectoryStore)."""

    def test_email_and_name_keys_resolve_to_same_person_once(
        self, test_db: psycopg.Connection[Any]
    ) -> None:
        store = DirectoryStore(test_db)
        store.upsert_pair(
            display_name="person-person-luke",
            email="person-luke@example.com",
            source="people_yml",
        )

        _seed_doc(
            test_db,
            source_kind="krisp",
            external_id="krisp-1",
            title="Sync with person-person-luke",
            metadata={
                "_participant_keys": ["person-luke@example.com", "person-person-luke"],
                "date": "2026-04-29",
            },
            content_type="transcript",
        )

        records = aggregate_people(
            test_db, owner_keys=frozenset(), min_docs=1
        )

        luke = _record_by_name(records, "person-person-luke")
        assert luke.in_people_yml is True
        assert luke.primary_email == "person-luke@example.com"
        # The doc is listed once even though both keys resolved to person-person-luke.
        assert len(luke.docs) == 1
        assert luke.docs[0].source_kind == "krisp"

    def test_krisp_name_only_key_resolves_via_directory(
        self, test_db: psycopg.Connection[Any]
    ) -> None:
        # Regression guard: the linker bridges name-only Krisp speakers to
        # emails via DirectoryStore; aggregate_people must reach the same
        # person even when the doc only carries the name token.
        store = DirectoryStore(test_db)
        store.upsert_pair(
            display_name="person-person-luke",
            email="person-luke@example.com",
            source="people_yml",
        )

        _seed_doc(
            test_db,
            source_kind="krisp",
            external_id="krisp-name-only",
            title="Standup",
            metadata={
                "_participant_keys": ["person-person-luke"],
                "date": "2026-04-30",
            },
        )

        records = aggregate_people(
            test_db, owner_keys=frozenset(), min_docs=1
        )

        luke = _record_by_name(records, "person-person-luke")
        assert len(luke.docs) == 1


# --------------------------------------------------------------------------
# A.1 — threshold + people_yml override
# --------------------------------------------------------------------------


class TestThresholdAndPeopleYmlOverride:
    """`_people.yml` curated person with 1 doc still emits; threshold person
    with 2 docs is filtered out at default min_docs=3."""

    def test_curated_person_below_threshold_still_emits(
        self, test_db: psycopg.Connection[Any]
    ) -> None:
        store = DirectoryStore(test_db)
        store.upsert_pair(
            display_name="person-person-luke",
            email="person-luke@example.com",
            source="people_yml",
        )
        store.upsert_pair(
            display_name="Dan Jones",
            email="dan@example.com",
            source="gmail",
        )

        # person-person-luke: 1 Gmail doc (below threshold but curated).
        _seed_doc(
            test_db,
            source_kind="gmail",
            external_id="gmail-person-luke",
            title="Re: thing",
            metadata={
                "from": "person-person-luke <person-luke@example.com>",
                "to": "pat@example.com",
                "thread_id": "t-person-luke",
                "date": "Wed, 15 Apr 2026 12:00:00 -0700",
            },
        )

        # Dan: 2 Gmail docs (below default threshold of 3).
        for i in range(2):
            _seed_doc(
                test_db,
                source_kind="gmail",
                external_id=f"gmail-dan-{i}",
                title=f"Dan note {i}",
                metadata={
                    "from": "Dan Jones <dan@example.com>",
                    "to": "pat@example.com",
                    "thread_id": f"t-dan-{i}",
                    "date": "Wed, 16 Apr 2026 12:00:00 -0700",
                },
            )

        records = aggregate_people(
            test_db, owner_keys=frozenset(), min_docs=3
        )

        names = [r.display_name for r in records]
        assert "person-person-luke" in names
        assert "dan jones" not in names

        luke = _record_by_name(records, "person-person-luke")
        assert luke.in_people_yml is True
        assert len(luke.docs) == 1

    def test_threshold_person_at_or_above_min_docs_emits(
        self, test_db: psycopg.Connection[Any]
    ) -> None:
        store = DirectoryStore(test_db)
        store.upsert_pair(
            display_name="Dan Jones",
            email="dan@example.com",
            source="gmail",
        )
        for i in range(3):
            _seed_doc(
                test_db,
                source_kind="gmail",
                external_id=f"gmail-dan-{i}",
                title=f"Dan note {i}",
                metadata={
                    "from": "Dan Jones <dan@example.com>",
                    "to": "pat@example.com",
                    "thread_id": f"t-dan-{i}",
                    "date": "Wed, 16 Apr 2026 12:00:00 -0700",
                },
            )

        records = aggregate_people(
            test_db, owner_keys=frozenset(), min_docs=3
        )

        dan = _record_by_name(records, "dan jones")
        assert dan.in_people_yml is False
        assert len(dan.docs) == 3

    def test_curated_zero_doc_person_still_emits(
        self, test_db: psycopg.Connection[Any]
    ) -> None:
        # A curated entry with no Gmail / Krisp activity still gets a record
        # (Phase B will render a "no documents yet" placeholder page).
        store = DirectoryStore(test_db)
        store.upsert_pair(
            display_name="Future Friend",
            email="future@example.com",
            source="people_yml",
        )
        records = aggregate_people(
            test_db, owner_keys=frozenset(), min_docs=3
        )
        rec = _record_by_name(records, "future friend")
        assert rec.in_people_yml is True
        assert rec.docs == []


# --------------------------------------------------------------------------
# A.1 — slug collision
# --------------------------------------------------------------------------


class TestSlugCollision:
    """Two *canonically distinct* display_names that slugify to the same slug
    → second gets the ``-2`` suffix; alpha order on the canonical key decides
    who wins the bare slug."""

    def test_collision_resolves_with_numeric_suffix(
        self, test_db: psycopg.Connection[Any]
    ) -> None:
        store = DirectoryStore(test_db)
        # ``john doe`` (canonical "john doe") and ``john-doe`` (canonical
        # "john-doe" — the hyphen is preserved, NOT collapsed) are two distinct
        # people whose slugs both reduce to "john-doe", so the second collides.
        store.upsert_pair(
            display_name="john doe", email="j@example.com", source="people_yml"
        )
        store.upsert_pair(
            display_name="john-doe", email="jd@example.com", source="people_yml"
        )

        records = aggregate_people(
            test_db, owner_keys=frozenset(), min_docs=3
        )
        names = [r.display_name for r in records]
        assert "john doe" in names
        assert "john-doe" in names

        first = _record_by_name(records, "john doe")
        second = _record_by_name(records, "john-doe")
        # ASCII space (32) < hyphen (45), so "john doe" wins the alpha tiebreak.
        assert first.slug == "john-doe"
        assert second.slug == "john-doe-2"


class TestSeparatorMerge:
    """Phase 1 regression: handle-style separators (``.`` / ``_``) collapse so
    ``Jane.Doe`` / ``jane_doe`` / ``Jane Doe`` all merge into ONE person with a
    single canonical key and a merged email list."""

    def test_dot_underscore_and_spaced_variants_merge(
        self, test_db: psycopg.Connection[Any]
    ) -> None:
        store = DirectoryStore(test_db)
        # Three directory rows for the same human under three separator styles.
        store.upsert_pair(
            display_name="jane doe", email="jane@example.com", source="people_yml"
        )
        store.upsert_pair(
            display_name="jane_doe", email="jane.alt@example.com", source="gmail"
        )
        # ``normalize_participant`` keeps the dot internal; the Phase 1
        # normalizer then collapses it to a space at canonical-key time.
        store.upsert_pair(
            display_name="jane.doe", email="jane.work@example.com", source="gmail"
        )

        records = aggregate_people(
            test_db, owner_keys=frozenset(), min_docs=0
        )

        # All three collapsed to the single canonical key "jane doe".
        jane_records = [r for r in records if r.display_name == "jane doe"]
        assert len(jane_records) == 1
        jane = jane_records[0]
        assert jane.all_emails == [
            "jane.alt@example.com",
            "jane.work@example.com",
            "jane@example.com",
        ]
        # people_yml among the merged rows → curated badge sticks.
        assert jane.in_people_yml is True


# --------------------------------------------------------------------------
# A.2 — owner exclusion
# --------------------------------------------------------------------------


class TestOwnerExclusion:
    """Owner keys are stripped from every person's match set, and no
    PersonRecord is emitted for the owner themselves."""

    def test_owner_not_emitted_and_owner_keys_stripped_from_others(
        self, test_db: psycopg.Connection[Any]
    ) -> None:
        store = DirectoryStore(test_db)
        store.upsert_pair(
            display_name="Pat Owner",
            email="pat.owner@example.com",
            source="people_yml",
        )
        store.upsert_pair(
            display_name="person-person-luke",
            email="person-luke@example.com",
            source="people_yml",
        )

        # Gmail thread owner↔person-person-luke. Owner_keys strips the owner →
        # only person-person-luke gets a record, with this doc on his roster.
        _seed_doc(
            test_db,
            source_kind="gmail",
            external_id="gmail-owner-luke",
            title="Coffee?",
            metadata={
                "from": "Pat Owner <pat.owner@example.com>",
                "to": "person-person-luke <person-luke@example.com>",
                "thread_id": "t-coffee",
                "date": "Wed, 15 Apr 2026 12:00:00 -0700",
            },
        )

        # Krisp call where the owner appears in _participant_keys — same exclusion.
        _seed_doc(
            test_db,
            source_kind="krisp",
            external_id="krisp-owner-luke",
            title="1:1",
            metadata={
                "_participant_keys": [
                    "pat.owner@example.com",
                    "pat owner",
                    "person-luke@example.com",
                    "person-person-luke",
                ],
                "date": "2026-04-29",
            },
        )

        owner_keys = frozenset({"pat.owner@example.com", "pat owner"})
        records = aggregate_people(
            test_db, owner_keys=owner_keys, min_docs=1
        )

        names = [r.display_name for r in records]
        assert names == ["person-person-luke"]

        luke = _record_by_name(records, "person-person-luke")
        # Two docs (gmail + krisp) — both list person-person-luke once (owner stripped).
        assert len(luke.docs) == 2
        sources = sorted(d.source_kind for d in luke.docs)
        assert sources == ["gmail", "krisp"]

    def test_owner_strip_drops_doc_with_only_owner_participants(
        self, test_db: psycopg.Connection[Any]
    ) -> None:
        # A doc whose only resolvable participants are owner keys contributes
        # zero rosters — nobody (not even owner-self) gets it.
        store = DirectoryStore(test_db)
        store.upsert_pair(
            display_name="Pat Owner",
            email="pat.owner@example.com",
            source="people_yml",
        )
        store.upsert_pair(
            display_name="person-person-luke",
            email="person-luke@example.com",
            source="people_yml",
        )
        _seed_doc(
            test_db,
            source_kind="gmail",
            external_id="gmail-self",
            title="Note to self",
            metadata={
                "from": "Pat Owner <pat.owner@example.com>",
                "to": "pat.owner@example.com",
                "thread_id": "t-self",
                "date": "Wed, 15 Apr 2026 12:00:00 -0700",
            },
        )
        records = aggregate_people(
            test_db,
            owner_keys=frozenset({"pat.owner@example.com", "pat owner"}),
            min_docs=1,
        )
        # person-person-luke is curated so he still emits, but with zero docs — the
        # owner-only doc contributed nothing to his roster.
        names = [r.display_name for r in records]
        assert names == ["person-person-luke"]
        luke = _record_by_name(records, "person-person-luke")
        assert luke.docs == []


# --------------------------------------------------------------------------
# Phase 1 — automated-sender + owner-variant filtering, via-decoration
# --------------------------------------------------------------------------


class TestAutomatedSenderFiltering:
    """no-reply / notification / org senders never become a person, even with
    many docs."""

    def test_no_reply_sender_dropped(
        self, test_db: psycopg.Connection[Any]
    ) -> None:
        store = DirectoryStore(test_db)
        store.upsert_pair(
            display_name="Acme Notifications",
            email="no-reply@acme.example.com",
            source="gmail",
        )
        store.upsert_pair(
            display_name="Jane Doe", email="jane@example.com", source="gmail"
        )
        # The automated sender appears on many docs; the real person on a few.
        for i in range(5):
            _seed_doc(
                test_db,
                source_kind="gmail",
                external_id=f"noreply-{i}",
                title=f"Statement {i}",
                metadata={
                    "from": "Acme Notifications <no-reply@acme.example.com>",
                    "to": "Jane Doe <jane@example.com>",
                    "thread_id": f"t-noreply-{i}",
                    "date": "Wed, 15 Apr 2026 12:00:00 -0700",
                },
            )
        records = aggregate_people(
            test_db, owner_keys=frozenset(), min_docs=1
        )
        names = [r.display_name for r in records]
        assert "acme notifications" not in names
        assert "jane doe" in names

    def test_extra_denylist_entry_dropped(
        self, test_db: psycopg.Connection[Any]
    ) -> None:
        store = DirectoryStore(test_db)
        store.upsert_pair(
            display_name="Billing Team",
            email="billing@acme.example.com",
            source="gmail",
        )
        _seed_doc(
            test_db,
            source_kind="gmail",
            external_id="billing-1",
            title="Invoice",
            metadata={
                "from": "Billing Team <billing@acme.example.com>",
                "to": "pat@example.com",
                "thread_id": "t-billing",
                "date": "Wed, 15 Apr 2026 12:00:00 -0700",
            },
        )
        records = aggregate_people(
            test_db,
            owner_keys=frozenset(),
            min_docs=1,
            sender_denylist=frozenset({"billing@"}),
        )
        assert "billing team" not in [r.display_name for r in records]


class TestOwnerVariantFiltering:
    """The owner can't leak in under a first-name-only / local-part variant —
    but a DISTINCT person who merely shares the owner's first name must survive
    (no over-filtering). Owner names here are SYNTHETIC ("Pat Owner")."""

    def test_owner_variant_dropped_distinct_person_kept(
        self, test_db: psycopg.Connection[Any]
    ) -> None:
        store = DirectoryStore(test_db)
        # Both entries are curated (people_yml) so the doc-count threshold is
        # never the reason either is dropped — only the owner filter can be.
        # "Pat" is the owner's leaked first-name-only variant; "Pat Rivera" is
        # a distinct person who happens to share the first name.
        store.upsert_pair(
            display_name="Pat", email="pat.leak@example.com", source="people_yml"
        )
        store.upsert_pair(
            display_name="Pat Rivera",
            email="pat.rivera@example.com",
            source="people_yml",
        )
        records = aggregate_people(
            test_db,
            owner_keys=frozenset({"pat owner", "pat.owner@example.com"}),
            min_docs=1,
        )
        names = [r.display_name for r in records]
        # The bare owner first-name variant is filtered out…
        assert "pat" not in names
        # …but the distinct person who shares the first name is KEPT.
        assert "pat rivera" in names


class TestViaDecorationMerge:
    """Mailing-list ``via X`` decoration is stripped at canonical-key time so
    the person merges with their clean name."""

    def test_via_name_merges_with_clean_name(
        self, test_db: psycopg.Connection[Any]
    ) -> None:
        store = DirectoryStore(test_db)
        # Clean entry + a Google-Groups-rewritten "via" entry, same human.
        store.upsert_pair(
            display_name="Jane Doe", email="jane@example.com", source="people_yml"
        )
        # ``normalize_participant`` lowercases + collapses but keeps the "via"
        # text; the Phase 1 normalizer strips it at canonical-key time.
        store.upsert_pair(
            display_name="Jane Doe via Acme Members",
            email="jane.list@example.com",
            source="gmail",
        )
        records = aggregate_people(
            test_db, owner_keys=frozenset(), min_docs=0
        )
        jane_records = [r for r in records if r.display_name == "jane doe"]
        assert len(jane_records) == 1
        assert "jane.list@example.com" in jane_records[0].all_emails


# --------------------------------------------------------------------------
# Defensive coverage
# --------------------------------------------------------------------------


class TestDefensive:
    """Edge cases: empty corpus, drafts excluded, dating fallback, etc."""

    def test_empty_directory_returns_empty(
        self, test_db: psycopg.Connection[Any]
    ) -> None:
        assert aggregate_people(
            test_db, owner_keys=frozenset(), min_docs=3
        ) == []

    def test_negative_min_docs_raises(
        self, test_db: psycopg.Connection[Any]
    ) -> None:
        with pytest.raises(ValueError, match="min_docs"):
            aggregate_people(test_db, owner_keys=frozenset(), min_docs=-1)

    def test_drafts_excluded(self, test_db: psycopg.Connection[Any]) -> None:
        store = DirectoryStore(test_db)
        store.upsert_pair(
            display_name="Bob",
            email="bob@example.com",
            source="people_yml",
        )
        # One real doc + one draft — only the real one shows up.
        _seed_doc(
            test_db,
            source_kind="gmail",
            external_id="real",
            title="Real",
            metadata={
                "from": "Bob <bob@example.com>",
                "to": "pat@example.com",
                "thread_id": "t-real",
                "date": "Wed, 15 Apr 2026 12:00:00 -0700",
            },
        )
        _seed_doc(
            test_db,
            source_kind="gmail",
            external_id="draft",
            title="Draft",
            metadata={
                "from": "Bob <bob@example.com>",
                "to": "pat@example.com",
                "thread_id": "t-draft",
                "date": "Thu, 16 Apr 2026 12:00:00 -0700",
            },
            draft=True,
        )
        records = aggregate_people(
            test_db, owner_keys=frozenset(), min_docs=1
        )
        bob = _record_by_name(records, "bob")
        assert [d.title for d in bob.docs] == ["Real"]

    def test_docs_sorted_by_date_desc_then_title(
        self, test_db: psycopg.Connection[Any]
    ) -> None:
        store = DirectoryStore(test_db)
        store.upsert_pair(
            display_name="Bob",
            email="bob@example.com",
            source="people_yml",
        )
        _seed_doc(
            test_db,
            source_kind="gmail",
            external_id="m-old",
            title="Older",
            metadata={
                "from": "Bob <bob@example.com>",
                "to": "pat@example.com",
                "thread_id": "t-old",
                "date": "Wed, 15 Apr 2026 12:00:00 -0700",
            },
        )
        _seed_doc(
            test_db,
            source_kind="gmail",
            external_id="m-new",
            title="Newer",
            metadata={
                "from": "Bob <bob@example.com>",
                "to": "pat@example.com",
                "thread_id": "t-new",
                "date": "Thu, 16 Apr 2026 12:00:00 -0700",
            },
        )
        # A doc with an unparseable date sorts last regardless of title.
        _seed_doc(
            test_db,
            source_kind="krisp",
            external_id="k-undated",
            title="Aaaa undated",
            metadata={
                "_participant_keys": ["bob@example.com"],
                "date": "not-a-date",
            },
        )
        records = aggregate_people(
            test_db, owner_keys=frozenset(), min_docs=1
        )
        bob = _record_by_name(records, "bob")
        titles = [d.title for d in bob.docs]
        assert titles == ["Newer", "Older", "Aaaa undated"]

    def test_unresolvable_keys_drop_silently(
        self, test_db: psycopg.Connection[Any]
    ) -> None:
        # A Gmail doc with an unknown sender (no directory_entries entry)
        # contributes to nobody's roster.
        _seed_doc(
            test_db,
            source_kind="gmail",
            external_id="anon",
            title="Anon",
            metadata={
                "from": "stranger@example.com",
                "to": "pat@example.com",
                "thread_id": "t-anon",
                "date": "Wed, 15 Apr 2026 12:00:00 -0700",
            },
        )
        records = aggregate_people(
            test_db, owner_keys=frozenset(), min_docs=1
        )
        assert records == []

    def test_primary_email_picks_highest_count_when_no_people_yml(
        self, test_db: psycopg.Connection[Any]
    ) -> None:
        store = DirectoryStore(test_db)
        # Bob has two emails; the higher-occurrence one wins primary.
        store.upsert_pair(
            display_name="Bob", email="bob@a.com", source="gmail"
        )
        store.upsert_pair(
            display_name="Bob", email="bob@b.com", source="gmail"
        )
        store.upsert_pair(
            display_name="Bob", email="bob@b.com", source="gmail"
        )
        # Three docs to pass the default threshold.
        for i in range(3):
            _seed_doc(
                test_db,
                source_kind="gmail",
                external_id=f"m-{i}",
                title=f"Doc {i}",
                metadata={
                    "from": "Bob <bob@b.com>",
                    "to": "pat@example.com",
                    "thread_id": f"t-{i}",
                    "date": "Wed, 15 Apr 2026 12:00:00 -0700",
                },
            )
        records = aggregate_people(
            test_db, owner_keys=frozenset(), min_docs=3
        )
        bob = _record_by_name(records, "bob")
        assert bob.primary_email == "bob@b.com"
        assert bob.all_emails == ["bob@a.com", "bob@b.com"]

    def test_doc_without_vault_path_yields_none_target(
        self, test_db: psycopg.Connection[Any]
    ) -> None:
        store = DirectoryStore(test_db)
        store.upsert_pair(
            display_name="Bob",
            email="bob@example.com",
            source="people_yml",
        )
        _seed_doc(
            test_db,
            source_kind="gmail",
            external_id="m1",
            title="No mirror yet",
            metadata={
                "from": "Bob <bob@example.com>",
                "to": "pat@example.com",
                "thread_id": "t-1",
                "date": "Wed, 15 Apr 2026 12:00:00 -0700",
            },
            vault_path=None,
        )
        bob = _record_by_name(
            aggregate_people(test_db, owner_keys=frozenset(), min_docs=1),
            "bob",
        )
        assert bob.docs[0].vault_target is None

    def test_gmail_sent_at_takes_precedence_over_metadata_date(
        self, test_db: psycopg.Connection[Any]
    ) -> None:
        # Validate the sent_at branch of _doc_date by writing the typed
        # column directly and stamping a contradictory metadata.date.
        store = DirectoryStore(test_db)
        store.upsert_pair(
            display_name="Bob",
            email="bob@example.com",
            source="people_yml",
        )
        doc_id = _seed_doc(
            test_db,
            source_kind="gmail",
            external_id="sent-at-precedence",
            title="Headers",
            metadata={
                "from": "Bob <bob@example.com>",
                "to": "pat@example.com",
                "thread_id": "t-sent",
                "date": "Wed, 15 Apr 2026 12:00:00 -0700",
            },
        )
        test_db.execute(
            "UPDATE documents SET sent_at = %s WHERE id = %s",
            ("2030-01-02 03:04:05+00", doc_id),
        )
        bob = _record_by_name(
            aggregate_people(test_db, owner_keys=frozenset(), min_docs=1),
            "bob",
        )
        assert bob.docs[0].date is not None
        assert bob.docs[0].date.year == 2030

    def test_non_string_date_metadata_yields_none_date(
        self, test_db: psycopg.Connection[Any]
    ) -> None:
        # JSONB date stored as a number / null still passes through without
        # crashing — we just record date=None.
        store = DirectoryStore(test_db)
        store.upsert_pair(
            display_name="Bob",
            email="bob@example.com",
            source="people_yml",
        )
        _seed_doc(
            test_db,
            source_kind="krisp",
            external_id="numeric-date",
            title="Numeric date",
            metadata={
                "_participant_keys": ["bob@example.com"],
                "date": 12345,
            },
        )
        bob = _record_by_name(
            aggregate_people(test_db, owner_keys=frozenset(), min_docs=1),
            "bob",
        )
        assert bob.docs[0].date is None

    def test_unparseable_date_yields_none_date(
        self, test_db: psycopg.Connection[Any]
    ) -> None:
        # Empty string and unparseable RFC-5322 both fall through to
        # ``date=None``. Mix them with one parseable doc to confirm the
        # null-date sort ordering.
        store = DirectoryStore(test_db)
        store.upsert_pair(
            display_name="Bob",
            email="bob@example.com",
            source="people_yml",
        )
        _seed_doc(
            test_db,
            source_kind="gmail",
            external_id="empty-date",
            title="Z empty",
            metadata={
                "from": "Bob <bob@example.com>",
                "to": "pat@example.com",
                "thread_id": "t-empty",
                "date": "   ",
            },
        )
        _seed_doc(
            test_db,
            source_kind="gmail",
            external_id="bad-date",
            title="A bad",
            metadata={
                "from": "Bob <bob@example.com>",
                "to": "pat@example.com",
                "thread_id": "t-bad",
                "date": "Bogus 99 Notamonth -1 12:00:00 -0700",
            },
        )
        bob = _record_by_name(
            aggregate_people(test_db, owner_keys=frozenset(), min_docs=1),
            "bob",
        )
        # Both docs have date=None; tie-broken by title ascending.
        assert all(d.date is None for d in bob.docs)
        assert [d.title for d in bob.docs] == ["A bad", "Z empty"]

    def test_emails_only_owner_match_skips_person(
        self, test_db: psycopg.Connection[Any]
    ) -> None:
        # If every email a person owns is in owner_keys (and so is the
        # display_name), strict owner exclusion drops them.
        store = DirectoryStore(test_db)
        store.upsert_pair(
            display_name="Alias Account",
            email="alias@example.com",
            source="gmail",
        )
        # Even with docs above threshold the person must drop.
        for i in range(5):
            _seed_doc(
                test_db,
                source_kind="gmail",
                external_id=f"alias-{i}",
                title=f"x {i}",
                metadata={
                    "from": "Alias <alias@example.com>",
                    "to": "pat@example.com",
                    "thread_id": f"t-alias-{i}",
                    "date": "Wed, 15 Apr 2026 12:00:00 -0700",
                },
            )
        records = aggregate_people(
            test_db,
            owner_keys=frozenset({"alias@example.com"}),
            min_docs=1,
        )
        assert "alias account" not in [r.display_name for r in records]


# ==========================================================================
# Phase B — page rendering (B.1 / B.2)
# ==========================================================================
#
# These are pure-function tests on hand-built PersonRecord values — they
# do NOT touch the DB. The DB → render round-trip lives further down.


def _make_person(
    *,
    slug: str = "person-person-luke",
    display_name: str = "person-person-luke",
    primary_email: str = "person-luke@example.com",
    all_emails: list[str] | None = None,
    docs: list[DocRef] | None = None,
    in_people_yml: bool = True,
) -> PersonRecord:
    """Tiny PersonRecord factory for the Phase B render tests."""
    return PersonRecord(
        slug=slug,
        display_name=display_name,
        primary_email=primary_email,
        all_emails=all_emails or [primary_email],
        docs=docs or [],
        in_people_yml=in_people_yml,
    )


class TestRenderPersonMdFrontmatter:
    """Frontmatter shape — keys, types, and ordering match the spec."""

    def test_frontmatter_round_trips(self) -> None:
        rec = _make_person(
            all_emails=["person-luke@example.com", "person-luke@home.com"],
            docs=[
                DocRef(
                    document_id="d1",
                    title="Sync",
                    source_kind="krisp",
                    date=__import__("datetime").datetime(2026, 5, 1),
                    vault_target="_ingested/krisp/2026-05-01-abc-sync",
                ),
            ],
        )
        rendered = render_person_md(rec)
        fm, _body = parse_frontmatter(rendered)
        assert fm["title"] == "Person-Person-Luke"
        assert fm["slug"] == "person-person-luke"
        assert fm["kind"] == "people"
        assert fm["emails"] == ["person-luke@example.com", "person-luke@home.com"]
        assert fm["doc_count"] == 1
        assert fm["in_people_yml"] is True

    def test_in_people_yml_false_for_threshold_persons(self) -> None:
        rec = _make_person(in_people_yml=False)
        fm, _ = parse_frontmatter(render_person_md(rec))
        assert fm["in_people_yml"] is False

    def test_doc_count_matches_docs_length(self) -> None:
        rec = _make_person(
            docs=[
                DocRef(f"id-{i}", f"Title {i}", "gmail", None, None)
                for i in range(7)
            ],
        )
        fm, _ = parse_frontmatter(render_person_md(rec))
        assert fm["doc_count"] == 7


class TestRenderPersonMdBody:
    """Body layout — H1, primary email, doc lines, empty-state placeholder."""

    def test_body_contains_h1_humanized_name(self) -> None:
        rec = _make_person(display_name="person-person-luke")
        rendered = render_person_md(rec)
        assert "# Person-Person-Luke" in rendered

    def test_primary_email_rendered_as_mailto(self) -> None:
        rendered = render_person_md(_make_person())
        expected = (
            "**Primary email:** "
            "[person-luke@example.com](mailto:person-luke@example.com)"
        )
        assert expected in rendered

    def test_other_emails_listed_when_multiple(self) -> None:
        rec = _make_person(
            all_emails=["person-luke@example.com", "person-luke@home.com", "person-luke@work.com"],
        )
        rendered = render_person_md(rec)
        assert "**Other emails:** person-luke@home.com, person-luke@work.com" in rendered

    def test_other_emails_omitted_when_only_primary(self) -> None:
        rec = _make_person(all_emails=["person-luke@example.com"])
        rendered = render_person_md(rec)
        assert "**Other emails:**" not in rendered

    def test_documents_section_uses_h2_with_count(self) -> None:
        rec = _make_person(
            docs=[
                DocRef(f"d{i}", f"T{i}", "gmail", None, None) for i in range(3)
            ],
        )
        assert "## Documents (3)" in render_person_md(rec)

    def test_doc_line_emits_h3_with_date_and_link(self) -> None:
        from datetime import datetime
        rec = _make_person(
            docs=[
                DocRef(
                    document_id="d1",
                    title="AI CoS Jam Session",
                    source_kind="krisp",
                    date=datetime(2026, 5, 6),
                    vault_target="_ingested/krisp/2026-05-06-abc-ai-cos-jam-session",
                )
            ],
        )
        rendered = render_person_md(rec)
        assert (
            "### 2026-05-06 · [[_ingested/krisp/2026-05-06-abc-ai-cos-jam-session"
            "|AI CoS Jam Session]] (krisp)"
        ) in rendered

    def test_doc_with_brackets_in_title_sanitized_to_parens(self) -> None:
        # Quartz's wiki-link parser rejects ``[``/``]`` in the alias slot.
        from datetime import datetime
        rec = _make_person(
            docs=[
                DocRef(
                    document_id="d1",
                    title="Re: [External] Q1 review",
                    source_kind="gmail",
                    date=datetime(2026, 4, 15),
                    vault_target="_ingested/gmail/2026-04-15-x-re-q1",
                )
            ],
        )
        rendered = render_person_md(rec)
        # Brackets in the alias slot replaced by parens.
        assert "Re: (External) Q1 review" in rendered
        assert "[External]" not in rendered

    def test_doc_without_vault_target_renders_plaintext(self) -> None:
        from datetime import datetime
        rec = _make_person(
            docs=[
                DocRef(
                    document_id="d1",
                    title="Plain note",
                    source_kind="manual",
                    date=datetime(2026, 4, 1),
                    vault_target=None,
                )
            ],
        )
        rendered = render_person_md(rec)
        # No wiki-link wrapper, just the title.
        assert "### 2026-04-01 · Plain note (manual)" in rendered
        assert "[[Plain note" not in rendered

    def test_doc_without_date_renders_undated(self) -> None:
        rec = _make_person(
            docs=[
                DocRef("d1", "Stale", "krisp", None, None),
            ],
        )
        rendered = render_person_md(rec)
        assert "### undated · Stale (krisp)" in rendered

    def test_zero_docs_renders_no_documents_yet_placeholder(self) -> None:
        # Curated person with no matched documents — the page must still
        # render (Phase A allows them, Phase B emits a placeholder).
        rec = _make_person(docs=[])
        rendered = render_person_md(rec)
        assert "## Documents (0)" in rendered
        assert "*No documents yet.*" in rendered

    def test_render_is_idempotent(self) -> None:
        rec = _make_person(
            docs=[DocRef("d1", "T", "gmail", None, "_ingested/gmail/x")]
        )
        first = render_person_md(rec)
        second = render_person_md(rec)
        assert first == second


class TestRenderIndexMd:
    """Index page — alphabetical, curated badge, empty-state placeholder."""

    def test_empty_records_render_placeholder(self) -> None:
        rendered = render_index_md([])
        fm, body = parse_frontmatter(rendered)
        assert fm["kind"] == "people-index"
        assert "*No people yet" in body

    def test_records_alphabetized_by_display_name(self) -> None:
        recs = [
            _make_person(slug="zoe-zhang", display_name="zoe zhang", in_people_yml=False),
            _make_person(slug="alice-anderson", display_name="alice anderson", in_people_yml=False),
            _make_person(
                slug="person-person-marc",
                display_name="person-person-marc",
                in_people_yml=True,
            ),
        ]
        rendered = render_index_md(recs)
        # Locate each name's position; Alice → person-person-marc → Zoe (alpha by display_name).
        idx_alice = rendered.index("Alice Anderson")
        idx_marc = rendered.index("person-person-marc")
        idx_zoe = rendered.index("Zoe Zhang")
        assert idx_alice < idx_marc < idx_zoe

    def test_curated_rows_get_check_badge(self) -> None:
        recs = [
            _make_person(slug="curated", display_name="curated person", in_people_yml=True),
            _make_person(slug="threshold", display_name="threshold person", in_people_yml=False),
        ]
        rendered = render_index_md(recs)
        # Curated row carries the badge; threshold row does not.
        curated_line = next(
            ln for ln in rendered.splitlines() if "Curated Person" in ln
        )
        threshold_line = next(
            ln for ln in rendered.splitlines() if "Threshold Person" in ln
        )
        assert "✅" in curated_line
        assert "✅" not in threshold_line

    def test_each_row_carries_doc_count_and_email(self) -> None:
        recs = [
            _make_person(
                slug="person-person-luke",
                display_name="person-person-luke",
                primary_email="person-luke@example.com",
                docs=[
                    DocRef(f"d{i}", f"t{i}", "krisp", None, None)
                    for i in range(4)
                ],
            )
        ]
        rendered = render_index_md(recs)
        expected = (
            "[[people/person-person-luke|Person-Person-Luke]] — 4 docs"
            " · person-luke@example.com"
        )
        assert expected in rendered

    def test_index_render_is_idempotent(self) -> None:
        recs = [_make_person()]
        assert render_index_md(recs) == render_index_md(recs)


# ==========================================================================
# Phase B — emit_people_pages (B.3): DB → tmp_path round-trip
# ==========================================================================


def _seed_curated_with_docs(
    test_db: psycopg.Connection[Any],
    *,
    name: str,
    email: str,
    n_gmail_docs: int = 0,
    vault_path_template: str = "_ingested/gmail/2026-04-{i:02d}-abc-doc-{i}",
) -> None:
    """Seed one ``people_yml`` person + N Gmail docs they participate in."""
    DirectoryStore(test_db).upsert_pair(
        display_name=name, email=email, source="people_yml"
    )
    for i in range(n_gmail_docs):
        _seed_doc(
            test_db,
            source_kind="gmail",
            external_id=f"gmail-{name}-{i}",
            title=f"{name.title()} doc {i}",
            metadata={
                "from": f"{name} <{email}>",
                "to": "pat@example.com",
                "thread_id": f"t-{name}-{i}",
                "date": f"Wed, {15 + i:02d} Apr 2026 12:00:00 -0700",
            },
            vault_path=vault_path_template.format(i=i),
        )


class TestEmitPeoplePagesRoundTrip:
    """Aggregate → render → write — verify the disk artifact."""

    def test_writes_one_page_per_record_and_index(
        self, test_db: psycopg.Connection[Any], tmp_path: Path
    ) -> None:
        _seed_curated_with_docs(
            test_db, name="person-person-luke", email="person-luke@example.com",
            n_gmail_docs=2,
        )
        report = emit_people_pages(
            test_db,
            vault_path=tmp_path,
            owner_keys=frozenset(),
            min_docs=1,
        )
        assert isinstance(report, EmitReport)
        assert report.pages_written == 1
        assert report.pages_deleted == 0
        assert report.index_written is True

        people_dir = tmp_path / "people"
        assert (people_dir / "person-person-luke.md").is_file()
        assert (people_dir / "index.md").is_file()

        # Round-trip: parse the written page's frontmatter back.
        fm, _body = parse_frontmatter(
            (people_dir / "person-person-luke.md").read_text(encoding="utf-8")
        )
        assert fm["slug"] == "person-person-luke"
        assert fm["doc_count"] == 2
        assert fm["emails"] == ["person-luke@example.com"]
        assert fm["in_people_yml"] is True

    def test_creates_people_dir_on_first_run(
        self, test_db: psycopg.Connection[Any], tmp_path: Path
    ) -> None:
        # Vault directory exists but ``people/`` does not yet.
        assert not (tmp_path / "people").exists()
        _seed_curated_with_docs(
            test_db, name="bob smith", email="bob@example.com", n_gmail_docs=1,
        )
        emit_people_pages(
            test_db, vault_path=tmp_path,
            owner_keys=frozenset(), min_docs=1,
        )
        assert (tmp_path / "people").is_dir()

    def test_empty_directory_still_writes_index_placeholder(
        self, test_db: psycopg.Connection[Any], tmp_path: Path
    ) -> None:
        # No directory_entries rows → no per-person pages → index renders
        # the empty-state placeholder.
        report = emit_people_pages(
            test_db, vault_path=tmp_path,
            owner_keys=frozenset(), min_docs=3,
        )
        assert report.pages_written == 0
        assert report.pages_deleted == 0
        assert report.index_written is True
        index_text = (tmp_path / "people" / "index.md").read_text(encoding="utf-8")
        assert "*No people yet" in index_text


class TestEmitPeoplePagesIdempotence:
    """Re-running with no DB drift must not re-write existing files."""

    def test_second_run_writes_zero_pages(
        self, test_db: psycopg.Connection[Any], tmp_path: Path
    ) -> None:
        _seed_curated_with_docs(
            test_db, name="person-person-luke", email="person-luke@example.com",
            n_gmail_docs=2,
        )
        first = emit_people_pages(
            test_db, vault_path=tmp_path,
            owner_keys=frozenset(), min_docs=1,
        )
        assert first.pages_written == 1
        assert first.index_written is True

        second = emit_people_pages(
            test_db, vault_path=tmp_path,
            owner_keys=frozenset(), min_docs=1,
        )
        # Bytes already match disk → nothing rewritten on either side.
        assert second.pages_written == 0
        assert second.skipped_unchanged == 1
        assert second.pages_deleted == 0
        assert second.index_written is False

    def test_mtime_preserved_on_skip(
        self, test_db: psycopg.Connection[Any], tmp_path: Path
    ) -> None:
        # The "skip if byte-identical" gate must preserve mtime so the
        # Quartz watcher doesn't fire a needless rebuild on a re-run.
        _seed_curated_with_docs(
            test_db, name="ann arbor", email="ann@example.com", n_gmail_docs=1,
        )
        emit_people_pages(
            test_db, vault_path=tmp_path,
            owner_keys=frozenset(), min_docs=1,
        )
        page = tmp_path / "people" / "ann-arbor.md"
        index = tmp_path / "people" / "index.md"
        page_mtime_before = page.stat().st_mtime_ns
        index_mtime_before = index.stat().st_mtime_ns

        emit_people_pages(
            test_db, vault_path=tmp_path,
            owner_keys=frozenset(), min_docs=1,
        )
        assert page.stat().st_mtime_ns == page_mtime_before
        assert index.stat().st_mtime_ns == index_mtime_before


class TestEmitPeoplePagesCleanup:
    """A removed ``_people.yml`` entry → next emit deletes the dropped page."""

    def test_removed_curated_person_page_deleted_on_next_emit(
        self, test_db: psycopg.Connection[Any], tmp_path: Path
    ) -> None:
        # First refresh: person-person-luke + person-person-marc both curated, both above threshold.
        DirectoryStore(test_db).upsert_pair(
            display_name="person-person-luke", email="person-luke@example.com",
            source="people_yml",
        )
        DirectoryStore(test_db).upsert_pair(
            display_name="person-person-marc", email="person-marc@example.com",
            source="people_yml",
        )
        first = emit_people_pages(
            test_db, vault_path=tmp_path,
            owner_keys=frozenset(), min_docs=1,
        )
        assert first.pages_written == 2
        assert (tmp_path / "people" / "person-person-luke.md").is_file()
        assert (tmp_path / "people" / "person-person-marc.md").is_file()

        # Drop person-person-luke from the directory (simulate user removing him from
        # _people.yml + a re-refresh).
        test_db.execute(
            "DELETE FROM directory_entries WHERE display_name = %s",
            ("person-person-luke",),
        )

        second = emit_people_pages(
            test_db, vault_path=tmp_path,
            owner_keys=frozenset(), min_docs=1,
        )
        assert second.pages_deleted == 1
        assert tmp_path / "people" / "person-person-luke.md" in second.deleted_paths
        assert not (tmp_path / "people" / "person-person-luke.md").exists()
        # person-person-marc's page survives.
        assert (tmp_path / "people" / "person-person-marc.md").is_file()
        # The index gets re-written because person-person-marc's row stays
        # but person-person-luke's row is gone — the rendered list changed.
        assert second.index_written is True

    def test_index_md_never_treated_as_a_person_page(
        self, test_db: psycopg.Connection[Any], tmp_path: Path
    ) -> None:
        # Even with no curated people, the cleanup pass must NOT delete
        # ``index.md`` (it's separately managed).
        emit_people_pages(
            test_db, vault_path=tmp_path,
            owner_keys=frozenset(), min_docs=3,
        )
        # Re-run — index.md must persist across the cleanup pass.
        emit_people_pages(
            test_db, vault_path=tmp_path,
            owner_keys=frozenset(), min_docs=3,
        )
        assert (tmp_path / "people" / "index.md").is_file()

    def test_unrelated_files_in_people_dir_left_untouched(
        self, test_db: psycopg.Connection[Any], tmp_path: Path
    ) -> None:
        # A user-authored attachment / non-.md file in people/ survives
        # the cleanup pass unscathed.
        people_dir = tmp_path / "people"
        people_dir.mkdir()
        (people_dir / "notes.txt").write_text("hand-written")
        (people_dir / "subdir").mkdir()  # ignored — not a regular file

        emit_people_pages(
            test_db, vault_path=tmp_path,
            owner_keys=frozenset(), min_docs=3,
        )
        assert (people_dir / "notes.txt").read_text() == "hand-written"
        assert (people_dir / "subdir").is_dir()


# ==========================================================================
# Phase B — defensive coverage of OSError catches in emit_people_pages
# ==========================================================================
#
# Each branch wraps a filesystem call that can fail (read for compare,
# unlink during cleanup, write for the per-person page, write for the
# index). These are belt-and-suspenders for the case where the user
# deleted permissions on a single file mid-run; we verify the logger
# warns and the run keeps going.


class TestEmitPeoplePagesDefensiveErrorHandling:
    """OSError on read/unlink/write must be logged + the run continues."""

    def test_unreadable_existing_page_still_writes(
        self,
        test_db: psycopg.Connection[Any],
        tmp_path: Path,
        mocker: "Any",  # noqa: F821 — pytest-mock injects MockerFixture at runtime
    ) -> None:
        # Pre-populate ``ann-arbor.md`` with garbage that will be overwritten.
        # Mock ``Path.read_text`` to raise OSError on the read-for-compare,
        # so we exercise the ``except OSError`` branch in _write_if_changed.
        _seed_curated_with_docs(
            test_db, name="ann arbor", email="ann@example.com", n_gmail_docs=1,
        )
        page_dir = tmp_path / "people"
        page_dir.mkdir()
        target = page_dir / "ann-arbor.md"
        target.write_text("stale", encoding="utf-8")

        original_read = Path.read_text

        def fake_read_text(self: Path, *a: Any, **kw: Any) -> str:
            if self == target:
                raise PermissionError("synthetic")
            return original_read(self, *a, **kw)

        mocker.patch.object(Path, "read_text", fake_read_text)

        report = emit_people_pages(
            test_db, vault_path=tmp_path,
            owner_keys=frozenset(), min_docs=1,
        )
        assert report.pages_written == 1  # write went through despite read failure

    def test_unlinkable_stale_page_logged_and_skipped(
        self,
        test_db: psycopg.Connection[Any],
        tmp_path: Path,
        caplog: pytest.LogCaptureFixture,
        mocker: "Any",  # noqa: F821
    ) -> None:
        # Drop a stale page on disk and force ``unlink`` to raise.
        people_dir = tmp_path / "people"
        people_dir.mkdir()
        stale = people_dir / "ghost.md"
        stale.write_text("stale", encoding="utf-8")
        original_unlink = Path.unlink

        def fake_unlink(self: Path, *a: Any, **kw: Any) -> None:
            if self == stale:
                raise PermissionError("synthetic unlink")
            original_unlink(self, *a, **kw)

        mocker.patch.object(Path, "unlink", fake_unlink)

        import logging
        with caplog.at_level(logging.WARNING):
            report = emit_people_pages(
                test_db, vault_path=tmp_path,
                owner_keys=frozenset(), min_docs=3,
            )
        assert report.pages_deleted == 0  # unlink failed → not counted
        assert any(
            "could not delete stale page" in r.message for r in caplog.records
        )

    def test_write_failure_per_person_logs_and_continues(
        self,
        test_db: psycopg.Connection[Any],
        tmp_path: Path,
        caplog: pytest.LogCaptureFixture,
        mocker: "Any",  # noqa: F821
    ) -> None:
        # Force the per-person write path to raise OSError; the index write
        # should still complete.
        _seed_curated_with_docs(
            test_db, name="bad write", email="bw@example.com", n_gmail_docs=1,
        )
        # Mock atomic_write_text in the brain.people module so only the
        # first call (per-person page) raises; the second (index) succeeds.
        target = tmp_path / "people" / "bad-write.md"
        original_atomic = __import__(
            "brain.people", fromlist=["atomic_write_text"]
        ).atomic_write_text

        def fake_atomic(path: Path, text: str) -> None:
            if path == target:
                raise PermissionError("synthetic write")
            original_atomic(path, text)

        mocker.patch("brain.people.atomic_write_text", fake_atomic)

        import logging
        with caplog.at_level(logging.WARNING):
            report = emit_people_pages(
                test_db, vault_path=tmp_path,
                owner_keys=frozenset(), min_docs=1,
            )
        assert report.pages_written == 0
        assert any(
            "could not write" in r.message and "bad-write" in r.message
            for r in caplog.records
        )
        # Index still wrote successfully.
        assert (tmp_path / "people" / "index.md").is_file()

    def test_index_write_failure_logged_and_report_false(
        self,
        test_db: psycopg.Connection[Any],
        tmp_path: Path,
        caplog: pytest.LogCaptureFixture,
        mocker: "Any",  # noqa: F821
    ) -> None:
        # Force only the index write to raise.
        index_target = tmp_path / "people" / "index.md"
        original_atomic = __import__(
            "brain.people", fromlist=["atomic_write_text"]
        ).atomic_write_text

        def fake_atomic(path: Path, text: str) -> None:
            if path == index_target:
                raise PermissionError("synthetic index write")
            original_atomic(path, text)

        mocker.patch("brain.people.atomic_write_text", fake_atomic)

        import logging
        with caplog.at_level(logging.WARNING):
            report = emit_people_pages(
                test_db, vault_path=tmp_path,
                owner_keys=frozenset(), min_docs=3,
            )
        assert report.index_written is False
        assert any(
            "could not write index" in r.message for r in caplog.records
        )
