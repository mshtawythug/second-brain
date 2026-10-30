"""The concept-skip policy, wired to its consumer (RM-30, criteria 6 and 7).

``graph_rag.extract_policy`` names the content types whose concept aspect is
never extracted, and the watermark such a document carries. These tests drive
``build_graph`` (and once the CLI) against the AGE test instance (port 5434),
with an extractor that FAILS the test if it is called for a skipped document.

Criterion 7 bumps the policy with ``monkeypatch.setattr`` on the policy module,
a standard test double (CLAUDE.md rule 13). All names are synthetic.
"""
from __future__ import annotations

import hashlib
import json
import uuid
from typing import Any

import psycopg
import pytest
from typer.testing import CliRunner

from brain.cli import app
from brain.graph_rag import extract_policy
from brain.graph_rag.build import build_graph
from brain.graph_rag.concepts import CONCEPTS_ASPECT
from brain.graph_rag.extract import ExtractedEntity
from brain.graph_rag.person_resolver import default_person_resolver
from brain.graph_rag.reconcile_plan import PEOPLE_ASPECT, plan_reconcile
from brain.graph_rag.relational import index_state
from tests.conftest import TEST_DATABASE_URL
from tests.test_graphrag_concepts import (
    FakeExtractor,
    _age_concept_count,
    _age_mentioned_in_count,
    _backend,
    _ccfg,
    _concept,
    _concept_keys,
    _concept_mention_count,
    _mention_count,
    _person_keys,
    _seed_directory,
    _seed_gmail_doc,
)

_TENANT = "default"
_PEOPLE = (("Avery Sample", "avery@example.com"), ("Blake Sample", "blake@example.com"))


class _ExtractorThatMustNotRun:
    """An :class:`EntityExtractor` that fails the test if it is called."""

    version = "fake-model@concepts-v1"

    def extract(self, text: str) -> list[ExtractedEntity]:
        pytest.fail("the concept extractor was called for a skipped document")


def _seed_calendar_doc(conn: psycopg.Connection[Any], *, external_id: str) -> str:
    """Insert a ``calendar_event`` document from the ``calendar`` source."""
    src = conn.execute(
        "INSERT INTO sources (kind, external_id, metadata) "
        "VALUES ('calendar', %s, '{}'::jsonb) RETURNING id",
        (external_id,),
    ).fetchone()
    assert src is not None
    content = f"Quarterly planning sync\n<!-- {uuid.uuid4()} -->"
    metadata = {"participants": [email for _name, email in _PEOPLE]}
    row = conn.execute(
        """
        INSERT INTO documents
            (source_id, title, content, content_hash, content_type, metadata)
        VALUES (%s, 'Quarterly planning sync', %s, %s, 'calendar_event', %s::jsonb)
        RETURNING id::text
        """,
        (
            src[0],
            content,
            hashlib.sha256(content.encode("utf-8")).hexdigest(),
            json.dumps(metadata),
        ),
    ).fetchone()
    assert row is not None
    return str(row[0])


def _content_hash(conn: psycopg.Connection[Any], document_id: str) -> str:
    row = conn.execute(
        "SELECT content_hash FROM documents WHERE id = %s", (document_id,)
    ).fetchone()
    assert row is not None
    return str(row[0])


def test_plan_for_a_skipped_type_uses_the_skip_pair_and_fetches_no_body(
    test_db: psycopg.Connection[Any],
) -> None:
    doc = _seed_calendar_doc(test_db, external_id="evt-plan")

    plan = plan_reconcile(
        test_db,
        doc,
        _ccfg(),
        person_resolver=default_person_resolver,
        extractor=_ExtractorThatMustNotRun(),
        force=False,
    )

    assert plan.concept_stale is True
    assert plan.concept_skipped is True
    assert plan.content == ""  # the body is never read for concepts
    assert (plan.concept_inputs_hash, plan.concept_extractor_ver) == (
        extract_policy.skip_watermark("calendar_event")
    )


def test_plan_for_another_type_is_not_skipped(test_db: psycopg.Connection[Any]) -> None:
    doc = _seed_gmail_doc(test_db, external_id="thr-plan", participants=_PEOPLE)
    extractor = FakeExtractor()

    plan = plan_reconcile(
        test_db,
        doc,
        _ccfg(),
        person_resolver=default_person_resolver,
        extractor=extractor,
        force=False,
    )

    assert plan.concept_skipped is False
    assert plan.concept_extractor_ver == extractor.version
    assert plan.content


