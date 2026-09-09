"""RSS 2.0 item walking, shared by the channel reader and the topic reader.

Pure: stdlib plus `domain/timestamps`, no network, no runtime bindings. Both
readers need the same three things — get the items out, find every field that
could hold a URL, turn `pubDate` into the one timestamp format — and duplicating
that in two files is how the two readers start disagreeing about what an item is.

Two shapes are handled. RSS 2.0 (`<rss><channel><item>`) is what both real feeds
serve today: a verified channel item carries exactly `title`, `description`,
`link`, `guid`, `pubDate`, with no `content:encoded` and no `dc:creator`. Atom
(`<feed><entry>`) is handled defensively because the feed generator's output
format is not this project's decision, and the failure mode of an unhandled switch
is a silent zero items rather than an error.

Three rules make that robustness cheap rather than speculative:

  * Elements are matched by **local name, lowercased** — never by prefix. A
    namespace prefix is the generator's private choice.
  * The five named fields accept every spelling of themselves (`guid`/`id`,
    `pubDate`/`published`/`updated`/`dc:date`, `description`/`content:encoded`/
    `summary`/`content`), so downstream code reads one field name.
  * Anything not claimed by a named field goes into `extras` instead of being
    dropped, which is where `dc:creator` — Discourse's author element — arrives.
    A generator that renames an element keeps working with no code change.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from xml.etree.ElementTree import Element

from linuxdo_oss.domain.timestamps import to_iso_utc

__all__ = [
    "RssItem",
    "parse_items",
    "rss_datetime_to_iso",
]

# ----------------------------------------------------------------------
# Tag vocabulary
# ----------------------------------------------------------------------

# RSS 2.0 and Atom names for "one entry in the feed".
_ITEM_TAGS = frozenset({"item", "entry"})

# Local name (lowercased) -> RssItem field. Several names map to one field because
# RSS and Atom spell the same thing differently, and because the generator could
# add a richer body element at any time. When more than one candidate is present
# the first in document order wins the field and the rest become extras — a
# positional rule rather than a preference ranking, because there is no defensible
# way to claim `summary` outranks `description` for an unknown future generator,
# and nothing is lost either way: extras are still scanned for URLs.
_FIELD_BY_TAG = {
    "title": "title",
    "link": "link",
    "description": "description",
    "encoded": "description",  # content:encoded
    "summary": "description",  # Atom
    "content": "description",  # Atom
    "guid": "guid",
    "id": "guid",  # Atom
    "pubdate": "pub_date",
    "published": "pub_date",  # Atom
    "updated": "pub_date",  # Atom
    "date": "pub_date",  # dc:date
}


# ----------------------------------------------------------------------
# Result
# ----------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class RssItem:
    """One `<item>` / `<entry>`, normalized but not interpreted.

    Every field is the text as it appeared, only whitespace-stripped: `pub_date` is
    still raw RFC 822 and `description` is still HTML. This module deliberately
    does not clean HTML or resolve links — the topic reader owns HTML-to-text and
    the channel reader owns URL allowlisting, and a parser that also sanitizes is a
    parser nobody can reason about.

    A field that is absent, empty, or whitespace-only is None. Collapsing the two
    empty cases here means no caller has to test for both.
    """

    title: str | None
    link: str | None
    description: str | None
    guid: str | None
    pub_date: str | None
    extras: tuple[tuple[str, str], ...]


# ----------------------------------------------------------------------
# Parsing
# ----------------------------------------------------------------------


def parse_items(root: Element) -> list[RssItem]:
    """Every item in the document, in document order.

    `iter()` over the whole tree rather than the `channel/item` path: RSS 2.0 nests
    items under `<channel>`, Atom puts entries directly under `<feed>`, and a proxy
    may wrap either. The item element is what identifies an item, not its depth.
    """
    return [
        _build_item(element) for element in root.iter() if _local_name(element.tag) in _ITEM_TAGS
    ]


def rss_datetime_to_iso(value: str | None) -> str | None:
    """RFC 822 `pubDate` -> the canonical ISO 8601 UTC millisecond format.

    Returns None instead of raising. A feed item with an unparseable date is
    ordinary input, not a failure: the topic still has to be processed, and only
    the caller knows whether a missing publication time matters.

    A missing zone or `-0000` is read as UTC. RFC 5322 defines `-0000` as "no
    information about the local time zone", so UTC is the only interpretation the
    format offers, and `email.utils` reports it as a naive datetime — which
    `to_iso_utc` refuses on purpose. The guess is made here, in the one place that
    knows which format was being read, rather than by loosening `to_iso_utc` for
    every caller.
    """
    if value is None:
        return None

    candidate = value.strip()
    if not candidate:
        return None

    moment = _parse_datetime(candidate)
    if moment is None:
        return None

    try:
        return to_iso_utc(moment)
    except (ValueError, OverflowError):
        # OverflowError: a year at the datetime bounds, where the shift to UTC
        # leaves the representable range.
        return None


# ----------------------------------------------------------------------
# Private helpers
# ----------------------------------------------------------------------


def _parse_datetime(candidate: str) -> datetime | None:
    """RFC 822 first, RFC 3339 second; None when neither applies.

    The second attempt exists because Atom entries are handled at all: Atom dates
    are RFC 3339 (`2026-08-31T15:18:11+08:00`), which `parsedate_to_datetime`
    rejects. Without it, `parse_items` would accept Atom entries and then silently
    lose every publication time — a half-supported format is worse than an
    unsupported one, because it fails as wrong data rather than as an error.

    Order matters: RSS 2.0 is what both real feeds serve, so its format is tried
    first and pays no cost for the fallback.
    """
    try:
        moment = parsedate_to_datetime(candidate)
    except (TypeError, ValueError):
        try:
            moment = datetime.fromisoformat(candidate)
        except (TypeError, ValueError):
            return None

    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=UTC)

    return moment


def _build_item(element: Element) -> RssItem:
    """One item, built from its direct children.

    Direct children only: `<description>` legitimately contains a whole HTML
    document, and descending into it would turn markup into fake fields.
    """
    fields: dict[str, str] = {}
    extras: list[tuple[str, str]] = []

    for child in element:
        name = _local_name(child.tag)
        if not name:
            continue

        value = _link_target(child) if name == "link" else _element_text(child)
        if value is None:
            continue

        slot = _FIELD_BY_TAG.get(name)

        if slot is not None and slot not in fields:
            fields[slot] = value
        else:
            extras.append((name, value))

    return RssItem(
        title=fields.get("title"),
        link=fields.get("link"),
        description=fields.get("description"),
        guid=fields.get("guid"),
        pub_date=fields.get("pub_date"),
        extras=tuple(extras),
    )


def _local_name(tag: object) -> str:
    """Namespace-stripped, lowercased element name; "" when there is no name.

    ElementTree reports a namespaced tag in Clark notation —
    `{http://purl.org/dc/elements/1.1/}creator` — so matching on the local name is
    the only stable option: the prefix a feed declares is its own business and can
    change without the feed changing meaning.

    Lowercasing is leniency on input only. XML is case-sensitive and the correct
    spelling is `pubDate`, but accepting `pubdate` from a sloppy generator costs
    nothing, because this module only ever reads.

    Comments and processing instructions carry a callable as their tag, hence the
    isinstance guard — reached only when a caller supplies a parser that keeps
    them, which the default one does not.
    """
    if not isinstance(tag, str):
        return ""

    if tag.startswith("{"):
        tag = tag.partition("}")[2]

    return tag.lower()


def _element_text(element: Element) -> str | None:
    """All text inside `element`, stripped, or None when there is none.

    `itertext` rather than `.text` because the two feed shapes store a body
    differently: RSS `<description>` holds escaped HTML that arrives as `.text`,
    while Atom `<content type="xhtml">` holds real child elements. Joining the
    subtree covers both without asking which one this is.
    """
    text = "".join(element.itertext()).strip()

    return text or None


def _link_target(element: Element) -> str | None:
    """The URL a `<link>` points at, in either spelling.

    RSS 2.0 puts it in the element text; Atom puts it in `href` and leaves the
    element empty. Text wins when both exist, because that is the RSS form and RSS
    is what both real feeds serve.
    """
    text = _element_text(element)
    if text is not None:
        return text

    href = (element.get("href") or "").strip()

    return href or None
