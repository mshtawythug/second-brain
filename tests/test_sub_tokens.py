"""Unit tests for ``brain.ingest.sub_tokens.extract_sub_tokens``."""
from brain.ingest.sub_tokens import _NOISE_TOKENS, extract_sub_tokens


def _tokens(text: str) -> list[str]:
    """Helper — split the function's whitespace-joined output into a list."""
    out = extract_sub_tokens(text)
    return out.split() if out else []


# ---------------------------------------------------------------------------
# Module-level constant exists (required by Phase A spec).
# ---------------------------------------------------------------------------
def test_noise_tokens_constant_is_a_frozenset_with_expected_entries() -> None:
    assert isinstance(_NOISE_TOKENS, frozenset)
    # Every entry should be lowercase since we compare via .lower().
    for token in _NOISE_TOKENS:
        assert token == token.lower()
    # Spot-check that the headline noise TLDs are in the blocklist.
    for expected in {"com", "org", "net", "io", "co", "gov", "edu"}:
        assert expected in _NOISE_TOKENS


# ---------------------------------------------------------------------------
# Empty / no-match inputs short-circuit to "".
# ---------------------------------------------------------------------------
def test_empty_string_returns_empty() -> None:
    assert extract_sub_tokens("") == ""


def test_plain_text_with_no_urls_returns_empty() -> None:
    assert extract_sub_tokens("plain text with no urls") == ""


# ---------------------------------------------------------------------------
# Spec examples from the Phase A brief.
# ---------------------------------------------------------------------------
def test_email_plus_hostname_example() -> None:
    text = "contact person-b@example-group.com or visit example.com/groups"
    tokens = _tokens(text)
    # Required sub-tokens.
    assert "person-b" in tokens
    assert "example-group" in tokens
    assert "example" in tokens
    # Noise TLDs / suffixes are dropped.
    assert "com" not in tokens
    assert "io" not in tokens


def test_url_components_example() -> None:
    text = (
        "https://files-example.s3-amazonaws.test/recording/example-team/123.mp3"
    )
    tokens = _tokens(text)
    for expected in {
        "files-example",
        "s3-amazonaws",
        "recording",
        "example-team",
    }:
        assert expected in tokens, f"missing {expected!r} from {tokens}"
    # Numeric "123" is digits-only and must be filtered.
    assert "123" not in tokens
    # "com" is a noise TLD.
    assert "com" not in tokens


# ---------------------------------------------------------------------------
# Mixed punctuation around the captured artefact.
# ---------------------------------------------------------------------------
def test_email_inside_parens() -> None:
    tokens = _tokens("(person-b@example-group.com)")
    assert "person-b" in tokens
    assert "example-group" in tokens


def test_email_with_trailing_period() -> None:
    tokens = _tokens("Email person-b@example-group.com.")
    assert "person-b" in tokens
    assert "example-group" in tokens


def test_url_with_trailing_punctuation() -> None:
    tokens = _tokens("see https://example-group.test/groups/g/worldwide).")
    assert "example-group" in tokens
    assert "groups" in tokens
    assert "worldwide" in tokens


# ---------------------------------------------------------------------------
# Multiple emails / URLs in one string.
# ---------------------------------------------------------------------------
def test_multiple_emails_and_urls() -> None:
    text = (
        "From: alice@foo-bar.example reach https://acme.example.org/path/page "
        "or bob+work@team-site.example."
    )
    tokens = _tokens(text)
    for expected in {
        "alice",
        "foo-bar",
        "example",
        "acme",
        "path",
        "page",
        "bob",
        "work",
        "team-site",
    }:
        assert expected in tokens, f"missing {expected!r} from {tokens}"
    # First-seen-order dedup: 'example' should appear only once even though
    # it occurs in three captures.
    assert tokens.count("example") == 1


