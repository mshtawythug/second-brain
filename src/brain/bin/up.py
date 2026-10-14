"""brain-up — start the supervised vault-sync watcher (the wiki is dormant since R2)."""
import sys

from ._launcher import exec_shim


def main() -> None:
    exec_shim("brain-up", sys.argv[1:])
