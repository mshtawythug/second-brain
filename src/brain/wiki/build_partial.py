"""Python subprocess wrapper for the Quartz ``build-partial`` CLI subcommand.

Drives the Node.js ``build-partial`` handler (T4) as a one-shot subprocess
and surfaces structured failure information via :class:`BrainWikiPartialBuildError`
so the watcher (T6) can route recovery decisions (e.g. force full build on exit 6
vs. increment a retry counter on exit 5) without string-matching stderr.
"""
from __future__ import annotations

import logging
import os
import re
import shutil
import subprocess
from dataclasses import dataclass
from enum import Enum
from pathlib import Path

from brain.wiki.errors import BrainWikiBuildError

logger = logging.getLogger(__name__)

# Maximum number of stderr characters surfaced in error messages.
# Long emitter stack traces are trimmed to avoid log bloat.
_STDERR_TAIL_LIMIT = 500

# Regex for the success stdout line emitted by build_partial_handler.js:
#   "wiki: build-partial slug=<slug> elapsed=<N>ms"
_ELAPSED_RE = re.compile(r"elapsed=(\d+)ms")


# ---------------------------------------------------------------------------
# Failure taxonomy
# ---------------------------------------------------------------------------


class PartialBuildFailureKind(Enum):
    """Distinct failure categories for telemetry and recovery routing.

    T6 uses this enum to decide whether to fall back to a full build
    immediately (e.g. ``UNSUPPORTED_SLUG_SCOPE``) or to increment a
    partial-failure counter first (e.g. ``EMITTER_FAILED``).
    """

    MISSING_NODE = "missing-node"
    """``node`` binary not found on PATH."""

    MISSING_BOOTSTRAP = "missing-bootstrap"
    """``bootstrap-cli.mjs`` absent in the workspace directory."""

    MANIFEST_OR_CONTENTMAP_UNREADABLE = "manifest-or-contentmap-unreadable"
    """Exit 1: manifest or contentmap JSON missing or unparseable."""

    ENVELOPE_MISMATCH = "envelope-mismatch"
    """Exit 2: ``version`` or ``parent_build_id`` differs between manifest and contentmap."""

    SLUG_NOT_IN_MANIFEST = "slug-not-in-manifest"
    """Exit 3: the requested slug is absent from ``manifest.slugs``."""

    SLUG_NOT_IN_CONTENTMAP = "slug-not-in-contentmap"
    """Exit 4: the slug is in the manifest but absent from ``contentmap.entries``."""

    EMITTER_FAILED = "emitter-failed"
    """Exit 5: an uncaught exception during the emitter walk (fail-fast)."""

    UNSUPPORTED_SLUG_SCOPE = "unsupported-slug-scope"
    """Exit 6: slug is a tag/folder/index page that requires a full build."""

    UNKNOWN_NONZERO = "unknown-nonzero"
    """Any nonzero exit code not covered by exit codes 1-6."""

    SUBPROCESS_TIMEOUT = "subprocess-timeout"
    """The build-partial subprocess exceeded the configured timeout."""


# Mapping from Node process exit codes (1-6) to failure kinds.
_EXIT_CODE_TO_KIND: dict[int, PartialBuildFailureKind] = {
    1: PartialBuildFailureKind.MANIFEST_OR_CONTENTMAP_UNREADABLE,
    2: PartialBuildFailureKind.ENVELOPE_MISMATCH,
    3: PartialBuildFailureKind.SLUG_NOT_IN_MANIFEST,
    4: PartialBuildFailureKind.SLUG_NOT_IN_CONTENTMAP,
    5: PartialBuildFailureKind.EMITTER_FAILED,
    6: PartialBuildFailureKind.UNSUPPORTED_SLUG_SCOPE,
}


# ---------------------------------------------------------------------------
# Exception
# ---------------------------------------------------------------------------


class BrainWikiPartialBuildError(BrainWikiBuildError):
    """Raised on any ``build-partial`` failure.

    Carries a structured :class:`PartialBuildFailureKind` for routing and
    the originating ``slug`` for diagnostics.  The human-readable message
    always includes the slug, exit code (where applicable), and the tail of
    stderr so an operator can diagnose without needing the raw subprocess
    output.
    """

    def __init__(
        self,
        message: str,
        *,
        kind: PartialBuildFailureKind,
        slug: str | None = None,
    ) -> None:
        super().__init__(message)
        self.kind = kind
        self.slug = slug


