"""Configuration loading from environment / .env."""
import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from dotenv import dotenv_values, find_dotenv

DEFAULT_OLLAMA_HOST = "http://localhost:11434"
DEFAULT_QWEN3_MODEL = "qwen3-embedding:8b"
DEFAULT_EMBEDDER = "arctic"
_VALID_EMBEDDERS = {"arctic", "voyage", "qwen3"}

# How long Ollama keeps a model loaded in VRAM between requests. Passed as
# ``keep_alive`` in every outgoing Ollama HTTP payload (``/api/embed``,
# ``/api/chat``, ``/api/generate``). "30m" keeps the model hot across bursts of
# ingest/search; raise to "1h" if your workflow has longer idle gaps. Set via
# ``BRAIN_OLLAMA_KEEP_ALIVE``; accepts any format Ollama understands: a positive
# integer duration string ("30m", "1h", "60s") or a bare positive integer seconds
# string ("60"). Zero and negative values are rejected — they would unload the
# model between calls (defeating the purpose) and cause latency spikes.
DEFAULT_OLLAMA_KEEP_ALIVE = "30m"

# Accepts "30m", "1h", "60s", "60" etc. — any POSITIVE integer optionally
# followed by m/h/s. "0", "-1", empty, and non-numeric strings are rejected.
_KEEP_ALIVE_RE = re.compile(r"^([1-9]\d*)(m|h|s)?$")

# Cosine-similarity floor for the vector leg of hybrid search. Tuned
# empirically against the live corpus on 2026-05-06 (see Phase D of
# `docs/plans/2026-05-06-search-ranking-fix.md` and
# `tests/test_search_floor_default_excludes_known_bad.py`).
#
# Measurement: real Arctic embedding of the query ``person-x``
# vs. stored chunk embeddings on the live corpus.
#
# * Known-bad docs (interview prep / cheatsheet false positives):
#   max cosine <= 0.20.
# * True positives kept by acceptance criterion #1: Krisp meeting
#   ``3508c63e`` max 0.36; Gmail thread ``bc9f06c9`` max 0.27.
#
# The clean gap is (0.20, 0.27). A floor of 0.25 sits ``max_bad +
# 0.05`` (per plan revision #3) and stays comfortably below the
# 0.27 true-positive ceiling so neither acceptance-criterion doc is
# dropped from the vector leg. Override via ``BRAIN_VECTOR_SIM_FLOOR``.
DEFAULT_VECTOR_SIM_FLOOR = 0.25

# Default exponential-decay half-life for the recency boost applied after RRF.
# At 180 days a document is a year old (365 days) => boost ~0.25x. Tuned as
# a reasonable default for a personal corpus that spans years; override via
# ``BRAIN_RECENCY_HALFLIFE_DAYS``. Set to a very large value (e.g. 999999)
# to effectively disable the boost.
DEFAULT_RECENCY_HALFLIFE_DAYS = 180.0

# Default token budget for snippet-context expansion. After the best-matching
# chunk is selected, this many tokens of neighboring-chunk context are
# stitched around it to give Claude / the user richer reading context. Set to
# 0 to disable expansion. Override via ``BRAIN_SNIPPET_CONTEXT_TOKENS``.
DEFAULT_SNIPPET_CONTEXT_TOKENS = 200

# Default vault location -- clean, no implicit cloud sync. Users who want iCloud
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

# Wave Q1-D -- enrichment (auto-summary + auto-tag) defaults.
#
# ``DEFAULT_ENRICH_MODEL`` is the Ollama model name passed to ``/api/chat``.
# llama3.1:8b is the only model authorized by the roadmap intake brief for
# Q1-D. Users override via ``BRAIN_ENRICH_MODEL`` -- anything pullable via
# ``ollama pull <name>`` works as long as it supports JSON-mode output.
DEFAULT_ENRICH_MODEL = "llama3.1:8b"

# Min content tokens (tiktoken ``cl100k_base``) below which the post-ingest
# enrichment hook silently skips. A 50-token doc is roughly two sentences;
# the title alone is already a fine summary. Conservative default mirrors
# the planner's recommendation (D3 in the wave plan).
DEFAULT_ENRICH_MIN_TOKENS = 50

# Max content tokens fed to the model. llama3.1:8b has a 128K context window
# but we never need more than the opening of a doc to summarize it. Capping
# at 1200 tokens (~900 words, the doc head) keeps the LLM round-trip under
# ~2.5s on M-series silicon (was ~7s at 4000 tokens) and bounds the prompt
# cost while still covering the content that matters most for a 2-3 sentence
# summary. Override via ``BRAIN_ENRICH_MAX_INPUT_TOKENS`` when higher fidelity
# on very long docs is more important than throughput.
DEFAULT_ENRICH_MAX_INPUT_TOKENS = 1200

# Ollama HTTP timeout for ``/api/chat`` calls. 60 s gives headroom for a
# cold-model swap-in on first call without spiraling. Override via
# ``BRAIN_ENRICH_TIMEOUT_SECONDS``.
DEFAULT_ENRICH_TIMEOUT_SECONDS = 60.0

