"""Install-time config lookups shared by the installers and the shell tooling.

``brain setup`` and ``brain-install-launchd`` call :func:`resolve_install_vault`
directly; ``brain-monitor`` does too. ``brain uninstall`` calls
:func:`resolve_install_vault_with_source`, the same precedence plus a label
saying which leg won, so it removes the vault the installers set up and can
tell the user where that path came from. The bash shims (``brain-status``,
``_brain-watcher-fg``) reach it through ``python -m brain.install_config
BRAIN_HOME`` (:func:`main`), so the vault precedence is defined once and never
restated in bash.
"""

import os
import sys
from io import StringIO
from pathlib import Path

from dotenv import dotenv_values

from . import config as _config


def brain_home_dotenv_value(brain_home: Path, name: str) -> str | None:
    """*name* from an existing ``$BRAIN_HOME/.env``, or ``None``.

    Neither installer calls ``Config.load``, so nothing publishes the dotenv
    chain into ``os.environ``: a knob read only from the process env ignores
    the file an earlier ``brain setup`` wrote. This is the fallback below the
    process env. Only ``$BRAIN_HOME/.env`` is read, not the whole chain -- it is
    the file setup provisions and appends to, and the one the launchd daemons
    (``BRAIN_HOME`` set, cwd walk-up off) resolve; in a dev checkout it is the
    repo ``.env`` by symlink or identity. Unreadable or missing reads as unset.
    """
    try:
        text = (brain_home / ".env").read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return None
    return dotenv_values(stream=StringIO(text)).get(name) or None


#: Labels :func:`resolve_install_vault_with_source` reports, one per leg.
VAULT_SOURCE_ENV = "BRAIN_VAULT_PATH env"
VAULT_SOURCE_BRAIN_HOME_DOTENV = "$BRAIN_HOME/.env"
VAULT_SOURCE_DEFAULT = "default ~/brain-vault"


def resolve_install_vault_with_source(brain_home: Path) -> tuple[Path, str]:
    """The install vault (no ``--vault`` override) and which leg supplied it.

    Precedence: ``BRAIN_VAULT_PATH`` in the process env >
    ``BRAIN_VAULT_PATH`` in ``$BRAIN_HOME/.env`` > the default vault, each leg
    ``expanduser``-ed; the label is one of the ``VAULT_SOURCE_*`` constants.
    :func:`resolve_install_vault` delegates here, so the precedence is defined
    once. A value that lives only in a repo or cwd ``.env`` is NOT read, so it
    reports ``VAULT_SOURCE_DEFAULT``: what a caller prints is what the
    installers and the daemons would use.
    """
    env = os.environ.get("BRAIN_VAULT_PATH")
    if env:
        return Path(env).expanduser(), VAULT_SOURCE_ENV
    dotenv = brain_home_dotenv_value(brain_home, "BRAIN_VAULT_PATH")
    if dotenv:
        return Path(dotenv).expanduser(), VAULT_SOURCE_BRAIN_HOME_DOTENV
    return _config.DEFAULT_VAULT_PATH, VAULT_SOURCE_DEFAULT


def resolve_install_vault(brain_home: Path, override: Path | None = None) -> Path:
    """The vault an install should use.

    Precedence: *override* (``--vault``) > ``BRAIN_VAULT_PATH`` in the process
    env > ``BRAIN_VAULT_PATH`` in ``$BRAIN_HOME/.env`` > the default vault. One
    definition for both installers, so ``brain setup`` and the
    ``brain-install-launchd`` that every ``brain-up`` runs cannot disagree about
    which vault the daemons watch. The default is read from :mod:`brain.config`
    at call time, not bound at import.

    Every leg is ``expanduser``-ed, the explicit one included: ``--vault
    '~/x'`` (quoted, so the shell left it alone) reaches here as a literal
    ``~``, and every installer write (``.env``, ``os.environ``, the plists)
    derives from this return value.
    """
    if override is not None:
        return override.expanduser()
    return resolve_install_vault_with_source(brain_home)[0]


_USAGE = "usage: python -m brain.install_config BRAIN_HOME"


def main(argv: list[str] | None = None) -> int:
    """Print the vault :func:`resolve_install_vault` picks for ``BRAIN_HOME``.

    The shell entry point: a bash shim passes the ``$BRAIN_HOME`` it already
    resolved for its pid paths, so the vault and the pid files come from the
    same root. Exit 2 on a wrong argument count.
    """
    args = sys.argv[1:] if argv is None else argv
    if len(args) != 1:
        print(_USAGE, file=sys.stderr)
        return 2
    print(resolve_install_vault(Path(args[0]).expanduser()))
    return 0


if __name__ == "__main__":  # pragma: no cover — exercised via subprocess
    raise SystemExit(main())
