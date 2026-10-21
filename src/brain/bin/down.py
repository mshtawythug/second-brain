"""brain-down — stop the supervised watcher (and legacy wiki leftovers)."""
import sys

from ._launcher import exec_shim


def main() -> None:
    exec_shim("brain-down", sys.argv[1:])
