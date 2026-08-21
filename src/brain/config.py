"""Configuration loading from environment / .env.

Over the 800-line ceiling (CLAUDE.md): one env-loading surface, and each
knob carries the measurement block that justifies its default in prose.
Those evidence blocks are the bulk of the file and the reason to split it
next -- into ``config.py`` plus a measurements/rationale doc -- not a
reason to scatter the knobs.

**Why it grew again (2026-08-20, +43 vs e6d6e47, all comments):** two documented bounds
were imprecise as written -- ``BRAIN_RECALL_MAX_BUDGET_TOKENS``' ratio range
was scoped to a corpus when it is really scoped to a BUDGET, and
``BRAIN_SHOW_MAX_CONTENT_TOKENS``' "2x" read as a payload guarantee when it
bounds the two FIELDS. Both blocks now carry the arithmetic. Corrections, not
features.
"""
import os
import re
from dataclasses import dataclass, field
from io import StringIO
from pathlib import Path
from typing import Any

from dotenv import dotenv_values, find_dotenv

from .errors import missing_database_url_message

DEFAULT_OLLAMA_HOST = "http://localhost:11434"
DEFAULT_QWEN3_MODEL = "qwen3-embedding:8b"
DEFAULT_EMBEDDER = "arctic"
# ``none`` is the FTS-only backend (see :class:`brain.embeddings.NullEmbedder`):
# a user with no Ollama gets a working brain (ingest + lexical search + passing
# doctor) instead of crashes. It produces no vectors, so hybrid search degrades
# to the FTS leg.
_VALID_EMBEDDERS = {"arctic", "voyage", "qwen3", "none"}

# How long Ollama keeps a model loaded in VRAM between requests. Passed as
# ``keep_alive`` in every outgoing Ollama HTTP payload (``/api/embed``,
# ``/api/chat``, ``/api/generate``). "30m" keeps the model hot across bursts of
# ingest/search; raise to "1h" if your workflow has longer idle gaps, or set
# "-1" to keep the model loaded indefinitely (never unloaded). Set via
# ``BRAIN_OLLAMA_KEEP_ALIVE``; accepts any format Ollama understands: a positive
# integer duration string ("30m", "1h", "60s"), a bare positive integer seconds
# string ("60"), "-1" (keep loaded forever), or "0" (unload immediately after
# each call). The shipped default stays "30m" so a machine shared with other GPU
# workloads frees VRAM on idle; on a dedicated box, "-1" eliminates the measured
# 5,028ms cold-embed latency cliff that hits the first embed once the model has
# gone idle past the keep_alive window. Other malformed values are rejected.
DEFAULT_OLLAMA_KEEP_ALIVE = "30m"

# Accepts "30m", "1h", "60s", "60" etc. — any POSITIVE integer optionally
# followed by m/h/s — plus the two Ollama sentinels "-1" (keep loaded
# indefinitely) and "0" (unload immediately). Empty / whitespace-only strings
# fall back to the default; every other malformed value ("-2", "-1m", "0m",
# "abc", "1.5m", …) is rejected at load time.
_KEEP_ALIVE_RE = re.compile(r"^(-1|0|[1-9]\d*(?:m|h|s)?)$")


def keep_alive_wire_value(keep_alive: str) -> str | int:
    """Coerce the numeric keep_alive sentinels to JSON numbers for Ollama.

    Ollama's ``/api/embed`` and ``/api/chat`` accept ``keep_alive`` as either
    a duration STRING with a unit (``"30m"``, ``"1h"``) or a bare JSON NUMBER
    of seconds (``-1`` = keep the model loaded indefinitely, ``0`` = unload
    immediately). They reject the unit-less strings ``"-1"`` / ``"0"`` with
    HTTP 400 (``time: missing unit in duration "-1"``). Config validation
    accepts ``"-1"`` / ``"0"`` as the documented sentinels, so EVERY Ollama
    payload site must translate through this one function; unit-bearing
    strings pass through unchanged.
    """
    if keep_alive in ("-1", "0"):
        return int(keep_alive)
    return keep_alive

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

# Wave 4 (agentic token reduction) -- the hard outer character cap on a
# stitched search snippet, previously the inlined constant
# ``4 x brain.search.SNIPPET_LENGTH``. 1600 is exactly that value, so the
# default is numerically unchanged.
#
# This is the ONLY knob Wave 4 left behind, and deliberately so. The wave's
# actual subject -- an Otsu cut over neighbour relevance, behind
# ``BRAIN_SNIPPET_ADAPTIVE`` / ``BRAIN_SNIPPET_SCORE_FLOOR`` -- was built,
# measured on the live corpus, and REMOVED: it engaged on 74.5% of results and
# changed zero bytes of the delivered payload on 55 of 55, because this cap
# (against a ~2,281-char median chunk) truncates the matched chunk itself on
# 47 of 55 results, long before neighbour selection gets a say. Making the
# binding constraint configurable is the durable half of that finding. Full
# write-up in :mod:`brain.snippet_context`'s module docstring.
DEFAULT_SNIPPET_MAX_CHARS = 1600

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

# Plan 09 -- quick-capture inbox (`brain capture`) defaults.
#
# Number of leading words from the body used to build the auto-title slug when
# ``--title`` is omitted. Override via ``BRAIN_CAPTURE_TITLE_WORDS``.
DEFAULT_CAPTURE_TITLE_WORDS = 6
# Inbox size strictly above which `brain doctor` warns the capture queue is
# backing up (count > threshold; consumed in Plan 09 Phase 3). Override via
# ``BRAIN_CAPTURE_INBOX_WARN_THRESHOLD``.
DEFAULT_CAPTURE_INBOX_WARN_THRESHOLD = 20

# Plan 10 -- `brain review weekly` periodic-synthesis defaults. All three are
# positive-integer display caps validated the same way as the capture knobs.
#
# Max activity / ingested documents listed per weekly report section.
DEFAULT_REVIEW_ACTIVITY_LIMIT = 20
# Max theme clusters (graph communities or tag clusters) per weekly report.
DEFAULT_REVIEW_THEME_LIMIT = 5
# Max open-loop (action item) rows listed per weekly report.
DEFAULT_REVIEW_OPEN_LOOP_LIMIT = 20

# Plan 01 -- `brain brief` proactive daily-digest defaults. All four are
# positive-integer knobs validated the same way as the capture knobs.
#
# Default capture window (hours) for the brief's "recent captures" section.
DEFAULT_BRIEF_SINCE_HOURS = 24
# Default open-todo window (days) for the brief's "open action items" section.
DEFAULT_BRIEF_TODO_SINCE_DAYS = 7
# Max recent-capture rows listed in the brief.
DEFAULT_BRIEF_CAPTURE_LIMIT = 20
# Max pinned-doc rows listed in the brief.
DEFAULT_BRIEF_PIN_LIMIT = 10

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

# Plan 02 -- spaced-repetition resurfacing (`brain resurface`) defaults.
#
# ``DEFAULT_RESURFACE_LIMIT`` is the number of docs surfaced per run when
# ``--limit`` is omitted. Override via ``BRAIN_RESURFACE_LIMIT`` (int >= 1).
DEFAULT_RESURFACE_LIMIT = 7
# Documents younger than this many days are excluded from the queue before
# scoring -- brand-new notes don't need resurfacing. Override via
# ``BRAIN_RESURFACE_MIN_AGE_DAYS`` (int >= 0).
DEFAULT_RESURFACE_MIN_AGE_DAYS = 14
# Exponential half-life (days) for the age factor: at this age the age factor
# is 0.5, saturating toward 1.0 for much older docs. Reuses the ``0.5^(t/hl)``
# decay shape from search.py, inverted so older = higher priority. Override via
# ``BRAIN_RESURFACE_AGE_HALFLIFE_DAYS`` (float > 0).
DEFAULT_RESURFACE_AGE_HALFLIFE_DAYS = 180.0
# Exponential half-life (days) for the access-staleness factor: a doc last
# opened this many days ago contributes 0.5. Shorter than the age half-life so
# a recent open visibly deprioritizes a doc. Override via
# ``BRAIN_RESURFACE_ACCESS_HALFLIFE_DAYS`` (float > 0).
DEFAULT_RESURFACE_ACCESS_HALFLIFE_DAYS = 90.0

# Wave G1-c -- GraphRAG incremental sync (people aspect) settings.
#
# Graph sync is OPT-IN by origin; ``BRAIN_GRAPH_ENABLED`` now defaults to True so
# new deployments get graph retrieval out of the box. Deployments on stock pgvector
# (no AGE) are safe: GraphSyncer.reconcile is best-effort + never-raises. When
# the AGE image is present, a post-write/post-delete hook keeps the graph in sync with
# ``documents`` (see :mod:`brain.graph_rag.sync`). The remaining knobs map
# 1:1 onto :class:`brain.graph_rag.reconcile.ReconcileConfig`; defaults
# mirror the canonical constants in :mod:`brain.graph_rag.cooccur` /
# :mod:`brain.graph_rag.weighting` -- kept as literals here (not imported) so
# ``config`` stays import-cheap and free of any cycle with the graph package.
DEFAULT_GRAPH_ENABLED = True
DEFAULT_GRAPH_TENANT_ID = "default"
DEFAULT_GRAPH_COOCCUR_WINDOW = 3  # == brain.graph_rag.cooccur.DEFAULT_COOCCUR_WINDOW
DEFAULT_GRAPH_MAX_ENTITIES = 40  # == cooccur.DEFAULT_MAX_ENTITIES_PER_DOC
DEFAULT_GRAPH_GENERIC_DF_RATIO = 0.30  # == weighting.DEFAULT_GENERIC_DF

# Accepted truthy spellings for every boolean env flag in this module (compared
# case-insensitively after ``.strip()``). Single definition so a simple opt-in
# flag (``BRAIN_IGNORE_CWD_DOTENV``) and the tri-state graph flags below agree
# on what "true" looks like.
_TRUTHY_ENV_VALUES = frozenset({"1", "true", "yes", "on"})

# Accepted spellings for the boolean ``BRAIN_GRAPH_ENABLED`` /
# ``BRAIN_GRAPH_CONCEPTS`` flags. These parse tri-state (truthy / falsy /
# ConfigError), unlike the simple opt-in flags which only test truthiness.
_GRAPH_ENABLED_TRUTHY = _TRUTHY_ENV_VALUES
_GRAPH_ENABLED_FALSY = frozenset({"0", "false", "no", "off"})

# Wave G2 -- GraphRAG concept extraction + bounded retrieval (spec §10). Parsed
# here in G2-a; the concept aspect (G2-b/c) and the local/themes retrieval
# surfaces (G2-d..i) consume them. Defaults follow spec §10 + Codex ruling Q4
# (``BRAIN_GRAPH_MAX_DEGREE`` = 50, ``BRAIN_GRAPH_MIN_EDGE_WEIGHT`` = 0.20).
DEFAULT_GRAPH_CONCEPTS = True
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

# Tacit-knowledge elicitation knobs (feat/tacit-knowledge-elicitation).
#
# ``DEFAULT_ELICIT_MIN_EVIDENCE_DOCS`` — minimum number of evidence documents
# required before a gap is eligible for surfacing. A gap with fewer supporting
# docs is too thin to act on; conservative default of 3 mirrors the People Hub
# threshold.
DEFAULT_ELICIT_MIN_EVIDENCE_DOCS = 3

