"""Channel feed reader — where every topic candidate enters the system.

Reads the one configured RSSHub feed and returns validated linux.do topic ids.
That is the entire output: **ids, not URLs**.

THE SECURITY RULE, and the reason this module exists (PRD: "RSS 中提取出的抓取地址
经过严格白名单校验，以便阻止 SSRF"): a URL found in the feed is never used as a
fetch target. This module extracts the numeric topic id, validates it, and then
throws the URL away. The URL the topic reader later fetches is CONSTRUCTED from
that int by `canonical_topic_feed_url`. The signatures carry the rule:
`extract_topic_ids` and `extract_topic_candidates` return `list[int]`, so there is
no path by which feed-controlled text can reach a fetch call — not by convention,
by construction. Anyone who wants to break it has to change a type.

Where the ids actually are (verified against the live feed, 2026-08-31): the RSS
2.0 items carry only title/description/link/guid/pubDate; `<link>` points at
Telegram (`https://t.me/linux_do_channel/492492`) and the linux.do URL lives inside
the HTML-escaped `<description>` with a floor suffix
(`https://linux.do/t/topic/2837720/1`), next to `onclick` attributes. Two
consequences: every text-bearing field is scanned and `<link>` is not special, and
the description is hostile input — it is scanned, never trusted and never returned.

Two scanners, because either shape can carry the link: a regex over the field text,
and a structural pass over `href` attributes using stdlib `html.parser`. The
structural pass is not redundant — an `href` whose value is entity-encoded
(`.../t/topic/&#50;837720/1`) is invisible to a text regex and unambiguous to a
parser, and an attribute value is a URL by definition rather than by a regex's
guess about where the URL ends.

Pure except for one injected callable. `fetch_text` arrives as a parameter, so this
module never imports `workers` and the whole reader — network path included — runs
under plain CPython pytest against a fake transport.
"""

from __future__ import annotations

import re
from html.parser import HTMLParser
from typing import Protocol
from urllib.parse import urlsplit

from linuxdo_oss.feeds.rss import item_text_fields, parse_items
from linuxdo_oss.feeds.xml_safe import MAX_FEED_BYTES, parse_feed

__all__ = [
    "MAX_TOPIC_ID",
    "TOPIC_HOST",
    "FeedResponse",
    "FetchText",
    "canonical_topic_feed_url",
    "canonical_topic_url",
    "extract_topic_candidates",
    "extract_topic_ids",
    "read_channel_feed",
    "topic_id_from_url",
]

# ----------------------------------------------------------------------
# Host rules
# ----------------------------------------------------------------------

# The one host, used for BOTH ends of the job: it is the only accepted host and it
# is the host of every URL this module builds. One constant means the allowlist and
# the constructed URL cannot drift apart.
TOPIC_HOST = "linux.do"

# Exact match, and exactly one entry.
#
# `www.linux.do` is deliberately NOT accepted, which is where this diverges from
# domain/github_url.py (that one allows `www.github.com`). The difference is real:
# both github.com spellings serve content and both appear in the wild, whereas a
# Discourse instance has a single canonical hostname and every link the site
# generates — including the one in the verified feed — uses the bare host. The cost
# of rejecting is also asymmetric: because the fetch target is constructed from the
# id, a rejected lookalike costs at most one missed discovery and never a wrong
# request. So the allowlist stays literally the host the PRD names, which keeps the
# rule reviewable against the PRD. If a www link ever shows up in a real feed this
# is one more frozenset entry plus one test.
_ALLOWED_HOSTS = frozenset({TOPIC_HOST})

# https only, per the PRD ("Only HTTPS URLs on host `linux.do` ... are accepted").
# An `http://linux.do/...` link is either ancient or forged — linux.do redirects to
# https — and, again, dropping it loses one discovery rather than admitting a bad
# fetch. Note the scheme is checked in `topic_id_from_url` and not in the regex, so
# that every accept/reject decision has exactly one implementation.
_ALLOWED_SCHEMES = frozenset({"https"})

# ----------------------------------------------------------------------
# Path and id rules
# ----------------------------------------------------------------------

