"""Topic feed reader — one Linux.do topic RSS in, the retained posts out.

Owns three decisions the PRD states and nothing else owns:

  1. **Which URL is fetched.** Built from the validated numeric topic id, never
     from anything a feed said. An `int` cannot carry a host, a path or a scheme,
     so `https://linux.do/t/topic/<id>.rss` is the only URL this module can
     produce — that is the SSRF control, not a convention.
  2. **Which posts are kept.** The first post always; a reply only when its
     *original* content contains an explicit GitHub repository URL. Everything
     else is discarded before it can cost an LLM call.
  3. **What a post is worth storing.** HTML becomes text (`feeds/html_text.py`),
     `pubDate` becomes the one canonical timestamp (`domain/timestamps.py` via
     `feeds/rss.py`), and the post number comes from the feed's own URLs.

Pure in the sense that matters: no `workers`, no `js`, no module-level runtime
import. `read_topic_feed` takes `adapters/http.py::fetch_text` as a parameter, so
the whole reader — network path included — runs under plain CPython pytest with a
fake transport. See .trellis/spec/backend/directory-structure.md.

Two outcomes that look like failures and are not:

  * **A permission-gated topic.** `/t/topic/<id>.rss` serves public content only.
    A gated topic answers 403/404, which `fetch_text` raises as a `FetchError`
    carrying `retryable=False` — an ordinary, expected result for a link the
    channel feed happened to mention, not a bug to work around here.
  * **A feed with no items.** Returns a `TopicFeed` with no posts rather than
    raising. The orchestrator settles that topic as `not_relevant`; inventing an
    exception would make an empty topic indistinguishable from a broken one.

Refusals — over the byte cap, a DTD, malformed XML — arrive as `XmlSafetyError`
and are deliberately left to propagate. It is already the single permanent-failure
type for this layer (.trellis/spec/backend/error-handling.md); re-wrapping would
only add a name.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Protocol

from linuxdo_oss.domain.github_url import contains_repository_url
from linuxdo_oss.feeds.html_text import extract_links, html_to_text
from linuxdo_oss.feeds.rss import RssItem, parse_items, rss_datetime_to_iso
from linuxdo_oss.feeds.xml_safe import MAX_FEED_BYTES, parse_feed

__all__ = [
    "MAX_POST_NUMBER",
    "FetchTextPort",
    "RetainedPost",
    "TopicFeed",
    "canonical_topic_url",
    "parse_topic_feed",
    "read_topic_feed",
    "topic_feed_url",
]

# ----------------------------------------------------------------------
# URLs
# ----------------------------------------------------------------------

# The one origin this module will ever fetch from. A constant rather than
# configuration: the PRD fixes the source, and a configurable host would reopen the
# SSRF hole that constructing the URL from an int closes.
_LINUXDO_TOPIC_BASE = "https://linux.do/t/topic"

# A post number above this can only come from a malformed or hostile URL — the
# largest Discourse topics run to tens of thousands of posts, not millions — and
# `topic_posts.post_number` is a SQLite INTEGER. Beyond the cap the number is
# treated as underivable and the fallback below assigns one.
MAX_POST_NUMBER = 1_000_000


def canonical_topic_url(topic_id: int) -> str:
    """`https://linux.do/t/topic/<id>` — the value stored in `topics.canonical_url`.

    Floor-suffix free by construction, which is what makes it comparable across the
    different spellings the channel feed contains.
    """
    return f"{_LINUXDO_TOPIC_BASE}/{_validated_topic_id(topic_id)}"


def topic_feed_url(topic_id: int) -> str:
    """The canonical RSS URL for a topic.

    Discourse also serves `/t/<slug>/<id>.rss`, but the slug is display data that
    can change; the slug-less form is stable and needs no input beyond the id.
    """
    return f"{_LINUXDO_TOPIC_BASE}/{_validated_topic_id(topic_id)}.rss"


def _validated_topic_id(topic_id: int) -> int:
    """The id, or ValueError.

    `bool` is rejected explicitly because it is a subclass of `int` and would
    format as `True` — a path segment nobody meant to request. The positivity check
    is not cosmetic either: `-1` would produce `/t/topic/-1.rss`.

    ValueError rather than a bespoke type: a caller reaching here with a non-id has
    a programming error, and the sync orchestrator classifies it as permanent like
    any other validation failure.
    """
    if isinstance(topic_id, bool) or not isinstance(topic_id, int):
        raise ValueError(f"topic id must be an int, got {type(topic_id).__name__}")

    if topic_id < 1:
        raise ValueError(f"topic id must be positive, got {topic_id}")

    return topic_id


# ----------------------------------------------------------------------
# Post numbering
# ----------------------------------------------------------------------


def _post_number_pattern(topic_id: int) -> re.Pattern[str]:
    """Matches the floor suffix of a URL belonging to *this* topic.

    Both spellings occur in the wild and both are handled by the optional slug
    group: `/t/topic/2837720/3` (what the channel feed carries) and
    `/t/share-a-tool/2837720/3` (what Discourse itself emits).

    The topic id is baked into the pattern instead of captured, so a URL pointing at
    a *different* topic simply does not match. That matters: reading a floor number
    out of someone else's link would write a wrong `(topic_id, post_number)` pair —
    a unique key — and the mistake would be invisible afterwards.
    """
    return re.compile(rf"/t/(?:[^/]+/)?{topic_id}/(\d+)")


def _derived_post_number(pattern: re.Pattern[str], item: RssItem) -> int | None:
    """The post number from the item's own URLs, or None.

    `link` before `guid` because `link` is the address of the post, while a `guid`
    is only required to be unique — Discourse happens to use the same URL for both,
    but a `tag:` guid is equally legal and carries no number.
    """
    for value in (item.link, item.guid):
        if not value:
            continue

        match = pattern.search(value)
        if match is None:
            continue

        number = int(match.group(1))
        if 1 <= number <= MAX_POST_NUMBER:
            return number

    return None


def _assign_post_numbers(topic_id: int, items: list[RssItem]) -> list[int]:
    """One number per item, guaranteed distinct.

    Distinctness is the point. `topic_posts` has `UNIQUE (topic_id, post_number)`
    and a topic is written in a single `batch()` — one duplicate would abort the
    batch and lose every good post in the topic along with the bad one.

    Two cases need a fallback:

      * no URL on the item carries a floor suffix (a generator that emits `tag:`
        guids and a slug-only link);
      * two items claim the same number, which means the feed contradicts itself.

    Both get the lowest number no derived value has claimed. That is safe against
    the unique constraint for this feed, and it is deterministic for a given
    document, so re-running the same feed writes the same rows. It is *not* stable
    across feed revisions — a later run whose feed gained an item could number a
    numberless post differently and insert a second row. Accepted: Discourse always
    puts the floor suffix in both `link` and `guid`, so the fallback is a guard
    against an unknown generator rather than a path the real source takes, and
    dropping such a post instead would silently lose the topic's only content. The
    partial unique index on `guid` catches the duplicate whenever a guid exists.
    """
    pattern = _post_number_pattern(topic_id)
    derived = [_derived_post_number(pattern, item) for item in items]
    claimed = {number for number in derived if number is not None}

    numbers: list[int] = []
    used: set[int] = set()
    next_free = 1

    for number in derived:
        if number is not None and number not in used:
            used.add(number)
            numbers.append(number)
            continue

        while next_free in claimed or next_free in used:
            next_free += 1

        used.add(next_free)
        numbers.append(next_free)

    return numbers


# ----------------------------------------------------------------------
# Results
# ----------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class RetainedPost:
    """One post that survived the selection rules — a `topic_posts` row.

    `text` is already plain text: raw RSS HTML must never reach the database or the
    browser. `source_url` is built from the topic id and the post number rather than
    copied from the feed, because it is rendered as a link in the frontend and a
    stored link is only as trustworthy as whoever wrote the feed.
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
    """A whole topic, normalized. `posts` is ordered by `post_number` ascending."""

    topic_id: int
    canonical_url: str
    title: str | None
    author: str | None
    published_at: str | None
    posts: tuple[RetainedPost, ...]


