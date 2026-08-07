"""`brain demo`'s packaged corpus needs no secret guard — proven, not assumed.

``brain.demo.seed`` calls ``ingest_document`` without ``secret_guard`` /
``allow_secrets``, and that is a deliberate decision rather than an oversight.
The demo ingests ``brain/demo/corpus/manifest.json`` — 22 synthetic documents
shipped inside the package. No user content ever reaches that call site, so
there is nothing for a guard to protect: it would scan our own constants on
every ``brain demo`` run, and a false positive (these regexes do fire on
legitimate prose) would break the zero-friction onboarding path the command
exists to provide.

That argument only holds while the shipped corpus is genuinely clean, which is
an assumption about a data file that someone could edit later. So rather than
wire a guard that can only ever scan constants we control, assert the property
the decision depends on: the corpus contains no secrets. If a future edit adds
one, this fails and the decision gets revisited deliberately — the same shape as
"deliberate exclusion, guarded by a test" used elsewhere in this suite.

Static: reads the packaged manifest and runs the real scanner. No database, no
network, no fixtures.
"""
from __future__ import annotations

import pytest

from brain.demo import load_corpus
from brain.ingest.guard import scan_secrets


def _corpus_records() -> list[dict[str, object]]:
    return load_corpus()


def test_corpus_is_not_empty() -> None:
    """The scan below must actually have something to scan.

    If ``load_corpus`` ever returned ``[]``, the parametrized test would collect
    zero cases and report green while checking nothing — the same vacuous-pass
    failure the other guards in this suite are written to avoid.
    """
    records = _corpus_records()
    assert len(records) >= 20, (
        f"expected the packaged 22-doc demo corpus, got {len(records)} records"
    )


@pytest.mark.parametrize(
    "record",
    _corpus_records(),
    ids=lambda r: str(r.get("external_id") or r.get("title", "?"))[:40],
)
def test_corpus_record_carries_no_secrets(record: dict[str, object]) -> None:
    """Every packaged demo document scans clean under the real detector.

    Uses :func:`brain.ingest.guard.scan_secrets` itself rather than a
    reimplementation, so the corpus is held to exactly the standard the guard
    would apply if it were wired.
    """
    body = str(record.get("content", ""))
    title = str(record.get("title", ""))
    findings = scan_secrets(f"{title}\n{body}")
    assert not findings, (
        f"demo corpus record {record.get('external_id')!r} contains "
        f"{len(findings)} secret-guard finding(s): "
        f"{[f.kind for f in findings]}. `brain demo` deliberately ingests this "
        "corpus WITHOUT a secret guard because it is packaged synthetic data; "
        "that reasoning fails if the data stops being clean. Either scrub the "
        "record or wire secret_guard= at brain/demo/__init__.py's "
        "ingest_document call."
    )