# ---------------------------------------------------------------------------
# Result
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class PartialBuildResult:
    """Summary of a successful ``build-partial`` invocation.

    ``elapsed_ms`` is parsed from the Node process stdout line
    ``"wiki: build-partial slug=<slug> elapsed=<N>ms"``.  If the pattern
    is not matched (e.g. the handler changed its output format), ``elapsed_ms``
    is ``-1`` and a warning is logged; the result is still considered
    successful because exit code 0 was returned.
    """

    slug: str
    elapsed_ms: int
    stdout: str
    stderr: str


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def run_build_partial(
    *,
    slug: str,
    vault_dir: Path,
    build_dir: Path,
    workspace_dir: Path,
    timeout_s: float = 30.0,
) -> PartialBuildResult:
    """Run the Quartz ``build-partial`` CLI subcommand for a single slug.

    Invokes::

        node <workspace_dir>/quartz/bootstrap-cli.mjs build-partial \\
          --directory <vault_dir> \\
          --output <build_dir> \\
          --slug <slug>

    with ``cwd=workspace_dir`` (the workspace ROOT) so Quartz can resolve
    ``./package.json`` and ``./quartz/*`` relative paths correctly.

    Args:
        slug: The Quartz slug to partially rebuild (vault-relative, no ``.md``).
        vault_dir: Vault root directory (``--directory`` argument to Quartz).
        build_dir: Quartz output directory for the active color
            (``--output`` argument; ``.build-id`` is written here on success).
        workspace_dir: **Quartz workspace root** (e.g. ``<vault>/.quartz``).
            Must contain ``quartz/bootstrap-cli.mjs`` and ``package.json``.
            The subprocess runs with ``cwd=workspace_dir`` so Quartz's relative
            paths (``./package.json``, ``./quartz/build.ts``, etc.) resolve
            correctly.  Relative paths are resolved to absolute before use.
        timeout_s: Hard wall-clock limit for the subprocess (default: 30 s).

    Returns:
        :class:`PartialBuildResult` on exit code 0.

    Raises:
        :class:`BrainWikiPartialBuildError`: On any failure.  The ``kind``
            attribute is a :class:`PartialBuildFailureKind` for routing;
            the message includes the slug, exit code, and stderr tail.
    """
    # Resolve workspace_dir to an absolute path up-front so that relative
    # inputs (e.g. Path(".quartz")) work correctly as subprocess cwd.
    workspace = workspace_dir.expanduser().resolve()

    # Step 1: Resolve node binary — hard-fail rather than falling back to npx.
    node = shutil.which("node")
    if node is None:
        raise BrainWikiPartialBuildError(
            "node binary not found on PATH; install Node.js"
            " (Homebrew: `brew install node`;"
            " Linux: nodejs.org or your distro's package manager)",
            kind=PartialBuildFailureKind.MISSING_NODE,
            slug=slug,
        )

    # Step 2: Verify bootstrap-cli.mjs exists at <workspace>/quartz/bootstrap-cli.mjs.
    bootstrap = workspace / "quartz" / "bootstrap-cli.mjs"
    if not bootstrap.is_file():
        raise BrainWikiPartialBuildError(
            f"Quartz bootstrap CLI not found at {bootstrap};"
            " run `brain vault render --overlay` to reinstall the workspace overlay",
            kind=PartialBuildFailureKind.MISSING_BOOTSTRAP,
            slug=slug,
        )

    # Step 3: Build subprocess argv.
    args: list[str] = [
        node,
        str(bootstrap),
        "build-partial",
        "--directory",
        str(vault_dir),
        "--output",
        str(build_dir),
        "--slug",
        slug,
    ]

    # Step 4: Run subprocess — capture output, never raise on exit code.
    # cwd = workspace ROOT so Quartz's ./package.json + ./quartz/* paths resolve.
    try:
        proc = subprocess.run(  # noqa: S603 — list-form args, no shell injection
            args,
            cwd=str(workspace),
            capture_output=True,
            text=True,
            timeout=timeout_s,
            env=os.environ,
            check=False,
        )
    except subprocess.TimeoutExpired as exc:
        raise BrainWikiPartialBuildError(
            f"build-partial slug={slug} timed out after {timeout_s}s",
            kind=PartialBuildFailureKind.SUBPROCESS_TIMEOUT,
            slug=slug,
        ) from exc
    except OSError as exc:
        raise BrainWikiPartialBuildError(
            f"build-partial slug={slug} failed to launch: {exc}",
            kind=PartialBuildFailureKind.UNKNOWN_NONZERO,
            slug=slug,
        ) from exc

    rc = proc.returncode

    # Step 5 (success): parse elapsed_ms from stdout and return.
    if rc == 0:
        elapsed_ms = -1
        m = _ELAPSED_RE.search(proc.stdout)
        if m:
            elapsed_ms = int(m.group(1))
        else:
            logger.warning(
                "build-partial slug=%s: could not parse elapsed_ms from stdout: %r",
                slug,
                proc.stdout[:200],
            )
        return PartialBuildResult(
            slug=slug,
            elapsed_ms=elapsed_ms,
            stdout=proc.stdout,
            stderr=proc.stderr,
        )

    # Steps 6-7: Non-zero exit — map to failure kind and raise.
    kind = _EXIT_CODE_TO_KIND.get(rc, PartialBuildFailureKind.UNKNOWN_NONZERO)
    # Trim stderr to avoid bloating structured logs with multi-KB stack traces.
    stderr_tail = proc.stderr[-_STDERR_TAIL_LIMIT:] if proc.stderr else ""
    raise BrainWikiPartialBuildError(
        f"build-partial slug={slug} exited {rc}: {stderr_tail}",
        kind=kind,
        slug=slug,
    )
