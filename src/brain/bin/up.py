"""brain-up — start the supervised vault-sync watcher and daily-brief job."""
import sys

from ._launcher import exec_shim, parse_no_args

_DESCRIPTION = (
    "Start the brain vault-sync watcher and the daily-brief job under launchd (runs "
    "brain-install-launchd). Idempotent. Takes no arguments."
)


def main() -> None:
    # Before the shim is installed or exec'd: --help must run nothing (F18).
    parse_no_args("brain-up", _DESCRIPTION, sys.argv[1:])
    exec_shim("brain-up", [])
