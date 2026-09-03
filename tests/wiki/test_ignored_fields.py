"""Tests for vault-sourced fastpath ignore rules and the v1→v2 transition."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from brain.wiki.edit_classifier import EditClassification, classify_edit
from brain.wiki.fastpath_manifest import (
    FINGERPRINT_VERSION,
    ManifestError,
    compute_fingerprint,
)
from brain.wiki.ignored_fields import (
    DEFAULT_IGNORE_RULES,
    DEFAULT_IGNORED_FIELDS,
    IGNORE_FILE_NAME,
    IgnoreRules,
    IgnoreRulesError,
    load_ignore_rules,
    parse_ignore_file,
)

# A frontmatter key that is neither structural nor in the built-in default —
# it stands in for the corpus-specific keys the shipped package no longer names.
VAULT_KEY = "acme_sweep_widgets"
VAULT_GLOB = "acme_sweep_*"


def _doc(extra_fm: str = "") -> bytes:
    return f"---\ntitle: Sample\n{extra_fm}---\n\nBody text.\n".encode()


# ---------------------------------------------------------------------------
# The shipped default carries no vault-specific identifiers
# ---------------------------------------------------------------------------


def test_default_rules_have_no_patterns() -> None:
    """The built-in default is literal-only — patterns are opt-in per vault."""
    assert DEFAULT_IGNORE_RULES.patterns == ()
    assert DEFAULT_IGNORE_RULES.literals == DEFAULT_IGNORED_FIELDS


def test_default_set_is_pinned() -> None:
    """The shipped default is pinned by size and digest.

    What this proves: the set cannot grow or change without someone editing
    this test.  What it does NOT prove: that the contents are free of product
    or codename strings — no assertion can decide that mechanically.  The
    value is procedural.  This list previously accreted 23 corpus-specific
    keys, one commit at a time, and shipped them to PyPI; pinning it turns
    every future addition into a deliberate act that a reviewer sees.

    If you are here because this test failed: confirm the key you added names
    nothing product-, vendor-, or customer-specific, then re-pin.  Keys that
    ARE corpus-specific belong in the vault's own ``.brain-fastpath-ignore``,
    which is exactly what the rest of this module exists to support.
    """
    assert len(DEFAULT_IGNORED_FIELDS) == 52
    digest = hashlib.sha256("\n".join(sorted(DEFAULT_IGNORED_FIELDS)).encode()).hexdigest()
    assert digest == (
        "210491558d804de9e59edb475341014babfd8a0246f56d698941dffe2cded7f4"
    ), f"shipped default ignore set changed (digest {digest})"


# ---------------------------------------------------------------------------
# Fail-closed: an unconfigured vault still refuses unknown keys
# ---------------------------------------------------------------------------


def test_unknown_key_raises_under_default_rules() -> None:
    """An unlisted frontmatter key forces the paranoid path (full build)."""
    with pytest.raises(ManifestError) as exc:
        compute_fingerprint(
            source_bytes=_doc(f"{VAULT_KEY}: true\n"),
            slug="s", source_path="s.md", output_path="s.html",
        )
    assert VAULT_KEY in str(exc.value)


def test_error_names_the_ignore_file_so_it_is_self_healing() -> None:
    """The raise tells the user exactly which file to add the key to."""
    with pytest.raises(ManifestError) as exc:
        compute_fingerprint(
            source_bytes=_doc(f"{VAULT_KEY}: true\n"),
            slug="s", source_path="s.md", output_path="s.html",
        )
    assert IGNORE_FILE_NAME in str(exc.value)


# ---------------------------------------------------------------------------
# Vault-supplied rules restore the allowlist behaviour
# ---------------------------------------------------------------------------


def test_literal_rule_from_vault_file_allows_key(tmp_path: Path) -> None:
    """A literal line in the vault's ignore file stops the raise."""
    (tmp_path / IGNORE_FILE_NAME).write_text(f"{VAULT_KEY}\n", encoding="utf-8")
    rules = load_ignore_rules(tmp_path)
    fp = compute_fingerprint(
        source_bytes=_doc(f"{VAULT_KEY}: true\n"),
        slug="s", source_path="s.md", output_path="s.html",
        ignore_rules=rules,
    )
    assert len(fp) == 64


def test_glob_rule_covers_a_whole_key_family(tmp_path: Path) -> None:
    """One glob line covers every key in a namespaced family."""
    (tmp_path / IGNORE_FILE_NAME).write_text(f"{VAULT_GLOB}\n", encoding="utf-8")
    rules = load_ignore_rules(tmp_path)
    for suffix in ("widgets", "invoices", "payroll"):
        assert rules.matches(f"acme_sweep_{suffix}")


