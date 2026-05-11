"""Tests for brain.config — env loading."""
import os
from pathlib import Path

import pytest

from brain import config as config_module
from brain.config import (
    DEFAULT_EMBEDDER,
    DEFAULT_OLLAMA_HOST,
    DEFAULT_QWEN3_MODEL,
    Config,
    ConfigError,
)


@pytest.fixture
def isolated_dotenv(monkeypatch, tmp_path: Path) -> Path:
    """Isolate Config.load() from any real .env discovery.

    Redirects all three lookup paths:
      - cwd walk-up: chdir into an empty tmp dir (write tmp_path/.env to
        exercise this branch).
      - project .env: point _project_dotenv at tmp_path/project.env (caller
        writes to enable this branch).
      - BRAIN_HOME .env: point _brain_home_dotenv at tmp_path/brain_home.env
        (caller writes to enable this branch).

    Returns the tmp path the project_dotenv shim points at; tests can write a
    fake .env there to exercise the project-fallback branch.
    """
    monkeypatch.chdir(tmp_path)
    fake_project_env = tmp_path / "project.env"
    monkeypatch.setattr(config_module, "_project_dotenv", lambda: fake_project_env)
    fake_brain_home_env = tmp_path / "brain_home.env"
    monkeypatch.setattr(config_module, "_brain_home_dotenv", lambda: fake_brain_home_env)
    # Strip any inherited overrides so default-vs-explicit tests stay
    # deterministic regardless of the developer's shell env.
    monkeypatch.delenv("OLLAMA_HOST", raising=False)
    monkeypatch.delenv("QWEN3_MODEL", raising=False)
    monkeypatch.delenv("BRAIN_EMBEDDER", raising=False)
    monkeypatch.delenv("VOYAGE_API_KEY", raising=False)
    monkeypatch.delenv("BRAIN_OWNER_PARTICIPANTS", raising=False)
    monkeypatch.delenv("BRAIN_PEOPLE_HUB_MIN_DOCS", raising=False)
    monkeypatch.delenv("BRAIN_HOME", raising=False)
    return fake_project_env


def test_loads_database_url(monkeypatch, isolated_dotenv):
    monkeypatch.setenv("DATABASE_URL", "postgresql://x:y@h:5432/d")
    cfg = Config.load()
    assert cfg.database_url == "postgresql://x:y@h:5432/d"


def test_missing_database_url_raises(monkeypatch, isolated_dotenv):
    monkeypatch.delenv("DATABASE_URL", raising=False)
    with pytest.raises(ConfigError, match="DATABASE_URL"):
        Config.load()


def test_ollama_host_and_model_default_when_unset(monkeypatch, isolated_dotenv):
    """With only DATABASE_URL set, defaults apply for OLLAMA_HOST and QWEN3_MODEL."""
    monkeypatch.setenv("DATABASE_URL", "postgresql://x:y@h:5432/d")
    cfg = Config.load()
    assert cfg.ollama_host == DEFAULT_OLLAMA_HOST
    assert cfg.qwen3_model == DEFAULT_QWEN3_MODEL


def test_ollama_host_and_model_read_from_env(monkeypatch, isolated_dotenv):
    """OLLAMA_HOST and QWEN3_MODEL are honored when set."""
    monkeypatch.setenv("DATABASE_URL", "postgresql://x:y@h:5432/d")
    monkeypatch.setenv("OLLAMA_HOST", "http://203.0.113.10:11434")
    monkeypatch.setenv("QWEN3_MODEL", "qwen3-embedding:4b")
    cfg = Config.load()
    assert cfg.ollama_host == "http://203.0.113.10:11434"
    assert cfg.qwen3_model == "qwen3-embedding:4b"


# ---------------------------------------------------------------------------
# Regression tests for "make brain CLI work from any cwd" — the project's
# .env must be discovered even when the cwd has no walk-up .env.
# ---------------------------------------------------------------------------


def test_loads_dotenv_from_project_root_when_cwd_unrelated(
    monkeypatch, isolated_dotenv: Path
):
    """When cwd has no .env, Config.load() falls back to the project .env."""
    monkeypatch.delenv("DATABASE_URL", raising=False)
    isolated_dotenv.write_text(
        "DATABASE_URL=postgresql://from-project-env:5432/db\n"
    )
    cfg = Config.load()
    assert cfg.database_url == "postgresql://from-project-env:5432/db"


