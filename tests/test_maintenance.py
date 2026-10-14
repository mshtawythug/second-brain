"""Unit + integration tests for the brain-rebuild full-corpus orchestrator."""
from __future__ import annotations

import sys
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
        "graph", "graph-weights", "communities", "connect", "mirror", "quartz",
    ]
    assert stages[0].steps[0].argv == m.brain_argv("reembed")
    assert stages[0].steps[0].fatal is True
    assert stages[1].steps[0].argv == m.brain_argv("enrich", "--backfill")
    assert stages[3].steps[0].argv == m.brain_argv("graphrag", "build", "--backfill")
    assert stages[4].steps[0].argv == m.brain_argv("graphrag", "refresh")
    assert stages[5].steps[0].argv == m.brain_argv("graphrag", "communities", "refresh")
    # Plan 07 — connect stage runs after communities, non-fatal (enhancement).
    assert stages[6].steps[0].argv == m.brain_argv("connect", "refresh")
    assert stages[6].steps[0].fatal is False


# ---------------------------------------------------------------------------
# Regression: every stage must invoke the CLI via the *running* interpreter.
#
# Bug: every Step used a bare ``"brain"`` as argv[0], which requires the console
# script's directory to be on PATH. CI installs into a repo-local ``.venv`` and
# runs ``.venv/bin/pytest`` without activating it, so PATH has no ``brain`` and
# the communities stage died with ``FileNotFoundError: 'brain'``. On a developer
# machine that *does* have a ``brain`` on PATH (a pipx / uv-tool install) the
# bare name resolved to that unrelated installation instead, so the test passed
# locally while exercising an entirely different copy of the code.
# ---------------------------------------------------------------------------


def test_brain_argv_targets_running_interpreter_not_path() -> None:
    """``brain_argv`` builds ``<sys.executable> -m brain ...`` — no PATH lookup."""
    assert m.brain_argv("doctor") == (sys.executable, "-m", "brain", "doctor")
    # argv[0] must be an existing absolute interpreter path, never a bare name
    # that the OS would have to resolve against PATH.
    argv0 = m.brain_argv("doctor")[0]
    assert Path(argv0).is_absolute(), argv0
    assert Path(argv0).exists(), argv0


def test_no_stage_step_depends_on_a_bare_brain_on_path() -> None:
    """No Step may use a bare ``brain`` argv[0]; that is the CI FileNotFoundError.

    Guards every stage, not just the communities one that happened to fail, so a
    newly added stage cannot silently reintroduce the PATH dependency.
    """
    stages = m.build_stages(vault_path=Path("/tmp/vault"), keep=3)
    steps = [step for stage in stages for step in stage.steps]
    assert steps, "build_stages returned no steps — this guard would never fire"
    offenders = [s.argv for s in steps if s.argv[0] == "brain"]
    assert not offenders, f"steps still shell out to a bare `brain`: {offenders}"
    # Positive half: every step really does start with the running interpreter,
    # so the assertion above is not passing merely because argv[0] is some other
    # unresolvable string.
    assert all(s.argv[0] == sys.executable for s in steps), [s.argv[0] for s in steps]


# ---------------------------------------------------------------------------
# Task 2: Stage selection
# ---------------------------------------------------------------------------


def _ids(stages: list[m.Stage]) -> list[str]:
    return [s.stage_id for s in stages]


def _stages() -> list[m.Stage]:
    return m.build_stages(vault_path=Path("/tmp/v"), keep=3)


def test_select_default_runs_every_stage_except_the_opt_in_quartz() -> None:
    """E1: the default plan is every stage but ``quartz``, and ``mirror`` is in it."""
    got = _ids(m.select_stages(_stages(), only=None, skip=None))
    assert got == [
        "embeddings", "summaries", "search", "graph", "graph-weights", "communities",
        "connect", "mirror",
    ]
    assert got == [i for i in m.ALL_STAGE_IDS if i != "quartz"]


def test_select_only_subset_preserves_registry_order() -> None:
    got = m.select_stages(_stages(), only=["communities", "graph"], skip=None)
    assert _ids(got) == ["graph", "communities"]


def test_select_skip_removes() -> None:
    got = m.select_stages(_stages(), only=None, skip=["summaries", "mirror"])
    assert _ids(got) == [
        "embeddings", "search", "graph", "graph-weights", "communities", "connect",
    ]


def test_select_skip_connect_stage() -> None:
    # Plan 07 — `--skip connect` drops just the auto-link suggestion stage.
    got = m.select_stages(_stages(), only=None, skip=["connect"])
    assert "connect" not in _ids(got)
    assert "mirror" in _ids(got)


def test_select_skip_never_brings_in_the_opt_in_quartz_stage() -> None:
    """E1: ``--skip`` subtracts from the DEFAULT plan, so ``quartz`` stays out."""
    assert "quartz" not in _ids(m.select_stages(_stages(), only=None, skip=["graph"]))
    assert "quartz" not in _ids(m.select_stages(_stages(), only=None, skip=["quartz"]))


