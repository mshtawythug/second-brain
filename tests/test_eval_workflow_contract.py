"""Contract tests for the ``eval`` CI workflow and its marker population.

The ``eval`` workflow (`.github/workflows/eval.yml`) runs ``pytest -m eval`` on a
runner that has Postgres but **no Ollama**. Its documented value is
import/collection regression coverage: every eval-marked gate that dials a live
model must SKIP cleanly when the model is unreachable, so the job is green
unless the harness itself breaks.

That clean skip depends on an invariant that is easy to violate silently, which
is exactly what happened in ``e084c79``:

``tests/conftest.py::_forbid_live_ollama`` is an autouse guard that raises
:class:`~tests.conftest.LiveOllamaForbidden` from ``socket.socket.connect`` for
the Ollama port. It derives from :class:`BaseException` **on purpose**, so that
the never-raise LLM surfaces cannot swallow it. The same property means it sails
straight through ``brain.chat._chat_once``'s
``except (httpx.ConnectError, ...) -> OllamaUnavailable`` translation — so an
eval gate's ``except OllamaUnavailable: pytest.skip(...)`` never fires and the
gate FAILS where it was designed to skip.

The guard documents its own escape hatch: ``@pytest.mark.live_ollama``. A gate
that genuinely dials a live model needs it. A gate that does *not* must keep the
socket ban, because that ban is the only thing stopping a supposedly hermetic
test from reaching the network. Those are opposite requirements, so this module
does not assert one blanket implication over the whole population — it pins an
explicit, hand-classified roster (:data:`LIVE_MODEL_EVAL_TESTS` /
:data:`HERMETIC_EVAL_TESTS`) and fails when a new eval test appears in neither.
Adding an eval test is then a deliberate two-line classification rather than a
standing instruction to strip a guard.

**Markers come from pytest, never from parsing source.** An earlier revision of
this module walked ``ast`` decorator lists, which recognised ``@pytest.mark.eval``
and nothing else — not ``pytestmark`` (this repo's dominant idiom, 23 modules),
not class-level marks, not ``from pytest import mark``. A test declared any of
those ways was invisible to the scanner, so the contract iterated past it and
passed. Re-deriving pytest's own resolution is the failure mode, so
:func:`_collected_eval_markers` asks pytest instead: one ``--collect-only -m eval``
subprocess with a probe plugin that reports ``item.iter_markers()`` per item.
That cannot drift from what pytest actually does, because it *is* what pytest
actually does.
"""
from __future__ import annotations

import functools
import json
import os
import re
import subprocess
import sys
import tempfile
from pathlib import Path

import pytest
import yaml

_REPO_ROOT = Path(__file__).resolve().parents[1]
_EVAL_WORKFLOW = _REPO_ROOT / ".github" / "workflows" / "eval.yml"
_CI_BASELINE = "tests/eval/baselines/ci.json"
_GOLDEN_CORPUS = "tests/eval/golden_corpus.yaml"
_COLLECT_TIMEOUT_SECONDS = 600

# Eval gates that really do reach a live model (chat or embedder). Each MUST
# carry ``live_ollama``, or `_forbid_live_ollama` turns its designed skip into a
# failure — the ``e084c79`` breakage.
LIVE_MODEL_EVAL_TESTS = frozenset(
    {
        "tests/test_eval_answer_harness.py::test_answer_eval_harness_meets_threshold",
        "tests/test_eval_harness_live.py::test_live_harness_runs_against_brain",
        "tests/test_graphrag_concept_gate.py::test_concept_extractor_gate",
        "tests/test_graphrag_concept_gate.py::test_concept_extractor_no_leakage_on_sparse_docs",
    }
)

# Eval gates that need the corpus but no model — e.g. a retrieval gate over a
# fake embedder. Each MUST NOT carry ``live_ollama``: keeping the socket ban is
# the only thing that stops it silently reaching the network. Empty today; this
# roster exists so that adding such a test is possible WITHOUT deleting its
# guard to satisfy a contract.
HERMETIC_EVAL_TESTS: frozenset[str] = frozenset()

