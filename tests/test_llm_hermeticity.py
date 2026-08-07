"""The test harness must never reach a live LLM, and must say so if it does.

Guards the two conftest fixtures that enforce this, plus their escape hatches.
Without these the protection is invisible: it would rot the first time somebody
reshuffled the fixtures, and the symptom — a five-minute "hang" that is really
a queued Ollama call, showing 0% CPU and ``idle in transaction`` on the
database side — is expensive to diagnose from scratch.

No documents, no LLM: every value here is synthetic.
"""
from __future__ import annotations

import socket
from typing import Any

import pytest

from brain.config import Config
from tests.conftest import (
    FakeEntityExtractor,
    LiveOllamaForbidden,
    _ollama_port,
    build_fake_enricher,
)

# ---------------------------------------------------------------------------
# The connection guard
# ---------------------------------------------------------------------------


def test_connecting_to_ollama_raises_loudly() -> None:
    """The backstop fires on an unmarked outbound connection to Ollama."""
    # Arrange
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)

    # Act / Assert
    with pytest.raises(LiveOllamaForbidden) as excinfo:
        sock.connect(("127.0.0.1", _ollama_port()))
    assert "live_ollama" in str(excinfo.value), (
        "the message must name the escape hatch — a guard that fires without "
        "saying how to opt in just relocates the confusion"
    )
    sock.close()


def test_guard_does_not_block_other_destinations() -> None:
    """Only the Ollama port is guarded; Postgres and friends pass through.

    Asserted via the exception TYPE: connecting to a closed local port raises
    ``OSError``, which proves the call reached the real ``socket.connect``
    instead of being intercepted.
    """
    # Arrange — a port nothing is listening on, and not Ollama's.
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.settimeout(0.25)
    closed_port = 9

    # Act / Assert
    assert closed_port != _ollama_port()
    with pytest.raises(OSError) as excinfo:
        sock.connect(("127.0.0.1", closed_port))
    assert not isinstance(excinfo.value, LiveOllamaForbidden)
    sock.close()


def test_guard_is_not_swallowed_by_never_raise_handlers() -> None:
    """``LiveOllamaForbidden`` must survive a bare ``except Exception``.

    Both LLM surfaces are contractually never-raise around transport errors,
    so an ``Exception`` subclass would be caught and logged at WARN — the exact
    silent degradation this guard exists to surface. Inheriting
    ``BaseException`` is load-bearing, not stylistic.
    """
    # Arrange
    assert issubclass(LiveOllamaForbidden, BaseException)
    assert not issubclass(LiveOllamaForbidden, Exception)
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    caught_by_broad_handler = False

    # Act
    try:
        try:
            sock.connect(("127.0.0.1", _ollama_port()))
        except Exception:  # noqa: BLE001 — simulating the never-raise handlers
            caught_by_broad_handler = True
    except LiveOllamaForbidden:
        pass
    sock.close()

    # Assert
    assert not caught_by_broad_handler


@pytest.mark.live_ollama
def test_live_ollama_marker_lifts_the_guard() -> None:
    """The escape hatch works — otherwise a genuine live test could not exist.

    Opens the socket but sends nothing, so no LLM work is queued either way.
    The assertion is only that the guard did not INTERCEPT: whether the
    connect succeeds (Ollama running) or raises ``OSError`` (nothing
    listening) is environment-dependent and deliberately not asserted.
    """
    # Arrange
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.settimeout(0.25)

    # Act / Assert
    try:
        sock.connect(("127.0.0.1", _ollama_port()))
    except LiveOllamaForbidden:  # pragma: no cover — the failure this pins
        pytest.fail("@pytest.mark.live_ollama did not lift the connection guard")
    except OSError:
        pass  # nothing listening locally — the guard still stood aside
    finally:
        sock.close()


# ---------------------------------------------------------------------------
# The default doubles
# ---------------------------------------------------------------------------


def test_enricher_seam_is_stubbed_by_default() -> None:
    """``cli._build_enricher`` yields a double that summarizes without a network."""
    # Arrange
    from brain.cli import _build_enricher

    # Act
    enricher = _build_enricher(Config(database_url="postgresql://x/y"))
    result = enricher.summarize("Synthetic title", "Synthetic body content.")

    # Assert
    assert result.summary == "Synthetic test summary."
    assert enricher.model == "fake-model:test"


