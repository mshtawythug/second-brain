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
With a cap, stdout is read in chunks and the process is killed as soon as the
cap is passed, so an oversized answer is refused instead of held in memory.
"""
import shutil
import subprocess
import tempfile
import threading

from brain.errors import DirectoryRefreshError

# Hard cap on stderr included in the translated error message — keeps logs
# readable when ``gws`` panics with a long Python traceback.
_STDERR_SNIPPET_LIMIT = 200

# Default subprocess timeout. ``gws`` calendar / people calls are I/O bound
# against Google APIs; 30s is generous for a single page.
_DEFAULT_TIMEOUT_SECONDS = 30

_READ_CHUNK_BYTES = 65536


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

    A timer kills the process at ``timeout``; stderr goes to a temporary file so
    a chatty ``gws`` cannot fill its pipe and stall the stdout read.
    """
    timed_out = threading.Event()
    try:
        with (
            tempfile.TemporaryFile() as stderr,
            subprocess.Popen(args, stdout=subprocess.PIPE, stderr=stderr) as proc,
        ):
            def _kill() -> None:
                timed_out.set()
                proc.kill()

            timer = threading.Timer(timeout, _kill)
            timer.start()
            try:
                chunks: list[bytes] = []
                size = 0
                assert proc.stdout is not None  # stdout=PIPE
                while chunk := proc.stdout.read(_READ_CHUNK_BYTES):
                    size += len(chunk)
                    if size > max_stdout_bytes:
                        proc.kill()
                        raise DirectoryRefreshError(
                            f"gws output exceeded {max_stdout_bytes} bytes; "
                            "narrow the --since/--until window",
                            log_detail=f"stdout over {max_stdout_bytes} bytes",
                        )
                    chunks.append(chunk)
                returncode = proc.wait()
            finally:
                timer.cancel()
            stderr.seek(0)
            stderr_snippet = stderr.read(_STDERR_SNIPPET_LIMIT).decode(errors="replace")
    except OSError as exc:
        raise DirectoryRefreshError(
            f"gws command failed: {exc}", log_detail="could not run gws"
        ) from exc
    if timed_out.is_set():
        raise DirectoryRefreshError(
            f"gws command timed out after {timeout:g}s: {' '.join(args)}",
            log_detail=f"timed out after {timeout:g}s",
        )
    if returncode != 0:
        raise DirectoryRefreshError(
            f"gws command failed (exit {returncode}): {stderr_snippet.strip()}",
            log_detail=f"exit {returncode}",
        )
    return b"".join(chunks).decode("utf-8", errors="replace")