def test_namespace_glob_does_not_cover_the_bare_namespace() -> None:
    """``alpha_*`` does not match bare ``alpha`` — use ``alpha*`` for that.

    Not a curiosity: of the three key families this package stopped shipping,
    one uses its namespace as a key in its own right, so the obvious
    ``<namespace>_*`` recipe silently covers 22 of 23 keys and leaves the
    23rd raising ManifestError.  Failing closed means the symptom is one
    document quietly always taking the full-build path — easy to miss.
    """
    underscore = IgnoreRules(literals=frozenset(), patterns=("alpha_*",), origin="t")
    assert underscore.matches("alpha_one")
    assert not underscore.matches("alpha")

    bare = IgnoreRules(literals=frozenset(), patterns=("alpha*",), origin="t")
    assert bare.matches("alpha_one")
    assert bare.matches("alpha")


def test_ignored_key_does_not_change_the_fingerprint(tmp_path: Path) -> None:
    """An ignored key is genuinely absent from the canonical blob."""
    (tmp_path / IGNORE_FILE_NAME).write_text(f"{VAULT_KEY}\n", encoding="utf-8")
    rules = load_ignore_rules(tmp_path)
    kw = dict(slug="s", source_path="s.md", output_path="s.html", ignore_rules=rules)
    with_key = compute_fingerprint(source_bytes=_doc(f"{VAULT_KEY}: true\n"), **kw)
    without = compute_fingerprint(source_bytes=_doc(), **kw)
    assert with_key == without


def test_vault_rules_union_the_default(tmp_path: Path) -> None:
    """A vault file adds to the default; it never replaces it."""
    (tmp_path / IGNORE_FILE_NAME).write_text(f"{VAULT_KEY}\n", encoding="utf-8")
    rules = load_ignore_rules(tmp_path)
    assert rules.matches(VAULT_KEY)
    assert rules.matches("vault_path")


def test_missing_ignore_file_yields_the_default(tmp_path: Path) -> None:
    """No file is not an error — it is the documented zero-config state."""
    assert load_ignore_rules(tmp_path) == DEFAULT_IGNORE_RULES


# ---------------------------------------------------------------------------
# Parsing
# ---------------------------------------------------------------------------


def test_parse_skips_comments_and_blank_lines() -> None:
    literals, patterns = parse_ignore_file("# a comment\n\n  \nalpha  # trailing\nbeta\n")
    assert literals == frozenset({"alpha", "beta"})
    assert patterns == ()


def test_parse_splits_globs_from_literals() -> None:
    literals, patterns = parse_ignore_file("alpha\nbeta_*\ngamma?\n")
    assert literals == frozenset({"alpha"})
    assert patterns == ("beta_*", "gamma?")


def test_parse_rejects_whitespace_inside_a_key() -> None:
    with pytest.raises(IgnoreRulesError):
        parse_ignore_file("not a key\n")


def test_matching_is_case_sensitive() -> None:
    """Glob matching does not fold case; YAML keys are case-sensitive.

    Mutation note: swapping ``fnmatchcase`` for ``fnmatch`` does NOT redden
    this on macOS or Linux, because ``fnmatch`` case-folds via
    ``os.path.normcase``, which is the identity function on POSIX.  The two
    are distinguishable only on Windows.  ``fnmatchcase`` is still the right
    call — it states the intent platform-independently — but the assertion
    below is proven against a different failure mode: a ``matches()`` that
    "normalises" keys by lowercasing them.
    """
    rules = IgnoreRules(literals=frozenset(), patterns=("alpha_*",), origin="test")
    assert rules.matches("alpha_one")
    assert not rules.matches("ALPHA_ONE")


def test_unreadable_ignore_file_raises(tmp_path: Path) -> None:
    """A present-but-undecodable file is loud, not silently skipped."""
    (tmp_path / IGNORE_FILE_NAME).write_bytes(b"\xff\xfe\x00bad")
    with pytest.raises(IgnoreRulesError):
        load_ignore_rules(tmp_path)


# ---------------------------------------------------------------------------
# Migration / transition — a stale v1 cache must rebuild, never answer "trivial"
# ---------------------------------------------------------------------------


def _write_manifest(fastpath_dir: Path, *, version: int, fingerprint: str) -> None:
    fastpath_dir.mkdir(parents=True, exist_ok=True)
    (fastpath_dir / "manifest.json").write_text(
        json.dumps({
            "version": version,
            "parent_build_id": "b1",
            "built_at_ms": 0,
            "slugs": {
                "note": {
                    "fingerprint": fingerprint,
                    "output_path": "note.html",
                    "source_path": "note.md",
                }
            },
        }),
        encoding="utf-8",
    )