# A collection plugin, materialised into a temp dir and loaded with ``-p``. It
# reports the markers pytest itself resolved for each collected item, which is
# the whole point: `pytestmark`, class-level marks and aliased imports are all
# already folded in by the time `iter_markers()` runs.
_PROBE_PLUGIN = '''\
"""Collection-time probe: dump {nodeid: [marker names]} for the selected items."""
import json
import os


def pytest_collection_finish(session):
    payload = {
        item.nodeid: sorted({mark.name for mark in item.iter_markers()})
        for item in session.items
    }
    with open(os.environ["EVAL_MARKER_PROBE_OUT"], "w", encoding="utf-8") as handle:
        json.dump(payload, handle)
'''


def _base_nodeid(nodeid: str) -> str:
    """``path::test[param]`` -> ``path::test`` (parametrisation-insensitive)."""
    return re.sub(r"\[.*\]$", "", nodeid)


@functools.lru_cache(maxsize=1)
def _collected_eval_markers() -> dict[str, frozenset[str]]:
    """``{base nodeid: markers}`` for everything pytest selects under ``-m eval``.

    Runs a real collection in a subprocess rather than re-implementing marker
    resolution. ``--no-cov`` because ``addopts`` carries a coverage floor a
    4-test collection cannot meet; ``-p no:cacheprovider`` so the child leaves
    no ``.pytest_cache`` behind; ``-m eval`` overrides the deselect expression
    ``addopts`` prepends.
    """
    with tempfile.TemporaryDirectory() as tmp:
        tmpdir = Path(tmp)
        (tmpdir / "eval_marker_probe.py").write_text(_PROBE_PLUGIN, encoding="utf-8")
        out = tmpdir / "markers.json"

        env = dict(os.environ)
        env["EVAL_MARKER_PROBE_OUT"] = str(out)
        env["PYTHONPATH"] = os.pathsep.join(
            [str(tmpdir), str(_REPO_ROOT), env.get("PYTHONPATH", "")]
        ).rstrip(os.pathsep)

        completed = subprocess.run(
            [
                sys.executable,
                "-m",
                "pytest",
                "--collect-only",
                "-q",
                "--no-cov",
                "-p",
                "no:cacheprovider",
                "-p",
                "eval_marker_probe",
                "-m",
                "eval",
            ],
            cwd=_REPO_ROOT,
            env=env,
            capture_output=True,
            text=True,
            timeout=_COLLECT_TIMEOUT_SECONDS,
            check=False,
        )
        assert completed.returncode == 0, (
            "`pytest --collect-only -m eval` failed, so this module cannot see the "
            f"real marker population (exit {completed.returncode}).\n"
            f"stdout:\n{completed.stdout}\nstderr:\n{completed.stderr}"
        )
        assert out.is_file(), (
            "the marker probe plugin never ran — `-p eval_marker_probe` did not "
            f"load.\nstdout:\n{completed.stdout}\nstderr:\n{completed.stderr}"
        )
        raw = json.loads(out.read_text(encoding="utf-8"))

    collected: dict[str, frozenset[str]] = {}
    for nodeid, markers in raw.items():
        base = _base_nodeid(nodeid)
        collected[base] = collected.get(base, frozenset()) | frozenset(markers)
    return collected


def test_eval_marker_population_is_not_empty() -> None:
    """Guard the guard: an empty population would make everything below vacuous."""
    assert _collected_eval_markers(), (
        "pytest collects no `eval`-marked tests — the eval workflow would be a "
        "green badge over zero coverage, and every assertion in this module "
        "would pass vacuously"
    )


