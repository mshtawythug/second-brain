"""Shared person-name normalization for the People Hub + graph reconcile.

Pure, DB-free, and heavily unit-tested. This module is the single source of
truth for turning a raw participant identifier (a display name from a Gmail
header, a Krisp speaker label, or a bare email address) into:

* a ``canonical_key`` — the lowercase, separator-collapsed *merge identity*
  (``Jane.Doe``, ``jane_doe``, and ``Jane Doe`` all collapse to ``jane doe``),
  and
* a ``display_name`` — the human-facing presentation form.

It also owns the two deterministic *filters* that gate person extraction:
:func:`is_automated_sender` (drop no-reply / notification / mailer org senders)
and :func:`expand_owner_keys` (widen the corpus-owner key set so the owner can
never leak in under a first-name-only or email-local-part variant).

Both the People-Hub aggregator (:mod:`brain.people`) and the graph
person reconcile (:mod:`brain.graph_rag.reconcile`) route through here, so the
graph's person entities and the rendered ``<vault>/people/`` roster derive from
exactly the same cleaned identity — they can never drift.

Design notes:

* Hyphens and apostrophes are preserved inside the canonical key (``anne-marie``,
  ``o'brien`` are real names). Only ``.``, ``_`` and whitespace runs collapse to
  a single space — that is what merges handle-style keys with their spaced
  display form.
* An email-shaped input is humanized from its *local part only* — the local part
  is canonicalized like any other name, the domain is discarded. A raw
  ``jane.doe@example.com`` therefore becomes ``jane doe`` / ``Jane Doe`` and is
  never title-cased into ``Jane.Doe@Example.Com``.
* Presentation casing uses :meth:`str.title`, matching the prior
  ``humanize_display_name`` behavior. Rare apostrophe / camel-case names
  (``d'arcy`` → ``D'Arcy``) are slightly mangled — an accepted trade-off the
  user can override via ``_people.yml``.
"""
import re
from collections.abc import Iterable
from dataclasses import dataclass

from brain.vault.derived_links.participants import is_email_like

__all__ = [
    "NormalizedName",
    "expand_owner_keys",
    "humanize_person_name",
    "is_automated_sender",
    "normalize_person_name",
]

# Minimum length (after canonicalization) for a name to be considered real.
# Single-letter "names" (``A``, ``J``) are too noisy to link on — mirrors
# ``brain.vault.derived_links.participants._MIN_NAME_LENGTH``.
_MIN_NAME_LENGTH = 2

# Mailing-list "via X" decoration (Google Groups rewrites the ``From`` header to
# ``"Jane Doe via Acme Members" <list@…>``). Strip ``via`` and everything after.
_VIA_RE = re.compile(r"\s+via\s+.+$", re.IGNORECASE)

# Trailing ``(Org…`` fragment — e.g. ``Smith, John (Acme Tech`` (often with no
# closing paren because the header was truncated). Drops from the first ``(``.
_ORG_PAREN_RE = re.compile(r"\s*\(.*$")

# Separators collapsed to a single space when building the canonical key. NOTE:
# hyphen is intentionally absent — ``anne-marie`` keeps its hyphen.
_SEPARATORS_RE = re.compile(r"[._\s]+")

# Outer quotes / brackets stripped from the whole token (stray ``'`` / ``<>`` /
# ``[]`` / ``{}`` left by header rewrites). Internal characters are untouched.
_OUTER_JUNK = " \t\r\n\f\v\"'`<>[]{}"

# Outer punctuation stripped after separator collapse — mirrors the strip set in
# ``normalize_participant`` so the two layers agree. Internal hyphens /
# apostrophes survive (only leading/trailing ones are removed).
_OUTER_PUNCT = " \t\n\r\f\v.,;:!?\"'`()[]{}<>-_/\\|"

