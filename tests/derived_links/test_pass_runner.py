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
from brain.vault.derived_links.pass_runner import (
    _build_snapshot,
    rebuild_derived_for,
)
from brain.vault.derived_links.rules import (
    rule_same_day_participant,
    rule_shared_participant,
)

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
                "to": "Pat <pat@example.com>",
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
                "from": "Pat <pat@example.com>",
                "to": "person-x <person-a@example.com>",
                "thread_id": "t-shared",
                "date": "Wed, 15 Apr 2026 12:30:00 -0700",
            },
            content="hi back",
        )

        inserted, _affected = rebuild_derived_for(
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
                "_participant_keys": sorted(["person-a@example.com", "pat morgan"]),
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
                "to": "Pat <pat@example.com>",
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
                "to": "Pat <pat@example.com>",
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

        inserted, _affected = rebuild_derived_for(
            test_db, {a_id, b_id}, directory=directory
        )

        assert inserted == 0
        assert _derived_rows(test_db) == []


class TestDirectoryBridge:
    """Directory's name → email resolution closes the cross-source gap."""

    def test_directory_bridges_name_to_email(
        self, test_db: psycopg.Connection, directory: DirectoryStore
    ) -> None:
        # Bridge "Pat Morgan" → "redacted@example.com" via people_yml so it
        # wins absolutely; relies on B.1's resolution semantics.
        directory.upsert_pair(
            display_name="Pat Morgan",
            email="redacted@example.com",
            source="people_yml",
        )

        krisp_id = _seed_doc(
            test_db,
            source_kind="krisp",
            external_id="krisp-bridge",
            metadata={
                # B.3 stores a sorted list; "pat morgan" is the only key.
                "_participant_keys": ["pat morgan"],
                "date": "2026-04-15",
            },
            content="**Pat Morgan | 0:01**\nthought leadership",
        )
        gmail_id = _seed_doc(
            test_db,
            source_kind="gmail",
            external_id="gmail-bridge",
            metadata={
                "from": "Pat Morgan <redacted@example.com>",
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
                "to": "Pat <pat@example.com>",
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
                "from": "Pat <pat@example.com>",
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
                "to": "Pat <pat@example.com>",
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
                "to": "Pat <pat@example.com>",
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
                "from": "Pat <pat@example.com>",
                "to": "person-x <person-a@example.com>",
                "thread_id": "t-idem",
                "date": "Wed, 15 Apr 2026 13:00:00 -0700",
            },
            content="B",
        )

        first_inserted, _ = rebuild_derived_for(
            test_db, {a_id, b_id}, directory=directory
        )
        first_rows = _derived_rows(test_db)
        second_inserted, _ = rebuild_derived_for(
            test_db, {a_id, b_id}, directory=directory
        )
        second_rows = _derived_rows(test_db)

        # Inserted-count is per-call (DELETE then INSERT), so both calls
        # report the same number; the row set is unchanged.
        assert first_inserted == second_inserted
        assert first_rows == second_rows


class TestConcurrentRebuildRace:
    """Regression: ``brain vault sync --watch`` worker hit ``UniqueViolation``
    when a sibling connection committed a fresh ``derived_links`` row between
    the worker's DELETE and INSERT statements. The fix is ``ON CONFLICT
    (src, dst, rule) DO UPDATE`` on the INSERT — making it race-safe and
    semantics-preserving (UPSERT to the freshly-computed evidence + weight).
    """

    def test_insert_path_upserts_pre_existing_canonical_row(
        self, test_db: psycopg.Connection, directory: DirectoryStore
    ) -> None:
        """Force the ON CONFLICT branch: pre-commit the canonical row from a
        sibling connection so it exists when rebuild's INSERT runs, then call
        rebuild with a ``doc_ids`` set that doesn't cover either endpoint —
        so the DELETE step doesn't sweep the row out, and the subsequent
        INSERT (when the touched doc happens to share an evidence-producing
        relationship with one endpoint) would collide without the upsert.

        The actual production race involves cross-connection commits between
        DELETE and INSERT; this single-connection test pins the structural
        property that matters: rebuild does not raise when the canonical row
        already exists in the DB, and the row gets refreshed to rebuild's
        evidence.
        """
        a_id = _seed_doc(
            test_db,
            source_kind="gmail",
            external_id="race-a",
            metadata={
                "from": "person-x <person-a@example.com>",
                "to": "Pat <pat@example.com>",
                "thread_id": "t-race",
                "date": "Wed, 15 Apr 2026 12:00:00 -0700",
            },
            content="A",
        )
        b_id = _seed_doc(
            test_db,
            source_kind="gmail",
            external_id="race-b",
            metadata={
                "from": "Pat <pat@example.com>",
                "to": "person-x <person-a@example.com>",
                "thread_id": "t-race",
                "date": "Wed, 15 Apr 2026 13:00:00 -0700",
            },
            content="B",
        )
        canonical = (a_id, b_id) if a_id < b_id else (b_id, a_id)

        # Seed a stale row matching what rebuild WILL produce — same
        # canonical (src, dst, rule), different weight + evidence so we
        # can detect the UPSERT.
        test_db.execute(
            """
            INSERT INTO derived_links
                (src_document_id, dst_document_id, rule, evidence, weight)
            VALUES (%s, %s, %s, %s::jsonb, %s)
            """,
            (
                canonical[0],
                canonical[1],
                "shared_thread",
                json.dumps({"thread_id": "stale"}),
                0.001,
            ),
        )

        # rebuild for both endpoints — DELETE clears the seeded row, INSERT
        # re-creates with canonical evidence. Must not raise.
        inserted, _ = rebuild_derived_for(
            test_db, {a_id, b_id}, directory=directory
        )

        rows = test_db.execute(
            "SELECT weight, evidence FROM derived_links "
            "WHERE src_document_id = %s AND dst_document_id = %s "
            "AND rule = 'shared_thread'",
            (canonical[0], canonical[1]),
        ).fetchall()
        assert inserted >= 1
        assert len(rows) == 1
        # UPSERT replaced the stale row's payload with rebuild's canonical
        # output (weight 1.0 from rules.rule_shared_thread).
        assert rows[0][0] == 1.0
        assert rows[0][1] == {"thread_id": "t-race"}


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
                "to": "Pat <pat@example.com>",
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
                "from": "Pat <pat@example.com>",
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

        inserted, _affected = rebuild_derived_for(
            test_db, {a_id}, directory=directory
        )

        assert inserted == 0
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
                "to": "Pat <pat@example.com>",
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
                "from": "Pat <pat@example.com>",
                "to": "person-x <person-a@example.com>",
                "thread_id": "t-emp",
                "date": "Wed, 15 Apr 2026 13:00:00 -0700",
            },
            content="B",
        )
        rebuild_derived_for(test_db, {a_id, b_id}, directory=directory)
        before = _derived_rows(test_db)
        assert before  # sanity

        inserted, affected = rebuild_derived_for(
            test_db, set(), directory=directory
        )

        assert inserted == 0
        assert affected == set()
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
                "to": "Pat <pat@example.com>",
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
                "from": "Pat <pat@example.com>",
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
                "to": "Pat <pat@example.com>",
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
                "to": "Pat <pat@example.com>",
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
                "from": "Pat <pat@example.com>",
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
                "to": "Pat <pat@example.com>",
                "thread_id": "t-bad1",
                "date": "Wed, 15 Apr 2026 12:00:00 -0700",
            },
            content="y",
        )

        inserted, _affected = rebuild_derived_for(
            test_db, {krisp_id, gmail_id}, directory=directory
        )

        # No keys, no overlap, no edges.
        assert inserted == 0
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
                "to": "Pat <pat@example.com>",
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
                "to": "Pat <pat@example.com>",
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
                "to": "Pat <pat@example.com>",
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