def test_local_part_split_on_dot_plus_underscore() -> None:
    # Splitter is ``.`` ``+`` ``_`` ``@`` etc — but NOT ``-``. Hyphenated
    # words like ``tag-suffix`` stay together so we don't fragment compound
    # tokens like ``files-example`` later.
    text = "first.last+tag-suffix_more@host.test"
    tokens = _tokens(text)
    assert "first" in tokens
    assert "last" in tokens
    assert "tag-suffix" in tokens
    assert "more" in tokens
    assert "host" in tokens


# ---------------------------------------------------------------------------
# Determinism + idempotence.
# ---------------------------------------------------------------------------
def test_output_is_deterministic_for_same_input() -> None:
    text = "person-b@example.com and example.com/groups"
    assert extract_sub_tokens(text) == extract_sub_tokens(text)


def test_first_seen_order_preserved() -> None:
    # 'person-b' appears before 'example-group'; output should reflect that.
    text = "person-b@example-group.com"
    tokens = _tokens(text)
    assert tokens.index("person-b") < tokens.index("example-group")


def test_idempotence_running_twice_does_not_crash() -> None:
    once = extract_sub_tokens("person-b@example.com")
    twice = extract_sub_tokens(once)
    # Output of the second pass is a subset (likely empty) — there are no
    # emails/URLs/dotted-hosts left in `once`.
    assert isinstance(twice, str)
    # All tokens in `twice` must have appeared in `once` (sane subset).
    once_tokens = set(once.split())
    twice_tokens = set(twice.split())
    assert twice_tokens.issubset(once_tokens)


# ---------------------------------------------------------------------------
# Filters — short tokens, digits-only, noise TLDs.
# ---------------------------------------------------------------------------
def test_short_tokens_filtered() -> None:
    # "a@b.cd" → local "a" is len 1, drop. "b" is len 1, drop. "cd" is len 2, keep.
    tokens = _tokens("a@b.cd")
    assert "a" not in tokens
    assert "b" not in tokens
    assert "cd" in tokens


def test_digits_only_filtered() -> None:
    text = "https://example-host.test/path/2026/page"
    tokens = _tokens(text)
    assert "2026" not in tokens
    assert "path" in tokens
    assert "page" in tokens


def test_noise_tld_blocklist_drops_each_entry() -> None:
    for noise in {"com", "org", "net", "io", "co", "gov", "edu"}:
        text = f"name@example.{noise}"
        tokens = _tokens(text)
        assert noise not in tokens, (
            f"noise TLD {noise!r} leaked through filter"
        )


# ---------------------------------------------------------------------------
# Unicode safety.
# ---------------------------------------------------------------------------
def test_unicode_local_part_and_path() -> None:
    # Non-ASCII letters in a URL path. The function must not crash and
    # should produce *some* output (the URL itself is captured).
    text = "see https://example.test/café/menu"
    out = extract_sub_tokens(text)
    assert isinstance(out, str)
    # We can't strictly require 'café' to survive since \w semantics depend
    # on regex flags — but we DO require no exception and non-empty output
    # for the non-noisy 'menu' / 'example' / 'test' tokens.
    tokens = out.split()
    assert "menu" in tokens or "example" in tokens or "test" in tokens


def test_unicode_only_no_urls() -> None:
    assert extract_sub_tokens("café résumé naïve") == ""


# ---------------------------------------------------------------------------
# Scheme stripping — 'http'/'https' are never emitted as sub-tokens.
# ---------------------------------------------------------------------------
def test_scheme_not_emitted_as_sub_token() -> None:
    tokens = _tokens("https://example-host.test/somepath")
    assert "https" not in tokens
    assert "http" not in tokens
    assert "example-host" in tokens


# ---------------------------------------------------------------------------
# Bare hostname with no scheme still produces sub-tokens (the regex
# captures dotted word groups too).
# ---------------------------------------------------------------------------
def test_bare_hostname_emits_components() -> None:
    tokens = _tokens("groups.example-group.test")
    assert "example-group" in tokens
    assert "groups" in tokens
    assert "io" not in tokens  # noise TLD


def test_bare_ip_like_dotted_numbers_are_filtered() -> None:
    # Each part is digits-only → filtered out entirely.
    tokens = _tokens("1.2.3.4")
    assert tokens == []