# Markers (matched against the email's LOCAL PART ONLY — never the domain, which
# would drop real humans like ``john@mailer-corp.example.com`` or
# ``jane@notifications.acme.com``) that flag an automated / non-human sender.
# Kept GENERIC — structural words only, no corpus-specific company names. The
# match uses word-boundary semantics (see :func:`_local_is_automated`): the
# local part must EQUAL a marker, or start with ``marker + sep``, or end with
# ``sep + marker`` (``sep`` ∈ :data:`_MARKER_SEPS`). That catches ``no-reply``,
# ``mailer-daemon``, ``bounce``, ``acme.noreply`` but NOT ``dmailer`` (Dana
# Mailer) or ``jbounce`` (Jane Bounce).
_AUTOMATED_MARKERS: frozenset[str] = frozenset(
    {
        "no-reply",
        "noreply",
        "no_reply",
        "donotreply",
        "do-not-reply",
        "do_not_reply",
        "notifications",
        "notification",
        "mailer-daemon",
        "mailer_daemon",
        "mailerdaemon",
        "postmaster",
        "bounce",
        "bounces",
        "auto-reply",
        "autoreply",
        "automated",
        "mailer",
    }
)

# Separators that delimit a marker inside a local part for the boundary match.
_MARKER_SEPS: tuple[str, ...] = ("-", ".", "_", "+")


@dataclass(frozen=True)
class NormalizedName:
    """A cleaned person identity: a merge key plus a presentation form.

    ``canonical_key`` is the lowercase, separator-collapsed identity used to
    merge variants of the same person and key ``graph_entities`` rows.
    ``display_name`` is the human-facing form (title-cased canonical key).
    """

    canonical_key: str
    display_name: str


def humanize_person_name(canonical_key: str) -> str:
    """Re-cap a lowercase canonical key for headings / frontmatter.

    Title-cases the canonical key. Inputs are always clean canonical keys (no
    ``@``, separators already collapsed) so this is a thin, deterministic
    presentation transform. Shared by the People Hub renderer, the graph
    reconcile resolver, and the ``brain people`` CLI.
    """
    return canonical_key.title()


def _canonical_from_raw(text: str) -> str | None:
    """Lowercase, collapse ``. _`` + whitespace, strip outer punctuation.

    Returns ``None`` when fewer than :data:`_MIN_NAME_LENGTH` characters
    survive — the caller drops such tokens.
    """
    collapsed = _SEPARATORS_RE.sub(" ", text.lower())
    cleaned = collapsed.strip(_OUTER_PUNCT).strip()
    if len(cleaned) < _MIN_NAME_LENGTH:
        return None
    return cleaned


def _flip_last_first(text: str) -> str:
    """Flip a single ``Last, First`` token to ``First Last``.

    Only acts when a comma is present. Any trailing ``(Org…`` fragment on the
    ``First`` side is dropped. Degenerate inputs (empty half after the flip)
    are returned unchanged for the downstream canonicalizer to handle.
    """
    if "," not in text:
        return text
    last, _, rest = text.partition(",")
    first = _ORG_PAREN_RE.sub("", rest.split(",")[0]).strip()
    last = last.strip()
    if not first or not last:
        return text
    return f"{first} {last}"


def _clean_name_to_canonical(text: str) -> str | None:
    """Apply the full name-cleaning pipeline, returning a canonical key or None.

    Order matters: strip outer junk first (so a leading quote doesn't hide the
    ``via`` delimiter), then drop mailing-list decoration, flip ``Last, First``,
    drop any surviving ``(Org`` fragment, and finally canonicalize.
    """
    text = text.strip(_OUTER_JUNK)
    text = _VIA_RE.sub("", text)
    text = _flip_last_first(text)
    text = _ORG_PAREN_RE.sub("", text)
    return _canonical_from_raw(text)


def normalize_person_name(raw: str) -> NormalizedName | None:
    """Normalize a raw participant identifier into a :class:`NormalizedName`.

    Handles every Phase-1 pattern:

    1. Mailing-list ``via X`` decoration is stripped.
    2. ``Last, First (Org`` is flipped to ``First Last`` and the org fragment
       dropped.
    3. ``. _`` and whitespace runs collapse so handle-style and spaced forms
       share a canonical key.
    4. An email-shaped input is humanized from its local part only (never the
       full ``local@domain``).

    Returns ``None`` when nothing usable survives (empty / sub-2-char tokens).
    """
    text = (raw or "").strip()
    if not text:
        return None

    # Email-shaped input: derive the name from the local part only.
    if is_email_like(text):
        local = text.split("@", 1)[0]
        canonical = _canonical_from_raw(local)
    else:
        canonical = _clean_name_to_canonical(text)

    if canonical is None:
        return None
    return NormalizedName(canonical, humanize_person_name(canonical))