def test_select_only_quartz_requests_the_quartz_build() -> None:
    """E4: naming ``quartz`` is the only way to select it."""
    assert _ids(m.select_stages(_stages(), only=["quartz"], skip=None)) == ["quartz"]


def test_select_only_mirror_and_quartz_keeps_registry_order() -> None:
    """E1: the vault mirror is exported before the Quartz build reads it."""
    got = m.select_stages(_stages(), only=["quartz", "mirror"], skip=None)
    assert _ids(got) == ["mirror", "quartz"]


def test_only_and_skip_mutually_exclusive() -> None:
    with pytest.raises(m.SelectionError):
        m.select_stages(_stages(), only=["graph"], skip=["mirror"])


def test_wiki_is_no_longer_a_stage_id() -> None:
    """A10: the ``wiki`` stage is re-formed, so its old id is unknown."""
    with pytest.raises(m.SelectionError) as exc:
        m.select_stages(_stages(), only=["wiki"], skip=None)
    assert "mirror" in str(exc.value) and "quartz" in str(exc.value)


def test_unknown_stage_id_errors_with_valid_list() -> None:
    with pytest.raises(m.SelectionError) as exc:
        m.select_stages(_stages(), only=["bogus"], skip=None)
    assert "bogus" in str(exc.value)
    assert "embeddings" in str(exc.value)


def _stage(stage_id: str, vault: Path = Path("/tmp/v")) -> m.Stage:
    (stage,) = [s for s in m.build_stages(vault_path=vault, keep=3) if s.stage_id == stage_id]
    return stage


def test_mirror_stage_is_exactly_the_three_vault_steps() -> None:
    """E2 / spec §9.4 A10: export → sync-summaries → prune-orphans, nothing Quartz.

    ``prune-orphans`` is a DELETER that lives or dies with the People Hub
    emitter (§9.4), so its exact argv and ``fatal=False`` are pinned here, not
    derived from ``brain_argv``.
    """
    py = sys.executable
    got = [(s.argv, s.fatal, s.env) for s in _stage("mirror").steps]
    assert got == [
        ((py, "-m", "brain", "vault", "export", "--to", "/tmp/v"), True, ()),
        ((py, "-m", "brain", "vault", "sync-summaries", "--vault", "/tmp/v"), False, ()),
        (
            (
                py, "-m", "brain", "vault", "prune-orphans", "--apply", "--include-stale",
                "--vault", "/tmp/v",
            ),
            False,
            (),
        ),
    ]


def test_quartz_stage_is_the_overlay_then_build_swap() -> None:
    """E4: the two Quartz steps A10 removes from the default plan, unchanged."""
    py = sys.executable
    got = [(s.argv, s.fatal, s.env) for s in _stage("quartz").steps]
    assert got == [
        (
            (py, "-m", "brain", "vault", "render", "--overlay", "--no-build", "--vault", "/tmp/v"),
            False,
            (),
        ),
        (
            (py, "-m", "brain.wiki.build_swap", "--vault", "/tmp/v", "--keep", "3"),
            True,
            (("BRAIN_WIKI_RELOAD", "1"),),
        ),
    ]


def test_default_plan_spawns_no_quartz_step() -> None:
    """E1: running the default selection never spawns ``vault render`` or ``build_swap``."""
    calls: list[tuple[str, ...]] = []

    def fake_run(argv: tuple[str, ...], env: dict[str, str] | None = None) -> int:
        calls.append(tuple(argv))
        return 0

    default = m.select_stages(_stages(), only=None, skip=None)
    m.run_stages(default, runner=fake_run, clean_cache=False, vault_path=Path("/tmp/v"))
    assert calls, "the default plan ran nothing — this guard would never fire"
    assert not [c for c in calls if "render" in c or "brain.wiki.build_swap" in c], calls
    assert (sys.executable, "-m", "brain", "vault", "prune-orphans", "--apply",
            "--include-stale", "--vault", "/tmp/v") in calls


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
    selected = m.select_stages(_stages(), only=["embeddings", "summaries", "search"], skip=None)
    calls: list[tuple[str, ...]] = []

    def fake_run(argv: tuple[str, ...], env: dict[str, str] | None = None) -> int:
        calls.append(tuple(argv))
        return 7 if tuple(argv) == m.brain_argv("enrich", "--backfill") else 0

    with pytest.raises(m.StageFailed) as exc:
        m.run_stages(selected, runner=fake_run, clean_cache=False, vault_path=Path("/tmp/v"))
    assert exc.value.stage_id == "summaries"
    assert exc.value.exit_code == 7
    assert m.brain_argv("reembed") in calls
    assert m.brain_argv("enrich", "--backfill") in calls
    assert m.brain_argv("backfill", "search") not in calls


def test_run_stages_nonfatal_step_continues_to_prune_orphans() -> None:
    selected = m.select_stages(_stages(), only=["mirror"], skip=None)
    calls: list[tuple[str, ...]] = []

    def fake_run(argv: tuple[str, ...], env: dict[str, str] | None = None) -> int:
        calls.append(tuple(argv))
        return 1 if "sync-summaries" in argv else 0

    m.run_stages(selected, runner=fake_run, clean_cache=False, vault_path=Path("/tmp/v"))
    assert any("prune-orphans" in c for c in calls)


