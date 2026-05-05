"""Helpers for the Phase 3 Quartz frontend integration harness.

This module is the building block under ``tests/test_quartz_e2e.py``.
It owns three responsibilities:

1. Fixture vault staging — copy ``tests/fixtures/quartz_e2e_vault/``
   into a tempdir so the test can mutate it without touching the
   committed fixture, and so multiple test runs are independent.

2. Build invocation — shell out to ``npx quartz build --directory
   <fixture> --output <build>`` from the live brain Quartz workspace
   at ``~/brain-vault/.quartz/``. We reuse the live workspace because
   it already has the brain overlay applied AND ``node_modules``
   installed; standing up a fresh Quartz workspace from scratch in a
   tempdir would cost minutes for a JS test toolchain that's
   intentionally not part of the brain test image. The trade-off:
   the harness has a dependency on the user's brain-vault setup
   being healthy, which the skip-gate enforces.

3. HTTP serve — spin up a thread-bound ``http.server.HTTPServer`` on
   a free localhost port, scoped to the build directory. The
   production blue/green serve flow uses Caddy with a strict Host
   matcher (only ``brain.test`` and ``localhost:8080``); for E2E
   testing we want a port-only matcher so the test can pick a port
   that won't collide with the running production server. A plain
   ``http.server`` is enough for static-file fetches; the harness
   doesn't need any of Caddy's URL-rewrite or symlink-tracking
   features.

Local invocation (when MCP + npx + the live workspace are present):

    pytest tests/test_quartz_e2e.py -v --no-cov -m e2e

If any prerequisite is missing the suite skips cleanly with a
descriptive reason rather than failing — the brain test image lacks a
JS toolchain by design.
"""
from __future__ import annotations

import contextlib
import http.server
import os
import shutil
import socket
import socketserver
import subprocess
import threading
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
FIXTURE_VAULT = REPO_ROOT / "tests" / "fixtures" / "quartz_e2e_vault"

# brain: the live Quartz workspace path. Pinned to the user's brain-
# vault checkout because that's where ``brain vault render --overlay``
# applies the brain overlay AND where ``npm install`` has already
# materialized ``node_modules``. Tests skip cleanly when this dir is
# absent (CI / fresh dev machines).
DEFAULT_QUARTZ_WORKSPACE = Path.home() / "brain-vault" / ".quartz"

# brain: hard ceiling on the build subprocess. Aligns with
# ``brain.wiki.build_swap``'s ``timeout_seconds=600`` default; an E2E
# fixture vault is tiny (5 markdown files) so this is generous, but
# we keep the same upper bound so a wedged Quartz process can't hold
# the test runner forever.
DEFAULT_BUILD_TIMEOUT_S = 600.0


# ---------------------------------------------------------------------------
# Skip-gate prerequisites
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class HarnessPreflight:
    """Result of probing the local environment for harness deps.

    Each field is ``None`` when the dep is satisfied; a string reason
    otherwise. A test is OK to proceed iff every field is ``None``.
    """

    npx_missing: str | None
    workspace_missing: str | None
    fixture_missing: str | None

    @property
    def ok(self) -> bool:
        return (
            self.npx_missing is None
            and self.workspace_missing is None
            and self.fixture_missing is None
        )

    @property
    def skip_reason(self) -> str:
        """Compact one-line reason suitable for ``pytest.skip(...)``."""
        reasons = [
            r
            for r in (self.npx_missing, self.workspace_missing, self.fixture_missing)
            if r is not None
        ]
        return "; ".join(reasons)


def preflight(
    *, workspace: Path = DEFAULT_QUARTZ_WORKSPACE
) -> HarnessPreflight:
    """Probe the local env for harness prerequisites.

    Returns a :class:`HarnessPreflight` so the caller can render its
    own skip message. We intentionally don't raise — the caller (a
    pytest fixture or a test) decides what to do with a missing dep.
    """
    npx_path = shutil.which("npx")
    npx_missing: str | None = (
        "`npx` not on PATH (Node.js toolchain absent)"
        if npx_path is None
        else None
    )

    workspace_missing: str | None
    if not workspace.is_dir():
        workspace_missing = f"Quartz workspace missing at {workspace}"
    elif not (workspace / "quartz.config.ts").is_file():
        workspace_missing = (
            f"Quartz workspace at {workspace} is missing quartz.config.ts"
        )
    elif not (workspace / "node_modules").is_dir():
        workspace_missing = (
            f"Quartz workspace at {workspace} has no node_modules — run `npm install`"
        )
    else:
        workspace_missing = None

    fixture_missing: str | None
    if not FIXTURE_VAULT.is_dir():
        fixture_missing = f"E2E fixture vault missing at {FIXTURE_VAULT}"
    else:
        fixture_missing = None

    return HarnessPreflight(
        npx_missing=npx_missing,
        workspace_missing=workspace_missing,
        fixture_missing=fixture_missing,
    )


# ---------------------------------------------------------------------------
# Vault staging + build
# ---------------------------------------------------------------------------


