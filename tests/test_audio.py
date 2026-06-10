"""Tests for ``brain.audio`` — Plan 04 two-host audio-overview generation.

Three layers, all offline (no live Ollama / TTS engine):

* **Pure unit** — value-object serialization, bundle assembly (top-K, no-summary
  fallback), prompt budgeting, turn validation / retry / truncation, and the
  ``ShellTtsBackend`` argv contract. The LLM round-trip is an injected fake
  ``chat_fn``; the TTS subprocess is a ``mock.patch`` on ``subprocess.run``.
* **Integration** (real Postgres ``test_db``) — bundle assembly over seeded
  ``documents`` rows proving full bodies never reach the prompt (the privacy
  contract), for both themes and global modes.
* **CLI** (``CliRunner``) — flag validation, artifact writing, ``--json`` stdout,
  the graph-disabled / no-themes guards, and the ``--tts`` shell bridge. Graph
  retrieval + the script generator are swapped for fakes via the cli factory
  seams (``_graphrag_search_or_exit`` / ``_build_script_generator``) — standard
  test doubles, no production monkey-patching.

All people / topics / documents are synthetic; no PII.
"""
from __future__ import annotations

import hashlib
import json
import uuid
from pathlib import Path
from typing import Any
from unittest import mock

import psycopg
import pytest
from typer.testing import CliRunner

import brain.audio as audio_module
from brain.audio import (
    GUEST,
    HOST,
    PodcastScript,
    ScriptGenerator,
    ScriptTurn,
    ShellTtsBackend,
    SourceBundle,
    _default_count_tokens,
    build_prompt,
    bundle_from_graph_context,
    make_script_generator,
    make_title,
    make_tts_backend,
)
from brain.cli import app
from brain.config import Config
from brain.errors import AudioError, TtsError
from brain.graph_rag.schema import (
    CommunityGroup,
    GraphContext,
    GraphEntity,
    ThemeGroup,
)
from tests.conftest import TEST_DATABASE_URL

# A sentinel that MUST NOT leak into any script prompt — proves the bundle path
# never loads ``documents.content``.
_BODY_SENTINEL = "SECRET_BODY_SENTINEL_do_not_leak"


# --------------------------------------------------------------------------- #
# Helpers / fakes
# --------------------------------------------------------------------------- #
def _cfg(**overrides: Any) -> Config:
    """Minimal :class:`Config` for the audio unit/CLI paths."""
    params: dict[str, Any] = {
        "database_url": TEST_DATABASE_URL,
        "ollama_host": "http://x",
        "audio_script_model": "audio-test-model",
        "audio_max_turns": 12,
        "audio_max_input_tokens": 3000,
        "audio_theme_limit": 4,
    }
    params.update(overrides)
    return Config(**params)


def _entity(name: str, *, entity_type: str = "topic") -> GraphEntity:
    return GraphEntity(
        id=uuid.uuid4().hex,
        entity_type=entity_type,
        name=name,
        canonical_key=name.lower(),
    )


def _theme(
    group_id: int,
    *,
    names: list[str],
    score: float,
    summary: str | None = None,
    doc_ids: list[str] | None = None,
) -> ThemeGroup:
    return ThemeGroup(
        group_id=group_id,
        entities=[_entity(n) for n in names],
        doc_ids=doc_ids or [],
        score=score,
        summary=summary,
    )


def _community(
    key: str,
    *,
    names: list[str],
    score: float,
    summary: str | None = None,
    doc_ids: list[str] | None = None,
) -> CommunityGroup:
    return CommunityGroup(
        community_key=key,
        member_count=len(names),
        score=score,
        summary=summary,
        entities=[_entity(n) for n in names],
        doc_ids=doc_ids or [],
    )


def _themes_ctx(
    themes: list[ThemeGroup], *, person: str | None = "Synthetic Person"
) -> GraphContext:
    return GraphContext(
        session_id=uuid.uuid4().hex,
        mode="themes",
        query="",
        person=person,
        themes=themes,
    )


