# RM-08 — Feature settings leaves

> **For agentic workers:** REQUIRED SUB-SKILL: Use team-driven-development to implement this plan task-by-task.

**Ticket:** RM-08 in [`docs/specs/2026-10-23-roadmap-completion-design.md`](../specs/2026-10-23-roadmap-completion-design.md)
(wave 2; G3; policy in §2.3). The ticket is the spec; this is the task list.
**Base:** `second-brain/master` `a2f22b0` (RM-01..RM-07 merged).

## Goal

The knobs this program adds never grow `config.py`. They live in feature settings leaves under
`src/brain/settings/`, parsed by shared helpers that read a plain `Mapping[str, str]`: in
production that is `os.environ` after `Config.load()` has published the dotenv chain into it, and
in tests it is a dict. No existing knob moves, and nothing in `config.py` changes.

## Files

| File | Change |
|---|---|
| `src/brain/settings/__init__.py` (new) | Docstring stating the policy (§2.3): one leaf per feature, a frozen dataclass plus `load_<feature>_settings(env)`, `env` is `os.environ` after `Config.load()`, secrets are `field(repr=False)`, leaves never read `os.environ` themselves. No code. |
| `src/brain/settings/_env.py` (new) | `env_bool`, `env_choice`, `env_int`, `env_float` (§2.3), plus `env_str`. Every refusal is a `brain.config.ConfigError` naming the variable and never the value, raised `from None` so no chained parser error carries the value either. |
| `tests/test_settings_env.py` (new) | AC1, AC2 and AC4 for every parser: pure, dict-driven. |
| `tests/test_settings_dotenv.py` (new) | AC3: a `tmp_path` `.env` through `tests/conftest.py`'s `isolate_config_dotenv`; `tests/env_isolation.py` restores `os.environ` afterwards. |
| `tests/test_settings_leaf_guards.py` (new) | Two structural guards over every module in `brain.settings`: no leaf reads the process environment itself, and every secret-named dataclass field is `repr=False`. Each guard's checker is also run against a synthetic violation, so it is shown to be able to fail while the package has no leaf yet. |
| `docs/configuration.md` | New section "Adding a knob: feature settings leaves": same `.env` chain as every other knob, how a leaf is written, why `config.py` is not grown. |
| `CONTRIBUTING.md` | One line for `settings/` in "Codebase layout". |
| `CHANGELOG.md` | Unreleased entry. |
| Spec Appendix B | Follow-up commit after the PR opens: RM-08 → `DONE (#PR)`. |

`config.py` is not touched, so its ceiling row is not touched either (no promotion is owed: the
promotion rule applies "on touching a file in this table"). `src/brain/settings/` is two new
modules, both far under 800.

## Parser contract (`_env.py`)

Each parser takes `(env, name, default, ...)`:

- **Unset or blank** (`""` or whitespace only) → `default`. This matches every `BRAIN_*` knob in
  `config.py` (`raw is None or raw.strip() == ""`).
- **Values are stripped** before parsing.
- **`env_bool`**: `1/true/yes/on` or `0/false/no/off`, case-insensitive. The truthy set is
  `config._TRUTHY_ENV_VALUES` and the falsy set `config._GRAPH_ENABLED_FALSY`, imported rather than
  restated, so a leaf flag and a `config.py` flag agree on what "true" is.
- **`env_choice`**: compared case-insensitively against `choices` (which must be lowercase) and
  returned lowercased. A `default` outside `choices` is a programming error and raises `ValueError`
  at the call, not `ConfigError`. The refusal lists the choices, sorted.
