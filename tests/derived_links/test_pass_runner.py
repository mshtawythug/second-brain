"""Tests for brain.vault.derived_links.pass_runner.rebuild_derived_for.

Real-DB integration tests: seed ``sources`` + ``documents`` rows directly
(bypassing the chunker/embedder so we don't need a fake embedder fixture
for these), then call ``rebuild_derived_for`` and assert the resulting
``derived_links`` rows.
"""
import hashlib
import json
import uuid
from typing import Any

import psycopg
import pytest

from brain.vault.derived_links.directory import DirectoryStore
from brain.vault.derived_links.pass_runner import rebuild_derived_for

# --------------------------------------------------------------------------
# Helpers — direct SQL seeding so each test can build the exact corpus shape
# the rule under test needs without dragging in the chunker / embedder.
# --------------------------------------------------------------------------


def _seed_doc(
    conn: psycopg.Connection,
    *,
    source_kind: str,
    external_id: str,
    metadata: dict[str, Any],
    content: str,
    title: str | None = None,
    content_type: str = "transcript",
) -> str:
    """Insert a ``sources`` + ``documents`` pair, return the new document id.

    Mirrors the live ingest shape closely enough for ``rebuild_derived_for``
    to read the same SELECT it would in production. Each call uses unique
    content (suffixed with a random uuid) so the global ``content_hash``
    UNIQUE constraint never collides between test docs.
    """
    src_row = conn.execute(
        "INSERT INTO sources (kind, external_id, metadata) "
        "VALUES (%s, %s, %s::jsonb) RETURNING id",
        (source_kind, external_id, json.dumps({})),
    ).fetchone()
    assert src_row is not None
    source_id = src_row[0]

    # Salt the body so two seeded docs never share content_hash.
    salted = f"{content}\n<!-- {uuid.uuid4()} -->"
    content_hash = hashlib.sha256(salted.encode("utf-8")).hexdigest()

    doc_row = conn.execute(
        """
        INSERT INTO documents
            (source_id, title, content, content_hash, content_type,
             source_path, tags, metadata)
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s::jsonb)
        RETURNING id::text
        """,
        (
            source_id,
            title or f"{source_kind} {external_id}",
            salted,
            content_hash,
            content_type,
            None,
            [],
            json.dumps(metadata),
        ),
    ).fetchone()
    assert doc_row is not None
    return str(doc_row[0])


def _derived_rows(conn: psycopg.Connection) -> list[tuple[Any, ...]]:
    """All ``derived_links`` rows ordered for stable assertions."""
    return conn.execute(
        "SELECT src_document_id::text, dst_document_id::text, rule, "
        "weight, evidence "
        "FROM derived_links ORDER BY src_document_id, dst_document_id, rule"
    ).fetchall()


@pytest.fixture
def directory(test_db: psycopg.Connection) -> DirectoryStore:
    """Bare DirectoryStore — most tests don't pre-populate it."""
    return DirectoryStore(test_db)


# --------------------------------------------------------------------------
# Test cases — one per row in the plan's required-test list.
# --------------------------------------------------------------------------


