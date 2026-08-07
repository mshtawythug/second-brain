"""Smoke tests for the Typer CLI app.

Two layers, and the second exists because the first is provably not enough.

``--help`` renders from the decorator metadata without ever entering the command
body, so a name that only resolves at call time is invisible to it. On
2026-07-31 ``cli_search.py`` called an undefined ``_validate_sensitivity_choice``
from inside the ``search`` body: ``import brain.cli_search`` succeeded, every
import-based check passed (including ``tests/test_import_cycles.py``), ``brain
search --help`` rendered fine — and ``brain search`` NameErrored on *every*
invocation. The most-used command in the tool, entirely dead, with the tree
looking healthy.

So the layers are:

1. :func:`test_command_help_succeeds` — ``--help`` across every registered
   command and group, enumerated from the app rather than hard-coded so a new
   command is covered the moment it is added. Cheap, no database; catches
   decorator/signature-level breakage across the whole surface.
2. :func:`test_core_command_invocation_succeeds` — actually RUNS the core
   read commands against a seeded database. This is the layer that would have
   caught the 2026-07-31 defect.

:func:`test_smoke_would_fail_on_a_broken_command_body` guards the guard: it
proves ``--help`` passes on a broken command while invocation fails, so the
distinction above is asserted rather than merely asserted-about-in-a-docstring.
Credit: w2b identified both the gap and the precise boundary of what import
tests can prove.

What layer 2 does NOT cover, and why
------------------------------------

Layer 2 invokes 23 read-only commands, every one verified by probe to exit 0
against a seeded scratch database. The following are deliberately excluded, and
the exclusions are the honest boundary of this module rather than a to-do list:

* ``doctor`` — probes a live Ollama socket and trips the suite's
  ``LiveOllamaForbidden`` hermeticity guard. Covering it would mean weakening
  that guard or marking this test ``live_ollama``; neither is worth it for a
  smoke check.
* **Destructive or long-running:** ``ingest*``, ``reembed``, ``backup``,
  ``restore``, ``enrich``, ``audio``, ``init``, ``setup``, ``uninstall``,
  ``rm``, ``edit``, ``tag``, ``rate``, ``mark-*``, ``vault sync``,
  ``note new/move/rename``, ``graphrag build/refresh``, ``review scan``,
  ``connect refresh/accept/reject``, ``owner add/remove/set``.

**These are not covered by any layer here, and mocking them would not fix
that.** A test that mocks away the command body asserts nothing — it is exactly
the vacuous pass this module is built to prevent. A construction-only probe
(resolving the callback and its parameter defaults without executing the body)
would also not help: the failure mode in question resolves names *inside* the
body, so construction succeeds either way.

The live proof: on 2026-08-05 ``vault/sync.py:1268`` raised
``NameError: DEFAULT_SENSITIVITY`` mid-run — the third instance of this pattern
in one session. ``vault sync`` is in the excluded set above, so **neither layer
here would have caught it**, and neither would a construction-only probe.
Covering that class means really running those commands, which this module does
not do. Recording the boundary is worth more than pretending to cover it.
"""
from __future__ import annotations

from collections.abc import Callable

import psycopg
import pytest
import typer
from typer.testing import CliRunner

from brain.cli import app


def _command_names() -> list[str]:
    """Every top-level command and group name registered on the app.

    Enumerated from the Typer app, never hard-coded: a hard-coded list silently
    stops covering the surface the moment someone adds a command, which is the
    same "passes while asserting nothing" failure this module exists to prevent.
    """
    names = [
        cmd.name or cmd.callback.__name__.replace("_", "-")
        for cmd in app.registered_commands
        if cmd.callback is not None or cmd.name
    ]
    names += [grp.name for grp in app.registered_groups if grp.name]
    return sorted(names)


COMMAND_NAMES = _command_names()


def test_help_succeeds() -> None:
    result = CliRunner().invoke(app, ["--help"])
    assert result.exit_code == 0
    assert "brain" in result.output.lower() or "Usage" in result.output


def test_help_documents_krisp_ingestion() -> None:
    """`brain --help` must explain how to import Krisp calls.

    Krisp has no native CLI command — transcripts are pulled by Claude via
    the Krisp MCP and piped into `brain ingest-stdin`. The --help epilog
    is the only place a human-or-Claude reading the CLI surface can discover
    that flow, so guard it with a smoke test.
    """
    result = CliRunner().invoke(app, ["--help"])
    assert result.exit_code == 0
    output = result.output
    assert "Importing Krisp calls" in output
    assert "ingest-stdin" in output
    assert "--source krisp" in output
    assert "--external-id" in output


# --- layer 1: --help across the whole surface -------------------------------


