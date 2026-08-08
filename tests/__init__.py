"""Test package — pins deterministic CLI rendering before anything imports Typer.

Typer decides whether to emit ANSI colour exactly ONCE, at ``typer.rich_utils``
import time, via a module-level constant::

    FORCE_TERMINAL = (
        True
        if getenv("GITHUB_ACTIONS") or getenv("FORCE_COLOR") or getenv("PY_COLORS")
        else None
    )
    if _TYPER_FORCE_DISABLE_TERMINAL:
        FORCE_TERMINAL = False

On GitHub Actions ``GITHUB_ACTIONS`` is always set, so every ``--help`` screen
arrives interleaved with escape codes and a plain
``assert "--some-flag" in result.output`` fails — while passing on every
developer machine, which is precisely how that divergence reached CI unnoticed.

``_TYPER_FORCE_DISABLE_TERMINAL`` is Typer's own escape hatch for this case, and
it is evaluated last, so it wins over the CI detection. Because the constant is
bound at import time, setting it afterwards is a no-op — hence this lives in
``tests/__init__.py`` rather than ``tests/conftest.py``: Python guarantees a
package's ``__init__`` executes before *any* module inside it, ``conftest``
included.

Scope: this suppresses colour only for Typer's own help/usage/error rendering.
It stubs nothing and weakens no assertion — help text must still genuinely
contain a flag for a flag test to pass.
"""

import os

os.environ["_TYPER_FORCE_DISABLE_TERMINAL"] = "1"
