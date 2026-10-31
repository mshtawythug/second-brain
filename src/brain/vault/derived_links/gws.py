"""Production ``gws`` subprocess runner conforming to the GwsRunner Protocol.

Mirrors the shell-out shape used by ``brain.ingest.gmail._run`` but routes
all expected subprocess failures (missing binary, non-zero exit, timeout,
unexpected ``OSError``) through :class:`brain.errors.DirectoryRefreshError`
so the refresh helpers in ``directory.py`` can downgrade them to soft
warnings (``refresh_calendar`` / ``refresh_contacts`` already catch
``DirectoryRefreshError`` from the runner).

CLAUDE.md security rules: explicit timeout (30s by default), narrow
exception catch, no ``shell=True``, output truncation in error messages so logs
stay readable when ``gws`` chatters on stderr.

A caller whose read is large (``brain ingest-calendar``'s ``--page-all`` over up
to 92 days, RM-30) passes its own ``timeout`` and a ``max_stdout_bytes`` cap.
With a cap, the read goes through ``capped_process.run_capped`` (sec-117 F6): stdout is
read in chunks and the process group is killed as soon as the cap is passed, at the
timeout, or on an interrupt, so an oversized answer is refused instead of held in
memory; stdin is ``/dev/null`` and stderr is discarded.
"""
import os
import shutil
import subprocess

from brain.errors import DirectoryRefreshError
from brain.ingest.capped_process import run_capped

# Hard cap on stderr included in the translated error message — keeps logs
# readable when ``gws`` panics with a long Python traceback.
_STDERR_SNIPPET_LIMIT = 200

# Default subprocess timeout. ``gws`` calendar / people calls are I/O bound
# against Google APIs; 30s is generous for a single page.
_DEFAULT_TIMEOUT_SECONDS = 30


def real_gws_runner(
    args: list[str],
    *,
    timeout: float = _DEFAULT_TIMEOUT_SECONDS,
    max_stdout_bytes: int | None = None,
) -> str:
    """Shell out to the ``gws`` CLI; return stdout, translate failures.

    Translates ``FileNotFoundError`` (gws missing), ``CalledProcessError``
    (non-zero exit), ``TimeoutExpired`` (timeout), and any other
    ``OSError`` into :class:`DirectoryRefreshError` so the refresh helpers
    in ``directory.py`` log them as warnings and return 0.

    Conforms to the :class:`brain.vault.derived_links.directory.GwsRunner`
    Protocol so it can be passed straight into
    ``refresh_calendar`` / ``refresh_contacts``. ``timeout`` and
    ``max_stdout_bytes`` are for callers with larger reads (module docstring);
    the defaults keep the original behaviour.
    """
    if not args:
        raise DirectoryRefreshError("gws runner invoked with empty args", log_detail="empty args")
    if not shutil.which(args[0]):
        raise DirectoryRefreshError(
            f"`{args[0]}` CLI not found on PATH", log_detail="gws CLI not found on PATH"
        )
    if max_stdout_bytes is not None:
        return _run_capped(args, timeout=timeout, max_stdout_bytes=max_stdout_bytes)
    try:
        proc = subprocess.run(
            args,
            capture_output=True,
            text=True,
            check=True,
            timeout=timeout,
        )
    except subprocess.CalledProcessError as exc:
        stderr_snippet = (exc.stderr or "").strip()[:_STDERR_SNIPPET_LIMIT]
        raise DirectoryRefreshError(
            f"gws command failed (exit {exc.returncode}): {stderr_snippet}",
            log_detail=f"exit {exc.returncode}",
        ) from exc
    except subprocess.TimeoutExpired as exc:
        raise DirectoryRefreshError(
            f"gws command timed out after {timeout:g}s: {' '.join(args)}",
            log_detail=f"timed out after {timeout:g}s",
        ) from exc
    except OSError as exc:
        raise DirectoryRefreshError(
            f"gws command failed: {exc}", log_detail="could not run gws"
        ) from exc
    return proc.stdout


def _run_capped(args: list[str], *, timeout: float, max_stdout_bytes: int) -> str:
    """:func:`real_gws_runner` with stdout read in chunks and refused past the cap.

    Runs through :func:`brain.ingest.capped_process.run_capped` (sec-117 F6): ``gws`` gets
    its own process group, which is killed at ``timeout``, when the cap is passed and on
    any exception in this process (Ctrl-C, or SIGTERM under ``sigterm_as_system_exit``),
    so a helper ``gws`` started cannot keep the read open past the timeout. stdin is
    ``/dev/null`` (an auth prompt cannot wait for input) and stderr is discarded unread,
    so no account text it might quote is ever kept.
    """
    binary = shutil.which(args[0])
    if binary is None:  # pragma: no cover - real_gws_runner checked it just before
        raise DirectoryRefreshError(
            f"`{args[0]}` CLI not found on PATH", log_detail="gws CLI not found on PATH"
        )
    try:
        run = run_capped(
            [binary, *args[1:]],
            timeout=timeout,
            max_stdout_bytes=max_stdout_bytes,
            env=os.environ,
        )
    except OSError as exc:
        raise DirectoryRefreshError(
            f"gws command failed: {exc}", log_detail="could not run gws"
        ) from exc
    if run.timed_out:
        raise DirectoryRefreshError(
            f"gws command timed out after {timeout:g}s: {' '.join(args)}",
            log_detail=f"timed out after {timeout:g}s",
        )
    if run.oversized:
        raise DirectoryRefreshError(
            f"gws output exceeded {max_stdout_bytes} bytes; "
            "narrow the --since/--until window",
            log_detail=f"stdout over {max_stdout_bytes} bytes",
        )
    if run.returncode != 0:
        raise DirectoryRefreshError(
            f"gws command failed (exit {run.returncode})",
            log_detail=f"exit {run.returncode}",
        )
    return run.stdout.decode("utf-8", errors="replace")
