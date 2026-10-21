"""brain-up — start the supervised vault-sync watcher and daily-brief job."""
import sys

from ._launcher import exec_shim


def main() -> None:
    exec_shim("brain-up", sys.argv[1:])