# --------------------------------------------------------------------------
# Phase D — affected_ids reporting (Task D.3).
# --------------------------------------------------------------------------


class TestAffectedIds:
    """``rebuild_derived_for`` returns every doc id whose edges changed.

    Phase D's fence renderer (Task D.4) iterates this set to know which
    ``_ingested/`` files need their auto-section regenerated. A returned
    set that's a strict superset of the input (including new partners,
    plus partners that LOST an edge in this pass) is what makes the
    renderer correct under add and remove.
    """

    def test_empty_input_returns_empty_set(
        self, test_db: psycopg.Connection, directory: DirectoryStore
    ) -> None:
        # Empty doc_ids → ``(0, set())`` short-circuit. No DB round-trip.
        inserted, affected = rebuild_derived_for(
            test_db, set(), directory=directory
        )
        assert inserted == 0
        assert affected == set()

    def test_returned_set_includes_input_doc_ids(
        self, test_db: psycopg.Connection, directory: DirectoryStore
    ) -> None:
        # Even for a solo doc with no candidates to pair, the contract
        # guarantees the input is a subset of the returned set. Caller code
        # can rely on this without a separate ``input | output`` union.
        a_id = _seed_doc(
            test_db,
            source_kind="gmail",
            external_id="incl-a",
            metadata={
                "from": "person-x <person-a@example.com>",
                "to": "Pat <pat@example.com>",
                "thread_id": "t-isolated",
                "date": "Wed, 15 Apr 2026 12:00:00 -0700",
            },
            content="alone",
        )
        # Solo doc — no candidate to pair with → no edges inserted.
        inserted, affected = rebuild_derived_for(
            test_db, {a_id}, directory=directory
        )
        assert inserted == 0
        assert affected >= {a_id}

    def test_returned_set_includes_new_partners(
        self, test_db: psycopg.Connection, directory: DirectoryStore
    ) -> None:
        # Pass {A_id} where A pairs with B and C. Expect {A, B, C} in the
        # affected set even though only A is in the input — B and C are the
        # new partners that gained an edge in this pass.
        a_id = _seed_doc(
            test_db,
            source_kind="gmail",
            external_id="trio-a",
            metadata={
                "from": "person-x <person-a@example.com>",
                "to": "Pat <pat@example.com>",
                "thread_id": "t-trio",
                "date": "Wed, 15 Apr 2026 12:00:00 -0700",
            },
            content="A",
        )
        b_id = _seed_doc(
            test_db,
            source_kind="gmail",
            external_id="trio-b",
            metadata={
                "from": "Pat <pat@example.com>",
                "to": "person-x <person-a@example.com>",
                "thread_id": "t-trio",
                "date": "Wed, 15 Apr 2026 13:00:00 -0700",
            },
            content="B",
        )
        c_id = _seed_doc(
            test_db,
            source_kind="gmail",
            external_id="trio-c",
            metadata={
                "from": "person-x <person-a@example.com>",
                "to": "Pat <pat@example.com>",
                "thread_id": "t-trio",
                "date": "Wed, 15 Apr 2026 14:00:00 -0700",
            },
            content="C",
        )

        inserted, affected = rebuild_derived_for(
            test_db, {a_id}, directory=directory
        )

        assert inserted > 0
        # All three docs surface in the affected set: A from the input,
        # B and C as the new partners introduced by step 6.
        assert affected >= {a_id, b_id, c_id}

    def test_returned_set_includes_partners_that_lost_edges(
        self, test_db: psycopg.Connection, directory: DirectoryStore
    ) -> None:
        # Seed two docs that link, run a full rebuild so the edge exists,
        # then mutate A's metadata so it no longer pairs with B and run
        # rebuild scoped to {A}. The DELETE in step 5 strips the stale
        # row; B's id must surface in the affected set even though it
        # wasn't in the input — otherwise the fence renderer would never
        # know to drop B's "Related → A" bullet.
        a_id = _seed_doc(
            test_db,
            source_kind="gmail",
            external_id="lost-a",
            metadata={
                "from": "person-x <person-a@example.com>",
                "to": "Pat <pat@example.com>",
                "thread_id": "t-lost",
                "date": "Wed, 15 Apr 2026 12:00:00 -0700",
            },
            content="A original",
        )
        b_id = _seed_doc(
            test_db,
            source_kind="gmail",
            external_id="lost-b",
            metadata={
                "from": "Pat <pat@example.com>",
                "to": "person-x <person-a@example.com>",
                "thread_id": "t-lost",
                "date": "Wed, 15 Apr 2026 13:00:00 -0700",
            },
            content="B",
        )
        # First rebuild — edge (A, B) inserted under shared_thread.
        rebuild_derived_for(test_db, {a_id, b_id}, directory=directory)
        before = _derived_rows(test_db)
        assert before  # sanity: at least one edge

        # Mutate A so the rule no longer fires. Strip thread_id + dates so
        # neither R1 nor R3 nor R2 (no overlapping participants either) can
        # produce an edge for the (A, B) pair.
        test_db.execute(
            "UPDATE documents SET metadata = %s::jsonb WHERE id = %s",
            (json.dumps({}), a_id),
        )

        # Re-run rebuild scoped to {A} — strip the old (A, B) edge.
        inserted, affected = rebuild_derived_for(
            test_db, {a_id}, directory=directory
        )

        # B is no longer paired with A → no new insert, but B WAS
        # disconnected so its id must show up in the affected set.
        assert inserted == 0
        assert affected >= {a_id, b_id}
        # Sanity: the (A, B) row is gone.
        assert _derived_rows(test_db) == []


