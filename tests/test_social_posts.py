"""Pure post logic for `brain ingest-bookmarks` (RM-44): ids, dates, handles, the document."""
from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from brain.ingest import social_posts as sp
from tests import social_archive_fixtures as fx
from tests.secret_fixtures import SYNTHETIC_AWS_KEY

WRITTEN = datetime(2026, 9, 14, 8, 30, 15, 250000, tzinfo=UTC)
PID = fx.post_id_at(WRITTEN)


def _post(entry: dict, collection: str = "like") -> sp.SocialPost:
    post = sp.post_from_entry(entry, collection, now=fx.NOW)
    assert isinstance(post, sp.SocialPost), post
    return post


# --- the snowflake date (AC4) -------------------------------------------------------------------


def test_the_date_is_decoded_from_the_id() -> None:
    assert sp.snowflake_time(int(PID), now=fx.NOW) == WRITTEN


def test_the_sequence_bits_do_not_move_the_date() -> None:
    assert sp.snowflake_time(int(fx.post_id_at(WRITTEN, seq=4095)), now=fx.NOW) == WRITTEN


def test_the_ticket_formula_at_a_known_instant() -> None:
    """``(id >> 22) + 1288834974657`` ms since the Unix epoch, the ticket's own formula."""
    post_id = (1_700_000_000_000 - 1288834974657) << 22

    assert sp.snowflake_time(post_id, now=fx.NOW) == datetime(2023, 11, 14, 22, 13, 20, tzinfo=UTC)


@pytest.mark.parametrize("post_id", [1, 20, 29_700_000_000, (59_999 << 22) | 4095])
def test_a_pre_snowflake_id_is_undated(post_id: int) -> None:
    assert sp.snowflake_time(post_id, now=fx.NOW) is None


def test_the_first_dated_id_is_one_minute_after_the_epoch() -> None:
    first = sp.snowflake_time(60_000 << 22, now=fx.NOW)

    assert first == datetime.fromtimestamp((fx.TWEPOCH_MS + 60_000) / 1000, UTC)


def test_an_id_dated_after_tomorrow_is_refused() -> None:
    with pytest.raises(ValueError, match="future"):
        sp.snowflake_time(int(fx.post_id_at(fx.NOW + fx.DAY + timedelta(seconds=1))), now=fx.NOW)


def test_an_id_dated_within_a_day_ahead_is_kept() -> None:
    ahead = fx.NOW + timedelta(hours=23)

    assert sp.snowflake_time(int(fx.post_id_at(ahead)), now=fx.NOW) == ahead


# --- one entry -----------------------------------------------------------------------------------


def test_an_entry_becomes_a_post() -> None:
    post = _post(fx.fields(PID, expanded_url=fx.status_url(PID)))

    assert post == sp.SocialPost(
        post_id=PID, text=fx.TEXT, handle=fx.AUTHOR, written_at=WRITTEN,
        collections=frozenset({"like"}),
    )
    assert post.url == f"https://x.com/{fx.AUTHOR}/status/{PID}"
    assert post.external_id == f"x-{PID}"


@pytest.mark.parametrize(
    "post_id",
    ["", "12a", " 123", "123 ", "-1", "1" * 20, "1" * 5000, str(2**63), "١٢٣", "+5"],
)
def test_a_malformed_id_is_skipped(post_id: str) -> None:
    """The 19-digit bound runs before ``int()``: a 4301+ digit string would raise (rev-116)."""
    assert sp.post_from_entry(fx.fields(post_id), "like", now=fx.NOW) == sp.SKIP_MALFORMED


@pytest.mark.parametrize("bad", [123, None, 1e400, ["1"], {"id": "1"}])
def test_an_id_that_is_not_a_string_is_skipped(bad: object) -> None:
    assert sp.post_from_entry({"tweetId": bad, "fullText": "x"}, "like", now=fx.NOW) == (
        sp.SKIP_MALFORMED
    )


def test_an_entry_without_an_id_is_skipped() -> None:
    assert sp.post_from_entry({"fullText": "x"}, "like", now=fx.NOW) == sp.SKIP_MALFORMED


def test_the_largest_id_is_accepted_when_its_date_is_not_future() -> None:
    assert sp.post_from_entry(fx.fields(str(2**63 - 1)), "like", now=fx.NOW) == (
        sp.SKIP_MALFORMED  # 2**63 - 1 decodes to the year 2080: future
    )
    far = datetime(2100, 1, 1, tzinfo=UTC)
    post = sp.post_from_entry(fx.fields(str(2**63 - 1)), "like", now=far)
    assert isinstance(post, sp.SocialPost)


