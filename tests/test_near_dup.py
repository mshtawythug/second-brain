"""Pure tests for :mod:`brain.near_dup` (RM-19): shingles, MinHash, bands, verification.

Every text is synthetic, built from the word list below with a fixed-seed generator, so no test
depends on real content.
"""
from __future__ import annotations

import hashlib
import random
import uuid

import pytest

from brain import near_dup
from brain.set_similarity import jaccard

_VOCAB = [
    "alpha", "bravo", "charlie", "delta", "echo", "foxtrot", "golf", "hotel", "india",
    "juliet", "kilo", "lima", "mike", "november", "oscar", "papa", "quebec", "romeo",
    "sierra", "tango", "uniform", "victor", "whiskey", "xray", "yankee", "zulu",
    "amber", "basil", "cedar", "dune", "ember", "fjord", "grove", "harbor", "islet",
    "jade", "knoll", "lagoon", "meadow", "nectar", "orchid", "pebble", "quartz",
]

_BIGINT_MIN = -(2**63)
_BIGINT_MAX = 2**63 - 1


def _text(words: int, seed: int) -> str:
    rng = random.Random(seed)
    return " ".join(rng.choice(_VOCAB) for _ in range(words))


def _with_sentence_changed(text: str) -> str:
    """The same text with one nine-word sentence in the middle replaced."""
    words = text.split()
    middle = len(words) // 2
    replacement = [
        "this", "sentence", "was", "rewritten", "entirely", "by", "the", "second", "sender",
    ]
    return " ".join(words[:middle] + replacement + words[middle + len(replacement) :])


# --- words, tokens, shingles -------------------------------------------------------------


def test_words_lowercase_and_collapse_whitespace() -> None:
    assert near_dup.words("Alpha  BRAVO\n\tcharlie ") == ["alpha", "bravo", "charlie"]


def test_token_count_is_the_word_count() -> None:
    assert near_dup.token_count("one two   three\nfour") == 4


def test_shingles_are_five_word_windows_hashed_to_64_bits() -> None:
    text = "a b c d e f"
    expected = {
        int.from_bytes(hashlib.blake2b(s.encode(), digest_size=8).digest(), "big")
        for s in ("a b c d e", "b c d e f")
    }

    assert near_dup.shingles(text) == expected


def test_shingles_ignore_case_and_whitespace_runs() -> None:
    assert near_dup.shingles("A b  C d\nE f") == near_dup.shingles("a b c d e f")


def test_fewer_than_five_words_give_no_shingles() -> None:
    assert near_dup.shingles("one two three four") == frozenset()


# --- signature -------------------------------------------------------------------------


def test_signature_has_128_values_below_the_mersenne_prime() -> None:
    sig = near_dup.signature(near_dup.shingles(_text(200, 1)))

    assert len(sig) == near_dup.NUM_PERM == 128
    assert all(0 <= value < near_dup.MERSENNE_61 for value in sig)


def test_signature_is_deterministic_across_calls() -> None:
    shingle_set = near_dup.shingles(_text(300, 2))

    assert near_dup.signature(shingle_set) == near_dup.signature(set(shingle_set))


def test_hash_parameters_are_fixed_and_pinned() -> None:
    # The seeds derive from blake2b, never ``random`` or ``hash()``: a changed derivation would
    # silently invalidate every stored signature, so its first value is pinned here.
    a0, b0 = near_dup.hash_parameters()[0]
    digest_a = hashlib.blake2b(f"{near_dup.SEED}:0:a".encode(), digest_size=8).digest()
    digest_b = hashlib.blake2b(f"{near_dup.SEED}:0:b".encode(), digest_size=8).digest()

    assert a0 == int.from_bytes(digest_a, "big") % (near_dup.MERSENNE_61 - 1) + 1
    assert b0 == int.from_bytes(digest_b, "big") % near_dup.MERSENNE_61
    assert len(near_dup.hash_parameters()) == 128


def test_signature_of_the_empty_set_is_refused() -> None:
    with pytest.raises(ValueError):
        near_dup.signature(frozenset())


# --- band hashes and signed conversion ---------------------------------------------------


def test_thirty_two_bands_of_four_rows() -> None:
    assert (near_dup.BANDS, near_dup.ROWS) == (32, 4)
    assert near_dup.BANDS * near_dup.ROWS == near_dup.NUM_PERM


def test_band_hash_is_the_salted_blake2b_of_its_rows() -> None:
    sig = tuple(range(128))
    digest = hashlib.blake2b(
        (3).to_bytes(1, "big") + b"".join(v.to_bytes(8, "big") for v in sig[12:16]),
        digest_size=8,
    ).digest()

    assert near_dup.band_hashes(sig)[3] == int.from_bytes(digest, "big", signed=True)


def test_equal_value_runs_in_different_bands_hash_differently() -> None:
    # AC2: every band holds the same four values, so only the band-index salt separates them.
    sig = (7, 8, 9, 10) * 32

    bands = near_dup.band_hashes(sig)

    assert len(bands) == 32
    assert len(set(bands)) == 32


def test_to_signed64_maps_the_high_bit_negative() -> None:
    assert near_dup.to_signed64(b"\xff" * 8) == -1
    assert near_dup.to_signed64(b"\x80" + b"\x00" * 7) == _BIGINT_MIN
    assert near_dup.to_signed64(b"\x7f" + b"\xff" * 7) == _BIGINT_MAX


