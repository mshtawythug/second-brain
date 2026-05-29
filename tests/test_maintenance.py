"""Unit + integration tests for the brain-rebuild full-corpus orchestrator."""
from __future__ import annotations

from collections.abc import Generator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

import psycopg
import pytest

from brain import maintenance as m
from brain.config import Config


def test_build_stages_canonical_order_and_ids() -> None:
    stages = m.build_stages(vault_path=Path("/tmp/vault"), keep=3)
    assert [s.stage_id for s in stages] == [
        "embeddings", "summaries", "search",
        "graph", "graph-weights", "communities", "wiki",
    ]
    assert stages[0].steps[0].argv == ("brain", "reembed")
    assert stages[0].steps[0].fatal is True
    assert stages[1].steps[0].argv == ("brain", "enrich", "--backfill")
    assert stages[3].steps[0].argv == ("brain", "graphrag", "build", "--backfill")
    assert stages[4].steps[0].argv == ("brain", "graphrag", "refresh")
    assert stages[5].steps[0].argv == ("brain", "graphrag", "communities", "refresh")


# ---------------------------------------------------------------------------
# Task 2: Stage selection
# ---------------------------------------------------------------------------


def _ids(stages: list[m.Stage]) -> list[str]:
    return [s.stage_id for s in stages]


def _stages() -> list[m.Stage]:
    return m.build_stages(vault_path=Path("/tmp/v"), keep=3)


def test_select_default_runs_all() -> None:
    assert _ids(m.select_stages(_stages(), only=None, skip=None, wiki_only=False)) == list(
        m.ALL_STAGE_IDS
    )


def test_select_only_subset_preserves_registry_order() -> None:
    got = m.select_stages(_stages(), only=["communities", "graph"], skip=None, wiki_only=False)
    assert _ids(got) == ["graph", "communities"]


def test_select_skip_removes() -> None:
    got = m.select_stages(_stages(), only=None, skip=["summaries", "wiki"], wiki_only=False)
    assert _ids(got) == ["embeddings", "search", "graph", "graph-weights", "communities"]


def test_select_wiki_only() -> None:
    assert _ids(m.select_stages(_stages(), only=None, skip=None, wiki_only=True)) == ["wiki"]


def test_only_and_skip_mutually_exclusive() -> None:
    with pytest.raises(m.SelectionError):
        m.select_stages(_stages(), only=["graph"], skip=["wiki"], wiki_only=False)


def test_wiki_only_with_only_errors() -> None:
    with pytest.raises(m.SelectionError):
        m.select_stages(_stages(), only=["graph"], skip=None, wiki_only=True)


def test_unknown_stage_id_errors_with_valid_list() -> None:
    with pytest.raises(m.SelectionError) as exc:
        m.select_stages(_stages(), only=["bogus"], skip=None, wiki_only=False)
    assert "bogus" in str(exc.value)
    assert "embeddings" in str(exc.value)


# ---------------------------------------------------------------------------
# Task 3: In-flight ingest guard
# ---------------------------------------------------------------------------


def test_ingest_in_flight_detects_ingest() -> None:
    procs = [
        "/usr/bin/python /x/bin/brain ingest-stdin --source krisp --title foo",
        "/usr/bin/python -m brain.wiki.build_watcher --vault /v --keep 3",
    ]
    assert m.ingest_in_flight(procs) is True


def test_ingest_in_flight_ignores_watchers_and_search() -> None:
    procs = [
        "/usr/bin/python /x/bin/brain vault sync --watch --vault /v",
        "/usr/bin/python -m brain.wiki.build_watcher --vault /v --keep 3",
        "/usr/bin/python /x/bin/brain search 'ingest pipeline'",
    ]
    assert m.ingest_in_flight(procs) is False


def test_ingest_in_flight_matches_all_ingest_variants() -> None:
    for cmd in ("ingest", "ingest-dir", "ingest-stdin", "ingest-gmail"):
        assert m.ingest_in_flight(
            [f"/usr/bin/python /x/bin/brain {cmd} /some/path"]
        ) is True, cmd


def test_ingest_in_flight_ignores_ingest_inside_quoted_args() -> None:
    # False-positive guard: the word "ingest" appearing inside a quoted argument
    # (e.g. a search query) must not trigger the guard.
    assert m.ingest_in_flight(
        ["/usr/bin/python /x/bin/brain search 'brain ingest foo'"]
    ) is False


def test_ingest_in_flight_ignores_ingest_as_bare_args_to_other_subcommand() -> None:
    # False-positive guard: bare `brain ingest` tokens that are positional args
    # to a different sub-command must not trigger the guard.
    assert m.ingest_in_flight(["/x/bin/brain search brain ingest foo"]) is False


# ---------------------------------------------------------------------------
# Task 4: Advisory lock (integration — requires test Postgres on port 5434)
# ---------------------------------------------------------------------------


