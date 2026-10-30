"""The ``--entity`` resolver and its graph checks (RM-24), against the test DB on 5434.

All entities are synthetic. Each test seeds the rows it reads; nothing depends on a graph build.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any

import psycopg
import pytest

from brain.config import Config
from brain.entity_filter import (
    ALIAS_FILE_MESSAGE,
    GRAPH_REQUIRED_MESSAGE,
    graph_tables_present,
    resolve_entity_ids,
    search_kwargs,
)
from brain.errors import (
    EntityAmbiguous,
    EntityFilterError,
    EntityGraphUnavailable,
    EntityNotFound,
)
from brain.graph_rag.aliases import AliasRule
from tests.entity_filter_seed import (
    BAD_ALIAS_FILES,
    BAD_ALIAS_SENTINEL,
    OTHER_TENANT,
    TENANT,
    seed_entity,
)

_ALIASES_YAML = """\
rules:
  - from: {type: project, key: harbor}
    to:   {type: project, key: harborlight}
"""


def _resolve(conn: psycopg.Connection[Any], name: str, **kwargs: Any) -> list[str]:
    return resolve_entity_ids(
        conn,
        tenant_id=kwargs.pop("tenant_id", TENANT),
        name=name,
        entity_type=kwargs.pop("entity_type", None),
        **kwargs,
    )


def test_canonical_key_match_is_case_insensitive(test_db: psycopg.Connection[Any]) -> None:
    entity = seed_entity(test_db, name="Quillmoor", canonical_key="quillmoor")

    assert _resolve(test_db, "QUILLMOOR") == [entity]


def test_strip_all_concept_key_matches_a_spaced_name(test_db: psycopg.Connection[Any]) -> None:
    # concepts-v6 keys strip every separator; `lower(name)` alone would miss this row.
    entity = seed_entity(test_db, name="Harbor Light", canonical_key="harborlight")

    assert _resolve(test_db, "harbor-light") == [entity]


def test_name_match_is_the_fallback(test_db: psycopg.Connection[Any]) -> None:
    entity = seed_entity(
        test_db, name="Pat Morgan", canonical_key="pat@example.com", entity_type="person"
    )

    assert _resolve(test_db, "pat morgan") == [entity]


def test_name_match_does_not_run_when_a_key_matched(test_db: psycopg.Connection[Any]) -> None:
    by_key = seed_entity(test_db, name="Ferncastle", canonical_key="ferncastle")
    seed_entity(test_db, name="ferncastle", canonical_key="ferncastle-old", entity_type="topic")

    assert _resolve(test_db, "Ferncastle") == [by_key]


def test_alias_keeps_the_source_beside_its_target(
    test_db: psycopg.Connection[Any],
) -> None:
    # Rules are merged only at `graphrag build` / `refresh`; ingest and the watcher
    # apply none, so an unmerged source can still carry mentions of its own.
    source = seed_entity(test_db, name="Harbor", canonical_key="harbor")
    target = seed_entity(test_db, name="Harbor Light", canonical_key="harborlight")
    rules = [AliasRule("project", "harbor", "project", "harborlight")]

    assert _resolve(test_db, "Harbor", rules=rules) == sorted([source, target])


def test_alias_with_no_target_yet_resolves_to_the_source(
    test_db: psycopg.Connection[Any],
) -> None:
    # Regression (rev-91 a): the rule's target has not been created yet.
    source = seed_entity(test_db, name="Harbor", canonical_key="harbor")
    rules = [AliasRule("project", "harbor", "project", "harborlight")]

    assert _resolve(test_db, "Harbor", rules=rules) == [source]


def test_an_alias_target_in_another_tenant_is_not_followed(
    test_db: psycopg.Connection[Any],
) -> None:
    # aud-91: the alias-target lookup is tenant-scoped. The rule's target exists only
    # under OTHER_TENANT, so resolving under TENANT returns the source alone.
    source = seed_entity(test_db, name="Harbor", canonical_key="harbor")
    seed_entity(
        test_db, name="Harbor Light", canonical_key="harborlight", tenant_id=OTHER_TENANT
    )
    rules = [AliasRule("project", "harbor", "project", "harborlight")]

    assert _resolve(test_db, "Harbor", rules=rules) == [source]


def test_an_alias_target_in_both_tenants_resolves_to_this_tenants_row(
    test_db: psycopg.Connection[Any],
) -> None:
    source = seed_entity(test_db, name="Harbor", canonical_key="harbor")
    mine = seed_entity(test_db, name="Harbor Light", canonical_key="harborlight")
    theirs = seed_entity(
        test_db, name="Harbor Light", canonical_key="harborlight", tenant_id=OTHER_TENANT
    )
    rules = [AliasRule("project", "harbor", "project", "harborlight")]

    got = _resolve(test_db, "Harbor", rules=rules)

    assert got == sorted([source, mine])
    assert theirs not in got


def test_a_name_matched_alias_source_also_returns_its_target(
    test_db: psycopg.Connection[Any],
) -> None:
    # aud-91: neither key form of "Pat Morgan" ("pat morgan", "patmorgan") is any
    # entity's canonical_key, so only the display-name fallback finds the source, and
    # the fallback must still follow the source's alias rule to its target.
    source = seed_entity(
        test_db, name="Pat Morgan", canonical_key="pat@example.com", entity_type="person"
    )
    target = seed_entity(
        test_db, name="P. Morgan", canonical_key="pmorgan", entity_type="person"
    )
    rules = [AliasRule("person", "pat@example.com", "person", "pmorgan")]

    assert _resolve(test_db, "Pat Morgan", rules=rules) == sorted([source, target])


def test_a_cross_type_alias_is_not_ambiguous(test_db: psycopg.Connection[Any]) -> None:
    # Ambiguity is judged after the redirect: a topic source that a rule maps to a
    # person counts as that person, so one name of "two types" is one entity.
    source = seed_entity(test_db, name="Sam", canonical_key="sam", entity_type="topic")
    target = seed_entity(
        test_db, name="Sam Rivera", canonical_key="sam rivera", entity_type="person"
    )
    rules = [AliasRule("topic", "sam", "person", "sam rivera")]

    assert _resolve(test_db, "Sam", rules=rules) == sorted([source, target])


def test_a_cross_type_alias_with_an_absent_target_is_still_ambiguous(
    test_db: psycopg.Connection[Any],
) -> None:
    # aud-91 M20: a source counts as its target's type only when the target row was
    # found. With no `sam rivera` row the topic stays a topic, so a person of the
    # same name makes the match ambiguous.
    seed_entity(test_db, name="Sam", canonical_key="sam", entity_type="topic")
    seed_entity(test_db, name="Sam", canonical_key="sam", entity_type="person")
    rules = [AliasRule("topic", "sam", "person", "sam rivera")]

    with pytest.raises(EntityAmbiguous) as exc_info:
        _resolve(test_db, "Sam", rules=rules)

    assert "topic" in str(exc_info.value)
    assert "person" in str(exc_info.value)


def test_alias_redirects_a_merged_source_already_deleted(
    test_db: psycopg.Connection[Any],
) -> None:
    target = seed_entity(test_db, name="Harbor Light", canonical_key="harborlight")
    rules = [AliasRule("project", "harbor", "project", "harborlight")]

    assert _resolve(test_db, "harbor", rules=rules) == [target]


def test_resolution_is_tenant_scoped(test_db: psycopg.Connection[Any]) -> None:
    mine = seed_entity(test_db, name="Quillmoor", canonical_key="quillmoor")
    seed_entity(
        test_db, name="Quillmoor", canonical_key="quillmoor", tenant_id=OTHER_TENANT
    )

    assert _resolve(test_db, "Quillmoor") == [mine]


def test_not_found_points_at_the_entity_listing(test_db: psycopg.Connection[Any]) -> None:
    seed_entity(test_db, name="Quillmoor", canonical_key="quillmoor", tenant_id=OTHER_TENANT)

    with pytest.raises(EntityNotFound) as exc_info:
        _resolve(test_db, "Quillmoor")

    assert "brain graphrag entities" in str(exc_info.value)


def test_several_types_without_a_type_is_ambiguous(test_db: psycopg.Connection[Any]) -> None:
    seed_entity(test_db, name="Ember", canonical_key="ember", entity_type="tool")
    seed_entity(test_db, name="Ember", canonical_key="ember", entity_type="topic")

    with pytest.raises(EntityAmbiguous) as exc_info:
        _resolve(test_db, "Ember")

    assert "tool" in str(exc_info.value)
    assert "topic" in str(exc_info.value)
    assert "--entity-type" in str(exc_info.value)


def test_a_type_settles_the_ambiguity(test_db: psycopg.Connection[Any]) -> None:
    tool = seed_entity(test_db, name="Ember", canonical_key="ember", entity_type="tool")
    seed_entity(test_db, name="Ember", canonical_key="ember", entity_type="topic")

    assert _resolve(test_db, "Ember", entity_type="tool") == [tool]


def test_an_unknown_type_is_refused(test_db: psycopg.Connection[Any]) -> None:
    with pytest.raises(EntityFilterError) as exc_info:
        _resolve(test_db, "Ember", entity_type="planet")

    assert "--entity-type" in str(exc_info.value)


def test_a_blank_name_is_not_found(test_db: psycopg.Connection[Any]) -> None:
    with pytest.raises(EntityNotFound):
        _resolve(test_db, "   ")


# ---------------------------------------------------------------------------
# search_kwargs: the graph checks, the tenant, and the alias file
# ---------------------------------------------------------------------------


def _cfg(**kwargs: Any) -> Config:
    return Config(database_url="postgresql://unused", **kwargs)


def test_no_entity_means_no_filter(test_db: psycopg.Connection[Any]) -> None:
    assert search_kwargs(test_db, _cfg(graph_enabled=False), None, None) == {}


def test_a_type_without_an_entity_is_refused(test_db: psycopg.Connection[Any]) -> None:
    with pytest.raises(EntityFilterError) as exc_info:
        search_kwargs(test_db, _cfg(graph_enabled=True), None, "tool")

    assert "--entity-type" in str(exc_info.value)


def test_graph_off_raises_the_graph_message(test_db: psycopg.Connection[Any]) -> None:
    seed_entity(test_db, name="Quillmoor", canonical_key="quillmoor")

    with pytest.raises(EntityGraphUnavailable) as exc_info:
        search_kwargs(test_db, _cfg(graph_enabled=False), "Quillmoor", None)

    assert str(exc_info.value) == GRAPH_REQUIRED_MESSAGE


def test_absent_graph_tables_raise_the_graph_message(
    test_db: psycopg.Connection[Any],
) -> None:
    # A transaction rolled back at the end, so the shared schema is untouched.
    with pytest.raises(EntityGraphUnavailable), test_db.transaction(force_rollback=True):
        test_db.execute("DROP TABLE graph_entity_mentions CASCADE")
        assert graph_tables_present(test_db) is False
        search_kwargs(test_db, _cfg(graph_enabled=True), "Quillmoor", None)

    assert graph_tables_present(test_db) is True


def test_graph_on_returns_the_predicate_kwargs(test_db: psycopg.Connection[Any]) -> None:
    entity = seed_entity(test_db, name="Quillmoor", canonical_key="quillmoor")

    got = search_kwargs(test_db, _cfg(graph_enabled=True), "Quillmoor", None)

    assert got == {"entity_ids": [entity], "entity_tenant": TENANT}


def test_the_configured_tenant_is_used(test_db: psycopg.Connection[Any]) -> None:
    theirs = seed_entity(
        test_db, name="Quillmoor", canonical_key="quillmoor", tenant_id=OTHER_TENANT
    )
    cfg = _cfg(graph_enabled=True, graph_tenant_id=OTHER_TENANT)

    assert search_kwargs(test_db, cfg, "Quillmoor", None) == {
        "entity_ids": [theirs],
        "entity_tenant": OTHER_TENANT,
    }


def test_the_alias_file_is_read(test_db: psycopg.Connection[Any], tmp_path: Path) -> None:
    target = seed_entity(test_db, name="Harbor Light", canonical_key="harborlight")
    aliases = tmp_path / "graph_aliases.yml"
    aliases.write_text(_ALIASES_YAML, encoding="utf-8")
    cfg = _cfg(graph_enabled=True, graph_aliases_path=aliases)

    assert search_kwargs(test_db, cfg, "Harbor", None)["entity_ids"] == [target]


@pytest.mark.parametrize("body", BAD_ALIAS_FILES.values(), ids=BAD_ALIAS_FILES.keys())
def test_a_bad_alias_file_is_a_filter_error_that_quotes_nothing(
    test_db: psycopg.Connection[Any], tmp_path: Path, body: str
) -> None:
    # chk-91: search never read this file before RM-24, and the loader's errors
    # escaped as a traceback. A YAML error quotes the bad line, and an invalid
    # type is printed unredacted, so the message names the failure's class only.
    seed_entity(test_db, name="Quillmoor", canonical_key="quillmoor")
    aliases = tmp_path / "graph_aliases.yml"
    aliases.write_text(body, encoding="utf-8")
    cfg = _cfg(graph_enabled=True, graph_aliases_path=aliases)

    with pytest.raises(EntityFilterError) as exc_info:
        search_kwargs(test_db, cfg, "Quillmoor", None)

    assert str(exc_info.value).startswith(ALIAS_FILE_MESSAGE)
    assert BAD_ALIAS_SENTINEL not in str(exc_info.value)
