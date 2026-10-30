"""Pure MinHash near-duplicate detection: shingles, signatures, LSH bands, verification (RM-19).

No I/O and no new dependency: ``hashlib.blake2b`` hashes the shingles and the bands, and the 128
universal hashes are plain integer arithmetic modulo the Mersenne prime 2^61 - 1. The SQL that
stores and pairs signatures is :mod:`brain.near_dup_store`.

- **Words** are the lowercased, whitespace-collapsed body (``text.lower().split()``). The
  50-token floor (:data:`MIN_TOKENS`) counts these same words, so the floor and the shingles mean
  one thing (RM-19 plan, R1).
- **Shingles** are runs of 5 words, each hashed to 64 bits.
- **The signature** is 128 minima of ``(a_i * x + b_i) mod p``. The parameters derive from
  ``blake2b`` of a fixed seed, never ``random`` or ``hash()``, so a signature is the same in
  every process and Python version (AC6). Every value is below 2^61 and fits ``BIGINT``.
- **Bands:** 32 bands of 4 rows. A band's hash salts its four values with the band index, so
  equal values in two bands never collide (AC2), and is converted to a SIGNED 64-bit integer so it
  fits ``BIGINT``. Two documents are candidates when any band hash matches; the S-curve threshold is
  about (1/32)^(1/4) ~= 0.42, well below the cut.
- **Verification:** a candidate pair is a near-duplicate only when the signatures' estimated
  Jaccard is at least :data:`JACCARD_CUT` (0.85, the roadmap's figure).

Plan: ``docs/plans/2026-10-30-rm-19-near-duplicates.md``.
"""
from __future__ import annotations

import hashlib
import uuid
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from functools import cache

#: Words per shingle.
SHINGLE_WORDS = 5
#: Values in a signature.
NUM_PERM = 128
#: LSH bands, and rows (signature values) per band.
BANDS = 32
ROWS = 4
#: The Mersenne prime 2^61 - 1, the universal hashes' modulus.
MERSENNE_61 = (1 << 61) - 1
#: Seed of the hash parameters. Changing it invalidates every stored signature.
SEED = "brain-near-dup-v1"
#: Documents with fewer words are never signed or paired (templates and stubs collide).
MIN_TOKENS = 50
#: The estimated Jaccard a candidate pair must reach to be recorded.
JACCARD_CUT = 0.85

_DIGEST_SIZE = 8


@dataclass(frozen=True)
class MinHash:
    """A document's signature and its band hashes, both ready for ``BIGINT[]``."""

    signature: tuple[int, ...]
    bands: tuple[int, ...]


def words(text: str) -> list[str]:
    """The lowercased words of ``text``, every whitespace run collapsed."""
    return text.lower().split()


def token_count(text: str) -> int:
    """The number of words, the unit of :data:`MIN_TOKENS`."""
    return len(words(text))


def _digest(data: bytes) -> bytes:
    return hashlib.blake2b(data, digest_size=_DIGEST_SIZE).digest()


def shingles(text: str) -> frozenset[int]:
    """The 64-bit hashes of every :data:`SHINGLE_WORDS`-word run; empty under 5 words."""
    tokens = words(text)
    return frozenset(
        int.from_bytes(_digest(" ".join(tokens[i : i + SHINGLE_WORDS]).encode()), "big")
        for i in range(len(tokens) - SHINGLE_WORDS + 1)
    )


@cache
def hash_parameters() -> tuple[tuple[int, int], ...]:
    """The :data:`NUM_PERM` fixed ``(a, b)`` pairs: ``a`` in ``[1, p-1]``, ``b`` in ``[0, p-1]``."""
    params = []
    for i in range(NUM_PERM):
        a = int.from_bytes(_digest(f"{SEED}:{i}:a".encode()), "big") % (MERSENNE_61 - 1) + 1
        b = int.from_bytes(_digest(f"{SEED}:{i}:b".encode()), "big") % MERSENNE_61
        params.append((a, b))
    return tuple(params)


def signature(shingle_set: Iterable[int]) -> tuple[int, ...]:
    """The 128-value MinHash signature of a non-empty shingle set.

    Raises:
        ValueError: the set is empty (there is no minimum to take).
    """
    values = [x % MERSENNE_61 for x in shingle_set]
    if not values:
        raise ValueError("cannot sign an empty shingle set")
    return tuple(min([(a * x + b) % MERSENNE_61 for x in values]) for a, b in hash_parameters())


def to_signed64(digest: bytes) -> int:
    """An 8-byte digest as a signed big-endian integer, so it fits ``BIGINT``."""
    return int.from_bytes(digest, "big", signed=True)


def band_hashes(sig: Sequence[int]) -> tuple[int, ...]:
    """The :data:`BANDS` band hashes of ``sig``, each salted with its band index.

    Raises:
        ValueError: ``sig`` does not hold :data:`NUM_PERM` values.
    """
    if len(sig) != NUM_PERM:
        raise ValueError(f"a signature has {NUM_PERM} values (got {len(sig)})")
    return tuple(
        to_signed64(
            _digest(
                band.to_bytes(1, "big")
                + b"".join(v.to_bytes(8, "big") for v in sig[band * ROWS : (band + 1) * ROWS])
            )
        )
        for band in range(BANDS)
    )


def estimated_jaccard(sig_a: Sequence[int], sig_b: Sequence[int]) -> float:
    """The fraction of positions where two signatures agree.

    Raises:
        ValueError: the signatures differ in length.
    """
    if len(sig_a) != len(sig_b) or not sig_a:
        raise ValueError("signatures must be non-empty and of equal length")
    return sum(1 for a, b in zip(sig_a, sig_b, strict=True) if a == b) / len(sig_a)


def verifies(sig_a: Sequence[int], sig_b: Sequence[int]) -> bool:
    """Whether two signatures are near-duplicates (estimated Jaccard >= :data:`JACCARD_CUT`)."""
    return estimated_jaccard(sig_a, sig_b) >= JACCARD_CUT


def same_thread(thread_a: str | None, thread_b: str | None) -> bool:
    """Whether two documents share a Gmail thread (never paired; thread assembly owns them)."""
    return thread_a is not None and thread_a == thread_b


def ordered_pair(doc_a: str, doc_b: str) -> tuple[str, str]:
    """The pair in ``near_duplicates`` order (``doc_a < doc_b`` as Postgres orders ``uuid``).

    Raises:
        ValueError: the two ids are the same document.
    """
    first, second = uuid.UUID(doc_a), uuid.UUID(doc_b)
    if first == second:
        raise ValueError("a document cannot be paired with itself")
    return (str(first), str(second)) if first < second else (str(second), str(first))


def compute(text: str) -> MinHash | None:
    """The document's :class:`MinHash`, or ``None`` under :data:`MIN_TOKENS` words."""
    if token_count(text) < MIN_TOKENS:
        return None
    sig = signature(shingles(text))
    return MinHash(signature=sig, bands=band_hashes(sig))