def _global_ctx(
    communities: list[CommunityGroup], *, query: str = "synthetic topic"
) -> GraphContext:
    return GraphContext(
        session_id=uuid.uuid4().hex,
        mode="global",
        query=query,
        communities=communities,
    )


class FakeChat:
    """A fake ``chat_fn``: records prompts, replays canned responses in order.

    A response that is an ``Exception`` is raised (to drive the retry / error
    paths); the last response repeats once exhausted.
    """

    def __init__(self, responses: list[Any]) -> None:
        self._responses = responses
        self.prompts: list[str] = []
        self.num_predicts: list[int] = []
        self.calls = 0

    def __call__(
        self, prompt: str, schema: dict[str, Any], num_predict: int
    ) -> dict[str, Any]:
        self.prompts.append(prompt)
        self.num_predicts.append(num_predict)
        index = min(self.calls, len(self._responses) - 1)
        self.calls += 1
        resp = self._responses[index]
        if isinstance(resp, Exception):
            raise resp
        return resp


def _turns_response(*pairs: tuple[str, str]) -> dict[str, Any]:
    return {"turns": [{"speaker": s, "text": t} for s, t in pairs]}


def _word_count(text: str) -> int:
    """Deterministic injected token counter (word count) for prompt tests."""
    return len(text.split())


def _generator(
    chat_fn: Any,
    *,
    max_turns: int = 12,
    max_input_tokens: int = 3000,
    model: str = "audio-test-model",
) -> ScriptGenerator:
    return ScriptGenerator(
        chat_fn=chat_fn,
        model=model,
        max_turns=max_turns,
        max_input_tokens=max_input_tokens,
        count_tokens=_word_count,
    )


def _insert_doc(
    conn: psycopg.Connection[Any],
    *,
    title: str,
    content: str,
    summary: str | None,
) -> str:
    """Insert a synthetic ``documents`` row (with summary); return its id."""
    src = conn.execute(
        "INSERT INTO sources (kind, external_id, metadata) "
        "VALUES ('manual', %s, '{}'::jsonb) RETURNING id",
        (uuid.uuid4().hex,),
    ).fetchone()
    assert src is not None
    salted = f"{content}\n<!-- {uuid.uuid4()} -->"
    content_hash = hashlib.sha256(salted.encode("utf-8")).hexdigest()
    row = conn.execute(
        "INSERT INTO documents (source_id, title, content, content_hash, "
        "content_type, summary) VALUES (%s, %s, %s, %s, 'note', %s) "
        "RETURNING id::text",
        (src[0], title, content, content_hash, summary),
    ).fetchone()
    assert row is not None
    return str(row[0])


# --------------------------------------------------------------------------- #
# Value-object serialization
# --------------------------------------------------------------------------- #
def test_script_turn_serialization() -> None:
    script = PodcastScript(
        title="A Conversation About Synthetic Things",
        source_kind="themes",
        source_person="Synthetic Person",
        source_topic=None,
        theme_count=2,
        generated_at="2026-06-09T00:00:00+00:00",
        model="audio-test-model",
        turns=[
            ScriptTurn(speaker=HOST, text="Welcome."),
            ScriptTurn(speaker=GUEST, text="Glad to be here."),
        ],
    )
    payload = script.to_dict()
    # Round-trips through JSON cleanly.
    assert json.loads(json.dumps(payload)) == payload
    assert payload["source"] == {
        "kind": "themes",
        "person": "Synthetic Person",
        "topic": None,
        "theme_count": 2,
    }
    assert payload["turns"][0] == {"speaker": "Host", "text": "Welcome."}
    assert payload["generated_at"] == "2026-06-09T00:00:00+00:00"


def test_podcast_script_to_markdown() -> None:
    script = PodcastScript(
        title="Synthetic Title",
        source_kind="global",
        source_person=None,
        source_topic="synthetic topic",
        theme_count=1,
        generated_at="2026-06-09T00:00:00+00:00",
        model="m",
        turns=[
            ScriptTurn(speaker=HOST, text="One."),
            ScriptTurn(speaker=GUEST, text="Two."),
        ],
    )
    md = script.to_markdown()
    assert md.startswith("## Synthetic Title")
    assert "> **Host:** One." in md
    assert "> **Guest:** Two." in md
    assert md.endswith("\n")


