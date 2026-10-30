"""The G1 log-privacy guard over ``src/brain``, and its self-tests (G2-G3, T1-T3).

No logger call in ``src/brain`` may carry a caught exception's MESSAGE, which can
quote document content; ``tests/test_log_privacy.py`` says why and reproduces the
leaks. The rules live in ``tests/log_privacy_rules.py``.

- **G1** is the guard: an AST scan of ``src/brain``. A traceback (``.exception`` or
  a truthy ``exc_info``) is allowed only at the sites in ``TRACEBACK_ALLOWLIST``,
  each one with its reason.
- **G2/G3** prove the scan can fail, and what it lets through.
- **T1-T3** do the same for the three blind spots PR #58 recorded as not covered
  (RM-28): an alias logged after its handler, an attribute store, and a nested
  ``def`` or ``lambda``. Plan: ``docs/plans/2026-10-30-rm-28-log-privacy-guard.md``.

Pure AST work: marked ``nodb``, so a run of this file alone opens no database
connection and takes no suite lock. All data is synthetic.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from tests.log_privacy_rules import RAW_ALLOWLIST, STORE_ALLOWLIST, log_privacy_violations

pytestmark = pytest.mark.nodb

_SRC = Path(__file__).resolve().parent.parent / "src" / "brain"


def test_no_src_brain_logger_call_carries_an_exception_message() -> None:
    """G1: the guard over every module in ``src/brain``."""
    hits = [
        hit
        for path in sorted(_SRC.rglob("*.py"))
        for hit in log_privacy_violations(path.read_text(), path.relative_to(_SRC).as_posix())
    ]

    assert hits == [], f"logger calls that can carry an exception's text: {hits}"


def _handler(body: str) -> str:
    """``body`` (one statement per line) inside ``def f(): try/except OSError as e:``."""
    lines = "".join(f"        {line}\n" for line in body.splitlines())
    return f"def f():\n    try:\n        pass\n    except OSError as e:\n{lines}"


@pytest.mark.parametrize(
    "body",
    [
        "logger.warning('x %s', e)",
        "_logger.error('x %s', str(e))",
        "logger.warning(f'x {e}')",
        "logger.warning('x %s' % e)",
        "logger.warning('x {}'.format(e))",
        "m = f'x {e}'\nlogger.warning('%s', m)",
        "self._logger.warning('x %s', e)",
        "logger.exception('not on the allowlist')",
        "logger.error('x', exc_info=True)",
        "logger.log(30, 'x %s', e)",
    ],
    ids=[
        "bare",
        "nested",
        "fstring",
        "percent",
        "format",
        "alias",
        "attr-receiver",
        "exception",
        "exc-info",
        "log-level",
    ],
)
def test_the_guard_flags_every_form_that_carries_the_message(body: str) -> None:
    """G2: the scan can fail, on each way an exception's text reaches a log line."""
    assert log_privacy_violations(_handler(body)) != [], f"the guard missed: {body!r}"


def test_the_guard_flags_a_helper_that_logs_the_exception_it_is_handed() -> None:
    """G2: no handler binds ``exc`` here; its annotation says what it is."""
    source = "def _warn(exc: OSError) -> None:\n    logger.warning('x %s', exc)\n"

    assert log_privacy_violations(source) != [], "the guard missed an exception parameter"


@pytest.mark.parametrize(
    "body",
    [
        "logger.warning('x %s', describe_failure(e))",
        "logger.warning('x %s', errors.describe_failure(e))",
        "logger.warning('x %s', type(e).__name__)",
        "logger.warning(f'x {describe_failure(e)}')",
        "logger.error('x', exc_info=False)",
        "logger.warning('x %s', describe_failure(e) if e is not None else 'none')",
    ],
    ids=[
        "describe",
        "describe-attr",
        "type-name",
        "fstring-describe",
        "exc-info-false",
        "conditional",
    ],
)
def test_the_guard_accepts_content_free_forms(body: str) -> None:
    """G3: what the guard lets through."""
    assert log_privacy_violations(_handler(body)) == []


def test_the_guard_ignores_names_outside_a_handler_and_honours_the_allowlist() -> None:
    """G3: an unbound ``e`` is not an exception, and an allowlisted traceback passes."""
    source = (
        "e = 1\nlogger.warning('x %s', e)\n"
        "def _worker_loop():\n"
        "    logger.exception('vault watcher: worker failed on %s %s', 1, 2)\n"
    )

    assert log_privacy_violations(source, "vault/watch.py") == []
    assert log_privacy_violations(source, "elsewhere.py") != [], (
        "the allowlist is not keyed by module"
    )


def test_the_raw_allowlist_is_keyed_by_module_function_and_message() -> None:
    """G3: an allowlisted derived value passes only at its own site."""
    ((module, func, message),) = RAW_ALLOWLIST
    source = (
        f"def {func}():\n    try:\n        pass\n    except OSError as exc:\n"
        f"        missing = f(exc)\n        logger.warning({message!r}, missing, 1)\n"
    )

    assert log_privacy_violations(source, module) == []
    assert log_privacy_violations(source, "elsewhere.py") != [], (
        "the raw allowlist is not keyed by module"
    )