def test_every_eval_test_is_classified_live_or_hermetic() -> None:
    """A new eval test must be classified before the invariants below can hold.

    Without this, the two rosters silently stop describing the population and
    the marker assertions quietly cover fewer tests than they appear to.
    """
    collected = set(_collected_eval_markers())
    classified = set(LIVE_MODEL_EVAL_TESTS) | set(HERMETIC_EVAL_TESTS)

    unclassified = sorted(collected - classified)
    assert not unclassified, (
        f"eval-marked tests missing from this module's roster: {unclassified}. "
        "Add each to LIVE_MODEL_EVAL_TESTS (it dials a live chat/embedder, and "
        "therefore needs @pytest.mark.live_ollama) or to HERMETIC_EVAL_TESTS (it "
        "needs the corpus but no model, and must KEEP the socket guard)."
    )

    stale = sorted(classified - collected)
    assert not stale, (
        f"rostered eval tests that pytest no longer collects: {stale}. They were "
        "renamed, deleted, or lost their `eval` marker — the roster must shrink "
        "deliberately, not rot."
    )


def test_live_model_eval_gates_opt_out_of_the_live_ollama_guard() -> None:
    """``live_ollama`` on every live gate, or the gate fails instead of skipping.

    Regression test for the ``eval`` workflow going red in ``e084c79``: three
    eval gates kept their ``except OllamaUnavailable -> pytest.skip`` contract
    but never got the marker that lets the connection attempt reach the
    translation layer at all.
    """
    assert LIVE_MODEL_EVAL_TESTS, "the live-gate roster is empty — nothing is checked"
    collected = _collected_eval_markers()

    unmarked = sorted(
        nodeid
        for nodeid in LIVE_MODEL_EVAL_TESTS
        if "live_ollama" not in collected.get(nodeid, frozenset())
    )
    assert not unmarked, (
        "live-model eval gates missing @pytest.mark.live_ollama: "
        f"{unmarked}. Without it tests/conftest.py::_forbid_live_ollama raises "
        "LiveOllamaForbidden (a BaseException) from socket.connect, which "
        "bypasses brain.chat's ConnectError -> OllamaUnavailable translation, "
        "so the test's clean-skip path never runs and the eval workflow fails."
    )


def test_hermetic_eval_gates_keep_the_live_ollama_socket_guard() -> None:
    """The mirror invariant: a hermetic gate must NOT opt out of the socket ban.

    Skips loudly while the roster is empty, so the inertness is visible in the
    run summary instead of reading as a passing check over nothing.
    """
    if not HERMETIC_EVAL_TESTS:
        pytest.skip(
            "HERMETIC_EVAL_TESTS is empty — no eval gate is classified as "
            "model-free yet, so there is nothing to check here"
        )

    collected = _collected_eval_markers()
    escaped = sorted(
        nodeid
        for nodeid in HERMETIC_EVAL_TESTS
        if "live_ollama" in collected.get(nodeid, frozenset())
    )
    assert not escaped, (
        f"hermetic eval gates carrying @pytest.mark.live_ollama: {escaped}. That "
        "marker lifts tests/conftest.py::_forbid_live_ollama's socket ban, so a "
        "test classified as needing no model can silently reach a live one."
    )


def _eval_workflow_steps() -> list[dict[str, object]]:
    """The ``eval`` job's steps, parsed from the workflow file."""
    workflow = yaml.safe_load(_EVAL_WORKFLOW.read_text(encoding="utf-8"))
    steps = workflow["jobs"]["eval"]["steps"]
    assert isinstance(steps, list)
    return steps


def test_eval_workflow_selects_the_eval_marker() -> None:
    """Pins the premise of the invariants above: the job runs ``-m eval``.

    If the selection ever changes, the marker rosters are no longer the right
    thing to pin and this test says so instead of letting them drift into a
    no-op.
    """
    runs = [str(step.get("run", "")) for step in _eval_workflow_steps()]
    assert any("pytest -m eval" in run for run in runs), (
        f"{_EVAL_WORKFLOW.name} no longer runs `pytest -m eval`; the "
        "eval/live_ollama marker invariant needs revisiting"
    )


