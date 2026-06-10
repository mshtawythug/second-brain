"""Plan 04 — `brain audio` two-host audio-overview script generation.

Turns a GraphRAG theme/community bundle into a NotebookLM-style two-host
podcast script (``PodcastScript``), plus a pluggable ``TtsBackend`` Protocol for
optional synthesis. This module owns ONE reason to change: bundle → prompt →
script. Graph retrieval, artifact writing, and CLI orchestration live in
``cli.py``; the LLM round-trip is delegated to the shared
:func:`brain.chat.chat_json` helper (consumed AS-IS — no signature drift).

Privacy contract: the bundle carries ONLY entity names, theme summaries, and
per-document ``(title, summary)`` pairs (``documents.summary`` is the ≤60-word
enrichment projection). Full document bodies never reach the prompt — the bundle
is assembled from :func:`brain.queries.fetch_document_summary`, which selects
``title``/``summary`` only.
"""
from __future__ import annotations

import logging
import shlex
import subprocess
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any, Protocol

from .chat import chat_json
from .errors import AudioError, TtsError

if TYPE_CHECKING:
    import psycopg

    from .config import Config
    from .graph_rag.schema import GraphContext, GraphEntity


class _GroupLike(Protocol):
    """Structural shape shared by ``ThemeGroup`` and ``CommunityGroup``.

    Both carry the fields the bundle assembly reads (``score``/``summary``/
    ``entities``/``doc_ids``); this Protocol lets one code path handle either
    without a runtime ``isinstance`` branch (Liskov substitution). Declared as
    read-only properties so a ``Sequence[_GroupLike]`` stays covariant over the
    two concrete frozen dataclasses.
    """

    @property
    def score(self) -> float: ...
    @property
    def summary(self) -> str | None: ...
    @property
    def entities(self) -> list[GraphEntity]: ...
    @property
    def doc_ids(self) -> list[str]: ...

_logger = logging.getLogger(__name__)

# Speaker labels for the two-host format. The generator validates every turn's
# speaker resolves to exactly one of these (spec §5 test_script_generator_speaker_names).
HOST = "Host"
GUEST = "Guest"
_VALID_SPEAKERS = (HOST, GUEST)

# Source-kind discriminators carried on the bundle + the PodcastScript.
KIND_THEMES = "themes"
KIND_GLOBAL = "global"

# Top-N representative documents pulled per group (spec §3 step 3c "top-2").
_DOCS_PER_GROUP = 2

# Default completion-length budget for the dialogue call (spec §3 step 4c).
_DEFAULT_NUM_PREDICT = 2048

# The schema handed to ``chat_json`` — its KEYS are the required top-level keys
# of the returned JSON object. The generator does the deeper per-turn structural
# validation itself (chat_json only checks key presence).
_TURNS_SCHEMA: dict[str, Any] = {"turns": "list of {speaker, text} dialogue turns"}

# A callable matching the shape the generator needs from ``chat_json``:
# ``(prompt, schema, num_predict) -> parsed JSON object``. Dependency-inverted so
# tests inject a fake without a live Ollama.
ChatFn = Callable[[str, dict[str, Any], int], dict[str, Any]]

# A token counter ``(text) -> int``. Injected so prompt budgeting is testable
# without tiktoken; production uses the cl100k_base counter below.
TokenCounter = Callable[[str], int]


# --------------------------------------------------------------------------- #
# Value objects
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class ScriptTurn:
    """One speaker turn in the dialogue. ``speaker`` is ``Host`` or ``Guest``."""

    speaker: str
    text: str

    def to_dict(self) -> dict[str, str]:
        """Serialize to the wire shape ``{"speaker", "text"}``."""
        return {"speaker": self.speaker, "text": self.text}