def test_environment_overrides_project_dotenv(monkeypatch, isolated_dotenv: Path):
    """Shell-set / monkeypatched env wins over the project .env."""
    isolated_dotenv.write_text(
        "DATABASE_URL=postgresql://from-project-env:5432/db\n"
    )
    monkeypatch.setenv("DATABASE_URL", "postgresql://from-shell:5432/db")
    cfg = Config.load()
    assert cfg.database_url == "postgresql://from-shell:5432/db"


def test_missing_dotenv_falls_through_to_strict_error(
    monkeypatch, isolated_dotenv: Path
):
    """No env, no project .env → ConfigError."""
    monkeypatch.delenv("DATABASE_URL", raising=False)
    assert not isolated_dotenv.exists()
    with pytest.raises(ConfigError):
        Config.load()


def test_project_dotenv_points_at_repo_root():
    """_project_dotenv resolves to <repo>/.env, two parents above the module."""
    from brain.config import _project_dotenv

    expected = Path(config_module.__file__).resolve().parent.parent.parent / ".env"
    assert _project_dotenv() == expected


# ---------------------------------------------------------------------------
# Phase 3.5: pluggable embedder backend selection.
# ---------------------------------------------------------------------------


def test_embedder_defaults_to_arctic(monkeypatch, isolated_dotenv):
    """No ``BRAIN_EMBEDDER`` set → arctic (the user-friendly default)."""
    monkeypatch.setenv("DATABASE_URL", "postgresql://x:y@h:5432/d")
    cfg = Config.load()
    assert cfg.embedder == DEFAULT_EMBEDDER
    assert cfg.embedder == "arctic"


def test_embedder_env_var_override(monkeypatch, isolated_dotenv):
    """``BRAIN_EMBEDDER=qwen3`` is honored verbatim (lowercased)."""
    monkeypatch.setenv("DATABASE_URL", "postgresql://x:y@h:5432/d")
    monkeypatch.setenv("BRAIN_EMBEDDER", "qwen3")
    cfg = Config.load()
    assert cfg.embedder == "qwen3"


def test_embedder_env_var_lowercased(monkeypatch, isolated_dotenv):
    """``BRAIN_EMBEDDER=ARCTIC`` is normalized to lowercase before validation."""
    monkeypatch.setenv("DATABASE_URL", "postgresql://x:y@h:5432/d")
    monkeypatch.setenv("BRAIN_EMBEDDER", "ARCTIC")
    cfg = Config.load()
    assert cfg.embedder == "arctic"


def test_embedder_invalid_value_raises(monkeypatch, isolated_dotenv):
    """Anything outside the 3-backend whitelist raises at load time."""
    monkeypatch.setenv("DATABASE_URL", "postgresql://x:y@h:5432/d")
    monkeypatch.setenv("BRAIN_EMBEDDER", "bogus-backend")
    with pytest.raises(ConfigError, match="must be one of"):
        Config.load()


def test_voyage_api_key_is_none_by_default(monkeypatch, isolated_dotenv):
    """Without ``VOYAGE_API_KEY`` set, the field is ``None`` (validated at use)."""
    monkeypatch.setenv("DATABASE_URL", "postgresql://x:y@h:5432/d")
    cfg = Config.load()
    assert cfg.voyage_api_key is None


def test_voyage_api_key_read_from_env(monkeypatch, isolated_dotenv):
    """``VOYAGE_API_KEY`` env var populates the optional field."""
    monkeypatch.setenv("DATABASE_URL", "postgresql://x:y@h:5432/d")
    monkeypatch.setenv("VOYAGE_API_KEY", "vk-test-secret")
    cfg = Config.load()
    assert cfg.voyage_api_key == "vk-test-secret"


# ---------------------------------------------------------------------------
# Vault path config (Phase 1 of the vault model).
# ---------------------------------------------------------------------------


def test_vault_path_defaults_to_home_brain_vault(monkeypatch, isolated_dotenv):
    """No ``BRAIN_VAULT_PATH`` set → ``~/brain-vault``."""
    from brain.config import DEFAULT_VAULT_PATH
    monkeypatch.setenv("DATABASE_URL", "postgresql://x:y@h:5432/d")
    monkeypatch.delenv("BRAIN_VAULT_PATH", raising=False)
    cfg = Config.load()
    assert cfg.vault_path == DEFAULT_VAULT_PATH
    assert cfg.vault_path.name == "brain-vault"


