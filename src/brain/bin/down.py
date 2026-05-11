"""brain-down — stop the wiki + supervised watchers."""
import sys

from ._launcher import exec_shim


def main() -> None:
    exec_shim("brain-down", sys.argv[1:])
