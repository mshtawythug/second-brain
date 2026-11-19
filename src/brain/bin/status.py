"""brain-status — show the brain runtime status."""
import sys

from ._launcher import exec_shim, parse_no_args

_DESCRIPTION = (
    "Show whether the brain vault-sync watcher is running, and the vault it watches. "
    "Read-only. Takes no arguments."
)


def main() -> None:
    # Before the shim is installed or exec'd: --help must run nothing (F18).
    parse_no_args("brain-status", _DESCRIPTION, sys.argv[1:])
    exec_shim("brain-status", [])