# Wave G1-c -- GraphRAG incremental sync (people aspect) settings.
#
# Graph sync is OPT-IN this wave: ``BRAIN_GRAPH_ENABLED`` defaults to False so
# existing deployments (and the prod DB, which predates the Apache AGE image)
# see no behavior change. When enabled AND the database actually ships AGE, a
# post-write / post-delete hook keeps the people graph in lock-step with the
# ``documents`` table (see :mod:`brain.graph_rag.sync`). The remaining knobs map
# 1:1 onto :class:`brain.graph_rag.reconcile.ReconcileConfig`; their defaults
# mirror the canonical constants in :mod:`brain.graph_rag.cooccur` /
# :mod:`brain.graph_rag.weighting` -- kept as literals here (not imported) so
# ``config`` stays import-cheap and free of any cycle with the graph package.
DEFAULT_GRAPH_ENABLED = False
DEFAULT_GRAPH_TENANT_ID = "default"
DEFAULT_GRAPH_COOCCUR_WINDOW = 3  # == brain.graph_rag.cooccur.DEFAULT_COOCCUR_WINDOW
DEFAULT_GRAPH_MAX_ENTITIES = 40  # == cooccur.DEFAULT_MAX_ENTITIES_PER_DOC
DEFAULT_GRAPH_GENERIC_DF_RATIO = 0.30  # == weighting.DEFAULT_GENERIC_DF

# Accepted spellings for the boolean ``BRAIN_GRAPH_ENABLED`` /
# ``BRAIN_GRAPH_CONCEPTS`` flags (compared case-insensitively after ``.strip()``).
_GRAPH_ENABLED_TRUTHY = frozenset({"1", "true", "yes", "on"})
_GRAPH_ENABLED_FALSY = frozenset({"0", "false", "no", "off"})

# Wave G2 -- GraphRAG concept extraction + bounded retrieval (spec §10). Parsed
# here in G2-a; the concept aspect (G2-b/c) and the local/themes retrieval
# surfaces (G2-d..i) consume them. Defaults follow spec §10 + Codex ruling Q4
# (``BRAIN_GRAPH_MAX_DEGREE`` = 50, ``BRAIN_GRAPH_MIN_EDGE_WEIGHT`` = 0.20).
DEFAULT_GRAPH_CONCEPTS = False
# Ollama model for the gated concept entity extractor (spec §3 D3: the default
# ``OllamaExtractor`` wraps ``enrichment.extract_entities()``). Mirrors the
# enrich-model default convention but kept a separate literal so the concept
# extractor and the summary enricher stay independently overridable -- ``config``
# carries no enrich<->graph coupling.
DEFAULT_GRAPH_EXTRACT_MODEL = "llama3.1:8b"
# Head-cap (tiktoken ``cl100k_base`` tokens) applied to a document body BEFORE the
# concept extractor chunks it (perf Fix C, 2026-05-24). The extractor calls the
# LLM once per ~1500-token chunk, so a long document generates a heavy tail of
# calls (the perf investigation measured 7.7% of docs driving ~33% of all calls).
# Capping the input to its first ~5-6 chunks bounds that tail while preserving
# recall on the bulk; mirrors the summary enricher's input head cap. ``0`` /
# ``none`` / ``unlimited`` disables the cap (whole document extracted) -- the
# escape hatch. A generous default of 8000 only trims the extreme tail.
DEFAULT_GRAPH_EXTRACT_MAX_INPUT_TOKENS = 8000
DEFAULT_GRAPH_DEPTH = 2  # spec §6 bounded variable-length traversal radius
DEFAULT_GRAPH_FRONTIER_CAP = 200  # spec §6 LIMIT on entities reached per seed
DEFAULT_GRAPH_MAX_DEGREE = 50  # Codex ruling Q4 -- per-node expansion fan-out cap
DEFAULT_GRAPH_MIN_EDGE_WEIGHT = 0.20  # Codex ruling Q4 -- normalized-lift floor
DEFAULT_GRAPH_THEME_LIMIT = 5  # spec §6b ranked ThemeGroup count