@pytest.mark.integration
def test_rebuild_lock_is_exclusive() -> None:
    # _force_test_database_url (autouse session fixture) ensures DATABASE_URL
    # is the test DB (port 5434), so Config.load() is safe here.
    url = Config.load().database_url
    with m.rebuild_lock(url):  # noqa: SIM117 — nesting is intentional: outer holds the lock
        with pytest.raises(m.RebuildLockHeld):
            with m.rebuild_lock(url):
                pass
    # After the outer block exits the lock is released; re-acquisition must succeed.
    with m.rebuild_lock(url):
        pass


# ---------------------------------------------------------------------------
# Task 5: Stage runner (fail-fast)
# ---------------------------------------------------------------------------


def test_run_stages_fail_fast_stops_after_first_fatal() -> None:
    selected = m.select_stages(
        _stages(), only=["embeddings", "summaries", "search"], skip=None, wiki_only=False
    )
    calls: list[tuple[str, ...]] = []

    def fake_run(argv: tuple[str, ...], env: dict[str, str] | None = None) -> int:
        calls.append(tuple(argv))
        return 7 if tuple(argv[:2]) == ("brain", "enrich") else 0

    with pytest.raises(m.StageFailed) as exc:
        m.run_stages(selected, runner=fake_run, clean_cache=False, vault_path=Path("/tmp/v"))
    assert exc.value.stage_id == "summaries"
    assert exc.value.exit_code == 7
    assert ("brain", "reembed") in calls
    assert ("brain", "enrich", "--backfill") in calls
    assert ("brain", "backfill", "search") not in calls


def test_run_stages_nonfatal_step_continues_to_build_swap() -> None:
    selected = m.select_stages(_stages(), only=["wiki"], skip=None, wiki_only=False)
    calls: list[tuple[str, ...]] = []

    def fake_run(argv: tuple[str, ...], env: dict[str, str] | None = None) -> int:
        calls.append(tuple(argv))
        return 1 if "sync-summaries" in argv else 0

    m.run_stages(selected, runner=fake_run, clean_cache=False, vault_path=Path("/tmp/v"))
    assert any("build_swap" in a for c in calls for a in c)


def test_run_stages_clean_cache_removes_only_parser_cache(tmp_path: Path) -> None:
    """``--clean-cache`` wipes ``<vault>/.quartz/.cache/parser`` and nothing else.

    Creates the parser cache dir plus a sibling dir under ``.quartz/.cache/``,
    then runs the wiki stage with ``clean_cache=True`` and a no-op fake runner.
    Asserts that the parser cache is gone and the sibling is untouched —
    proving the ``shutil.rmtree`` target is exactly ``_PARSER_CACHE_RELPATH``.
    """
    vault = tmp_path / "vault"
    parser = vault / ".quartz" / ".cache" / "parser"
    sibling = vault / ".quartz" / ".cache" / "other"
    parser.mkdir(parents=True)
    (parser / "x.json").write_text("{}", encoding="utf-8")
    sibling.mkdir(parents=True)
    (sibling / "keep.txt").write_text("keep", encoding="utf-8")

    wiki = [s for s in m.build_stages(vault_path=vault, keep=3) if s.stage_id == "wiki"]
    m.run_stages(
        wiki,
        runner=lambda argv, env=None: 0,
        clean_cache=True,
        vault_path=vault,
    )
    assert not parser.exists(), "parser cache dir must be wiped by --clean-cache"
    assert sibling.exists(), "sibling cache dir must be untouched by --clean-cache"
    assert (sibling / "keep.txt").exists(), "sibling file must survive --clean-cache"


# ---------------------------------------------------------------------------
# Task 6: main(argv) — argparse entry, dry-run, guard + lock wiring
# ---------------------------------------------------------------------------


@contextmanager
def _noop_lock(_url: str) -> Generator[None, None, None]:
    yield


def _base_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("BRAIN_VAULT_PATH", "/tmp/v")
    monkeypatch.setenv("DATABASE_URL", "postgresql://unused/db")


