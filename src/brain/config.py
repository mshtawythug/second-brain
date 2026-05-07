"""Configuration loading from environment / .env."""
import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import find_dotenv, load_dotenv

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

    @classmethod
    def load(cls) -> "Config":
        # First, walk upward from the actual cwd. ``usecwd=True`` prevents
        # python-dotenv from inspecting caller frames (which would silently
        # resolve to <repo>/.env regardless of cwd) and gives explicit,
        # testable behavior: a project-local .env in the user's cwd wins for
        # in-repo runs.
        load_dotenv(find_dotenv(usecwd=True), override=False)
        # Then, fall back to the project's .env so `brain` works from any cwd.
        # override=False ensures shell env (and pytest monkeypatch.setenv) wins
        # and any cwd-discovered .env wins over the project .env when both set.
        project_env = _project_dotenv()
        if project_env.is_file():
            load_dotenv(project_env, override=False)
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
        return cls(
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
        )