def test_vault_path_env_var_override(monkeypatch, isolated_dotenv, tmp_path):
    """``BRAIN_VAULT_PATH=...`` is honored verbatim."""
    monkeypatch.setenv("DATABASE_URL", "postgresql://x:y@h:5432/d")
    monkeypatch.setenv("BRAIN_VAULT_PATH", str(tmp_path / "custom-vault"))
    cfg = Config.load()
    assert cfg.vault_path == tmp_path / "custom-vault"


def test_vault_path_expands_user_tilde(monkeypatch, isolated_dotenv):
    """A leading ``~`` in BRAIN_VAULT_PATH expands to the home directory."""
    monkeypatch.setenv("DATABASE_URL", "postgresql://x:y@h:5432/d")
    monkeypatch.setenv("BRAIN_VAULT_PATH", "~/my-brain")
    cfg = Config.load()
    assert cfg.vault_path == Path.home() / "my-brain"


# ---------------------------------------------------------------------------
# BRAIN_OWNER_PARTICIPANTS — corpus-owner identifiers stripped from
# ``DocSnapshot.participant_keys`` before R2/R3 derived-edge rules evaluate.
# ---------------------------------------------------------------------------


class TestOwnerParticipants:
    """Env parsing for ``BRAIN_OWNER_PARTICIPANTS``."""

    def test_unset_yields_empty_frozenset(
        self, monkeypatch, isolated_dotenv
    ) -> None:
        monkeypatch.setenv("DATABASE_URL", "postgresql://x:y@h:5432/d")
        monkeypatch.delenv("BRAIN_OWNER_PARTICIPANTS", raising=False)
        cfg = Config.load()
        assert cfg.owner_participants == frozenset()

    def test_blank_value_yields_empty_frozenset(
        self, monkeypatch, isolated_dotenv
    ) -> None:
        # An empty / whitespace-only env var must not produce phantom keys
        # like ``""`` or ``"   "`` that would compare-equal to a stripped key.
        monkeypatch.setenv("DATABASE_URL", "postgresql://x:y@h:5432/d")
        monkeypatch.setenv("BRAIN_OWNER_PARTICIPANTS", "   ")
        cfg = Config.load()
        assert cfg.owner_participants == frozenset()

    def test_csv_lowercased_and_trimmed(
        self, monkeypatch, isolated_dotenv
    ) -> None:
        monkeypatch.setenv("DATABASE_URL", "postgresql://x:y@h:5432/d")
        monkeypatch.setenv(
            "BRAIN_OWNER_PARTICIPANTS",
            "Ali Sarkis,redacted@example.com",
        )
        cfg = Config.load()
        assert cfg.owner_participants == frozenset(
            {"ali sarkis", "redacted@example.com"}
        )

    def test_csv_drops_empty_entries_and_normalises(
        self, monkeypatch, isolated_dotenv
    ) -> None:
        # Whitespace-padded entries get trimmed; trailing empties from
        # ``Ali ,, ALI@x.com ,`` are dropped; mixed case is folded.
        monkeypatch.setenv("DATABASE_URL", "postgresql://x:y@h:5432/d")
        monkeypatch.setenv(
            "BRAIN_OWNER_PARTICIPANTS",
            "  Ali  ,  ALI@x.com  ,  ",
        )
        cfg = Config.load()
        assert cfg.owner_participants == frozenset({"ali", "ali@x.com"})

    def test_duplicates_collapsed(
        self, monkeypatch, isolated_dotenv
    ) -> None:
        # frozenset() naturally de-dupes, but we still pin the contract so a
        # mixed-case duplicate entry doesn't double-count via casing skew.
        monkeypatch.setenv("DATABASE_URL", "postgresql://x:y@h:5432/d")
        monkeypatch.setenv(
            "BRAIN_OWNER_PARTICIPANTS",
            "ali@example.com, ALI@EXAMPLE.COM ,Ali Sarkis,ali sarkis",
        )
        cfg = Config.load()
        assert cfg.owner_participants == frozenset(
            {"ali@example.com", "ali sarkis"}
        )


# ---------------------------------------------------------------------------
# BRAIN_PEOPLE_HUB_MIN_DOCS — doc-count threshold for the People Hub.
# Phase C of the 2026-05-07 People Hub plan.
# ---------------------------------------------------------------------------