def test_main_dry_run_prints_plan_runs_nothing(
    capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    _base_env(monkeypatch)
    # If ingest_in_flight or run_stages are called during --dry-run the test fails.
    monkeypatch.setattr(
        m,
        "ingest_in_flight",
        lambda *a, **k: (_ for _ in ()).throw(AssertionError("guard called")),
    )
    monkeypatch.setattr(
        m,
        "run_stages",
        lambda *a, **k: (_ for _ in ()).throw(AssertionError("ran")),
    )
    code = m.main(["--dry-run"])
    out = capsys.readouterr().out
    assert code == 0
    assert "embeddings" in out and "wiki" in out


def test_main_refuses_when_ingest_in_flight(monkeypatch: pytest.MonkeyPatch) -> None:
    _base_env(monkeypatch)
    monkeypatch.setattr(m, "ingest_in_flight", lambda *a, **k: True)
    assert m.main([]) == 3


def test_main_force_bypasses_ingest_guard(monkeypatch: pytest.MonkeyPatch) -> None:
    _base_env(monkeypatch)
    monkeypatch.setattr(m, "ingest_in_flight", lambda *a, **k: True)
    monkeypatch.setattr(m, "rebuild_lock", _noop_lock)
    ran: list[int] = []
    monkeypatch.setattr(m, "run_stages", lambda *a, **k: ran.append(1))
    assert m.main(["--force", "--wiki-only"]) == 0
    assert ran == [1]


def test_main_lock_held_returns_4(monkeypatch: pytest.MonkeyPatch) -> None:
    _base_env(monkeypatch)
    monkeypatch.setattr(m, "ingest_in_flight", lambda *a, **k: False)

    @contextmanager
    def boom(_url: str) -> Generator[None, None, None]:
        raise m.RebuildLockHeld("busy")
        yield  # pragma: no cover

    monkeypatch.setattr(m, "rebuild_lock", boom)
    assert m.main(["--wiki-only"]) == 4


def test_main_stage_failure_returns_1(monkeypatch: pytest.MonkeyPatch) -> None:
    _base_env(monkeypatch)
    monkeypatch.setattr(m, "ingest_in_flight", lambda *a, **k: False)
    monkeypatch.setattr(m, "rebuild_lock", _noop_lock)

    def boom(*a: object, **k: object) -> None:
        raise m.StageFailed("graph", 5)

    monkeypatch.setattr(m, "run_stages", boom)
    assert m.main([]) == 1


def test_main_bad_only_returns_2(monkeypatch: pytest.MonkeyPatch) -> None:
    _base_env(monkeypatch)
    assert m.main(["--only", "bogus"]) == 2


# ---------------------------------------------------------------------------
# Task 8: Regression — full run leaves communities fingerprint current
# ---------------------------------------------------------------------------


@pytest.mark.integration
def test_full_run_communities_leaves_fingerprint_current(
    test_db: psycopg.Connection[Any],
) -> None:
    """After the communities stage runs, the stored fingerprint equals the live hash.

    Seeds a minimal triangle graph (3 synthetic entities, 3 edges — exactly the
    default ``min_size=3``) on the test DB, then invokes
    ``m.main(["--only", "communities", "--force"])`` which spawns
    ``brain graphrag communities refresh`` as a subprocess against the same test
    DB (via the session-scoped ``DATABASE_URL=TEST_DATABASE_URL`` fixture).

    After the subprocess returns, verifies that the set of distinct
    ``source_graph_hash`` values stored in ``graph_communities`` is exactly
    ``{current_hash}`` — the invariant that ``brain doctor`` checks under its
    ``communities current`` line.

    Requires the Apache AGE test container (port 5434,
    ``docker-compose.age-test.yml``).  If AGE is unavailable,
    ``brain graphrag communities refresh`` exits 1, ``m.main`` returns 1, and
    the ``assert m.main(...) == 0`` line fails with an explicit ENV-ERROR.
    No PII: all entity names and keys are synthetic.
    """
    from brain.cli import _relationship_edges, _stored_community_fingerprints
    from brain.db import connect
    from brain.graph_rag.communities import compute_source_graph_hash
    from brain.graph_rag.tenancy import resolve_tenant

    # Seed a one-triangle graph: 3 entities (canonical keys maint-key-{0,1,2}),
    # 3 CO_OCCURS edges at weight 0.8.  Exactly min_size=3 → one community.
    # test_db uses autocommit=True, so data is visible to the subprocess immediately.
    tenant = "default"
    entity_ids: list[str] = []
    for i in range(3):
        row = test_db.execute(
            "INSERT INTO graph_entities (tenant_id, entity_type, name, canonical_key) "
            "VALUES (%s, 'person', %s, %s) RETURNING id::text",
            (tenant, f"maint-person-{i}", f"maint-key-{i}"),
        ).fetchone()
        assert row is not None
        entity_ids.append(str(row[0]))
    for a, b in [(0, 1), (0, 2), (1, 2)]:
        src, dst = sorted((entity_ids[a], entity_ids[b]))
        test_db.execute(
            "INSERT INTO graph_relationships "
            "(tenant_id, src_id, dst_id, rel_type, weight, co_count, doc_count) "
            "VALUES (%s, %s, %s, 'co_occurs', %s, 1, 1)",
            (tenant, src, dst, 0.8),
        )

    cfg = Config.load()
    resolved_tenant = resolve_tenant(cfg)

    # m.main spawns `brain graphrag communities refresh` as a subprocess.
    # --force bypasses the ingest guard (ingest_in_flight check) so the test
    # does not depend on the process table.  The communities refresh command
    # itself always forces (bypass dirty gate) since it uses the "refresh"
    # subcommand — the dirty gate is a no-op when force=True.
    result = m.main(["--only", "communities", "--force"])
    assert result == 0, (
        f"m.main returned {result} — likely AGE container unavailable (ENV-ERROR) "
        "or a logic bug in communities refresh.  Run `docker compose "
        "-f docker-compose.age-test.yml up -d --build` and retry."
    )

    with connect(cfg.database_url) as conn:
        current = compute_source_graph_hash(_relationship_edges(conn, resolved_tenant))
        stored = _stored_community_fingerprints(conn, resolved_tenant)
    assert stored == {current}, (
        f"Fingerprint mismatch: stored={stored!r} current={current!r}.  "
        "communities refresh did not stamp communities with the current graph hash."
    )