def test_backfill_reconciles_a_calendar_event_once_then_skips_it(
    test_db: psycopg.Connection[Any],
) -> None:
    """Criterion 6, through ``build_graph``."""
    backend = _backend(test_db)
    doc = _seed_calendar_doc(test_db, external_id="evt-1")
    extractor = _ExtractorThatMustNotRun()

    first = build_graph(
        test_db, [doc], backend=backend, config=_ccfg(), extractor=extractor
    )

    assert (first.reconciled, first.skipped) == (1, 0)
    inputs_hash, extractor_ver = extract_policy.skip_watermark("calendar_event") or ("", "")
    stored = index_state(test_db, _TENANT, doc, CONCEPTS_ASPECT)
    assert stored is not None
    assert stored[:3] == (_content_hash(test_db, doc), inputs_hash, extractor_ver)
    assert index_state(test_db, _TENANT, doc, PEOPLE_ASPECT) is not None

    second = build_graph(
        test_db, [doc], backend=backend, config=_ccfg(), extractor=extractor
    )

    assert (second.reconciled, second.skipped) == (0, 1)


def test_cli_backfill_reports_the_calendar_event_skipped_on_the_second_run(
    test_db: psycopg.Connection[Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    """Criterion 6, through ``brain graphrag build --backfill --concepts``."""
    _seed_calendar_doc(test_db, external_id="evt-cli")
    monkeypatch.setattr(
        "brain.graph_rag.extract.make_extractor", lambda cfg: _ExtractorThatMustNotRun()
    )
    monkeypatch.setenv("DATABASE_URL", TEST_DATABASE_URL)
    monkeypatch.setenv("BRAIN_GRAPH_GENERIC_DF", "1.0")
    args = ["graphrag", "build", "--backfill", "--concepts"]

    first = CliRunner().invoke(app, args)
    second = CliRunner().invoke(app, args)

    assert first.exit_code == 0, first.output
    assert "1 processed (reconciled 1, skipped 0)" in first.output
    assert second.exit_code == 0, second.output
    assert "1 processed (reconciled 0, skipped 1)" in second.output


def test_a_policy_bump_strips_earlier_concepts_and_keeps_people(
    test_db: psycopg.Connection[Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    """Criterion 7: a type added to the set loses its concept graph, not its people."""
    backend = _backend(test_db)
    _seed_directory(test_db, _PEOPLE)
    doc = _seed_gmail_doc(
        test_db, external_id="thr-1", participants=_PEOPLE, content="acmepay phoenix"
    )
    working = FakeExtractor(
        default=[_concept("acmepay", "tool"), _concept("phoenix", "project", positions=(1,))]
    )
    build_graph(test_db, [doc], backend=backend, config=_ccfg(), extractor=working)
    people_before = _person_keys(test_db, _TENANT)
    person_mentions = _mention_count(test_db, _TENANT) - _concept_mention_count(
        test_db, _TENANT
    )
    assert _concept_keys(test_db, _TENANT) == {"acmepay", "phoenix"}
    assert len(people_before) == 2
    assert _age_concept_count(test_db, _TENANT) == 2

    monkeypatch.setattr(
        extract_policy,
        "CONCEPT_SKIP_CONTENT_TYPES",
        extract_policy.CONCEPT_SKIP_CONTENT_TYPES | {"email"},
    )
    monkeypatch.setattr(extract_policy, "CONCEPT_SKIP_POLICY_VERSION", "v-test-bump")
    result = build_graph(
        test_db,
        [doc],
        backend=backend,
        config=_ccfg(),
        extractor=_ExtractorThatMustNotRun(),
    )

    assert result.reconciled == 1
    assert _concept_mention_count(test_db, _TENANT) == 0
    assert _concept_keys(test_db, _TENANT) == set()  # orphan concepts collected
    assert _age_concept_count(test_db, _TENANT) == 0
    assert _person_keys(test_db, _TENANT) == people_before
    assert _mention_count(test_db, _TENANT) == person_mentions
    # Only the people's MENTIONED_IN edges are left in AGE.
    assert _age_mentioned_in_count(test_db, _TENANT) == person_mentions
    concept_contributions = test_db.execute(
        "SELECT count(*) FROM graph_edge_contributions c "
        "JOIN graph_entities e ON e.tenant_id = c.tenant_id "
        "AND e.id IN (c.src_id, c.dst_id) "
        "WHERE c.tenant_id = %s AND e.entity_type <> 'person'",
        (_TENANT,),
    ).fetchone()
    assert concept_contributions is not None and concept_contributions[0] == 0
    stored = index_state(test_db, _TENANT, doc, CONCEPTS_ASPECT)
    assert stored is not None
    assert stored[1:3] == ("skip:email:v-test-bump", "skipped-by-policy")