class TestSharedThread:
    """R1 — two Gmail docs with the same thread_id."""

    def test_two_gmail_same_thread_yields_R1(
        self, test_db: psycopg.Connection, directory: DirectoryStore
    ) -> None:
        a_id = _seed_doc(
            test_db,
            source_kind="gmail",
            external_id="msg-a",
            metadata={
                "from": "person-x <person-a@example.com>",
                "to": "Ali <ali@example.com>",
                "thread_id": "t-shared",
                "date": "Wed, 15 Apr 2026 12:00:00 -0700",
            },
            content="hello there",
        )
        b_id = _seed_doc(
            test_db,
            source_kind="gmail",
            external_id="msg-b",
            metadata={
                "from": "Ali <ali@example.com>",
                "to": "person-x <person-a@example.com>",
                "thread_id": "t-shared",
                "date": "Wed, 15 Apr 2026 12:30:00 -0700",
            },
            content="hi back",
        )

        inserted = rebuild_derived_for(
            test_db, {a_id, b_id}, directory=directory
        )

        rows = _derived_rows(test_db)
        # R1 (shared_thread, weight 1.0) AND R3 (same_day_participant, 0.7)
        # both fire because the docs share emails and the same date. The
        # test asserts R1 specifically; R3 coexistence is covered by the
        # rule_priority test below.
        r1_rows = [r for r in rows if r[2] == "shared_thread"]
        assert len(r1_rows) == 1
        src, dst, rule, weight, evidence = r1_rows[0]
        assert {src, dst} == {a_id, b_id}
        assert src < dst  # canonical ordering
        assert rule == "shared_thread"
        assert weight == pytest.approx(1.0)
        assert evidence["thread_id"] == "t-shared"
        # ``inserted`` is the total across all rules — R1 + R3 here.
        assert inserted == len(rows)


class TestSameDayParticipant:
    """R3 — Krisp + Gmail with overlapping participants on the same date."""

    def test_krisp_gmail_shared_participant_same_day_yields_R3(
        self, test_db: psycopg.Connection, directory: DirectoryStore
    ) -> None:
        krisp_id = _seed_doc(
            test_db,
            source_kind="krisp",
            external_id="krisp-1",
            metadata={
                "_participant_keys": sorted(["person-a@example.com", "ali sarkis"]),
                "date": "2026-04-15",
            },
            content="**person-x | 0:01**\nhello",
        )
        gmail_id = _seed_doc(
            test_db,
            source_kind="gmail",
            external_id="gmail-1",
            metadata={
                "from": "person-x <person-a@example.com>",
                "to": "Ali <ali@example.com>",
                "thread_id": "t-1",
                "date": "Wed, 15 Apr 2026 09:00:00 -0700",
            },
            content="email body",
        )

        rebuild_derived_for(test_db, {krisp_id, gmail_id}, directory=directory)

        rows = _derived_rows(test_db)
        rules = [r[2] for r in rows]
        assert "same_day_participant" in rules
        assert "shared_participant" not in rules  # R3 supersedes R2

        r3_row = next(r for r in rows if r[2] == "same_day_participant")
        assert r3_row[3] == pytest.approx(0.7)
        assert r3_row[4]["participant"] == "person-a@example.com"

    def test_krisp_gmail_shared_participant_distant_dates_yields_R2(
        self, test_db: psycopg.Connection, directory: DirectoryStore
    ) -> None:
        krisp_id = _seed_doc(
            test_db,
            source_kind="krisp",
            external_id="krisp-2",
            metadata={
                "_participant_keys": ["person-a@example.com"],
                "date": "2026-04-15",
            },
            content="**person-a@example.com | 0:01**\nhello",
        )
        gmail_id = _seed_doc(
            test_db,
            source_kind="gmail",
            external_id="gmail-2",
            metadata={
                "from": "person-x <person-a@example.com>",
                "to": "Ali <ali@example.com>",
                "thread_id": "t-2",
                "date": "Mon, 20 Apr 2026 09:00:00 -0700",  # 5 days later
            },
            content="email body",
        )

        rebuild_derived_for(test_db, {krisp_id, gmail_id}, directory=directory)

        rows = _derived_rows(test_db)
        assert len(rows) == 1
        assert rows[0][2] == "shared_participant"
        assert rows[0][3] == pytest.approx(0.4)