class TestPeopleHubMinDocs:
    """Env parsing + validation for ``BRAIN_PEOPLE_HUB_MIN_DOCS``."""

    def test_unset_yields_default(self, monkeypatch, isolated_dotenv) -> None:
        from brain.config import DEFAULT_PEOPLE_HUB_MIN_DOCS

        monkeypatch.setenv("DATABASE_URL", "postgresql://x:y@h:5432/d")
        monkeypatch.delenv("BRAIN_PEOPLE_HUB_MIN_DOCS", raising=False)
        cfg = Config.load()
        assert cfg.people_hub_min_docs == DEFAULT_PEOPLE_HUB_MIN_DOCS
        assert cfg.people_hub_min_docs == 3

    def test_blank_value_falls_back_to_default(
        self, monkeypatch, isolated_dotenv
    ) -> None:
        # Mirrors BRAIN_VECTOR_SIM_FLOOR — whitespace-only env values
        # are treated as "not set" rather than "empty integer" so a
        # ``.env`` with a quoted blank survives load.
        from brain.config import DEFAULT_PEOPLE_HUB_MIN_DOCS

        monkeypatch.setenv("DATABASE_URL", "postgresql://x:y@h:5432/d")
        monkeypatch.setenv("BRAIN_PEOPLE_HUB_MIN_DOCS", "   ")
        cfg = Config.load()
        assert cfg.people_hub_min_docs == DEFAULT_PEOPLE_HUB_MIN_DOCS

    def test_valid_integer_honored(self, monkeypatch, isolated_dotenv) -> None:
        monkeypatch.setenv("DATABASE_URL", "postgresql://x:y@h:5432/d")
        monkeypatch.setenv("BRAIN_PEOPLE_HUB_MIN_DOCS", "5")
        cfg = Config.load()
        assert cfg.people_hub_min_docs == 5

    def test_zero_is_valid(self, monkeypatch, isolated_dotenv) -> None:
        # Zero is a valid configuration: "render every person regardless
        # of doc count". Curated-only is the goal — the threshold floor
        # is opt-in via ``in_people_yml`` always-on rendering.
        monkeypatch.setenv("DATABASE_URL", "postgresql://x:y@h:5432/d")
        monkeypatch.setenv("BRAIN_PEOPLE_HUB_MIN_DOCS", "0")
        cfg = Config.load()
        assert cfg.people_hub_min_docs == 0

    def test_negative_value_rejected(
        self, monkeypatch, isolated_dotenv
    ) -> None:
        monkeypatch.setenv("DATABASE_URL", "postgresql://x:y@h:5432/d")
        monkeypatch.setenv("BRAIN_PEOPLE_HUB_MIN_DOCS", "-1")
        with pytest.raises(ConfigError, match="non-negative integer"):
            Config.load()

    def test_non_integer_value_rejected(
        self, monkeypatch, isolated_dotenv
    ) -> None:
        monkeypatch.setenv("DATABASE_URL", "postgresql://x:y@h:5432/d")
        monkeypatch.setenv("BRAIN_PEOPLE_HUB_MIN_DOCS", "three")
        with pytest.raises(ConfigError, match="non-negative integer"):
            Config.load()


# ---------------------------------------------------------------------------
# BRAIN_RECENCY_HALFLIFE_DAYS — exponential-decay half-life for the recency
# boost applied after RRF (Wave Q1-A, 2026-05-11).
# ---------------------------------------------------------------------------