# The literal path prefix. linux.do topics have Chinese titles, so Discourse falls
# back to the placeholder slug `topic`, which is why the PRD can name the path
# exactly instead of allowing an arbitrary slug segment.
_TOPIC_PATH_PREFIX = ("t", "topic")

# Nine digits.
#
# The live maximum is ~2,837,720 (seven digits), so this is ~350x headroom on a
# monotonically increasing Discourse sequence — decades of it. The bound is not
# cosmetic:
#
#   * It is enforced by DIGIT COUNT in the pattern below, so a 40-digit hostile
#     number is refused as text and never becomes an arbitrary-precision int.
#   * A topic id crosses into JavaScript twice (the D1 binding, and the JSON the
#     frontend reads), and JS integers are only exact below 2**53. Staying nine
#     digits keeps the value exact everywhere, not just inside SQLite's 64-bit
#     INTEGER.
MAX_TOPIC_ID_DIGITS = 9
MAX_TOPIC_ID = 10**MAX_TOPIC_ID_DIGITS - 1

# One anchored pattern kills three special cases at once: a leading zero (`007`) is
# unmatchable, `0` is unmatchable, and the length is capped. Derived from
# MAX_TOPIC_ID_DIGITS so the pattern and the number cannot disagree.
_TOPIC_ID_RE = re.compile(rf"^[1-9][0-9]{{0,{MAX_TOPIC_ID_DIGITS - 1}}}$")

# The optional floor suffix (`/t/topic/2837720/1`). Digits only, and never converted
# to an int — the floor is discarded, so only its shape is checked. Bounded for the
# same reason as the id: an unbounded `[0-9]+` invites a megabyte-long segment.
_FLOOR_RE = re.compile(r"^[0-9]{1,9}$")

# ----------------------------------------------------------------------
# Extraction
# ----------------------------------------------------------------------

# Deliberately broad on the HOST so that lookalikes (`linux.do.evil.example`) are
# captured in full and then rejected by `topic_id_from_url`, rather than being
# trimmed by the regex into something that looks acceptable.
#
# The URL body is an ALLOWLIST of the ASCII characters RFC 3986 permits after the
# authority. A "not whitespace" body would be wrong here: channel text is Chinese
# and has no spaces, so `https://linux.do/t/topic/2837720/1，另外` would be
# swallowed whole. An ASCII-only body makes every CJK character and every full-width
# punctuation mark terminate the match by itself.
#
# The leading guard is an explicit ASCII class rather than `(?<!\w)`: `\w` matches
# CJK, and Chinese prose regularly glues a URL straight onto a character
# (`原帖https://linux.do/t/topic/2837720`). With `\w` that link is silently dropped —
# the worst kind of bug here, since nothing fails. The class still blocks the two
# cases the guard is for: a match starting inside a longer host (`notlinux.do`) and
# a match starting after userinfo (`user@linux.do`).
_URL_RE = re.compile(
    r"""(?<![A-Za-z0-9@/._-])
        (?P<url>
            https?://
            (?:[A-Za-z0-9-]+\.)*          # optional leading subdomains
            linux\.do
            (?:\.[A-Za-z0-9-]+)*          # trailing labels, so lookalikes match in full
            (?::[0-9]+)?                  # port
            (?:[/?\#][A-Za-z0-9\-._~%!$&'()*+,;=:@/?\#\[\]]*)?
        )
    """,
    re.VERBOSE | re.IGNORECASE,
)

# Prose punctuation that ends a sentence rather than a URL. ASCII only, because the
# body allowlist above already stops the match at any full-width character.
#
# Unlike domain/github_url.py this needs no balanced-parenthesis exception: a topic
# path is digits and slashes, so a `)` is never part of one. It does need to exist,
# though — `.../t/topic/2837720.` would otherwise parse its last segment as
# `2837720.` and be rejected, losing the topic.
_TRAILING_JUNK = ".,;:!?)]}'\">"

# ----------------------------------------------------------------------
# The injected transport
# ----------------------------------------------------------------------


class FeedResponse(Protocol):
    """The one thing this reader needs from a fetch result.

    A read-only property, not a mutable attribute, so `adapters.http.FetchResult` —
    a frozen dataclass — satisfies it.
    """

    @property
    def text(self) -> str: ...


