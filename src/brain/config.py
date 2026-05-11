"""Configuration loading from environment / .env."""
import os
from dataclasses import dataclass, field
from pathlib import Path

from dotenv import dotenv_values, find_dotenv

DEFAULT_OLLAMA_HOST = "http://localhost:11434"
DEFAULT_QWEN3_MODEL = "qwen3-embedding:8b"
DEFAULT_EMBEDDER = "arctic"
_VALID_EMBEDDERS = {"arctic", "voyage", "qwen3"}

# Cosine-similarity floor for the vector leg of hybrid search. Tuned
# empirically against the live corpus on 2026-05-06 (see Phase D of
# `docs/plans/2026-05-06-search-ranking-fix.md` and
# `tests/test_search_floor_default_excludes_known_bad.py`).
#
# Measurement: real Arctic embedding of the query ``person-x``
# vs. stored chunk embeddings on the live corpus.
#
# * Known-bad docs (interview prep / cheatsheet false positives):
#   max cosine ≤ 0.20.
# * True positives kept by acceptance criterion #1: Krisp meeting
#   ``3508c63e`` max 0.36; Gmail thread ``bc9f06c9`` max 0.27.
#
# The clean gap is (0.20, 0.27). A floor of 0.25 sits ``max_bad +
# 0.05`` (per plan revision #3) and stays comfortably below the
# 0.27 true-positive ceiling so neither acceptance-criterion doc is
# dropped from the vector leg. Override via ``BRAIN_VECTOR_SIM_FLOOR``.
DEFAULT_VECTOR_SIM_FLOOR = 0.25

# Default exponential-decay half-life for the recency boost applied after RRF.
# At 180 days a document is a year old (365 days) → boost ≈ 0.25×. Tuned as
# a reasonable default for a personal corpus that spans years; override via
# ``BRAIN_RECENCY_HALFLIFE_DAYS``. Set to a very large value (e.g. 999999)
# to effectively disable the boost.
DEFAULT_RECENCY_HALFLIFE_DAYS = 180.0

# Default token budget for snippet-context expansion. After the best-matching
# chunk is selected, this many tokens of neighboring-chunk context are
# stitched around it to give Claude / the user richer reading context. Set to
# 0 to disable expansion. Override via ``BRAIN_SNIPPET_CONTEXT_TOKENS``.
DEFAULT_SNIPPET_CONTEXT_TOKENS = 200

# Default vault location — clean, no implicit cloud sync. Users who want iCloud
# can either symlink ``~/brain-vault`` to an iCloud Drive folder or set
# ``BRAIN_VAULT_PATH`` to an iCloud path.
DEFAULT_VAULT_PATH = Path.home() / "brain-vault"

# Default doc-count threshold for the People Hub (``brain people``,
# ``<vault>/people/``). Persons with fewer than this many documents whose
# names are NOT pinned in ``<vault>/_people.yml`` are filtered out. The
# default of 3 keeps the long-tail of one-off cc'd recipients from each
# getting their own page; curated ``_people.yml`` entries always render
# regardless. Override via ``BRAIN_PEOPLE_HUB_MIN_DOCS``.
DEFAULT_PEOPLE_HUB_MIN_DOCS = 3

# Wave Q1-D — enrichment (auto-summary + auto-tag) defaults.
#
# ``DEFAULT_ENRICH_MODEL`` is the Ollama model name passed to ``/api/chat``.
# llama3.1:8b is the only model authorized by the roadmap intake brief for
# Q1-D. Users override via ``BRAIN_ENRICH_MODEL`` — anything pullable via
# ``ollama pull <name>`` works as long as it supports JSON-mode output.
DEFAULT_ENRICH_MODEL = "llama3.1:8b"

# Min content tokens (tiktoken ``cl100k_base``) below which the post-ingest
# enrichment hook silently skips. A 50-token doc is roughly two sentences;
# the title alone is already a fine summary. Conservative default mirrors
# the planner's recommendation (D3 in the wave plan).
DEFAULT_ENRICH_MIN_TOKENS = 50

# Max content tokens fed to the model. llama3.1:8b has a 128K context window
# but we never need more than the opening of a doc to summarize it. Capping
# at 4K keeps the LLM round-trip predictable (~3-8 s on M-series silicon)
# and bounds the prompt cost.
DEFAULT_ENRICH_MAX_INPUT_TOKENS = 4000

# Ollama HTTP timeout for ``/api/chat`` calls. 60 s gives headroom for a
# cold-model swap-in on first call without spiraling. Override via
# ``BRAIN_ENRICH_TIMEOUT_SECONDS``.
DEFAULT_ENRICH_TIMEOUT_SECONDS = 60.0

