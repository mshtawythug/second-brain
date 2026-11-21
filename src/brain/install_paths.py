"""Where user-writable files live: the source checkout in dev, ``$BRAIN_HOME`` when installed.

A non-editable install (``pip install secondbrain-py``, ``uv tool install``)
puts the package at ``<venv>/lib/python3.X/site-packages/brain/``. Any path
derived from ``__file__`` then lands inside the virtualenv, which is not the
user's config and is wiped by the next reinstall (F25). Only a source checkout
(an editable install) has a meaningful package-relative location.

The checkout test here is the one :func:`brain.config._brain_home_root` has
always used -- ``pyproject.toml`` three levels above the module -- extracted so
that every caller asks the same question rather than a second heuristic.

``brain.config`` is imported lazily inside the functions that need it, as in
:mod:`brain.log_rotation`: ``config`` imports :func:`checkout_root_for` from
here, so a module-level import would close a cycle.
"""
from pathlib import Path

from dotenv import dotenv_values

from .errors import BrainError

#: Directory, under the checkout or ``$BRAIN_HOME``, holding the eval harness's
#: corpora and baselines.
_CHECKOUT_EVAL_DIR = ("tests", "eval")
_BRAIN_HOME_EVAL_DIR = "eval"


class DanglingDotenvLinkError(BrainError):
    """A ``.env`` to be written is a symlink whose target does not exist."""


def checkout_root_for(module_file: Path) -> Path | None:
    """Return the checkout root for ``<root>/src/brain/<module>.py``, or ``None``.

    The root is ``module_file``'s third ancestor, and it counts as a checkout
    only when it holds ``pyproject.toml``. Pure: no ``resolve()`` here, so a
    caller passing a synthetic path (``_brain_home_root``'s test seam) gets
    exactly that path's answer. Production callers resolve first.
    """
    root = module_file.parent.parent.parent
    return root if (root / "pyproject.toml").is_file() else None


def source_checkout_root() -> Path | None:
    """The checkout this package runs from, or ``None`` for a non-editable install.

    This module sits beside ``config.py`` in ``src/brain/``, so the same three
    hops apply. Tests simulate a wheel install by monkeypatching this function.
    """
    return checkout_root_for(Path(__file__).resolve())


def writable_dotenv() -> Path:
    """The ``.env`` that CLI writers (``brain owner``) update.

    In a checkout: the repo ``.env`` (``config._project_dotenv``), exactly as
    before F25, even when ``$BRAIN_HOME`` points elsewhere. Installed: the
    canonical ``$BRAIN_HOME/.env`` -- never a file beside the package. Read
    through the ``config`` module's attributes so tests that redirect either
    helper redirect this too. Resolved per call, never cached.
    """
    from . import config

    if source_checkout_root() is not None:
        return config._project_dotenv()
    return config._brain_home_dotenv()


def dotenv_write_target(path: Path) -> Path:
    """The file a write to *path* must replace: the link's target when *path* is a symlink.

    ``brain setup`` deliberately makes ``$BRAIN_HOME/.env`` a symlink to a
    checkout's ``.env``; an atomic ``os.replace`` onto the link itself would
    turn it into a regular copy and silently fork the config, so writers go
    through the link instead. A target outside ``$BRAIN_HOME`` is fine -- that
    is what the link is for. A non-link path is returned unchanged.

    Raises:
        DanglingDotenvLinkError: *path* is a symlink to a missing file. The
            user's config presumably moved (``brain setup`` reports this shape
            as dangling rather than replacing it), so neither re-creating the
            old target nor replacing the link is safe to do silently.
    """
    if not path.is_symlink():
        return path
    if not path.exists():
        raise DanglingDotenvLinkError(
            f"{path} is a symlink to {path.readlink()}, which does not exist; "
            "fix or remove the link, then retry"
        )
    return path.resolve()


def eval_data_dir() -> Path:
    """Where ``brain eval`` keeps its corpora and baselines.

    In a checkout: ``<checkout>/tests/eval``, the paths the eval gate
    (``ci.json``, the gitignored ``golden_corpus.yaml``) has always used.
    Installed: ``$BRAIN_HOME/eval``. Resolved per call, never cached, like
    :func:`brain.config.default_backup_dir`.
    """
    root = source_checkout_root()
    if root is not None:
        return root.joinpath(*_CHECKOUT_EVAL_DIR)
    from . import config

    return config._brain_home_root() / _BRAIN_HOME_EVAL_DIR


def eval_baselines_dir() -> Path:
    """Where ``brain eval --record-baseline`` / ``--baseline`` read and write ``<NAME>.json``."""
    return eval_data_dir() / "baselines"


def legacy_dotenv_shadow_warning(key: str) -> str | None:
    """A one-line warning when the package-relative ``.env`` still sets *key*, else ``None``.

    Before F25 an installed ``brain owner`` wrote ``<venv>/lib/python3.X/.env``.
    That file is still link 2 of ``Config.load``'s chain -- deliberately: it may
    hold the user's real settings -- and it outranks ``$BRAIN_HOME/.env``, so a
    new write there is silently shadowed until the user moves the line over.
    Names both files and the key, NEVER the value. ``None`` in a checkout (the
    package-relative ``.env`` IS the file written), when the legacy file is
    absent or unreadable, or when it does not set *key*. Parsed with the same
    ``dotenv_values`` reader ``Config.load`` uses.
    """
    if source_checkout_root() is not None:
        return None
    from . import config

    legacy = config._project_dotenv()
    try:
        if not legacy.is_file() or dotenv_values(legacy).get(key) is None:
            return None
    except (OSError, UnicodeDecodeError):
        return None
    target = config._brain_home_dotenv()
    return (
        f"warning: {legacy} also sets {key} and outranks {target}, so it wins until you "
        f"move its {key} line into {target} and delete it"
    )