class TestRecencyHalflifeDays:
    """Env parsing + validation for ``BRAIN_RECENCY_HALFLIFE_DAYS``."""

    def test_unset_yields_default(self, monkeypatch, isolated_dotenv) -> None:
        from brain.config import DEFAULT_RECENCY_HALFLIFE_DAYS

        monkeypatch.setenv("DATABASE_URL", "postgresql://x:y@h:5432/d")
        monkeypatch.delenv("BRAIN_RECENCY_HALFLIFE_DAYS", raising=False)
        cfg = Config.load()
        assert cfg.recency_halflife_days == DEFAULT_RECENCY_HALFLIFE_DAYS
        assert cfg.recency_halflife_days == 180.0

    def test_blank_value_falls_back_to_default(
        self, monkeypatch, isolated_dotenv
    ) -> None:
        from brain.config import DEFAULT_RECENCY_HALFLIFE_DAYS

        monkeypatch.setenv("DATABASE_URL", "postgresql://x:y@h:5432/d")
        monkeypatch.setenv("BRAIN_RECENCY_HALFLIFE_DAYS", "   ")
        cfg = Config.load()
        assert cfg.recency_halflife_days == DEFAULT_RECENCY_HALFLIFE_DAYS

    def test_valid_float_honored(self, monkeypatch, isolated_dotenv) -> None:
        monkeypatch.setenv("DATABASE_URL", "postgresql://x:y@h:5432/d")
        monkeypatch.setenv("BRAIN_RECENCY_HALFLIFE_DAYS", "365.0")
        cfg = Config.load()
        assert cfg.recency_halflife_days == 365.0

    def test_integer_string_accepted(self, monkeypatch, isolated_dotenv) -> None:
        monkeypatch.setenv("DATABASE_URL", "postgresql://x:y@h:5432/d")
        monkeypatch.setenv("BRAIN_RECENCY_HALFLIFE_DAYS", "90")
        cfg = Config.load()
        assert cfg.recency_halflife_days == 90.0

    def test_zero_is_invalid(self, monkeypatch, isolated_dotenv) -> None:
        monkeypatch.setenv("DATABASE_URL", "postgresql://x:y@h:5432/d")
        monkeypatch.setenv("BRAIN_RECENCY_HALFLIFE_DAYS", "0")
        with pytest.raises(ConfigError, match="positive float"):
            Config.load()

    def test_negative_is_invalid(self, monkeypatch, isolated_dotenv) -> None:
        monkeypatch.setenv("DATABASE_URL", "postgresql://x:y@h:5432/d")
        monkeypatch.setenv("BRAIN_RECENCY_HALFLIFE_DAYS", "-1")
        with pytest.raises(ConfigError, match="positive float"):
            Config.load()

    def test_non_numeric_raises(self, monkeypatch, isolated_dotenv) -> None:
        monkeypatch.setenv("DATABASE_URL", "postgresql://x:y@h:5432/d")
        monkeypatch.setenv("BRAIN_RECENCY_HALFLIFE_DAYS", "abc")
        with pytest.raises(ConfigError, match="positive float"):
            Config.load()

    def test_large_value_effectively_disables_boost(
        self, monkeypatch, isolated_dotenv
    ) -> None:
        """A very large half-life is valid — caller treats it as 'no boost'."""
        monkeypatch.setenv("DATABASE_URL", "postgresql://x:y@h:5432/d")
        monkeypatch.setenv("BRAIN_RECENCY_HALFLIFE_DAYS", "999999")
        cfg = Config.load()
        assert cfg.recency_halflife_days == 999999.0


# ---------------------------------------------------------------------------
# BRAIN_SNIPPET_CONTEXT_TOKENS — neighboring-chunk token budget (Wave Q1-A).
# ---------------------------------------------------------------------------


