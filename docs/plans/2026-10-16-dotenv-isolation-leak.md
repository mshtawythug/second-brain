# Dotenv isolation leak — implementation plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use team-driven-development to implement this plan task-by-task.

**Goal:** a test's dotenv values that `Config.load` publishes into `os.environ`
can no longer reach another test. The inlined `_project_dotenv` /
`_brain_home_dotenv` isolation blocks are migrated to `isolate_config_dotenv`
wherever the conversion is exact.

**Spec:** `docs/specs/2026-10-16-dotenv-isolation-leak-design.md`. It has the
root cause, the decision against changing production, and the per-module
migration table.

**Test-only.** No `src/` change and no CHANGELOG entry.

## Tasks

1. **Red.** Reproduce at base:
   `pytest --no-cov tests/test_config_dotenv_chain.py tests/test_cli_doctor.py`
   gives 7 failures. Bisect to the single leaking test.
2. **Regression tests first** (`tests/test_env_isolation.py`): the mechanism
   test, the `pytester` boundary pair with its no-plugin negative control, and
   the wiring test. They are red while `tests/env_isolation.py` is missing.
3. **Guard** (`tests/env_isolation.py`): `restored_environ()` and the autouse
   `_restore_os_environ`. Register it in `tests/conftest.py`'s `pytest_plugins`,
   with an inline why-comment and a docstring pointer, because conftest is over
   the ceiling.
4. **Helper:** add `brain_home_root=` to `isolate_config_dotenv`, and update
   its docstring.
5. **Migrate** the 13 sites the spec table marks "migrated". Leave the two
   chain sites inline.
6. **Mutation proofs, five clauses each** (`PYTHONDONTWRITEBYTECODE=1`):
   * M1: `restored_environ` restores nothing, so the mechanism and boundary
     tests go red.
   * M2: the `pytest_plugins` entry is removed, so the wiring test goes red and
     the bad-order run goes red again.
   * M3: the guard skips keys that were deleted or changed, not just added
     ones, so the mechanism test goes red on the restore assertions.
   * M4: the isolation is removed from one migrated module while a `.env` is
     planted at the project root, so that module goes red on the planted value.
7. **Verify:** `ruff check`, `mypy src/`, every touched test file, the
   bad-order run, the reverse order, and three or more explicit permutations of
   the config/dotenv/doctor/cli files. Do not run the full suite; GitHub CI is
   the full-suite evidence.
8. Commit without pushing, and report to team-lead.
