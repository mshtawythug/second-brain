"""Helper that wraps every `docker compose` invocation with the correct flags.

Centralising this prevents call sites from drifting — every invocation MUST
use both ``-f <BRAIN_HOME>/docker-compose.yml`` AND ``--project-name brain``.
Without ``--project-name``, the project name defaults to cwd basename and shells
from different directories can target different compose projects against the
same compose file.
"""
from pathlib import Path

from .config import _brain_home_root


def compose_cmd(*args: str, brain_home: Path | None = None) -> list[str]:
    """Build a docker-compose argv list with mandatory -f and --project-name flags.

    Resolves ``$BRAIN_HOME`` via :func:`brain.config._brain_home_root` — so the
    compose file path is consistent with the rest of the runtime.  The optional
    ``brain_home`` kwarg overrides the resolved path and is the recommended seam
    for tests: pass ``tmp_path / ".brain"`` rather than monkeypatching env vars.
    """
    home = brain_home if brain_home is not None else _brain_home_root()
    compose_file = home / "docker-compose.yml"
    return [
        "docker",
        "compose",
        "-f",
        str(compose_file),
        "--project-name",
        "brain",
        *args,
    ]
