"""Tests for ``brain.wiki.build_people.aggregate_people`` (Phase A).

Real-Postgres integration tests — seed ``sources``, ``documents``, and
``directory_entries`` directly so we exercise the production SELECT and the
real participant-extraction helpers without dragging the chunker / embedder
into the loop.
"""
import hashlib
import json
import uuid
from typing import Any

import psycopg
import pytest

from brain.vault.derived_links.directory import DirectoryStore
from brain.wiki.build_people import PersonRecord, aggregate_people

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
        assert alice.docs[0].vault_slug == (
            "2026-04-15-deadbeef-lunch-plans"
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

        person-luke = _record_by_name(records, "person-person-luke")
        assert person-luke.in_people_yml is True
        assert person-luke.primary_email == "person-luke@example.com"
        # The doc is listed once even though both keys resolved to person-person-luke.
        assert len(person-luke.docs) == 1
        assert person-luke.docs[0].source_kind == "krisp"

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

        person-luke = _record_by_name(records, "person-person-luke")
        assert len(person-luke.docs) == 1


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
                "to": "ali@example.com",
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
                    "to": "ali@example.com",
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

        person-luke = _record_by_name(records, "person-person-luke")
        assert person-luke.in_people_yml is True
        assert len(person-luke.docs) == 1

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
                    "to": "ali@example.com",
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
    """Two distinct display_names that slugify to the same slug → second
    gets the ``-2`` suffix; alpha order on display_name decides who wins
    the bare slug."""

    def test_collision_resolves_with_numeric_suffix(
        self, test_db: psycopg.Connection[Any]
    ) -> None:
        store = DirectoryStore(test_db)
        # Two display_names that survive ``normalize_participant`` distinct
        # but collapse to the same slug after ``slugify`` — internal
        # punctuation (underscore / hyphen) collapses to ``-`` in slugify.
        store.upsert_pair(
            display_name="john doe", email="j@example.com", source="people_yml"
        )
        store.upsert_pair(
            display_name="john_doe", email="jd@example.com", source="people_yml"
        )

        records = aggregate_people(
            test_db, owner_keys=frozenset(), min_docs=3
        )
        names = [r.display_name for r in records]
        assert "john doe" in names
        assert "john_doe" in names

        first = _record_by_name(records, "john doe")
        second = _record_by_name(records, "john_doe")
        # ASCII space (32) < underscore (95), so "john doe" wins the alpha tiebreak.
        assert first.slug == "john-doe"
        assert second.slug == "john-doe-2"


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
            display_name="Ali Sarkis",
            email="ali@example.com",
            source="people_yml",
        )
        store.upsert_pair(
            display_name="person-person-luke",
            email="person-luke@example.com",
            source="people_yml",
        )

        # Gmail thread Ali↔person-person-luke. Owner_keys strips Ali → only person-person-luke gets a
        # record, with this doc on his roster.
        _seed_doc(
            test_db,
            source_kind="gmail",
            external_id="gmail-ali-person-luke",
            title="Coffee?",
            metadata={
                "from": "Ali Sarkis <ali@example.com>",
                "to": "person-person-luke <person-luke@example.com>",
                "thread_id": "t-coffee",
                "date": "Wed, 15 Apr 2026 12:00:00 -0700",
            },
        )

        # Krisp call where Ali appears in _participant_keys — same exclusion.
        _seed_doc(
            test_db,
            source_kind="krisp",
            external_id="krisp-ali-person-luke",
            title="1:1",
            metadata={
                "_participant_keys": [
                    "ali@example.com",
                    "ali sarkis",
                    "person-luke@example.com",
                    "person-person-luke",
                ],
                "date": "2026-04-29",
            },
        )

        owner_keys = frozenset({"ali@example.com", "ali sarkis"})
        records = aggregate_people(
            test_db, owner_keys=owner_keys, min_docs=1
        )

        names = [r.display_name for r in records]
        assert names == ["person-person-luke"]

        person-luke = _record_by_name(records, "person-person-luke")
        # Two docs (gmail + krisp) — both list person-person-luke once (owner stripped).
        assert len(person-luke.docs) == 2
        sources = sorted(d.source_kind for d in person-luke.docs)
        assert sources == ["gmail", "krisp"]

    def test_owner_strip_drops_doc_with_only_owner_participants(
        self, test_db: psycopg.Connection[Any]
    ) -> None:
        # A doc whose only resolvable participants are owner keys contributes
        # zero rosters — nobody (not even owner-self) gets it.
        store = DirectoryStore(test_db)
        store.upsert_pair(
            display_name="Ali Sarkis",
            email="ali@example.com",
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
                "from": "Ali <ali@example.com>",
                "to": "ali@example.com",
                "thread_id": "t-self",
                "date": "Wed, 15 Apr 2026 12:00:00 -0700",
            },
        )
        records = aggregate_people(
            test_db,
            owner_keys=frozenset({"ali@example.com", "ali sarkis"}),
            min_docs=1,
        )
        # person-person-luke is curated so he still emits, but with zero docs — the
        # owner-only doc contributed nothing to his roster.
        names = [r.display_name for r in records]
        assert names == ["person-person-luke"]
        person-luke = _record_by_name(records, "person-person-luke")
        assert person-luke.docs == []


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
                "to": "ali@example.com",
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
                "to": "ali@example.com",
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
                "to": "ali@example.com",
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
                "to": "ali@example.com",
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
                "to": "ali@example.com",
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
                    "to": "ali@example.com",
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

    def test_doc_without_vault_path_yields_none_slug(
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
                "to": "ali@example.com",
                "thread_id": "t-1",
                "date": "Wed, 15 Apr 2026 12:00:00 -0700",
            },
            vault_path=None,
        )
        bob = _record_by_name(
            aggregate_people(test_db, owner_keys=frozenset(), min_docs=1),
            "bob",
        )
        assert bob.docs[0].vault_slug is None

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
                "to": "ali@example.com",
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
                "to": "ali@example.com",
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
                "to": "ali@example.com",
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
                    "to": "ali@example.com",
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