def stage_fixture_vault(dest: Path) -> Path:
    """Copy the committed fixture vault into ``dest`` and return the path.

    ``dest`` is created if missing. We copy (rather than symlink) so a
    test that mutates the staged vault — e.g. to verify the watcher's
    rebuild path in a future iteration — doesn't pollute the
    committed fixture. ``copytree`` with ``dirs_exist_ok=False``
    enforces that the caller passes a fresh dir.
    """
    if not FIXTURE_VAULT.is_dir():
        raise FileNotFoundError(f"fixture vault not found at {FIXTURE_VAULT}")
    if dest.exists():
        # Same fail-loudly contract as ``shutil.copytree`` defaults.
        raise FileExistsError(f"refusing to overwrite existing dir at {dest}")
    shutil.copytree(FIXTURE_VAULT, dest)
    return dest


def quartz_build(
    *,
    vault: Path,
    output: Path,
    workspace: Path = DEFAULT_QUARTZ_WORKSPACE,
    npx_path: str | None = None,
    timeout_seconds: float = DEFAULT_BUILD_TIMEOUT_S,
    env: dict[str, str] | None = None,
) -> None:
    """Run ``npx quartz build --directory <vault> --output <output>``.

    The invocation matches ``brain.wiki.build_swap._run_build``'s
    ``output-flag`` branch — same flag form, same workspace cwd, same
    arg order. Reusing the canonical shape (rather than reimplementing
    it) keeps drift between the harness and the production builder
    minimal.

    ``output`` must NOT exist before the call (Quartz will create it).
    Raises ``subprocess.CalledProcessError`` on non-zero exit; the
    caller is expected to wrap that into a test-friendly message.
    """
    if output.exists():
        raise FileExistsError(
            f"refusing to overwrite existing build output at {output}"
        )
    npx = npx_path or shutil.which("npx")
    if npx is None:
        raise RuntimeError("`npx` not on PATH — cannot run Quartz build")
    args = [
        npx,
        "quartz",
        "build",
        "--directory",
        str(vault),
        "--output",
        str(output),
    ]
    merged_env: dict[str, str] | None = None
    if env is not None:
        merged_env = dict(os.environ)
        merged_env.update(env)
    subprocess.run(  # noqa: S603 — list-form args, no shell
        args,
        cwd=str(workspace),
        check=True,
        timeout=timeout_seconds,
        env=merged_env,
    )


# ---------------------------------------------------------------------------
# HTTP serve
# ---------------------------------------------------------------------------


def find_free_port() -> int:
    """Return an OS-allocated free TCP port on ``127.0.0.1``.

    Using port 0 + immediate close is the standard idiom — the kernel
    picks a port in the ephemeral range that's free at the moment of
    the bind. There's a theoretical race where another process grabs
    the same port between this call and the harness's own bind, but
    in practice on a developer machine the window is sub-millisecond.
    """
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


class _ThreadedHTTPServer(socketserver.ThreadingMixIn, http.server.HTTPServer):
    """Threaded HTTP server so multiple requests don't serialize.

    The default ``HTTPServer`` is single-threaded — fine for one-shot
    fetches, but the harness sometimes parallel-fetches the index +
    the contentIndex JSON, and a serial server can wedge if the
    Playwright probe's first request stalls. ``ThreadingMixIn`` is a
    one-line fix.
    """

    daemon_threads = True
    allow_reuse_address = True


@contextlib.contextmanager
def serve_directory(
    directory: Path, *, port: int | None = None
) -> Iterator[tuple[str, int]]:
    """Serve ``directory`` over HTTP on ``127.0.0.1`` and yield ``(url, port)``.

    Context-manager semantics: the server starts before ``yield`` and
    is shut down + joined on exit (success OR failure). The yielded
    URL has no trailing slash so callers can build paths off it
    cleanly: ``f"{url}/static/contentIndex.json"``.

    ``port`` defaults to a kernel-allocated free port. Pass an
    explicit port only if the test needs a stable URL across runs
    (e.g. to share between subprocesses).
    """
    if not directory.is_dir():
        raise FileNotFoundError(f"cannot serve missing directory {directory}")
    chosen_port = port if port is not None else find_free_port()

    # SimpleHTTPRequestHandler resolves paths relative to its `cwd` —
    # but cwd is process-global. Subclassing to bind a directory at
    # construction time is cleaner than mutating cwd.
    handler_dir = str(directory)

    class _Handler(http.server.SimpleHTTPRequestHandler):
        def __init__(self, *args: object, **kwargs: object) -> None:
            super().__init__(*args, directory=handler_dir, **kwargs)  # type: ignore[arg-type]

        def log_message(self, format: str, *args: object) -> None:
            # Silence the default per-request stderr noise — pytest
            # captures stderr and a 1000-line spam scrolls the real
            # output offscreen. Keep a hook for re-enabling under a
            # debug flag if a future test needs it.
            return

    server = _ThreadedHTTPServer(("127.0.0.1", chosen_port), _Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield (f"http://127.0.0.1:{chosen_port}", chosen_port)
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5.0)