# --------------------------------------------------------------------------- #
# Bundle assembly
# --------------------------------------------------------------------------- #
def test_bundle_assembly_top_k() -> None:
    themes = [
        _theme(1, names=["alpha"], score=0.2),
        _theme(2, names=["beta"], score=0.9),
        _theme(3, names=["gamma"], score=0.5),
        _theme(4, names=["delta"], score=0.7),
        _theme(5, names=["epsilon"], score=0.1),
    ]
    bundle = bundle_from_graph_context(
        None,  # type: ignore[arg-type]  # no doc_ids -> conn unused
        _themes_ctx(themes),
        theme_limit=2,
        fetch_summary=lambda _conn, _doc: None,
    )
    # Top-2 by score: beta (0.9), delta (0.7).
    names = [g.entity_names[0] for g in bundle.groups]
    assert names == ["beta", "delta"]
    assert bundle.kind == "themes"
    assert bundle.person == "Synthetic Person"
    assert bundle.theme_count == 2


def test_bundle_assembly_fewer_themes_than_k() -> None:
    themes = [_theme(1, names=["solo"], score=0.5)]
    bundle = bundle_from_graph_context(
        None,  # type: ignore[arg-type]
        _themes_ctx(themes),
        theme_limit=4,
        fetch_summary=lambda _conn, _doc: None,
    )
    assert bundle.theme_count == 1
    assert bundle.groups[0].entity_names == ["solo"]


def test_bundle_assembly_no_summaries_falls_back_to_entities() -> None:
    themes = [_theme(1, names=["widgets", "gadgets"], score=0.5, summary=None)]
    bundle = bundle_from_graph_context(
        None,  # type: ignore[arg-type]
        _themes_ctx(themes),
        theme_limit=4,
        fetch_summary=lambda _conn, _doc: None,
    )
    group = bundle.groups[0]
    assert group.summary is None
    assert group.entity_names == ["widgets", "gadgets"]
    # The prompt is still non-empty and mentions the entities.
    prompt = build_prompt(
        bundle, max_turns=4, max_input_tokens=10_000, count_tokens=_word_count
    )
    assert "widgets" in prompt
    assert prompt.strip()


def test_bundle_includes_docs_without_summary() -> None:
    # A doc whose summary is NULL is still surfaced by title alone (no crash).
    themes = [_theme(1, names=["topic"], score=0.5, doc_ids=["doc-1"])]
    bundle = bundle_from_graph_context(
        None,  # type: ignore[arg-type]
        _themes_ctx(themes),
        theme_limit=4,
        fetch_summary=lambda _conn, _doc: ("Untitled Synthetic Doc", None),
    )
    assert bundle.groups[0].docs == [("Untitled Synthetic Doc", None)]
    prompt = build_prompt(
        bundle, max_turns=4, max_input_tokens=10_000, count_tokens=_word_count
    )
    assert "Untitled Synthetic Doc" in prompt


def test_bundle_skips_missing_documents() -> None:
    # fetch_summary returning None (doc id absent) is silently skipped.
    themes = [_theme(1, names=["topic"], score=0.5, doc_ids=["gone"])]
    bundle = bundle_from_graph_context(
        None,  # type: ignore[arg-type]
        _themes_ctx(themes),
        theme_limit=4,
        fetch_summary=lambda _conn, _doc: None,
    )
    assert bundle.groups[0].docs == []


def test_bundle_global_mode_sets_topic() -> None:
    communities = [_community("c1", names=["infra"], score=0.8)]
    bundle = bundle_from_graph_context(
        None,  # type: ignore[arg-type]
        _global_ctx(communities, query="infrastructure cost"),
        theme_limit=4,
        fetch_summary=lambda _conn, _doc: None,
    )
    assert bundle.kind == "global"
    assert bundle.topic == "infrastructure cost"
    assert bundle.person is None