# Boilerplate regex patterns stripped from email bodies during Gmail ingest.
# Compiled with ``re.MULTILINE | re.IGNORECASE`` in
# :func:`brain.ingest.gmail.strip_boilerplate`. Default-deny for ``re.DOTALL``;
# patterns that genuinely need cross-line matching opt in with the inline
# ``(?s)`` flag and document why. Cross-line matches MUST be bounded by a
# non-greedy lookahead (``.*?(?=\n\n|\Z)``) so a single notice block can be
# absorbed without devouring the rest of the message.
#
# Single-source-of-truth: tweak the list here, no code change required.
BOILERPLATE_PATTERNS: tuple[str, ...] = (
    # Common mobile-app footers — single line, terminated by EOL.
    r"^Sent from my (iPhone|iPad|Android|BlackBerry|Windows Phone)\.?$",
    r"^Get Outlook for (iOS|Android)\s*<https?://[^>]+>\s*$",
    # Confidentiality / corporate-disclaimer footers run multiple lines.
    # ``(?s)`` enables DOTALL on this pattern only so ``.*?`` can cross
    # newlines; the ``(?=\n\n|\Z)`` lookahead caps the match at the next
    # blank line (or EOF) so we don't eat into legitimate downstream content.
    r"(?s)^CONFIDENTIALITY NOTICE:.*?(?=\n\n|\Z)",
    r"(?s)^This email and any attachments are confidential.*?(?=\n\n|\Z)",
)


def _project_dotenv() -> Path:
    """Path to the .env file at the repo root, relative to this module.

    config.py lives at <repo>/src/brain/config.py, so the repo root is two
    parents up from this file's directory.
    """
    return Path(__file__).resolve().parent.parent.parent / ".env"


def _brain_home_root(_config_file: Path | None = None) -> Path:
    """Resolve the $BRAIN_HOME directory per T1.1 priority order.

    Priority:
      1. $BRAIN_HOME env var (expanduser applied).
      2. Repo-root walk-up: if <three-levels-up>/pyproject.toml exists, that's
         a dev checkout — use the repo root (dev backcompat).
      3. ~/.brain — NOT created here; ``brain setup`` creates it lazily.

    The optional ``_config_file`` parameter is a test-only seam: production
    code never passes it.  Tests exercise the dev-checkout branch by passing a
    synthetic path whose three-levels-up ancestor contains ``pyproject.toml``.
    """
    env = os.environ.get("BRAIN_HOME")
    if env:
        return Path(env).expanduser()
    config_file = _config_file or Path(__file__).resolve()
    repo_root = config_file.parent.parent.parent
    if (repo_root / "pyproject.toml").is_file():
        return repo_root
    return Path.home() / ".brain"


def _brain_home_dotenv() -> Path:
    """Path to $BRAIN_HOME/.env (resolved via _brain_home_root)."""
    return _brain_home_root() / ".env"


class ConfigError(RuntimeError):
    pass