# Wave G3 -- global community detection (networkx Louvain) knobs (spec §17c).
# Parsed here in G3-a; the detection core (G3-b), the lazy/eager summaries
# (G3-c), and the global retrieval path (G3-d) consume them. §17c pins the
# migration-013 schema (Q1) and the perf budgets (Q8) but does NOT pin these
# tuning defaults, so the values below are CHOSEN to be consistent with the
# existing graph knobs and are documented as such in ``.env.example``.
DEFAULT_GRAPH_COMMUNITY_RESOLUTION = 1.0  # networkx louvain_communities() default
DEFAULT_GRAPH_COMMUNITY_SEED = 1234  # deterministic Louvain RNG seed (chosen)
DEFAULT_GRAPH_COMMUNITY_MIN_SIZE = 3  # min members to materialize a community (chosen)
DEFAULT_GRAPH_COMMUNITY_JACCARD = 0.5  # stable-identity match threshold (§17c Q3/Q7; chosen)
DEFAULT_GRAPH_COMMUNITY_LIMIT = 5  # global retrieval community count (== theme limit)
# Perf/ops safety valve: max communities materialized per tenant per build
# (bounds the summary + embedding cost behind the §17c Q8 budgets). ``None`` ==
# unlimited so the default is behavior-neutral; mirrors the int|none idiom of
# ``BRAIN_GRAPH_MAX_ENTITIES_PER_DOC``.
DEFAULT_GRAPH_COMMUNITY_MAX: int | None = None

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
    # Common mobile-app footers -- single line, terminated by EOL.
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
         a dev checkout -- use the repo root (dev backcompat).
      3. ~/.brain -- NOT created here; ``brain setup`` creates it lazily.

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
    mode in the rendered wiki -- the Quartz transformer reads
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
    # rejected at load time via :class:`ConfigError` -- a negative
    # threshold would silently flip the filter (no effective filtering)
    # and is almost certainly a config bug.
    people_hub_min_docs: int = DEFAULT_PEOPLE_HUB_MIN_DOCS
    # Exponential-decay half-life (days) for the recency boost applied after
    # RRF. ``boost = 0.5 ** (age_days / recency_halflife_days)`` where
    # ``age_days`` is clamped to [0, +inf) so future-dated rows get boost=1.0.
    # Loaded from ``BRAIN_RECENCY_HALFLIFE_DAYS``; must be a positive float.
    recency_halflife_days: float = DEFAULT_RECENCY_HALFLIFE_DAYS
    # Token budget for per-search snippet-context expansion. After the
    # best-matching chunk is selected, this many tokens of neighboring-chunk
    # context are stitched around it. 0 = disabled. Loaded from
    # ``BRAIN_SNIPPET_CONTEXT_TOKENS``; must be a non-negative integer.
    snippet_context_tokens: int = DEFAULT_SNIPPET_CONTEXT_TOKENS
    # Wave Q1-D -- per-document auto-summary + auto-tag enrichment.
    # The four fields are tightly coupled (they all feed ``OllamaEnricher``)
    # so they live together at the tail of the dataclass.
    enrich_model: str = DEFAULT_ENRICH_MODEL
    enrich_min_tokens: int = DEFAULT_ENRICH_MIN_TOKENS
    enrich_max_input_tokens: int = DEFAULT_ENRICH_MAX_INPUT_TOKENS
    enrich_timeout_seconds: float = DEFAULT_ENRICH_TIMEOUT_SECONDS
    # Per-request ``keep_alive`` sent in every outgoing Ollama payload (embedder
    # ``/api/embed``, enricher ``/api/chat``, extractor ``/api/generate``). Keeps
    # the model loaded in VRAM for this long after the last request, preventing
    # the cold-load latency spike on the next call. Accepts any format Ollama
    # understands: a positive integer duration string ("30m", "1h", "60s") or a
    # bare positive integer seconds string ("60"). Zero and negative values are
    # rejected at load time via ConfigError (they unload the model between calls).
    # Override via ``BRAIN_OLLAMA_KEEP_ALIVE``.
    ollama_keep_alive: str = DEFAULT_OLLAMA_KEEP_ALIVE
    # Wave G1-c -- GraphRAG people-aspect incremental sync. ``graph_enabled``
    # gates the post-write/delete reconcile hook; the other four resolve into
    # the single shared :class:`ReconcileConfig`
    # (:func:`brain.graph_rag.sync.build_reconcile_config`). ``owner_participants``
    # (above) is reused as the reconcile ``owner_keys`` -- the corpus owner is
    # stripped from the graph's person roster exactly as from the People Hub, so
    # there is no separate ``BRAIN_GRAPH_OWNER_*`` knob.
    graph_enabled: bool = DEFAULT_GRAPH_ENABLED
    graph_tenant_id: str = DEFAULT_GRAPH_TENANT_ID
    graph_cooccur_window: int = DEFAULT_GRAPH_COOCCUR_WINDOW
    graph_max_entities: int | None = DEFAULT_GRAPH_MAX_ENTITIES
    graph_generic_df_ratio: float = DEFAULT_GRAPH_GENERIC_DF_RATIO
    # Wave G2 -- concept extraction + bounded-retrieval knobs (spec §10). Parsed
    # in G2-a; consumed by the concept aspect (G2-b/c) and the local/themes
    # traversal (G2-d..i). ``graph_concepts`` gates the concept aspect;
    # ``graph_extract_model`` selects the Ollama extractor model; the remaining
    # five are the hard traversal caps + theme-ranking knob.
    graph_concepts: bool = DEFAULT_GRAPH_CONCEPTS
    graph_extract_model: str = DEFAULT_GRAPH_EXTRACT_MODEL
    # Concept-extractor input head cap (perf Fix C). ``None`` == no cap (whole
    # document extracted); a positive int caps the body to its first N
    # ``cl100k_base`` tokens before chunking. Threaded into
    # :func:`brain.graph_rag.extract.make_extractor`.
    graph_extract_max_input_tokens: int | None = DEFAULT_GRAPH_EXTRACT_MAX_INPUT_TOKENS
    graph_depth: int = DEFAULT_GRAPH_DEPTH
    graph_frontier_cap: int = DEFAULT_GRAPH_FRONTIER_CAP
    graph_max_degree: int = DEFAULT_GRAPH_MAX_DEGREE
    graph_min_edge_weight: float = DEFAULT_GRAPH_MIN_EDGE_WEIGHT
    graph_theme_limit: int = DEFAULT_GRAPH_THEME_LIMIT
    # Wave G3 -- global community detection (spec §17c). Parsed in G3-a;
    # consumed by detection (G3-b), summaries (G3-c), and global retrieval
    # (G3-d). ``graph_community_max`` is an ops safety cap (None == unlimited).
    graph_community_resolution: float = DEFAULT_GRAPH_COMMUNITY_RESOLUTION
    graph_community_seed: int = DEFAULT_GRAPH_COMMUNITY_SEED
    graph_community_min_size: int = DEFAULT_GRAPH_COMMUNITY_MIN_SIZE
    graph_community_jaccard: float = DEFAULT_GRAPH_COMMUNITY_JACCARD
    graph_community_limit: int = DEFAULT_GRAPH_COMMUNITY_LIMIT
    graph_community_max: int | None = DEFAULT_GRAPH_COMMUNITY_MAX
    # Phase 1 data-quality remediation (2026-05-23). Extra automated-sender
    # denylist entries (substrings or full addresses) layered on top of the
    # always-on generic heuristic (no-reply / notifications / mailer / …) used by
    # :func:`brain.wiki._person_name.is_automated_sender`. Loaded from
    # ``BRAIN_GRAPH_SENDER_DENYLIST`` (comma-separated); entries are trimmed,
    # lowercased, and de-duplicated at load time. Empty frozenset (default) means
    # only the generic heuristic runs. Threaded into both the People Hub
    # (``emit_people_pages``) and the graph reconcile (``ReconcileConfig``) so a
    # no-reply / org sender becomes a person in neither.
    graph_sender_denylist: frozenset[str] = frozenset()
    # Phase B (2026-05-25) — operator-curated concept extraction stopwords.
    # Entities whose ``canonical_key`` appears in this set are dropped by the
    # extractor's ``_finalize`` chokepoint (after the generic noise filter) even
    # when they pass presence validation. Real terms are employer-specific (rule
    # 15) so the default is **empty** — operators set
    # ``BRAIN_GRAPH_EXTRACT_STOPWORDS`` locally. Parsed as a comma-separated list;
    # entries are trimmed, lowercased, and de-duplicated at load time. Also folded
    # into :func:`brain.graph_rag.concepts.concept_inputs_hash` so a stopword
    # change re-extracts (beyond the one-time ``EXTRACTOR_VERSION`` bump).
    graph_extract_stopwords: frozenset[str] = frozenset()
    # Phase C (2026-05-25) — curated entity alias/merge rules. Real rules contain
    # real entity names (rule 15) so they live in a gitignored local file. This
    # path is resolved from ``BRAIN_GRAPH_ALIASES_PATH``; if unset it defaults to
    # ``$BRAIN_HOME/graph_aliases.yml`` when that file exists, else ``None``
    # (feature is opt-in — absent file → no aliases applied). Eager-validated:
    # when a path IS resolved it must be readable (raises ``ConfigError`` if not).
    graph_aliases_path: Path | None = None

    @classmethod
    def load(cls) -> "Config":
        """Load config from env / .env files. Raises ConfigError if DATABASE_URL is unset."""
        fields = cls._load_field_dict(require_db=True)
        return cls(**fields)

    @classmethod
    def load_minimal(cls) -> "Config":
        """Same as load() but doesn't require DATABASE_URL.

        Used by purely-filesystem commands (brain vault render --overlay,
        brain wiki install, brain claude install-skill) that run BEFORE
        brain setup writes .env. The brain_home field still resolves, but
        database_url defaults to an empty string sentinel and any caller
        that tries to actually USE database_url on this config will fail
        at the first DB connection attempt -- which is the right level to
        fail.
        """
        fields = cls._load_field_dict(require_db=False)
        return cls(**fields)

    @classmethod
    def _load_field_dict(cls, *, require_db: bool) -> dict[str, Any]:
        """Shared parser. Loads the dotenv chain, then parses every field.

        If require_db=True, raises ConfigError on missing DATABASE_URL;
        if False, sets it to "" (empty string sentinel). This is the single
        source of truth for all field parsing -- load() and load_minimal()
        both delegate here so overlapping fields can never drift.
        """
        # Load .env files using a merged-dict + setdefault algorithm so that:
        #   1. os.environ (process env) is NEVER overwritten -- highest priority.
        #   2. <repo-root>/.env wins over cwd and BRAIN_HOME .env files.
        #   3. <cwd>/.env (via walk-up) wins over BRAIN_HOME .env.
        #   4. $BRAIN_HOME/.env is the lowest-priority file source.
        #
        # Files are layered in REVERSE priority order (lowest first) into a
        # merged dict; higher-priority files overwrite lower-priority ones on
        # key collisions. Process env is applied last via os.environ.setdefault
        # so an existing value is never clobbered -- preserving the precedence
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
        if require_db and not database_url:
            raise ConfigError("DATABASE_URL is not set (see .env.example)")
        database_url = database_url or ""  # empty sentinel when require_db=False
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
        # P4.4 -- owner identity for the email-thread "Show only my replies"
        # filter. Optional; an unset/empty value renders the button but
        # the runtime filter no-ops (no message ever matches the empty
        # user identity, so toggling the button hides every section --
        # which is the right "I forgot to set this" feedback signal).
        # ``.strip()`` so trailing newlines from a `.env` quirk don't
        # bleed into the JS global.
        user_email_raw = os.environ.get("BRAIN_USER_EMAIL")
        user_email = (user_email_raw or "").strip() or None
        # Vector cosine floor -- see DEFAULT_VECTOR_SIM_FLOOR. Validation:
        # must parse as float in [0.0, 1.0] (cosine similarity range).
        # Negative values would silently re-admit the noise tail; >1
        # would exclude every chunk. Either is a config bug -- surface it
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
        # Owner participants -- identifiers (emails and/or display names)
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
        # People Hub doc-count threshold -- see DEFAULT_PEOPLE_HUB_MIN_DOCS.
        # Validation: must parse as a non-negative integer. A negative
        # threshold is almost certainly a config typo (it would render every
        # person, since ``len(docs) < negative`` is never true) -- surface
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
        # Recency half-life -- see DEFAULT_RECENCY_HALFLIFE_DAYS.
        # Validation: must parse as a positive float. Zero is invalid
        # (produces 0 ** inf = 0 for any finite age, degenerate). Negative
        # is invalid -- flips the decay direction so old docs score higher.
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
        # Snippet context tokens -- see DEFAULT_SNIPPET_CONTEXT_TOKENS.
        # Validation: must parse as a non-negative integer. 0 = disabled.
        # Negative is invalid -- there is no sensible semantic for a negative
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
        # Wave Q1-D -- enrichment env vars. Same validation pattern as the
        # snippet-context / recency-halflife knobs above: unset/blank ->
        # default; non-parseable / out-of-range -> ConfigError eagerly so a
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
                    f"(got {enrich_min_raw!r})"
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
                    f"(got {enrich_timeout_raw!r})"
                )

        # Ollama keep_alive -- see DEFAULT_OLLAMA_KEEP_ALIVE. Accepts a
        # positive integer optionally followed by m/h/s (e.g. "30m", "1h",
        # "60s", "60"). Zero, negative, empty, and non-numeric strings are
        # rejected via ConfigError so a config typo surfaces at startup, not
        # during an embed/chat call.
        keep_alive_raw = os.environ.get("BRAIN_OLLAMA_KEEP_ALIVE")
        if keep_alive_raw is None or keep_alive_raw.strip() == "":
            ollama_keep_alive = DEFAULT_OLLAMA_KEEP_ALIVE
        else:
            stripped_ka = keep_alive_raw.strip()
            if not _KEEP_ALIVE_RE.match(stripped_ka):
                raise ConfigError(
                    "BRAIN_OLLAMA_KEEP_ALIVE must be a positive integer or duration "
                    "string like '30m', '1h', '60s', '60' "
                    f"(got {keep_alive_raw!r})"
                )
            ollama_keep_alive = stripped_ka

        # Wave G1-c -- GraphRAG sync env vars. Same eager-validation pattern as
        # the enrich knobs above: unset/blank -> default; non-parseable /
        # out-of-range -> ConfigError so a typo surfaces at startup, never
        # mid-ingest.
        graph_enabled_raw = os.environ.get("BRAIN_GRAPH_ENABLED")
        if graph_enabled_raw is None or graph_enabled_raw.strip() == "":
            graph_enabled = DEFAULT_GRAPH_ENABLED
        else:
            token = graph_enabled_raw.strip().lower()
            if token in _GRAPH_ENABLED_TRUTHY:
                graph_enabled = True
            elif token in _GRAPH_ENABLED_FALSY:
                graph_enabled = False
            else:
                raise ConfigError(
                    "BRAIN_GRAPH_ENABLED must be one of "
                    "1/true/yes/on or 0/false/no/off "
                    f"(got {graph_enabled_raw!r})"
                )

        graph_tenant_raw = os.environ.get("BRAIN_GRAPH_TENANT")
        if graph_tenant_raw is None or graph_tenant_raw.strip() == "":
            graph_tenant_id = DEFAULT_GRAPH_TENANT_ID
        else:
            graph_tenant_id = graph_tenant_raw.strip()

        graph_window_raw = os.environ.get("BRAIN_GRAPH_COOCCUR_WINDOW")
        if graph_window_raw is None or graph_window_raw.strip() == "":
            graph_cooccur_window = DEFAULT_GRAPH_COOCCUR_WINDOW
        else:
            try:
                graph_cooccur_window = int(graph_window_raw)
            except ValueError as exc:
                raise ConfigError(
                    f"BRAIN_GRAPH_COOCCUR_WINDOW must be a positive integer "
                    f"(got {graph_window_raw!r})"
                ) from exc
            if graph_cooccur_window < 1:
                raise ConfigError(
                    f"BRAIN_GRAPH_COOCCUR_WINDOW must be a positive integer "
                    f"(got {graph_window_raw!r})"
                )

        # ``none`` / ``unlimited`` disables the per-doc cap (maps to ``None``,
        # which :class:`ReconcileConfig` accepts); otherwise a positive int.
        graph_max_raw = os.environ.get("BRAIN_GRAPH_MAX_ENTITIES_PER_DOC")
        graph_max_entities: int | None
        if graph_max_raw is None or graph_max_raw.strip() == "":
            graph_max_entities = DEFAULT_GRAPH_MAX_ENTITIES
        elif graph_max_raw.strip().lower() in {"none", "unlimited"}:
            graph_max_entities = None
        else:
            try:
                graph_max_entities = int(graph_max_raw)
            except ValueError as exc:
                raise ConfigError(
                    "BRAIN_GRAPH_MAX_ENTITIES_PER_DOC must be a positive integer "
                    f"or 'none' (got {graph_max_raw!r})"
                ) from exc
            if graph_max_entities < 1:
                raise ConfigError(
                    "BRAIN_GRAPH_MAX_ENTITIES_PER_DOC must be a positive integer "
                    f"or 'none' (got {graph_max_raw!r})"
                )

        graph_ratio_raw = os.environ.get("BRAIN_GRAPH_GENERIC_DF")
        if graph_ratio_raw is None or graph_ratio_raw.strip() == "":
            graph_generic_df_ratio = DEFAULT_GRAPH_GENERIC_DF_RATIO
        else:
            try:
                graph_generic_df_ratio = float(graph_ratio_raw)
            except ValueError as exc:
                raise ConfigError(
                    "BRAIN_GRAPH_GENERIC_DF must be a float in (0.0, 1.0] "
                    f"(got {graph_ratio_raw!r})"
                ) from exc
            if not (0.0 < graph_generic_df_ratio <= 1.0):
                raise ConfigError(
                    "BRAIN_GRAPH_GENERIC_DF must be a float in (0.0, 1.0] "
                    f"(got {graph_ratio_raw!r})"
                )

        # Wave G2 -- concept-extraction + bounded-retrieval env vars. Same
        # eager-validation idiom as the G1-c graph knobs above: unset/blank ->
        # default; non-parseable / out-of-range -> ConfigError at startup so a
        # typo never surfaces mid-retrieval.
        graph_concepts_raw = os.environ.get("BRAIN_GRAPH_CONCEPTS")
        if graph_concepts_raw is None or graph_concepts_raw.strip() == "":
            graph_concepts = DEFAULT_GRAPH_CONCEPTS
        else:
            token = graph_concepts_raw.strip().lower()
            if token in _GRAPH_ENABLED_TRUTHY:
                graph_concepts = True
            elif token in _GRAPH_ENABLED_FALSY:
                graph_concepts = False
            else:
                raise ConfigError(
                    "BRAIN_GRAPH_CONCEPTS must be one of "
                    "1/true/yes/on or 0/false/no/off "
                    f"(got {graph_concepts_raw!r})"
                )

        graph_extract_model_raw = os.environ.get("BRAIN_GRAPH_EXTRACT_MODEL")
        if (
            graph_extract_model_raw is None
            or graph_extract_model_raw.strip() == ""
        ):
            graph_extract_model = DEFAULT_GRAPH_EXTRACT_MODEL
        else:
            graph_extract_model = graph_extract_model_raw.strip()

        # Concept-extractor input head cap (perf Fix C). ``0`` / ``none`` /
        # ``unlimited`` disables the cap (maps to ``None``); otherwise a positive
        # int. Mirrors the ``BRAIN_GRAPH_MAX_ENTITIES_PER_DOC`` sentinel idiom,
        # additionally accepting ``0`` as the disable token per the perf plan.
        gem_raw = os.environ.get("BRAIN_GRAPH_EXTRACT_MAX_INPUT_TOKENS")
        graph_extract_max_input_tokens: int | None
        if gem_raw is None or gem_raw.strip() == "":
            graph_extract_max_input_tokens = DEFAULT_GRAPH_EXTRACT_MAX_INPUT_TOKENS
        elif gem_raw.strip().lower() in {"0", "none", "unlimited"}:
            graph_extract_max_input_tokens = None
        else:
            try:
                graph_extract_max_input_tokens = int(gem_raw.strip())
            except ValueError as exc:
                raise ConfigError(
                    "BRAIN_GRAPH_EXTRACT_MAX_INPUT_TOKENS must be a positive integer "
                    f"or 0/'none'/'unlimited' to disable (got {gem_raw!r})"
                ) from exc
            if graph_extract_max_input_tokens < 1:
                raise ConfigError(
                    "BRAIN_GRAPH_EXTRACT_MAX_INPUT_TOKENS must be a positive integer "
                    f"or 0/'none'/'unlimited' to disable (got {gem_raw!r})"
                )

        graph_depth_raw = os.environ.get("BRAIN_GRAPH_DEPTH")
        if graph_depth_raw is None or graph_depth_raw.strip() == "":
            graph_depth = DEFAULT_GRAPH_DEPTH
        else:
            try:
                graph_depth = int(graph_depth_raw)
            except ValueError as exc:
                raise ConfigError(
                    f"BRAIN_GRAPH_DEPTH must be a positive integer "
                    f"(got {graph_depth_raw!r})"
                ) from exc
            if graph_depth < 1:
                raise ConfigError(
                    f"BRAIN_GRAPH_DEPTH must be a positive integer "
                    f"(got {graph_depth_raw!r})"
                )

        graph_frontier_raw = os.environ.get("BRAIN_GRAPH_FRONTIER_CAP")
        if graph_frontier_raw is None or graph_frontier_raw.strip() == "":
            graph_frontier_cap = DEFAULT_GRAPH_FRONTIER_CAP
        else:
            try:
                graph_frontier_cap = int(graph_frontier_raw)
            except ValueError as exc:
                raise ConfigError(
                    f"BRAIN_GRAPH_FRONTIER_CAP must be a positive integer "
                    f"(got {graph_frontier_raw!r})"
                ) from exc
            if graph_frontier_cap < 1:
                raise ConfigError(
                    f"BRAIN_GRAPH_FRONTIER_CAP must be a positive integer "
                    f"(got {graph_frontier_raw!r})"
                )

        graph_max_degree_raw = os.environ.get("BRAIN_GRAPH_MAX_DEGREE")
        if graph_max_degree_raw is None or graph_max_degree_raw.strip() == "":
            graph_max_degree = DEFAULT_GRAPH_MAX_DEGREE
        else:
            try:
                graph_max_degree = int(graph_max_degree_raw)
            except ValueError as exc:
                raise ConfigError(
                    f"BRAIN_GRAPH_MAX_DEGREE must be a positive integer "
                    f"(got {graph_max_degree_raw!r})"
                ) from exc
            if graph_max_degree < 1:
                raise ConfigError(
                    f"BRAIN_GRAPH_MAX_DEGREE must be a positive integer "
                    f"(got {graph_max_degree_raw!r})"
                )

        graph_min_edge_raw = os.environ.get("BRAIN_GRAPH_MIN_EDGE_WEIGHT")
        if graph_min_edge_raw is None or graph_min_edge_raw.strip() == "":
            graph_min_edge_weight = DEFAULT_GRAPH_MIN_EDGE_WEIGHT
        else:
            try:
                graph_min_edge_weight = float(graph_min_edge_raw)
            except ValueError as exc:
                raise ConfigError(
                    "BRAIN_GRAPH_MIN_EDGE_WEIGHT must be a float in [0.0, 1.0] "
                    f"(got {graph_min_edge_raw!r})"
                ) from exc
            if not (0.0 <= graph_min_edge_weight <= 1.0):
                raise ConfigError(
                    "BRAIN_GRAPH_MIN_EDGE_WEIGHT must be a float in [0.0, 1.0] "
                    f"(got {graph_min_edge_raw!r})"
                )

        graph_theme_limit_raw = os.environ.get("BRAIN_GRAPH_THEME_LIMIT")
        if graph_theme_limit_raw is None or graph_theme_limit_raw.strip() == "":
            graph_theme_limit = DEFAULT_GRAPH_THEME_LIMIT
        else:
            try:
                graph_theme_limit = int(graph_theme_limit_raw)
            except ValueError as exc:
                raise ConfigError(
                    f"BRAIN_GRAPH_THEME_LIMIT must be a positive integer "
                    f"(got {graph_theme_limit_raw!r})"
                ) from exc
            if graph_theme_limit < 1:
                raise ConfigError(
                    f"BRAIN_GRAPH_THEME_LIMIT must be a positive integer "
                    f"(got {graph_theme_limit_raw!r})"
                )

        # Wave G3 -- global community-detection env vars (spec §17c). Same
        # eager-validation idiom as the G2 graph knobs above: unset/blank ->
        # default; non-parseable / out-of-range -> ConfigError at startup so a
        # typo never surfaces mid community-build.
        gc_resolution_raw = os.environ.get("BRAIN_GRAPH_COMMUNITY_RESOLUTION")
        if gc_resolution_raw is None or gc_resolution_raw.strip() == "":
            graph_community_resolution = DEFAULT_GRAPH_COMMUNITY_RESOLUTION
        else:
            try:
                graph_community_resolution = float(gc_resolution_raw)
            except ValueError as exc:
                raise ConfigError(
                    "BRAIN_GRAPH_COMMUNITY_RESOLUTION must be a positive float "
                    f"(got {gc_resolution_raw!r})"
                ) from exc
            if graph_community_resolution <= 0:
                raise ConfigError(
                    "BRAIN_GRAPH_COMMUNITY_RESOLUTION must be a positive float "
                    f"(got {gc_resolution_raw!r})"
                )

        gc_seed_raw = os.environ.get("BRAIN_GRAPH_COMMUNITY_SEED")
        if gc_seed_raw is None or gc_seed_raw.strip() == "":
            graph_community_seed = DEFAULT_GRAPH_COMMUNITY_SEED
        else:
            try:
                graph_community_seed = int(gc_seed_raw)
            except ValueError as exc:
                raise ConfigError(
                    "BRAIN_GRAPH_COMMUNITY_SEED must be a non-negative integer "
                    f"(got {gc_seed_raw!r})"
                ) from exc
            if graph_community_seed < 0:
                raise ConfigError(
                    "BRAIN_GRAPH_COMMUNITY_SEED must be a non-negative integer "
                    f"(got {gc_seed_raw!r})"
                )

        gc_min_size_raw = os.environ.get("BRAIN_GRAPH_COMMUNITY_MIN_SIZE")
        if gc_min_size_raw is None or gc_min_size_raw.strip() == "":
            graph_community_min_size = DEFAULT_GRAPH_COMMUNITY_MIN_SIZE
        else:
            try:
                graph_community_min_size = int(gc_min_size_raw)
            except ValueError as exc:
                raise ConfigError(
                    "BRAIN_GRAPH_COMMUNITY_MIN_SIZE must be a positive integer "
                    f"(got {gc_min_size_raw!r})"
                ) from exc
            if graph_community_min_size < 1:
                raise ConfigError(
                    "BRAIN_GRAPH_COMMUNITY_MIN_SIZE must be a positive integer "
                    f"(got {gc_min_size_raw!r})"
                )

        gc_jaccard_raw = os.environ.get("BRAIN_GRAPH_COMMUNITY_JACCARD")
        if gc_jaccard_raw is None or gc_jaccard_raw.strip() == "":
            graph_community_jaccard = DEFAULT_GRAPH_COMMUNITY_JACCARD
        else:
            try:
                graph_community_jaccard = float(gc_jaccard_raw)
            except ValueError as exc:
                raise ConfigError(
                    "BRAIN_GRAPH_COMMUNITY_JACCARD must be a float in [0.0, 1.0] "
                    f"(got {gc_jaccard_raw!r})"
                ) from exc
            if not (0.0 <= graph_community_jaccard <= 1.0):
                raise ConfigError(
                    "BRAIN_GRAPH_COMMUNITY_JACCARD must be a float in [0.0, 1.0] "
                    f"(got {gc_jaccard_raw!r})"
                )

        gc_limit_raw = os.environ.get("BRAIN_GRAPH_COMMUNITY_LIMIT")
        if gc_limit_raw is None or gc_limit_raw.strip() == "":
            graph_community_limit = DEFAULT_GRAPH_COMMUNITY_LIMIT
        else:
            try:
                graph_community_limit = int(gc_limit_raw)
            except ValueError as exc:
                raise ConfigError(
                    "BRAIN_GRAPH_COMMUNITY_LIMIT must be a positive integer "
                    f"(got {gc_limit_raw!r})"
                ) from exc
            if graph_community_limit < 1:
                raise ConfigError(
                    "BRAIN_GRAPH_COMMUNITY_LIMIT must be a positive integer "
                    f"(got {gc_limit_raw!r})"
                )

        # ``none`` / ``unlimited`` disables the per-tenant community cap (maps to
        # ``None``); otherwise a positive int. Mirrors
        # ``BRAIN_GRAPH_MAX_ENTITIES_PER_DOC``.
        gc_max_raw = os.environ.get("BRAIN_GRAPH_COMMUNITY_MAX")
        graph_community_max: int | None
        if gc_max_raw is None or gc_max_raw.strip() == "":
            graph_community_max = DEFAULT_GRAPH_COMMUNITY_MAX
        elif gc_max_raw.strip().lower() in {"none", "unlimited"}:
            graph_community_max = None
        else:
            try:
                graph_community_max = int(gc_max_raw)
            except ValueError as exc:
                raise ConfigError(
                    "BRAIN_GRAPH_COMMUNITY_MAX must be a positive integer "
                    f"or 'none' (got {gc_max_raw!r})"
                ) from exc
            if graph_community_max < 1:
                raise ConfigError(
                    "BRAIN_GRAPH_COMMUNITY_MAX must be a positive integer "
                    f"or 'none' (got {gc_max_raw!r})"
                )

        # Phase 1 -- extra automated-sender denylist entries. Same comma-split /
        # trim / lowercase / dedupe shape as ``BRAIN_OWNER_PARTICIPANTS``;
        # unset/blank yields an empty frozenset (generic heuristic only).
        sender_denylist_raw = os.environ.get("BRAIN_GRAPH_SENDER_DENYLIST", "")
        graph_sender_denylist = frozenset(
            piece
            for piece in (
                entry.strip().lower() for entry in sender_denylist_raw.split(",")
            )
            if piece
        )

        # Phase B (2026-05-25) -- operator-curated concept extraction stopwords.
        # Same comma-split / trim / lowercase / dedupe shape as
        # ``BRAIN_GRAPH_SENDER_DENYLIST``; default empty (real terms are
        # employer-specific, rule 15 — operators set this locally).
        stopwords_raw = os.environ.get("BRAIN_GRAPH_EXTRACT_STOPWORDS", "")
        graph_extract_stopwords = frozenset(
            piece
            for piece in (
                entry.strip().lower() for entry in stopwords_raw.split(",")
            )
            if piece
        )

        # Phase C (2026-05-25) -- curated entity alias rules path. Opt-in:
        # 1. ``BRAIN_GRAPH_ALIASES_PATH`` when set → use that path (expand user).
        # 2. Else ``$BRAIN_HOME/graph_aliases.yml`` when that file exists.
        # 3. Else ``None`` (feature off; ``load_alias_rules(None)`` returns ``[]``).
        # When a path is resolved, eager-validate it is readable (ConfigError if not).
        graph_aliases_path: Path | None
        aliases_env_raw = os.environ.get("BRAIN_GRAPH_ALIASES_PATH")
        if aliases_env_raw and aliases_env_raw.strip():
            graph_aliases_path = Path(aliases_env_raw.strip()).expanduser()
            try:
                graph_aliases_path.read_text(encoding="utf-8")
            except OSError as exc:
                raise ConfigError(
                    f"BRAIN_GRAPH_ALIASES_PATH is not readable: {graph_aliases_path} "
                    f"({exc})"
                ) from exc
        else:
            _default_aliases = _brain_home_root() / "graph_aliases.yml"
            graph_aliases_path = _default_aliases if _default_aliases.exists() else None

        return {
            # brain_home resolves via default_factory=_brain_home_root.
            "database_url": database_url,
            "ollama_host": ollama_host,
            "qwen3_model": qwen3_model,
            "embedder": embedder,
            "voyage_api_key": voyage_api_key,
            "vault_path": vault_path,
            "user_email": user_email,
            "vector_sim_floor": vector_sim_floor,
            "owner_participants": owner_participants,
            "people_hub_min_docs": people_hub_min_docs,
            "recency_halflife_days": recency_halflife_days,
            "snippet_context_tokens": snippet_context_tokens,
            "enrich_model": enrich_model,
            "enrich_min_tokens": enrich_min_tokens,
            "enrich_max_input_tokens": enrich_max_input_tokens,
            "enrich_timeout_seconds": enrich_timeout_seconds,
            "ollama_keep_alive": ollama_keep_alive,
            "graph_enabled": graph_enabled,
            "graph_tenant_id": graph_tenant_id,
            "graph_cooccur_window": graph_cooccur_window,
            "graph_max_entities": graph_max_entities,
            "graph_generic_df_ratio": graph_generic_df_ratio,
            "graph_concepts": graph_concepts,
            "graph_extract_model": graph_extract_model,
            "graph_extract_max_input_tokens": graph_extract_max_input_tokens,
            "graph_depth": graph_depth,
            "graph_frontier_cap": graph_frontier_cap,
            "graph_max_degree": graph_max_degree,
            "graph_min_edge_weight": graph_min_edge_weight,
            "graph_theme_limit": graph_theme_limit,
            "graph_community_resolution": graph_community_resolution,
            "graph_community_seed": graph_community_seed,
            "graph_community_min_size": graph_community_min_size,
            "graph_community_jaccard": graph_community_jaccard,
            "graph_community_limit": graph_community_limit,
            "graph_community_max": graph_community_max,
            "graph_sender_denylist": graph_sender_denylist,
            "graph_extract_stopwords": graph_extract_stopwords,
            "graph_aliases_path": graph_aliases_path,
        }