# --------------------------------------------------------------------------- #
# Title + prompt construction
# --------------------------------------------------------------------------- #
def test_make_title_themes_and_global() -> None:
    themes_bundle = SourceBundle(
        kind="themes", person="Synthetic Person", topic=None
    )
    assert "Synthetic Person" in make_title(themes_bundle)
    global_bundle = SourceBundle(kind="global", person=None, topic="cost")
    assert make_title(global_bundle) == "A Conversation About cost"
    fallback = SourceBundle(kind="themes", person=None, topic=None)
    assert make_title(fallback) == "An Audio Overview From My Second Brain"


def test_build_prompt_respects_token_budget() -> None:
    themes = [
        _theme(1, names=["alpha"], score=0.9, summary="first summary"),
        _theme(2, names=["beta"], score=0.8, summary="second summary"),
        _theme(3, names=["gamma"], score=0.7, summary="third summary"),
    ]
    bundle = bundle_from_graph_context(
        None,  # type: ignore[arg-type]
        _themes_ctx(themes),
        theme_limit=4,
        fetch_summary=lambda _conn, _doc: None,
    )
    # A tiny budget forces the greedy builder to stop after the first group.
    prompt = build_prompt(
        bundle, max_turns=4, max_input_tokens=40, count_tokens=_word_count
    )
    assert "alpha" in prompt
    assert "gamma" not in prompt  # trimmed by the budget
    # First group is always included even when it alone exceeds the budget.
    tiny = build_prompt(
        bundle, max_turns=4, max_input_tokens=1, count_tokens=_word_count
    )
    assert "alpha" in tiny


# --------------------------------------------------------------------------- #
# Script generator: validation / retry / truncation
# --------------------------------------------------------------------------- #
def _simple_bundle() -> SourceBundle:
    themes = [_theme(1, names=["topic"], score=0.5, summary="a summary")]
    return bundle_from_graph_context(
        None,  # type: ignore[arg-type]
        _themes_ctx(themes),
        theme_limit=4,
        fetch_summary=lambda _conn, _doc: None,
    )


def test_script_generator_validates_turns_and_retries_once() -> None:
    # First response has a malformed turn (no speaker); second is valid.
    bad = {"turns": [{"text": "no speaker"}]}
    good = _turns_response((HOST, "Hello."), (GUEST, "Hi."))
    chat = FakeChat([bad, good])
    script = _generator(chat).generate(
        _simple_bundle(), title="T", generated_at="2026-06-09T00:00:00+00:00"
    )
    assert chat.calls == 2  # retried exactly once
    assert len(script.turns) == 2


def test_script_generator_raises_after_two_bad_responses() -> None:
    bad = {"turns": "not a list"}
    chat = FakeChat([bad, bad])
    with pytest.raises(AudioError):
        _generator(chat).generate(
            _simple_bundle(), title="T", generated_at="2026-06-09T00:00:00+00:00"
        )
    assert chat.calls == 2


def test_script_generator_speaker_names_normalized() -> None:
    # Mixed-case / suffixed speaker labels normalize to exactly Host / Guest.
    resp = _turns_response(("host", "a"), ("GUEST", "b"), ("Host:", "c"))
    chat = FakeChat([resp])
    script = _generator(chat).generate(
        _simple_bundle(), title="T", generated_at="2026-06-09T00:00:00+00:00"
    )
    assert [t.speaker for t in script.turns] == [HOST, GUEST, HOST]


def test_script_generator_truncates_excess_turns() -> None:
    resp = _turns_response(
        (HOST, "1"), (GUEST, "2"), (HOST, "3"), (GUEST, "4")
    )
    chat = FakeChat([resp])
    script = _generator(chat, max_turns=2).generate(
        _simple_bundle(), title="T", generated_at="2026-06-09T00:00:00+00:00"
    )
    assert len(script.turns) == 2
    assert [t.text for t in script.turns] == ["1", "2"]


def test_script_generator_rejects_empty_turns() -> None:
    chat = FakeChat([{"turns": []}, {"turns": []}])
    with pytest.raises(AudioError):
        _generator(chat).generate(
            _simple_bundle(), title="T", generated_at="2026-06-09T00:00:00+00:00"
        )