def test_extractor_seam_is_stubbed_by_default() -> None:
    """``make_extractor`` yields the fake, so ingest costs no LLM round-trip."""
    # Arrange
    from brain.graph_rag.extract import make_extractor

    # Act
    extractor = make_extractor(Config(database_url="postgresql://x/y"))

    # Assert
    assert isinstance(extractor, FakeEntityExtractor)
    assert extractor.extract("acmepay and phoenix") == []
    assert extractor.version == "fake-extractor@test"


@pytest.mark.real_llm_backends
def test_real_llm_backends_marker_restores_the_real_factory() -> None:
    """The opt-out works — factory tests still see the concrete types."""
    # Arrange
    from brain.graph_rag.extract import OllamaExtractor, make_extractor

    # Act
    extractor = make_extractor(Config(database_url="postgresql://x/y"))

    # Assert
    assert isinstance(extractor, OllamaExtractor)


def test_a_local_patch_still_beats_the_default_stub(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A test's own double must win over the autouse default.

    This is the property that holds the blast radius down to "tests silently
    relying on live Ollama". The first cut of the fixture stubbed
    ``make_graph_syncer`` and thereby pre-empted the fake that
    ``test_graphrag_concepts`` had already installed on ``make_extractor``.
    """
    # Arrange
    sentinel = FakeEntityExtractor()
    monkeypatch.setattr(
        "brain.graph_rag.extract.make_extractor", lambda cfg: sentinel
    )

    # Act
    from brain.graph_rag.extract import make_extractor

    got = make_extractor(Config(database_url="postgresql://x/y"))

    # Assert
    assert got is sentinel


def test_fake_enricher_exposes_the_full_public_surface() -> None:
    """The double is the REAL class, so no method is missing.

    A hand-rolled stub would satisfy the auto-summary hook and then raise
    ``AttributeError`` the moment a test exercised ``propose_tags`` or a
    contradiction check. Building a genuine ``OllamaEnricher`` over a mock
    transport keeps every parser honest.
    """
    # Arrange
    from brain.enrichment import OllamaEnricher

    # Act
    enricher = build_fake_enricher()

    # Assert
    assert isinstance(enricher, OllamaEnricher)
    for method in (
        "summarize", "propose_tags", "extract_entities", "count_tokens",
        "truncate_to_tokens", "summarize_group", "summarize_bucket",
        "draft_rule", "assess_contradiction",
    ):
        assert callable(getattr(enricher, method)), f"missing {method}"


# ---------------------------------------------------------------------------
# Graph flags must remain settable from outside
# ---------------------------------------------------------------------------


def test_graph_flags_are_overridable_by_an_explicit_export(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A test that wants concepts OFF can say so and be obeyed.

    ``_force_graph_flags_default`` isolates the suite from the repo ``.env``
    (which sets both flags to ``true``). It used to do that by clobbering the
    variables unconditionally — and since an empty value parses as "use the
    code default", which is ``True`` for both, the flags could not be turned
    off from anywhere. An isolation fixture nobody can opt out of is a trap.
    """
    # Arrange / Act
    monkeypatch.setenv("BRAIN_GRAPH_CONCEPTS", "false")
    cfg = Config.load()

    # Assert
    assert cfg.graph_concepts is False


def test_graph_flags_default_to_the_code_default_without_an_override() -> None:
    """With no override the suite still ignores ``.env`` and uses the default."""
    # Arrange / Act
    cfg = Config.load()

    # Assert — the code default, not whatever the operator's .env happens to say.
    assert cfg.graph_concepts is True


def test_ollama_port_matches_the_config_default() -> None:
    """The guard watches the port the code would actually dial."""
    # Arrange / Act
    cfg = Config(database_url="postgresql://x/y")

    # Assert
    assert str(_ollama_port()) in cfg.ollama_host


def test_fake_extractor_satisfies_the_entity_extractor_protocol() -> None:
    """Liskov: the double is substitutable for the real extractor."""
    # Arrange
    from brain.graph_rag.extract import EntityExtractor

    # Act
    extractor: Any = FakeEntityExtractor()

    # Assert
    assert isinstance(extractor, EntityExtractor)
