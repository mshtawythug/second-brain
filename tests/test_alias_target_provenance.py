"""A gated lens names an entity only if a public record carries its name (RM-46 sec-118f LOW-1).

An owner-written alias rule (``zc`` → ``zephyrcode``) re-points a public
document's mention of "ZC" onto the target entity "Zephyrcode". If "Zephyrcode"
appears only in confidential mail, the public-mention tests still passed (the
target now HAS a public mention), so the confidential-only name came back from
the timeline's entity names and co-topics, the brief's new entities, the review
findings list and the conflict scan.

The gated lens now also asks for name provenance:
``brain.sensitivity.name_in_public_record_sql``. Some non-confidential, non-draft
document that mentions the entity must carry the name in its title, content or
metadata. Metadata counts so that a person known from an email header or a call's
participant list still passes. A target whose own name is in a public document is
listed as before, and the permissive lens is unchanged.

The alias apply step is simulated by the mention it leaves behind. Everything
here is synthetic.
"""
from __future__ import annotations

import json
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

import psycopg
import pytest

from brain.brief_entities import new_entities
from brain.config import Config
from brain.review.queries import iter_entities_for_conflict_scan, list_review_queue
from brain.sensitivity import name_in_public_record_sql
from brain.timeline import build_timeline
from brain.timeline_entities import resolve_entities, top_cotopics
from tests.conftest import TEST_DATABASE_URL

pytestmark = pytest.mark.usefixtures("test_db")

TENANT = "default"
SECRET = "Zephyrcode"
PUBLIC = "Lanterncode"


def _doc(
    conn: psycopg.Connection[Any],
    content: str,
    *,
    confidential: bool = False,
    metadata: dict[str, Any] | None = None,
) -> str:
    row = conn.execute(
        "INSERT INTO documents (title, content, content_hash, content_type, sensitivity, "
        "summary, metadata) VALUES ('Synthetic note', %s, %s, 'note', %s, 'synthetic', %s) "
        "RETURNING id::text",
        (
            content,
            uuid.uuid4().hex,
            "confidential" if confidential else "normal",
            json.dumps(metadata or {}),
        ),
    ).fetchone()
    assert row is not None
    return str(row[0])


def _entity(conn: psycopg.Connection[Any], name: str, entity_type: str = "project") -> str:
    row = conn.execute(
        "INSERT INTO graph_entities (tenant_id, entity_type, name, canonical_key) "
        "VALUES (%s, %s, %s, %s) RETURNING id::text",
        (TENANT, entity_type, name, name.lower()),
    ).fetchone()
    assert row is not None
    return str(row[0])


def _mention(conn: psycopg.Connection[Any], entity_id: str, doc_id: str) -> None:
    conn.execute(
        "INSERT INTO graph_entity_mentions (tenant_id, entity_id, document_id, source) "
        "VALUES (%s, %s, %s, 'extractor:fake@test')",
        (TENANT, entity_id, doc_id),
    )


def _edge(conn: psycopg.Connection[Any], doc_id: str, a: str, b: str) -> None:
    src, dst = sorted([a, b])
    conn.execute(
        "INSERT INTO graph_edge_contributions "
        "(tenant_id, document_id, src_id, dst_id, cooccur_count) VALUES (%s, %s, %s, %s, 1)",
        (TENANT, doc_id, src, dst),
    )


@pytest.fixture
def corpus(test_db: psycopg.Connection[Any]) -> dict[str, str]:
    """The alias target's name is only in confidential mail; the control's is public."""
    secret_mail = _doc(test_db, f"Terms for {SECRET} are attached.", confidential=True)
    public_note = _doc(test_db, f"ZC and {PUBLIC} both came up in the roadmap call.")
    secret = _entity(test_db, SECRET)
    control = _entity(test_db, PUBLIC)
    seed = _entity(test_db, "Roadmap")
    _mention(test_db, secret, secret_mail)
    _mention(test_db, secret, public_note)  # what the zc -> zephyrcode alias leaves behind
    _mention(test_db, control, public_note)
    _mention(test_db, seed, public_note)
    _edge(test_db, public_note, seed, secret)
    _edge(test_db, public_note, seed, control)
    return {"public_note": public_note, "secret": secret, "control": control, "seed": seed}


# ------------------------------------------------------------------------- timeline


def test_timeline_names_only_a_publicly_named_entity_in_the_gated_lens(
    test_db: psycopg.Connection[Any], corpus: dict[str, str]
) -> None:
    gated = {row.name for row in resolve_entities(test_db, TENANT, "code", public_only=True)}
    every = {row.name for row in resolve_entities(test_db, TENANT, "code")}

    assert gated == {PUBLIC}
    assert every == {SECRET, PUBLIC}