def test_script_generator_rejects_unknown_speaker() -> None:
    # A speaker that resolves to neither Host nor Guest is rejected (both tries).
    resp = _turns_response(("Narrator", "a"))
    chat = FakeChat([resp, resp])
    with pytest.raises(AudioError):
        _generator(chat).generate(
            _simple_bundle(), title="T", generated_at="2026-06-09T00:00:00+00:00"
        )


def test_script_generator_rejects_non_object_turn() -> None:
    chat = FakeChat([{"turns": ["just a string"]}, {"turns": ["still bad"]}])
    with pytest.raises(AudioError):
        _generator(chat).generate(
            _simple_bundle(), title="T", generated_at="2026-06-09T00:00:00+00:00"
        )


def test_script_generator_rejects_empty_text() -> None:
    blank = {"turns": [{"speaker": "Host", "text": "   "}]}
    chat = FakeChat([blank, blank])
    with pytest.raises(AudioError):
        _generator(chat).generate(
            _simple_bundle(), title="T", generated_at="2026-06-09T00:00:00+00:00"
        )


# --------------------------------------------------------------------------- #
# Production wiring (make_script_generator / token counter)
# --------------------------------------------------------------------------- #
def test_make_script_generator_binds_audio_model(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, Any] = {}

    def _fake_chat_json(
        prompt: str,
        *,
        schema: dict[str, Any],
        cfg: Config,
        model: str | None = None,
        num_predict: int | None = None,
    ) -> dict[str, Any]:
        captured["model"] = model
        captured["num_predict"] = num_predict
        return _turns_response((HOST, "a"), (GUEST, "b"))

    monkeypatch.setattr(audio_module, "chat_json", _fake_chat_json)
    cfg = _cfg(audio_script_model="custom-audio-model")
    generator = make_script_generator(cfg, max_turns=6)
    script = generator.generate(
        _simple_bundle(), title="T", generated_at="2026-06-09T00:00:00+00:00"
    )
    # The chat closure routes to chat_json with the AUDIO model (not enrich).
    assert captured["model"] == "custom-audio-model"
    assert captured["num_predict"] is not None
    assert script.model == "custom-audio-model"
    assert len(script.turns) == 2


def test_make_script_generator_defaults_max_turns_from_cfg() -> None:
    cfg = _cfg(audio_max_turns=8)
    generator = make_script_generator(cfg)
    assert generator._max_turns == 8  # noqa: SLF001 — white-box wiring check


def test_default_count_tokens_is_positive() -> None:
    assert _default_count_tokens("hello synthetic world") > 0
    assert _default_count_tokens("") == 0


# --------------------------------------------------------------------------- #
# ShellTtsBackend / make_tts_backend
# --------------------------------------------------------------------------- #
def _dummy_script() -> PodcastScript:
    return PodcastScript(
        title="T",
        source_kind="themes",
        source_person="X",
        source_topic=None,
        theme_count=1,
        generated_at="2026-06-09T00:00:00+00:00",
        model="m",
        turns=[ScriptTurn(speaker=HOST, text="hi")],
    )


def test_shell_tts_backend_argv_construction() -> None:
    backend = ShellTtsBackend("/usr/bin/tts-wrap --voice nova")
    json_path = Path("/tmp/some dir/out.json")
    out_path = Path("/tmp/some dir/out.mp3")
    with mock.patch("brain.audio.subprocess.run") as run:
        backend.synthesize(_dummy_script(), json_path, out_path)
    # argv split via shlex + the two paths appended as discrete list entries.
    args, kwargs = run.call_args
    argv = args[0]
    assert argv == [
        "/usr/bin/tts-wrap",
        "--voice",
        "nova",
        "/tmp/some dir/out.json",
        "/tmp/some dir/out.mp3",
    ]
    # shell=False (the subprocess default) — never shell=True.
    assert kwargs.get("shell", False) is False
    assert kwargs["check"] is True


def test_shell_tts_backend_empty_command_raises() -> None:
    with pytest.raises(TtsError):
        ShellTtsBackend("   ")


