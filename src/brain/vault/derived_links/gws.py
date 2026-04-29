"""Production ``gws`` subprocess runner conforming to the GwsRunner Protocol.

Mirrors the shell-out shape used by ``brain.ingest.gmail._run`` but routes
all expected subprocess failures (missing binary, non-zero exit, timeout,
unexpected ``OSError``) through :class:`brain.errors.DirectoryRefreshError`
so the refresh helpers in ``directory.py`` can downgrade them to soft
warnings (``refresh_calendar`` / ``refresh_contacts`` already catch
``DirectoryRefreshError`` from the runner).

CLAUDE.md security rules: explicit timeout (30s), narrow exception catch,
no ``shell=True``, output truncation in error messages so logs stay
readable when ``gws`` chatters on stderr.
"""
import shutil
import subprocess

from brain.errors import DirectoryRefreshError

# Hard cap on stderr included in the translated error message — keeps logs
# readable when ``gws`` panics with a long Python traceback.
_STDERR_SNIPPET_LIMIT = 200

# Default subprocess timeout. ``gws`` calendar / people calls are I/O bound
# against Google APIs; 30s is generous for a single page.
_DEFAULT_TIMEOUT_SECONDS = 30


def real_gws_runner(args: list[str]) -> str:
    """Shell out to the ``gws`` CLI; return stdout, translate failures.

    Translates ``FileNotFoundError`` (gws missing), ``CalledProcessError``
    (non-zero exit), ``TimeoutExpired`` (timeout), and any other
    ``OSError`` into :class:`DirectoryRefreshError` so the refresh helpers
    in ``directory.py`` log them as warnings and return 0.

    Conforms to the :class:`brain.vault.derived_links.directory.GwsRunner`
    Protocol so it can be passed straight into
    ``refresh_calendar`` / ``refresh_contacts``.
    """
    if not args:
        raise DirectoryRefreshError("gws runner invoked with empty args")
    if not shutil.which(args[0]):
        raise DirectoryRefreshError(f"`{args[0]}` CLI not found on PATH")
    try:
        proc = subprocess.run(
            args,
            capture_output=True,
            text=True,
            check=True,
            timeout=_DEFAULT_TIMEOUT_SECONDS,
        )
    except subprocess.CalledProcessError as exc:
        stderr_snippet = (exc.stderr or "").strip()[:_STDERR_SNIPPET_LIMIT]
        raise DirectoryRefreshError(
            f"gws command failed (exit {exc.returncode}): {stderr_snippet}"
        ) from exc
    except subprocess.TimeoutExpired as exc:
        raise DirectoryRefreshError(
            f"gws command timed out after {_DEFAULT_TIMEOUT_SECONDS}s: "
            f"{' '.join(args)}"
        ) from exc
    except OSError as exc:
        raise DirectoryRefreshError(f"gws command failed: {exc}") from exc
    return proc.stdout
