"""The weekly review's graph themes never come from a community with a confidential member (F6).

RM-46 sec-118e MEDIUM-1. ``weekly_active_communities`` ranked communities by
in-window edge weight with no sensitivity predicate, and the theme block then
carried every member's NAME and the community's stored SUMMARY, which is written
from member names and representative titles. Only the block's ``docs`` were gated.
So the default lens, the emitted vault page ("ALWAYS gated") and, over HTTP with
``--allow-writes``, a remote caller all received names and a summary drawn from
confidential mail.

With ``exclude_confidential`` a community any of whose members a confidential
document mentions is excluded BEFORE the ``LIMIT``, by the shared
``confidential_member_sql``: the rule ``list_communities`` and global graph
retrieval already apply. Every name and summary here is synthetic.
"""
from __future__ import annotations

import json
import uuid
from collections.abc import Callable
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any

import psycopg
import pytest

from brain import mcp_server
from brain.config import Config
from brain.review.render import render_weekly_md
from brain.review.weekly import build_weekly_report, weekly_active_communities
from brain.sensitivity import confidential_member_sql
from tests.conftest import TEST_DATABASE_URL

WEEK = "2026-W23"
GENERATED_ON = date(2026, 6, 9)
IN_WINDOW = datetime(2026, 6, 3, 12, 0, 0, tzinfo=UTC)
WIN_AFTER = datetime(2026, 6, 1, tzinfo=UTC)
WIN_BEFORE = datetime(2026, 6, 7, 23, 59, 59, tzinfo=UTC)

SECRET_NAME = "Zephyrcode"
SECRET_SUMMARY = "Acquisition talks for Zephyrcode (synthetic)"
PUBLIC_NAME = "roadmap-synthetic"
PUBLIC_SUMMARY = "Roadmap planning with the team (synthetic)"


def _cfg() -> Config:
    return Config(
        database_url=TEST_DATABASE_URL,
        graph_enabled=True,
        graph_tenant_id="default",
        review_theme_limit=5,
    )


def _community(
    conn: psycopg.Connection[Any],
    doc_id: str,
    names: tuple[str, str],
    *,
    summary: str,
    weight: int,
) -> str:
    """A two-member community active through one edge contributed by ``doc_id``."""
    ids = [str(uuid.uuid4()), str(uuid.uuid4())]
    for entity_id, name in zip(ids, names, strict=True):
        conn.execute(
            "INSERT INTO graph_entities (id, tenant_id, entity_type, name, canonical_key) "
            "VALUES (%s, 'default', 'topic', %s, %s)",
            (entity_id, name, name.lower()),
        )
        conn.execute(
            "INSERT INTO graph_entity_mentions (tenant_id, entity_id, document_id, source) "
            "VALUES ('default', %s, %s, 'people')",
            (entity_id, doc_id),
        )
    key = str(uuid.uuid4())
    conn.execute(
        "INSERT INTO graph_communities "
        "(tenant_id, community_key, source_graph_hash, members_hash, member_count, summary) "
        "VALUES ('default', %s, %s, %s, 2, %s)",
        (key, f"h-{key}", f"mh-{key}", summary),
    )
    for rank, entity_id in enumerate(ids):
        conn.execute(
            "INSERT INTO graph_community_members "
            "(tenant_id, community_key, entity_id, member_rank, member_weight) "
            "VALUES ('default', %s, %s, %s, 1.0)",
            (key, entity_id, rank),
        )
    src, dst = sorted(ids)
    conn.execute(
        "INSERT INTO graph_edge_contributions "
        "(tenant_id, document_id, src_id, dst_id, cooccur_count) "
        "VALUES ('default', %s, %s, %s, %s)",
        (doc_id, src, dst, weight),
    )
    return key


