"""Structural guards over every module in ``brain.settings`` (RM-08, roadmap §2.3).

Two rules every feature settings leaf must keep, checked for every module the
package holds now and every leaf a later ticket adds:

1. **A leaf never reads the process environment itself.** It takes ``env:
   Mapping[str, str]``; the caller passes ``os.environ`` after ``Config.load()``,
   and tests pass a plain dict. A leaf that reached for ``os.environ`` would let
   a variable exported in a developer's shell change a test's result.
2. **A secret-named dataclass field is ``repr=False``**, so a settings object
   that is logged or printed in a traceback does not carry the secret.

Today the package holds no leaf, so each checker is also run against a
synthetic violation: a guard that cannot fail proves nothing.
"""

from __future__ import annotations

import ast
import dataclasses
import importlib
import inspect
import pkgutil
import re
import subprocess
import sys
from pathlib import Path
from types import ModuleType

import pytest

import brain.settings

pytestmark = pytest.mark.nodb

# A field holding a secret must end in one of these (documented in
# brain/settings/__init__.py), so this guard can find it by name. Case-insensitive:
# ruff enforces no naming rule here, so ``API_KEY`` is as legal a field name as
# ``api_key``.
SECRET_FIELD_PATTERN = re.compile(
    r"(?:^|_)(?:token|secret|password|api_key|secret_key|private_key|webhook_url)$",
    re.IGNORECASE,
)


def _settings_modules() -> list[ModuleType]:
    modules: list[ModuleType] = [brain.settings]
    for info in pkgutil.walk_packages(brain.settings.__path__, "brain.settings."):
        modules.append(importlib.import_module(info.name))
    return modules


# ---------------------------------------------------------------------------
# Guard 1: no leaf reads the process environment
# ---------------------------------------------------------------------------


# A settings leaf needs nothing from these modules (paths come from pathlib), so
# importing any of them is refused outright: ``from os import *`` and
# ``from posix import environ`` reach the environment without an ``os.`` prefix,
# and ``dotenv`` (a shipped dependency) would read ``.env`` files around the
# chain ``Config.load()`` resolves, and write into ``os.environ``. Relative
# imports are refused too: a sibling module named ``os`` is not worth a carve-out.
_BANNED_ROOT_MODULES = frozenset({"os", "posix", "nt", "dotenv"})
_ENV_NAMES = frozenset({"environ", "environb", "getenv", "getenvb"})


def environ_reads(source: str) -> list[str]:
    """Return each way ``source`` could reach the process environment.

    Four rules, each catching what the others miss:

    1. any import of ``os``, ``posix``, ``nt`` or ``dotenv`` (including
       ``from os import *``, and relative imports of a module of those names);
    2. attribute access to ``environ`` / ``environb`` / ``getenv`` / ``getenvb``
       on anything;
    3. one of those names as a string constant (``getattr(m, "environ")``,
       ``vars(m)["environ"]``);
    4. a dynamic import of a banned module (``__import__("os")``,
       ``importlib.import_module("posix")``).
    """
    found: list[str] = []
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name.split(".")[0] in _BANNED_ROOT_MODULES:
                    found.append(f"line {node.lineno}: import {alias.name}")
        elif isinstance(node, ast.ImportFrom):
            root = (node.module or "").split(".")[0]
            if root in _BANNED_ROOT_MODULES:
                found.append(f"line {node.lineno}: from {node.module} import ...")
        elif isinstance(node, ast.Attribute) and node.attr in _ENV_NAMES:
            found.append(f"line {node.lineno}: .{node.attr}")
        elif isinstance(node, ast.Constant) and node.value in _ENV_NAMES:
            found.append(f"line {node.lineno}: string {node.value!r}")
        elif isinstance(node, ast.Call) and _is_dynamic_banned_import(node):
            found.append(f"line {node.lineno}: dynamic import")
    return found


def _is_dynamic_banned_import(call: ast.Call) -> bool:
    func = call.func
    is_importer = (isinstance(func, ast.Name) and func.id == "__import__") or (
        isinstance(func, ast.Attribute) and func.attr == "import_module"
    )
    if not (is_importer and call.args):
        return False
    first = call.args[0]
    return (
        isinstance(first, ast.Constant)
        and isinstance(first.value, str)
        and first.value.split(".")[0] in _BANNED_ROOT_MODULES
    )


def test_no_settings_module_reads_the_process_environment() -> None:
    offenders = {
        module.__name__: reads
        for module in _settings_modules()
        if (reads := environ_reads(Path(inspect.getfile(module)).read_text()))
    }

    assert offenders == {}, (
        f"a settings leaf must take env: Mapping[str, str], never read os.environ: {offenders}"
    )


