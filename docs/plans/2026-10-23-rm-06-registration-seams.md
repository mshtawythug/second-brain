# RM-06 — Registration seams for new CLI commands and MCP tools

> **For agentic workers:** REQUIRED SUB-SKILL: Use team-driven-development to implement this plan task-by-task.

**Ticket:** RM-06 in [`docs/specs/2026-10-23-roadmap-completion-design.md`](../specs/2026-10-23-roadmap-completion-design.md)
(wave 2; G1; the smallest scope of backlog BL-2). The ticket is the spec; this is the task list.
**Base:** `second-brain/master` `b359ac0` (wave 1 merged).

## Goal

After this ticket, a new command module or MCP tool module is added without editing `cli.py` or
`mcp_server.py`. No existing command or tool changes name or behaviour, and none moves (moving is
RM-48).

## Files

| File | Change |
|---|---|
| `src/brain/cli_registry.py` | `Registrar` alias, `EXTENSION_REGISTRARS = ()`, `register_extensions(app)` (reads the tuple at call time); docstring states the "new command over a new flag" policy. |
| `src/brain/cli.py` | Import `register_extensions` in the existing `cli_registry` block; one call after the last registration (`gaps push`), with its inline reason; a module-docstring pointer (ninth growth). |
| `src/brain/mcp_tools/__init__.py` (new) | `register_extension_tools(app, ctx, *, modules=None)`, `discover_tool_modules()`, `_reset_contexts_for_tests(modules=None)`; the docstring lists the helpers every tool must use, the lens first. |
| `src/brain/mcp_tools/_context.py` (new) | `ToolContext` (frozen dataclass of the six helpers), `ContextSlot` (a module's bound context; refuses a rebind), `ToolRegistrationError`. |
| `src/brain/mcp_server.py` | Import; after the last tool, the `ToolContext(...)` construction and `register_extension_tools(mcp_app, ctx)` under `if __name__ != "__main__":`; the `__main__` block becomes `from brain.mcp_server import main as _main; _main()`; a module-docstring pointer (eleventh growth). |
| `tests/test_cli_registry_extensions.py` (new) | AC1. |
| `tests/test_mcp_tools_seam.py` (new) | AC2, AC3, the discovery/`modules=` filter, the no-`brain.mcp_server`-import rule. |
| `tests/mcp_extension_probe.py` (new) | A test-only tool module (the shape RM-12 copies): `register(app, ctx)` + `brain_probe_state`. |
| `tests/test_mcp_tool_registration.py` | AC4: the roster also covers every module-level `brain_*` function in `brain.mcp_tools.*`. |
| `tests/test_mcp_server_protocol.py` | AC5: stdio list equals the in-process list; a copy of the package with the probe planted proves the served module registers extensions exactly once and that the probe reads `main()`'s state. |
| `CLAUDE.md` | Ceiling rows for `cli.py` and `mcp_server.py`: promote each last descriptive hop, add this ticket's. |
| `CONTRIBUTING.md`, `CHANGELOG.md` | "Adding a command or an MCP tool"; Unreleased entry. |
| Spec Appendix B; backlog BL-2 | RM-06 → DONE (#PR); BL-2 partly addressed, the rest is RM-48. |

## Design notes beyond the ticket

- **Help order.** Typer lists every `@app.command` before every `add_typer` group, in registration
  order within each kind. So "extension commands last" means: an extension *command* lands after
  every built-in command, and an extension *sub-app* after every built-in sub-app. AC1's test pins
  both.
- **One rebind policy, not one per module.** Each tool module holds `_CONTEXT = ContextSlot(__name__)`
  as its module global; `register` calls `_CONTEXT.bind(ctx)`. The refusal and the test-only reset
  live in `ContextSlot`, so a later module cannot get them wrong. A bind with an *equal* context is
  a no-op (idempotent); a *different* one raises `ToolRegistrationError` unless
  `_reset_contexts_for_tests` ran first.
- **Discovery** imports every non-underscore module of `brain.mcp_tools` (`pkgutil.iter_modules`), in
  name order, and calls its `register`. A module without `register` raises, rather than being
  silently skipped.
- **Why the stdio test plants a probe.** With no extension tool shipped yet, the `__name__` guard and
  the `__main__` block cannot change the advertised list, so a list-equality test alone would pass
  under both mutations. The protocol test therefore copies the installed `brain` package into
  `tmp_path`, adds `mcp_tools/stdio_probe.py` (the probe's source), and runs
  `python -m brain.mcp_server` against the copy with `PYTHONPATH` pointing at it. No production
  module is modified and nothing is patched: it is a real tree with one more tool module in it.

## Tests (RED first)

1. AC1 — `test_extension_command_lists_after_every_builtin_command`, and the sub-app twin.
2. AC2 — `test_register_extension_tools_with_modules_registers_only_those`,
   `test_extension_tool_reads_the_state_installed_on_mcp_server`,
   `test_modules_filter_leaves_real_tool_modules_untouched`.
3. AC3 — `test_rebinding_a_different_context_raises`, `test_rebinding_after_reset_is_allowed`,
   `test_rebinding_the_same_context_is_a_no_op`.
4. AC4 — `test_every_brain_tool_function_is_registered` over both rosters.
5. AC5 — `test_stdio_tool_list_equals_in_process_list`,
   `test_served_module_registers_extensions_once_and_reads_main_state`.
6. AC6 — the existing `test_importing_brain_cli_does_not_pull_starlette_or_uvicorn`, unchanged.
7. Behaviour unchanged — the CLI command tree and MCP tool list are dumped at `b359ac0` and at the
   branch head and diffed (evidence in the PR).

## Mutation proofs (git-archive copy only)

The rebind refusal; the `__name__` guard; the `modules=` filter; the `__main__` block; the
`EXTENSION_REGISTRARS` read; the `mcp_tools` roster in the registration guard.

## Risks

- **An extension tool forgetting the confidential lens.** `mcp_tools/__init__.py`'s docstring says
  every retrieval tool uses `ctx.confidential_lens`; each tool ticket names its lens and pins it.
- **The double import under `python -m`** runs the module body twice (two `make_server` calls). The
  second FastMCP's logging setup is a no-op because the root logger already has its handler; the
  `__main__` copy's app is never served.
- **Ceiling.** `cli.py` and `mcp_server.py` grow, recorded inline and in each module docstring.