@dataclass(frozen=True)
class PodcastScript:
    """A generated two-host audio-overview script (spec §3 script schema).

    ``generated_at`` is an ISO-8601 UTC string stamped by the CLI caller (this
    module never calls ``datetime.now()`` — workflow-resume safe). ``model`` is
    the Ollama model that produced the dialogue.
    """

    title: str
    source_kind: str
    source_person: str | None
    source_topic: str | None
    theme_count: int
    generated_at: str
    model: str
    turns: list[ScriptTurn]

    def to_dict(self) -> dict[str, Any]:
        """Serialize to the public JSON artifact shape (spec §3)."""
        return {
            "title": self.title,
            "source": {
                "kind": self.source_kind,
                "person": self.source_person,
                "topic": self.source_topic,
                "theme_count": self.theme_count,
            },
            "generated_at": self.generated_at,
            "model": self.model,
            "turns": [turn.to_dict() for turn in self.turns],
        }

    def to_markdown(self) -> str:
        """Render the human-editable Markdown transcript artifact (spec §3)."""
        lines = [f"## {self.title}", ""]
        for turn in self.turns:
            lines.append(f"> **{turn.speaker}:** {turn.text}")
            lines.append("")
        return "\n".join(lines).rstrip() + "\n"


@dataclass(frozen=True)
class BundleGroup:
    """One theme/community in the bundle: entity labels + summaries (no bodies)."""

    entity_names: list[str] = field(default_factory=list)
    summary: str | None = None
    # Per-document (title, summary) pairs — never full body content.
    docs: list[tuple[str, str | None]] = field(default_factory=list)


@dataclass(frozen=True)
class SourceBundle:
    """The internal value object fed to the script prompt (not serialized).

    Assembled from a :class:`~brain.graph_rag.schema.GraphContext` by
    :func:`bundle_from_graph_context`. Carries entity-name lists + per-doc
    ``(title, summary)`` pairs only — the privacy boundary.
    """

    kind: str
    person: str | None
    topic: str | None
    groups: list[BundleGroup] = field(default_factory=list)

    @property
    def theme_count(self) -> int:
        """Number of groups in the bundle (the script's ``theme_count``)."""
        return len(self.groups)


# A summary fetcher ``(conn, document_id) -> (title, summary) | None``. Injected
# so the bundle assembly is testable without the default queries import.
SummaryFetcher = Callable[
    ["psycopg.Connection[Any]", str], "tuple[str, str | None] | None"
]


# --------------------------------------------------------------------------- #
# Bundle assembly
# --------------------------------------------------------------------------- #
def _select_top_groups(
    groups: Sequence[_GroupLike], theme_limit: int
) -> list[_GroupLike]:
    """Return the top ``theme_limit`` groups by ``score`` DESC (stable tiebreak).

    The original index is the deterministic tiebreak so equal-score groups keep
    their retrieval order across runs. Typed against :class:`_GroupLike` so one
    body ranks ``ThemeGroup``s and ``CommunityGroup``s alike.
    """
    scored = [(group.score, index, group) for index, group in enumerate(groups)]
    scored.sort(key=lambda item: (-item[0], item[1]))
    return [group for _, _, group in scored[:theme_limit]]


def bundle_from_graph_context(
    conn: psycopg.Connection[Any],
    ctx: GraphContext,
    *,
    theme_limit: int,
    fetch_summary: SummaryFetcher | None = None,
) -> SourceBundle:
    """Assemble a :class:`SourceBundle` from a graph retrieval context.

    Takes the top ``theme_limit`` groups (``ctx.themes`` for themes mode, else
    ``ctx.communities`` for global mode) ranked by ``score`` descending — a
    stable original-index tiebreak keeps repeated runs byte-identical. For each
    group it collects entity names + the group ``summary`` + the top
    :data:`_DOCS_PER_GROUP` documents' ``(title, summary)`` pairs via
    ``fetch_summary`` (default :func:`brain.queries.fetch_document_summary`,
    which selects ``title``/``summary`` only — never ``content``).

    Groups without a summary fall back to entity names + doc titles, so the
    prompt is never empty even on an un-enriched corpus.
    """
    if fetch_summary is None:
        from .queries import fetch_document_summary

        fetch_summary = fetch_document_summary

    if ctx.themes:
        kind = KIND_THEMES
        selected = _select_top_groups(ctx.themes, theme_limit)
    else:
        kind = KIND_GLOBAL
        selected = _select_top_groups(ctx.communities, theme_limit)

    groups: list[BundleGroup] = []
    for group in selected:
        entity_names = [
            entity.name for entity in group.entities if entity.name.strip()
        ]
        docs: list[tuple[str, str | None]] = []
        for doc_id in group.doc_ids[:_DOCS_PER_GROUP]:
            fetched = fetch_summary(conn, doc_id)
            if fetched is not None:
                docs.append(fetched)
        groups.append(
            BundleGroup(entity_names=entity_names, summary=group.summary, docs=docs)
        )

    return SourceBundle(
        kind=kind,
        person=ctx.person,
        topic=ctx.query if kind == KIND_GLOBAL else None,
        groups=groups,
    )