class TestNoIntersection:
    """Disjoint participants — zero rows."""

    def test_no_shared_participants_yields_zero(
        self, test_db: psycopg.Connection, directory: DirectoryStore
    ) -> None:
        a_id = _seed_doc(
            test_db,
            source_kind="gmail",
            external_id="msg-x",
            metadata={
                "from": "Alice <alice@example.com>",
                "to": "Bob <bob@example.com>",
                "thread_id": "t-x",
                "date": "Wed, 15 Apr 2026 12:00:00 -0700",
            },
            content="x",
        )
        b_id = _seed_doc(
            test_db,
            source_kind="gmail",
            external_id="msg-y",
            metadata={
                "from": "Carol <carol@example.com>",
                "to": "Dave <dave@example.com>",
                "thread_id": "t-y",
                "date": "Wed, 15 Apr 2026 12:00:00 -0700",
            },
            content="y",
        )

        result = rebuild_derived_for(
            test_db, {a_id, b_id}, directory=directory
        )

        assert result == 0
        assert _derived_rows(test_db) == []


class TestDirectoryBridge:
    """Directory's name → email resolution closes the cross-source gap."""

    def test_directory_bridges_name_to_email(
        self, test_db: psycopg.Connection, directory: DirectoryStore
    ) -> None:
        # Bridge "Ali Sarkis" → "redacted@example.com" via people_yml so it
        # wins absolutely; relies on B.1's resolution semantics.
        directory.upsert_pair(
            display_name="Ali Sarkis",
            email="redacted@example.com",
            source="people_yml",
        )

        krisp_id = _seed_doc(
            test_db,
            source_kind="krisp",
            external_id="krisp-bridge",
            metadata={
                # B.3 stores a sorted list; "ali sarkis" is the only key.
                "_participant_keys": ["ali sarkis"],
                "date": "2026-04-15",
            },
            content="**Ali Sarkis | 0:01**\nthought leadership",
        )
        gmail_id = _seed_doc(
            test_db,
            source_kind="gmail",
            external_id="gmail-bridge",
            metadata={
                "from": "Ali Sarkis <redacted@example.com>",
                "to": "person-x <person-a@example.com>",
                "thread_id": "t-bridge",
                "date": "Wed, 15 Apr 2026 12:00:00 -0700",
            },
            content="bridge",
        )

        rebuild_derived_for(test_db, {krisp_id, gmail_id}, directory=directory)

        rows = _derived_rows(test_db)
        # R3 fires (same day + bridged participant); R1 doesn't (only one
        # gmail doc); R2 is suppressed by R3.
        assert [r[2] for r in rows] == ["same_day_participant"]
        assert rows[0][4]["participant"] == "redacted@example.com"


class TestTouchedScope:
    """Rebuild only writes edges touching ``doc_ids``."""

    def test_rebuild_only_touches_doc_ids_in_set(
        self, test_db: psycopg.Connection, directory: DirectoryStore
    ) -> None:
        # Three Gmail docs in the same thread — without the touched-set
        # scope, every pair would land (A↔B, A↔C, B↔C). Calling with only
        # {A} should drop B↔C.
        a_id = _seed_doc(
            test_db,
            source_kind="gmail",
            external_id="g-a",
            metadata={
                "from": "person-x <person-a@example.com>",
                "to": "Ali <ali@example.com>",
                "thread_id": "t-tri",
                "date": "Wed, 15 Apr 2026 12:00:00 -0700",
            },
            content="A body",
        )
        b_id = _seed_doc(
            test_db,
            source_kind="gmail",
            external_id="g-b",
            metadata={
                "from": "Ali <ali@example.com>",
                "to": "person-x <person-a@example.com>",
                "thread_id": "t-tri",
                "date": "Wed, 15 Apr 2026 13:00:00 -0700",
            },
            content="B body",
        )
        c_id = _seed_doc(
            test_db,
            source_kind="gmail",
            external_id="g-c",
            metadata={
                "from": "person-x <person-a@example.com>",
                "to": "Ali <ali@example.com>",
                "thread_id": "t-tri",
                "date": "Wed, 15 Apr 2026 14:00:00 -0700",
            },
            content="C body",
        )

        rebuild_derived_for(test_db, {a_id}, directory=directory)

        rows = _derived_rows(test_db)
        # Each row pairs A with B or C; B↔C must be absent.
        for src, dst, *_ in rows:
            assert a_id in {src, dst}
        bc_rows = [
            r for r in rows
            if {r[0], r[1]} == {b_id, c_id}
        ]
        assert bc_rows == []


