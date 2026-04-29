"""Tests for brain.config — env loading."""
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

    Redirects both lookup paths:
      - cwd walk-up: chdir into an empty tmp dir.
      - project .env: point _project_dotenv at a tmp path the caller controls.

    Returns the tmp path the project_dotenv shim points at; tests can write a
    fake .env there to exercise the project-fallback branch.
    """
    monkeypatch.chdir(tmp_path)
    fake_project_env = tmp_path / "project.env"
    monkeypatch.setattr(config_module, "_project_dotenv", lambda: fake_project_env)
    # Strip any inherited Ollama overrides so default-vs-explicit tests stay
    # deterministic regardless of the developer's shell env.
    monkeypatch.delenv("OLLAMA_HOST", raising=False)
    monkeypatch.delenv("QWEN3_MODEL", raising=False)
    monkeypatch.delenv("BRAIN_EMBEDDER", raising=False)
    monkeypatch.delenv("VOYAGE_API_KEY", raising=False)
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
