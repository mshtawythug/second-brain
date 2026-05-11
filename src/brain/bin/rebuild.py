"""brain-rebuild — rebuild the wiki workspace + cold-restart watchers."""
import sys

from ._launcher import exec_shim


def main() -> None:
    exec_shim("brain-rebuild", sys.argv[1:])