class TestIdempotence:
    """Two consecutive calls with the same input land the same row set."""

    def test_rebuild_idempotent(
        self, test_db: psycopg.Connection, directory: DirectoryStore
    ) -> None:
        a_id = _seed_doc(
            test_db,
            source_kind="gmail",
            external_id="ig-a",
            metadata={
                "from": "person-x <person-a@example.com>",
                "to": "Ali <ali@example.com>",
                "thread_id": "t-idem",
                "date": "Wed, 15 Apr 2026 12:00:00 -0700",
            },
            content="A",
        )
        b_id = _seed_doc(
            test_db,
            source_kind="gmail",
            external_id="ig-b",
            metadata={
                "from": "Ali <ali@example.com>",
                "to": "person-x <person-a@example.com>",
                "thread_id": "t-idem",
                "date": "Wed, 15 Apr 2026 13:00:00 -0700",
            },
            content="B",
        )

        first = rebuild_derived_for(
            test_db, {a_id, b_id}, directory=directory
        )
        first_rows = _derived_rows(test_db)
        second = rebuild_derived_for(
            test_db, {a_id, b_id}, directory=directory
        )
        second_rows = _derived_rows(test_db)

        # Inserted-count is per-call (DELETE then INSERT), so both calls
        # report the same number; the row set is unchanged.
        assert first == second
        assert first_rows == second_rows


class TestCanonicalOrdering:
    """``src_document_id < dst_document_id`` after canonicalization."""

    def test_pair_canonical_ordering(
        self, test_db: psycopg.Connection, directory: DirectoryStore
    ) -> None:
        a_id = _seed_doc(
            test_db,
            source_kind="gmail",
            external_id="co-a",
            metadata={
                "from": "person-x <person-a@example.com>",
                "to": "Ali <ali@example.com>",
                "thread_id": "t-co",
                "date": "Wed, 15 Apr 2026 12:00:00 -0700",
            },
            content="A",
        )
        b_id = _seed_doc(
            test_db,
            source_kind="gmail",
            external_id="co-b",
            metadata={
                "from": "Ali <ali@example.com>",
                "to": "person-x <person-a@example.com>",
                "thread_id": "t-co",
                "date": "Wed, 15 Apr 2026 13:00:00 -0700",
            },
            content="B",
        )

        rebuild_derived_for(test_db, {a_id, b_id}, directory=directory)

        rows = _derived_rows(test_db)
        assert rows  # at least one edge
        for src, dst, *_ in rows:
            assert src < dst, (
                "canonical ordering should always yield src lex-< dst"
            )
            assert {src, dst} == {a_id, b_id}


class TestSelfPair:
    """A doc paired with itself never produces an edge."""

    def test_self_pair_never_inserted(
        self, test_db: psycopg.Connection, directory: DirectoryStore
    ) -> None:
        a_id = _seed_doc(
            test_db,
            source_kind="gmail",
            external_id="self-a",
            metadata={
                # Email pointed at itself; no second doc in the corpus.
                "from": "Self <self@example.com>",
                "to": "Self <self@example.com>",
                "thread_id": "t-self",
                "date": "Wed, 15 Apr 2026 12:00:00 -0700",
            },
            content="lonely",
        )

        result = rebuild_derived_for(
            test_db, {a_id}, directory=directory
        )

        assert result == 0
        assert _derived_rows(test_db) == []