@pytest.fixture
def communities(
    test_db: psycopg.Connection[Any], seed_doc: Callable[..., str]
) -> dict[str, str]:
    """C1 is active only through a confidential document, and outweighs C2."""
    secret_doc = seed_doc(title="Synthetic confidential mail", content="secret body")
    public_doc = seed_doc(title="Synthetic roadmap call", content="public body")
    test_db.execute(
        "UPDATE documents SET sensitivity = 'confidential' WHERE id = %s", (secret_doc,)
    )
    for doc_id in (secret_doc, public_doc):
        test_db.execute(
            "UPDATE documents SET ingested_at = %s WHERE id = %s", (IN_WINDOW, doc_id)
        )
    secret = _community(
        test_db, secret_doc, (SECRET_NAME, "Ann Vale"), summary=SECRET_SUMMARY, weight=9
    )
    public = _community(
        test_db, public_doc, (PUBLIC_NAME, "Ben Lark"), summary=PUBLIC_SUMMARY, weight=2
    )
    return {"secret": secret, "public": public}


def _active(conn: psycopg.Connection[Any], *, exclude: bool, limit: int = 10) -> list[str]:
    return [
        key
        for key, _weight in weekly_active_communities(
            conn, tenant_id="default", after=WIN_AFTER, before=WIN_BEFORE,
            theme_limit=limit, exclude_confidential=exclude,
        )
    ]


def test_a_community_with_a_confidential_member_is_not_active_under_the_lens(
    test_db: psycopg.Connection[Any], communities: dict[str, str]
) -> None:
    gated = _active(test_db, exclude=True)
    everything = _active(test_db, exclude=False)

    assert gated == [communities["public"]]
    assert everything == [communities["secret"], communities["public"]]


def test_the_exclusion_runs_before_the_limit(
    test_db: psycopg.Connection[Any], communities: dict[str, str]
) -> None:
    # C1 outweighs C2, so a cut after the filter would return nothing.
    assert _active(test_db, exclude=True, limit=1) == [communities["public"]]


def _report(conn: psycopg.Connection[Any], *, exclude: bool) -> Any:
    return build_weekly_report(
        conn, _cfg(), week=WEEK, generated_on=GENERATED_ON, no_graph=False,
        exclude_confidential=exclude,
    )


@pytest.mark.usefixtures("communities")
def test_the_gated_report_and_its_page_name_no_confidential_community(
    test_db: psycopg.Connection[Any],
) -> None:
    report = _report(test_db, exclude=True)
    page = render_weekly_md(report)

    assert report.graph_used is True
    assert [theme.synthesis for theme in report.themes] == [PUBLIC_SUMMARY]
    assert PUBLIC_NAME in page and PUBLIC_SUMMARY in page
    for text in (SECRET_NAME, SECRET_SUMMARY, "Ann Vale"):
        assert text not in page
        assert text not in json.dumps([theme.entity_names for theme in report.themes])


@pytest.mark.usefixtures("communities")
def test_the_permissive_report_still_carries_both(test_db: psycopg.Connection[Any]) -> None:
    report = _report(test_db, exclude=False)

    assert [theme.synthesis for theme in report.themes] == [SECRET_SUMMARY, PUBLIC_SUMMARY]


@pytest.mark.usefixtures("communities")
@pytest.mark.parametrize("include", [False, True])
def test_brain_review_weekly_never_emits_a_confidential_community(
    monkeypatch: pytest.MonkeyPatch, fake_embedder: object, tmp_path: Path, include: bool
) -> None:
    state = mcp_server._State(
        cfg=Config(
            database_url=TEST_DATABASE_URL, vault_path=tmp_path, graph_enabled=True,
            graph_tenant_id="default", review_theme_limit=5,
        ),
        embedder=fake_embedder,  # type: ignore[arg-type]
    )
    monkeypatch.setattr(mcp_server, "_state", state)

    payload = mcp_server.brain_review_weekly(
        week=WEEK, emit=True, include_confidential=include
    )

    page = (tmp_path / "reviews" / f"{WEEK}.md").read_text(encoding="utf-8")
    blob = json.dumps(payload, ensure_ascii=False)
    assert PUBLIC_SUMMARY in page and PUBLIC_SUMMARY in blob
    assert SECRET_SUMMARY not in page and SECRET_NAME not in page
    # The caller who opted in still gets the whole retrospective; the page never.
    assert (SECRET_SUMMARY in blob) is include


def test_the_shared_community_predicate_refuses_a_non_identifier_alias() -> None:
    with pytest.raises(ValueError, match="plain identifier"):
        confidential_member_sql("c; DROP TABLE documents")
    assert "c.community_key" in confidential_member_sql("c")
