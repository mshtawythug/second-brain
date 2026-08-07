"""Tests for the Wave-0 pre-landed :class:`Config` fields (Task 0B, plan 2026-07-25).

Mirrors ``test_config_audio_envvars.py``: the ``isolated_dotenv`` fixture blocks
every on-disk ``.env`` source so only ``os.environ`` reaches ``Config.load()``,
then each new knob is asserted for its default, a valid override, and the eager
``ConfigError`` validation path.

These six fields are scaffolding — no feature reads them yet. They land in Wave 0
so ``config.py`` has exactly one writer for the whole release and the parallel
Wave-1/4/5 worktrees never collide on it.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from brain import config as config_module
from brain.config import (
    AGENT_ID_PATTERN,
    DEFAULT_BACKUP_DIR_NAME,
    DEFAULT_RECALL_BUDGET_TOKENS,
    DEFAULT_RECALL_MAX_CANDIDATES,
    DEFAULT_RECALL_PASSAGE_TOKENS,
    DEFAULT_SECRET_GUARD,
    Config,
    ConfigError,
)
from tests.conftest import TEST_DATABASE_URL

# Resolved from the environment via conftest — a pinned literal here
# diverges from the database the test_db fixture actually uses the
# moment anyone overrides TEST_DATABASE_URL (every parallel agent, CI).
_TEST_DATABASE_URL = TEST_DATABASE_URL

_NEW_ENV_VARS = (
    "BRAIN_SECRET_GUARD",
    "BRAIN_RECALL_BUDGET_TOKENS",
    "BRAIN_RECALL_PASSAGE_TOKENS",
    "BRAIN_RECALL_MAX_CANDIDATES",
    "BRAIN_AGENT_ID",
    "BRAIN_BACKUP_DIR",
)


@pytest.fixture()
def isolated_dotenv(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    """Block all .env file sources so only os.environ reaches Config.load().

    Returns the fake ``$BRAIN_HOME`` root the patched ``_brain_home_root``
    resolves to, so the ``backup_dir`` default can be asserted against it
    without touching the developer's real home directory.
    """
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(
        config_module, "_project_dotenv", lambda: tmp_path / "project.env"
    )
    monkeypatch.setattr(
        config_module, "_brain_home_dotenv", lambda: tmp_path / "brain_home.env"
    )
    brain_home_root = tmp_path / "brain_home_root"
    monkeypatch.setattr(
        config_module, "_brain_home_root", lambda _config_file=None: brain_home_root
    )
    monkeypatch.delenv("BRAIN_HOME", raising=False)
    monkeypatch.setenv("DATABASE_URL", _TEST_DATABASE_URL)
    for key in _NEW_ENV_VARS:
        monkeypatch.delenv(key, raising=False)
    return brain_home_root


# --------------------------------------------------------------------------
# Defaults -- every field resolves with no env set.
# --------------------------------------------------------------------------


def test_new_fields_default_when_unset(isolated_dotenv: Path) -> None:
    cfg = Config.load()

    assert cfg.secret_guard == DEFAULT_SECRET_GUARD == "warn"
    assert cfg.recall_budget_tokens == DEFAULT_RECALL_BUDGET_TOKENS == 2000
    assert cfg.recall_passage_tokens == DEFAULT_RECALL_PASSAGE_TOKENS == 120
    assert cfg.recall_max_candidates == DEFAULT_RECALL_MAX_CANDIDATES == 25
    assert cfg.agent_id is None
    assert cfg.backup_dir == isolated_dotenv / DEFAULT_BACKUP_DIR_NAME


def test_backup_dir_default_is_under_brain_home(isolated_dotenv: Path) -> None:
    # The default is derived from $BRAIN_HOME, not hardcoded, so a relocated
    # brain home relocates its backups with it.
    cfg = Config.load()

    assert cfg.backup_dir.parent == isolated_dotenv
    assert cfg.backup_dir.name == "backups"


# --------------------------------------------------------------------------
# secret_guard -- the "must be one of" enum idiom.
# --------------------------------------------------------------------------


@pytest.mark.parametrize("value", ["warn", "redact", "reject", "off"])
def test_secret_guard_accepts_every_documented_mode(
    monkeypatch: pytest.MonkeyPatch, isolated_dotenv: Path, value: str
) -> None:
    monkeypatch.setenv("BRAIN_SECRET_GUARD", value)

    cfg = Config.load()

    assert cfg.secret_guard == value


def test_secret_guard_is_case_insensitive_and_trimmed(
    monkeypatch: pytest.MonkeyPatch, isolated_dotenv: Path
) -> None:
    monkeypatch.setenv("BRAIN_SECRET_GUARD", "  REJECT  ")

    cfg = Config.load()

    assert cfg.secret_guard == "reject"


def test_secret_guard_blank_uses_default(
    monkeypatch: pytest.MonkeyPatch, isolated_dotenv: Path
) -> None:
    monkeypatch.setenv("BRAIN_SECRET_GUARD", "   ")

    cfg = Config.load()

    assert cfg.secret_guard == DEFAULT_SECRET_GUARD


@pytest.mark.parametrize("bad", ["strict", "yes", "1", "warn,redact"])
def test_secret_guard_invalid_raises_must_be_one_of(
    monkeypatch: pytest.MonkeyPatch, isolated_dotenv: Path, bad: str
) -> None:
    monkeypatch.setenv("BRAIN_SECRET_GUARD", bad)

    with pytest.raises(ConfigError, match="BRAIN_SECRET_GUARD must be one of"):
        Config.load()


# --------------------------------------------------------------------------
# recall_* -- the _parse_positive_int_env family.
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("env_var", "attr", "value"),
    [
        ("BRAIN_RECALL_BUDGET_TOKENS", "recall_budget_tokens", "4096"),
        ("BRAIN_RECALL_PASSAGE_TOKENS", "recall_passage_tokens", "80"),
        ("BRAIN_RECALL_MAX_CANDIDATES", "recall_max_candidates", "10"),
    ],
)
def test_recall_int_knobs_read_from_env(
    monkeypatch: pytest.MonkeyPatch,
    isolated_dotenv: Path,
    env_var: str,
    attr: str,
    value: str,
) -> None:
    monkeypatch.setenv(env_var, value)

    cfg = Config.load()

    assert getattr(cfg, attr) == int(value)


@pytest.mark.parametrize(
    "env_var",
    [
        "BRAIN_RECALL_BUDGET_TOKENS",
        "BRAIN_RECALL_PASSAGE_TOKENS",
        "BRAIN_RECALL_MAX_CANDIDATES",
    ],
)
@pytest.mark.parametrize("bad", ["0", "-1", "notanint", "12.5"])
def test_recall_int_knobs_reject_non_positive(
    monkeypatch: pytest.MonkeyPatch, isolated_dotenv: Path, env_var: str, bad: str
) -> None:
    monkeypatch.setenv(env_var, bad)

    with pytest.raises(ConfigError, match=f"{env_var} must be an integer >= 1"):
        Config.load()


# --------------------------------------------------------------------------
# agent_id -- regex validated without importing brain.agent (Wave 4).
# --------------------------------------------------------------------------


def test_agent_id_blank_is_none(
    monkeypatch: pytest.MonkeyPatch, isolated_dotenv: Path
) -> None:
    monkeypatch.setenv("BRAIN_AGENT_ID", "   ")

    cfg = Config.load()

    assert cfg.agent_id is None


@pytest.mark.parametrize(
    "value",
    [
        "claude",
        "claude-code",
        "agent.1",
        "agent-id:sub",
        "A",
        "0",
        "a" * 64,  # 1 leading char + 63 more == the documented maximum.
    ],
)
def test_agent_id_accepts_valid_ids(
    monkeypatch: pytest.MonkeyPatch, isolated_dotenv: Path, value: str
) -> None:
    monkeypatch.setenv("BRAIN_AGENT_ID", value)

    cfg = Config.load()

    assert cfg.agent_id == value


def test_agent_id_is_trimmed(
    monkeypatch: pytest.MonkeyPatch, isolated_dotenv: Path
) -> None:
    monkeypatch.setenv("BRAIN_AGENT_ID", "  claude-code  ")

    cfg = Config.load()

    assert cfg.agent_id == "claude-code"


@pytest.mark.parametrize(
    "bad",
    [
        "-leading-hyphen",
        ".leading-dot",
        ":leading-colon",
        "_leading-underscore",
        "has space",
        "has/slash",
        "emoji-\N{ROCKET}",
        "a" * 65,  # one char past the 64-char maximum.
    ],
)
def test_agent_id_invalid_raises(
    monkeypatch: pytest.MonkeyPatch, isolated_dotenv: Path, bad: str
) -> None:
    monkeypatch.setenv("BRAIN_AGENT_ID", bad)

    with pytest.raises(ConfigError, match="BRAIN_AGENT_ID must match"):
        Config.load()


def test_agent_id_pattern_is_the_documented_regex() -> None:
    # Task 4A's brain.agent.normalize_agent_id must use this exact pattern; the
    # parity test there compares against this public constant. Pinning the
    # literal here makes any silent edit to it a visible test failure.
    assert AGENT_ID_PATTERN == r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,63}$"


def test_config_does_not_import_brain_agent() -> None:
    # The regex must be validated WITHOUT importing brain.agent, which does not
    # exist until Wave 4. A stray import would make config.py unimportable.
    source = Path(config_module.__file__).read_text(encoding="utf-8")

    assert "from brain.agent" not in source
    assert "from .agent import" not in source
    assert "import brain.agent" not in source


# --------------------------------------------------------------------------
# backup_dir -- Path(...).expanduser().
# --------------------------------------------------------------------------


def test_backup_dir_read_from_env(
    monkeypatch: pytest.MonkeyPatch, isolated_dotenv: Path, tmp_path: Path
) -> None:
    target = tmp_path / "elsewhere" / "archives"
    monkeypatch.setenv("BRAIN_BACKUP_DIR", str(target))

    cfg = Config.load()

    assert cfg.backup_dir == target


def test_backup_dir_expands_user(
    monkeypatch: pytest.MonkeyPatch, isolated_dotenv: Path
) -> None:
    monkeypatch.setenv("BRAIN_BACKUP_DIR", "~/brain-archives")

    cfg = Config.load()

    assert cfg.backup_dir == Path.home() / "brain-archives"
    assert "~" not in str(cfg.backup_dir)


def test_backup_dir_blank_falls_back_to_brain_home(
    monkeypatch: pytest.MonkeyPatch, isolated_dotenv: Path
) -> None:
    monkeypatch.setenv("BRAIN_BACKUP_DIR", "   ")

    cfg = Config.load()

    assert cfg.backup_dir == isolated_dotenv / DEFAULT_BACKUP_DIR_NAME


def test_backup_dir_is_not_created_at_load_time(
    monkeypatch: pytest.MonkeyPatch, isolated_dotenv: Path, tmp_path: Path
) -> None:
    # Config.load() is a pure parse -- Task 1A creates the directory when a
    # backup actually runs, so merely loading config must not touch the disk.
    target = tmp_path / "not-yet-created"
    monkeypatch.setenv("BRAIN_BACKUP_DIR", str(target))

    cfg = Config.load()

    assert cfg.backup_dir == target
    assert not target.exists()


# --------------------------------------------------------------------------
# Direct construction -- a Config built bypassing load() still has the fields.
# --------------------------------------------------------------------------


def test_fields_are_present_on_directly_constructed_config(
    isolated_dotenv: Path,
) -> None:
    # Many test fixtures build Config(...) directly rather than via load();
    # every new field must carry a usable default there too.
    cfg = Config(database_url=_TEST_DATABASE_URL)

    assert cfg.secret_guard == DEFAULT_SECRET_GUARD
    assert cfg.recall_budget_tokens == DEFAULT_RECALL_BUDGET_TOKENS
    assert cfg.recall_passage_tokens == DEFAULT_RECALL_PASSAGE_TOKENS
    assert cfg.recall_max_candidates == DEFAULT_RECALL_MAX_CANDIDATES
    assert cfg.agent_id is None
    assert cfg.backup_dir == isolated_dotenv / DEFAULT_BACKUP_DIR_NAME