class FetchText(Protocol):
    """The egress port: `adapters.http.fetch_text` structurally.

    Declared here rather than imported, because importing `adapters.http` would drag
    `from workers import fetch` into this module's import graph and make the pure
    test suite unimportable on CPython. `fetch_text`'s extra keyword arguments
    (`method`, `headers`, `body`) all have defaults, so it satisfies this narrower
    shape.
    """

    async def __call__(
        self, url: str, *, timeout_seconds: float, max_bytes: int
    ) -> FeedResponse: ...


# ----------------------------------------------------------------------
# URL construction — the only place a fetchable URL is produced
# ----------------------------------------------------------------------


def canonical_topic_url(topic_id: int) -> str:
    """The canonical human URL for a topic (`topics.canonical_url`)."""
    return f"https://{TOPIC_HOST}/t/topic/{_checked_topic_id(topic_id)}"


def canonical_topic_feed_url(topic_id: int) -> str:
    """The canonical RSS URL for a topic — the topic reader's fetch target.

    Built from an int that has passed `_checked_topic_id`, which is what makes the
    SSRF rule structural: there is no overload taking a string, so no feed-supplied
    text can become a request.
    """
    return f"https://{TOPIC_HOST}/t/topic/{_checked_topic_id(topic_id)}.rss"


def _checked_topic_id(topic_id: int) -> int:
    """Reject anything that is not a usable topic id, loudly.

    Raises rather than returning None: the callers build a URL, and there is no
    sensible URL for a bad id. This is the chokepoint both constructors share, so
    `canonical_topic_url(-1)` cannot exist even as a transient string.

    The isinstance check is not ceremony — it is what stops a `str` reaching an
    f-string and producing `https://linux.do/t/topic/../../evil` from a caller that
    lost track of its types.
    """
    if not isinstance(topic_id, int) or not 1 <= topic_id <= MAX_TOPIC_ID:
        raise ValueError(f"not a usable linux.do topic id: {topic_id!r}")

    return topic_id


# ----------------------------------------------------------------------
# Validation — the single accept/reject decision
# ----------------------------------------------------------------------


def topic_id_from_url(url: str) -> int | None:
    """The topic id `url` refers to, or None if it is not a linux.do topic URL.

    Returning None rather than raising is deliberate: a channel item full of
    Telegram, GitHub and lookalike links is ordinary content, not an error.

    Every rule lives here, so there is one implementation of "is this ours":
    scheme https, host exactly `linux.do`, path `/t/topic/<digits>` with at most a
    numeric floor suffix. Query and fragment are ignored — they carry tracking
    parameters (`?u=someone`) and anchors (`#post_2`), never identity.
    """
    candidate = url.strip()
    if not candidate:
        return None

    try:
        parts = urlsplit(candidate)
    except ValueError:
        # Malformed authority, e.g. an unterminated IPv6 literal.
        return None

    if parts.scheme.lower() not in _ALLOWED_SCHEMES:
        return None

    # `hostname` lowercases and strips the port AND any userinfo. That last part is
    # what rejects `https://linux.do@evil.example/t/topic/1`: the host is
    # `evil.example`, which no amount of string matching on the raw URL would tell
    # you reliably.
    host = parts.hostname
    if host is None or host.lower() not in _ALLOWED_HOSTS:
        return None

    segments = [segment for segment in parts.path.split("/") if segment]

    # Exactly the id, or the id plus one floor number. Anything longer is a shape
    # this project has never seen, and guessing at it is how `/t/topic/1/../../x`
    # becomes acceptable.
    if len(segments) not in {3, 4}:
        return None
    if tuple(segments[:2]) != _TOPIC_PATH_PREFIX:
        return None
    if len(segments) == 4 and not _FLOOR_RE.match(segments[3]):
        return None
    if not _TOPIC_ID_RE.match(segments[2]):
        return None

    return int(segments[2])


# ----------------------------------------------------------------------
# Scanning
# ----------------------------------------------------------------------


