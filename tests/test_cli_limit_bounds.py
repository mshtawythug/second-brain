"""Every ``-n``/``--limit`` option in the CLI carries a minimum (F29, 0.4.0 QA).

QA found ``graphrag communities list -n -1`` and ``review list -n -1`` sending a negative
``LIMIT`` to Postgres (an ``InvalidRowCountInLimitClause`` traceback), and ``graphrag
search``/``themes``/``entities -n -1`` accepted silently. The fix is a Typer ``min`` on every
such option. A per-option fix only stays fixed if the NEXT option is held to it too, so the
first test walks every registered command and group callback, instead of naming them.

Pure: Typer refuses an out-of-range value before any command body runs, so no test here
touches a database.
"""
from __future__ import annotations

from collections.abc import Iterator
from typing import Any

import pytest
import typer
from typer.testing import CliRunner

from brain.cli import app

#: Options where 0 has a DOCUMENTED meaning. Their minimum must be exactly 0, so 0 keeps
#: working and a negative value is still refused.
ZERO_MEANS: dict[str, str] = {
    "backfill scan-secrets": "--limit 0 = no limit (help text)",
    "elicit list": "-n 0 = BRAIN_ELICIT_QUEUE_LIMIT (help text, docs/cli-reference.md)",
    "graphrag entity": "-n 0 = every neighbour (help text, docs/graphrag.md)",
    "graphrag entities": "-n 0 = every entity (help text, docs/cli-reference.md)",
}

#: Commands whose limit options another PR owns. Each is still held to min >= 1 once it
#: has a minimum, so the test is right in either merge order.
OWNED_ELSEWHERE: dict[str, str] = {}


_ISOLATED_ENV = {
    "COLUMNS": "300",
    "DATABASE_URL": "postgresql://brain:brain@localhost:1/none_test",
    "OLLAMA_HOST": "http://127.0.0.1:1",
}


def _walk(cmd: Any, path: tuple[str, ...]) -> Iterator[tuple[str, Any]]:
    # A group's own callback can carry options (`brain gaps -n`, `brain connect -n`).
    yield " ".join(path), cmd
    for name, sub in sorted(getattr(cmd, "commands", {}).items()):
        yield from _walk(sub, (*path, name))


def _is_limit_option(param: Any) -> bool:
    opts = [*param.opts, *param.secondary_opts]
    return param.param_type_name == "option" and (
        "-n" in opts or any(o.startswith("--") and "limit" in o for o in opts)
    )


def _limit_options() -> list[tuple[str, Any]]:
    root = typer.main.get_command(app)
    return [
        (path, param)
        for path, cmd in _walk(root, ())
        for param in cmd.params
        if _is_limit_option(param)
    ]


def test_every_limit_option_has_a_minimum() -> None:
    found = _limit_options()
    assert len(found) > 20, "the walk found too few limit options to be the real CLI"
    problems: list[str] = []
    for path, param in found:
        label = f"brain {path} {'/'.join(param.opts)}"
        minimum = getattr(param.type, "min", None)
        # Typer 0.27 vendors click as ``typer._click``, where these are named "int" and
        # "int range" (measured); upstream Click says "integer" / "integer range". The
        # prefix accepts both.
        if not param.type.name.startswith("int"):
            problems.append(f"{label}: not an int option ({param.type.name})")
        elif path in OWNED_ELSEWHERE and minimum is None:
            continue
        elif path in ZERO_MEANS:
            if minimum != 0:
                problems.append(f"{label}: 0 is documented, so min must be 0 (got {minimum})")
        elif minimum is None or minimum < 1:
            problems.append(f"{label}: needs min=1 (got {minimum})")
    assert not problems, "\n".join(problems)


def test_the_allowlists_name_only_live_commands() -> None:
    """A stale entry would quietly exempt nothing, and hide a removed command's twin."""
    paths = {path for path, _ in _limit_options()}
    stale = sorted((set(ZERO_MEANS) | set(OWNED_ELSEWHERE)) - paths)
    assert not stale, f"allowlist entries with no -n/--limit option: {stale}"


def test_an_owned_elsewhere_entry_expires_once_its_owner_bounds_it() -> None:
    """The exemption is temporary: when the owning PR lands its ``min``, the entry must go.

    Whichever of the two PRs merges second turns this red on its master merge, and the
    fix is to delete the entry.
    """
    options: dict[str, list[Any]] = {}
    for path, param in _limit_options():
        options.setdefault(path, []).append(param)
    stale = [
        f"stale OWNED_ELSEWHERE entry {path!r}: it is bounded now, remove it"
        for path in OWNED_ELSEWHERE
        if options.get(path)
        and all((getattr(p.type, "min", None) or 0) >= 1 for p in options[path])
    ]
    assert not stale, "\n".join(stale)


@pytest.mark.parametrize(
    "argv",
    [
        # The QA cases (B21, B28, B13) first.
        ["graphrag", "communities", "list", "-n", "-1"],
        ["review", "list", "-n", "-1"],
        ["graphrag", "search", "synthetic query", "-n", "-1"],
        ["graphrag", "themes", "--person", "Synthetic Person", "-n", "-1"],
        ["graphrag", "entities", "-n", "-1"],
        ["todo", "-n", "0"],
        # Every other option this fix bounded.
        ["graphrag", "build", "-n", "0"],
        ["graphrag", "communities", "build", "-n", "0"],
        ["graphrag", "communities", "refresh", "-n", "0"],
        ["timeline", "synthetic query", "-n", "0"],
        ["enrich", "--backfill", "-n", "0"],
        ["review", "scan", "--limit", "0"],
        ["resurface", "-n", "0"],
        ["vault", "sync-summaries", "--limit", "0"],
        ["elicit", "list", "-n", "-1"],
        ["gaps", "-n", "-1"],
        ["capture", "review", "-n", "-1"],
        ["connect", "-n", "-1"],
        ["connect", "list", "-n", "-1"],
    ],
)
def test_an_out_of_range_limit_is_a_usage_error(argv: list[str]) -> None:
    # A wide terminal so Rich's error box cannot wrap the message mid-phrase. The
    # database and Ollama are unreachable on purpose: the refusal must come before
    # either is touched, and a regression fails here instead of reaching them.
    result = CliRunner().invoke(app, argv, env=_ISOLATED_ENV)

    assert result.exit_code == 2, result.output
    assert "is not in the range" in result.output
    assert "Traceback" not in result.output
