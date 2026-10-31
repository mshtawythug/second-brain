"""A synthetic git repository for the RM-43 code-ingest suite, built in ``tmp_path``.

Every name, address and file body here is synthetic. The repository is made with
the real ``git`` under a hermetic environment (no global or system config, a
``HOME`` inside ``tmp_path``, fixed dates), so no test reads the owner's git
setup or touches any real repository.
"""
from __future__ import annotations

import os
import shutil
import subprocess
from collections.abc import Mapping
from pathlib import Path

from tests.secret_fixtures import SYNTHETIC_AWS_KEY

OWNER_EMAIL = "owner@example.test"
OWNER_NAME = "Owner Sample"
OTHER_EMAIL = "other@example.test"
OTHER_NAME = "Other Sample"
#: Contains the owner's address, so a substring match would wrongly include it.
LOOKALIKE_EMAIL = "coowner@example.test"

#: ``--since`` for the suite: commits dated before it are out of the window.
SINCE = "2026-06-01"
OLD_DATE = "2026-01-15T12:00:00+00:00"
NEW_DATE = "2026-07-15T12:00:00+00:00"

PY_MODULE = (
    '"""A synthetic module."""\n'
    "import os\n"
    "\n"
    "\n"
    "def widget_path():\n"
    "    return os.sep\n"
    "\n"
    "\n"
    "class Gadget:\n"
    "    def spin(self):\n"
    "        return 1\n"
)
TS_FILE = "export function frobnicate(x: number): number {\n  return x * 2;\n}\n"
README = "# Sample project\n\nA synthetic repository.\n"
OTHER_FILE = "def theirs():\n    return 'not the owner'\n"
OLD_FILE = "def ancient():\n    return 0\n"
SECRET_FILE = f'AWS_KEY = "{SYNTHETIC_AWS_KEY}"\n'

#: Files the owner committed inside the window that must be ingested.
OWNER_KEPT = {
    "src/widget.py": PY_MODULE,
    "web/app.ts": TS_FILE,
    "README.md": README,
}
#: Files the owner committed inside the window that every exclusion must drop,
#: with the reason the report counts.
OWNER_EXCLUDED_BY_PATH = {
    "node_modules/pkg/index.js": "module.exports = 1;\n",
    "vendor/lib/util.go": "package lib\n",
    "build/out.py": "x = 1\n",
    "package-lock.json": '{"lockfileVersion": 3}\n',
    "web/app.min.js": "var a=1;\n",
}
LONG_LINE = "var a=" + "1+" * 800 + "1;"
OWNER_EXCLUDED_BY_CONTENT: dict[str, bytes] = {
    "web/bundle.js": (LONG_LINE + "\n" + LONG_LINE + "\n").encode(),
    "assets/blob.bin": b"\x89PNG\x00\x01\x02binary",
    "docs/latin1.txt": "caf\xe9\n".encode("latin-1"),
    "docs/empty.txt": b"   \n\n",
    "data/huge.py": b"x = 1\n" * (210 * 1024 // 6),
}
IGNORED_FORCED = "secrets.local.py"
IGNORED_UNTRACKED = "scratch.log"
SYMLINK = "src/link.py"
SECRET_PATH = "src/settings.py"
OTHER_PATH = "src/theirs.py"
OLD_PATH = "src/ancient.py"


def git_bin() -> str:
    binary = shutil.which("git")
    assert binary is not None, "the RM-43 suite needs git on PATH"
    return binary


def hermetic_env(home: Path) -> dict[str, str]:
    """The environment every fixture ``git`` call runs under."""
    return {
        "PATH": os.defpath,
        "HOME": str(home),
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_CONFIG_GLOBAL": os.devnull,
        "LC_ALL": "C",
    }


def git(repo: Path, *args: str, home: Path, author: tuple[str, str] | None = None,
        date: str = NEW_DATE) -> None:
    """Run one fixture ``git`` command in ``repo``; fail loudly on a non-zero exit."""
    env = hermetic_env(home)
    if author is not None:
        name, email = author
        env.update(
            GIT_AUTHOR_NAME=name, GIT_AUTHOR_EMAIL=email, GIT_AUTHOR_DATE=date,
            GIT_COMMITTER_NAME=name, GIT_COMMITTER_EMAIL=email, GIT_COMMITTER_DATE=date,
        )
    subprocess.run(
        [git_bin(), *args], cwd=repo, env=env, check=True, capture_output=True, timeout=30
    )


def write(repo: Path, rel: str, body: str | bytes) -> None:
    path = repo / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    if isinstance(body, bytes):
        path.write_bytes(body)
    else:
        path.write_text(body, encoding="utf-8")


def commit(repo: Path, files: Mapping[str, str | bytes], *, home: Path,
           author: tuple[str, str], date: str = NEW_DATE, force: bool = False) -> None:
    for rel, body in files.items():
        write(repo, rel, body)
    git(repo, "add", *(["-f"] if force else []), *files, home=home)
    git(repo, "commit", "-q", "-m", "synthetic change", home=home, author=author, date=date)


def owner() -> tuple[str, str]:
    return (OWNER_NAME, OWNER_EMAIL)


def build_repo(root: Path, *, name: str = "sample-repo") -> Path:
    """The standard repository: every case the acceptance criteria name."""
    home = root / "home"
    home.mkdir(parents=True, exist_ok=True)
    repo = root / name
    repo.mkdir(parents=True)
    git(repo, "init", "-q", home=home)
    commit(repo, {".gitignore": f"*.log\n{IGNORED_FORCED}\n"}, home=home, author=owner(),
           date=OLD_DATE)
    commit(repo, {OLD_PATH: OLD_FILE}, home=home, author=owner(), date=OLD_DATE)
    commit(repo, OWNER_KEPT, home=home, author=owner())
    commit(repo, {**OWNER_EXCLUDED_BY_PATH, **OWNER_EXCLUDED_BY_CONTENT}, home=home,
           author=owner())
    commit(repo, {SECRET_PATH: SECRET_FILE}, home=home, author=owner())
    commit(repo, {OTHER_PATH: OTHER_FILE}, home=home, author=(OTHER_NAME, OTHER_EMAIL))
    commit(repo, {"src/lookalike.py": "y = 2\n"}, home=home,
           author=("Look Alike", LOOKALIKE_EMAIL))
    commit(repo, {IGNORED_FORCED: "z = 3\n"}, home=home, author=owner(), force=True)
    (repo / SYMLINK).symlink_to("widget.py")
    git(repo, "add", SYMLINK, home=home)
    git(repo, "commit", "-q", "-m", "link", home=home, author=owner())
    write(repo, IGNORED_UNTRACKED, "untracked and ignored\n")
    return repo


def home_of(root: Path) -> Path:
    return root / "home"
