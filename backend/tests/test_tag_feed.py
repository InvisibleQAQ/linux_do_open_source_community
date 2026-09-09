"""Tests for the tag feed reader.

Three failure modes, none of which announces itself:

  * **A dropped topic.** The feed window is thirty items with no pagination, so a
    topic this parser skips is never seen again. Nothing errors — the project
    simply never appears.
  * **A leaked trailer.** Discourse appends a post-count line and a "read full
    topic" link to every `<description>`. Both are translated per instance, so a
    rule that matched their English wording would keep passing on `meta.discourse.org`
    and silently stop working on a Chinese forum, pushing boilerplate into every
    `cleaned_text` the model reads.
  * **A foreign row.** `canonical_url` is rendered as a link in the frontend and the
    topic id is a database key, so an item belonging to another host must not
    produce a row at all.

Fixtures mirror the live structure verified against `meta.discourse.org` on
2026-09-08 and recorded in the task's `research/discourse-tag-rss.md`: RSS 2.0,
`<guid isPermaLink="false">` of the form `{host}-topic-{id}`, and the first post's
complete cooked HTML inside `<description>`.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from pathlib import Path

import pytest

from linuxdo_oss.feeds.tag_feed import (
    FIRST_POST_NUMBER,
    canonical_topic_url,
    feed_host,
    parse_tag_feed,
    read_tag_feed,
    strip_generated_trailer,
    topic_id_from_guid,
)
from linuxdo_oss.feeds.xml_safe import MAX_FEED_BYTES, XmlSafetyError

FIXTURES = Path(__file__).parent / "fixtures"

# The configured source, and the only URL the reader is ever allowed to fetch.
FEED_URL = "https://forum.example/tag/oss/42.rss"
HOST = "forum.example"


def feed(name: str) -> str:
    return (FIXTURES / name).read_text(encoding="utf-8")


def parse(name: str) -> list:
    return parse_tag_feed(FEED_URL, feed(name), max_bytes=MAX_FEED_BYTES)


# ----------------------------------------------------------------------
# Host derivation — the trust anchor
# ----------------------------------------------------------------------


@pytest.mark.parametrize(
    ("url", "expected"),
    [
        ("https://forum.example/tag/oss/42.rss", "forum.example"),
        ("https://FORUM.EXAMPLE/tag/oss/42.rss", "forum.example"),
        ("https://forum.example:8443/tag/oss/42.rss", "forum.example"),
        ("https://linux.do/tag/2234-tag/2234.rss", "linux.do"),
    ],
)
def test_feed_host_comes_from_the_configured_url(url: str, expected: str) -> None:
    assert feed_host(url) == expected


def test_feed_host_rejects_a_url_without_one() -> None:
    with pytest.raises(ValueError, match="no host"):
        feed_host("not-a-url")


def test_canonical_url_is_built_from_host_and_id() -> None:
    """Never copied from the feed: it is stored and rendered as a link."""
    assert canonical_topic_url(HOST, 1001) == "https://forum.example/t/topic/1001"
    assert canonical_topic_url("linux.do", 2837720) == "https://linux.do/t/topic/2837720"


# ----------------------------------------------------------------------
# Identity — guid parsing
# ----------------------------------------------------------------------


@pytest.mark.parametrize(
    "guid",
    [
        "forum.example-topic-1001",
        "FORUM.EXAMPLE-TOPIC-1001",
        "  forum.example-topic-1001  ",
    ],
)
def test_topic_id_is_read_from_a_matching_guid(guid: str) -> None:
    assert topic_id_from_guid(guid, host=HOST) == 1001


@pytest.mark.parametrize(
    "guid",
    [
        None,
        "",
        # A different host entirely.
        "evil.test-topic-1001",
        # The expected host as a SUFFIX of a longer one — the lookalike that a
        # naive `endswith` or `(.+)-topic-(\d+)` split would accept.
        "notforum.example-topic-1001",
        # The expected host as a PREFIX of a longer one.
        "forum.example.evil.test-topic-1001",
        # Right shape, wrong separator content.
        "forum.example-post-1001",
        "forum.example-topic-abc",
        "forum.example-topic-",
        # Ids a database key may not take.
        "forum.example-topic-0",
        "forum.example-topic--5",
    ],
)
def test_unusable_guids_yield_no_id(guid: str | None) -> None:
    assert topic_id_from_guid(guid, host=HOST) is None


def test_a_hyphenated_host_is_not_split_at_the_wrong_hyphen() -> None:
    """`(.+)-topic-(\\d+)` would happily match the wrong prefix here."""
    assert topic_id_from_guid("my-forum.example-topic-7", host="my-forum.example") == 7
    assert topic_id_from_guid("my-forum.example-topic-7", host="forum.example") is None


def test_an_id_beyond_the_safe_integer_range_is_refused() -> None:
    assert topic_id_from_guid(f"{HOST}-topic-{2**53}", host=HOST) is None
    assert topic_id_from_guid(f"{HOST}-topic-{2**53 - 1}", host=HOST) == 2**53 - 1


# ----------------------------------------------------------------------
# Trailer stripping — by structure, never by wording
# ----------------------------------------------------------------------

LINK = "https://forum.example/t/topic/1001"


def test_english_trailer_is_stripped() -> None:
    body = (
        "<p>Body.</p>\n"
        "<p><small>26 posts - 9 participants</small></p>\n"
        f'<p><a href="{LINK}">Read full topic</a></p>\n'
    )

    assert strip_generated_trailer(body, LINK) == "<p>Body.</p>"


def test_translated_trailer_is_stripped_the_same_way() -> None:
    """THE test for this module's trailer rule.

    Same structure, no English anywhere. A matcher keyed on wording passes the test
    above and fails this one — which is the production case, since linux.do serves
    Chinese.
    """
    body = (
        "<p>正文。</p>\n"
        "<p><small>26 个帖子 - 9 位用户</small></p>\n"
        f'<p><a href="{LINK}">阅读完整话题</a></p>\n'
    )

    assert strip_generated_trailer(body, LINK) == "<p>正文。</p>"


def test_a_trailing_slash_on_the_link_still_matches() -> None:
    body = f'<p>Body.</p><p><a href="{LINK}/">Read full topic</a></p>'

    assert strip_generated_trailer(body, LINK) == "<p>Body.</p>"


def test_a_body_link_to_the_same_topic_is_not_stripped() -> None:
    """Only the FINAL paragraph is a trailer; an in-body self-link is content."""
    body = f'<p>See <a href="{LINK}">my earlier post</a> for context.</p><p>More.</p>'

    assert strip_generated_trailer(body, LINK) == body


def test_a_body_paragraph_that_starts_with_a_self_link_survives_the_real_trailer() -> None:
    """The regression the `\\Z` anchor invites, and the one that costs a whole post.

    The trailer pattern must end at the end of the string. A lazy `.*?` label
    starting at THIS paragraph can satisfy that by swallowing everything between it
    and the genuine trailer, so the body — GitHub link and all — disappears and the
    topic is then dropped as empty. Only forbidding the label from crossing `</p>`
    keeps the match confined to one paragraph.
    """
    body = (
        f'<p><a href="{LINK}">my own topic</a></p>\n'
        "<p>Real content https://github.com/owner/repo</p>\n"
        "<p><small>26 posts - 9 participants</small></p>\n"
        f'<p><a href="{LINK}">Read full topic</a></p>\n'
    )

    assert strip_generated_trailer(body, LINK) == (
        f'<p><a href="{LINK}">my own topic</a></p>\n'
        "<p>Real content https://github.com/owner/repo</p>"
    )


def test_a_trailer_label_carrying_markup_across_lines_is_still_stripped() -> None:
    """The reason the label is spanned lazily rather than by `[^<]*`: some themes
    wrap it in an element, and Discourse pretty-prints the markup."""
    body = f'<p>Body.</p><p><a href="{LINK}"><span>Read\nfull\ntopic</span></a></p>'

    assert strip_generated_trailer(body, LINK) == "<p>Body.</p>"


def test_a_github_link_in_the_last_paragraph_survives() -> None:
    """The stripper must never eat the thing the whole project exists to find."""
    body = '<p>Body.</p><p><a href="https://github.com/owner/repo">owner/repo</a></p>'

    assert strip_generated_trailer(body, LINK) == body


def test_an_unrecognised_trailer_is_left_alone() -> None:
    """Degrades to noise, never to an exception or to lost content."""
    body = "<p>Body.</p><div>something else entirely</div>"

    assert strip_generated_trailer(body, LINK) == body


def test_stripping_without_a_link_still_removes_the_count_line() -> None:
    body = "<p>Body.</p><p><small>3 posts - 2 participants</small></p>"

    assert strip_generated_trailer(body, None) == "<p>Body.</p>"


# ----------------------------------------------------------------------
# Parsing a whole feed
# ----------------------------------------------------------------------


def test_every_well_formed_topic_is_returned_in_feed_order() -> None:
    """Feed order is `bumped_at` descending and is preserved, not re-sorted.

    The fixture's second item is from 2024 and sits between two 2026 items, exactly
    as a bumped old topic does live. Sorting by `published_at` here would invent an
    order the feed did not express.
    """
    topics = parse("tag_feed.xml")

    assert [topic.topic_id for topic in topics] == [1001, 1002, 1003]


def test_topic_metadata_comes_from_the_item() -> None:
    topic = parse("tag_feed.xml")[0]

    assert topic.title == "分享一个好用的命令行工具"
    assert topic.author == "alice"
    assert topic.canonical_url == "https://forum.example/t/topic/1001"
    # `pubDate` is the topic's CREATION time. The feed is ordered by last activity
    # but never reports it, which is what keeps `topics.published_at` meaning what
    # the public feed sorts on.
    assert topic.published_at == "2026-09-07T21:00:11.000Z"


def test_the_bumped_old_topic_keeps_its_original_creation_date() -> None:
    topic = parse("tag_feed.xml")[1]

    assert topic.published_at == "2024-05-07T11:04:55.000Z"


def test_each_topic_yields_exactly_one_first_post() -> None:
    for topic in parse("tag_feed.xml"):
        assert len(topic.posts) == 1
        post = topic.posts[0]
        assert post.is_first_post is True
        assert post.post_number == FIRST_POST_NUMBER
        assert post.source_url == f"{topic.canonical_url}/{FIRST_POST_NUMBER}"


def test_post_text_is_plain_text_with_the_trailer_gone() -> None:
    post = parse("tag_feed.xml")[0].posts[0]

    assert "<p>" not in post.text
    assert "阅读完整话题" not in post.text
    assert "个帖子" not in post.text
    assert "最近发现了" in post.text


def test_repository_links_survive_into_the_stored_text() -> None:
    """The classifier reads `post.text`, so an href that dies here is a project lost."""
    posts = [topic.posts[0] for topic in parse("tag_feed.xml")]

    assert "https://github.com/owner/repo" in posts[0].text
    assert "https://github.com/other/tool" in posts[1].text
    assert "github.com" not in posts[2].text


def test_code_blocks_survive() -> None:
    assert "npm install repo" in parse("tag_feed.xml")[0].posts[0].text


def test_the_guid_is_carried_onto_the_post() -> None:
    assert parse("tag_feed.xml")[0].posts[0].guid == "forum.example-topic-1001"


# ----------------------------------------------------------------------
# Hostile and malformed items
# ----------------------------------------------------------------------


def test_only_items_belonging_to_the_configured_host_survive() -> None:
    """THE security test for this module.

    The fixture carries a lookalike host, a prefix host, a missing guid, an empty
    body, a non-numeric id and a zero id. Exactly one topic comes out, and every
    repository named by a rejected item is absent from the result.
    """
    topics = parse("tag_feed_hostile.xml")

    assert [topic.topic_id for topic in topics] == [2007]
    assert all("evil" not in topic.posts[0].text for topic in topics)
    assert all(topic.canonical_url.startswith(f"https://{HOST}/") for topic in topics)


def test_a_duplicate_topic_id_keeps_the_first_occurrence() -> None:
    topic = parse("tag_feed_hostile.xml")[0]

    assert "good/repo" in topic.posts[0].text
    assert "evil/six" not in topic.posts[0].text


def test_one_bad_item_does_not_cost_the_others_their_run() -> None:
    """Six of eight items in the fixture are unusable and nothing raises."""
    assert len(parse("tag_feed_hostile.xml")) == 1


def test_an_empty_feed_yields_no_topics() -> None:
    empty = '<?xml version="1.0"?><rss version="2.0"><channel></channel></rss>'

    assert parse_tag_feed(FEED_URL, empty, max_bytes=MAX_FEED_BYTES) == []


def test_a_malformed_document_is_a_parse_failure() -> None:
    with pytest.raises(Exception):  # noqa: B017 - ElementTree's own ParseError
        parse_tag_feed(FEED_URL, "<rss><channel>", max_bytes=MAX_FEED_BYTES)


def test_a_doctype_is_refused_before_parsing() -> None:
    """Entity expansion is the attack; `xml_safe` owns the rule and must still run."""
    hostile = '<?xml version="1.0"?><!DOCTYPE r [<!ENTITY x "y">]><rss><channel/></rss>'

    with pytest.raises(XmlSafetyError):
        parse_tag_feed(FEED_URL, hostile, max_bytes=MAX_FEED_BYTES)


def test_the_byte_cap_is_enforced() -> None:
    with pytest.raises(XmlSafetyError):
        parse_tag_feed(FEED_URL, feed("tag_feed.xml"), max_bytes=10)


# ----------------------------------------------------------------------
# Transport
# ----------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class FakeResponse:
    """Structurally what `adapters.http.FetchResult` gives the reader."""

    text: str


@dataclass(frozen=True, slots=True)
class FetchCall:
    url: str
    timeout_seconds: float
    max_bytes: int


class FakeFetchError(RuntimeError):
    """Stands in for `adapters.http.FetchError`.

    Declared here rather than imported: `adapters/http.py` does `from workers import
    fetch` at module scope, so importing it would make this whole file unimportable
    under plain CPython. That constraint is exactly why the reader takes the
    transport as a parameter.
    """

    def __init__(self, message: str, *, retryable: bool) -> None:
        super().__init__(message)
        self.retryable = retryable


@dataclass
class RecordingFetch:
    """A `fetch_text` that records every call and never touches the network."""

    body: str = ""
    error: Exception | None = None
    calls: list[FetchCall] = field(default_factory=list)

    async def __call__(self, url: str, *, timeout_seconds: float, max_bytes: int) -> FakeResponse:
        self.calls.append(FetchCall(url, timeout_seconds, max_bytes))

        if self.error is not None:
            raise self.error

        return FakeResponse(self.body)


def read(
    fetch: RecordingFetch, *, timeout_seconds: float = 10.0, max_bytes: int = MAX_FEED_BYTES
) -> list:
    """Drive the coroutine.

    `asyncio.run` rather than an async test, because the pure suite must run under
    plain `pytest` with no plugin: a `@pytest.mark.asyncio` test silently does not
    execute when pytest-asyncio is absent, which is the worst possible outcome for a
    test that exists to prove a security property.
    """
    return asyncio.run(
        read_tag_feed(fetch, FEED_URL, timeout_seconds=timeout_seconds, max_bytes=max_bytes)
    )


def test_reader_returns_the_topics_from_the_body() -> None:
    fetch = RecordingFetch(body=feed("tag_feed.xml"))

    assert [topic.topic_id for topic in read(fetch)] == [1001, 1002, 1003]


def test_reader_fetches_only_the_configured_url() -> None:
    """One request per run, to the URL that came from configuration.

    The hostile fixture is full of foreign hosts. Nothing derived from a response
    body is ever handed back to the transport — there is no second request at all
    now that topics are not fetched individually.
    """
    fetch = RecordingFetch(body=feed("tag_feed_hostile.xml"))

    read(fetch)

    assert [call.url for call in fetch.calls] == [FEED_URL]


def test_reader_forwards_its_bounds_to_the_transport() -> None:
    """The PRD requires a timeout and a size limit on every external request."""
    fetch = RecordingFetch(body=feed("tag_feed.xml"))

    read(fetch, timeout_seconds=3.5, max_bytes=MAX_FEED_BYTES)

    assert fetch.calls[0].timeout_seconds == 3.5
    assert fetch.calls[0].max_bytes == MAX_FEED_BYTES


def test_reader_propagates_transport_failures_with_their_classification() -> None:
    """`FetchError.retryable` is what the orchestrator classifies on, so it must
    reach it unchanged. This is the run's only source: a failure here fails the run.
    """
    fetch = RecordingFetch(error=FakeFetchError("timeout", retryable=True))

    with pytest.raises(FakeFetchError) as caught:
        read(fetch)

    assert caught.value.retryable is True


def test_reader_propagates_a_malformed_body_as_a_parse_failure() -> None:
    fetch = RecordingFetch(body="<rss><channel>")

    with pytest.raises(Exception):  # noqa: B017 - ElementTree's own ParseError
        read(fetch)