class TestSnippetContextTokens:
    """Env parsing + validation for ``BRAIN_SNIPPET_CONTEXT_TOKENS``."""

    def test_unset_yields_default(self, monkeypatch, isolated_dotenv) -> None:
        from brain.config import DEFAULT_SNIPPET_CONTEXT_TOKENS

        monkeypatch.setenv("DATABASE_URL", "postgresql://x:y@h:5432/d")
        monkeypatch.delenv("BRAIN_SNIPPET_CONTEXT_TOKENS", raising=False)
        cfg = Config.load()
        assert cfg.snippet_context_tokens == DEFAULT_SNIPPET_CONTEXT_TOKENS
        assert cfg.snippet_context_tokens == 200

    def test_blank_value_falls_back_to_default(
        self, monkeypatch, isolated_dotenv
    ) -> None:
        from brain.config import DEFAULT_SNIPPET_CONTEXT_TOKENS

        monkeypatch.setenv("DATABASE_URL", "postgresql://x:y@h:5432/d")
        monkeypatch.setenv("BRAIN_SNIPPET_CONTEXT_TOKENS", "   ")
        cfg = Config.load()
        assert cfg.snippet_context_tokens == DEFAULT_SNIPPET_CONTEXT_TOKENS

    def test_valid_integer_honored(self, monkeypatch, isolated_dotenv) -> None:
        monkeypatch.setenv("DATABASE_URL", "postgresql://x:y@h:5432/d")
        monkeypatch.setenv("BRAIN_SNIPPET_CONTEXT_TOKENS", "500")
        cfg = Config.load()
        assert cfg.snippet_context_tokens == 500

    def test_zero_is_valid_disables_expansion(
        self, monkeypatch, isolated_dotenv
    ) -> None:
        """Zero is explicitly allowed — it disables snippet expansion."""
        monkeypatch.setenv("DATABASE_URL", "postgresql://x:y@h:5432/d")
        monkeypatch.setenv("BRAIN_SNIPPET_CONTEXT_TOKENS", "0")
        cfg = Config.load()
        assert cfg.snippet_context_tokens == 0

    def test_negative_is_invalid(self, monkeypatch, isolated_dotenv) -> None:
        monkeypatch.setenv("DATABASE_URL", "postgresql://x:y@h:5432/d")
        monkeypatch.setenv("BRAIN_SNIPPET_CONTEXT_TOKENS", "-1")
        with pytest.raises(ConfigError, match="non-negative integer"):
            Config.load()

    def test_non_integer_raises(self, monkeypatch, isolated_dotenv) -> None:
        monkeypatch.setenv("DATABASE_URL", "postgresql://x:y@h:5432/d")
        monkeypatch.setenv("BRAIN_SNIPPET_CONTEXT_TOKENS", "3.5")
        with pytest.raises(ConfigError, match="non-negative integer"):
            Config.load()

    def test_non_numeric_raises(self, monkeypatch, isolated_dotenv) -> None:
        monkeypatch.setenv("DATABASE_URL", "postgresql://x:y@h:5432/d")
        monkeypatch.setenv("BRAIN_SNIPPET_CONTEXT_TOKENS", "many")
        with pytest.raises(ConfigError, match="non-negative integer"):
            Config.load()


# ---------------------------------------------------------------------------
# T1.0 — four-source dotenv chain with process-env precedence
#
# Priority (highest → lowest):
#   1. os.environ  (process env, never overwritten)
#   2. <repo-root>/.env  (_project_dotenv, shim → tmp_path/project.env)
#   3. <cwd>/.env        (find_dotenv(usecwd=True) → tmp_path/.env when cwd=tmp_path)
#   4. $BRAIN_HOME/.env  (_brain_home_dotenv, shim → tmp_path/brain_home.env)
# ---------------------------------------------------------------------------