def test_command_list_is_not_empty() -> None:
    """``COMMAND_NAMES`` actually enumerated something.

    If the introspection above silently returned ``[]``, the parametrized help
    test would collect zero cases and report green while checking nothing.
    """
    assert len(COMMAND_NAMES) > 20, (
        f"expected the full CLI surface, enumerated only {COMMAND_NAMES!r} — "
        "app introspection has probably broken against a new Typer version"
    )


@pytest.mark.parametrize("command", COMMAND_NAMES)
def test_command_help_succeeds(command: str) -> None:
    """``brain <command> --help`` exits 0 for every registered command/group.

    Catches decorator- and signature-level breakage across the whole surface.
    Deliberately NOT sufficient on its own — ``--help`` never enters the command
    body; see :func:`test_smoke_would_fail_on_a_broken_command_body`.
    """
    result = CliRunner().invoke(app, [command, "--help"])
    assert result.exit_code == 0, (
        f"`brain {command} --help` exited {result.exit_code}\n"
        f"exception: {result.exception!r}\n{result.output}"
    )


# --- layer 2: actually run the core read commands ---------------------------


def test_core_command_invocation_succeeds(
    test_db: psycopg.Connection,
    fake_embedder: object,
    patch_embedder: Callable[[object], None],
    seed_doc: Callable[..., str],
) -> None:
    """The core read commands RUN, not merely render help.

    This is the layer that catches a name resolving only at call time — the
    2026-07-31 ``_validate_sensitivity_choice`` defect. Each command is invoked
    against a seeded database with a real (fake-embedder) execution path.

    Every entry was verified by probe to exit 0 on a seeded scratch database;
    none is aspirational. See ``EXCLUDED_FROM_INVOCATION`` in the module
    docstring for what is deliberately not here and why.
    """
    patch_embedder(fake_embedder)
    doc_id = seed_doc(title="Smoke Doc", content="alpha beta gamma")
    short = doc_id[:8]

    invocations: list[list[str]] = [
        # core read path
        ["status"],
        ["list"],
        ["search", "alpha"],
        ["show", short],
        ["explain", "alpha"],
        # link / graph read paths
        ["backlinks", short],
        ["links", short],
        ["orphans"],
        ["graph"],
        # derived / analysis read paths
        ["people"],
        ["resurface"],
        ["todo"],
        ["timeline", "alpha"],
        # sub-app read paths — each group's non-mutating surface
        ["connect", "list"],
        ["connect", "stats"],
        ["review", "list"],
        ["review", "weekly"],
        ["graphrag", "stats"],
        ["graphrag", "entities"],
        ["graphrag", "search", "alpha"],
        ["elicit", "list"],
        ["capture", "list"],
        ["owner", "show"],
    ]
    failures: list[str] = []
    for argv in invocations:
        result = CliRunner().invoke(app, argv)
        if result.exit_code != 0:
            failures.append(
                f"`brain {' '.join(argv)}` exited {result.exit_code} "
                f"({result.exception!r})"
            )

    # Collect ALL failures rather than stopping at the first: a half-landed
    # refactor usually breaks several commands at once, and one traceback per
    # run turns that into several sequential debugging cycles.
    assert not failures, (
        "read-only command invocation(s) failed. A command that imports "
        "cleanly and renders --help can still fail on every real "
        "invocation.\n  " + "\n  ".join(failures)
    )


# --- the guard's own guard --------------------------------------------------


def test_smoke_would_fail_on_a_broken_command_body() -> None:
    """Prove the invocation layer catches what the help layer cannot.

    Without this, :func:`test_core_command_invocation_succeeds` could pass
    vacuously — a runner that swallowed body exceptions, or an ``exit_code``
    that stopped reflecting them, would leave it green forever while asserting
    nothing. So build a command with the exact 2026-07-31 shape (a name that
    resolves only when the body runs) and assert BOTH halves of the contract:

    * ``--help`` exits 0 — the help layer is genuinely blind to this class,
      which is *why* layer 2 has to exist;
    * invoking it exits non-zero and surfaces the ``NameError``.

    Uses a throwaway Typer app rather than a brain command: the real commands
    are (correctly) working, so they cannot exercise the failure path.
    """
    broken = typer.Typer()

    @broken.command()
    def search(query: str = "q") -> None:
        """Renders help fine; explodes the moment it runs."""
        _undefined_helper(query)  # type: ignore[name-defined]  # noqa: F821

    runner = CliRunner()

    help_result = runner.invoke(broken, ["--help"])
    assert help_result.exit_code == 0, (
        "the premise of this module is that --help passes on a body-broken "
        f"command; it exited {help_result.exit_code}"
    )

    run_result = runner.invoke(broken, [])
    assert run_result.exit_code != 0, (
        "CliRunner reported success for a command whose body raises NameError — "
        "the invocation smoke test above would pass vacuously"
    )
    assert isinstance(run_result.exception, NameError), (
        "expected the NameError to reach the result; got "
        f"{run_result.exception!r}"
    )