@dataclass(frozen=True)
class Config:
    """Project configuration loaded from environment / .env.

    ``embedder`` selects the embedding backend at setup time (one of
    ``arctic`` / ``voyage`` / ``qwen3``). ``voyage_api_key`` is only
    consulted when ``embedder == "voyage"``; for the other backends it can
    be ``None``.

    ``user_email`` (P4.4) is the owner's primary email address, optionally
    set via ``BRAIN_USER_EMAIL``. Consumed by the email-thread reading
    mode in the rendered wiki — the Quartz transformer reads
    ``process.env.BRAIN_USER_EMAIL`` at build time and bakes it into a
    ``window.BRAIN_USER_EMAIL`` global so the runtime "Show only my
    replies" filter knows whose ``From:`` address counts as the user's.
    Empty string / ``None`` disables the filter (button still renders;
    matching no-ops).
    """

    database_url: str
    brain_home: Path = field(default_factory=_brain_home_root)
    ollama_host: str = DEFAULT_OLLAMA_HOST
    qwen3_model: str = DEFAULT_QWEN3_MODEL
    embedder: str = DEFAULT_EMBEDDER
    voyage_api_key: str | None = None
    vault_path: Path = DEFAULT_VAULT_PATH
    user_email: str | None = None
    vector_sim_floor: float = DEFAULT_VECTOR_SIM_FLOOR
    # Comma-separated list of identifiers (emails AND/OR display names) that
    # count as the corpus owner. Stripped from ``DocSnapshot.participant_keys``
    # before R2 (``shared_participant``) and R3 (``same_day_participant``)
    # evaluate, so a meeting/email isn't linked to every other doc the owner
    # is on. Loaded from ``BRAIN_OWNER_PARTICIPANTS``; entries are trimmed,
    # lowercased, and de-duplicated at load time so downstream comparisons
    # can be a single ``.lower() in owner_participants`` check. Empty
    # frozenset (default) is a fast-path no-op.
    owner_participants: frozenset[str] = frozenset()
    # Minimum number of associated documents required for a non-curated
    # person to render a ``/people/<slug>`` hub page. Curated entries
    # (anyone in ``<vault>/_people.yml``) always render regardless of
    # this threshold. Loaded from ``BRAIN_PEOPLE_HUB_MIN_DOCS``; default
    # is :data:`DEFAULT_PEOPLE_HUB_MIN_DOCS`. Negative values are
    # rejected at load time via :class:`ConfigError` — a negative
    # threshold would silently flip the filter (no effective filtering)
    # and is almost certainly a config bug.
    people_hub_min_docs: int = DEFAULT_PEOPLE_HUB_MIN_DOCS
    # Exponential-decay half-life (days) for the recency boost applied after
    # RRF. ``boost = 0.5 ** (age_days / recency_halflife_days)`` where
    # ``age_days`` is clamped to [0, +∞) so future-dated rows get boost=1.0.
    # Loaded from ``BRAIN_RECENCY_HALFLIFE_DAYS``; must be a positive float.
    recency_halflife_days: float = DEFAULT_RECENCY_HALFLIFE_DAYS
    # Token budget for per-search snippet-context expansion. After the
    # best-matching chunk is selected, this many tokens of neighboring-chunk
    # context are stitched around it. 0 = disabled. Loaded from
    # ``BRAIN_SNIPPET_CONTEXT_TOKENS``; must be a non-negative integer.
    snippet_context_tokens: int = DEFAULT_SNIPPET_CONTEXT_TOKENS
    # Wave Q1-D — per-document auto-summary + auto-tag enrichment.
    # The four fields are tightly coupled (they all feed ``OllamaEnricher``)
    # so they live together at the tail of the dataclass.
    enrich_model: str = DEFAULT_ENRICH_MODEL
    enrich_min_tokens: int = DEFAULT_ENRICH_MIN_TOKENS
    enrich_max_input_tokens: int = DEFAULT_ENRICH_MAX_INPUT_TOKENS
    enrich_timeout_seconds: float = DEFAULT_ENRICH_TIMEOUT_SECONDS

    @classmethod
    def load(cls) -> "Config":
        # Load .env files using a merged-dict + setdefault algorithm so that:
        #   1. os.environ (process env) is NEVER overwritten — highest priority.
        #   2. <repo-root>/.env wins over cwd and BRAIN_HOME .env files.
        #   3. <cwd>/.env (via walk-up) wins over BRAIN_HOME .env.
        #   4. $BRAIN_HOME/.env is the lowest-priority file source.
        #
        # Files are layered in REVERSE priority order (lowest first) into a
        # merged dict; higher-priority files overwrite lower-priority ones on
        # key collisions. Process env is applied last via os.environ.setdefault
        # so an existing value is never clobbered — preserving the precedence
        # contract regardless of who set it (shell, parent process, or
        # monkeypatch.setenv).
        merged: dict[str, str] = {}
        cwd_env_str = find_dotenv(usecwd=True)
        for candidate in (
            _brain_home_dotenv(),
            Path(cwd_env_str) if cwd_env_str else None,
            _project_dotenv(),
        ):
            if candidate is not None and candidate.exists():
                merged.update(
                    {k: v for k, v in dotenv_values(candidate).items() if v is not None}
                )
        for key, value in merged.items():
            os.environ.setdefault(key, value)
        database_url = os.environ.get("DATABASE_URL")
        if not database_url:
            raise ConfigError("DATABASE_URL is not set (see .env.example)")
        ollama_host = os.environ.get("OLLAMA_HOST", DEFAULT_OLLAMA_HOST)
        qwen3_model = os.environ.get("QWEN3_MODEL", DEFAULT_QWEN3_MODEL)
        embedder = os.environ.get("BRAIN_EMBEDDER", DEFAULT_EMBEDDER).lower()
        if embedder not in _VALID_EMBEDDERS:
            raise ConfigError(
                f"BRAIN_EMBEDDER must be one of: arctic, voyage, qwen3 "
                f"(got {embedder!r})"
            )
        voyage_api_key = os.environ.get("VOYAGE_API_KEY")
        vault_path_env = os.environ.get("BRAIN_VAULT_PATH")
        vault_path = (
            Path(vault_path_env).expanduser()
            if vault_path_env
            else DEFAULT_VAULT_PATH
        )
        # P4.4 — owner identity for the email-thread "Show only my replies"
        # filter. Optional; an unset/empty value renders the button but
        # the runtime filter no-ops (no message ever matches the empty
        # user identity, so toggling the button hides every section —
        # which is the right "I forgot to set this" feedback signal).
        # ``.strip()`` so trailing newlines from a `.env` quirk don't
        # bleed into the JS global.
        user_email_raw = os.environ.get("BRAIN_USER_EMAIL")
        user_email = (user_email_raw or "").strip() or None
        # Vector cosine floor — see DEFAULT_VECTOR_SIM_FLOOR. Validation:
        # must parse as float in [0.0, 1.0] (cosine similarity range).
        # Negative values would silently re-admit the noise tail; >1
        # would exclude every chunk. Either is a config bug — surface it
        # eagerly with ``ConfigError`` (per plan revision #6).
        floor_raw = os.environ.get("BRAIN_VECTOR_SIM_FLOOR")
        if floor_raw is None or floor_raw.strip() == "":
            vector_sim_floor = DEFAULT_VECTOR_SIM_FLOOR
        else:
            try:
                vector_sim_floor = float(floor_raw)
            except ValueError as exc:
                raise ConfigError(
                    f"BRAIN_VECTOR_SIM_FLOOR must be a float in [0.0, 1.0] "
                    f"(got {floor_raw!r})"
                ) from exc
            if not (0.0 <= vector_sim_floor <= 1.0):
                raise ConfigError(
                    f"BRAIN_VECTOR_SIM_FLOOR must be a float in [0.0, 1.0] "
                    f"(got {vector_sim_floor!r})"
                )
        # Owner participants — identifiers (emails and/or display names)
        # whose presence in a doc's participant set is treated as
        # ``corpus owner``. Comma-separated; trim + lowercase + drop empty
        # entries at load time so the downstream filter is a fast
        # ``key.lower() in owner_participants`` check. Unset / blank /
        # whitespace-only strings produce an empty frozenset (no
        # behavioral change).
        owner_raw = os.environ.get("BRAIN_OWNER_PARTICIPANTS", "")
        owner_participants = frozenset(
            piece
            for piece in (entry.strip().lower() for entry in owner_raw.split(","))
            if piece
        )
        # People Hub doc-count threshold — see DEFAULT_PEOPLE_HUB_MIN_DOCS.
        # Validation: must parse as a non-negative integer. A negative
        # threshold is almost certainly a config typo (it would render every
        # person, since ``len(docs) < negative`` is never true) — surface
        # eagerly via ConfigError so the user fixes the typo rather than
        # silently getting a hub flooded with one-off recipients.
        people_min_raw = os.environ.get("BRAIN_PEOPLE_HUB_MIN_DOCS")
        if people_min_raw is None or people_min_raw.strip() == "":
            people_hub_min_docs = DEFAULT_PEOPLE_HUB_MIN_DOCS
        else:
            try:
                people_hub_min_docs = int(people_min_raw)
            except ValueError as exc:
                raise ConfigError(
                    f"BRAIN_PEOPLE_HUB_MIN_DOCS must be a non-negative integer "
                    f"(got {people_min_raw!r})"
                ) from exc
            if people_hub_min_docs < 0:
                raise ConfigError(
                    f"BRAIN_PEOPLE_HUB_MIN_DOCS must be a non-negative integer "
                    f"(got {people_hub_min_docs!r})"
                )
        # Recency half-life — see DEFAULT_RECENCY_HALFLIFE_DAYS.
        # Validation: must parse as a positive float. Zero is invalid
        # (produces 0 ** inf = 0 for any finite age, degenerate). Negative
        # is invalid — flips the decay direction so old docs score higher.
        halflife_raw = os.environ.get("BRAIN_RECENCY_HALFLIFE_DAYS")
        if halflife_raw is None or halflife_raw.strip() == "":
            recency_halflife_days = DEFAULT_RECENCY_HALFLIFE_DAYS
        else:
            try:
                recency_halflife_days = float(halflife_raw)
            except ValueError as exc:
                raise ConfigError(
                    f"BRAIN_RECENCY_HALFLIFE_DAYS must be a positive float "
                    f"(got {halflife_raw!r})"
                ) from exc
            if recency_halflife_days <= 0:
                raise ConfigError(
                    f"BRAIN_RECENCY_HALFLIFE_DAYS must be a positive float "
                    f"(got {recency_halflife_days!r})"
                )
        # Snippet context tokens — see DEFAULT_SNIPPET_CONTEXT_TOKENS.
        # Validation: must parse as a non-negative integer. 0 = disabled.
        # Negative is invalid — there is no sensible semantic for a negative
        # token budget.
        ctx_raw = os.environ.get("BRAIN_SNIPPET_CONTEXT_TOKENS")
        if ctx_raw is None or ctx_raw.strip() == "":
            snippet_context_tokens = DEFAULT_SNIPPET_CONTEXT_TOKENS
        else:
            try:
                snippet_context_tokens = int(ctx_raw)
            except ValueError as exc:
                raise ConfigError(
                    f"BRAIN_SNIPPET_CONTEXT_TOKENS must be a non-negative integer "
                    f"(got {ctx_raw!r})"
                ) from exc
            if snippet_context_tokens < 0:
                raise ConfigError(
                    f"BRAIN_SNIPPET_CONTEXT_TOKENS must be a non-negative integer "
                    f"(got {snippet_context_tokens!r})"
                )
        # Wave Q1-D — enrichment env vars. Same validation pattern as the
        # snippet-context / recency-halflife knobs above: unset/blank →
        # default; non-parseable / out-of-range → ConfigError eagerly so a
        # config typo surfaces at startup instead of mid-ingest.
        enrich_model_raw = os.environ.get("BRAIN_ENRICH_MODEL")
        if enrich_model_raw is None or enrich_model_raw.strip() == "":
            enrich_model = DEFAULT_ENRICH_MODEL
        else:
            enrich_model = enrich_model_raw.strip()

        enrich_min_raw = os.environ.get("BRAIN_ENRICH_MIN_TOKENS")
        if enrich_min_raw is None or enrich_min_raw.strip() == "":
            enrich_min_tokens = DEFAULT_ENRICH_MIN_TOKENS
        else:
            try:
                enrich_min_tokens = int(enrich_min_raw)
            except ValueError as exc:
                raise ConfigError(
                    f"BRAIN_ENRICH_MIN_TOKENS must be a non-negative integer "
                    f"(got {enrich_min_raw!r})"
                ) from exc
            if enrich_min_tokens < 0:
                raise ConfigError(
                    f"BRAIN_ENRICH_MIN_TOKENS must be a non-negative integer "
                    f"(got {enrich_min_tokens!r})"
                )

        enrich_max_raw = os.environ.get("BRAIN_ENRICH_MAX_INPUT_TOKENS")
        if enrich_max_raw is None or enrich_max_raw.strip() == "":
            enrich_max_input_tokens = DEFAULT_ENRICH_MAX_INPUT_TOKENS
        else:
            try:
                enrich_max_input_tokens = int(enrich_max_raw)
            except ValueError as exc:
                raise ConfigError(
                    f"BRAIN_ENRICH_MAX_INPUT_TOKENS must be a positive integer "
                    f"(got {enrich_max_raw!r})"
                ) from exc
            if enrich_max_input_tokens <= 0:
                raise ConfigError(
                    f"BRAIN_ENRICH_MAX_INPUT_TOKENS must be a positive integer "
                    f"(got {enrich_max_input_tokens!r})"
                )

        enrich_timeout_raw = os.environ.get("BRAIN_ENRICH_TIMEOUT_SECONDS")
        if enrich_timeout_raw is None or enrich_timeout_raw.strip() == "":
            enrich_timeout_seconds = DEFAULT_ENRICH_TIMEOUT_SECONDS
        else:
            try:
                enrich_timeout_seconds = float(enrich_timeout_raw)
            except ValueError as exc:
                raise ConfigError(
                    f"BRAIN_ENRICH_TIMEOUT_SECONDS must be a positive float "
                    f"(got {enrich_timeout_raw!r})"
                ) from exc
            if enrich_timeout_seconds <= 0:
                raise ConfigError(
                    f"BRAIN_ENRICH_TIMEOUT_SECONDS must be a positive float "
                    f"(got {enrich_timeout_seconds!r})"
                )

        return cls(
            brain_home=_brain_home_root(),
            database_url=database_url,
            ollama_host=ollama_host,
            qwen3_model=qwen3_model,
            embedder=embedder,
            voyage_api_key=voyage_api_key,
            vault_path=vault_path,
            user_email=user_email,
            vector_sim_floor=vector_sim_floor,
            owner_participants=owner_participants,
            people_hub_min_docs=people_hub_min_docs,
            recency_halflife_days=recency_halflife_days,
            snippet_context_tokens=snippet_context_tokens,
            enrich_model=enrich_model,
            enrich_min_tokens=enrich_min_tokens,
            enrich_max_input_tokens=enrich_max_input_tokens,
            enrich_timeout_seconds=enrich_timeout_seconds,
        )