def test_timeline_cotopics_name_only_a_publicly_named_entity_in_the_gated_lens(
    test_db: psycopg.Connection[Any], corpus: dict[str, str]
) -> None:
    def cotopics(public_only: bool) -> list[str]:
        return top_cotopics(
            test_db, TENANT, [corpus["public_note"]], [corpus["seed"]], [],
            public_only=public_only,
        )

    assert cotopics(True) == [PUBLIC]
    assert sorted(cotopics(False)) == [PUBLIC, SECRET]
    assert top_cotopics(test_db, TENANT, [], [corpus["seed"]], []) == []


@pytest.mark.parametrize(("hidden", "expected"), [(True, [PUBLIC]), (False, [PUBLIC, SECRET])])
def test_the_timeline_passes_its_mode_to_the_cotopics(
    test_db: psycopg.Connection[Any], corpus: dict[str, str], hidden: bool, expected: list[str]
) -> None:
    ctx = build_timeline(
        test_db, Config(database_url=TEST_DATABASE_URL), "Roadmap",
        exclude_confidential=True, public_entities_only=hidden,
    )

    assert sorted(name for bucket in ctx.buckets for name in bucket.cotopics) == expected


# ---------------------------------------------------------------------------- brief


@pytest.mark.usefixtures("corpus")
def test_brief_lists_only_a_publicly_named_new_entity_in_the_gated_lens(
    test_db: psycopg.Connection[Any],
) -> None:
    since = datetime.now(UTC) - timedelta(days=7)

    def names(exclude: bool) -> set[str]:
        return {
            entity.name
            for entity in new_entities(
                test_db, tenant_id=TENANT, since=since, exclude_confidential=exclude
            )
        }

    assert names(True) == {PUBLIC, "Roadmap"}
    assert names(False) == {SECRET, PUBLIC, "Roadmap"}


def test_a_person_known_from_a_participant_list_still_passes(
    test_db: psycopg.Connection[Any],
) -> None:
    call = _doc(
        test_db, "We went through the plan.", metadata={"participants": ["Ann Vale"]}
    )
    _mention(test_db, _entity(test_db, "Ann Vale", "person"), call)
    since = datetime.now(UTC) - timedelta(days=7)

    listed = new_entities(test_db, tenant_id=TENANT, since=since, exclude_confidential=True)

    assert [entity.name for entity in listed] == ["Ann Vale"]


# --------------------------------------------------------------------------- review


@pytest.mark.usefixtures("corpus")
def test_the_conflict_scan_skips_an_entity_named_only_in_confidential_mail(
    test_db: psycopg.Connection[Any],
) -> None:
    def keys(exclude: bool) -> set[str]:
        return {
            candidate.canonical_key
            for candidate in iter_entities_for_conflict_scan(
                test_db, tenant_id=TENANT, min_docs=1, limit=10, exclude_confidential=exclude
            )
        }

    assert SECRET.lower() not in keys(True)
    assert PUBLIC.lower() in keys(True)
    assert SECRET.lower() in keys(False)


def _finding(conn: psycopg.Connection[Any], key: str, evidence: list[str]) -> None:
    conn.execute(
        "INSERT INTO elicitation_gaps (tenant_id, signal_kind, target_type, target_id, score, "
        "evidence_ids, rationale, status) VALUES (%s, 'contradiction', 'project', %s, 0.9, %s, "
        "%s, 'surfaced')",
        (TENANT, key, evidence, f"Two notes disagree about {key}."),
    )


def test_the_findings_list_withholds_a_finding_about_a_confidentially_named_target(
    test_db: psycopg.Connection[Any], corpus: dict[str, str]
) -> None:
    _finding(test_db, SECRET.lower(), [corpus["public_note"]])
    _finding(test_db, PUBLIC.lower(), [corpus["public_note"]])

    def targets(exclude: bool) -> set[str]:
        return {
            row.target_id
            for row in list_review_queue(
                test_db, tenant_id=TENANT, signal_kinds=["contradiction"], limit=10,
                exclude_confidential=exclude,
            )
        }

    assert targets(True) == {PUBLIC.lower()}
    assert targets(False) == {SECRET.lower(), PUBLIC.lower()}


def test_the_provenance_predicate_refuses_a_non_identifier_alias() -> None:
    with pytest.raises(ValueError, match="plain identifier"):
        name_in_public_record_sql("e; DROP TABLE documents")
    assert "e.name" in name_in_public_record_sql("e")
