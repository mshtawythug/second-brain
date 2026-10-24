"""AC3 of RM-08: a feature-settings knob set in a ``.env`` file is honoured after ``Config.load()``.

A leaf reads ``os.environ`` only after ``Config.load()`` has published the
dotenv chain into it (``Config._load_field_dict``'s ``os.environ.setdefault``
loop), so a leaf knob lives in the same ``.env`` as every other knob. Every
``.env`` here is under ``tmp_path``; ``tests/env_isolation.py`` removes the keys
``Config.load()`` publishes, after each test.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from brain.config import Config
from brain.settings._env import env_bool, env_choice, env_float, env_int, env_str
from tests.conftest import isolate_config_dotenv

INT_KNOB = "BRAIN_SETTINGS_PROBE_INT"
BOOL_KNOB = "BRAIN_SETTINGS_PROBE_BOOL"
CHOICE_KNOB = "BRAIN_SETTINGS_PROBE_CHOICE"
FLOAT_KNOB = "BRAIN_SETTINGS_PROBE_FLOAT"
STR_KNOB = "BRAIN_SETTINGS_PROBE_STR"
ALL_KNOBS = (INT_KNOB, BOOL_KNOB, CHOICE_KNOB, FLOAT_KNOB, STR_KNOB)

# Synthetic test database URL; Config.load() refuses to run without one and
# never connects to it.
DOTENV_DATABASE_URL = "postgresql://synthetic:synthetic@localhost:5434/never_opened"


@pytest.fixture
def project_env(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    """A project ``.env`` under ``tmp_path`` that is the only dotenv source."""
    env_file = tmp_path / "project.env"
    isolate_config_dotenv(
        monkeypatch,
        tmp_path,
        clear=("DATABASE_URL", *ALL_KNOBS),
        project_env=env_file,
    )
    return env_file


def test_dotenv_value_is_honoured_after_config_load(project_env: Path) -> None:
    project_env.write_text(
        f"DATABASE_URL={DOTENV_DATABASE_URL}\n"
        f"{INT_KNOB}=17\n"
        f"{BOOL_KNOB}=yes\n"
        f"{CHOICE_KNOB}=Voyage\n"
        f"{FLOAT_KNOB}=0.45\n"
        f"{STR_KNOB}=bge-reranker-v2-m3\n"
    )
    # Before the load, the knobs are only in the file: each parser sees the default.
    assert env_int(os.environ, INT_KNOB, 3, minimum=1) == 3

    Config.load()

    assert env_int(os.environ, INT_KNOB, 3, minimum=1) == 17
    assert env_bool(os.environ, BOOL_KNOB, False) is True
    assert env_choice(os.environ, CHOICE_KNOB, frozenset({"none", "voyage"}), "none") == "voyage"
    assert env_float(os.environ, FLOAT_KNOB, 0.1, minimum=0.0) == pytest.approx(0.45)
    assert env_str(os.environ, STR_KNOB, None) == "bge-reranker-v2-m3"


def test_process_env_still_wins_over_dotenv(
    monkeypatch: pytest.MonkeyPatch, project_env: Path
) -> None:
    project_env.write_text(f"DATABASE_URL={DOTENV_DATABASE_URL}\n{INT_KNOB}=17\n")
    monkeypatch.setenv(INT_KNOB, "42")

    Config.load()

    assert env_int(os.environ, INT_KNOB, 3, minimum=1) == 42


def test_knob_absent_from_dotenv_still_yields_default(project_env: Path) -> None:
    project_env.write_text(f"DATABASE_URL={DOTENV_DATABASE_URL}\n")

    Config.load()

    assert env_int(os.environ, INT_KNOB, 3, minimum=1) == 3