# Minimum gap score (in [0.0, 1.0]) for a gap to enter the queue. Below this
# threshold the gap is computed but silently discarded. Default 0.3 keeps
# low-confidence noise out while still surfacing moderate signals.
DEFAULT_ELICIT_MIN_GAP_SCORE = 0.3

# Maximum number of open gaps returned by ``brain elicit list`` in a single
# query. Keeps the CLI output manageable; override via
# ``BRAIN_ELICIT_QUEUE_LIMIT``.
DEFAULT_ELICIT_QUEUE_LIMIT = 20

# Gates the contradiction detector. Off by default because contradiction
# detection requires at least ``elicit_contradiction_min_docs`` documents for
# a target and can produce false positives on sparse corpora.
DEFAULT_ELICIT_CONTRADICTION_ENABLED = False

# Minimum documents for a target before contradiction detection runs on it.
# Ignored when ``elicit_contradiction_enabled`` is False.
DEFAULT_ELICIT_CONTRADICTION_MIN_DOCS = 5

# Plan 05 -- `brain timeline` (temporal evolution) knobs.
#
# ``DEFAULT_TIMELINE_GRANULARITY`` is the default time-bucket width; one of
# ``auto`` / ``month`` / ``quarter`` / ``year``. ``auto`` (the default) picks the
# coarsest of {year, quarter, month} that yields >=3 non-empty buckets for the
# matched docs' date span — a fixed ``quarter`` collapsed a young (few-month)
# corpus into a single bucket, hiding all evolution. An explicit value forces
# that granularity exactly. Override via ``BRAIN_TIMELINE_GRANULARITY``.
DEFAULT_TIMELINE_GRANULARITY = "auto"
_VALID_TIMELINE_GRANULARITIES = frozenset({"auto", "month", "quarter", "year"})

# Max buckets returned by ``brain timeline`` before the sparse tail is trimmed.
# Positive int. Override via ``BRAIN_TIMELINE_LIMIT``.
DEFAULT_TIMELINE_LIMIT = 20

# Max number of densest buckets that get an Ollama ``--synthesize`` summary.
# Non-negative int; ``0`` disables synthesis even when ``--synthesize`` is
# passed (keeps the LLM round-trips bounded). Override via
# ``BRAIN_TIMELINE_SYNTH_LIMIT``.
DEFAULT_TIMELINE_SYNTH_LIMIT = 5

# Which end of the timeline is trimmed when ``--limit`` is hit: ``oldest``
# (drop the earliest buckets, keep the most recent) or ``sparsest`` (drop the
# buckets with the fewest docs). Override via ``BRAIN_TIMELINE_TRIM``.
DEFAULT_TIMELINE_TRIM = "oldest"
_VALID_TIMELINE_TRIMS = frozenset({"oldest", "sparsest"})

# Plan 07 -- `brain connect` proactive auto-link suggestion knobs.
#
# ``DEFAULT_CONNECT_MIN_SCORE`` is the RRF-blend confidence floor: candidate
# pairs scoring below this are silently discarded (no DB write). Tuned against
# a live ~1.3k-doc corpus (2026-06-10): the original 0.30 floor admitted
# essentially every candidate (6.2k pending — the per-doc cap, not the floor,
# was binding); 0.60 keeps ~1.5 high-signal suggestions per doc. Lower via
# ``BRAIN_CONNECT_MIN_SCORE`` for more recall; must be a float in (0.0, 1.0].
DEFAULT_CONNECT_MIN_SCORE = 0.60
# Per-source-doc cap on candidate targets pulled from EACH leg (graph +
# embedding) before the RRF blend. Bounds the per-doc cost. Override via
# ``BRAIN_CONNECT_CANDIDATE_LIMIT``; must be an integer >= 1.
DEFAULT_CONNECT_CANDIDATE_LIMIT = 50
# Max suggestions persisted per source doc after gating + ranking. Keeps the
# review queue focused. Override via ``BRAIN_CONNECT_MAX_PER_DOC``; integer >= 1.
DEFAULT_CONNECT_MAX_PER_DOC = 5
# Plan 03 -- `brain review scan` contradiction + staleness knobs.
# Conflict scan caps: max entity candidates per run, max doc pairs adjudicated
# per surviving entity, and the embedding cosine floor below which a pair is too
# topically distant to possibly contradict. The product of the first two bounds
# the worst-case LLM call budget (default 30 x 3 = 90).
DEFAULT_REVIEW_CONFLICT_LIMIT = 30
DEFAULT_REVIEW_CONFLICT_PAIRS_PER_ENTITY = 3
DEFAULT_REVIEW_EMBED_SIM_FLOOR = 0.40
# Staleness scan: a doc older than ``stale_age_days`` is a candidate; a newer
# doc ingested within ``stale_supersede_window_days`` sharing an entity and with
# cosine >= ``stale_sim_floor`` supersedes it. ``stale_limit`` caps candidates.
DEFAULT_REVIEW_STALE_AGE_DAYS = 365
DEFAULT_REVIEW_STALE_SUPERSEDE_WINDOW_DAYS = 90
DEFAULT_REVIEW_STALE_SIM_FLOOR = 0.60
DEFAULT_REVIEW_STALE_LIMIT = 200
# Plan 06 -- `brain ask` agentic-synthesis knobs.
#
# ``DEFAULT_ASK_MAX_ITERATIONS`` is the hard cap on plan/retrieve/reflect loop
# iterations when ``--no-loop`` is not given. 3 covers the initial plan plus two
# reflect-driven follow-up rounds -- enough for multi-hop coverage without
# unbounded Ollama latency. Override via ``BRAIN_ASK_MAX_ITERATIONS`` (int >= 1).
DEFAULT_ASK_MAX_ITERATIONS = 3

# Max documents retrieved per sub-query per iteration. Mirrors the search
# ``--limit`` default. Override via ``BRAIN_ASK_DOCS_PER_ITER`` (int >= 1).
DEFAULT_ASK_DOCS_PER_ITER = 5

# Ollama HTTP timeout (seconds) for each ask plan/reflect/synthesize chat call.
# 90 s gives headroom for a cold-model swap-in on the first (plan) call plus the
# longer synthesize completion without spiraling. Override via
# ``BRAIN_ASK_TIMEOUT_SECONDS`` (float > 0).
DEFAULT_ASK_TIMEOUT_SECONDS = 90.0
# Plan 04 -- `brain audio` (NotebookLM-style two-host overview) knobs.
#
# ``DEFAULT_AUDIO_SCRIPT_MODEL`` is the Ollama model name passed to the shared
# ``chat_json`` helper for dialogue generation. Kept a separate literal from the
# enrich / extract models so the audio script model stays independently
# overridable via ``BRAIN_AUDIO_SCRIPT_MODEL``.
DEFAULT_AUDIO_SCRIPT_MODEL = "llama3.1:8b"
# Max speaker turns in the generated dialogue. Must be a POSITIVE EVEN integer
# (pairs of Host/Guest). The generator truncates any surplus turns the model
# emits beyond this cap. Override via ``BRAIN_AUDIO_MAX_TURNS``.
DEFAULT_AUDIO_MAX_TURNS = 12
# Max prompt tokens (tiktoken ``cl100k_base``) fed to the script generator. The
# bundle (entity names + theme summaries + doc titles/summaries — never raw
# bodies) is trimmed group-by-group to fit this budget before the LLM call,
# bounding the round-trip cost. Override via ``BRAIN_AUDIO_MAX_INPUT_TOKENS``.
DEFAULT_AUDIO_MAX_INPUT_TOKENS = 3000
# Max theme / community groups pulled from the graph context into the source
# bundle. Positive int. Override via ``BRAIN_AUDIO_THEME_LIMIT``.
DEFAULT_AUDIO_THEME_LIMIT = 4

# Plan 08 -- `brain gaps` search-failure-driven knowledge-gap detection knobs.
# ``DEFAULT_GAPS_LOOKBACK_DAYS`` is the mining window for the ``search_queries``
# log; ``DEFAULT_GAPS_MIN_CLUSTER_SIZE`` is the minimum query-occurrence count
# before a failed-query cluster surfaces as a gap. Both are positive integers
# validated like the other int knobs. Override via ``BRAIN_GAPS_LOOKBACK_DAYS``
# / ``BRAIN_GAPS_MIN_CLUSTER_SIZE``.
DEFAULT_GAPS_LOOKBACK_DAYS = 30
DEFAULT_GAPS_MIN_CLUSTER_SIZE = 2


# ---------------------------------------------------------------------------
# Pre-landed scaffolding (Task 0B, docs/plans/2026-07-25-agent-memory-safety-ui).
#
# No feature reads these yet. They are declared up-front in Wave 0 so this
# module has exactly ONE writer for the whole release -- five parallel worktrees
# would otherwise all need to append fields here and would collide on every
# merge. A later wave that finds a knob missing escalates to the coordinator
# rather than editing config.py.
# ---------------------------------------------------------------------------

# F4 -- ingest-time secret guard mode. ``warn`` logs a detection and ingests
# anyway (the default: never lose content to a false positive), ``redact``
# masks the match before storage, ``reject`` refuses the document with
# :class:`brain.errors.SecretGuardError`, and ``off`` skips scanning entirely.
# Override via ``BRAIN_SECRET_GUARD``; an unrecognized value is a
# :class:`ConfigError` at load time.
DEFAULT_SECRET_GUARD = "warn"
_VALID_SECRET_GUARDS = frozenset({"warn", "redact", "reject", "off"})

# F10 -- `brain recall` budgets. ``recall_budget_tokens`` caps the whole recall
# payload handed back to an agent, ``recall_passage_tokens`` caps each
# individual passage inside it, and ``recall_max_candidates`` bounds how many
# documents are considered before trimming to budget. All positive ints (>= 1)
# validated by :func:`_parse_positive_int_env`. Override via
# ``BRAIN_RECALL_BUDGET_TOKENS`` / ``BRAIN_RECALL_PASSAGE_TOKENS`` /
# ``BRAIN_RECALL_MAX_CANDIDATES``.
DEFAULT_RECALL_BUDGET_TOKENS = 2000
DEFAULT_RECALL_PASSAGE_TOKENS = 120
DEFAULT_RECALL_MAX_CANDIDATES = 25