def test_shell_tts_backend_missing_command_raises() -> None:
    backend = ShellTtsBackend("/nonexistent/cmd-xyz-12345")
    with pytest.raises(TtsError):
        backend.synthesize(
            _dummy_script(), Path("/tmp/a.json"), Path("/tmp/a.mp3")
        )


def test_shell_tts_backend_nonzero_exit_raises() -> None:
    backend = ShellTtsBackend("false")
    with pytest.raises(TtsError):
        backend.synthesize(
            _dummy_script(), Path("/tmp/a.json"), Path("/tmp/a.mp3")
        )


def test_shell_tts_backend_timeout_raises() -> None:
    import subprocess

    backend = ShellTtsBackend("/usr/bin/tts-wrap")
    with mock.patch(
        "brain.audio.subprocess.run",
        side_effect=subprocess.TimeoutExpired(cmd="tts-wrap", timeout=1.0),
    ), pytest.raises(TtsError):
        backend.synthesize(
            _dummy_script(), Path("/tmp/a.json"), Path("/tmp/a.mp3")
        )


def test_make_tts_backend_shell_spec() -> None:
    backend = make_tts_backend("shell:/usr/bin/say")
    assert isinstance(backend, ShellTtsBackend)


def test_make_tts_backend_unknown_spec() -> None:
    with pytest.raises(TtsError):
        make_tts_backend("bogus:x")


# --------------------------------------------------------------------------- #
# Integration: bundle over real seeded documents (privacy contract)
# --------------------------------------------------------------------------- #
def test_audio_bundle_no_body_leak_themes(
    test_db: psycopg.Connection[Any],
) -> None:
    doc1 = _insert_doc(
        test_db,
        title="Synthetic Widgets Note",
        content=f"{_BODY_SENTINEL} the widgets discussion went on at length",
        summary="synthetic summary about widgets",
    )
    doc2 = _insert_doc(
        test_db,
        title="Synthetic Gadgets Note",
        content=f"{_BODY_SENTINEL} gadgets too",
        summary="synthetic summary about gadgets",
    )
    themes = [
        _theme(
            1,
            names=["widgets"],
            score=0.9,
            summary="theme-level synthetic summary",
            doc_ids=[doc1, doc2],
        )
    ]
    bundle = bundle_from_graph_context(
        test_db, _themes_ctx(themes), theme_limit=4
    )
    group = bundle.groups[0]
    assert ("Synthetic Widgets Note", "synthetic summary about widgets") in group.docs

    chat = FakeChat([_turns_response((HOST, "a"), (GUEST, "b"))])
    script = _generator(chat).generate(
        bundle, title=make_title(bundle), generated_at="2026-06-09T00:00:00+00:00"
    )
    assert len(script.turns) >= 2
    # The privacy guarantee: no document BODY content reached the LLM prompt.
    assert _BODY_SENTINEL not in chat.prompts[0]
    assert "synthetic summary about widgets" in chat.prompts[0]


def test_audio_bundle_global_mode(test_db: psycopg.Connection[Any]) -> None:
    doc = _insert_doc(
        test_db,
        title="Synthetic Infra Note",
        content=f"{_BODY_SENTINEL} infra body",
        summary="synthetic infra summary",
    )
    communities = [
        _community("c1", names=["infra"], score=0.8, doc_ids=[doc])
    ]
    bundle = bundle_from_graph_context(
        test_db, _global_ctx(communities, query="infrastructure cost"), theme_limit=4
    )
    assert bundle.kind == "global"
    assert bundle.topic == "infrastructure cost"
    chat = FakeChat([_turns_response((HOST, "a"), (GUEST, "b"))])
    script = _generator(chat).generate(
        bundle, title=make_title(bundle), generated_at="2026-06-09T00:00:00+00:00"
    )
    assert script.source_kind == "global"
    assert script.turns
    assert _BODY_SENTINEL not in chat.prompts[0]