def test_a_future_id_is_skipped_as_malformed() -> None:
    future = fx.post_id_at(fx.NOW + 3 * fx.DAY)

    assert sp.post_from_entry(fx.fields(future), "like", now=fx.NOW) == sp.SKIP_MALFORMED


@pytest.mark.parametrize("text", [None, "", "   \n\t "])
def test_a_post_without_text_is_skipped(text: str | None) -> None:
    assert sp.post_from_entry(fx.fields(PID, text=text), "like", now=fx.NOW) == sp.SKIP_NO_TEXT


@pytest.mark.parametrize("text", [5, ["a"], {"a": 1}, True])
def test_text_that_is_not_a_string_is_skipped_as_malformed(text: object) -> None:
    entry = {"tweetId": PID, "fullText": text}

    assert sp.post_from_entry(entry, "like", now=fx.NOW) == sp.SKIP_MALFORMED


def test_text_over_the_cap_is_skipped_as_malformed() -> None:
    entry = fx.fields(PID, text="a" * (sp.MAX_TEXT_CHARS + 1))

    assert sp.post_from_entry(entry, "like", now=fx.NOW) == sp.SKIP_MALFORMED
    assert isinstance(
        sp.post_from_entry(fx.fields(PID, text="a" * sp.MAX_TEXT_CHARS), "like", now=fx.NOW),
        sp.SocialPost,
    )


def test_control_characters_are_dropped_and_newlines_kept() -> None:
    post = _post(fx.fields(PID, text="  line one\x00\x1b[31m\r\nline\ttwo\x7f​ \n"))

    assert post.text == "line one[31m\nline\ttwo"


def test_unknown_inner_keys_are_ignored() -> None:
    post = _post(fx.fields(PID, favoritedAt="2026-01-01", extra={"nested": [1, 2]}))

    assert post.text == fx.TEXT


def test_a_pre_snowflake_post_is_undated() -> None:
    post = _post(fx.fields("20"))

    assert post.written_at is None


def test_the_collection_is_recorded() -> None:
    assert _post(fx.fields(PID), "bookmark").collections == frozenset({"bookmark"})


def test_an_unknown_collection_is_a_programming_error() -> None:
    with pytest.raises(ValueError, match="collection"):
        sp.post_from_entry(fx.fields(PID), "retweet", now=fx.NOW)


# --- the handle and the URL (AC3) ----------------------------------------------------------------


@pytest.mark.parametrize(
    "host", ["x.com", "twitter.com", "www.x.com", "www.twitter.com", "mobile.twitter.com",
             "X.COM", "x.com."],
)
def test_the_handle_comes_from_a_status_url_on_the_platform(host: str) -> None:
    assert sp.handle_from_url(fx.status_url(PID, host=host), PID) == fx.AUTHOR


def test_the_query_fragment_and_userinfo_are_never_kept() -> None:
    host = "x.com"
    raw = f"https://user:pw@{host}/{fx.AUTHOR}/status/{PID}?s=20&t=SyntheticToken123#frag"

    post = _post(fx.fields(PID, expanded_url=raw))

    assert post.handle == fx.AUTHOR
    assert post.url == f"https://x.com/{fx.AUTHOR}/status/{PID}"
    document = sp.build_document(post)
    for leaked in ("SyntheticToken123", "s=20", "frag", "pw@", "user:"):
        assert leaked not in document.content
        assert leaked not in str(document.metadata)


def test_a_path_parameter_does_not_hide_the_handle() -> None:
    raw = f"https://x.com/{fx.AUTHOR};jsessionid=abc/status/{PID}"

    assert sp.handle_from_url(raw, PID) == fx.AUTHOR


@pytest.mark.parametrize(
    "raw",
    [
        None,
        42,
        "",
        "not a url",
        f"javascript:alert(1)//x.com/{fx.AUTHOR}/status/{PID}",
        f"https://evil.example.com/{fx.AUTHOR}/status/{PID}",
        f"https://x.com.example.com/{fx.AUTHOR}/status/{PID}",
        f"https://x.com/{fx.AUTHOR}/status/{PID}1",
        f"https://x.com/{fx.AUTHOR}/status/1{PID}",
        f"https://x.com/{fx.AUTHOR}/status/{PID}/photo/1",
        f"https://x.com/{fx.AUTHOR}/statuses/{PID}",
        f"https://x.com/i/web/status/{PID}",
        f"https://x.com/I/status/{PID}",
        f"https://x.com/{'a' * 16}/status/{PID}",
        f"https://x.com/bad-handle/status/{PID}",
        f"https://x.com/{fx.AUTHOR}/../status/{PID}",
        f"https://x.com/status/{PID}",
        f"ftp://x.com/{fx.AUTHOR}/status/{PID}",
        f"https://x.com:8443/{fx.AUTHOR}/status/{PID}",
    ],
)
def test_anything_else_yields_no_handle(raw: object) -> None:
    assert sp.handle_from_url(raw, PID) is None


