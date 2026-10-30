"""Tests for ``$BRAIN_HOME/drop-folders.toml``, :mod:`brain.settings.drop_folders` (RM-33).

Every file is written into ``tmp_path``; the real ``$BRAIN_HOME`` is never read.
Error messages name the entry and the key (``folder[1].kind``), never the value.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from brain.config import ConfigError
from brain.drop_folder import DropFolder
from brain.settings.drop_folders import (
    CONFIG_NAME,
    DEFAULT_EXTENSIONS,
    DEFAULT_KIND,
    DEFAULT_SENSITIVITY,
    config_path,
    load_drop_folders,
)

SECRET_LOOKING = "zz-value-that-must-not-be-echoed"


def _write(home: Path, text: str) -> Path:
    path = home / CONFIG_NAME
    path.write_text(text, encoding="utf-8")
    return path


@pytest.fixture
def home(tmp_path: Path) -> Path:
    brain_home = tmp_path / "brain-home"
    brain_home.mkdir()
    return brain_home


@pytest.fixture
def drop(tmp_path: Path) -> Path:
    folder = tmp_path / "drop"
    folder.mkdir()
    return folder


def test_config_path_is_under_brain_home(home: Path) -> None:
    assert config_path(home) == home / "drop-folders.toml"


def test_a_missing_file_means_no_folders(home: Path) -> None:
    assert load_drop_folders(home) == ()


def test_an_empty_file_means_no_folders(home: Path) -> None:
    _write(home, "")
    assert load_drop_folders(home) == ()


def test_one_folder_with_defaults(home: Path, drop: Path) -> None:
    _write(home, f'[[folder]]\npath = "{drop}"\n')

    assert load_drop_folders(home) == (
        DropFolder(
            path=drop.resolve(),
            mode="ingest",
            kind="screenshot",
            extensions=frozenset({".png", ".jpg", ".jpeg"}),
            sensitivity="confidential",
            auto=True,
        ),
    )
    assert DEFAULT_KIND == "screenshot"
    assert DEFAULT_SENSITIVITY == "confidential"
    assert frozenset({".png", ".jpg", ".jpeg"}) == DEFAULT_EXTENSIONS


def test_every_key_set(home: Path, tmp_path: Path) -> None:
    inbox = tmp_path / "inbox"
    inbox.mkdir()
    _write(
        home,
        f'[[folder]]\npath = "{inbox}"\nmode = "ingest"\nkind = "manual"\n'
        'extensions = [".pdf", ".md"]\nsensitivity = "normal"\nauto = false\n',
    )

    (folder,) = load_drop_folders(home)

    assert folder == DropFolder(
        path=inbox.resolve(),
        mode="ingest",
        kind="manual",
        extensions=frozenset({".pdf", ".md"}),
        sensitivity="normal",
        auto=False,
    )


def test_two_folders_keep_their_order(home: Path, tmp_path: Path) -> None:
    a, b = tmp_path / "a", tmp_path / "b"
    a.mkdir()
    b.mkdir()
    _write(home, f'[[folder]]\npath = "{b}"\n[[folder]]\npath = "{a}"\n')

    assert [f.path for f in load_drop_folders(home)] == [b.resolve(), a.resolve()]


def test_a_tilde_path_is_expanded(
    home: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    (tmp_path / "Shots").mkdir()
    _write(home, '[[folder]]\npath = "~/Shots"\n')

    assert load_drop_folders(home)[0].path == (tmp_path / "Shots").resolve()


def test_a_symlinked_folder_path_is_stored_resolved(home: Path, tmp_path: Path) -> None:
    real = tmp_path / "real"
    real.mkdir()
    (tmp_path / "alias").symlink_to(real)
    _write(home, f'[[folder]]\npath = "{tmp_path / "alias"}"\n')

    assert load_drop_folders(home)[0].path == real.resolve()


def test_extensions_are_lowercased(home: Path, drop: Path) -> None:
    _write(home, f'[[folder]]\npath = "{drop}"\nextensions = [".PNG"]\n')

    assert load_drop_folders(home)[0].extensions == frozenset({".png"})


@pytest.mark.parametrize(
    ("body", "where"),
    [
        ('mode = "capture"', "folder[0].mode"),
        (f'mode = "{SECRET_LOOKING}"', "folder[0].mode"),
        (f'kind = "{SECRET_LOOKING}"', "folder[0].kind"),
        ("kind = 3", "folder[0].kind"),
        ("kind = []", "folder[0].kind"),
        ("sensitivity = []", "folder[0].sensitivity"),
        ("mode = []", "folder[0].mode"),
        (f'extensions = ["{SECRET_LOOKING}"]', "folder[0].extensions"),
        ('extensions = [".exe"]', "folder[0].extensions"),
        ("extensions = []", "folder[0].extensions"),
        ('extensions = ".png"', "folder[0].extensions"),
        ("extensions = [1]", "folder[0].extensions"),
        (f'sensitivity = "{SECRET_LOOKING}"', "folder[0].sensitivity"),
        ('auto = "yes"', "folder[0].auto"),
        (f'{SECRET_LOOKING.replace("-", "_")} = 1', "folder[0]"),
    ],
)
def test_a_bad_value_names_the_key_and_never_the_value(
    home: Path, drop: Path, body: str, where: str
) -> None:
    _write(home, f'[[folder]]\npath = "{drop}"\n{body}\n')

    with pytest.raises(ConfigError) as err:
        load_drop_folders(home)

    assert where in str(err.value)
    assert SECRET_LOOKING not in str(err.value)
    assert SECRET_LOOKING.replace("-", "_") not in str(err.value)


def test_capture_mode_says_when_it_arrives(home: Path, drop: Path) -> None:
    _write(home, f'[[folder]]\npath = "{drop}"\nmode = "capture"\n')

    with pytest.raises(ConfigError, match="RM-39"):
        load_drop_folders(home)


@pytest.mark.parametrize(
    "path_line",
    [
        "",  # missing
        "path = 3",
        'path = "relative/dir"',
        f'path = "/{SECRET_LOOKING}/nowhere"',
    ],
)
def test_a_bad_path_names_the_key(home: Path, path_line: str) -> None:
    _write(home, f"[[folder]]\n{path_line}\n")

    with pytest.raises(ConfigError) as err:
        load_drop_folders(home)

    assert "folder[0].path" in str(err.value)
    assert SECRET_LOOKING not in str(err.value)


def test_a_file_path_is_not_a_folder(home: Path, tmp_path: Path) -> None:
    plain = tmp_path / "plain.txt"
    plain.write_text("x", encoding="utf-8")
    _write(home, f'[[folder]]\npath = "{plain}"\n')

    with pytest.raises(ConfigError, match=r"folder\[0\]\.path"):
        load_drop_folders(home)


def test_two_entries_for_one_folder_are_refused(home: Path, tmp_path: Path, drop: Path) -> None:
    (tmp_path / "alias").symlink_to(drop)
    _write(home, f'[[folder]]\npath = "{drop}"\n[[folder]]\npath = "{tmp_path / "alias"}"\n')

    with pytest.raises(ConfigError, match=r"folder\[1\]\.path"):
        load_drop_folders(home)


@pytest.mark.parametrize(
    "text",
    [
        f"folder = 1\n{SECRET_LOOKING.replace('-', '_')} = 2\n",
        f"{SECRET_LOOKING.replace('-', '_')} = 2\n",
        "folder = 1\n",
        "folder = [1]\n",
    ],
)
def test_a_bad_top_level_is_refused(home: Path, text: str) -> None:
    _write(home, text)

    with pytest.raises(ConfigError) as err:
        load_drop_folders(home)

    assert SECRET_LOOKING.replace("-", "_") not in str(err.value)


def test_invalid_toml_is_refused_without_quoting_it(home: Path) -> None:
    _write(home, f'[[folder]]\npath = "{SECRET_LOOKING}\n')

    with pytest.raises(ConfigError) as err:
        load_drop_folders(home)

    assert "drop-folders.toml" in str(err.value)
    assert SECRET_LOOKING not in str(err.value)
    assert err.value.__cause__ is None
    assert err.value.__suppress_context__


def test_an_unreadable_file_is_a_config_error(home: Path) -> None:
    (home / CONFIG_NAME).mkdir()  # a directory where the file should be

    with pytest.raises(ConfigError, match="drop-folders.toml"):
        load_drop_folders(home)