# --------------------------------------------------------------------------- #
# Title + prompt construction (pure)
# --------------------------------------------------------------------------- #
def make_title(bundle: SourceBundle) -> str:
    """Build a deterministic episode title from the bundle's source."""
    if bundle.kind == KIND_THEMES and bundle.person:
        return f"Themes From My Conversations With {bundle.person}"
    if bundle.kind == KIND_GLOBAL and bundle.topic:
        return f"A Conversation About {bundle.topic}"
    return "An Audio Overview From My Second Brain"


_SYSTEM_FRAMING = (
    "You are the script writer for a two-host audio overview, in the style of a "
    "short explanatory podcast. Two hosts — Host and Guest — discuss the source "
    "material below in a natural, curious, back-and-forth conversation. Host "
    "leads and asks questions; Guest adds insight and detail. Ground EVERY "
    "statement strictly in the provided material — never invent facts, names, "
    "numbers, or quotes. Keep each turn to two or three sentences."
)


def _format_group(index: int, group: BundleGroup) -> str:
    """Render one bundle group as a prompt section (entity names + summaries)."""
    lines = [f"Theme {index}:"]
    if group.entity_names:
        lines.append(f"- Key topics: {', '.join(group.entity_names)}")
    if group.summary:
        lines.append(f"- Summary: {group.summary}")
    for title, summary in group.docs:
        if summary:
            lines.append(f"- Source “{title}”: {summary}")
        else:
            lines.append(f"- Source “{title}”")
    return "\n".join(lines)


def build_prompt(
    bundle: SourceBundle,
    *,
    max_turns: int,
    max_input_tokens: int,
    count_tokens: TokenCounter,
) -> str:
    """Build the single user prompt for the dialogue call (privacy-bounded).

    Greedily includes bundle groups while the running prompt stays within
    ``max_input_tokens`` (measured by the injected ``count_tokens``); the first
    group is always included even if it alone exceeds the budget, so a prompt is
    never empty. The model is instructed to emit JSON ``{"turns": [...]}`` with
    at most ``max_turns`` alternating Host/Guest turns.
    """
    header = _SYSTEM_FRAMING
    instruction = (
        f"Write a dialogue of AT MOST {max_turns} turns, strictly alternating "
        f'"{HOST}" and "{GUEST}", starting with {HOST}. Respond with ONLY a JSON '
        'object of the form {"turns": [{"speaker": "Host", "text": "..."}, '
        '{"speaker": "Guest", "text": "..."}]}. Use no speaker names other than '
        f'"{HOST}" and "{GUEST}".'
    )

    included: list[str] = []
    for index, group in enumerate(bundle.groups, start=1):
        section = _format_group(index, group)
        candidate = "\n\n".join([header, *included, section, instruction])
        if included and count_tokens(candidate) > max_input_tokens:
            _logger.debug(
                "audio prompt: stopping at %d/%d groups (token budget %d)",
                len(included),
                len(bundle.groups),
                max_input_tokens,
            )
            break
        included.append(section)

    return "\n\n".join([header, *included, instruction])


# --------------------------------------------------------------------------- #
# Turn parsing / validation (pure)
# --------------------------------------------------------------------------- #
def _normalize_speaker(raw: object) -> str | None:
    """Map a raw speaker label to ``Host`` / ``Guest`` (or ``None`` if neither)."""
    if not isinstance(raw, str):
        return None
    token = raw.strip().lower()
    if token.startswith("host"):
        return HOST
    if token.startswith("guest"):
        return GUEST
    return None