class TestEmptyInput:
    """Empty doc_ids → zero, no DB writes."""

    def test_empty_doc_ids_returns_zero(
        self, test_db: psycopg.Connection, directory: DirectoryStore
    ) -> None:
        # Pre-seed an unrelated derived_links row so we can prove the empty
        # call neither reads nor writes the table. Build it via the public
        # path: a rebuild over a 2-doc corpus, then assert empty doc_ids
        # leaves it intact.
        a_id = _seed_doc(
            test_db,
            source_kind="gmail",
            external_id="emp-a",
            metadata={
                "from": "person-x <person-a@example.com>",
                "to": "Ali <ali@example.com>",
                "thread_id": "t-emp",
                "date": "Wed, 15 Apr 2026 12:00:00 -0700",
            },
            content="A",
        )
        b_id = _seed_doc(
            test_db,
            source_kind="gmail",
            external_id="emp-b",
            metadata={
                "from": "Ali <ali@example.com>",
                "to": "person-x <person-a@example.com>",
                "thread_id": "t-emp",
                "date": "Wed, 15 Apr 2026 13:00:00 -0700",
            },
            content="B",
        )
        rebuild_derived_for(test_db, {a_id, b_id}, directory=directory)
        before = _derived_rows(test_db)
        assert before  # sanity

        result = rebuild_derived_for(test_db, set(), directory=directory)

        assert result == 0
        assert _derived_rows(test_db) == before


class TestRulePriority:
    """R1 (thread match) and R2 (shared participant) can coexist on the same
    Gmail+Gmail pair; R2 is only suppressed when R3 fires (which requires
    Krisp+Gmail)."""

    def test_rule_priority_R1_and_R2_coexist(
        self, test_db: psycopg.Connection, directory: DirectoryStore
    ) -> None:
        a_id = _seed_doc(
            test_db,
            source_kind="gmail",
            external_id="co1-a",
            metadata={
                "from": "person-x <person-a@example.com>",
                "to": "Ali <ali@example.com>",
                "thread_id": "t-co1",
                "date": "Wed, 15 Apr 2026 12:00:00 -0700",
            },
            content="A",
        )
        b_id = _seed_doc(
            test_db,
            source_kind="gmail",
            external_id="co1-b",
            metadata={
                "from": "Ali <ali@example.com>",
                "to": "person-x <person-a@example.com>",
                "thread_id": "t-co1",
                "date": "Wed, 15 Apr 2026 13:00:00 -0700",
            },
            content="B",
        )

        rebuild_derived_for(test_db, {a_id, b_id}, directory=directory)

        rows = _derived_rows(test_db)
        rules = sorted(r[2] for r in rows)
        # R1 and R2 can coexist on a Gmail+Gmail pair because R2 is only
        # suppressed when R3 fires.
        assert rules == ["shared_participant", "shared_thread"]
        # Both rows reference the same canonical pair.
        for src, dst, *_ in rows:
            assert src < dst
            assert {src, dst} == {a_id, b_id}


class TestR3SupersedesR2:
    """For a Krisp+Gmail pair where both R3 and R2 are eligible, only R3 lands."""

    def test_R3_supersedes_R2_same_pair(
        self, test_db: psycopg.Connection, directory: DirectoryStore
    ) -> None:
        krisp_id = _seed_doc(
            test_db,
            source_kind="krisp",
            external_id="sup-k",
            metadata={
                "_participant_keys": ["person-a@example.com"],
                "date": "2026-04-15",
            },
            content="**person-a@example.com | 0:01**\nhello",
        )
        gmail_id = _seed_doc(
            test_db,
            source_kind="gmail",
            external_id="sup-g",
            metadata={
                "from": "person-x <person-a@example.com>",
                "to": "Ali <ali@example.com>",
                "thread_id": "t-sup",
                "date": "Wed, 15 Apr 2026 12:00:00 -0700",
            },
            content="body",
        )

        rebuild_derived_for(
            test_db, {krisp_id, gmail_id}, directory=directory
        )

        rows = _derived_rows(test_db)
        rules = [r[2] for r in rows]
        # R3 wins; R2 is suppressed; R1 is impossible (krisp + gmail).
        assert rules == ["same_day_participant"]


