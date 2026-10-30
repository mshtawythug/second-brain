"""The G1 log-privacy guard over ``src/brain``, and its self-tests.

No logger call in ``src/brain`` may carry a caught exception's MESSAGE, which can
quote document content; ``tests/test_log_privacy.py`` says why and keeps the leak
reproductions. The rules live in ``tests/log_privacy_rules.py`` and the
allowlists, each entry with its reason, in ``tests/log_privacy_allowlists.py``.

- **G1** is the guard: an AST scan of ``src/brain``. It also fails on an
  allowlist entry that no site matches (S7), and on a ``src/brain`` exception
  class the annotation rule cannot see.
- **G2/G3** prove the scan can fail, and what it lets through.
- **T1-T3** (RM-28) do the same for the three blind spots PR #58 recorded: an
  alias logged after its handler, an attribute store, a nested ``def``/``lambda``.
- **S1-S8, R1-R2, A5** (RM-28 review round) close the gaps its review found.
  Plan: ``docs/plans/2026-10-30-rm-28-log-privacy-guard.md``.

Pure AST work: marked ``nodb``, so a run of this file alone opens no database
connection and takes no suite lock. All data is synthetic.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from tests.log_privacy_allowlists import RAW_ALLOWLIST, STORE_ALLOWLIST
from tests.log_privacy_rules import (
    EXCEPTION_CLASS_NAMES,
    exception_class_names,
    log_privacy_violations,
    scan,
    stale_entries,
)

pytestmark = pytest.mark.nodb

_SRC = Path(__file__).resolve().parent.parent / "src" / "brain"


def test_no_src_brain_logger_call_carries_an_exception_message() -> None:
    """G1: the guard over every module in ``src/brain``, and no stale allowlist entry."""
    hits: list[str] = []
    used: set[tuple[str, tuple[str, ...]]] = set()
    for path in sorted(_SRC.rglob("*.py")):
        found, matched = scan(path.read_text(), path.relative_to(_SRC).as_posix())
        hits += found
        used |= matched

    assert hits == [], f"sites that can carry an exception's text: {hits}"
    assert stale_entries(used) == [], f"allowlist entries no site matches: {stale_entries(used)}"


def test_every_src_exception_class_is_visible_to_the_annotation_rule() -> None:
    """G1 (S6): a new exception class whose name does not end with Error/Exception is listed."""
    derived = exception_class_names([p.read_text() for p in _SRC.rglob("*.py")])
    unseen = {n for n in derived if not n.endswith(("Error", "Exception"))}

    assert unseen - EXCEPTION_CLASS_NAMES == set(), "add these to EXCEPTION_CLASS_NAMES"
    assert EXCEPTION_CLASS_NAMES - derived == set(), "remove these from EXCEPTION_CLASS_NAMES"


def test_a_stale_allowlist_entry_is_reported() -> None:
    """S7: with no site matched, every entry of every allowlist is stale."""
    stale = stale_entries(set())

    assert {kind for kind, _ in stale} == {"raw", "traceback", "store"}


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


def test_t2_store_allowlist_is_keyed_by_the_whole_statement() -> None:
    """T2/S7: the allowlisted store passes only at its site, and only with its own value."""
    module, func, statement = ("ui/jobs.py", "_fail", "job.error = error")
    head = f"def {func}(self, job, error: JobError, other: JobError) -> None:\n"

    assert (module, func, statement) in STORE_ALLOWLIST
    assert all(STORE_ALLOWLIST.values()) and all(RAW_ALLOWLIST.values()), "an entry has no reason"
    assert log_privacy_violations(f"{head}    {statement}\n", module) == []
    assert log_privacy_violations(f"{head}    {statement}\n", "elsewhere.py") != []
    assert log_privacy_violations(f"{head}    job.error = other\n", module) != [], (
        "the store allowlist is keyed by target, not by the whole statement"
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


@pytest.mark.parametrize(
    "body",
    [
        "cb = lambda e=e: logger.warning('x %s', e)",
        "def cb(m=str(e)):\n    logger.warning('x %s', m)",
        "def cb(*, m=e.args):\n    logger.warning('x %s', m)",
    ],
    ids=["lambda-default", "def-default", "kwonly-default"],
)
def test_s1_flags_a_parameter_whose_default_is_tainted(body: str) -> None:
    """S1: a shadowing parameter stays tainted when its default carries ``e``."""
    assert log_privacy_violations(_handler(body)) != [], f"S1 missed: {body!r}"


def test_s1_spares_a_class_only_default() -> None:
    """S1: a default that goes through ``describe_failure`` is clean."""
    body = "def cb(m=describe_failure(e)):\n    logger.warning('x %s', m)"

    assert log_privacy_violations(_handler(body)) == []


_EXCEPT_STAR = "def f():\n    try:\n        pass\n    except* OSError as eg:\n"
_ASYNC = "async def f():\n    try:\n        pass\n    except OSError as e:\n"


@pytest.mark.parametrize(
    "source",
    [
        _handler("for a in e.args:\n    logger.warning('x %s', a)"),
        _ASYNC + "        async for a in agen(e):\n            logger.warning('x %s', a)\n",
        _handler("[logger.warning('x %s', a) for a in e.args]"),
        _handler("with ctx(str(e)) as m:\n    pass\nlogger.warning('x %s', m)"),
        _handler("match e:\n    case OSError(strerror=s):\n        logger.warning('x %s', s)"),
        _handler("match e.args:\n    case [first, *rest]:\n        logger.warning('x %s', rest)"),
        _handler("match vars(e):\n    case {**rest}:\n        logger.warning('x %s', rest)"),
        _handler("match e:\n    case OSError() as caught:\n        logger.warning('x %s', caught)"),
        _EXCEPT_STAR
        + "        for sub in eg.exceptions:\n            logger.warning('x %s', sub)\n",
    ],
    ids=[
        "for",
        "async-for",
        "comprehension",
        "with-as",
        "match-class-keyword",
        "match-star",
        "match-mapping-rest",
        "match-as",
        "except-star",
    ],
)
def test_s2_flags_every_binding_form(source: str) -> None:
    """S2: taint flows through every form that binds a name, not only assignment."""
    assert log_privacy_violations(source) != [], f"S2 missed: {source!r}"


@pytest.mark.parametrize(
    "body",
    [
        "for a in range(3):\n    logger.info('x %s', a)",
        "with open(path) as fh:\n    logger.info('x %s', fh.name)",
        "match describe_failure(e):\n    case str() as label:\n        logger.info('x %s', label)",
    ],
    ids=["for-clean", "with-clean", "match-class-only"],
)
def test_s2_spares_a_name_bound_from_a_clean_value(body: str) -> None:
    """S2: a binding form taints only when its value is tainted."""
    assert log_privacy_violations(_handler(body)) == []


_BARE = "def f():\n    try:\n        pass\n    except Exception:\n"


@pytest.mark.parametrize(
    "line",
    [
        "logger.warning('%s', traceback.format_exc())",
        "logger.warning('%s', format_exc())",
        "logger.warning('%s', traceback.format_exception(*sys.exc_info()))",
        "logger.warning('%s', sys.exc_info()[1])",
        "logger.warning('%s', sys.exception())",
        "err = future.exception()\nlogger.warning('%s', err)",
        "traceback.print_exc()",
    ],
    ids=["format-exc", "bare-format-exc", "format-exception", "exc-info", "sys-exception",
         "future-exception", "print-exc"],
)  # fmt: skip
def test_s3_flags_an_exception_reached_without_the_handlers_name(line: str) -> None:
    """S3: the current exception's text, reached through ``traceback``/``sys``/a future."""
    source = _BARE + "".join(f"        {part}\n" for part in line.splitlines())

    assert log_privacy_violations(source) != [], f"S3 missed: {line!r}"