# ----------------------------------------------------------------------
# Selection
# ----------------------------------------------------------------------

# Element names that carry a post's author. Discourse emits `<dc:creator>`, which
# `feeds/rss.py` surfaces as the extra `creator` (namespace stripped, lowercased);
# Atom's `<author><name>` arrives as `author`. Neither is one of the five fields
# `RssItem` names, so `extras` is where they land.
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


def _original_content(item: RssItem) -> str:
    """The text the reply-retention rule is allowed to judge.

    The PRD says "original content", so this is the description's raw HTML — checked
    *before* the conversion to text, which is the only lossy step. The anchor hrefs
    are appended because `html.parser` decodes character references inside attribute
    values: `href="https://github.com&#x2F;owner&#x2F;repo"` is a repository to
    `extract_links` and is unreadable to any regex run over the markup itself.

    Deliberately NOT included:

      * `title` — Discourse repeats the topic title verbatim in every item, so one
        repository URL in the title would retain every reply in the topic and defeat
        the rule entirely.
      * `link` and `guid` — linux.do URLs by construction; a GitHub URL cannot
        appear there without the feed being forged, and scanning them only adds
        ways to be wrong.
    """
    body = item.description
    if not body:
        return ""

    links = extract_links(body)
    if not links:
        return body

    return body + "\n" + "\n".join(links)