def _function(body: str) -> str:
    """``body`` (one statement per line, 4-space relative indents) as the body of ``def f():``."""
    return "def f():\n" + "".join(f"    {line}\n" for line in body.splitlines())


_TRY = "try:\n    pass\nexcept OSError as e:\n"


@pytest.mark.parametrize(
    "body",
    [
        _TRY + "    m = str(e)\nlogger.warning('x %s', m)",
        _TRY + "    m = f'x {e}'\nfull = 'y ' + m\nlogger.warning(full)",
        "m = None\nfor _ in range(2):\n    logger.warning('x %s', m)\n    "
        + _TRY.replace("\n", "\n    ").rstrip()
        + "\n        m = e.args",
    ],
    ids=["after-handler", "chained-after-handler", "loop-above-handler"],
)
def test_t1_flags_an_alias_logged_after_its_handler(body: str) -> None:
    """T1: a name assigned from ``e`` in the handler stays tainted for the whole function."""
    assert log_privacy_violations(_function(body)) != [], f"T1 missed: {body!r}"


@pytest.mark.parametrize(
    "body",
    [
        _TRY + "    m = describe_failure(e)\nlogger.warning('x %s', m)",
        _TRY + "    pass\nfor e in ('a', 'b'):\n    logger.info('x %s', e)",
    ],
    ids=["describe-alias", "name-reused-after-handler"],
)
def test_t1_spares_a_class_only_alias_and_a_reused_handler_name(body: str) -> None:
    """T1: a class-only alias is clean, and ``e`` itself is not carried out of the handler."""
    assert log_privacy_violations(_function(body)) == []


@pytest.mark.parametrize(
    "body",
    [
        _TRY + "    self.last_error = e",
        _TRY + "    obj.detail = str(e)",
        _TRY + "    self.code, self.detail = 1, e",
        _TRY + "    self.detail: str = repr(e)",
        _TRY + "    m = e.args\nself.detail = m",
    ],
    ids=["bare", "str", "tuple-unpack", "annotated", "alias-after-handler"],
)
def test_t2_flags_an_exception_stored_on_an_attribute(body: str) -> None:
    """T2: an attribute can be logged anywhere, so the store itself is the leak."""
    assert log_privacy_violations(_function(body)) != [], f"T2 missed: {body!r}"


def test_t2_flags_an_exception_parameter_stored_on_an_attribute() -> None:
    """T2: no handler binds ``exc`` here; its annotation says what it is."""
    source = "def keep(self, exc: OSError) -> None:\n    self.last = exc\n"

    assert log_privacy_violations(source) != [], "T2 missed an exception parameter"


@pytest.mark.parametrize(
    "body",
    [
        _TRY + "    self.last = describe_failure(e)",
        _TRY + "    self.kind = type(e).__name__",
        _TRY + "    self.failed = True",
        _TRY + "    self.errno_matches = e.errno == 2",
    ],
    ids=["describe", "type-name", "constant", "comparison"],
)
def test_t2_spares_a_class_only_store(body: str) -> None:
    """T2: a store that goes through a content-free call, or holds no exception, passes."""
    assert log_privacy_violations(_function(body)) == []


def test_t2_store_allowlist_is_keyed_by_module_function_and_target() -> None:
    """T2: the allowlisted store passes only at its own site, each entry with a reason."""
    ((module, func, target),) = STORE_ALLOWLIST
    source = f"def {func}(self, job, error: JobError) -> None:\n    {target} = error\n"

    assert all(reason for reason in STORE_ALLOWLIST.values()), "an entry has no reason"
    assert log_privacy_violations(source, module) == []
    assert log_privacy_violations(source, "elsewhere.py") != [], (
        "the store allowlist is not keyed by module"
    )


@pytest.mark.parametrize(
    "body",
    [
        "def cb():\n    logger.warning('x %s', e)",
        "m = str(e)\ndef cb():\n    logger.warning('x %s', m)",
        "cb = lambda: logger.warning('x %s', e)",
        "def cb():\n    def inner():\n        logger.warning('x %s', e)",
    ],
    ids=["def", "def-alias", "lambda", "doubly-nested"],
)
def test_t3_flags_a_nested_scope_that_logs_the_handlers_exception(body: str) -> None:
    """T3: a ``def`` or ``lambda`` inside a handler closes over ``e``."""
    assert log_privacy_violations(_handler(body)) != [], f"T3 missed: {body!r}"


@pytest.mark.parametrize(
    "body",
    [
        "def cb(e):\n    logger.warning('x %s', e)",
        "cb = lambda e: logger.warning('x %s', e)",
        "def cb():\n    logger.warning('x %s', describe_failure(e))",
    ],
    ids=["def-param-shadows", "lambda-param-shadows", "def-describe"],
)
def test_t3_spares_a_shadowing_parameter_and_a_class_only_closure(body: str) -> None:
    """T3: a parameter named ``e`` is not the handler's exception."""
    assert log_privacy_violations(_handler(body)) == []