# Each case is reachable by exactly one rule of ``environ_reads``, so removing
# a rule turns its cases red. None imports ``os`` unless the case is about that.
ENVIRONMENT_READS = {
    "import_os": "import os\n",
    "import_os_path": "import os.path\n",
    "from_os_import_environ": "from os import environ\n",
    "from_os_import_star": "from os import *\nX = environ\n",
    "from_posix_import_environ": "from posix import environ\n",
    "import_nt": "import nt\n",
    "relative_from_os": "from .os import thing\n",
    "import_dotenv": "import dotenv\n",
    "from_dotenv_import_load_dotenv": "from dotenv import load_dotenv\n",
    "attribute_environ": "X = module.environ.get('A')\n",
    "attribute_getenv": "X = module.getenv('A')\n",
    "attribute_getenvb": "X = module.getenvb(b'A')\n",
    "attribute_environb": "X = module.environb\n",
    "getattr_string": "X = getattr(module, 'environ')\n",
    "vars_subscript": "X = vars(module)['environ']\n",
    "dunder_import": "X = __import__('os')\n",
    "import_module": "X = importlib.import_module('posix')\n",
}


@pytest.mark.parametrize("case", sorted(ENVIRONMENT_READS))
def test_environ_checker_flags_each_way_of_reading_the_environment(case: str) -> None:
    assert environ_reads(ENVIRONMENT_READS[case]), case


def test_environ_checker_accepts_a_leaf_that_reads_only_its_env_argument() -> None:
    source = (
        "from collections.abc import Mapping\n"
        "from pathlib import Path\n"
        "from .rerank_defaults import DEFAULT_MODEL\n"
        "def f(env: Mapping[str, str]) -> str | None:\n"
        "    return env.get('BRAIN_X')\n"
    )

    assert environ_reads(source) == []


def test_config_does_not_import_settings() -> None:
    """``brain.settings`` imports ``brain.config``; the reverse would be a cycle."""
    code = "import sys, brain.config; print('brain.settings' in sys.modules)"
    result = subprocess.run(
        [sys.executable, "-c", code], capture_output=True, text=True, timeout=60, check=True
    )

    assert result.stdout.strip() == "False"


# ---------------------------------------------------------------------------
# Guard 2: secret-named fields are hidden from repr
# ---------------------------------------------------------------------------


def secrets_shown_in_repr(module: ModuleType) -> list[str]:
    """Return ``Class.field`` for each secret-named dataclass field that ``repr`` would show."""
    shown: list[str] = []
    for name, obj in vars(module).items():
        if not (inspect.isclass(obj) and dataclasses.is_dataclass(obj)):
            continue
        if obj.__module__ != module.__name__:
            continue
        for field in dataclasses.fields(obj):
            if SECRET_FIELD_PATTERN.search(field.name) and field.repr:
                shown.append(f"{name}.{field.name}")
    return shown


def test_secret_named_fields_are_hidden_from_repr() -> None:
    offenders = {
        module.__name__: shown
        for module in _settings_modules()
        if (shown := secrets_shown_in_repr(module))
    }

    assert offenders == {}, f"secret-named settings fields must be repr=False: {offenders}"


def _synthetic_leaf(*, hide_secret: bool) -> ModuleType:
    """A stand-in leaf module holding one secret-named field and one ordinary one."""
    module = ModuleType("synthetic_leaf")

    @dataclasses.dataclass(frozen=True)
    class DeliverySettings:
        channel: str
        slack_webhook_url: str | None = dataclasses.field(default=None, repr=not hide_secret)

    DeliverySettings.__module__ = module.__name__
    module.DeliverySettings = DeliverySettings  # type: ignore[attr-defined]
    return module


def test_secret_checker_flags_a_secret_shown_in_repr() -> None:
    assert secrets_shown_in_repr(_synthetic_leaf(hide_secret=False)) == [
        "DeliverySettings.slack_webhook_url"
    ]


def test_secret_checker_accepts_a_hidden_secret_and_the_value_stays_out_of_repr() -> None:
    module = _synthetic_leaf(hide_secret=True)
    settings = module.DeliverySettings(  # type: ignore[attr-defined]
        channel="slack", slack_webhook_url="https://hooks.example.invalid/SECRET-PATH"
    )

    assert secrets_shown_in_repr(module) == []
    assert "SECRET-PATH" not in repr(settings)


def test_secret_field_pattern_matches_the_documented_suffixes() -> None:
    for name in (
        "api_token",
        "client_secret",
        "password",
        "voyage_api_key",
        "webhook_url",
        "slack_webhook_url",
        "token",
        "API_KEY",
        "Slack_Webhook_URL",
        "secret_key",
        "private_key",
    ):
        assert SECRET_FIELD_PATTERN.search(name), name
    for name in ("rerank_url", "model", "tokenizer", "depth", "secretary", "keyword"):
        assert not SECRET_FIELD_PATTERN.search(name), name
