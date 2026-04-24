"""Test harness — uses a real Postgres test DB and a fake embedder.

The test DB is reset (schema dropped + recreated) before each test that uses it.
"""

import hashlib
import os
from collections.abc import Iterator
from pathlib import Path

import psycopg
import pytest
from dotenv import load_dotenv

load_dotenv()

TEST_DATABASE_URL = os.environ.get(
    "TEST_DATABASE_URL",
    "postgresql://brain:brain@localhost:5433/second_brain_test",
)


def _migrations_dir() -> Path:
    return Path(__file__).parent.parent / "migrations"


def _apply_migrations(conn: psycopg.Connection) -> None:
    for sql_file in sorted(_migrations_dir().glob("*.sql")):
        conn.execute(sql_file.read_text())


@pytest.fixture
def test_db() -> Iterator[psycopg.Connection]:
    """Fresh schema in the test DB for each test."""
    with psycopg.connect(TEST_DATABASE_URL, autocommit=True) as conn:
        conn.execute("DROP SCHEMA public CASCADE; CREATE SCHEMA public;")
        _apply_migrations(conn)
        yield conn


class FakeEmbedder:
    """Deterministic embedder for tests — hashes text into a stable 1024-dim vector."""

    def embed(self, texts: list[str], input_type: str = "document") -> list[list[float]]:
        return [self._vec(t, input_type) for t in texts]

    def count_tokens(self, text: str) -> int:
        # rough approximation: 1 token per ~4 chars
        return max(1, len(text) // 4)

    @staticmethod
    def _vec(text: str, input_type: str) -> list[float]:
        h = hashlib.sha256((input_type + ":" + text).encode()).digest()
        # 32 bytes → 32 floats, tiled to 1024
        floats = [b / 255.0 - 0.5 for b in h]
        return (floats * 32)[:1024]


@pytest.fixture
def fake_embedder() -> FakeEmbedder:
    return FakeEmbedder()


@pytest.fixture
def fixtures_dir() -> Path:
    return Path(__file__).parent / "fixtures"