def test_a_missing_handle_is_allowed() -> None:
    post = _post(fx.fields(PID))

    assert post.handle is None
    assert post.url == f"https://x.com/i/web/status/{PID}"


def test_an_off_platform_url_is_never_stored() -> None:
    post = _post(fx.fields(PID, expanded_url=f"https://evil.example.com/{fx.AUTHOR}/status/{PID}"))

    document = sp.build_document(post)

    assert "evil.example.com" not in document.content
    assert "evil.example.com" not in str(document.metadata)


# --- merging and collecting ----------------------------------------------------------------------


def test_one_post_liked_and_bookmarked_is_merged() -> None:
    liked = _post(fx.fields(PID), "like")
    bookmarked = _post(fx.fields(PID, text="ignored", expanded_url=fx.status_url(PID)), "bookmark")

    (merged,) = sp.merge_posts([liked, bookmarked])

    assert merged.collections == frozenset({"like", "bookmark"})
    assert merged.text == fx.TEXT
    assert merged.handle == fx.AUTHOR


def test_the_first_handle_wins() -> None:
    first = _post(fx.fields(PID, expanded_url=fx.status_url(PID, fx.AUTHOR)))
    second = _post(fx.fields(PID, expanded_url=fx.status_url(PID, fx.OTHER_AUTHOR)))

    (merged,) = sp.merge_posts([first, second])

    assert merged.handle == fx.AUTHOR


def test_collect_counts_the_skips_and_merges_the_rest() -> None:
    other = fx.post_id_at(WRITTEN - fx.DAY)
    likes = [fx.fields(PID), fx.fields(other, text=None), fx.fields("bad"), fx.fields(PID)]
    bookmarks = [fx.fields(PID), fx.fields(fx.post_id_at(WRITTEN, seq=7))]

    collected = sp.collect_posts(likes, bookmarks, now=fx.NOW)

    assert collected.no_text == 1
    assert collected.malformed == 1
    assert collected.liked == 1
    assert collected.bookmarked == 2
    by_id = {post.post_id: post for post in collected.posts}
    assert set(by_id) == {PID, fx.post_id_at(WRITTEN, seq=7)}
    assert by_id[PID].collections == frozenset({"like", "bookmark"})


# --- selection (AC6) -----------------------------------------------------------------------------


def _dated(days_ago: float, seq: int = 0) -> sp.SocialPost:
    return _post(fx.fields(fx.post_id_at(fx.NOW - timedelta(days=days_ago), seq)))


def test_selection_is_newest_first_with_undated_last() -> None:
    old, new, undated = _dated(5), _dated(1), _post(fx.fields("20"))

    batch = sp.select_posts([old, undated, new], since=None, limit=10)

    assert batch.posts == (new, old, undated)
    assert (batch.before_since, batch.undated, batch.remainder) == (0, 0, 0)


def test_ties_order_by_id_descending() -> None:
    a, b = _dated(1, seq=1), _dated(1, seq=2)

    assert sp.select_posts([a, b], since=None, limit=10).posts == (b, a)


def test_since_drops_older_posts_and_every_undated_one() -> None:
    since = fx.NOW - 3 * fx.DAY
    on_the_boundary = _post(fx.fields(fx.post_id_at(since)))
    just_before = _post(fx.fields(fx.post_id_at(since - timedelta(milliseconds=1))))
    newer = _dated(1)

    batch = sp.select_posts(
        [on_the_boundary, just_before, newer, _post(fx.fields("20"))], since=since, limit=10
    )

    assert batch.posts == (newer, on_the_boundary)
    assert batch.before_since == 1
    assert batch.undated == 1


def test_the_cap_keeps_the_newest_and_counts_the_rest() -> None:
    posts = [_dated(days) for days in (1, 2, 3, 4)]

    batch = sp.select_posts(posts, since=None, limit=3)

    assert batch.posts == tuple(posts[:3])
    assert batch.remainder == 1


# --- the document --------------------------------------------------------------------------------