# Wave 3 (agentic token reduction) -- MCP payload ceilings. Each bounds a
# single MCP response so no one tool call can eat a large fraction of an
# agent's context window. Enforced in :mod:`brain.mcp_limits`, which raises
# with the ceiling named rather than truncating silently.
#
# EVERY default here is a judgement call sized off live-corpus percentiles, not
# a law. That is why each is an env var.
#
# ``BRAIN_SHOW_MAX_CONTENT_TOKENS`` -- 0 = unlimited (the operator opt-out; it
# uses :func:`_parse_non_negative_int_env`). The live corpus p95 body is
# ~58,900 chars (~14.7k tokens), so 25000 leaves the typical bad case untouched
# while capping the tail -- the largest live document MEASURED (2026-08-13,
# read-only on prod) at 67,410 tokens / 266,888 chars, so the ceiling cuts the
# worst case by ~63%.
#
# Do not "correct" this to 74,258. A completion audit asserted that figure; it
# was re-measured directly and is wrong. 67,410 is the number, and it agrees
# with the independently committed
# docs/audits/2026-08-11-wave2-routing-counterfactual.md.
#
# It bounds ``documents.summary`` as well as the body, and not only under
# ``summary_only=true``. The summary is a ``TEXT`` column (migration 011) with
# no length constraint -- short only because ``OllamaEnricher`` writes it that
# way -- so leaving it out would have made ``summary_only``, the escape hatch
# FROM this ceiling, the one payload with no ceiling. Consequence stated rather
# than buried: ``content`` and ``summary`` are EACH bounded by this value, so
# the two FIELDS together total at most 2x it. One knob, one bound, where there
# was none.
#
# THE FIELDS, NOT THE PAYLOAD (corrected 2026-08-20; QA read the old wording as
# a payload guarantee, which it was not). The serialized response also carries
# title, tags, source_path, ids and -- when a cut happened -- the recovery-marker
# prose. Measured at ``max_content_tokens=500``: the two fields totalled exactly
# 1,000 tokens and the whole payload was 1,226, i.e. ~226 tokens of fixed
# overhead, proportionally larger at smaller caps. Size a context window against
# 2x this value PLUS that overhead, not against 2x alone.
#
# Bounding the SUM instead -- so the knob's name promised exactly what it
# delivered -- was considered and rejected. It makes the two fields COMPETE for
# one budget, and whichever is measured first wins: a long body would silently
# starve the summary, or the reverse, depending on evaluation order. That turns
# a stated cap into an order-dependent one, and the field an agent loses is the
# one it cannot tell it lost. Per-field is the weaker guarantee and the
# predictable one, and the asymmetry is written down here rather than
# discovered. If the 2x ever actually binds, the fix is a second knob for the
# summary, NOT a shared budget.
DEFAULT_SHOW_MAX_CONTENT_TOKENS = 25000
# == ``brain.search.CANDIDATE_LIMIT``: above it a larger ``limit`` cannot
# produce more documents anyway.
DEFAULT_SEARCH_MAX_LIMIT = 50
# `brain_recall` delivers roughly 2.2x its ``budget_tokens`` because the MCP
# payload ships every passage TWICE -- once structured in ``passages[].text``
# and again rendered inside ``context_block``. Measured on 11 live queries at
# ``budget_tokens=2000``: 4,025-4,726 delivered tokens (2.01x-2.36x, mean
# 2.23x). The intended bound is ~32000 DELIVERED tokens (~16% of a 200k
# window), so the accepted budget is 32000 / 2.36 (the measured worst case)
# ~= 13,500 -> 13000.
#
# THE 2.36 DIVISOR IS AN EMPIRICAL CONSTANT FROM AN 11-QUERY SAMPLE, NOT A LAW.
# Re-derive it -- or delete it and restore 32000 -- the moment the
# double-render is removed; a stale divisor would then under-bound by half.
#
# PROVENANCE, so the next reader can get back to the measurement:
#   artifact  docs/audits/2026-08-10-token-payload-baseline.json
#   harness   scripts/token_payload_report.py (`measure_recall`, which builds
#             `to_dict()` + `context_block` exactly as `mcp_server.brain_recall`
#             does) over scripts/token_payload_queries.txt, embedder=arctic
#   sample    11 queries, all at ``budget_tokens=2000``: 4,025-4,726 delivered
#             tokens, i.e. 2.01x-2.36x, mean 2.23x
#
# RE-MEASURED 2026-08-13 against the same 11 queries on the live corpus, after
# this wave landed -- docs/audits/2026-08-13-token-payload-after-wave3.json.
# Every query came in EXACTLY +8 tokens (total 49,132 -> 49,220, +0.18%), which
# is the additive `payload_tokens` key this wave added and nothing else. The
# worst case therefore moved 2.3630 -> 2.3670, a hair ABOVE the 2.36 this
# constant is derived from. Stated plainly because it is the kind of drift that
# gets rounded away: the bound still holds with margin --
#     13000 x 2.3670 = 30,771  <= 32,000   (the intended delivered bound)
#     32000 / 2.3670 = 13,519  >= 13,000   (this constant)
# -- so 13000 stands. If a future re-measurement pushes the worst case past
# 2.4615 (= 32000/13000), it does NOT and this constant must come down.
#
# THE RANGE IS SCOPED TO ITS MEASUREMENT, AND THE SCOPE IS THE BUDGET, NOT JUST
# THE CORPUS (added 2026-08-20 after end-to-end QA measured 2.44x and read it
# as the range being wrong). 2.01x-2.36x is what 11 live queries cost AT
# ``budget_tokens=2000``. QA measured 1,466 delivered tokens at
# ``budget_tokens=600`` -- 2.4433x -- on short synthetic passages. Both are
# right, and the gap is the mechanism this block already describes: delivered
# ~= 2 x used + overhead, so the RATIO is 2 + overhead/budget and rises as the
# budget FALLS. Same code, r = 2.3670 at 2000 and 2.4433 at 600.
#
# The ceiling still holds, two ways:
#     13000 x 2.4433 = 31,763  <= 32,000   (substituting QA's ratio directly)
#     32000 / 2.4433 = 13,097  >= 13,000   (adopting it as the divisor)
# and 2.4433 is still under the 2.4615 break point above. The margin is thin
# (237 tokens, 0.7%) ONLY under the direct substitution, which is the wrong
# arithmetic: applying a 600-budget ratio at a 13,000 budget over-states the
# overshoot by 21.7x on the fixed-overhead term. Do not widen the range to
# absorb 2.44 -- that would imply 2.44 was measured under the same conditions,
# and would move the input the divisor is derived from for no reason. Re-derive
# only from a measurement taken AT the ceiling.
#
# SCALE CAVEAT -- the ratio is MEASURED at 2000 and APPLIED at 13000, 6.5x
# away. That extrapolation errs safe, and here is why: the overshoot is ~2x
# structural duplication (every passage ships in `passages[].text` AND again
# inside `context_block`) plus a roughly FIXED JSON envelope. The envelope is a
# larger fraction of a small payload, so the ratio should FALL toward the ~2.0
# duplication floor as the budget rises -- 2.36 is a worst case that gets more
# conservative, not less, at 13000. It was not re-measured at 13000; if the
# duplication is ever removed or a passage's rendering changes, re-run the
# harness at the ceiling itself rather than trusting this reasoning.
DEFAULT_RECALL_MAX_BUDGET_TOKENS = 13000
# MEASURED, not estimated: the plan's "500 x ~10 tokens/entity ~= 5k" is wrong.
# The live graph serializes at ~37 tokens/entity (entities carry `description`),
# so all 6,589 cost 246,724 tokens and 500 cost 17,672 (re-measured
# read-only on prod 2026-08-13; the plan's "~66k" was off by 3.7x). 500 is kept anyway --
# it bounds a deliberate opt-in ask (the DEFAULT `limit` is 50, ~1.8k tokens)
# and still cuts the worst case by 92.8%. Lower it if 17.7k is too much for one
# admin call; that is what the env var is for.
#
# HOW TO REPRODUCE, because one digit of these depends on it: cl100k_base over
# ``json.dumps(entities, ensure_ascii=False)`` -- the house convention (see
# `payload_tokens` in mcp_server.py and scripts/token_payload_report.py) and
# what the MCP wire actually ships. json.dumps' ASCII-escaping DEFAULT gives
# 247,070 / 17,711 instead, because non-ASCII in entity names and descriptions
# escapes to a 6-char \uXXXX. Both are "measured"; only one is the payload.
# Do not "correct" 246,724 to 247,070 without also changing the serialization
# named here. (50 entities cost 1,750 either way -- a sample that agrees under
# both is not evidence the method matches.)
DEFAULT_GRAPH_ENTITIES_MAX_LIMIT = 500
# Covers the live maximum outgoing-link count (166) without special-casing.
DEFAULT_MCP_ROWS_MAX_LIMIT = 200
# Admin listing of materialized communities. Deliberately NOT
# ``DEFAULT_GRAPH_COMMUNITY_LIMIT`` (5), which governs retrieval-time global
# theme selection in :mod:`brain.graph_rag.global_` -- overloading it would
# couple an admin view to a ranking knob.
DEFAULT_GRAPH_COMMUNITIES_LIST_LIMIT = 25

# F10 -- the agent-id grammar: one alphanumeric leading character followed by up
# to 63 more alphanumerics / dot / underscore / colon / hyphen (64 chars total).
# Leading punctuation is rejected so an id can never be confused with a CLI flag.
#
# DELIBERATELY inlined here rather than imported: ``brain.agent`` does not exist
# until Wave 4, and config.py must stay importable without it. Its Wave-4
# sibling ``brain.agent.normalize_agent_id`` validates against this SAME public
# constant, so the two can never drift.
AGENT_ID_PATTERN = r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,63}$"
_AGENT_ID_RE = re.compile(AGENT_ID_PATTERN)

# F3 -- directory `brain backup` writes archives into and `brain restore`
# discovers them from. Defaults to ``$BRAIN_HOME/backups`` (resolved lazily so a
# relocated brain home relocates its backups too); override with an absolute or
# ``~``-relative path via ``BRAIN_BACKUP_DIR``. Never created at load time --
# only when a backup actually runs.
DEFAULT_BACKUP_DIR_NAME = "backups"


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
    """Path to $BRAIN_HOME/.env (resolved via _brain_home_root).

    This is the CANONICAL user config location: a pip / uvx user has no
    checkout at all, so "the repo's .env" can never be their config, and the
    cwd walk-up only works while they happen to stand in the right directory.
    ``brain setup`` / ``brain init`` provision this file (see
    :func:`brain.setup.provision_brain_home_dotenv`).
    """
    return _brain_home_root() / ".env"


# ---------------------------------------------------------------------------
# Dotenv chain resolution — shared by Config.load() and `brain doctor`.
#
# Deliberately NOT a method on Config: doctor must be able to introspect a
# BROKEN install, i.e. exactly the case where ``Config.load()`` raises. Keep
# :func:`dotenv_chain` callable with no successfully-constructed config.
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class DotenvSource:
    """One candidate ``.env`` file in the resolution chain, with its state.

    ``exists`` follows symlinks, so a DANGLING symlink reports
    ``exists=False`` — a caller that must tell "broken link" apart from "never
    created" (``brain doctor``, which reports a relocated dev checkout) checks
    ``path.is_symlink()`` on top: that stays True for a dangling link and is
    False for a path that simply does not exist.

    ``loaded`` is True only when the loader actually read key/value pairs out
    of the file. ``exists=True, loaded=False`` means the path is there but
    could not be parsed (a directory, bad permissions) — a different fault
    from "missing", and the two must never be reported the same way.
    """

    path: Path
    exists: bool
    loaded: bool


#: Env var that drops the cwd walk-up (link 3) from the dotenv chain entirely.
IGNORE_CWD_DOTENV_ENV = "BRAIN_IGNORE_CWD_DOTENV"