def test_s3_spares_a_class_only_view_of_the_current_exception() -> None:
    """S3: ``describe_failure(sys.exception())`` is content-free."""
    source = _BARE + "        logger.warning('%s', describe_failure(sys.exception()))\n"

    assert log_privacy_violations(source) == []


@pytest.mark.parametrize(
    "body",
    [
        "setattr(self, 'detail', str(e))",
        "object.__setattr__(self, 'detail', e)",
        "self.errors[path] = str(e)",
        "report.errors.append(f'bad: {e}')",
        "self.errors.extend(e.args)",
        "self.seen.add(repr(e))",
        "self.errors.insert(0, e)",
        "self.detail.update(reason=str(e))",
    ],
    ids=["setattr", "object-setattr", "attr-subscript", "append-y4", "extend", "add",
         "insert", "update"],
)  # fmt: skip
def test_s4_flags_every_store_shape(body: str) -> None:
    """S4: ``setattr``, a subscript on an attribute, and an attribute container's methods."""
    assert log_privacy_violations(_handler(body)) != [], f"S4 missed: {body!r}"


@pytest.mark.parametrize(
    "body",
    [
        "report.errors.append(describe_failure(e))",
        "setattr(self, 'kind', type(e).__name__)",
        "self._queue.append(job)",
        "self.errors[path] = describe_failure(e)",
    ],
    ids=["append-describe", "setattr-type-name", "append-clean", "attr-subscript-describe"],
)
def test_s4_spares_a_class_only_or_clean_store(body: str) -> None:
    """S4: the store shapes pass when the stored value is class-only or untainted."""
    assert log_privacy_violations(_handler(body)) == []


