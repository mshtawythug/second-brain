"""brain-status — show the brain runtime status."""
import sys

from ._launcher import exec_shim


def main() -> None:
    exec_shim("brain-status", sys.argv[1:])