def test_band_hashes_refuse_a_signature_of_the_wrong_length() -> None:
    with pytest.raises(ValueError):
        near_dup.band_hashes((1, 2, 3))


def test_every_stored_value_fits_bigint_across_many_texts() -> None:
    # AC6: the signature values and the band hashes both land in BIGINT's range.
    for seed in range(40):
        minhash = near_dup.compute(_text(120, seed))
        assert minhash is not None
        assert all(_BIGINT_MIN <= v <= _BIGINT_MAX for v in minhash.signature)
        assert all(_BIGINT_MIN <= v <= _BIGINT_MAX for v in minhash.bands)
    negatives = [
        v for seed in range(40) for v in near_dup.compute(_text(120, seed)).bands  # type: ignore[union-attr]
        if v < 0
    ]
    assert negatives, "some band hash must have the high bit set, or signed conversion is moot"


def test_compute_is_deterministic_across_runs() -> None:
    # AC6: fixed seeds, so two computations of one text agree exactly.
    text = _text(400, 9)

    assert near_dup.compute(text) == near_dup.compute(text)


# --- the estimate and the verification -----------------------------------------------------


@pytest.mark.parametrize("seed", range(6))
def test_estimate_tracks_the_exact_jaccard(seed: int) -> None:
    base = _text(400, 100 + seed)
    rng = random.Random(seed)
    words = base.split()
    for index in rng.sample(range(len(words)), 25):
        words[index] = "zz" + words[index]
    variant = " ".join(words)
    exact = jaccard(near_dup.shingles(base), near_dup.shingles(variant))

    estimate = near_dup.estimated_jaccard(
        near_dup.signature(near_dup.shingles(base)),
        near_dup.signature(near_dup.shingles(variant)),
    )

    # 128 permutations: the standard error is about sqrt(J(1-J)/128) <= 0.045.
    assert abs(estimate - exact) < 0.15


def test_one_sentence_changed_in_400_words_verifies() -> None:
    # AC1 (pure half).
    base = _text(400, 21)
    a = near_dup.compute(base)
    b = near_dup.compute(_with_sentence_changed(base))

    assert a is not None and b is not None
    assert near_dup.verifies(a.signature, b.signature)
    assert set(a.bands) & set(b.bands), "a near-copy must share at least one band"


def test_unrelated_texts_do_not_verify() -> None:
    a = near_dup.compute(_text(400, 31))
    b = near_dup.compute(_text(400, 32))

    assert a is not None and b is not None
    assert not near_dup.verifies(a.signature, b.signature)


def test_verification_cut_is_inclusive_at_085() -> None:
    sig = tuple(range(128))
    # 109/128 = 0.8516 agree (passes); 108/128 = 0.84375 (fails).
    passing = sig[:109] + tuple(v + 10_000 for v in sig[109:])
    failing = sig[:108] + tuple(v + 10_000 for v in sig[108:])

    assert near_dup.JACCARD_CUT == 0.85
    assert near_dup.verifies(sig, passing)
    assert not near_dup.verifies(sig, failing)


def test_estimated_jaccard_refuses_mismatched_lengths() -> None:
    with pytest.raises(ValueError):
        near_dup.estimated_jaccard((1, 2), (1, 2, 3))


# --- the floor, threads, ordering ---------------------------------------------------------


def test_a_30_token_stub_is_skipped() -> None:
    # AC1: under the 50-token floor nothing is computed.
    assert near_dup.MIN_TOKENS == 50
    assert near_dup.compute(_text(30, 4)) is None


def test_the_floor_is_inclusive_at_50_tokens() -> None:
    assert near_dup.compute(_text(49, 5)) is None
    assert near_dup.compute(_text(50, 5)) is not None


@pytest.mark.parametrize(
    ("a", "b", "expected"),
    [
        ("t1", "t1", True),
        ("t1", "t2", False),
        (None, None, False),
        ("t1", None, False),
        (None, "t1", False),
    ],
)
def test_same_thread(a: str | None, b: str | None, expected: bool) -> None:
    assert near_dup.same_thread(a, b) is expected


def test_ordered_pair_puts_the_smaller_uuid_first() -> None:
    low = "00000000-0000-0000-0000-000000000001"
    high = "ffffffff-0000-0000-0000-000000000000"

    assert near_dup.ordered_pair(high, low) == (low, high)
    assert near_dup.ordered_pair(low, high) == (low, high)


def test_ordered_pair_matches_postgres_uuid_order() -> None:
    # Postgres orders uuid by its bytes; uuid.UUID compares by the same 128-bit integer.
    ids = [str(uuid.UUID(int=random.Random(s).getrandbits(128))) for s in range(30)]
    for a in ids:
        for b in ids:
            if a == b:
                continue
            first, second = near_dup.ordered_pair(a, b)
            assert uuid.UUID(first).bytes < uuid.UUID(second).bytes


def test_ordered_pair_refuses_a_document_paired_with_itself() -> None:
    same = "00000000-0000-0000-0000-000000000001"
    with pytest.raises(ValueError):
        near_dup.ordered_pair(same, same)
