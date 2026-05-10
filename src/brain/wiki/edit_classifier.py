"""Edit classifier: classify a vault file edit as trivial or non-trivial.

A **trivial** edit is one whose canonical structural fingerprint did NOT
change — only body prose / whitespace / HTML comments / ignored frontmatter
fields were touched.  A **non-trivial** edit requires a full Quartz build
(fingerprint changed, slug unknown, manifest missing / unreadable, source
path diverged from manifest record, etc.).

Used by the watcher (T6) to route edits through the fast path or the full
build path.

Public surface:
    classify_edit(*, fastpath_dir, source_path, vault_root) → ClassificationResult

Slug helpers live in ``brain.wiki.slug``; import them from there.
"""
from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from pathlib import Path

from brain.wiki.fastpath_manifest import (
    ManifestError,
    compute_fingerprint,
    read_manifest,
)
from brain.wiki.slug import slugify_source_path

# ---------------------------------------------------------------------------
# Public types
# ---------------------------------------------------------------------------


class EditClassification(Enum):
    """Result of classify_edit."""

    TRIVIAL = "trivial"
    NON_TRIVIAL = "non-trivial"


@dataclass(frozen=True, slots=True)
class ClassificationResult:
    """Full result of a single-file edit classification."""

    classification: EditClassification
    reason: str  # human-readable, for logs / telemetry
    slug: str | None  # None only if slug could not be determined
    old_fingerprint: str | None  # None if manifest missing / slug unknown
    new_fingerprint: str | None  # None if file unreadable / compute failed


# ---------------------------------------------------------------------------
# Public classifier
# ---------------------------------------------------------------------------


def classify_edit(
    *,
    fastpath_dir: Path,
    source_path: Path,
    vault_root: Path,
) -> ClassificationResult:
    """Classify a single vault file edit as trivial or non-trivial.

    A trivial edit is one where the canonical structural fingerprint is
    unchanged — only prose / whitespace / ignored frontmatter fields were
    modified.  A non-trivial edit (or any error condition) falls back to a
    full Quartz build.

    Algorithm:
      1. Validate ``source_path`` is inside ``vault_root`` (raises ValueError
         on programmer error — T6 must pre-validate event paths).
      2. Compute slug from ``source_path`` relative to ``vault_root``.
      3. Check ``source_path`` still exists (delete/move → non-trivial).
      4. Read manifest from ``fastpath_dir``; catch ManifestError → non-trivial.
      5. Look up slug in manifest; missing → non-trivial.
      5a. **Rename guard:** compare current vault-relative path against
          ``entry.source_path`` from the manifest.  If they differ, the slug
          collision is spurious (e.g. "a b.md" and "a-b.md" both slugify to
          ``a-b``); force non-trivial so the full build reconciles the rename.
      6. Read file bytes; OS error → non-trivial.
      7. After step 5a verifies the current vault-relative path equals
         ``entry.source_path``, compute the new fingerprint using the manifest's
         recorded ``source_path`` / ``output_path`` (sanity guard against
         drift between the manifest writer's and reader's path conventions).
      8. Compare fingerprints; equal → trivial, unequal → non-trivial.

    Args:
        fastpath_dir: Path to ``<vault>/.quartz/.cache/fastpath/``.
        source_path: Absolute path to the edited file.
        vault_root: Absolute path to the vault root directory.

    Returns:
        :class:`ClassificationResult` with all fields populated.

    Raises:
        ValueError: If ``source_path`` is not inside ``vault_root``.
    """
    # Step 1 — validate source_path inside vault_root (programmer-error guard).
    try:
        source_path.relative_to(vault_root)
    except ValueError as exc:
        raise ValueError(
            f"source_path {source_path!r} is not inside vault_root {vault_root!r}"
        ) from exc

    # Step 2 — compute slug.
    slug = slugify_source_path(source_path, vault_root)

    # Step 3 — source file must exist (delete / move → non-trivial).
    if not source_path.exists():
        return ClassificationResult(
            classification=EditClassification.NON_TRIVIAL,
            reason="source file missing",
            slug=slug,
            old_fingerprint=None,
            new_fingerprint=None,
        )

    # Step 4 — read manifest; any ManifestError → non-trivial.
    try:
        manifest = read_manifest(fastpath_dir)
    except ManifestError as exc:
        raw = str(exc)
        # Produce a user-friendly reason while preserving the error detail.
        if "cannot read manifest" in raw:
            reason = "manifest missing — first build pending or cleared"
        elif "malformed manifest JSON" in raw:
            reason = f"manifest unreadable: {exc}"
        elif "version" in raw:
            reason = raw  # already contains version numbers from read_manifest
        else:
            reason = f"manifest error: {exc}"
        return ClassificationResult(
            classification=EditClassification.NON_TRIVIAL,
            reason=reason,
            slug=slug,
            old_fingerprint=None,
            new_fingerprint=None,
        )

    # Step 5 — look up slug entry.
    entry = manifest.slugs.get(slug)
    if entry is None:
        return ClassificationResult(
            classification=EditClassification.NON_TRIVIAL,
            reason=f"slug not in manifest: {slug}",
            slug=slug,
            old_fingerprint=None,
            new_fingerprint=None,
        )

    # Step 5a — rename guard: verify current path matches manifest's record.
    # Two distinct filenames can produce the same slug (e.g. "a b.md" and
    # "a-b.md" both → "a-b").  If the paths differ the slug match is spurious;
    # force full build so the rename is properly handled.
    current_source_path = source_path.relative_to(vault_root).as_posix()
    if entry.source_path != current_source_path:
        return ClassificationResult(
            classification=EditClassification.NON_TRIVIAL,
            reason=(
                f"source path changed: manifest={entry.source_path!r} "
                f"current={current_source_path!r}"
            ),
            slug=slug,
            old_fingerprint=entry.fingerprint,
            new_fingerprint=None,
        )

    # Step 6 — read current file bytes.
    try:
        source_bytes = source_path.read_bytes()
    except OSError as exc:
        return ClassificationResult(
            classification=EditClassification.NON_TRIVIAL,
            reason=f"source unreadable: {exc}",
            slug=slug,
            old_fingerprint=entry.fingerprint,
            new_fingerprint=None,
        )

    # Step 7 — compute new fingerprint using manifest's recorded paths.
    try:
        new_fp = compute_fingerprint(
            source_bytes=source_bytes,
            slug=slug,
            source_path=entry.source_path,
            output_path=entry.output_path,
        )
    except ManifestError as exc:
        return ClassificationResult(
            classification=EditClassification.NON_TRIVIAL,
            reason=f"fingerprint computation failed: {exc}",
            slug=slug,
            old_fingerprint=entry.fingerprint,
            new_fingerprint=None,
        )

    # Step 8 — compare fingerprints.
    if new_fp == entry.fingerprint:
        return ClassificationResult(
            classification=EditClassification.TRIVIAL,
            reason="fingerprint unchanged",
            slug=slug,
            old_fingerprint=entry.fingerprint,
            new_fingerprint=new_fp,
        )
    return ClassificationResult(
        classification=EditClassification.NON_TRIVIAL,
        reason="fingerprint changed",
        slug=slug,
        old_fingerprint=entry.fingerprint,
        new_fingerprint=new_fp,
    )
