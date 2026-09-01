"""Tests for the channel feed reader.

Two failure modes, both expensive and neither noisy:

  * A missed link means a topic is never discovered. Nothing errors; the project
    simply never appears. That is why the table below covers Chinese prose glued to
    a URL, trailing punctuation, entity-encoded attributes and every field an id can
    hide in.
  * A false accept is the SSRF hole the PRD names. The reader's answer is a
    `list[int]`, so the real assertion is structural — but a bogus id that survives
    validation still becomes a request to a URL derived from hostile input, so the
    lookalike hosts get a table of their own.

Feeds come from `backend/tests/fixtures/`, shaped like the live response verified on
2026-08-31: RSS 2.0, five elements per item, `link` pointing at Telegram, and the
linux.do URL only reachable through the HTML inside `description`.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from pathlib import Path

import pytest

from linuxdo_oss.feeds.channel import (
    MAX_TOPIC_ID,
    TOPIC_HOST,
    canonical_topic_feed_url,
    canonical_topic_url,
    extract_topic_candidates,
    extract_topic_ids,
    read_channel_feed,
    topic_id_from_url,
)
from linuxdo_oss.feeds.xml_safe import MAX_FEED_BYTES, XmlSafetyError

FIXTURES = Path(__file__).parent / "fixtures"

# The configured channel source, the only URL the reader is ever allowed to fetch.
FEED_URL = "https://rsshub.rssforever.com/telegram/channel/linux_do_channel"


def feed(name: str) -> str:
    return (FIXTURES / name).read_text(encoding="utf-8")


# ----------------------------------------------------------------------
# URL construction — the SSRF rule's other half
# ----------------------------------------------------------------------


def test_canonical_urls_are_built_from_the_id() -> None:
    assert canonical_topic_url(2837720) == "https://linux.do/t/topic/2837720"
    assert canonical_topic_feed_url(2837720) == "https://linux.do/t/topic/2837720.rss"


def test_constructed_urls_use_the_allowlisted_host() -> None:
    """The accepted host and the constructed host are the same constant, so an
    allowlist edit cannot silently start building URLs for somewhere else."""
    assert canonical_topic_url(1).startswith(f"https://{TOPIC_HOST}/")
    assert canonical_topic_feed_url(1).startswith(f"https://{TOPIC_HOST}/")


UNUSABLE_IDS = [0, -1, -2837720, MAX_TOPIC_ID + 1, 10**40]


@pytest.mark.parametrize("topic_id", UNUSABLE_IDS)
def test_unusable_ids_cannot_become_urls(topic_id: int) -> None:
    with pytest.raises(ValueError):
        canonical_topic_url(topic_id)
    with pytest.raises(ValueError):
        canonical_topic_feed_url(topic_id)


NON_INT_IDS = ["2837720", "1/../../evil", "", None, 2.0, [2837720]]


@pytest.mark.parametrize("topic_id", NON_INT_IDS)
def test_a_non_int_never_reaches_the_url_template(topic_id: object) -> None:
    """The one way feed text could still become a fetch target is a caller that lost
    track of its types. It fails here instead."""
    with pytest.raises(ValueError):
        canonical_topic_feed_url(topic_id)  # type: ignore[arg-type]


def test_the_maximum_id_is_usable() -> None:
    """Pins the off-by-one at the top of the range: MAX_TOPIC_ID is accepted, and the
    test above proves MAX_TOPIC_ID + 1 is not."""
    assert canonical_topic_url(MAX_TOPIC_ID) == f"https://{TOPIC_HOST}/t/topic/{MAX_TOPIC_ID}"


# ----------------------------------------------------------------------
# Validation: accepted URLs
# ----------------------------------------------------------------------

ACCEPTED_URLS = [
    # (url, expected topic id)
    ("https://linux.do/t/topic/2837720", 2837720),
    ("https://linux.do/t/topic/2837720/", 2837720),
    # The floor suffix the live feed always carries. It identifies a post, not a
    # topic, so it is dropped.
    ("https://linux.do/t/topic/2837720/1", 2837720),
    ("https://linux.do/t/topic/2837720/12", 2837720),
    ("https://linux.do/t/topic/2837720/999999999", 2837720),
    # Query and fragment are tracking data and anchors, never identity.
    ("https://linux.do/t/topic/2837720?u=someone", 2837720),
    ("https://linux.do/t/topic/2837720#post_2", 2837720),
    ("https://linux.do/t/topic/2837720/3?u=someone#post_3", 2837720),
    ("https://linux.do/t/topic/2837720?", 2837720),
    # Case is not part of a hostname, and the scheme is case-insensitive too.
    ("https://LINUX.DO/t/topic/2837720", 2837720),
    ("HTTPS://linux.do/t/topic/2837720", 2837720),
    ("https://Linux.Do/t/topic/2837720/1", 2837720),
    # An explicit port is stripped by `hostname`.
    ("https://linux.do:443/t/topic/2837720", 2837720),
    # Surrounding whitespace comes free with an attribute value.
    ("  https://linux.do/t/topic/2837720/1  ", 2837720),
    # Range boundaries.
    ("https://linux.do/t/topic/1", 1),
    (f"https://linux.do/t/topic/{MAX_TOPIC_ID}", MAX_TOPIC_ID),
]


@pytest.mark.parametrize(("url", "expected"), ACCEPTED_URLS)
def test_accepted_urls_yield_their_topic_id(url: str, expected: int) -> None:
    assert topic_id_from_url(url) == expected


# ----------------------------------------------------------------------
# Validation: rejected URLs
# ----------------------------------------------------------------------

REJECTED_URLS = [
    # --- deceptive hosts ---------------------------------------------------
    "https://linux.do.evil.example/t/topic/1",
    "https://notlinux.do/t/topic/1",
    "https://linux.do.attacker.example/t/topic/1",
    "https://xlinux.do/t/topic/1",
    "https://linux.dO.evil.example/t/topic/1",
    # `hostname` strips userinfo, so the host here is `evil.example`. String
    # matching on the raw URL is what gets this class of attack wrong.
    "https://linux.do@evil.example/t/topic/1",
    "https://linux.do:pass@evil.example/t/topic/1",
    "https://linux.do.evil.example@linux.do.attacker.example/t/topic/1",
    # `www` is deliberately excluded — see the comment on `_ALLOWED_HOSTS`.
    "https://www.linux.do/t/topic/1",
    # A subdomain is not the site.
    "https://cdn.linux.do/t/topic/1",
    "https://linux.do./t/topic/1",
    "https://192.168.0.1/t/topic/1",
    "https://localhost/t/topic/1",
    "https://127.0.0.1:8787/t/topic/1",
    # --- schemes -----------------------------------------------------------
    "http://linux.do/t/topic/1",
    "ftp://linux.do/t/topic/1",
    "file:///t/topic/1",
    "javascript:alert('https://linux.do/t/topic/1')",
    "data:text/html,https://linux.do/t/topic/1",
    "//linux.do/t/topic/1",
    "/t/topic/1",
    "linux.do/t/topic/1",
    # --- paths -------------------------------------------------------------
    "https://linux.do/",
    "https://linux.do",
    "https://linux.do/latest",
    "https://linux.do/u/someone",
    "https://linux.do/t/topic",
    "https://linux.do/t/topic/",
    "https://linux.do/t/2900700/1",
    "https://linux.do/t/topic/1/2/3",
    "https://linux.do/t/topic/1/notafloor",
    "https://linux.do/c/topic/1",
    "https://linux.do/t/topic/1.rss",
    # --- ids ---------------------------------------------------------------
    "https://linux.do/t/topic/0",
    "https://linux.do/t/topic/00",
    "https://linux.do/t/topic/007",
    "https://linux.do/t/topic/-1",
    "https://linux.do/t/topic/1e9",
    "https://linux.do/t/topic/abc",
    "https://linux.do/t/topic/2837720abc",
    "https://linux.do/t/topic/%32837720",
    f"https://linux.do/t/topic/{MAX_TOPIC_ID + 1}",
    "https://linux.do/t/topic/1234567890123456789012345678901234567890",
    # --- not a URL at all --------------------------------------------------
    "",
    "   ",
    "https://",
    "https://[::1/t/topic/1",
]


@pytest.mark.parametrize("url", REJECTED_URLS)
def test_rejected_urls_yield_nothing(url: str) -> None:
    assert topic_id_from_url(url) is None, f"should have been rejected: {url}"


# ----------------------------------------------------------------------
# Scanning one field
# ----------------------------------------------------------------------

TEXT_CASES = [
    # (label, text, expected ids)
    ("bare url", "https://linux.do/t/topic/2837720", [2837720]),
    ("in a sentence", "see https://linux.do/t/topic/2837720/1 for details", [2837720]),
    # `\w` matches CJK, so a `(?<!\w)` guard would silently drop this — and Chinese
    # prose glues URLs onto characters constantly.
    ("glued to Chinese", "原帖https://linux.do/t/topic/2837720/1，欢迎讨论", [2837720]),
    ("followed by a full stop", "见 https://linux.do/t/topic/2837720.", [2837720]),
    ("wrapped in parentheses", "(https://linux.do/t/topic/2837720)", [2837720]),
    ("followed by a comma", "https://linux.do/t/topic/2837720, and more", [2837720]),
    ("quoted", '"https://linux.do/t/topic/2837720"', [2837720]),
    ("full-width punctuation ends it", "https://linux.do/t/topic/2837720。后面", [2837720]),
    # Deduplication inside one field: the same topic linked twice at two floors.
    (
        "same topic twice",
        "https://linux.do/t/topic/2837720/1 and https://linux.do/t/topic/2837720/9",
        [2837720],
    ),
    # First appearance decides the order.
    (
        "two topics keep document order",
        "https://linux.do/t/topic/2837722 then https://linux.do/t/topic/2837720",
        [2837722, 2837720],
    ),
    ("no url", "今天天气不错，没有链接", []),
    ("empty", "", []),
    ("telegram only", "https://t.me/linux_do_channel/492492", []),
    ("github only", "https://github.com/owner/repo", []),
    ("lookalike host", "https://linux.do.evil.example/t/topic/2837720", []),
    ("prefixed host", "https://notlinux.do/t/topic/2837720", []),
    ("userinfo smuggle", "https://linux.do@evil.example/t/topic/2837720", []),
    ("looks like an email", "写信到 a@linux.do/t/topic/2837720", []),
    ("plain http", "http://linux.do/t/topic/2837720", []),
    ("absurd id", "https://linux.do/t/topic/1234567890123456789012345678901234567890", []),
    ("zero id", "https://linux.do/t/topic/0", []),
    ("leading zero id", "https://linux.do/t/topic/007", []),
    # A redirector that carries a real topic URL in its query. The id IS extracted:
    # it is a genuine topic reference, and acting on it means fetching a URL this
    # module builds itself, never the redirector. Recording the behaviour rather
    # than pretending it does not happen.
    (
        "topic url inside a redirector query",
        "https://evil.example/go?to=https://linux.do/t/topic/2837720/1",
        [2837720],
    ),
]


@pytest.mark.parametrize(
    ("text", "expected"),
    [(text, expected) for _label, text, expected in TEXT_CASES],
    ids=[label for label, _text, _expected in TEXT_CASES],
)
def test_topic_ids_are_extracted_from_text(text: str, expected: list[int]) -> None:
    assert extract_topic_ids(text) == expected


# ----------------------------------------------------------------------
# Scanning HTML structurally
# ----------------------------------------------------------------------

# The id's leading `2` is written as a character reference. A text regex sees
# `.../t/topic/&#50;900004/1` and rejects it; an HTML parser resolves the attribute.
ENTITY_ENCODED_ANCHOR = '<a href="https://linux.do/t/topic/&#50;900004/1">看这里</a>'


def test_entity_encoded_hrefs_are_resolved() -> None:
    assert extract_topic_ids(ENTITY_ENCODED_ANCHOR) == [2900004]


def test_the_same_url_outside_an_attribute_is_not_resolved() -> None:
    """Proves the structural pass is load-bearing rather than duplicated effort: the
    identical string as plain text yields nothing, because unescaping arbitrary text
    would let the feed manufacture URLs that were never written."""
    assert extract_topic_ids("https://linux.do/t/topic/&#50;900004/1") == []


HTML_CASES = [
    ("double quoted", '<a href="https://linux.do/t/topic/2837720/1">x</a>', [2837720]),
    ("single quoted", "<a href='https://linux.do/t/topic/2837720/1'>x</a>", [2837720]),
    ("unquoted", "<a href=https://linux.do/t/topic/2837720>x</a>", [2837720]),
    ("self closing", '<a href="https://linux.do/t/topic/2837720/1"/>', [2837720]),
    ("uppercase attribute", '<A HREF="https://linux.do/t/topic/2837720">x</A>', [2837720]),
    ("whitespace in the value", '<a href=" https://linux.do/t/topic/2837720 ">x</a>', [2837720]),
    # The live description carries onclick handlers. They are scanned as text like
    # everything else and produce nothing; the href beside them still works.
    (
        "hostile attributes alongside",
        '<a href="https://linux.do/t/topic/2837720/1" onclick="return confirm(\'x\')" '
        'target="_blank" rel="noopener">linux.do</a>',
        [2837720],
    ),
    (
        "a lookalike href is rejected structurally too",
        '<a href="https://linux.do.evil.example/t/topic/9999991/1">x</a>',
        [],
    ),
    ("javascript href", "<a href=\"javascript:alert('x')\">x</a>", []),
    ("empty href", '<a href="">x</a>', []),
    ("no href", "<p>没有链接</p>", []),
]


@pytest.mark.parametrize(
    ("html_text", "expected"),
    [(html_text, expected) for _label, html_text, expected in HTML_CASES],
    ids=[label for label, _html, _expected in HTML_CASES],
)
def test_topic_ids_are_extracted_from_html(html_text: str, expected: list[int]) -> None:
    assert extract_topic_ids(html_text) == expected


MALFORMED_HTML = [
    '<a href="https://linux.do/t/topic/2837720/1">unclosed',
    '<a href="https://linux.do/t/topic/2837720/1" <b> <<>> ',
    '<<a href="https://linux.do/t/topic/2837720/1">',
    "<a href=<script>https://linux.do/t/topic/2837720</script>>",
    "<!-- <a href=\"https://linux.do/t/topic/2837720\"> --><a href='#'>",
    "<a href=" + '"' * 50,
    "</a></p><a",
    '<![CDATA[ <a href="https://linux.do/t/topic/2837720"> ]]>',
]


@pytest.mark.parametrize("html_text", MALFORMED_HTML)
def test_malformed_html_does_not_raise(html_text: str) -> None:
    """The description is hostile input. `html.parser` has been non-strict since
    Python 3.5 (`HTMLParseError` was removed) and resynchronises instead of raising —
    pinned here, because if it ever did raise, one bad item would take down discovery
    for the whole feed."""
    assert isinstance(extract_topic_ids(html_text), list)


# ----------------------------------------------------------------------
# Whole feeds
# ----------------------------------------------------------------------


def test_the_live_feed_shape_yields_its_topics() -> None:
    """The topic URLs are inside the description HTML with floor suffixes, while
    `link` points at Telegram. Scanning only `link` would return nothing at all."""
    assert extract_topic_candidates(feed("channel_feed.xml")) == [2837720, 2837721, 2837722]


def test_a_topic_repeated_across_items_is_returned_once() -> None:
    """The channel republishes a topic on every new reply. Re-fetching it inside one
    run would spend subrequests for nothing."""
    candidates = extract_topic_candidates(feed("channel_feed.xml"))

    assert candidates.count(2837720) == 1
    # First appearance wins the position: item 1 mentions it, item 2 mentions it
    # again at a different floor.
    assert candidates[0] == 2837720


def test_an_item_with_no_topic_link_contributes_nothing() -> None:
    """Item 3 of the fixture links only GitHub and Telegram."""
    assert 492000 not in extract_topic_candidates(feed("channel_feed.xml"))


def test_ids_are_found_in_title_guid_and_unknown_elements() -> None:
    """The PRD requires every supported field to be inspected. The fourth item is
    reachable only through the structural href pass."""
    assert extract_topic_candidates(feed("channel_feed_other_fields.xml")) == [
        2900001,
        2900002,
        2900003,
        2900004,
    ]


def test_hostile_hosts_and_absurd_ids_are_dropped_without_losing_the_real_one() -> None:
    """Sixteen attempts to smuggle a fetch target, and one legitimate topic in the
    last item. A rejection must not cost the good sibling."""
    assert extract_topic_candidates(feed("channel_feed_hostile_hosts.xml")) == [2900500]


def test_an_empty_feed_is_not_an_error() -> None:
    """RSSHub serves this whenever the channel published nothing in the window."""
    assert extract_topic_candidates(feed("channel_feed_empty.xml")) == []


def test_malformed_xml_surfaces_as_the_parsing_layers_error() -> None:
    """One exception type for every refusal, so the orchestrator can classify a feed
    failure without knowing why the document was refused."""
    with pytest.raises(XmlSafetyError):
        extract_topic_candidates(feed("channel_feed_malformed.xml"))


def test_an_oversized_feed_is_refused_before_it_is_parsed() -> None:
    padding = "x" * MAX_FEED_BYTES
    oversized = feed("channel_feed.xml").replace(
        "</channel>", f"<item><title>{padding}</title></item></channel>"
    )

    with pytest.raises(XmlSafetyError):
        extract_topic_candidates(oversized)


def test_the_size_cap_is_the_caller_s_to_set() -> None:
    """`max_bytes` is threaded from `Settings.max_response_bytes` rather than being a
    constant of the parser, so a smaller transport budget really does bound parsing."""
    document = feed("channel_feed.xml")

    with pytest.raises(XmlSafetyError):
        extract_topic_candidates(document, max_bytes=len(document.encode("utf-8")) - 1)


# ----------------------------------------------------------------------
# The reader, against a fake transport
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
    fetch: RecordingFetch, *, timeout_seconds: float = 10.0, max_bytes: int = 1024
) -> list[int]:
    """Drive the coroutine.

    `asyncio.run` rather than an async test, because the pure suite must run under
    plain `pytest` with no plugin: a `@pytest.mark.asyncio` test silently does not
    execute when pytest-asyncio is absent, which is the worst possible outcome for a
    test that exists to prove a security property.
    """
    return asyncio.run(
        read_channel_feed(fetch, FEED_URL, timeout_seconds=timeout_seconds, max_bytes=max_bytes)
    )


def test_read_channel_feed_returns_the_candidates_from_the_body() -> None:
    fetch = RecordingFetch(body=feed("channel_feed.xml"))

    assert read(fetch, max_bytes=MAX_FEED_BYTES) == [2837720, 2837721, 2837722]


def test_read_channel_feed_fetches_only_the_configured_url() -> None:
    """THE test for this module.

    The fixture is full of linux.do URLs, lookalike hosts and a userinfo smuggle.
    Exactly one request is made, to the URL that came from configuration, and nothing
    derived from the response body is ever handed back to the transport.
    """
    fetch = RecordingFetch(body=feed("channel_feed_hostile_hosts.xml"))

    read(fetch, max_bytes=MAX_FEED_BYTES)

    assert [call.url for call in fetch.calls] == [FEED_URL]
    assert all("linux.do" not in call.url for call in fetch.calls)
    assert all("evil.example" not in call.url for call in fetch.calls)


def test_read_channel_feed_forwards_its_bounds_to_the_transport() -> None:
    """The PRD requires a timeout and a size limit on every external request; the
    reader must not quietly use its own numbers."""
    fetch = RecordingFetch(body=feed("channel_feed_empty.xml"))

    read(fetch, timeout_seconds=3.5, max_bytes=4096)

    assert fetch.calls == [FetchCall(FEED_URL, 3.5, 4096)]


def test_read_channel_feed_propagates_transport_failures_with_their_classification() -> None:
    """Re-wrapping would lose `retryable`, which is what decides whether a later cron
    run tries again."""
    fetch = RecordingFetch(error=FakeFetchError("upstream returned 503", retryable=True))

    with pytest.raises(FakeFetchError) as raised:
        read(fetch)

    assert raised.value.retryable is True


def test_read_channel_feed_propagates_a_malformed_body_as_a_parse_failure() -> None:
    fetch = RecordingFetch(body=feed("channel_feed_malformed.xml"))

    with pytest.raises(XmlSafetyError):
        read(fetch, max_bytes=MAX_FEED_BYTES)


def test_every_candidate_produces_a_fetchable_canonical_url() -> None:
    """The handover to the topic reader: ids in, constructed URLs out. Nothing in
    between carries a string from the feed."""
    candidates = read(RecordingFetch(body=feed("channel_feed.xml")), max_bytes=MAX_FEED_BYTES)

    assert [canonical_topic_feed_url(topic_id) for topic_id in candidates] == [
        "https://linux.do/t/topic/2837720.rss",
        "https://linux.do/t/topic/2837721.rss",
        "https://linux.do/t/topic/2837722.rss",
    ]