def test_run_stages_quartz_build_swap_gets_wiki_reload() -> None:
    """E4: ``--only quartz`` runs build_swap with ``BRAIN_WIKI_RELOAD=1``."""
    seen: list[tuple[tuple[str, ...], dict[str, str] | None]] = []

    def fake_run(argv: tuple[str, ...], env: dict[str, str] | None = None) -> int:
        seen.append((tuple(argv), env))
        return 0

    selected = m.select_stages(_stages(), only=["quartz"], skip=None)
    m.run_stages(selected, runner=fake_run, clean_cache=False, vault_path=Path("/tmp/v"))
    swaps = [env for argv, env in seen if "brain.wiki.build_swap" in argv]
    assert swaps == [{"BRAIN_WIKI_RELOAD": "1"}]


def test_run_stages_clean_cache_removes_only_parser_cache(tmp_path: Path) -> None:
    """``--clean-cache`` wipes ``<vault>/.quartz/.cache/parser`` and nothing else.

    Creates the parser cache dir plus a sibling dir under ``.quartz/.cache/``,
    then runs the quartz stage with ``clean_cache=True`` and a no-op fake runner.
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

    m.run_stages(
        [_stage("quartz", vault)],
        runner=lambda argv, env=None: 0,
        clean_cache=True,
        vault_path=vault,
    )
    assert not parser.exists(), "parser cache dir must be wiped by --clean-cache"
    assert sibling.exists(), "sibling cache dir must be untouched by --clean-cache"
    assert (sibling / "keep.txt").exists(), "sibling file must survive --clean-cache"


def test_run_stages_clean_cache_leaves_the_parser_cache_for_mirror(tmp_path: Path) -> None:
    """The wipe belongs to the Quartz build; the mirror stage never touches it."""
    vault = tmp_path / "vault"
    parser = vault / ".quartz" / ".cache" / "parser"
    parser.mkdir(parents=True)
    m.run_stages(
        [_stage("mirror", vault)], runner=lambda argv, env=None: 0, clean_cache=True,
        vault_path=vault,
    )
    assert parser.exists()


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
    assert "embeddings" in out and "mirror" in out
    assert "quartz" not in out.lower(), "E1: the default plan names no Quartz stage"


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
    assert m.main(["--force", "--only", "mirror"]) == 0
    assert ran == [1]


def test_main_lock_held_returns_4(monkeypatch: pytest.MonkeyPatch) -> None:
    _base_env(monkeypatch)
    monkeypatch.setattr(m, "ingest_in_flight", lambda *a, **k: False)

    @contextmanager
    def boom(_url: str) -> Generator[None, None, None]:
        raise m.RebuildLockHeld("busy")
        yield  # pragma: no cover

    monkeypatch.setattr(m, "rebuild_lock", boom)
    assert m.main(["--only", "mirror"]) == 4


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


def _forbid_running(monkeypatch: pytest.MonkeyPatch) -> None:
    def boom(*a: object, **k: object) -> None:
        raise AssertionError("must exit before running anything")

    monkeypatch.setattr(m, "ingest_in_flight", boom)
    monkeypatch.setattr(m, "rebuild_lock", boom)
    monkeypatch.setattr(m, "run_stages", boom)


@pytest.mark.parametrize(
    "argv", [["--wiki-only"], ["--wiki-only", "--force"], ["--wiki-only", "--only", "graph"]]
)
def test_main_wiki_only_exits_2_naming_the_replacements(
    argv: list[str], capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """E3 / R8-3: ``--wiki-only`` is removed, not aliased, and says what to use."""
    _base_env(monkeypatch)
    _forbid_running(monkeypatch)
    assert m.main(argv) == 2
    err = capsys.readouterr().err
    assert "--wiki-only" in err
    assert "--only mirror" in err and "--only quartz" in err


@pytest.mark.parametrize("argv", [["--clean-cache"], ["--clean-cache", "--only", "mirror"]])
def test_main_clean_cache_without_quartz_is_a_usage_error(
    argv: list[str], capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """E4 / R8-3: the parser cache belongs to the Quartz build, so ask for that build."""
    _base_env(monkeypatch)
    _forbid_running(monkeypatch)
    assert m.main(argv) == 2
    assert "--only quartz" in capsys.readouterr().err


def test_main_only_quartz_with_clean_cache_runs_the_quartz_stage(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """E4: a requested Quartz build runs, and ``--clean-cache`` reaches the runner."""
    _base_env(monkeypatch)
    monkeypatch.setattr(m, "ingest_in_flight", lambda *a, **k: False)
    monkeypatch.setattr(m, "rebuild_lock", _noop_lock)
    seen: list[tuple[list[str], bool]] = []

    def capture(stages: list[m.Stage], **kwargs: Any) -> None:
        seen.append(([s.stage_id for s in stages], kwargs["clean_cache"]))

    monkeypatch.setattr(m, "run_stages", capture)
    assert m.main(["--only", "quartz", "--clean-cache"]) == 0
    assert seen == [(["quartz"], True)]


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
