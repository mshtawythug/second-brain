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

import brain.settings

# A field holding a secret must end in one of these (documented in
# brain/settings/__init__.py), so this guard can find it by name.
SECRET_FIELD_PATTERN = re.compile(r"(?:^|_)(?:token|secret|password|api_key|webhook_url)$")


def _settings_modules() -> list[ModuleType]:
    modules: list[ModuleType] = [brain.settings]
    for info in pkgutil.walk_packages(brain.settings.__path__, "brain.settings."):
        modules.append(importlib.import_module(info.name))
    return modules


# ---------------------------------------------------------------------------
# Guard 1: no leaf reads the process environment
# ---------------------------------------------------------------------------


def environ_reads(source: str) -> list[str]:
    """Return each ``os.environ`` / ``os.getenv`` / ``environ`` import found in ``source``."""
    found: list[str] = []
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.Attribute) and node.attr in {"environ", "getenv", "environb"}:
            found.append(f"line {node.lineno}: .{node.attr}")
        elif isinstance(node, ast.ImportFrom) and node.module == "os":
            for alias in node.names:
                if alias.name in {"environ", "getenv", "environb"}:
                    found.append(f"line {node.lineno}: from os import {alias.name}")
    return found


def test_no_settings_module_reads_the_process_environment() -> None:
    offenders = {
        module.__name__: reads
        for module in _settings_modules()
        if (reads := environ_reads(Path(inspect.getfile(module)).read_text()))
    }

    assert offenders == {}, (
        f"a settings leaf must take env: Mapping[str, str], never read os.environ: {offenders}"
    )


def test_environ_checker_flags_each_way_of_reading_the_environment() -> None:
    assert environ_reads("import os\nX = os.environ.get('A')\n")
    assert environ_reads("import os\nX = os.getenv('A')\n")
    assert environ_reads("from os import environ\n")
    assert environ_reads("from os import getenv as g\n")
    assert (
        environ_reads(
            "from collections.abc import Mapping\ndef f(env: Mapping[str, str]):\n"
            "    return env.get('A')\n"
        )
        == []
    )


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
    ):
        assert SECRET_FIELD_PATTERN.search(name), name
    for name in ("rerank_url", "model", "tokenizer", "depth", "secretary", "keyword"):
        assert not SECRET_FIELD_PATTERN.search(name), name