class TestPreservesUnrelated:
    """Edges that don't touch ``doc_ids`` stay put."""

    def test_existing_unrelated_edges_preserved(
        self, test_db: psycopg.Connection, directory: DirectoryStore
    ) -> None:
        # First, build A↔B via the public path (gmail same-thread).
        a_id = _seed_doc(
            test_db,
            source_kind="gmail",
            external_id="pre-a",
            metadata={
                "from": "person-x <person-a@example.com>",
                "to": "Ali <ali@example.com>",
                "thread_id": "t-ab",
                "date": "Wed, 15 Apr 2026 12:00:00 -0700",
            },
            content="A",
        )
        b_id = _seed_doc(
            test_db,
            source_kind="gmail",
            external_id="pre-b",
            metadata={
                "from": "Ali <ali@example.com>",
                "to": "person-x <person-a@example.com>",
                "thread_id": "t-ab",
                "date": "Wed, 15 Apr 2026 13:00:00 -0700",
            },
            content="B",
        )
        rebuild_derived_for(test_db, {a_id, b_id}, directory=directory)
        ab_count_before = test_db.execute(
            "SELECT count(*) FROM derived_links "
            "WHERE (src_document_id::text = %s AND dst_document_id::text = %s) "
            "OR (src_document_id::text = %s AND dst_document_id::text = %s)",
            (a_id, b_id, b_id, a_id),
        ).fetchone()
        assert ab_count_before is not None
        assert ab_count_before[0] >= 1

        # Now drop a manually-crafted C↔D row in via direct SQL — simulates
        # an edge whose endpoints are different docs the rebuild won't touch.
        c_id = _seed_doc(
            test_db,
            source_kind="gmail",
            external_id="pre-c",
            metadata={
                "from": "Carol <carol@example.com>",
                "to": "Dave <dave@example.com>",
                "thread_id": "t-cd",
                "date": "Wed, 15 Apr 2026 12:00:00 -0700",
            },
            content="C",
        )
        d_id = _seed_doc(
            test_db,
            source_kind="gmail",
            external_id="pre-d",
            metadata={
                "from": "Dave <dave@example.com>",
                "to": "Carol <carol@example.com>",
                "thread_id": "t-cd",
                "date": "Wed, 15 Apr 2026 13:00:00 -0700",
            },
            content="D",
        )
        canonical_src, canonical_dst = sorted([c_id, d_id])
        test_db.execute(
            "INSERT INTO derived_links "
            "(src_document_id, dst_document_id, rule, evidence, weight) "
            "VALUES (%s, %s, %s, %s::jsonb, %s)",
            (
                canonical_src,
                canonical_dst,
                "shared_thread",
                json.dumps({"thread_id": "t-cd"}),
                1.0,
            ),
        )

        # Rebuild only A's edges. C↔D must survive.
        rebuild_derived_for(test_db, {a_id}, directory=directory)

        cd_row = test_db.execute(
            "SELECT count(*) FROM derived_links "
            "WHERE src_document_id::text = %s AND dst_document_id::text = %s",
            (canonical_src, canonical_dst),
        ).fetchone()
        assert cd_row is not None
        assert cd_row[0] == 1


# --------------------------------------------------------------------------
# Malformed-metadata branches — exercise the defensive guards in
# ``_parse_date`` and ``_krisp_participant_keys``. These rarely occur in
# practice but the rules are public-facing helpers, so the guards must be
# proven not to crash the pass.
# --------------------------------------------------------------------------