- **`env_int` / `env_float`**: `minimum` is required and inclusive; `maximum` is an optional,
  keyword-only, inclusive bound (RM-14a's `BRAIN_RERANK_DEPTH` is 2 to 50). `env_float` refuses
  `nan` and `inf`: `float()` accepts them, and `nan` passes every `<` comparison, so a bound check
  alone would admit it.
- **`env_str`**: the stripped value, or `default` (which may be `None`). It never refuses; a leaf
  validates the shape itself (RM-14a's loopback URL, RM-16's webhook prefix) and raises
  `ConfigError` naming the variable only.
- **Refusals** say what the variable must be (`BRAIN_X must be an integer in [2, 50]`) and never
  quote the value, unlike `config.py`'s `(got {raw!r})`, because a leaf may hold a secret. They are
  raised `from None`: `int("…")`'s own `ValueError` quotes its input, and a chained cause is printed
  in a traceback.

## Deviations from the ticket

The spec text is left as written (§0); these are recorded here and in the PR body.

1. **`env_str` is added** beside the four parsers §2.3 lists. Every later leaf the spec names reads
   at least one string (`BRAIN_RERANK_URL`, `BRAIN_RERANK_MODEL`, `BRAIN_OCR_LANG`,
   `BRAIN_WHISPER_MODEL`, `BRAIN_BRIEF_SLACK_WEBHOOK_URL`), and without a shared helper each would
   restate the unset-or-blank rule.
2. **`env_int` and `env_float` take an optional keyword-only `maximum`.** §2.3's signatures carry
   `minimum` only, and RM-14a needs an upper bound. Calls written to §2.3's signatures are
   unaffected.
3. **Two guard tests** that §2.3 implies but does not list: leaves never read the process
   environment (§2.3: "Tests pass a plain dict, so no test touches the real environment"), and
   secret-named fields are `repr=False` (§2.3, the ticket's security line). The secret guard keys on
   field names ending in `_token`, `_secret`, `_password`, `_api_key` or `_webhook_url`; a leaf that
   holds a secret under another name must use one of those suffixes.

## Acceptance criteria → tests

| AC | Test |
|---|---|
| 1. Each parser accepts valid values and refuses invalid ones with the variable named | `test_settings_env.py`: `test_env_bool_accepts_every_spelling`, `test_env_bool_refuses_other_values`, `test_env_choice_*`, `test_env_int_*`, `test_env_float_*`, `test_env_str_*` |
| 2. An unset variable yields the default | `test_settings_env.py`: `test_unset_or_blank_yields_default` (parametrized over every parser, unset and blank) |
| 3. A value set in a `.env` file is honoured after `Config.load()` | `test_settings_dotenv.py`: `test_dotenv_value_is_honoured_after_config_load`, `test_process_env_still_wins_over_dotenv` |
| 4. A secret never appears in `repr()` or an error | `test_settings_env.py`: `test_refusal_never_echoes_the_value` (every parser, with a secret-shaped value, checks `str`, `args` and that no cause or context is attached); `test_settings_leaf_guards.py`: `test_secret_named_fields_are_hidden_from_repr` and its checker's synthetic failure |

## Mutation proofs (in a `git archive` copy only)

At least four, each shown to turn a named test red:

1. `env_float` without the finiteness check → `nan` / `inf` tests fail.
2. `env_int` without the `maximum` check → the upper-bound test fails.
3. `raise … from None` → `from exc` → `test_refusal_never_echoes_the_value` fails.
4. Blank no longer treated as unset → `test_unset_or_blank_yields_default` fails.
5. `env_choice` without lowercasing → the case-insensitivity test fails.
6. The secret guard's suffix match removed → its synthetic-failure test fails.
7. A refusal that quotes the value → `test_refusal_never_echoes_the_value` fails.
8. The environment checker missing `os.getenv` → its synthetic-failure test fails.
9. `config._TRUTHY_ENV_VALUES` gains `enabled` → `test_env_bool_refuses_other_values[enabled]`
   fails, which shows that the leaf follows `config.py`'s set.
10. `config._GRAPH_ENABLED_FALSY` gains `n` → `test_env_bool_refuses_other_values[n]` fails.
11. The leaf restates the truthy set as a literal → `test_env_bool_agrees_with_config_on_spellings`
    fails.

All eleven were killed (coordinator approved the plan and the three deviations on 2026-10-23).

## Risks

- **None to existing behaviour**: no existing module imports `brain.settings`, and `config.py` is
  unchanged.
- **Import cycle**: `_env.py` imports `brain.config`; `config.py` must never import
  `brain.settings`. The no-environ guard also asserts that `brain.config` does not import it.
