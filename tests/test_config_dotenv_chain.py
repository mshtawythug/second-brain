"""Regression tests for dotenv-chain introspection + the missing-DATABASE_URL message.

Bug (2026-08-07): `$BRAIN_HOME/.env` was never created by any command, so a
`brain` invoked from outside the source checkout found no config at all and
died with the bare message ``DATABASE_URL is not set (see .env.example)``.
That message named the wrong cause — the database was healthy the whole time —
and sent 12 days of debugging at Postgres.

These tests pin the two halves of the fix that live in ``brain.config`` /
``brain.errors``:

* :func:`brain.config.dotenv_chain` — the shared contract ``brain doctor``
  consumes: every candidate path, in precedence order, with its resolution
  state, callable even when config loading FAILS.
* The rendered error — it must name every searched path and its state, and it
  must distinguish "no config file anywhere" from "config loaded, key absent".

Every test uses ``tmp_path`` + ``monkeypatch``; none touches the real
``~/.brain/`` or the repo ``.env``.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from brain import config as config_module
from brain.config import Config, ConfigError, DotenvSource, dotenv_chain


@pytest.fixture
def chain_paths(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> dict[str, Path]:
    """Point all three chain links at isolated tmp paths.

    Returns the three candidate paths keyed ``project`` / ``cwd`` /
    ``brain_home``. None of them exists yet — a test writes the ones it wants
    to exercise.
    """
    cwd = tmp_path / "cwd"
    cwd.mkdir()
    monkeypatch.chdir(cwd)
    project = tmp_path / "repo" / ".env"
    project.parent.mkdir()
    brain_home = tmp_path / "brain_home" / ".env"
    brain_home.parent.mkdir()
    monkeypatch.setattr(config_module, "_project_dotenv", lambda: project)
    monkeypatch.setattr(config_module, "_brain_home_dotenv", lambda: brain_home)
    monkeypatch.setattr(
        config_module,
        "_brain_home_root",
        lambda _config_file=None: brain_home.parent,
    )
    monkeypatch.delenv("DATABASE_URL", raising=False)
    monkeypatch.delenv("BRAIN_HOME", raising=False)
    return {"project": project, "cwd": cwd / ".env", "brain_home": brain_home}


# ---------------------------------------------------------------------------
# dotenv_chain() — the shared contract
# ---------------------------------------------------------------------------


def test_dotenv_chain_orders_highest_precedence_first(chain_paths: dict[str, Path]):
    """Chain order mirrors the loader: repo .env > cwd .env > $BRAIN_HOME/.env."""
    chain = dotenv_chain()

    assert [source.path for source in chain] == [
        chain_paths["project"],
        chain_paths["cwd"],
        chain_paths["brain_home"],
    ]


def test_dotenv_chain_reports_exists_and_loaded_flags(chain_paths: dict[str, Path]):
    """A written candidate is exists+loaded; an absent one is neither."""
    chain_paths["brain_home"].write_text("DATABASE_URL=postgresql://x:y@h:5432/d\n")

    by_path = {source.path: source for source in dotenv_chain()}

    assert by_path[chain_paths["brain_home"]] == DotenvSource(
        path=chain_paths["brain_home"], exists=True, loaded=True
    )
    assert by_path[chain_paths["project"]] == DotenvSource(
        path=chain_paths["project"], exists=False, loaded=False
    )


def test_dotenv_chain_dangling_symlink_is_distinguishable_from_missing(
    chain_paths: dict[str, Path],
):
    """A broken $BRAIN_HOME/.env link must not look identical to 'never created'.

    ``exists`` follows the link so both report False; ``is_symlink()`` on the
    path is what tells the two apart, and `brain doctor` relies on exactly that.
    """
    chain_paths["brain_home"].symlink_to(chain_paths["project"])  # target absent

    by_path = {source.path: source for source in dotenv_chain()}
    dangling = by_path[chain_paths["brain_home"]]
    missing = by_path[chain_paths["cwd"]]

    assert dangling.exists is False and dangling.loaded is False
    assert dangling.path.is_symlink() is True
    assert missing.path.is_symlink() is False


def test_dotenv_chain_collapses_duplicate_candidates(
    monkeypatch: pytest.MonkeyPatch, chain_paths: dict[str, Path]
):
    """A dev checkout where $BRAIN_HOME IS the repo lists that file once."""
    monkeypatch.setattr(
        config_module, "_brain_home_dotenv", lambda: chain_paths["project"]
    )

    paths = [source.path for source in dotenv_chain()]

    assert paths.count(chain_paths["project"]) == 1


def test_cwd_walkup_is_dropped_when_ignore_flag_set(
    monkeypatch: pytest.MonkeyPatch, chain_paths: dict[str, Path]
):
    """BRAIN_IGNORE_CWD_DOTENV removes link 3 from the chain entirely.

    The walk-up climbs to the filesystem root, so a non-interactive process
    started in an arbitrary directory can silently pick up a stranger's
    ``.env`` and talk to the wrong database. Long-running contexts (the launchd
    daemons) opt out; a dropped link must NOT appear in the chain, because
    `brain doctor` renders straight from it.
    """
    monkeypatch.setenv("BRAIN_IGNORE_CWD_DOTENV", "1")

    paths = [source.path for source in dotenv_chain()]

    assert paths == [chain_paths["project"], chain_paths["brain_home"]]


def test_ignored_cwd_dotenv_is_not_loaded(
    monkeypatch: pytest.MonkeyPatch, chain_paths: dict[str, Path]
):
    """The opt-out is real: an ambient cwd .env cannot supply DATABASE_URL."""
    chain_paths["cwd"].write_text("DATABASE_URL=postgresql://stranger:5432/db\n")
    monkeypatch.setenv("BRAIN_IGNORE_CWD_DOTENV", "1")

    with pytest.raises(ConfigError) as exc_info:
        Config.load()

    assert str(chain_paths["cwd"]) not in str(exc_info.value)


def test_cwd_walkup_kept_by_default_and_on_falsy_flag(
    monkeypatch: pytest.MonkeyPatch, chain_paths: dict[str, Path]
):
    """Default and explicit-falsy both keep the walk-up — no silent removal."""
    chain_paths["cwd"].write_text("DATABASE_URL=postgresql://from-cwd:5432/db\n")

    assert Config.load().database_url == "postgresql://from-cwd:5432/db"

    monkeypatch.setenv("BRAIN_IGNORE_CWD_DOTENV", "0")
    monkeypatch.delenv("DATABASE_URL", raising=False)

    assert Config.load().database_url == "postgresql://from-cwd:5432/db"


def test_dotenv_chain_callable_when_config_load_fails(chain_paths: dict[str, Path]):
    """doctor must be able to introspect a BROKEN install — the whole point."""
    with pytest.raises(ConfigError):
        Config.load()

    assert len(dotenv_chain()) == 3


def test_dotenv_chain_flags_present_but_unreadable_source(
    chain_paths: dict[str, Path],
):
    """exists=True, loaded=False is a distinct fault from 'missing'.

    python-dotenv swallows an unreadable path and hands back an empty mapping,
    which would report a directory / permission-denied ``.env`` as cleanly
    "loaded" — the chain must not inherit that lie.
    """
    chain_paths["brain_home"].mkdir()  # a directory where a file was expected

    by_path = {source.path: source for source in dotenv_chain()}

    assert by_path[chain_paths["brain_home"]].exists is True
    assert by_path[chain_paths["brain_home"]].loaded is False


def test_unreadable_dotenv_is_not_reported_as_missing(chain_paths: dict[str, Path]):
    """A present-but-unparseable .env gets the 'no usable file' wording."""
    chain_paths["brain_home"].mkdir()

    with pytest.raises(ConfigError) as exc_info:
        Config.load()

    message = str(exc_info.value)
    assert "No usable .env file was found" in message
    assert "found, NOT readable" in message


# ---------------------------------------------------------------------------
# The error message
# ---------------------------------------------------------------------------


def test_missing_database_url_names_every_searched_path(chain_paths: dict[str, Path]):
    """REGRESSION: the error must name all three resolved paths and their state."""
    with pytest.raises(ConfigError) as exc_info:
        Config.load()

    message = str(exc_info.value)
    for path in chain_paths.values():
        assert str(path) in message, f"{path} missing from:\n{message}"
    assert message.count("(missing)") == 3
    assert "No .env file was found" in message
    assert f"brain setup` to create {chain_paths['brain_home']}" in message


def test_missing_database_url_reports_dangling_link_not_missing(
    chain_paths: dict[str, Path],
):
    """A broken link is reported as broken, never as 'missing'."""
    chain_paths["brain_home"].symlink_to(chain_paths["project"])

    with pytest.raises(ConfigError) as exc_info:
        Config.load()

    message = str(exc_info.value)
    assert "BROKEN symlink" in message
    assert str(chain_paths["project"]) in message
    assert message.count("(missing)") == 2  # project + cwd, NOT the broken link
    # A path that IS there but unusable is never described as "not found" —
    # the remedy is to repair it, not to create a second one.
    assert "No .env file was found in any of" not in message
    assert "No usable .env file was found" in message
    assert "symlink whose target no longer exists" in message
    # `brain setup` never clobbers an existing path, so it is a NO-OP here and
    # would reproduce the identical error. The remedy must remove the link
    # first, worded identically to `brain doctor`'s remedy for this state.
    assert f"Fix: rm {chain_paths['brain_home']} && brain setup" in message


def test_found_but_incomplete_dotenv_reports_loaded_not_missing(
    chain_paths: dict[str, Path],
):
    """REGRESSION: a loaded .env without DATABASE_URL is a DIFFERENT fault.

    It must not read as "no config found" — that sends the user off to create a
    file that already exists instead of editing the one that does.
    """
    chain_paths["brain_home"].write_text("BRAIN_EMBEDDER=none\n")

    with pytest.raises(ConfigError) as exc_info:
        Config.load()

    message = str(exc_info.value)
    assert "No .env file was found" not in message
    assert "found, loaded" in message
    assert f"Config WAS found and loaded from: {chain_paths['brain_home']}" in message
    assert "does not define DATABASE_URL" in message


def test_brain_home_dotenv_alone_satisfies_config_load(chain_paths: dict[str, Path]):
    """The canonical location works on its own — no checkout, no cwd .env."""
    chain_paths["brain_home"].write_text(
        "DATABASE_URL=postgresql://u:p@localhost:55432/db\n"
    )

    cfg = Config.load()

    assert cfg.database_url == "postgresql://u:p@localhost:55432/db"
