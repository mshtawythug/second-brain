"""Install-time config lookups shared by ``brain setup`` and ``brain-install-launchd``."""

import os
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


def resolve_install_vault(brain_home: Path, override: Path | None = None) -> Path:
    """The vault an install should use.

    Precedence: *override* (``--vault``) > ``BRAIN_VAULT_PATH`` in the process
    env > ``BRAIN_VAULT_PATH`` in ``$BRAIN_HOME/.env`` > the default vault. One
    definition for both installers, so ``brain setup`` and the
    ``brain-install-launchd`` that every ``brain-up`` runs cannot disagree about
    which vault the daemons watch. The default is read from :mod:`brain.config`
    at call time, not bound at import.
    """
    if override is not None:
        return override
    raw = os.environ.get("BRAIN_VAULT_PATH") or brain_home_dotenv_value(
        brain_home, "BRAIN_VAULT_PATH"
    )
    return Path(raw).expanduser() if raw else _config.DEFAULT_VAULT_PATH
