"""brain-up — start the wiki + supervised watchers."""
import sys

from ._launcher import exec_shim


def main() -> None:
    exec_shim("brain-up", sys.argv[1:])
