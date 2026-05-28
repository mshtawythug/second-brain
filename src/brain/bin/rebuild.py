"""brain-rebuild — full-corpus rebuild orchestrator (see brain.maintenance)."""
import sys

from ..maintenance import main as _main


def main() -> None:
    raise SystemExit(_main(sys.argv[1:]))