def _cwd_dotenv_enabled() -> bool:
    """Whether the cwd walk-up participates in the chain (``BRAIN_IGNORE_CWD_DOTENV``).

    DECISION (2026-08-07), recorded because the walk-up is an ambient-cwd READ
    on the daemon path and a live candidate for silent misconfiguration:

    **The walk-up STAYS in the chain by default, and non-interactive contexts
    opt out explicitly.**

    Why keep it: it is long-standing behaviour that makes a source checkout
    work from any subdirectory, and removing it mid-release would silently
    break anyone whose workflow depends on it — a second invisible config
    change on top of the one being fixed.

    Why an explicit opt-out rather than auto-detection: sniffing the context
    (``isatty``, "am I a daemon?") would make WHICH DATABASE you talk to depend
    on whether stdout is a pipe. ``brain search | jq`` would resolve config
    differently from ``brain search``. That is the same class of invisible,
    environment-dependent divergence as the outage itself, so it is refused.

    Why it matters at all: the walk-up climbs to the filesystem root, so a
    process started in an arbitrary directory can pick up a stranger's ``.env``
    and silently talk to the wrong database. A daemon doing that is worse than
    one with no config, because the failure is invisible instead of loud.
    Long-running non-interactive contexts (the launchd plists) should therefore
    set ``BRAIN_IGNORE_CWD_DOTENV=1`` alongside an explicit ``BRAIN_HOME``.

    When set, the link is REMOVED from the chain — it does not appear in
    :func:`dotenv_chain`, so ``brain doctor`` renders exactly the paths that
    were really consulted.
    """
    raw = os.environ.get(IGNORE_CWD_DOTENV_ENV, "")
    return raw.strip().lower() not in _TRUTHY_ENV_VALUES


def _cwd_dotenv() -> Path:
    """The cwd walk-up ``.env`` candidate, or ``<cwd>/.env`` when none is found.

    ``find_dotenv`` returns ``""`` when the walk-up finds nothing, but the
    chain still needs a CONCRETE path to report as missing (an error message
    that says "somewhere near your cwd" helps nobody), so fall back to the
    obvious ``<cwd>/.env``.
    """
    found = find_dotenv(usecwd=True)
    return Path(found) if found else Path.cwd() / ".env"


def _resolve_dotenv_chain() -> tuple[tuple["DotenvSource", ...], dict[str, str]]:
    """Resolve the dotenv chain AND its merged payload in a single pass.

    Returns ``(sources, merged)`` where *sources* is ordered highest precedence
    first and *merged* is the key/value payload the caller layers under
    ``os.environ``. One pass so the state reported by :func:`dotenv_chain` and
    the values actually loaded by :meth:`Config._load_field_dict` can never
    disagree — the whole point of the shared contract.

    Duplicate candidates are collapsed (in a dev checkout ``$BRAIN_HOME`` IS
    the repo root, so links 2 and 4 are literally the same file). Comparison is
    on the LITERAL path, never ``resolve()``, so a ``$BRAIN_HOME/.env``
    symlinked at the repo ``.env`` stays a distinct, separately-reportable
    entry.
    """
    ordered: list[Path] = [_project_dotenv()]
    if _cwd_dotenv_enabled():
        ordered.append(_cwd_dotenv())
    ordered.append(_brain_home_dotenv())
    candidates: list[Path] = []
    for candidate in ordered:
        if candidate not in candidates:
            candidates.append(candidate)
    sources: list[DotenvSource] = []
    parsed_by_path: dict[Path, dict[str, str]] = {}
    for path in candidates:
        exists = path.exists()
        loaded = False
        if exists:
            # Read the bytes OURSELVES rather than handing the path to
            # dotenv_values: python-dotenv swallows an unreadable path and
            # returns an empty mapping, which would report a directory /
            # permission-denied .env as cleanly "loaded". Parsing the text we
            # actually read keeps ``loaded`` honest.
            try:
                text = path.read_text(encoding="utf-8")
            except (OSError, UnicodeDecodeError):
                pass
            else:
                loaded = True
                parsed_by_path[path] = {
                    k: v
                    for k, v in dotenv_values(stream=StringIO(text)).items()
                    if v is not None
                }
        sources.append(DotenvSource(path=path, exists=exists, loaded=loaded))
    # Layer in REVERSE priority order (lowest first) so a higher-priority file
    # overwrites a lower-priority one on key collisions.
    merged: dict[str, str] = {}
    for source in reversed(sources):
        merged.update(parsed_by_path.get(source.path, {}))
    return tuple(sources), merged


def dotenv_chain() -> tuple[DotenvSource, ...]:
    """Ordered dotenv candidates, highest precedence first, with resolution state.

    Order mirrors :meth:`Config._load_field_dict`'s file precedence:
    ``<repo>/.env`` > ``<cwd>/.env`` (walk-up) > ``$BRAIN_HOME/.env``. Process
    env outranks every file and is not represented here (it is not a file).

    The cwd walk-up is ABSENT from the returned chain when
    ``BRAIN_IGNORE_CWD_DOTENV`` is set (see :func:`_cwd_dotenv_enabled`) — the
    chain always reports exactly the files the loader consulted, so a dropped
    link never appears and a kept link is always visible.

    Safe to call on a BROKEN install — it never raises and never requires a
    loadable config.
    """
    return _resolve_dotenv_chain()[0]


class ConfigError(RuntimeError):
    pass


def _parse_positive_int_env(env_var: str, default: int) -> int:
    """Parse ``env_var`` as an integer ``>= 1``, falling back to ``default``.

    Unset / blank → ``default``; non-parseable or ``< 1`` → :class:`ConfigError`
    at load time so a typo surfaces at startup, not mid-command. Mirrors the
    eager-validation idiom used by every other int knob in this module, factored
    out for the Plan 01 / Plan 10 positive-int families.
    """
    raw = os.environ.get(env_var)
    if raw is None or raw.strip() == "":
        return default
    try:
        value = int(raw)
    except ValueError as exc:
        raise ConfigError(
            f"{env_var} must be an integer >= 1 (got {raw!r})"
        ) from exc
    if value < 1:
        raise ConfigError(f"{env_var} must be an integer >= 1 (got {raw!r})")
    return value


def _parse_non_negative_int_env(env_var: str, default: int) -> int:
    """Parse ``env_var`` as an integer ``>= 0``, falling back to ``default``.

    The sibling of :func:`_parse_positive_int_env` for the handful of knobs
    whose ``0`` is a meaningful operator opt-out ("no ceiling") rather than a
    typo. Deliberately a separate function: loosening
    :func:`_parse_positive_int_env` would silently admit ``0`` for the 20+
    knobs that depend on it rejecting it.
    """
    raw = os.environ.get(env_var)
    if raw is None or raw.strip() == "":
        return default
    try:
        value = int(raw)
    except ValueError as exc:
        raise ConfigError(
            f"{env_var} must be an integer >= 0 (got {raw!r})"
        ) from exc
    if value < 0:
        raise ConfigError(f"{env_var} must be an integer >= 0 (got {raw!r})")
    return value


def _parse_unit_interval_env(env_var: str, default: float) -> float:
    """Parse ``env_var`` as a float in ``[0.0, 1.0]``, falling back to ``default``.

    Unset / blank → ``default``; non-parseable or out of ``[0.0, 1.0]`` →
    :class:`ConfigError` at load time so a typo surfaces at startup, not
    mid-command. Mirrors the inline ``BRAIN_VECTOR_SIM_FLOOR`` /
    ``BRAIN_ELICIT_MIN_GAP_SCORE`` validation, factored out for the Plan 03
    cosine-floor knobs.
    """
    raw = os.environ.get(env_var)
    if raw is None or raw.strip() == "":
        return default
    try:
        value = float(raw)
    except ValueError as exc:
        raise ConfigError(
            f"{env_var} must be a float in [0.0, 1.0] (got {raw!r})"
        ) from exc
    if not (0.0 <= value <= 1.0):
        raise ConfigError(
            f"{env_var} must be a float in [0.0, 1.0] (got {raw!r})"
        )
    return value


def _default_vault_path() -> Path:
    """Resolve the vault root from ``BRAIN_VAULT_PATH``, else ``DEFAULT_VAULT_PATH``.

    Single source of truth for vault-root resolution. Used BOTH as the
    :attr:`Config.vault_path` field ``default_factory`` AND inside
    :meth:`Config._load_field_dict`. Because the field default resolves the env
    var at construction time, a ``Config`` built directly — bypassing
    :meth:`Config.load` (as many test fixtures do) — honors ``BRAIN_VAULT_PATH``
    exactly like ``Config.load()`` instead of silently falling back to the real
    ``~/brain-vault``. That parity is what stops the test suite's ingest mirrors
    from leaking into the live vault; an empty / unset value yields the packaged
    default.
    """
    raw = os.environ.get("BRAIN_VAULT_PATH")
    return Path(raw).expanduser() if raw else DEFAULT_VAULT_PATH


