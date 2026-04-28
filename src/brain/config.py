"""Configuration loading from environment / .env."""
import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import find_dotenv, load_dotenv

DEFAULT_OLLAMA_HOST = "http://localhost:11434"
DEFAULT_QWEN3_MODEL = "qwen3-embedding:8b"
DEFAULT_EMBEDDER = "arctic"
_VALID_EMBEDDERS = {"arctic", "voyage", "qwen3"}


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
    """

    database_url: str
    ollama_host: str = DEFAULT_OLLAMA_HOST
    qwen3_model: str = DEFAULT_QWEN3_MODEL
    embedder: str = DEFAULT_EMBEDDER
    voyage_api_key: str | None = None

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
        return cls(
            database_url=database_url,
            ollama_host=ollama_host,
            qwen3_model=qwen3_model,
            embedder=embedder,
            voyage_api_key=voyage_api_key,
        )