@pytest.mark.parametrize(
    "source",
    [
        _handler("logger.warn('x %s', e)"),
        _handler("logger.fatal('x %s', e)"),
        _handler("logging.getLogger(__name__).warning('x %s', e)"),
        "def f(exc: OllamaUnavailable) -> None:\n    logger.warning('x %s', exc)\n",
        "def f(exc: AskTimedOut) -> None:\n    logger.warning('x %s', exc)\n",
        "def f(exc: ChatOutputTruncated) -> None:\n    logger.warning('x %s', exc)\n",
        "def f(exc: OllamaContextOverflow) -> None:\n    logger.warning('x %s', exc)\n",
        "def f(exc: psycopg.errors.UniqueViolation) -> None:\n    logger.warning('x %s', exc)\n",
        "def f(exc: KeyboardInterrupt | None) -> None:\n    logger.warning('x %s', exc)\n",
    ],
    ids=["warn", "fatal", "get-logger", "ollama-unavailable", "ask-timed-out",
         "chat-output-truncated", "context-overflow", "psycopg-errors", "builtin"],
)  # fmt: skip
def test_s6_flags_every_logger_and_exception_annotation(source: str) -> None:
    """S6: the ``warn``/``fatal`` levels, a ``getLogger(...)`` receiver, every exception name."""
    assert log_privacy_violations(source) != [], f"S6 missed: {source!r}"


@pytest.mark.parametrize(
    "annotation",
    ["ErrorMapper", "str", "Callable[[str], None]"],
    ids=["error-mapper", "str", "callable"],
)
def test_s6_spares_a_parameter_that_is_not_an_exception(annotation: str) -> None:
    """S6: an identifier must END with Error/Exception; ``ErrorMapper`` is a callable alias."""
    source = f"def f(cb: {annotation}) -> None:\n    logger.warning('x %s', cb)\n"

    assert log_privacy_violations(source) == []


def test_s8_taint_in_one_function_does_not_reach_a_sibling() -> None:
    """S8: the module scope does not descend into top-level ``def`` bodies."""
    source = (
        "def f():\n    try:\n        pass\n    except OSError as e:\n        m = str(e)\n"
        "def g(m):\n    logger.warning('x %s', m)\n"
        "def h():\n    logger.warning('x %s', m)\n"
    )

    assert log_privacy_violations(source) == []


@pytest.mark.parametrize(
    "body",
    [
        "logger.warning('%s', describe_failure(e, detail=str(e)))",
        "logger.warning('%s', type(e)(str(e)))",
        "logger.warning('%s', isinstance(e, OSError) and len(e.args, str(e)))",
    ],
    ids=["describe-keyword", "type-call", "len-second-arg"],
)
def test_r1_a_content_free_call_exempts_only_its_first_argument(body: str) -> None:
    """R1: ``describe_failure(e, detail=str(e))`` still carries the message."""
    assert log_privacy_violations(_handler(body)) != [], f"R1 missed: {body!r}"


def test_r1_spares_a_count() -> None:
    """R1: ``len`` reduces its argument to a count."""
    assert log_privacy_violations(_handler("logger.warning('%d', len(e.args))")) == []


@pytest.mark.parametrize(
    "body",
    [
        "@deco(logger.exception('x'))\ndef cb():\n    pass",
        "def cb(x: logger.warning('%s', e)):\n    pass",
        "def cb() -> logger.warning('%s', e):\n    pass",
    ],
    ids=["decorator", "annotation", "return-annotation"],
)
def test_r2_scans_a_nested_defs_decorators_and_annotations(body: str) -> None:
    """R2: decorators and annotations run in the enclosing scope, with its taint."""
    assert log_privacy_violations(_handler(body)) != [], f"R2 missed: {body!r}"


@pytest.mark.parametrize(
    "body",
    [
        "job.result = e\nlogger.info('x %s', job.id)",
        "errs[path] = str(e)\nlogger.info('x %s', path)",
    ],
    ids=["attribute-base", "subscript-key"],
)
def test_a5_a_stores_base_and_key_stay_clean(body: str) -> None:
    """A5: only NAME targets are tainted; T2 reports the store, not its base or key."""
    hits = log_privacy_violations(_handler(body))

    assert not any(hit.endswith(" raw") for hit in hits), hits