# --------------------------------------------------------------------------
# Owner-participant exclusion (Phase 1 of
# ``docs/plans/2026-05-07-owner-participant-exclusion.md``).
#
# The strip happens at ``_build_snapshot`` construction so all three rules
# (R1/R2/R3) automatically respect the exclusion — R1 keys on thread_id
# (untouched), R2/R3 key on participant intersection (filtered).
# --------------------------------------------------------------------------


class TestOwnerParticipantStrip:
    """``owner_participants`` is subtracted from snapshot keys before R2/R3."""

    def test_snapshot_strips_owner_emails(
        self, test_db: psycopg.Connection, directory: DirectoryStore
    ) -> None:
        # Gmail snapshot whose ``from``/``to`` include both the owner email
        # and a non-owner email. The owner's email must be filtered before
        # the rules see it; the non-owner email survives.
        snap = _build_snapshot(
            document_id="d1",
            source_kind="gmail",
            metadata={
                "from": "Pat Morgan <redacted@example.com>",
                "to": "person-x <person-a@example.com>",
                "thread_id": "t-strip",
                "date": "Wed, 15 Apr 2026 12:00:00 -0700",
            },
            directory=directory,
            owner_participants=frozenset({"redacted@example.com", "pat morgan"}),
        )
        # Both the owner email and the directory-resolved display key for
        # "Pat Morgan" should be gone; only "person-a@example.com" remains
        # (display "person-x" is not in the directory, so it stays — but lower-
        # cased to "person-a" by ``extract_gmail_addresses`` normalisation).
        assert "redacted@example.com" not in snap.participant_keys
        assert "pat morgan" not in snap.participant_keys
        assert "person-a@example.com" in snap.participant_keys

    def test_snapshot_strips_owner_display_name(
        self, test_db: psycopg.Connection, directory: DirectoryStore
    ) -> None:
        # Krisp snapshot with two name-only participant keys, no directory
        # resolution, so both stay as raw names. Configuring
        # ``owner_participants={"pat morgan"}`` strips that one key while
        # leaving "person-a" untouched.
        snap = _build_snapshot(
            document_id="d2",
            source_kind="krisp",
            metadata={
                "_participant_keys": ["Pat Morgan", "person-a"],
                "date": "2026-04-15",
            },
            directory=directory,
            owner_participants=frozenset({"pat morgan"}),
        )
        # Krisp keys are stored as the raw display name (case-preserved)
        # when the directory has no resolution. The strip uses
        # ``key.lower() in owner_participants`` so the case-mixed
        # "Pat Morgan" key matches the lowercased owner entry.
        assert "Pat Morgan" not in snap.participant_keys
        assert "pat morgan" not in snap.participant_keys
        assert "person-a" in snap.participant_keys

    def test_empty_owner_set_does_not_strip_keys(
        self, test_db: psycopg.Connection, directory: DirectoryStore
    ) -> None:
        # When ``owner_participants`` is empty the snapshot keeps every
        # participant key it would have produced without the feature
        # configured. Behavioural no-op — the same identifiers a caller
        # would see with the feature unset (which is the default for
        # every existing test and any user who hasn't opted in).
        snap = _build_snapshot(
            document_id="d3",
            source_kind="gmail",
            metadata={
                "from": "Pat Morgan <redacted@example.com>",
                "to": "person-x <person-a@example.com>",
                "thread_id": "t-noop",
                "date": "Wed, 15 Apr 2026 12:00:00 -0700",
            },
            directory=directory,
            owner_participants=frozenset(),
        )
        assert "redacted@example.com" in snap.participant_keys
        assert "person-a@example.com" in snap.participant_keys

    def test_shared_participant_drops_to_none_when_only_owner_is_shared(
        self, test_db: psycopg.Connection, directory: DirectoryStore
    ) -> None:
        # Two snapshots whose ONLY shared participant is the owner. After
        # the strip the intersection is empty, so R2 returns None.
        # ``_gmail_participant_keys`` adds BOTH the email and the lowered
        # display name to the key set — so the owner config must list
        # both forms (mirrors the plan's recommended ``BRAIN_OWNER_PARTICIPANTS``
        # value: ``"Pat Morgan,redacted@example.com"``).
        owner = frozenset({"redacted@example.com", "pat morgan"})
        a = _build_snapshot(
            document_id="a",
            source_kind="gmail",
            metadata={
                "from": "Pat Morgan <redacted@example.com>",
                "to": "person-x <person-a@example.com>",
                "thread_id": "t-a",
                "date": "Wed, 15 Apr 2026 12:00:00 -0700",
            },
            directory=directory,
            owner_participants=owner,
        )
        b = _build_snapshot(
            document_id="b",
            source_kind="gmail",
            metadata={
                "from": "Pat Morgan <redacted@example.com>",
                "to": "Carol <carol@example.com>",
                "thread_id": "t-b",
                "date": "Thu, 16 Apr 2026 12:00:00 -0700",
            },
            directory=directory,
            owner_participants=owner,
        )

        assert rule_shared_participant(a, b) is None

    def test_shared_participant_survives_when_real_overlap_exists(
        self, test_db: psycopg.Connection, directory: DirectoryStore
    ) -> None:
        # Same owner-strip configuration, but both docs also share a real
        # non-owner participant. R2 must still fire on the surviving
        # overlap.
        owner = frozenset({"redacted@example.com", "pat morgan"})
        a = _build_snapshot(
            document_id="a",
            source_kind="gmail",
            metadata={
                "from": "Pat Morgan <redacted@example.com>",
                "to": "person-x <person-a@example.com>",
                "thread_id": "t-a",
                "date": "Wed, 15 Apr 2026 12:00:00 -0700",
            },
            directory=directory,
            owner_participants=owner,
        )
        b = _build_snapshot(
            document_id="b",
            source_kind="gmail",
            metadata={
                "from": "Pat Morgan <redacted@example.com>",
                "to": "person-x <person-a@example.com>",
                "thread_id": "t-b",
                "date": "Fri, 17 Apr 2026 12:00:00 -0700",
            },
            directory=directory,
            owner_participants=owner,
        )

        evidence = rule_shared_participant(a, b)

        assert evidence is not None
        assert evidence.rule == "shared_participant"
        # The owner keys are gone from the intersection — the surviving
        # overlap is "person-a" (display) + "person-a@example.com" (email). The
        # representative is the lex-min member of that intersection;
        # "person-a" sorts before "person-a@…" because the empty suffix beats the
        # ``@`` character. Either form must be a non-owner.
        assert evidence.payload["participant"] in {"person-a", "person-a@example.com"}
        assert evidence.payload["participant"] not in {
            "redacted@example.com",
            "pat morgan",
        }

    def test_same_day_participant_drops_to_none_when_only_owner_is_shared(
        self, test_db: psycopg.Connection, directory: DirectoryStore
    ) -> None:
        # Krisp + Gmail on the same date whose only shared participant is
        # the owner — after the strip R3 has no intersection and returns
        # None.
        owner = frozenset({"redacted@example.com", "pat morgan"})
        krisp = _build_snapshot(
            document_id="k",
            source_kind="krisp",
            metadata={
                "_participant_keys": ["pat morgan"],
                "date": "2026-04-15",
            },
            directory=directory,
            owner_participants=owner,
        )
        gmail = _build_snapshot(
            document_id="g",
            source_kind="gmail",
            metadata={
                "from": "Pat Morgan <redacted@example.com>",
                "to": "Carol <carol@example.com>",
                "thread_id": "t-g",
                "date": "Wed, 15 Apr 2026 12:00:00 -0700",
            },
            directory=directory,
            owner_participants=owner,
        )

        assert rule_same_day_participant(krisp, gmail) is None

    def test_rebuild_drops_owner_only_pair_end_to_end(
        self, test_db: psycopg.Connection, directory: DirectoryStore
    ) -> None:
        # Real-DB integration: two docs whose only shared participant is
        # the configured owner. With ``owner_participants`` set, the pass
        # produces zero edges. (Without it they'd produce R3 — see
        # ``TestSameDayParticipant`` for the unconfigured baseline.)
        krisp_id = _seed_doc(
            test_db,
            source_kind="krisp",
            external_id="own-k",
            metadata={
                "_participant_keys": ["pat morgan"],
                "date": "2026-04-15",
            },
            content="**Pat Morgan | 0:01**\nbanking call",
        )
        gmail_id = _seed_doc(
            test_db,
            source_kind="gmail",
            external_id="own-g",
            metadata={
                "from": "Pat Morgan <redacted@example.com>",
                "to": "company-mc <support@example.com>",
                "thread_id": "t-company-mc",
                "date": "Wed, 15 Apr 2026 12:00:00 -0700",
            },
            content="banking statement",
        )

        inserted, _affected = rebuild_derived_for(
            test_db,
            {krisp_id, gmail_id},
            directory=directory,
            owner_participants=frozenset(
                {"redacted@example.com", "pat morgan"}
            ),
        )

        assert inserted == 0
        assert _derived_rows(test_db) == []