def _local_is_automated(local: str) -> bool:
    """True iff the email local part is an automated marker by word boundary.

    Boundary semantics (NOT substring): the local part must EQUAL a marker, or
    start with ``marker + sep``, or end with ``sep + marker`` for some ``sep``
    in :data:`_MARKER_SEPS`. This catches ``no-reply`` / ``mailer-daemon`` /
    ``acme.noreply`` / ``bounce`` while leaving ``nmailer`` and ``jbounce``
    (real names that merely contain a marker substring) untouched.
    """
    for marker in _AUTOMATED_MARKERS:
        if local == marker:
            return True
        for sep in _MARKER_SEPS:
            if local.startswith(marker + sep) or local.endswith(sep + marker):
                return True
    return False


def is_automated_sender(
    email: str,
    *,
    denylist: frozenset[str] = frozenset(),
) -> bool:
    """Is this ``email`` an automated / non-human sender?

    Generic, corpus-agnostic rules (in order):

    1. Any ``denylist`` entry (a substring or full address) found in the
       lowercased email — the configurable ``BRAIN_GRAPH_SENDER_DENYLIST``
       escape hatch for org / bulk senders.
    2. The email's LOCAL PART (lowercased, ``+tag`` stripped) matches a known
       automated marker (``no-reply`` / ``mailer-daemon`` / ``bounce`` /
       ``notifications`` / ``postmaster`` / …) by word boundary — see
       :func:`_local_is_automated`.

    The DOMAIN is never inspected by rule 2 (it would drop real humans like
    ``john@mailer-corp.example.com`` / ``jane@notifications.acme.com``), and there is no
    display-name heuristic (it false-positived ``bob@bob.com``). A
    non-email-shaped value only matches the denylist; an empty value is never
    automated.
    """
    addr = email.strip().lower()
    if not addr:
        return False

    for entry in denylist:
        token = entry.strip().lower()
        if token and token in addr:
            return True

    if not is_email_like(addr):
        return False

    # Local part only — strip any ``+tag`` suffix before the boundary match.
    local = addr.split("@", 1)[0].split("+", 1)[0]
    return _local_is_automated(local)


def _owner_variants(key: str) -> set[str]:
    """Derive the owner-key variants implied by a single owner identifier.

    Adds the raw key, its canonical form, the first-name-only canonical token,
    and (for emails) the bare local part — so an owner listed only as
    ``pat.owner@example.com`` or ``Pat Owner`` also matches the leaked
    first-name-only / local-part forms ``pat``.
    """
    variants: set[str] = set()
    k = key.strip().lower()
    if not k:
        return variants
    variants.add(k)

    if "@" in k and is_email_like(k):
        local = k.split("@", 1)[0]
        variants.add(local)
        local_first = _SEPARATORS_RE.sub(" ", local).strip().split(" ", 1)[0]
        if len(local_first) >= _MIN_NAME_LENGTH:
            variants.add(local_first)

    normalized = normalize_person_name(k)
    if normalized is not None:
        canonical = normalized.canonical_key
        variants.add(canonical)
        first = canonical.split(" ", 1)[0]
        if len(first) >= _MIN_NAME_LENGTH:
            variants.add(first)

    return variants


def expand_owner_keys(owner_keys: Iterable[str]) -> frozenset[str]:
    """Widen an owner-key set with first-name-only + email-local-part variants.

    The corpus owner must never surface as a person, even under a partial form
    (just their first name, or the local part of their email). This expands the
    configured ``BRAIN_OWNER_PARTICIPANTS`` set so the People Hub and graph
    reconcile owner filters catch those leaks. Pure + deterministic; every
    entry is lowercased.
    """
    expanded: set[str] = set()
    for key in owner_keys:
        expanded |= _owner_variants(key)
    return frozenset(expanded)