class TestMalformedMetadata:
    """Defensive parsing guards for edge-case metadata shapes."""

    def test_krisp_participant_keys_not_a_list_yields_empty_keys(
        self, test_db: psycopg.Connection, directory: DirectoryStore
    ) -> None:
        # ``_participant_keys`` was somehow stored as a non-list (e.g. a
        # legacy row or a hand-edited DB). The rebuild must skip cleanly.
        krisp_id = _seed_doc(
            test_db,
            source_kind="krisp",
            external_id="bad-k1",
            metadata={
                "_participant_keys": "person-a@example.com",  # wrong type
                "date": "2026-04-15",
            },
            content="x",
        )
        gmail_id = _seed_doc(
            test_db,
            source_kind="gmail",
            external_id="bad-g1",
            metadata={
                "from": "person-x <person-a@example.com>",
                "to": "Ali <ali@example.com>",
                "thread_id": "t-bad1",
                "date": "Wed, 15 Apr 2026 12:00:00 -0700",
            },
            content="y",
        )

        result = rebuild_derived_for(
            test_db, {krisp_id, gmail_id}, directory=directory
        )

        # No keys, no overlap, no edges.
        assert result == 0
        assert _derived_rows(test_db) == []

    def test_krisp_participant_keys_with_garbage_entries_filtered(
        self, test_db: psycopg.Connection, directory: DirectoryStore
    ) -> None:
        # A list mixing non-string + empty string entries — they're dropped;
        # the lone valid email key still allows the cross-source match.
        krisp_id = _seed_doc(
            test_db,
            source_kind="krisp",
            external_id="bad-k2",
            metadata={
                "_participant_keys": [
                    None,                     # non-string
                    "",                       # empty
                    "   ",                    # whitespace-only
                    "person-a@example.com",        # valid
                ],
                "date": "2026-04-15",
            },
            content="x",
        )
        gmail_id = _seed_doc(
            test_db,
            source_kind="gmail",
            external_id="bad-g2",
            metadata={
                "from": "person-x <person-a@example.com>",
                "to": "Ali <ali@example.com>",
                "thread_id": "t-bad2",
                "date": "Wed, 15 Apr 2026 12:00:00 -0700",
            },
            content="y",
        )

        rebuild_derived_for(
            test_db, {krisp_id, gmail_id}, directory=directory
        )

        rows = _derived_rows(test_db)
        # R3 fires (same date + shared participant); R2 suppressed by R3.
        assert [r[2] for r in rows] == ["same_day_participant"]

    def test_missing_or_blank_dates_degrade_to_R2(
        self, test_db: psycopg.Connection, directory: DirectoryStore
    ) -> None:
        # Both dates missing → R3 can't fire; R2 still does.
        krisp_id = _seed_doc(
            test_db,
            source_kind="krisp",
            external_id="bad-k3",
            metadata={
                "_participant_keys": ["person-a@example.com"],
                "date": None,  # not a string
            },
            content="x",
        )
        gmail_id = _seed_doc(
            test_db,
            source_kind="gmail",
            external_id="bad-g3",
            metadata={
                "from": "person-x <person-a@example.com>",
                "to": "Ali <ali@example.com>",
                "thread_id": "t-bad3",
                "date": "   ",  # blank string
            },
            content="y",
        )

        rebuild_derived_for(
            test_db, {krisp_id, gmail_id}, directory=directory
        )

        rows = _derived_rows(test_db)
        assert [r[2] for r in rows] == ["shared_participant"]

    def test_unparseable_dates_degrade_to_R2(
        self, test_db: psycopg.Connection, directory: DirectoryStore
    ) -> None:
        # Krisp ISO parse fails; Gmail RFC parse fails. Pass shouldn't crash;
        # R2 still lands.
        krisp_id = _seed_doc(
            test_db,
            source_kind="krisp",
            external_id="bad-k4",
            metadata={
                "_participant_keys": ["person-a@example.com"],
                "date": "not-a-date",
            },
            content="x",
        )
        gmail_id = _seed_doc(
            test_db,
            source_kind="gmail",
            external_id="bad-g4",
            metadata={
                "from": "person-x <person-a@example.com>",
                "to": "Ali <ali@example.com>",
                "thread_id": "t-bad4",
                "date": "totally bogus",
            },
            content="y",
        )

        rebuild_derived_for(
            test_db, {krisp_id, gmail_id}, directory=directory
        )

        rows = _derived_rows(test_db)
        assert [r[2] for r in rows] == ["shared_participant"]