# --------------------------------------------------------------------------- #
# CLI surface
# --------------------------------------------------------------------------- #
@pytest.fixture
def _patch_audio(monkeypatch: pytest.MonkeyPatch) -> Any:
    """Swap the cli graph seam + generator factory for fakes (no AGE/Ollama).

    Returns a callable that installs a given GraphContext + chat responses and
    yields the installed ``FakeChat`` so the test can inspect prompts.
    """
    monkeypatch.setenv("DATABASE_URL", TEST_DATABASE_URL)

    def _install(ctx: GraphContext, responses: list[Any]) -> FakeChat:
        chat = FakeChat(responses)

        def _fake_search(*_args: Any, **_kwargs: Any) -> GraphContext:
            return ctx

        def _fake_generator(
            cfg: Config, *, max_turns: int | None = None
        ) -> ScriptGenerator:
            return _generator(
                chat, max_turns=max_turns if max_turns is not None else 12
            )

        monkeypatch.setattr("brain.cli._graphrag_search_or_exit", _fake_search)
        monkeypatch.setattr("brain.cli._build_script_generator", _fake_generator)
        return chat

    return _install


def test_audio_requires_person_or_topic() -> None:
    result = CliRunner().invoke(app, ["audio"])
    assert result.exit_code != 0


def test_audio_mutually_exclusive_flags() -> None:
    result = CliRunner().invoke(
        app, ["audio", "--person", "X", "--topic", "Y"]
    )
    assert result.exit_code != 0


def test_audio_rejects_odd_turns(_patch_audio: Any) -> None:
    _patch_audio(_themes_ctx([_theme(1, names=["t"], score=0.5)]), [])
    result = CliRunner().invoke(
        app, ["audio", "--person", "Synthetic Person", "--turns", "3"]
    )
    assert result.exit_code != 0