def _parse_turns(response: dict[str, Any]) -> list[ScriptTurn]:
    """Validate the model response into a list of :class:`ScriptTurn`.

    Raises :class:`AudioError` (which the generator catches to trigger its one
    retry) when ``turns`` is not a list of ``{"speaker", "text"}`` objects with a
    resolvable speaker and non-empty text.
    """
    raw_turns = response.get("turns")
    if not isinstance(raw_turns, list) or not raw_turns:
        raise AudioError("script response 'turns' is not a non-empty list")
    turns: list[ScriptTurn] = []
    for item in raw_turns:
        if not isinstance(item, dict):
            raise AudioError(f"script turn is not an object: {item!r}")
        speaker = _normalize_speaker(item.get("speaker"))
        text = item.get("text")
        if speaker is None:
            raise AudioError(f"script turn has an invalid speaker: {item!r}")
        if not isinstance(text, str) or not text.strip():
            raise AudioError(f"script turn has empty/invalid text: {item!r}")
        turns.append(ScriptTurn(speaker=speaker, text=text.strip()))
    return turns


# --------------------------------------------------------------------------- #
# Script generator
# --------------------------------------------------------------------------- #
class ScriptGenerator:
    """Generates a :class:`PodcastScript` from a :class:`SourceBundle`.

    The LLM round-trip is injected as ``chat_fn`` (dependency inversion) so tests
    drive it with a fake; production wires :func:`brain.chat.chat_json` bound to
    ``cfg.audio_script_model`` via :func:`make_script_generator`. The generator
    owns the prompt build, the structural validation + one retry, and the
    surplus-turn truncation.
    """

    def __init__(
        self,
        *,
        chat_fn: ChatFn,
        model: str,
        max_turns: int,
        max_input_tokens: int,
        count_tokens: TokenCounter,
        num_predict: int = _DEFAULT_NUM_PREDICT,
    ) -> None:
        self._chat_fn = chat_fn
        self._model = model
        self._max_turns = max_turns
        self._max_input_tokens = max_input_tokens
        self._count_tokens = count_tokens
        self._num_predict = num_predict

    def generate(
        self, bundle: SourceBundle, *, title: str, generated_at: str
    ) -> PodcastScript:
        """Produce a script for ``bundle`` (title + ``generated_at`` injected)."""
        prompt = build_prompt(
            bundle,
            max_turns=self._max_turns,
            max_input_tokens=self._max_input_tokens,
            count_tokens=self._count_tokens,
        )
        turns = self._generate_turns(prompt)
        if len(turns) > self._max_turns:
            _logger.debug(
                "audio: truncating %d turns to max_turns=%d",
                len(turns),
                self._max_turns,
            )
            turns = turns[: self._max_turns]
        return PodcastScript(
            title=title,
            source_kind=bundle.kind,
            source_person=bundle.person,
            source_topic=bundle.topic,
            theme_count=bundle.theme_count,
            generated_at=generated_at,
            model=self._model,
            turns=turns,
        )

    def _generate_turns(self, prompt: str) -> list[ScriptTurn]:
        """Call ``chat_fn`` and validate; retry once on a structural failure."""
        last_error: AudioError | None = None
        for _ in (1, 2):
            response = self._chat_fn(prompt, _TURNS_SCHEMA, self._num_predict)
            try:
                return _parse_turns(response)
            except AudioError as exc:
                last_error = exc
                continue
        raise AudioError(
            f"script generation failed after 2 attempts: {last_error}"
        )


def _default_count_tokens(text: str) -> int:
    """Count tokens via tiktoken ``cl100k_base`` (the project-wide tokenizer)."""
    import tiktoken

    encoder = tiktoken.get_encoding("cl100k_base")
    return len(encoder.encode(text))


