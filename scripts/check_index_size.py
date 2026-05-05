"""Enforce the gzipped-size budget on Quartz's emitted ``contentIndex.json``.

The slim transform in ``quartz_overrides/quartz/plugins/emitters/contentIndex.ts``
strips full document bodies out of the index and writes them to per-slug
``static/contentBodies/<slug>.json`` files instead. After that change the
index is expected to fit under 2 MB gzipped (down from ~5 MB before the
split). This script reads the emitted JSON, gzip-compresses it in memory,
and exits non-zero if the compressed size exceeds the budget — a small CLI
tool that can be wired into CI later or run by hand after a build.

Usage::

    python scripts/check_index_size.py [path/to/contentIndex.json]

When invoked without an argument it falls back to the canonical post-build
artifact path ``<repo>/dist/static/contentIndex.json``.
"""
import argparse
import gzip
import sys
from pathlib import Path

# 2 MB gzipped — the budget set by the P3.1 plan
# (`docs/plans/2026-05-03-wiki-ux-overhaul.md`). Pinned as bytes (not MB) so
# the comparison is exact and the constant doubles as the failure-message
# numerator.
DEFAULT_BUDGET_BYTES = 2 * 1024 * 1024

REPO_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_INDEX_PATH = REPO_ROOT / "dist" / "static" / "contentIndex.json"


def gzipped_size(payload: bytes) -> int:
    """Return the gzipped byte length of ``payload``.

    Uses :func:`gzip.compress` so the helper has no on-disk side effects
    (write a temp file, run ``gzip``, stat it). The compressed bytes are
    discarded — we only care about the size — so the caller needn't worry
    about memory pressure: even a 20 MB index compresses in milliseconds
    on a modern machine and the result is well under ``MAX_INT``.
    """
    return len(gzip.compress(payload))


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    """Parse CLI args. Split out so tests can drive it programmatically."""
    parser = argparse.ArgumentParser(
        description=(
            "Check that Quartz's emitted contentIndex.json fits inside the "
            "gzipped-size budget (2 MB by default)."
        ),
    )
    parser.add_argument(
        "path",
        nargs="?",
        type=Path,
        default=DEFAULT_INDEX_PATH,
        help=(
            "Path to contentIndex.json. Defaults to "
            "<repo>/dist/static/contentIndex.json."
        ),
    )
    parser.add_argument(
        "--budget-bytes",
        type=int,
        default=DEFAULT_BUDGET_BYTES,
        help=(
            "Maximum allowed gzipped size in bytes "
            f"(default: {DEFAULT_BUDGET_BYTES})."
        ),
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    """CLI entrypoint. Returns a process exit code (0 ok, 1 over budget, 2 missing)."""
    args = parse_args(argv)
    path: Path = args.path
    budget: int = args.budget_bytes

    if not path.is_file():
        print(f"error: contentIndex.json not found at {path}", file=sys.stderr)
        return 2

    raw = path.read_bytes()
    raw_size = len(raw)
    compressed = gzipped_size(raw)

    if compressed > budget:
        print(
            f"FAIL: {path} gzipped is {compressed:,} B "
            f"(> budget {budget:,} B; raw {raw_size:,} B)",
            file=sys.stderr,
        )
        return 1

    print(
        f"OK: {path} gzipped is {compressed:,} B "
        f"(<= budget {budget:,} B; raw {raw_size:,} B)"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