def _default_backup_dir() -> Path:
    """Resolve the backup root from ``BRAIN_BACKUP_DIR``, else ``$BRAIN_HOME/backups``.

    Single source of truth for backup-root resolution, mirroring
    :func:`_default_vault_path`. Used BOTH as the :attr:`Config.backup_dir`
    field ``default_factory`` AND inside :meth:`Config._load_field_dict`, so a
    ``Config`` built directly -- bypassing :meth:`Config.load`, as many test
    fixtures do -- honors ``BRAIN_BACKUP_DIR`` exactly like ``Config.load()``.
    Blank / unset falls back to ``$BRAIN_HOME/backups``, resolved through
    :func:`_brain_home_root` at call time (never cached) so relocating the brain
    home relocates its backups with it. Purely a path computation: the directory
    is NOT created here.
    """
    raw = os.environ.get("BRAIN_BACKUP_DIR")
    if raw and raw.strip():
        return Path(raw.strip()).expanduser()
    return _brain_home_root() / DEFAULT_BACKUP_DIR_NAME


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
    vault_path: Path = field(default_factory=_default_vault_path)
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
    # Plan 09 -- quick-capture inbox knobs.
    # ``capture_title_words`` is how many leading body words form the auto-title
    # slug (>= 1); ``capture_inbox_warn_threshold`` is the inbox size above
    # which `brain doctor` warns (>= 1; Phase 3 compares strictly greater-than).
    # Both are validated at load time, mirroring the other int env knobs.
    capture_title_words: int = DEFAULT_CAPTURE_TITLE_WORDS
    capture_inbox_warn_threshold: int = DEFAULT_CAPTURE_INBOX_WARN_THRESHOLD
    # Plan 10 -- `brain review weekly` display caps. All three are positive
    # integers (>= 1), validated at load time like the capture knobs.
    review_activity_limit: int = DEFAULT_REVIEW_ACTIVITY_LIMIT
    review_theme_limit: int = DEFAULT_REVIEW_THEME_LIMIT
    review_open_loop_limit: int = DEFAULT_REVIEW_OPEN_LOOP_LIMIT
    # Plan 01 -- `brain brief` daily-digest knobs. All four are positive
    # integers (>= 1), validated at load time like the capture knobs.
    brief_since_hours: int = DEFAULT_BRIEF_SINCE_HOURS
    brief_todo_since_days: int = DEFAULT_BRIEF_TODO_SINCE_DAYS
    brief_capture_limit: int = DEFAULT_BRIEF_CAPTURE_LIMIT
    brief_pin_limit: int = DEFAULT_BRIEF_PIN_LIMIT
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
    # Wave 4 -- hard outer character cap on a stitched snippet. Positive int
    # from ``BRAIN_SNIPPET_MAX_CHARS``; see the DEFAULT_* block for why this is
    # the one knob that wave left behind.
    snippet_max_chars: int = DEFAULT_SNIPPET_MAX_CHARS
    # Plan 02 -- spaced-repetition resurfacing knobs (`brain resurface`). All
    # four feed :func:`brain.resurface.resurface_docs`; grouped together and
    # validated the same way as the other int/float env knobs. ``limit`` >= 1,
    # ``min_age_days`` >= 0, both half-lives > 0.
    resurface_limit: int = DEFAULT_RESURFACE_LIMIT
    resurface_min_age_days: int = DEFAULT_RESURFACE_MIN_AGE_DAYS
    resurface_age_halflife_days: float = DEFAULT_RESURFACE_AGE_HALFLIFE_DAYS
    resurface_access_halflife_days: float = DEFAULT_RESURFACE_ACCESS_HALFLIFE_DAYS
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
    # understands: a positive integer duration string ("30m", "1h", "60s"), a
    # bare positive integer seconds string ("60"), "-1" (keep loaded
    # indefinitely — kills the measured 5,028ms cold-embed cliff), or "0"
    # (unload immediately). Malformed values ("-2", "-1m", "abc", …) are
    # rejected at load time via ConfigError. Override via
    # ``BRAIN_OLLAMA_KEEP_ALIVE``.
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
    # Tacit-knowledge elicitation knobs (feat/tacit-knowledge-elicitation).
    # Minimum evidence docs required for a gap to surface.  See
    # :data:`DEFAULT_ELICIT_MIN_EVIDENCE_DOCS`. Non-negative integer.
    elicit_min_evidence_docs: int = DEFAULT_ELICIT_MIN_EVIDENCE_DOCS
    # Minimum gap score in [0.0, 1.0] for a gap to enter the queue. See
    # :data:`DEFAULT_ELICIT_MIN_GAP_SCORE`. Out-of-range values are rejected
    # at load time via :class:`ConfigError`.
    elicit_min_gap_score: float = DEFAULT_ELICIT_MIN_GAP_SCORE
    # Maximum rows returned by ``brain elicit list``. Non-negative integer.
    elicit_queue_limit: int = DEFAULT_ELICIT_QUEUE_LIMIT
    # Gates the contradiction detector. Bool, default off.
    elicit_contradiction_enabled: bool = DEFAULT_ELICIT_CONTRADICTION_ENABLED
    # Minimum docs per target before contradiction detection runs. Non-negative int.
    elicit_contradiction_min_docs: int = DEFAULT_ELICIT_CONTRADICTION_MIN_DOCS
    # Plan 05 -- `brain timeline` knobs. ``timeline_granularity`` is the default
    # bucket width (validated ∈ {auto, month, quarter, year}); ``timeline_limit`` the
    # default max buckets (positive int); ``timeline_synth_limit`` the max
    # densest buckets synthesized (non-negative int, 0 disables);
    # ``timeline_trim`` which end is trimmed at the limit (∈ {oldest, sparsest}).
    timeline_granularity: str = DEFAULT_TIMELINE_GRANULARITY
    timeline_limit: int = DEFAULT_TIMELINE_LIMIT
    timeline_synth_limit: int = DEFAULT_TIMELINE_SYNTH_LIMIT
    timeline_trim: str = DEFAULT_TIMELINE_TRIM
    # Plan 07 -- `brain connect` auto-link suggestion knobs. ``connect_min_score``
    # is the RRF-blend confidence floor in (0.0, 1.0]; ``connect_candidate_limit``
    # and ``connect_max_per_doc`` are positive-integer caps. All three are
    # eager-validated at load time, mirroring the other env knobs.
    connect_min_score: float = DEFAULT_CONNECT_MIN_SCORE
    connect_candidate_limit: int = DEFAULT_CONNECT_CANDIDATE_LIMIT
    connect_max_per_doc: int = DEFAULT_CONNECT_MAX_PER_DOC
    # Plan 03 -- `brain review scan` contradiction + staleness knobs. The two
    # ``*_sim_floor`` values are cosine floors in [0.0, 1.0]; the rest are
    # positive ints. All eager-validated at load time via ``ConfigError``.
    review_conflict_limit: int = DEFAULT_REVIEW_CONFLICT_LIMIT
    review_conflict_pairs_per_entity: int = DEFAULT_REVIEW_CONFLICT_PAIRS_PER_ENTITY
    review_embed_sim_floor: float = DEFAULT_REVIEW_EMBED_SIM_FLOOR
    review_stale_age_days: int = DEFAULT_REVIEW_STALE_AGE_DAYS
    review_stale_supersede_window_days: int = DEFAULT_REVIEW_STALE_SUPERSEDE_WINDOW_DAYS
    review_stale_sim_floor: float = DEFAULT_REVIEW_STALE_SIM_FLOOR
    review_stale_limit: int = DEFAULT_REVIEW_STALE_LIMIT
    # Plan 06 -- `brain ask` agentic-synthesis knobs. ``ask_max_iterations`` and
    # ``ask_docs_per_iter`` are positive ints (>= 1); ``ask_timeout_seconds`` is
    # a positive float; ``ask_model`` defaults to ``enrich_model`` (blank env
    # resets to that default), so the ask loop reuses the configured chat model
    # unless explicitly overridden via ``BRAIN_ASK_MODEL``.
    ask_max_iterations: int = DEFAULT_ASK_MAX_ITERATIONS
    ask_docs_per_iter: int = DEFAULT_ASK_DOCS_PER_ITER
    ask_model: str = DEFAULT_ENRICH_MODEL
    ask_timeout_seconds: float = DEFAULT_ASK_TIMEOUT_SECONDS
    # Plan 04 -- `brain audio` knobs. ``audio_script_model`` selects the Ollama
    # dialogue model; ``audio_max_turns`` caps the dialogue (positive even int);
    # ``audio_max_input_tokens`` bounds the bundle prompt (positive int);
    # ``audio_theme_limit`` caps the theme/community groups pulled into the
    # source bundle (positive int).
    audio_script_model: str = DEFAULT_AUDIO_SCRIPT_MODEL
    audio_max_turns: int = DEFAULT_AUDIO_MAX_TURNS
    audio_max_input_tokens: int = DEFAULT_AUDIO_MAX_INPUT_TOKENS
    audio_theme_limit: int = DEFAULT_AUDIO_THEME_LIMIT
    # Plan 08 -- `brain gaps` search-failure knobs. Both positive ints (>= 1),
    # eager-validated at load time via ``ConfigError`` like the review knobs.
    gaps_lookback_days: int = DEFAULT_GAPS_LOOKBACK_DAYS
    gaps_min_cluster_size: int = DEFAULT_GAPS_MIN_CLUSTER_SIZE
    # Task 0B pre-landed scaffolding (plan 2026-07-25). No feature reads these
    # yet; see the DEFAULT_* block above for what each one will govern.
    # F4 -- ingest secret-guard mode, one of warn/redact/reject/off.
    secret_guard: str = DEFAULT_SECRET_GUARD
    # F10 -- `brain recall` budgets. All positive ints (>= 1), eager-validated
    # at load time via ``ConfigError`` like the other int knobs.
    recall_budget_tokens: int = DEFAULT_RECALL_BUDGET_TOKENS
    recall_passage_tokens: int = DEFAULT_RECALL_PASSAGE_TOKENS
    recall_max_candidates: int = DEFAULT_RECALL_MAX_CANDIDATES
    # Wave 3 -- MCP payload ceilings (see the DEFAULT_* block for rationale).
    # All eager-validated at load time. ``show_max_content_tokens`` is the only
    # one accepting 0 (= unlimited); the rest are >= 1.
    show_max_content_tokens: int = DEFAULT_SHOW_MAX_CONTENT_TOKENS
    search_max_limit: int = DEFAULT_SEARCH_MAX_LIMIT
    recall_max_budget_tokens: int = DEFAULT_RECALL_MAX_BUDGET_TOKENS
    graph_entities_max_limit: int = DEFAULT_GRAPH_ENTITIES_MAX_LIMIT
    mcp_rows_max_limit: int = DEFAULT_MCP_ROWS_MAX_LIMIT
    graph_communities_list_limit: int = DEFAULT_GRAPH_COMMUNITIES_LIST_LIMIT
    # F10 -- optional agent identity attributed to writes. Unset / blank stays
    # ``None`` (attribution disabled); a set value must match
    # :data:`AGENT_ID_PATTERN` or load fails.
    agent_id: str | None = None
    # F3 -- `brain backup` archive directory; resolves via
    # :func:`_default_backup_dir` so a directly-constructed Config honors
    # ``BRAIN_BACKUP_DIR`` identically to ``Config.load()``.
    backup_dir: Path = field(default_factory=_default_backup_dir)

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
        # Resolution + merging live in _resolve_dotenv_chain() so `brain doctor`
        # (via the public dotenv_chain()) reports exactly the paths this loader
        # consulted. Process env is applied last via os.environ.setdefault so an
        # existing value is never clobbered -- preserving the precedence
        # contract regardless of who set it (shell, parent process, or
        # monkeypatch.setenv).
        sources, merged = _resolve_dotenv_chain()
        for key, value in merged.items():
            os.environ.setdefault(key, value)
        database_url = os.environ.get("DATABASE_URL")
        if require_db and not database_url:
            # Render the message FROM the same chain doctor introspects, so the
            # error and the health check can never disagree about which files
            # were searched or what state each one is in. A bare
            # "DATABASE_URL is not set (see .env.example)" cost 12 days of
            # debugging a database that was healthy the whole time.
            raise ConfigError(
                missing_database_url_message(
                    sources, brain_home_dotenv=_brain_home_dotenv()
                )
            )
        database_url = database_url or ""  # empty sentinel when require_db=False
        ollama_host = os.environ.get("OLLAMA_HOST", DEFAULT_OLLAMA_HOST)
        qwen3_model = os.environ.get("QWEN3_MODEL", DEFAULT_QWEN3_MODEL)
        embedder = os.environ.get("BRAIN_EMBEDDER", DEFAULT_EMBEDDER).lower()
        if embedder not in _VALID_EMBEDDERS:
            raise ConfigError(
                f"BRAIN_EMBEDDER must be one of: arctic, voyage, qwen3, none "
                f"(got {embedder!r})"
            )
        voyage_api_key = os.environ.get("VOYAGE_API_KEY")
        vault_path = _default_vault_path()
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
        # Plan 09 -- quick-capture inbox knobs. Same eager-validation idiom as
        # the other int env vars: unset -> default; non-parseable or
        # out-of-range -> ConfigError at startup instead of a mid-capture crash.
        capture_words_raw = os.environ.get("BRAIN_CAPTURE_TITLE_WORDS")
        if capture_words_raw is None or capture_words_raw.strip() == "":
            capture_title_words = DEFAULT_CAPTURE_TITLE_WORDS
        else:
            try:
                capture_title_words = int(capture_words_raw)
            except ValueError as exc:
                raise ConfigError(
                    f"BRAIN_CAPTURE_TITLE_WORDS must be an integer >= 1 "
                    f"(got {capture_words_raw!r})"
                ) from exc
            if capture_title_words < 1:
                raise ConfigError(
                    f"BRAIN_CAPTURE_TITLE_WORDS must be an integer >= 1 "
                    f"(got {capture_title_words!r})"
                )

        capture_warn_raw = os.environ.get("BRAIN_CAPTURE_INBOX_WARN_THRESHOLD")
        if capture_warn_raw is None or capture_warn_raw.strip() == "":
            capture_inbox_warn_threshold = DEFAULT_CAPTURE_INBOX_WARN_THRESHOLD
        else:
            try:
                capture_inbox_warn_threshold = int(capture_warn_raw)
            except ValueError as exc:
                raise ConfigError(
                    f"BRAIN_CAPTURE_INBOX_WARN_THRESHOLD must be an integer >= 1 "
                    f"(got {capture_warn_raw!r})"
                ) from exc
            if capture_inbox_warn_threshold < 1:
                raise ConfigError(
                    f"BRAIN_CAPTURE_INBOX_WARN_THRESHOLD must be an integer >= 1 "
                    f"(got {capture_inbox_warn_threshold!r})"
                )
        # Plan 10 -- `brain review weekly` display caps. Positive ints.
        review_activity_limit = _parse_positive_int_env(
            "BRAIN_REVIEW_ACTIVITY_LIMIT", DEFAULT_REVIEW_ACTIVITY_LIMIT
        )
        review_theme_limit = _parse_positive_int_env(
            "BRAIN_REVIEW_THEME_LIMIT", DEFAULT_REVIEW_THEME_LIMIT
        )
        review_open_loop_limit = _parse_positive_int_env(
            "BRAIN_REVIEW_OPEN_LOOP_LIMIT", DEFAULT_REVIEW_OPEN_LOOP_LIMIT
        )
        # Plan 01 -- `brain brief` daily-digest knobs. Positive ints.
        brief_since_hours = _parse_positive_int_env(
            "BRAIN_BRIEF_SINCE_HOURS", DEFAULT_BRIEF_SINCE_HOURS
        )
        brief_todo_since_days = _parse_positive_int_env(
            "BRAIN_BRIEF_TODO_SINCE_DAYS", DEFAULT_BRIEF_TODO_SINCE_DAYS
        )
        brief_capture_limit = _parse_positive_int_env(
            "BRAIN_BRIEF_CAPTURE_LIMIT", DEFAULT_BRIEF_CAPTURE_LIMIT
        )
        brief_pin_limit = _parse_positive_int_env(
            "BRAIN_BRIEF_PIN_LIMIT", DEFAULT_BRIEF_PIN_LIMIT
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
        # Wave 4 -- the snippet character cap. ``0`` is rejected rather than
        # treated as "no cap": it would blank every snippet in every payload,
        # which is indistinguishable from a working search that found nothing.
        snippet_max_chars = _parse_positive_int_env(
            "BRAIN_SNIPPET_MAX_CHARS", DEFAULT_SNIPPET_MAX_CHARS
        )
        # Plan 02 -- resurface env vars. Same eager-validation idiom as the
        # snippet-context / recency-halflife knobs above: unset/blank ->
        # default; non-parseable / out-of-range -> ConfigError at startup so a
        # config typo surfaces immediately rather than mid-resurface.
        resurface_limit_raw = os.environ.get("BRAIN_RESURFACE_LIMIT")
        if resurface_limit_raw is None or resurface_limit_raw.strip() == "":
            resurface_limit = DEFAULT_RESURFACE_LIMIT
        else:
            try:
                resurface_limit = int(resurface_limit_raw)
            except ValueError as exc:
                raise ConfigError(
                    f"BRAIN_RESURFACE_LIMIT must be an integer >= 1 "
                    f"(got {resurface_limit_raw!r})"
                ) from exc
            if resurface_limit < 1:
                raise ConfigError(
                    f"BRAIN_RESURFACE_LIMIT must be an integer >= 1 "
                    f"(got {resurface_limit!r})"
                )

        resurface_min_age_raw = os.environ.get("BRAIN_RESURFACE_MIN_AGE_DAYS")
        if resurface_min_age_raw is None or resurface_min_age_raw.strip() == "":
            resurface_min_age_days = DEFAULT_RESURFACE_MIN_AGE_DAYS
        else:
            try:
                resurface_min_age_days = int(resurface_min_age_raw)
            except ValueError as exc:
                raise ConfigError(
                    f"BRAIN_RESURFACE_MIN_AGE_DAYS must be a non-negative integer "
                    f"(got {resurface_min_age_raw!r})"
                ) from exc
            if resurface_min_age_days < 0:
                raise ConfigError(
                    f"BRAIN_RESURFACE_MIN_AGE_DAYS must be a non-negative integer "
                    f"(got {resurface_min_age_days!r})"
                )

        resurface_age_hl_raw = os.environ.get("BRAIN_RESURFACE_AGE_HALFLIFE_DAYS")
        if resurface_age_hl_raw is None or resurface_age_hl_raw.strip() == "":
            resurface_age_halflife_days = DEFAULT_RESURFACE_AGE_HALFLIFE_DAYS
        else:
            try:
                resurface_age_halflife_days = float(resurface_age_hl_raw)
            except ValueError as exc:
                raise ConfigError(
                    f"BRAIN_RESURFACE_AGE_HALFLIFE_DAYS must be a positive float "
                    f"(got {resurface_age_hl_raw!r})"
                ) from exc
            if resurface_age_halflife_days <= 0:
                raise ConfigError(
                    f"BRAIN_RESURFACE_AGE_HALFLIFE_DAYS must be a positive float "
                    f"(got {resurface_age_halflife_days!r})"
                )

        resurface_access_hl_raw = os.environ.get(
            "BRAIN_RESURFACE_ACCESS_HALFLIFE_DAYS"
        )
        if resurface_access_hl_raw is None or resurface_access_hl_raw.strip() == "":
            resurface_access_halflife_days = DEFAULT_RESURFACE_ACCESS_HALFLIFE_DAYS
        else:
            try:
                resurface_access_halflife_days = float(resurface_access_hl_raw)
            except ValueError as exc:
                raise ConfigError(
                    f"BRAIN_RESURFACE_ACCESS_HALFLIFE_DAYS must be a positive float "
                    f"(got {resurface_access_hl_raw!r})"
                ) from exc
            if resurface_access_halflife_days <= 0:
                raise ConfigError(
                    f"BRAIN_RESURFACE_ACCESS_HALFLIFE_DAYS must be a positive float "
                    f"(got {resurface_access_halflife_days!r})"
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

        # Ollama keep_alive -- see DEFAULT_OLLAMA_KEEP_ALIVE. Accepts a positive
        # integer optionally followed by m/h/s (e.g. "30m", "1h", "60s", "60")
        # plus the two Ollama sentinels "-1" (keep loaded indefinitely) and "0"
        # (unload immediately). Empty / whitespace-only falls back to the
        # default; every other malformed value ("-2", "-1m", "abc", …) is
        # rejected via ConfigError so a config typo surfaces at startup, not
        # during an embed/chat call.
        keep_alive_raw = os.environ.get("BRAIN_OLLAMA_KEEP_ALIVE")
        if keep_alive_raw is None or keep_alive_raw.strip() == "":
            ollama_keep_alive = DEFAULT_OLLAMA_KEEP_ALIVE
        else:
            stripped_ka = keep_alive_raw.strip()
            if not _KEEP_ALIVE_RE.match(stripped_ka):
                raise ConfigError(
                    "BRAIN_OLLAMA_KEEP_ALIVE must be '-1' (keep loaded), '0' "
                    "(unload immediately), or a positive integer / duration "
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

        # Tacit-knowledge elicitation knobs. Same eager-validation idiom as the
        # enrich / graph knobs above: unset/blank -> default; non-parseable /
        # out-of-range -> ConfigError at startup so a typo never surfaces
        # mid-elicitation.
        elicit_min_evidence_raw = os.environ.get("BRAIN_ELICIT_MIN_EVIDENCE_DOCS")
        if elicit_min_evidence_raw is None or elicit_min_evidence_raw.strip() == "":
            elicit_min_evidence_docs = DEFAULT_ELICIT_MIN_EVIDENCE_DOCS
        else:
            try:
                elicit_min_evidence_docs = int(elicit_min_evidence_raw)
            except ValueError as exc:
                raise ConfigError(
                    f"BRAIN_ELICIT_MIN_EVIDENCE_DOCS must be a non-negative integer "
                    f"(got {elicit_min_evidence_raw!r})"
                ) from exc
            if elicit_min_evidence_docs < 0:
                raise ConfigError(
                    f"BRAIN_ELICIT_MIN_EVIDENCE_DOCS must be a non-negative integer "
                    f"(got {elicit_min_evidence_docs!r})"
                )

        elicit_gap_score_raw = os.environ.get("BRAIN_ELICIT_MIN_GAP_SCORE")
        if elicit_gap_score_raw is None or elicit_gap_score_raw.strip() == "":
            elicit_min_gap_score = DEFAULT_ELICIT_MIN_GAP_SCORE
        else:
            try:
                elicit_min_gap_score = float(elicit_gap_score_raw)
            except ValueError as exc:
                raise ConfigError(
                    f"BRAIN_ELICIT_MIN_GAP_SCORE must be a float in [0.0, 1.0] "
                    f"(got {elicit_gap_score_raw!r})"
                ) from exc
            if not (0.0 <= elicit_min_gap_score <= 1.0):
                raise ConfigError(
                    f"BRAIN_ELICIT_MIN_GAP_SCORE must be a float in [0.0, 1.0] "
                    f"(got {elicit_min_gap_score!r})"
                )

        elicit_queue_limit_raw = os.environ.get("BRAIN_ELICIT_QUEUE_LIMIT")
        if elicit_queue_limit_raw is None or elicit_queue_limit_raw.strip() == "":
            elicit_queue_limit = DEFAULT_ELICIT_QUEUE_LIMIT
        else:
            try:
                elicit_queue_limit = int(elicit_queue_limit_raw)
            except ValueError as exc:
                raise ConfigError(
                    f"BRAIN_ELICIT_QUEUE_LIMIT must be a non-negative integer "
                    f"(got {elicit_queue_limit_raw!r})"
                ) from exc
            if elicit_queue_limit < 0:
                raise ConfigError(
                    f"BRAIN_ELICIT_QUEUE_LIMIT must be a non-negative integer "
                    f"(got {elicit_queue_limit_raw!r})"
                )

        elicit_contradiction_enabled_raw = os.environ.get(
            "BRAIN_ELICIT_CONTRADICTION_ENABLED"
        )
        if (
            elicit_contradiction_enabled_raw is None
            or elicit_contradiction_enabled_raw.strip() == ""
        ):
            elicit_contradiction_enabled = DEFAULT_ELICIT_CONTRADICTION_ENABLED
        else:
            token = elicit_contradiction_enabled_raw.strip().lower()
            if token in _GRAPH_ENABLED_TRUTHY:
                elicit_contradiction_enabled = True
            elif token in _GRAPH_ENABLED_FALSY:
                elicit_contradiction_enabled = False
            else:
                raise ConfigError(
                    "BRAIN_ELICIT_CONTRADICTION_ENABLED must be one of "
                    "1/true/yes/on or 0/false/no/off "
                    f"(got {elicit_contradiction_enabled_raw!r})"
                )

        elicit_contradiction_min_docs_raw = os.environ.get(
            "BRAIN_ELICIT_CONTRADICTION_MIN_DOCS"
        )
        if (
            elicit_contradiction_min_docs_raw is None
            or elicit_contradiction_min_docs_raw.strip() == ""
        ):
            elicit_contradiction_min_docs = DEFAULT_ELICIT_CONTRADICTION_MIN_DOCS
        else:
            try:
                elicit_contradiction_min_docs = int(elicit_contradiction_min_docs_raw)
            except ValueError as exc:
                raise ConfigError(
                    f"BRAIN_ELICIT_CONTRADICTION_MIN_DOCS must be a non-negative integer "
                    f"(got {elicit_contradiction_min_docs_raw!r})"
                ) from exc
            if elicit_contradiction_min_docs < 0:
                raise ConfigError(
                    f"BRAIN_ELICIT_CONTRADICTION_MIN_DOCS must be a non-negative integer "
                    f"(got {elicit_contradiction_min_docs!r})"
                )

        # Plan 05 -- `brain timeline` env vars. Same eager-validation idiom as
        # the enrich / graph knobs above: unset/blank -> default; invalid value
        # -> ConfigError at startup so a typo surfaces before any timeline runs.
        timeline_gran_raw = os.environ.get("BRAIN_TIMELINE_GRANULARITY")
        if timeline_gran_raw is None or timeline_gran_raw.strip() == "":
            timeline_granularity = DEFAULT_TIMELINE_GRANULARITY
        else:
            timeline_granularity = timeline_gran_raw.strip().lower()
            if timeline_granularity not in _VALID_TIMELINE_GRANULARITIES:
                raise ConfigError(
                    "BRAIN_TIMELINE_GRANULARITY must be one of auto/month/quarter/year "
                    f"(got {timeline_gran_raw!r})"
                )

        timeline_limit_raw = os.environ.get("BRAIN_TIMELINE_LIMIT")
        if timeline_limit_raw is None or timeline_limit_raw.strip() == "":
            timeline_limit = DEFAULT_TIMELINE_LIMIT
        else:
            try:
                timeline_limit = int(timeline_limit_raw)
            except ValueError as exc:
                raise ConfigError(
                    f"BRAIN_TIMELINE_LIMIT must be a positive integer "
                    f"(got {timeline_limit_raw!r})"
                ) from exc
            if timeline_limit < 1:
                raise ConfigError(
                    f"BRAIN_TIMELINE_LIMIT must be a positive integer "
                    f"(got {timeline_limit_raw!r})"
                )

        timeline_synth_raw = os.environ.get("BRAIN_TIMELINE_SYNTH_LIMIT")
        if timeline_synth_raw is None or timeline_synth_raw.strip() == "":
            timeline_synth_limit = DEFAULT_TIMELINE_SYNTH_LIMIT
        else:
            try:
                timeline_synth_limit = int(timeline_synth_raw)
            except ValueError as exc:
                raise ConfigError(
                    f"BRAIN_TIMELINE_SYNTH_LIMIT must be a non-negative integer "
                    f"(got {timeline_synth_raw!r})"
                ) from exc
            if timeline_synth_limit < 0:
                raise ConfigError(
                    f"BRAIN_TIMELINE_SYNTH_LIMIT must be a non-negative integer "
                    f"(got {timeline_synth_raw!r})"
                )

        timeline_trim_raw = os.environ.get("BRAIN_TIMELINE_TRIM")
        if timeline_trim_raw is None or timeline_trim_raw.strip() == "":
            timeline_trim = DEFAULT_TIMELINE_TRIM
        else:
            timeline_trim = timeline_trim_raw.strip().lower()
            if timeline_trim not in _VALID_TIMELINE_TRIMS:
                raise ConfigError(
                    "BRAIN_TIMELINE_TRIM must be one of oldest/sparsest "
                    f"(got {timeline_trim_raw!r})"
                )

        # Plan 07 -- `brain connect` knobs. Same eager-validation idiom as the
        # elicit / graph knobs above: unset/blank -> default; non-parseable /
        # out-of-range -> ConfigError at startup so a typo never surfaces
        # mid-refresh.
        connect_min_score_raw = os.environ.get("BRAIN_CONNECT_MIN_SCORE")
        if connect_min_score_raw is None or connect_min_score_raw.strip() == "":
            connect_min_score = DEFAULT_CONNECT_MIN_SCORE
        else:
            try:
                connect_min_score = float(connect_min_score_raw)
            except ValueError as exc:
                raise ConfigError(
                    "BRAIN_CONNECT_MIN_SCORE must be a float in (0.0, 1.0] "
                    f"(got {connect_min_score_raw!r})"
                ) from exc
            if not (0.0 < connect_min_score <= 1.0):
                raise ConfigError(
                    "BRAIN_CONNECT_MIN_SCORE must be a float in (0.0, 1.0] "
                    f"(got {connect_min_score_raw!r})"
                )

        connect_candidate_raw = os.environ.get("BRAIN_CONNECT_CANDIDATE_LIMIT")
        if connect_candidate_raw is None or connect_candidate_raw.strip() == "":
            connect_candidate_limit = DEFAULT_CONNECT_CANDIDATE_LIMIT
        else:
            try:
                connect_candidate_limit = int(connect_candidate_raw)
            except ValueError as exc:
                raise ConfigError(
                    "BRAIN_CONNECT_CANDIDATE_LIMIT must be an integer >= 1 "
                    f"(got {connect_candidate_raw!r})"
                ) from exc
            if connect_candidate_limit < 1:
                raise ConfigError(
                    "BRAIN_CONNECT_CANDIDATE_LIMIT must be an integer >= 1 "
                    f"(got {connect_candidate_raw!r})"
                )

        connect_max_per_doc_raw = os.environ.get("BRAIN_CONNECT_MAX_PER_DOC")
        if connect_max_per_doc_raw is None or connect_max_per_doc_raw.strip() == "":
            connect_max_per_doc = DEFAULT_CONNECT_MAX_PER_DOC
        else:
            try:
                connect_max_per_doc = int(connect_max_per_doc_raw)
            except ValueError as exc:
                raise ConfigError(
                    "BRAIN_CONNECT_MAX_PER_DOC must be an integer >= 1 "
                    f"(got {connect_max_per_doc_raw!r})"
                ) from exc
            if connect_max_per_doc < 1:
                raise ConfigError(
                    "BRAIN_CONNECT_MAX_PER_DOC must be an integer >= 1 "
                    f"(got {connect_max_per_doc_raw!r})"
                )
        # Plan 03 -- `brain review scan` knobs (positive ints + cosine floors).
        review_conflict_limit = _parse_positive_int_env(
            "BRAIN_REVIEW_CONFLICT_LIMIT", DEFAULT_REVIEW_CONFLICT_LIMIT
        )
        review_conflict_pairs_per_entity = _parse_positive_int_env(
            "BRAIN_REVIEW_CONFLICT_PAIRS_PER_ENTITY",
            DEFAULT_REVIEW_CONFLICT_PAIRS_PER_ENTITY,
        )
        review_embed_sim_floor = _parse_unit_interval_env(
            "BRAIN_REVIEW_EMBED_SIM_FLOOR", DEFAULT_REVIEW_EMBED_SIM_FLOOR
        )
        review_stale_age_days = _parse_positive_int_env(
            "BRAIN_REVIEW_STALE_AGE_DAYS", DEFAULT_REVIEW_STALE_AGE_DAYS
        )
        review_stale_supersede_window_days = _parse_positive_int_env(
            "BRAIN_REVIEW_STALE_SUPERSEDE_WINDOW_DAYS",
            DEFAULT_REVIEW_STALE_SUPERSEDE_WINDOW_DAYS,
        )
        review_stale_sim_floor = _parse_unit_interval_env(
            "BRAIN_REVIEW_STALE_SIM_FLOOR", DEFAULT_REVIEW_STALE_SIM_FLOOR
        )
        review_stale_limit = _parse_positive_int_env(
            "BRAIN_REVIEW_STALE_LIMIT", DEFAULT_REVIEW_STALE_LIMIT
        )
        # Plan 06 -- `brain ask` knobs. Same eager-validation pattern as the
        # enrich / timeline knobs above: unset/blank -> default; non-parseable /
        # out-of-range -> ConfigError so a typo surfaces at startup, not mid-loop.
        ask_max_iter_raw = os.environ.get("BRAIN_ASK_MAX_ITERATIONS")
        if ask_max_iter_raw is None or ask_max_iter_raw.strip() == "":
            ask_max_iterations = DEFAULT_ASK_MAX_ITERATIONS
        else:
            try:
                ask_max_iterations = int(ask_max_iter_raw)
            except ValueError as exc:
                raise ConfigError(
                    f"BRAIN_ASK_MAX_ITERATIONS must be a positive integer "
                    f"(got {ask_max_iter_raw!r})"
                ) from exc
            if ask_max_iterations < 1:
                raise ConfigError(
                    f"BRAIN_ASK_MAX_ITERATIONS must be a positive integer "
                    f"(got {ask_max_iter_raw!r})"
                )

        ask_docs_raw = os.environ.get("BRAIN_ASK_DOCS_PER_ITER")
        if ask_docs_raw is None or ask_docs_raw.strip() == "":
            ask_docs_per_iter = DEFAULT_ASK_DOCS_PER_ITER
        else:
            try:
                ask_docs_per_iter = int(ask_docs_raw)
            except ValueError as exc:
                raise ConfigError(
                    f"BRAIN_ASK_DOCS_PER_ITER must be a positive integer "
                    f"(got {ask_docs_raw!r})"
                ) from exc
            if ask_docs_per_iter < 1:
                raise ConfigError(
                    f"BRAIN_ASK_DOCS_PER_ITER must be a positive integer "
                    f"(got {ask_docs_raw!r})"
                )

        # ``ask_model`` inherits ``enrich_model`` when unset/blank (the locked
        # default per the plan's config table) so the ask loop reuses the same
        # chat model as enrichment unless explicitly overridden.
        ask_model_raw = os.environ.get("BRAIN_ASK_MODEL")
        if ask_model_raw is None or ask_model_raw.strip() == "":
            ask_model = enrich_model
        else:
            ask_model = ask_model_raw.strip()

        ask_timeout_raw = os.environ.get("BRAIN_ASK_TIMEOUT_SECONDS")
        if ask_timeout_raw is None or ask_timeout_raw.strip() == "":
            ask_timeout_seconds = DEFAULT_ASK_TIMEOUT_SECONDS
        else:
            try:
                ask_timeout_seconds = float(ask_timeout_raw)
            except ValueError as exc:
                raise ConfigError(
                    f"BRAIN_ASK_TIMEOUT_SECONDS must be a positive float "
                    f"(got {ask_timeout_raw!r})"
                ) from exc
            if ask_timeout_seconds <= 0:
                raise ConfigError(
                    f"BRAIN_ASK_TIMEOUT_SECONDS must be a positive float "
                    f"(got {ask_timeout_raw!r})"
                )

        # Plan 04 -- `brain audio` env vars. Same eager-validation idiom as the
        # enrich / timeline knobs above: unset/blank -> default; non-parseable /
        # out-of-range -> ConfigError at startup so a typo never surfaces
        # mid-generation.
        audio_model_raw = os.environ.get("BRAIN_AUDIO_SCRIPT_MODEL")
        if audio_model_raw is None or audio_model_raw.strip() == "":
            audio_script_model = DEFAULT_AUDIO_SCRIPT_MODEL
        else:
            audio_script_model = audio_model_raw.strip()

        audio_max_turns_raw = os.environ.get("BRAIN_AUDIO_MAX_TURNS")
        if audio_max_turns_raw is None or audio_max_turns_raw.strip() == "":
            audio_max_turns = DEFAULT_AUDIO_MAX_TURNS
        else:
            try:
                audio_max_turns = int(audio_max_turns_raw)
            except ValueError as exc:
                raise ConfigError(
                    "BRAIN_AUDIO_MAX_TURNS must be a positive even integer "
                    f"(got {audio_max_turns_raw!r})"
                ) from exc
            if audio_max_turns <= 0 or audio_max_turns % 2 != 0:
                raise ConfigError(
                    "BRAIN_AUDIO_MAX_TURNS must be a positive even integer "
                    f"(got {audio_max_turns_raw!r})"
                )

        audio_max_input_raw = os.environ.get("BRAIN_AUDIO_MAX_INPUT_TOKENS")
        if audio_max_input_raw is None or audio_max_input_raw.strip() == "":
            audio_max_input_tokens = DEFAULT_AUDIO_MAX_INPUT_TOKENS
        else:
            try:
                audio_max_input_tokens = int(audio_max_input_raw)
            except ValueError as exc:
                raise ConfigError(
                    "BRAIN_AUDIO_MAX_INPUT_TOKENS must be a positive integer "
                    f"(got {audio_max_input_raw!r})"
                ) from exc
            if audio_max_input_tokens <= 0:
                raise ConfigError(
                    "BRAIN_AUDIO_MAX_INPUT_TOKENS must be a positive integer "
                    f"(got {audio_max_input_raw!r})"
                )

        audio_theme_limit_raw = os.environ.get("BRAIN_AUDIO_THEME_LIMIT")
        if audio_theme_limit_raw is None or audio_theme_limit_raw.strip() == "":
            audio_theme_limit = DEFAULT_AUDIO_THEME_LIMIT
        else:
            try:
                audio_theme_limit = int(audio_theme_limit_raw)
            except ValueError as exc:
                raise ConfigError(
                    "BRAIN_AUDIO_THEME_LIMIT must be a positive integer "
                    f"(got {audio_theme_limit_raw!r})"
                ) from exc
            if audio_theme_limit <= 0:
                raise ConfigError(
                    "BRAIN_AUDIO_THEME_LIMIT must be a positive integer "
                    f"(got {audio_theme_limit_raw!r})"
                )
        # Plan 08 -- `brain gaps` knobs (positive ints).
        gaps_lookback_days = _parse_positive_int_env(
            "BRAIN_GAPS_LOOKBACK_DAYS", DEFAULT_GAPS_LOOKBACK_DAYS
        )
        gaps_min_cluster_size = _parse_positive_int_env(
            "BRAIN_GAPS_MIN_CLUSTER_SIZE", DEFAULT_GAPS_MIN_CLUSTER_SIZE
        )

        # Task 0B pre-landed scaffolding (plan 2026-07-25). Same eager-validation
        # idiom as every knob above: unset/blank -> default; invalid -> ConfigError
        # at startup so a typo surfaces before any command runs.
        secret_guard_raw = os.environ.get("BRAIN_SECRET_GUARD")
        if secret_guard_raw is None or secret_guard_raw.strip() == "":
            secret_guard = DEFAULT_SECRET_GUARD
        else:
            secret_guard = secret_guard_raw.strip().lower()
            if secret_guard not in _VALID_SECRET_GUARDS:
                raise ConfigError(
                    "BRAIN_SECRET_GUARD must be one of warn/redact/reject/off "
                    f"(got {secret_guard_raw!r})"
                )

        recall_budget_tokens = _parse_positive_int_env(
            "BRAIN_RECALL_BUDGET_TOKENS", DEFAULT_RECALL_BUDGET_TOKENS
        )
        recall_passage_tokens = _parse_positive_int_env(
            "BRAIN_RECALL_PASSAGE_TOKENS", DEFAULT_RECALL_PASSAGE_TOKENS
        )
        recall_max_candidates = _parse_positive_int_env(
            "BRAIN_RECALL_MAX_CANDIDATES", DEFAULT_RECALL_MAX_CANDIDATES
        )

        # Wave 3 -- MCP payload ceilings.
        show_max_content_tokens = _parse_non_negative_int_env(
            "BRAIN_SHOW_MAX_CONTENT_TOKENS", DEFAULT_SHOW_MAX_CONTENT_TOKENS
        )
        search_max_limit = _parse_positive_int_env(
            "BRAIN_SEARCH_MAX_LIMIT", DEFAULT_SEARCH_MAX_LIMIT
        )
        recall_max_budget_tokens = _parse_positive_int_env(
            "BRAIN_RECALL_MAX_BUDGET_TOKENS", DEFAULT_RECALL_MAX_BUDGET_TOKENS
        )
        graph_entities_max_limit = _parse_positive_int_env(
            "BRAIN_GRAPH_ENTITIES_MAX_LIMIT", DEFAULT_GRAPH_ENTITIES_MAX_LIMIT
        )
        mcp_rows_max_limit = _parse_positive_int_env(
            "BRAIN_MCP_ROWS_MAX_LIMIT", DEFAULT_MCP_ROWS_MAX_LIMIT
        )
        graph_communities_list_limit = _parse_positive_int_env(
            "BRAIN_GRAPH_COMMUNITIES_LIST_LIMIT",
            DEFAULT_GRAPH_COMMUNITIES_LIST_LIMIT,
        )
        # The default budget must fit under its own ceiling. Without this, an
        # operator who raises BRAIN_RECALL_BUDGET_TOKENS past the max breaks
        # EVERY default `brain_recall` call: the omitted `budget_tokens` falls
        # back to the configured default, which then trips the ceiling and
        # returns INVALID_PARAMS telling the *agent* to re-ask smaller — an
        # error that blames the caller for the operator's misconfiguration, on
        # a tool that is dead until someone reads the source. Eager ConfigError
        # at load is this module's idiom (it is why _parse_positive_int_env
        # exists); name both vars so the fix is unambiguous.
        if recall_budget_tokens > recall_max_budget_tokens:
            raise ConfigError(
                "BRAIN_RECALL_BUDGET_TOKENS "
                f"({recall_budget_tokens}) must be <= "
                f"BRAIN_RECALL_MAX_BUDGET_TOKENS ({recall_max_budget_tokens}); "
                "the default recall budget cannot exceed its own ceiling"
            )

        # Validated against the inlined :data:`AGENT_ID_PATTERN` -- NOT by
        # importing ``brain.agent.normalize_agent_id``, which does not exist
        # until Wave 4 (see the constant's comment above).
        agent_id_raw = os.environ.get("BRAIN_AGENT_ID")
        agent_id: str | None
        if agent_id_raw is None or agent_id_raw.strip() == "":
            agent_id = None
        else:
            agent_id = agent_id_raw.strip()
            if not _AGENT_ID_RE.match(agent_id):
                raise ConfigError(
                    f"BRAIN_AGENT_ID must match {AGENT_ID_PATTERN} "
                    f"(got {agent_id_raw!r})"
                )

        backup_dir = _default_backup_dir()

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
            "capture_title_words": capture_title_words,
            "capture_inbox_warn_threshold": capture_inbox_warn_threshold,
            "review_activity_limit": review_activity_limit,
            "review_theme_limit": review_theme_limit,
            "review_open_loop_limit": review_open_loop_limit,
            "brief_since_hours": brief_since_hours,
            "brief_todo_since_days": brief_todo_since_days,
            "brief_capture_limit": brief_capture_limit,
            "brief_pin_limit": brief_pin_limit,
            "recency_halflife_days": recency_halflife_days,
            "snippet_context_tokens": snippet_context_tokens,
            "snippet_max_chars": snippet_max_chars,
            "resurface_limit": resurface_limit,
            "resurface_min_age_days": resurface_min_age_days,
            "resurface_age_halflife_days": resurface_age_halflife_days,
            "resurface_access_halflife_days": resurface_access_halflife_days,
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
            "elicit_min_evidence_docs": elicit_min_evidence_docs,
            "elicit_min_gap_score": elicit_min_gap_score,
            "elicit_queue_limit": elicit_queue_limit,
            "elicit_contradiction_enabled": elicit_contradiction_enabled,
            "elicit_contradiction_min_docs": elicit_contradiction_min_docs,
            "timeline_granularity": timeline_granularity,
            "timeline_limit": timeline_limit,
            "timeline_synth_limit": timeline_synth_limit,
            "timeline_trim": timeline_trim,
            "connect_min_score": connect_min_score,
            "connect_candidate_limit": connect_candidate_limit,
            "connect_max_per_doc": connect_max_per_doc,
            "review_conflict_limit": review_conflict_limit,
            "review_conflict_pairs_per_entity": review_conflict_pairs_per_entity,
            "review_embed_sim_floor": review_embed_sim_floor,
            "review_stale_age_days": review_stale_age_days,
            "review_stale_supersede_window_days": review_stale_supersede_window_days,
            "review_stale_sim_floor": review_stale_sim_floor,
            "review_stale_limit": review_stale_limit,
            "ask_max_iterations": ask_max_iterations,
            "ask_docs_per_iter": ask_docs_per_iter,
            "ask_model": ask_model,
            "ask_timeout_seconds": ask_timeout_seconds,
            "audio_script_model": audio_script_model,
            "audio_max_turns": audio_max_turns,
            "audio_max_input_tokens": audio_max_input_tokens,
            "audio_theme_limit": audio_theme_limit,
            "gaps_lookback_days": gaps_lookback_days,
            "gaps_min_cluster_size": gaps_min_cluster_size,
            "secret_guard": secret_guard,
            "recall_budget_tokens": recall_budget_tokens,
            "recall_passage_tokens": recall_passage_tokens,
            "recall_max_candidates": recall_max_candidates,
            "show_max_content_tokens": show_max_content_tokens,
            "search_max_limit": search_max_limit,
            "recall_max_budget_tokens": recall_max_budget_tokens,
            "graph_entities_max_limit": graph_entities_max_limit,
            "mcp_rows_max_limit": mcp_rows_max_limit,
            "graph_communities_list_limit": graph_communities_list_limit,
            "agent_id": agent_id,
            "backup_dir": backup_dir,
        }
