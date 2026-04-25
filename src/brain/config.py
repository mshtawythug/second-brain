"""Configuration loading from environment / .env."""
import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import find_dotenv, load_dotenv


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
    database_url: str
    voyage_api_key: str

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
        voyage_api_key = os.environ.get("VOYAGE_API_KEY")
        if not database_url:
            raise ConfigError("DATABASE_URL is not set (see .env.example)")
        if not voyage_api_key:
            raise ConfigError("VOYAGE_API_KEY is not set (see .env.example)")
        return cls(database_url=database_url, voyage_api_key=voyage_api_key)
