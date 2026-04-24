"""Configuration loading from environment / .env."""
import os
from dataclasses import dataclass

from dotenv import load_dotenv


class ConfigError(RuntimeError):
    pass


@dataclass(frozen=True)
class Config:
    database_url: str
    voyage_api_key: str

    @classmethod
    def load(cls) -> "Config":
        load_dotenv(override=False)
        database_url = os.environ.get("DATABASE_URL")
        voyage_api_key = os.environ.get("VOYAGE_API_KEY")
        if not database_url:
            raise ConfigError("DATABASE_URL is not set (see .env.example)")
        if not voyage_api_key:
            raise ConfigError("VOYAGE_API_KEY is not set (see .env.example)")
        return cls(database_url=database_url, voyage_api_key=voyage_api_key)
