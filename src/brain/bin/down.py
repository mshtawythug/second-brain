"""brain-down — stop the supervised watcher (and legacy wiki leftovers)."""
import sys

from ._launcher import exec_shim, parse_no_args

_DESCRIPTION = (
    "Stop the brain vault-sync watcher: boot out its LaunchAgents and stop the processes in "
    "its pid files (and legacy wiki leftovers). The plists stay. Idempotent. Takes no "
    "arguments."
)


def main() -> None:
    # Before the shim is installed or exec'd: --help must run nothing (F18).
    parse_no_args("brain-down", _DESCRIPTION, sys.argv[1:])
    exec_shim("brain-down", [])
