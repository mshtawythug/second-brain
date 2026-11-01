"""Reading a ``pg_restore -l`` table of contents for the extensions a dump creates (RM-47).

sec-119 I4: the staging prep runs ``CREATE EXTENSION age``, which needs a
superuser, so it must run only when the archive itself carries the ``age``
EXTENSION entry. A non-AGE archive restored onto an AGE-capable server must
not newly require superuser. The TOC lines below are synthetic but in the exact
shape ``pg_restore -l`` prints.
"""
from __future__ import annotations

import pytest

from brain.backup.staging_prep import STAGING_EXTENSIONS, extensions_in_toc, staging_needs

pytestmark = pytest.mark.nodb

AGE_TOC = """\
;
; Archive created at 2026-11-01 07:30:15 UTC
;     dbname: synthetic_primary
;
; Selected TOC Entries:
;
4; 3079 1001 EXTENSION - age 
4583; 0 0 COMMENT - EXTENSION age 
3; 3079 1002 EXTENSION - pgcrypto 
2; 3079 1003 EXTENSION - vector 
220; 1259 1004 TABLE public schema_migrations brain
"""

PLAIN_TOC = """\
;
; Selected TOC Entries:
;
3; 3079 1002 EXTENSION - pgcrypto 
4584; 0 0 COMMENT - EXTENSION pgcrypto 
2; 3079 1003 EXTENSION - vector 
220; 1259 1004 TABLE public schema_migrations brain
"""


def test_extension_entries_are_read_from_the_toc() -> None:
    assert extensions_in_toc(AGE_TOC) == frozenset({"age", "pgcrypto", "vector"})


def test_a_toc_without_age_has_no_age() -> None:
    assert extensions_in_toc(PLAIN_TOC) == frozenset({"pgcrypto", "vector"})


@pytest.mark.parametrize(
    "line",
    [
        "4583; 0 0 COMMENT - EXTENSION age ",  # a comment ON the extension
        "221; 1259 1005 TABLE public age brain",  # a table named age
        "; 4; 3079 1001 EXTENSION - age",  # a commented-out entry
        "4; 3079 1001 EXTENSION - agenda",  # a different extension
    ],
)
def test_lines_that_merely_mention_age_are_not_an_age_extension(line: str) -> None:
    assert "age" not in extensions_in_toc(line + "\n")


def test_an_empty_toc_names_nothing() -> None:
    assert extensions_in_toc("") == frozenset()


def test_staging_needs_only_what_the_archive_creates() -> None:
    assert STAGING_EXTENSIONS == ("age",)
    assert staging_needs(AGE_TOC) == ("age",)
    assert staging_needs(PLAIN_TOC) == ()