def parse_topic_feed(
    topic_id: int, feed_text: str, *, max_bytes: int = MAX_FEED_BYTES
) -> TopicFeed:
    """Normalize one topic feed. Pure — no I/O, no clock, no randomness.

    The first post is identified by the *lowest post number present*, not by
    position: item order is the generator's choice (Discourse serves oldest-first,
    a proxy or a "latest posts" view may not) and building on it would mark a reply
    as the topic's opening post.

    The lowest number present, rather than strictly `post_number == 1`, guarantees
    the topic always yields at least one post. If a feed ever truncates away the
    real first post, the earliest post it does carry is both the best available
    context for the classifier and a single deterministic row; requiring a literal
    `1` would instead turn that topic into a silent zero-post result that looks
    exactly like an empty one.
    """
    canonical = canonical_topic_url(topic_id)

    items = parse_items(parse_feed(feed_text, max_bytes=max_bytes))
    numbers = _assign_post_numbers(topic_id, items)
    first_number = min(numbers) if numbers else None

    first_item: RssItem | None = None
    posts: list[RetainedPost] = []

    for item, number in zip(items, numbers, strict=True):
        is_first = number == first_number

        if is_first:
            first_item = item
        elif not contains_repository_url(_original_content(item)):
            # A reply with no explicit repository URL is discarded here and never
            # reaches the database or the LLM. That discard is the PRD's cost
            # control, and it is why the check runs on the original HTML.
            continue

        posts.append(
            RetainedPost(
                post_number=number,
                guid=item.guid,
                author=_author_of(item),
                published_at=rss_datetime_to_iso(item.pub_date),
                source_url=f"{canonical}/{number}",
                text=html_to_text(item.description or ""),
                is_first_post=is_first,
            )
        )

    posts.sort(key=lambda post: post.post_number)

    return TopicFeed(
        topic_id=topic_id,
        canonical_url=canonical,
        # Topic-level metadata is the first post's: a Discourse topic's title,
        # author and publication time *are* those of its opening post. Reading the
        # channel-level `<title>` instead would need this module to walk the element
        # tree itself and would report the feed's title, which a proxy may rewrite.
        title=first_item.title if first_item is not None else None,
        author=_author_of(first_item) if first_item is not None else None,
        published_at=rss_datetime_to_iso(first_item.pub_date) if first_item is not None else None,
        posts=tuple(posts),
    )


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


async def read_topic_feed(
    fetch_text: FetchTextPort,
    topic_id: int,
    *,
    timeout_seconds: float,
    max_bytes: int,
) -> TopicFeed:
    """Fetch and normalize one topic.

    `timeout_seconds` and `max_bytes` have no defaults on purpose — the PRD requires
    every external request to be bounded, and a default here would let a caller that
    forgot its `Settings` look correct.

    The same `max_bytes` bounds the transport and the parser. They are separate
    checks (a lying `content-length`, a chunked body) and both must hold, but there
    is one number so a response can never be small enough to accept and too large to
    parse.

    `FetchError` from the transport is left to propagate: it already carries the
    `retryable` flag the orchestrator classifies on, and only the caller knows
    whether this topic has retries left.
    """
    response = await fetch_text(
        topic_feed_url(topic_id),
        timeout_seconds=timeout_seconds,
        max_bytes=max_bytes,
    )

    return parse_topic_feed(topic_id, response.text, max_bytes=max_bytes)