def test_the_document_with_a_handle() -> None:
    post = _post(fx.fields(PID, expanded_url=fx.status_url(PID)))

    document = sp.build_document(post)

    url = f"https://x.com/{fx.AUTHOR}/status/{PID}"
    assert document.title == f"@{fx.AUTHOR}: {fx.TEXT}"
    assert document.content == f"@{fx.AUTHOR}\n\n{fx.TEXT}\n\n{url}"
    assert document.content_type == "social_post"
    assert document.source_path is None
    assert document.metadata == {
        "platform": "x",
        "post_id": PID,
        "url": url,
        "handle": fx.AUTHOR,
        "date": WRITTEN.isoformat(),
        "collections": ["like"],
    }


def test_the_document_without_a_handle_or_a_date() -> None:
    post = _post(fx.fields("20"), "bookmark")

    document = sp.build_document(post)

    assert document.title == fx.TEXT
    assert document.content == f"{fx.TEXT}\n\nhttps://x.com/i/web/status/20"
    assert "handle" not in document.metadata
    assert "date" not in document.metadata
    assert document.metadata["collections"] == ["bookmark"]


def test_a_long_title_is_cut_on_one_line() -> None:
    text = "word " * 40 + "\nsecond line"
    post = _post(fx.fields(PID, text=text, expanded_url=fx.status_url(PID)))

    title = sp.build_document(post).title

    assert "\n" not in title
    assert title.endswith("…")
    assert len(title) == len(f"@{fx.AUTHOR}: ") + sp.TITLE_CHARS_WITH_HANDLE + 1
    plain = sp.build_document(_post(fx.fields(PID, text=text))).title
    assert len(plain) == sp.TITLE_CHARS + 1


def test_the_body_does_not_depend_on_the_collection() -> None:
    liked = sp.build_document(_post(fx.fields(PID), "like"))
    both = sp.build_document(sp.merge_posts([
        _post(fx.fields(PID), "like"), _post(fx.fields(PID), "bookmark")
    ])[0])

    assert liked.title == both.title
    assert liked.content == both.content
    assert both.metadata["collections"] == ["bookmark", "like"]


def test_volatile_metadata_unions_the_stored_collections() -> None:
    post = _post(fx.fields(PID), "like")

    assert sp.volatile_metadata(post, {"collections": ["bookmark"]}) == {
        "collections": ["bookmark", "like"]
    }
    assert sp.volatile_metadata(post, {}) == {"collections": ["like"]}


@pytest.mark.parametrize("stored", [{"collections": "like"}, {"collections": [1, "evil"]},
                                    {"collections": None}])
def test_a_malformed_stored_list_keeps_only_known_names(stored: dict) -> None:
    post = _post(fx.fields(PID), "bookmark")

    assert sp.volatile_metadata(post, stored) == {"collections": ["bookmark"]}


def test_the_volatile_keys() -> None:
    assert sp.VOLATILE_KEYS == ("collections",)


# --- sec-116 F1: a secret never reaches the title -------------------------------------------------


def test_a_secret_at_the_start_of_a_post_is_redacted_in_the_title() -> None:
    post = _post(fx.fields(PID, text=f"{SYNTHETIC_AWS_KEY} leaked by accident",
                           expanded_url=fx.status_url(PID)))

    title = sp.build_document(post).title

    assert SYNTHETIC_AWS_KEY not in title
    assert "AKIA" not in title
    assert title.startswith(f"@{fx.AUTHOR}: [REDACTED:")


def test_a_secret_across_the_title_cut_is_redacted_before_cutting() -> None:
    text = "a" * (sp.TITLE_CHARS - 5) + f" {SYNTHETIC_AWS_KEY}"
    post = _post(fx.fields(PID, text=text))

    title = sp.build_document(post).title

    assert text[: sp.TITLE_CHARS].endswith("AKIA")  # a cut-then-redact title would keep it
    assert "AKIA" not in title


# --- sec-116 F3: one canonical id per post --------------------------------------------------------


@pytest.mark.parametrize("post_id", ["0160000000000000000", "0", "00020", "0" * 19])
def test_an_id_with_a_leading_zero_is_malformed(post_id: str) -> None:
    """sec-116 F3: ``0160…`` and ``160…`` are one post; only the canonical form is accepted."""
    assert isinstance(sp.post_from_entry(fx.fields("160000000000000000"), "like", now=fx.NOW),
                      sp.SocialPost)
    assert sp.post_from_entry(fx.fields(post_id), "like", now=fx.NOW) == sp.SKIP_MALFORMED