def test_audio_graph_disabled(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DATABASE_URL", TEST_DATABASE_URL)
    monkeypatch.setenv("BRAIN_GRAPH_ENABLED", "false")
    result = CliRunner().invoke(app, ["audio", "--person", "Synthetic Person"])
    assert result.exit_code == 1
    assert "graph" in result.output.lower()


def test_audio_no_themes_found(_patch_audio: Any) -> None:
    _patch_audio(_themes_ctx([], person="Synthetic Person"), [])
    result = CliRunner().invoke(app, ["audio", "--person", "Synthetic Person"])
    assert result.exit_code == 1
    assert "no themes" in result.output.lower()


def test_audio_out_flag_writes_json_and_md(
    _patch_audio: Any, tmp_path: Path
) -> None:
    ctx = _themes_ctx(
        [_theme(1, names=["topic"], score=0.5, summary="syn summary")]
    )
    _patch_audio(ctx, [_turns_response((HOST, "a"), (GUEST, "b"))])
    base = tmp_path / "overview"
    result = CliRunner().invoke(
        app,
        ["audio", "--person", "Synthetic Person", "--out", str(base)],
    )
    assert result.exit_code == 0, result.output
    json_path = base.with_suffix(".json")
    md_path = base.with_suffix(".md")
    assert json_path.exists()
    assert md_path.exists()
    payload = json.loads(json_path.read_text())
    assert payload["source"]["kind"] == "themes"
    assert len(payload["turns"]) == 2
    assert md_path.read_text().startswith("## ")


def test_audio_json_flag_stdout(_patch_audio: Any, tmp_path: Path) -> None:
    ctx = _themes_ctx(
        [_theme(1, names=["topic"], score=0.5, summary="syn summary")]
    )
    _patch_audio(ctx, [_turns_response((HOST, "a"), (GUEST, "b"))])
    base = tmp_path / "overview"
    result = CliRunner().invoke(
        app,
        ["audio", "--person", "Synthetic Person", "--out", str(base), "--json"],
    )
    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["turns"][0]["speaker"] == "Host"
    # --json writes no files.
    assert not base.with_suffix(".json").exists()
    assert not base.with_suffix(".md").exists()


def test_audio_topic_global_mode(_patch_audio: Any, tmp_path: Path) -> None:
    ctx = _global_ctx(
        [_community("c1", names=["infra"], score=0.8, summary="syn community summary")],
        query="infrastructure cost",
    )
    _patch_audio(ctx, [_turns_response((HOST, "a"), (GUEST, "b"))])
    base = tmp_path / "overview"
    result = CliRunner().invoke(
        app,
        ["audio", "--topic", "infrastructure cost", "--out", str(base), "--json"],
    )
    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["source"]["kind"] == "global"
    assert payload["source"]["topic"] == "infrastructure cost"


def test_audio_tts_invokes_backend(_patch_audio: Any, tmp_path: Path) -> None:
    ctx = _themes_ctx(
        [_theme(1, names=["topic"], score=0.5, summary="syn summary")]
    )
    _patch_audio(ctx, [_turns_response((HOST, "a"), (GUEST, "b"))])
    base = tmp_path / "overview"
    # `touch <json> <mp3>` exits 0 and creates the mp3 artifact.
    result = CliRunner().invoke(
        app,
        [
            "audio",
            "--person",
            "Synthetic Person",
            "--out",
            str(base),
            "--tts",
            "shell:touch",
        ],
    )
    assert result.exit_code == 0, result.output
    assert base.with_suffix(".mp3").exists()


def test_audio_tts_with_json_rejected(_patch_audio: Any, tmp_path: Path) -> None:
    ctx = _themes_ctx([_theme(1, names=["t"], score=0.5)])
    _patch_audio(ctx, [])
    result = CliRunner().invoke(
        app,
        [
            "audio",
            "--person",
            "Synthetic Person",
            "--tts",
            "shell:touch",
            "--json",
        ],
    )
    assert result.exit_code != 0


def test_audio_tts_failure_keeps_artifacts(
    _patch_audio: Any, tmp_path: Path
) -> None:
    ctx = _themes_ctx(
        [_theme(1, names=["topic"], score=0.5, summary="syn summary")]
    )
    _patch_audio(ctx, [_turns_response((HOST, "a"), (GUEST, "b"))])
    base = tmp_path / "overview"
    # `false` exits non-zero -> TtsError -> exit 1, but .json/.md survive.
    result = CliRunner().invoke(
        app,
        [
            "audio",
            "--person",
            "Synthetic Person",
            "--out",
            str(base),
            "--tts",
            "shell:false",
        ],
    )
    assert result.exit_code == 1
    assert "TTS failed" in result.output
    assert base.with_suffix(".json").exists()
    assert base.with_suffix(".md").exists()


def test_audio_ollama_unavailable(_patch_audio: Any, tmp_path: Path) -> None:
    from brain.errors import OllamaUnavailable

    ctx = _themes_ctx([_theme(1, names=["topic"], score=0.5)])
    _patch_audio(ctx, [OllamaUnavailable("ollama down")])
    result = CliRunner().invoke(
        app, ["audio", "--person", "Synthetic Person", "--out", str(tmp_path / "o")]
    )
    assert result.exit_code == 1
    assert "ollama" in result.output.lower()


def test_audio_generation_error(_patch_audio: Any, tmp_path: Path) -> None:
    ctx = _themes_ctx([_theme(1, names=["topic"], score=0.5)])
    bad = {"turns": "not a list"}
    _patch_audio(ctx, [bad, bad])
    result = CliRunner().invoke(
        app, ["audio", "--person", "Synthetic Person", "--out", str(tmp_path / "o")]
    )
    assert result.exit_code == 1


def test_audio_default_out_path(
    _patch_audio: Any, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("BRAIN_HOME", str(tmp_path))
    ctx = _themes_ctx(
        [_theme(1, names=["topic"], score=0.5, summary="syn summary")]
    )
    _patch_audio(ctx, [_turns_response((HOST, "a"), (GUEST, "b"))])
    result = CliRunner().invoke(app, ["audio", "--person", "Synthetic Person"])
    assert result.exit_code == 0, result.output
    written = list((tmp_path / "audio").glob("*.json"))
    assert len(written) == 1
    assert (tmp_path / "audio").glob("*.md")


def test_build_script_generator_returns_generator() -> None:
    from brain.cli import _build_script_generator

    generator = _build_script_generator(_cfg(), max_turns=6)
    assert isinstance(generator, ScriptGenerator)