def make_script_generator(
    cfg: Config, *, max_turns: int | None = None
) -> ScriptGenerator:
    """Build the production :class:`ScriptGenerator` from ``cfg``.

    Wires :func:`brain.chat.chat_json` bound to ``cfg.audio_script_model`` (NOT
    ``cfg.enrich_model``) as the ``chat_fn``, the tiktoken token counter, and the
    audio caps. The chat helper handles JSON-mode + one transport-level retry;
    ``ScriptGenerator`` layers its own structural validation + retry on top.

    ``max_turns`` overrides ``cfg.audio_max_turns`` (the ``--turns`` CLI flag);
    the caller is responsible for validating it is a positive even integer.
    """

    def _chat(
        prompt: str, schema: dict[str, Any], num_predict: int
    ) -> dict[str, Any]:
        return chat_json(
            prompt,
            schema=schema,
            cfg=cfg,
            model=cfg.audio_script_model,
            num_predict=num_predict,
        )

    return ScriptGenerator(
        chat_fn=_chat,
        model=cfg.audio_script_model,
        max_turns=cfg.audio_max_turns if max_turns is None else max_turns,
        max_input_tokens=cfg.audio_max_input_tokens,
        count_tokens=_default_count_tokens,
    )


# --------------------------------------------------------------------------- #
# TTS pluggability (Wave B)
# --------------------------------------------------------------------------- #
class TtsBackend(Protocol):
    """Pluggable text-to-speech backend (mirrors the ``Embedder`` Protocol).

    ``synthesize`` receives the in-memory script plus the path to the already
    written ``.json`` artifact and the desired audio output path. Backends live
    OUTSIDE this repo (Claude skills ``venice-audio-speech`` / ``speech`` /
    ``fal-lip-sync``); :class:`ShellTtsBackend` is the shell bridge.
    """

    def synthesize(
        self,
        script: PodcastScript,
        script_json_path: Path,
        output_path: Path,
    ) -> None: ...


# How long a synthesis subprocess may run before it is reaped (seconds).
_DEFAULT_TTS_TIMEOUT = 300.0

# The ``shell:`` spec prefix selects :class:`ShellTtsBackend`.
_SHELL_PREFIX = "shell:"


class ShellTtsBackend:
    """Shells out to a user-supplied executable to synthesize audio.

    The command (everything after ``shell:``) is split into an argv list via
    :func:`shlex.split` at construction. At synthesis time the backend appends
    two positional arguments — the script JSON path and the output path — and
    runs the command with ``shell=False`` (the subprocess default), so paths
    containing spaces are passed safely as discrete argv entries (no quoting
    bugs, no shell injection).
    """

    def __init__(
        self, command_template: str, timeout: float = _DEFAULT_TTS_TIMEOUT
    ) -> None:
        argv = shlex.split(command_template)
        if not argv:
            raise TtsError("TTS shell command is empty")
        self._argv_template = argv
        self._timeout = timeout

    def synthesize(
        self,
        script: PodcastScript,
        script_json_path: Path,
        output_path: Path,
    ) -> None:
        """Run ``<command> <script_json_path> <output_path>`` (shell=False)."""
        argv = [*self._argv_template, str(script_json_path), str(output_path)]
        try:
            subprocess.run(argv, check=True, timeout=self._timeout)  # noqa: S603
        except FileNotFoundError as exc:
            raise TtsError(
                f"TTS command not found: {self._argv_template[0]!r}"
            ) from exc
        except subprocess.CalledProcessError as exc:
            raise TtsError(
                f"TTS command failed (exit {exc.returncode}): "
                f"{' '.join(self._argv_template)}"
            ) from exc
        except subprocess.TimeoutExpired as exc:
            raise TtsError(
                f"TTS command timed out after {self._timeout}s"
            ) from exc


def make_tts_backend(
    spec: str, *, timeout: float = _DEFAULT_TTS_TIMEOUT
) -> TtsBackend:
    """Build a :class:`TtsBackend` from a ``--tts`` spec string.

    Currently only the ``shell:<command>`` form is supported. Future registered
    aliases (e.g. ``venice``, ``elevenlabs``) can be added without touching the
    Protocol. An unrecognized spec raises :class:`TtsError`.
    """
    if spec.startswith(_SHELL_PREFIX):
        return ShellTtsBackend(spec[len(_SHELL_PREFIX) :], timeout=timeout)
    raise TtsError(
        f"unknown TTS backend spec: {spec!r} (expected 'shell:<command>')"
    )