def test_eval_regression_gate_is_conditional_on_a_committed_baseline() -> None:
    """``brain eval --fail-below`` must stay behind the ``ci.json`` existence check.

    The gate scores against a live corpus. Running it unconditionally on a
    runner that has no corpus would fail every build; dropping the guard the
    other way (running the gate only inside a branch that can never be taken)
    would make it dormant forever. Assert the two appear together, in that
    order, in the same step.
    """
    gate_steps = [
        str(step.get("run", ""))
        for step in _eval_workflow_steps()
        if "--fail-below" in str(step.get("run", ""))
    ]
    assert len(gate_steps) == 1, (
        f"expected exactly one `--fail-below` step in {_EVAL_WORKFLOW.name}, "
        f"found {len(gate_steps)}"
    )
    script = gate_steps[0]
    guard = f"[ -f {_CI_BASELINE} ]"
    assert guard in script, (
        f"the eval regression gate must be guarded by `{guard}`; without it a "
        "runner with no committed baseline fails every build"
    )
    assert script.index(guard) < script.index("--fail-below"), (
        "the ci.json existence check must precede the gate invocation"
    )


def test_eval_regression_gate_also_requires_the_golden_corpus() -> None:
    """The gate must require the corpus file too, not just the committed baseline.

    Guarding on ``ci.json`` alone was a latent build-breaker: the baseline is
    committed but ``tests/eval/golden_corpus.yaml`` is gitignored by design, so
    the moment a baseline landed the gate armed on a runner that has no corpus
    and ``brain eval`` exited 1 in ``load_corpus`` — before it opened a database
    connection, so the failure did not even name the real problem.

    Requiring both keeps the step self-arming on a machine that can genuinely
    run it (a developer box, or a self-hosted runner attached to a live brain)
    and a no-op everywhere else, which is the honest shape for a check whose
    ground truth is one machine's document ids.
    """
    gate_steps = [
        str(step.get("run", ""))
        for step in _eval_workflow_steps()
        if "--fail-below" in str(step.get("run", ""))
    ]
    assert len(gate_steps) == 1
    script = gate_steps[0]
    corpus_guard = f"[ -f {_GOLDEN_CORPUS} ]"

    assert corpus_guard in script, (
        f"the eval regression gate must also be guarded by `{corpus_guard}`; "
        f"{_CI_BASELINE} alone arms it on a runner that has no corpus, where "
        "`brain eval` exits 1 in load_corpus"
    )
    assert script.index(corpus_guard) < script.index("--fail-below"), (
        "the corpus existence check must precede the gate invocation"
    )


def test_golden_corpus_stays_gitignored() -> None:
    """The corpus must remain local-only — it is the premise of the guard above.

    If someone ever commits a ``golden_corpus.yaml``, the gate arms in CI and
    fails differently (empty database, then non-matching UUIDs, exit 3) rather
    than skipping. Pin the ignore so that change has to be deliberate.
    """
    gitignore = (_REPO_ROOT / "tests" / "eval" / ".gitignore").read_text(encoding="utf-8")
    lines = [line.strip() for line in gitignore.splitlines() if line.strip()]
    assert "golden_corpus.yaml" in lines, (
        "golden_corpus.yaml must stay gitignored; the eval gate's corpus guard "
        "assumes it is absent on a fresh checkout"
    )


def test_baseline_gitignore_allowlists_ci_json_only() -> None:
    """``tests/eval/baselines/.gitignore`` blanket-ignores JSON but allows ci.json.

    The conditional above is only meaningful if a recorded baseline can actually
    be committed. A blanket ``*.json`` with no allowlist would keep the gate
    permanently dormant no matter what a coordinator records; dropping the
    blanket would push every locally-recorded baseline into git.
    """
    gitignore = (_REPO_ROOT / "tests" / "eval" / "baselines" / ".gitignore").read_text(
        encoding="utf-8"
    )
    lines = [line.strip() for line in gitignore.splitlines() if line.strip()]

    assert "!ci.json" in lines, "ci.json must be allowlisted or the gate is dead"
    assert "*.json" in lines, "local baselines must stay out of git by default"