class TestDotenvChain:
    """Dotenv chain: per-source presence + all required conflict cases (T1.0)."""

    # ------------------------------------------------------------------
    # Per-source presence tests — each branch alone produces the right value
    # ------------------------------------------------------------------

    def test_brain_home_env_loaded_as_lowest_priority(
        self, monkeypatch, isolated_dotenv: Path, tmp_path: Path
    ) -> None:
        """BRAIN_HOME/.env is read when no other source defines the key."""
        monkeypatch.delenv("DATABASE_URL", raising=False)
        (tmp_path / "brain_home.env").write_text(
            "DATABASE_URL=postgresql://from-brain-home:5432/db\n"
        )
        cfg = Config.load()
        assert cfg.database_url == "postgresql://from-brain-home:5432/db"

    def test_cwd_env_loaded_alone(
        self, monkeypatch, isolated_dotenv: Path, tmp_path: Path
    ) -> None:
        """cwd/.env is read when BRAIN_HOME/.env is absent."""
        monkeypatch.delenv("DATABASE_URL", raising=False)
        (tmp_path / ".env").write_text(
            "DATABASE_URL=postgresql://from-cwd:5432/db\n"
        )
        cfg = Config.load()
        assert cfg.database_url == "postgresql://from-cwd:5432/db"

    def test_project_env_loaded_alone(
        self, monkeypatch, isolated_dotenv: Path, tmp_path: Path
    ) -> None:
        """repo-root/.env is read when cwd and BRAIN_HOME sources are absent."""
        monkeypatch.delenv("DATABASE_URL", raising=False)
        isolated_dotenv.write_text(
            "DATABASE_URL=postgresql://from-project:5432/db\n"
        )
        cfg = Config.load()
        assert cfg.database_url == "postgresql://from-project:5432/db"

    def test_nothing_found_env_vars_only_fallback(
        self, monkeypatch, isolated_dotenv: Path, tmp_path: Path
    ) -> None:
        """Config.load() works with no .env files — process env alone is sufficient."""
        monkeypatch.setenv("DATABASE_URL", "postgresql://env-only:5432/db")
        assert not (tmp_path / ".env").exists()
        assert not isolated_dotenv.exists()
        assert not (tmp_path / "brain_home.env").exists()
        cfg = Config.load()
        assert cfg.database_url == "postgresql://env-only:5432/db"

    # ------------------------------------------------------------------
    # Conflict cases — when two sources define the same key, higher wins
    # ------------------------------------------------------------------

    def test_conflict_a_process_env_beats_brain_home(
        self, monkeypatch, isolated_dotenv: Path, tmp_path: Path
    ) -> None:
        """(a) os.environ beats BRAIN_HOME/.env for the same key."""
        monkeypatch.setenv("DATABASE_URL", "postgresql://process-wins:5432/db")
        (tmp_path / "brain_home.env").write_text(
            "DATABASE_URL=postgresql://brain-home-loses:5432/db\n"
        )
        cfg = Config.load()
        assert cfg.database_url == "postgresql://process-wins:5432/db"

    def test_conflict_b_project_env_beats_brain_home(
        self, monkeypatch, isolated_dotenv: Path, tmp_path: Path
    ) -> None:
        """(b) repo-root/.env beats BRAIN_HOME/.env for the same key."""
        monkeypatch.delenv("DATABASE_URL", raising=False)
        monkeypatch.delenv("OLLAMA_HOST", raising=False)
        (tmp_path / "brain_home.env").write_text(
            "DATABASE_URL=postgresql://x:5432/d\nOLLAMA_HOST=http://brain-home:11434\n"
        )
        isolated_dotenv.write_text(
            "DATABASE_URL=postgresql://x:5432/d\nOLLAMA_HOST=http://project-root:11434\n"
        )
        cfg = Config.load()
        assert cfg.ollama_host == "http://project-root:11434"

    def test_conflict_c_cwd_env_beats_brain_home(
        self, monkeypatch, isolated_dotenv: Path, tmp_path: Path
    ) -> None:
        """(c) cwd/.env beats BRAIN_HOME/.env for the same key."""
        monkeypatch.delenv("DATABASE_URL", raising=False)
        monkeypatch.delenv("OLLAMA_HOST", raising=False)
        (tmp_path / "brain_home.env").write_text(
            "DATABASE_URL=postgresql://x:5432/d\nOLLAMA_HOST=http://brain-home:11434\n"
        )
        (tmp_path / ".env").write_text("OLLAMA_HOST=http://cwd:11434\n")
        cfg = Config.load()
        assert cfg.ollama_host == "http://cwd:11434"

    def test_conflict_d_project_env_beats_cwd_env(
        self, monkeypatch, isolated_dotenv: Path, tmp_path: Path
    ) -> None:
        """(d) repo-root/.env beats cwd/.env for the same key."""
        monkeypatch.delenv("DATABASE_URL", raising=False)
        monkeypatch.delenv("OLLAMA_HOST", raising=False)
        (tmp_path / ".env").write_text(
            "DATABASE_URL=postgresql://x:5432/d\nOLLAMA_HOST=http://cwd:11434\n"
        )
        isolated_dotenv.write_text("OLLAMA_HOST=http://project-root:11434\n")
        cfg = Config.load()
        assert cfg.ollama_host == "http://project-root:11434"

    # ------------------------------------------------------------------
    # Explicit non-overwrite guarantee
    # ------------------------------------------------------------------

    def test_process_env_never_overwritten(
        self, monkeypatch, isolated_dotenv: Path, tmp_path: Path
    ) -> None:
        """os.environ value is never replaced by ANY .env file source.

        Sets DATABASE_URL in process env BEFORE calling Config.load(), then
        checks that all three .env files cannot clobber it.
        """
        monkeypatch.setenv("DATABASE_URL", "postgresql://process-env:5432/db")
        (tmp_path / "brain_home.env").write_text(
            "DATABASE_URL=postgresql://brain-home-should-not-win:5432/db\n"
        )
        (tmp_path / ".env").write_text(
            "DATABASE_URL=postgresql://cwd-should-not-win:5432/db\n"
        )
        isolated_dotenv.write_text(
            "DATABASE_URL=postgresql://project-should-not-win:5432/db\n"
        )
        Config.load()
        # The process-env value must survive through Config.load() unchanged.
        assert os.environ["DATABASE_URL"] == "postgresql://process-env:5432/db"
