"""Reads the one configured Discourse tag feed and returns whole topics.

A Discourse list feed (`/tag/<slug>/<id>.rss`, and the identically shaped
`/latest.rss` and `/c/<slug>.rss`) carries the first post's **complete cooked
HTML** inside each item's `<description>` — not an excerpt. That single fact is
why this module replaced the earlier two-step pipeline of "discover topic ids from
a channel feed, then fetch each topic's own feed": one request now yields both the
topic list and the text the classifier needs. See
`docs/adr/0007-single-source-tag-feed.md`.

Three properties of the format shape this module, and none can be negotiated by
changing code here:

  * **Replies are not in the feed.** Every item is a topic's opening post; the rest
    of the thread survives only as a generated line counting posts and
    participants. A repository first linked in a reply is invisible to this
    project — that is a deliberate, documented loss.
  * **Ordering is `bumped_at` descending and cannot be changed.** `?order=created`
    is accepted and ignored; `/l/latest.rss` is a 404. An old topic reappears at the
    top of the feed whenever anyone replies to it, which is precisely why
    `save_discovered_topics` must skip a topic it already knows.
  * **The window is ~30 items with no pagination.** Discourse has no RFC 5005
    support, so a topic that falls off the end is unreachable forever.

`<description>` ends with two generated paragraphs that are *not* part of the
post: a `<small>` line counting posts and participants, and a paragraph linking
back to the topic. Both are stripped structurally — by element shape and by
comparing the anchor's href against the item's own `<link>` — rather than by
matching their text. A Discourse instance serves both lines translated, and
linux.do is a Chinese site: any rule that keyed off the English wording would
silently stop stripping and leak boilerplate into `cleaned_text`.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from typing import Protocol
from urllib.parse import urlsplit

from linuxdo_oss.feeds.html_text import html_to_text
from linuxdo_oss.feeds.rss import RssItem, parse_items, rss_datetime_to_iso
from linuxdo_oss.feeds.xml_safe import MAX_FEED_BYTES, parse_feed

logger = logging.getLogger(__name__)


# ----------------------------------------------------------------------
# Shape of a topic
# ----------------------------------------------------------------------

# Every topic yields exactly one post now, so the number is a constant rather than
# something derived from the feed. It stays a real column because `topic_posts` is
# keyed by `(topic_id, post_number)` and `project_mentions` points at a post row:
# collapsing it to nothing would mean a schema migration for no gain.
FIRST_POST_NUMBER = 1


@dataclass(frozen=True, slots=True)
class RetainedPost:
    """One post that reaches the database — a `topic_posts` row.

    `text` is already plain text: raw feed HTML must never reach the database or
    the browser. `source_url` is built from the topic id and the post number rather
    than copied from the feed, because it is rendered as a link in the frontend and
    a stored link is only as trustworthy as whoever wrote the feed.
    """

    post_number: int
    guid: str | None
    author: str | None
    published_at: str | None
    source_url: str
    text: str
    is_first_post: bool


@dataclass(frozen=True, slots=True)
class TopicFeed:
    """A whole topic, normalized. `posts` holds exactly one entry, the first post.

    The tuple is kept rather than flattened to a single field so that
    `sync.py::_discovered_topic` and `write_repository._post_rows` iterate over
    posts the same way they always have. If replies ever become available again,
    the shape that carries them already exists.
    """

    topic_id: int
    canonical_url: str
    title: str | None
    author: str | None
    published_at: str | None
    posts: tuple[RetainedPost, ...]


# ----------------------------------------------------------------------
# Trailer stripping
# ----------------------------------------------------------------------

# `<p><small>…</small></p>` as the final element. Matched by structure, never by
# the words inside — Discourse translates that line, so `[^<]*` is the whole point.
_COUNT_TRAILER_RE = re.compile(
    r"\s*<p>\s*<small>[^<]*</small>\s*</p>\s*\Z",
    re.IGNORECASE,
)

# Anchors whose href is entity-escaped in the markup. Discourse writes topic URLs
# without a query string, so `&amp;` is the only escape that can realistically
# appear; decoding just that one keeps the comparison exact without pulling the
# whole HTML parser into a string operation.
_ESCAPED_AMP_RE = re.compile(r"&amp;", re.IGNORECASE)


def _link_trailer_re(link: str) -> re.Pattern[str]:
    """Final `<p>` holding one anchor that points back at `link`.

    Keying on the href rather than on the anchor's text is what makes this survive
    translation. The label is spanned by a lazy repeat because it may contain markup
    on some themes, and `re.S` lets it wrap across lines.

    That repeat is forbidden from crossing a `</p>`, and that guard is load-bearing
    rather than tidiness. `\\Z` requires the match to end at the end of the string,
    so a plain `.*?` starting at an EARLIER paragraph would happily swallow every
    paragraph between it and the real trailer to satisfy the anchor. A post whose
    body contains a `<p>` beginning with a link to its own topic — a "see my
    original post" paragraph — would then lose its entire body, GitHub links
    included, and the topic would be dropped as empty with only a log line to say
    so. Refusing to cross a paragraph boundary makes the match provably one `<p>`,
    which is the only thing the trailer can be.
    """
    escaped = re.escape(link)
    return re.compile(
        r"\s*<p>\s*<a\b[^>]*\bhref\s*=\s*[\"']"
        + escaped
        + r"/?[\"'][^>]*>(?:(?!</p>).)*?</a>\s*</p>\s*\Z",
        re.IGNORECASE | re.DOTALL,
    )


def strip_generated_trailer(description: str, link: str | None) -> str:
    """`description` without Discourse's two generated closing paragraphs.

    Order matters: the topic link is the last element, so it goes first and the
    count line is only then in final position.

    A trailer that fails to match is left in place on purpose. It costs two lines
    of noise in `cleaned_text` and cannot manufacture a repository candidate — the
    anchor points at the forum, not at GitHub — so degrading quietly beats raising
    on a template this module does not control.
    """
    body = description

    if link:
        body = _link_trailer_re(_ESCAPED_AMP_RE.sub("&", link)).sub("", body)

    return _COUNT_TRAILER_RE.sub("", body)


# ----------------------------------------------------------------------
# Identity
# ----------------------------------------------------------------------

# Discourse's `<guid isPermaLink="false">` for a list feed is `{host}-topic-{id}`.
# The id is read from here rather than parsed out of `<link>` because the link
# carries a display slug (`/t/<slug>/<id>`) that a Chinese title turns into
# `/t/topic/<id>`, and a URL is a much larger surface to validate than this.
_TOPIC_ID_MAX = 2**53 - 1


def feed_host(feed_url: str) -> str:
    """The host every item in `feed_url`'s feed must belong to.

    Deriving the expected host from configuration, rather than hard-coding
    `linux.do`, is what lets the whole pipeline run against any Discourse instance
    — including one reachable from a development machine. The trust anchor is the
    operator-supplied URL, which is strictly stronger than a constant: a feed can
    never widen its own authority.
    """
    host = urlsplit(feed_url).hostname
    if not host:
        raise ValueError(f"feed URL has no host: {feed_url!r}")

    return host.lower()


def canonical_topic_url(host: str, topic_id: int) -> str:
    """`https://<host>/t/topic/<id>` — the value stored in `topics.canonical_url`.

    `/t/topic/<id>` rather than `/t/<slug>/<id>`: Discourse resolves a topic by id
    alone and the slug is display data. Building it here from validated parts means
    no feed-supplied URL is ever stored.
    """
    return f"https://{host}/t/topic/{topic_id}"


def topic_id_from_guid(guid: str | None, *, host: str) -> int | None:
    """The topic id in `guid`, or None when it does not belong to `host`.

    The host is interpolated into the pattern already escaped, so a host containing
    a hyphen (`my-forum.example`) cannot be split at the wrong place — which a
    generic `(.+)-topic-(\\d+)` would do.
    """
    if not guid:
        return None

    match = re.fullmatch(re.escape(host) + r"-topic-(\d+)", guid.strip(), re.IGNORECASE)
    if match is None:
        return None

    topic_id = int(match.group(1))
    if topic_id <= 0 or topic_id > _TOPIC_ID_MAX:
        return None

    return topic_id


# ----------------------------------------------------------------------
# Item metadata
# ----------------------------------------------------------------------

# Discourse emits `<dc:creator>`; `feeds/rss.py` surfaces it as the extra `creator`
# with the namespace stripped. Atom's `<author><name>` arrives as `author`. Neither
# is one of the five fields `RssItem` names, so `extras` is where they land.
_AUTHOR_TAGS = frozenset({"author", "creator"})


def _author_of(item: RssItem) -> str | None:
    """The first author-ish extra, in document order.

    Order rather than a preference between the two spellings: a feed emits one or
    the other, never both, and inventing a ranking would be a rule with no case to
    justify it.
    """
    for tag, text in item.extras:
        if tag in _AUTHOR_TAGS:
            return text

    return None


# ----------------------------------------------------------------------
# Parsing
# ----------------------------------------------------------------------


def parse_tag_feed(
    feed_url: str, feed_text: str, *, max_bytes: int = MAX_FEED_BYTES
) -> list[TopicFeed]:
    """Every usable topic in one tag feed, in feed order. Pure — no I/O, no clock.

    Feed order is `bumped_at` descending and is preserved rather than sorted: the
    caller upserts by primary key and never depends on the sequence, and imposing an
    order here would invent a ranking the feed did not express.

    An item is skipped, not raised on, when it has no usable guid, belongs to
    another host, or carries no description. One malformed entry in a
    thirty-entry feed must not cost the other twenty-nine their run — the run-level
    failure path is reserved for a feed that cannot be parsed at all.

    A duplicate topic id keeps its first occurrence. Discourse does not repeat a
    topic within one response, so this only fires on a rewritten or proxied feed;
    letting the later copy win would make the result depend on how far down the
    duplicate sat.
    """
    host = feed_host(feed_url)
    items = parse_items(parse_feed(feed_text, max_bytes=max_bytes))

    topics: list[TopicFeed] = []
    seen: set[int] = set()

    for item in items:
        topic_id = topic_id_from_guid(item.guid, host=host)
        if topic_id is None:
            logger.warning("tag feed: skipping item with unusable guid %r", item.guid)
            continue

        if topic_id in seen:
            logger.warning("tag feed: duplicate topic %s, keeping first", topic_id)
            continue

        body = strip_generated_trailer(item.description or "", item.link)
        text = html_to_text(body)
        if not text:
            # No text means nothing for the classifier to read and no evidence a
            # mention could point at. Storing the row anyway would put a topic in
            # the queue that can only ever settle as `not_relevant`.
            logger.warning("tag feed: skipping topic %s with empty description", topic_id)
            continue

        seen.add(topic_id)
        canonical = canonical_topic_url(host, topic_id)
        published_at = rss_datetime_to_iso(item.pub_date)

        topics.append(
            TopicFeed(
                topic_id=topic_id,
                canonical_url=canonical,
                # Item metadata *is* the opening post's metadata in a Discourse list
                # feed, so topic-level and post-level values are the same reading.
                # `pubDate` is the topic's creation time — the feed is ordered by
                # last activity, but it never reports that time — which is what
                # keeps `topics.published_at` meaning what the frontend sorts on.
                title=item.title,
                author=_author_of(item),
                published_at=published_at,
                posts=(
                    RetainedPost(
                        post_number=FIRST_POST_NUMBER,
                        guid=item.guid,
                        author=_author_of(item),
                        published_at=published_at,
                        source_url=f"{canonical}/{FIRST_POST_NUMBER}",
                        text=text,
                        is_first_post=True,
                    ),
                ),
            )
        )

    return topics


# ----------------------------------------------------------------------
# Network
# ----------------------------------------------------------------------


class TextResponse(Protocol):
    """The one field of `adapters/http.py::FetchResult` this module reads."""

    text: str


class FetchTextPort(Protocol):
    """The shape of `adapters/http.py::fetch_text`.

    Structural, not an import: `adapters/http.py` does `from workers import fetch`
    at module scope and therefore cannot be imported under CPython at all. Taking
    the callable as a parameter is what keeps this module testable with a fake, and
    keeps `adapters/` the only place that knows a runtime exists.
    """

    async def __call__(
        self, url: str, *, timeout_seconds: float, max_bytes: int
    ) -> TextResponse: ...


async def read_tag_feed(
    fetch_text: FetchTextPort,
    feed_url: str,
    *,
    timeout_seconds: float,
    max_bytes: int,
) -> list[TopicFeed]:
    """Fetch and normalize the configured tag feed.

    `timeout_seconds` and `max_bytes` have no defaults on purpose — the PRD requires
    every external request to be bounded, and a default here would let a caller that
    forgot its `Settings` look correct.

    The same `max_bytes` bounds the transport and the parser. They are separate
    checks (a lying `content-length`, a chunked body) and both must hold, but there
    is one number so a response can never be small enough to accept and too large to
    parse.

    `FetchError` from the transport is left to propagate: this is the run's only
    source, so a failure here is a run-level failure and `sync.py` records it as
    one.
    """
    response = await fetch_text(
        feed_url,
        timeout_seconds=timeout_seconds,
        max_bytes=max_bytes,
    )

    return parse_tag_feed(feed_url, response.text, max_bytes=max_bytes)