def extract_topic_ids(text: str) -> list[int]:
    """Every topic id in one field's text or HTML, first appearance first.

    Pure, and deliberately usable on its own: the same rules must apply whether the
    text came from `<title>`, from `<guid>` or from inside the description's HTML.

    The plain scan runs first and is positional, so the normal case — an `href`
    whose value is also readable as text — comes out in document order. The `href`
    pass can then only ADD ids the plain scan was unable to see (an entity-encoded
    or otherwise obscured attribute value); those are appended. Order for everything
    the plain scan can see is therefore exactly first-appearance order.
    """
    if not text:
        return []

    found: dict[int, None] = {}

    for match in _URL_RE.finditer(text):
        topic_id = topic_id_from_url(match.group("url").rstrip(_TRAILING_JUNK))
        if topic_id is not None:
            found[topic_id] = None

    for href in _href_targets(text):
        topic_id = topic_id_from_url(href)
        if topic_id is not None:
            found[topic_id] = None

    return list(found)


def extract_topic_candidates(feed_text: str, *, max_bytes: int = MAX_FEED_BYTES) -> list[int]:
    """Every topic id in a whole channel feed document, globally deduplicated.

    Pure: the caller has already got the bytes from somewhere. Raises
    `XmlSafetyError` (and nothing else) for a document that is oversized, declares a
    DTD, or is malformed — `feeds/xml_safe.py` owns that judgement and one exception
    type is what lets the orchestrator file all of them as a permanent failure.

    Deduplication is global rather than per item because the same topic is
    legitimately posted to the channel more than once (a new reply produces a new
    Telegram message), and re-fetching it inside one run would waste subrequests
    against a bounded cron budget. Order is first appearance across the whole
    document: item order, then field order within an item.
    """
    root = parse_feed(feed_text, max_bytes=max_bytes)

    found: dict[int, None] = {}

    for item in parse_items(root):
        # Every text-bearing field, not `<link>`: in the verified feed `<link>` is a
        # Telegram URL and the topic link is inside the description HTML.
        for field in item_text_fields(item):
            for topic_id in extract_topic_ids(field):
                found[topic_id] = None

    return list(found)


async def read_channel_feed(
    fetch_text: FetchText,
    feed_url: str,
    *,
    timeout_seconds: float,
    max_bytes: int,
) -> list[int]:
    """Fetch the configured channel feed and return its topic candidates.

    `feed_url` comes from `Settings.channel_feed_url` — deployment configuration,
    the PRD's "RSS fetch destinations use fixed configuration". It is the ONLY URL
    this function ever fetches: nothing derived from the response body is ever
    passed back to `fetch_text`, which is exactly what a test asserts.

    Errors propagate untranslated. `FetchError` already carries `retryable` (set
    where the status is known) and `XmlSafetyError` is always permanent; re-wrapping
    them here would only lose that classification.
    """
    response = await fetch_text(feed_url, timeout_seconds=timeout_seconds, max_bytes=max_bytes)

    return extract_topic_candidates(response.text, max_bytes=max_bytes)


# ----------------------------------------------------------------------
# Private helpers
# ----------------------------------------------------------------------


class _HrefCollector(HTMLParser):
    """Collects `href` attribute values from a fragment of untrusted HTML.

    Only `href`. The description's markup also carries `onclick`, `target` and
    `rel`; a topic link is an anchor target, and widening this to every attribute
    would start reading script text.
    """

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.hrefs: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        for name, value in attrs:
            if name.lower() == "href" and value:
                self.hrefs.append(value.strip())

    # `handle_startendtag` is not overridden on purpose: its default implementation
    # forwards to `handle_starttag`, so `<a href="..."/>` is already covered.


def _href_targets(html_text: str) -> list[str]:
    """Every `href` value in `html_text`, in document order.

    `html.parser` is non-strict since Python 3.5 (`HTMLParseError` was removed): it
    does not raise on malformed markup, it resynchronises. That property is what
    makes it safe to point at a hostile description without a `try` that would have
    to swallow the whole field — and it is pinned by a test rather than trusted.

    Attribute values are unescaped by the parser regardless of `convert_charrefs`,
    which is the entire reason this pass exists alongside the regex.
    """
    collector = _HrefCollector()
    collector.feed(html_text)
    collector.close()

    return collector.hrefs
