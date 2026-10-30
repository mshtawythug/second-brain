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
    CAPTURE_EXTENSIONS,
    CONFIG_NAME,
    DEFAULT_EXTENSIONS,
    DEFAULT_KIND,
    DEFAULT_SENSITIVITY,
    KIND_EXTENSIONS,
    VOICE_EXTENSIONS,
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
    assert load_drop_folders(home).folders == ()


def test_an_empty_file_means_no_folders(home: Path) -> None:
    _write(home, "")
    assert load_drop_folders(home).folders == ()


def test_one_folder_with_defaults(home: Path, drop: Path) -> None:
    _write(home, f'[[folder]]\npath = "{drop}"\n')

    assert load_drop_folders(home).folders == (
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

    (folder,) = load_drop_folders(home).folders

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

    assert [f.path for f in load_drop_folders(home).folders] == [b.resolve(), a.resolve()]


def test_a_tilde_path_is_expanded(
    home: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    (tmp_path / "Shots").mkdir()
    _write(home, '[[folder]]\npath = "~/Shots"\n')

    assert load_drop_folders(home).folders[0].path == (tmp_path / "Shots").resolve()


def test_a_symlinked_folder_path_is_stored_resolved(home: Path, tmp_path: Path) -> None:
    real = tmp_path / "real"
    real.mkdir()
    (tmp_path / "alias").symlink_to(real)
    _write(home, f'[[folder]]\npath = "{tmp_path / "alias"}"\n')

    assert load_drop_folders(home).folders[0].path == real.resolve()


def test_extensions_are_lowercased(home: Path, drop: Path) -> None:
    _write(home, f'[[folder]]\npath = "{drop}"\nextensions = [".PNG"]\n')

    assert load_drop_folders(home).folders[0].extensions == frozenset({".png"})


@pytest.mark.parametrize(
    ("body", "where"),
    [
        ('mode = "capture"\nkind = "manual"', "folder[0].kind"),
        ('mode = "capture"\nextensions = [".png"]', "folder[0].extensions"),
        ('mode = "capture"\nextensions = []', "folder[0].extensions"),
        ('mode = "capture"\nextensions = ".md"', "folder[0].extensions"),
        (f'mode = "capture"\nkind = "{SECRET_LOOKING}"', "folder[0].kind"),
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


def test_a_capture_folder_takes_text_files_and_defaults_to_normal(
    home: Path, drop: Path
) -> None:
    """RM-39: a capture folder has no kind, takes .md/.txt, and is ``normal`` by default
    (a capture note's default; a share can still escalate itself to confidential)."""
    _write(home, f'[[folder]]\npath = "{drop}"\nmode = "capture"\n')

    (folder,) = load_drop_folders(home).folders

    assert folder == DropFolder(
        path=drop.resolve(),
        mode="capture",
        kind=None,
        extensions=CAPTURE_EXTENSIONS,
        sensitivity="normal",
        auto=True,
    )
    assert frozenset({".md", ".txt"}) == CAPTURE_EXTENSIONS


def test_a_capture_folder_keeps_its_listed_extensions_and_sensitivity(
    home: Path, drop: Path
) -> None:
    _write(
        home,
        f'[[folder]]\npath = "{drop}"\nmode = "capture"\nextensions = [".MD"]\n'
        'sensitivity = "confidential"\nauto = false\n',
    )

    (folder,) = load_drop_folders(home).folders

    assert (folder.extensions, folder.sensitivity, folder.auto) == (
        frozenset({".md"}),
        "confidential",
        False,
    )


def test_the_capture_extensions_are_served_by_an_extractor() -> None:
    from brain.ingest.dispatch import supported_extensions

    assert set(supported_extensions()) >= CAPTURE_EXTENSIONS


@pytest.mark.parametrize(
    "path_line",
    [
        "",  # missing
        "path = 3",
        'path = "relative/dir"',
    ],
)
def test_a_bad_path_names_the_key(home: Path, path_line: str) -> None:
    _write(home, f"[[folder]]\n{path_line}\n")

    with pytest.raises(ConfigError) as err:
        load_drop_folders(home)

    assert "folder[0].path" in str(err.value)
    assert SECRET_LOOKING not in str(err.value)


def test_a_relative_path_is_refused_even_when_it_exists_under_the_cwd(
    home: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (tmp_path / "relative" / "dir").mkdir(parents=True)
    monkeypatch.chdir(tmp_path)
    _write(home, '[[folder]]\npath = "relative/dir"\n')

    with pytest.raises(ConfigError, match=r"folder\[0\]\.path must be absolute"):
        load_drop_folders(home)


def test_a_missing_folder_is_skipped_by_index_not_refused(home: Path, tmp_path: Path) -> None:
    """rev-105 F6: an unplugged folder must not stop the others."""
    plain = tmp_path / "plain.txt"
    plain.write_text("x", encoding="utf-8")
    here = tmp_path / "here"
    here.mkdir()
    _write(
        home,
        f'[[folder]]\npath = "/{SECRET_LOOKING}/nowhere"\n'
        f'[[folder]]\npath = "{here}"\n'
        f'[[folder]]\npath = "{plain}"\n',
    )

    config = load_drop_folders(home)

    assert [f.path for f in config.folders] == [here.resolve()]
    assert config.missing == (0, 2)


def test_a_missing_folder_still_has_its_other_keys_checked(home: Path) -> None:
    _write(home, '[[folder]]\npath = "/nowhere-synthetic"\nkind = "nope"\n')

    with pytest.raises(ConfigError, match=r"folder\[0\]\.kind"):
        load_drop_folders(home)


def test_two_missing_entries_for_one_path_are_not_a_duplicate(home: Path) -> None:
    _write(home, '[[folder]]\npath = "/nowhere-synthetic"\n' * 2)

    assert load_drop_folders(home).missing == (0, 1)


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


# --- RM-38.D2: a folder's default extensions follow its kind -------------------------


def test_a_voice_folder_without_extensions_takes_audio(home: Path, drop: Path) -> None:
    _write(home, f'[[folder]]\npath = "{drop}"\nkind = "voice"\n')

    (folder,) = load_drop_folders(home).folders

    assert folder.kind == "voice"
    assert folder.extensions == VOICE_EXTENSIONS == frozenset({".m4a", ".mp3", ".wav"})
    assert folder.sensitivity == "confidential"


def test_a_screenshot_folder_without_extensions_takes_images(home: Path, drop: Path) -> None:
    _write(home, f'[[folder]]\npath = "{drop}"\nkind = "screenshot"\n')

    assert load_drop_folders(home).folders[0].extensions == DEFAULT_EXTENSIONS


def test_any_other_kind_without_extensions_takes_images(home: Path, drop: Path) -> None:
    _write(home, f'[[folder]]\npath = "{drop}"\nkind = "manual"\n')

    assert load_drop_folders(home).folders[0].extensions == DEFAULT_EXTENSIONS


def test_a_voice_folder_keeps_the_extensions_it_lists(home: Path, drop: Path) -> None:
    _write(home, f'[[folder]]\npath = "{drop}"\nkind = "voice"\nextensions = [".m4a"]\n')

    assert load_drop_folders(home).folders[0].extensions == frozenset({".m4a"})


def test_a_voice_folder_can_override_its_sensitivity(home: Path, drop: Path) -> None:
    _write(home, f'[[folder]]\npath = "{drop}"\nkind = "voice"\nsensitivity = "normal"\n')

    assert load_drop_folders(home).folders[0].sensitivity == "normal"


def test_the_voice_defaults_are_what_the_voice_extractor_serves() -> None:
    from brain.ingest import dispatch

    for ext in VOICE_EXTENSIONS:
        spec = dispatch.spec_for(Path(f"memo{ext}"))
        assert spec is not None and spec.name == "voice"
    assert KIND_EXTENSIONS == {"voice": VOICE_EXTENSIONS}