def test_stale_v1_manifest_forces_full_build_not_trivial(tmp_path: Path) -> None:
    """The v1→v2 transition fails closed.

    This is the dangerous case: a stale fingerprint is a 64-hex string that
    looks exactly like a valid one.  If the version gate did not reject it, an
    unchanged-looking hash would route a real edit down the fast path and ship
    stale HTML.  Assert the *classification*, not just that an error was raised.
    """
    vault = tmp_path / "vault"
    vault.mkdir()
    source = vault / "note.md"
    source.write_bytes(_doc())
    fastpath = tmp_path / "fastpath"

    # A fingerprint computed by the CURRENT code, stored under the OLD version.
    current_fp = compute_fingerprint(
        source_bytes=source.read_bytes(),
        slug="note", source_path="note.md", output_path="note.html",
    )
    _write_manifest(fastpath, version=FINGERPRINT_VERSION - 1, fingerprint=current_fp)

    result = classify_edit(
        fastpath_dir=fastpath, source_path=source, vault_root=vault
    )
    assert result.classification is EditClassification.NON_TRIVIAL
    assert "version" in result.reason


def test_current_version_manifest_still_classifies_trivial(tmp_path: Path) -> None:
    """Control for the test above: at the current version the fast path works.

    Without this, the transition test would still pass if classify_edit had
    regressed into returning NON_TRIVIAL unconditionally.
    """
    vault = tmp_path / "vault"
    vault.mkdir()
    source = vault / "note.md"
    source.write_bytes(_doc())
    fastpath = tmp_path / "fastpath"
    current_fp = compute_fingerprint(
        source_bytes=source.read_bytes(),
        slug="note", source_path="note.md", output_path="note.html",
    )
    _write_manifest(fastpath, version=FINGERPRINT_VERSION, fingerprint=current_fp)

    result = classify_edit(
        fastpath_dir=fastpath, source_path=source, vault_root=vault
    )
    assert result.classification is EditClassification.TRIVIAL


def test_newly_unignored_key_forces_full_build(tmp_path: Path) -> None:
    """A key that used to ship in the default now fails closed, not open.

    Removing entries from the package default can only add full builds; it can
    never let a changed document through as trivial.
    """
    vault = tmp_path / "vault"
    vault.mkdir()
    source = vault / "note.md"
    source.write_bytes(_doc(f"{VAULT_KEY}: true\n"))
    fastpath = tmp_path / "fastpath"
    _write_manifest(fastpath, version=FINGERPRINT_VERSION, fingerprint="0" * 64)

    result = classify_edit(
        fastpath_dir=fastpath, source_path=source, vault_root=vault
    )
    assert result.classification is EditClassification.NON_TRIVIAL
    assert "fingerprint computation failed" in result.reason


def test_classifier_reads_rules_from_the_vault(tmp_path: Path) -> None:
    """With the vault's own ignore file present, the same edit is trivial again."""
    vault = tmp_path / "vault"
    vault.mkdir()
    (vault / IGNORE_FILE_NAME).write_text(f"{VAULT_GLOB}\n", encoding="utf-8")
    source = vault / "note.md"
    source.write_bytes(_doc(f"{VAULT_KEY}: true\n"))
    fastpath = tmp_path / "fastpath"
    fp = compute_fingerprint(
        source_bytes=source.read_bytes(),
        slug="note", source_path="note.md", output_path="note.html",
        ignore_rules=load_ignore_rules(vault),
    )
    _write_manifest(fastpath, version=FINGERPRINT_VERSION, fingerprint=fp)

    result = classify_edit(
        fastpath_dir=fastpath, source_path=source, vault_root=vault
    )
    assert result.classification is EditClassification.TRIVIAL


def test_broken_ignore_file_forces_full_build(tmp_path: Path) -> None:
    """A malformed ignore file degrades to full builds, not to wrong rules."""
    vault = tmp_path / "vault"
    vault.mkdir()
    (vault / IGNORE_FILE_NAME).write_bytes(b"\xff\xfe\x00bad")
    source = vault / "note.md"
    source.write_bytes(_doc())
    fastpath = tmp_path / "fastpath"
    _write_manifest(fastpath, version=FINGERPRINT_VERSION, fingerprint="0" * 64)

    result = classify_edit(
        fastpath_dir=fastpath, source_path=source, vault_root=vault
    )
    assert result.classification is EditClassification.NON_TRIVIAL
    assert "ignore rules unreadable" in result.reason
