"""Tests for the shared RSS item walker.

Both feed readers stand on this module, so its two failure modes are expensive: an
item field that quietly goes missing means a topic is never discovered, and a
`pubDate` that quietly becomes None means the feed cannot be ordered newest-first.

Fixtures are shaped like the real responses (verified 2026-08-31): the channel feed
is RSS 2.0 whose items carry only title/description/link/guid/pubDate, `link` points
at Telegram rather than linux.do, and the topic URL is inside the HTML in
`description` — with a floor suffix and with hostile `onclick` attributes.

Documents go through `parse_feed` rather than `ElementTree.fromstring`, because that
is the only way the readers are allowed to parse and it keeps the two modules
honest about working together.
"""

from __future__ import annotations

import pytest

from linuxdo_oss.domain.timestamps import is_iso_utc
from linuxdo_oss.feeds.rss import RssItem, item_text_fields, parse_items, rss_datetime_to_iso
from linuxdo_oss.feeds.xml_safe import parse_feed

# ----------------------------------------------------------------------
# Fixtures
# ----------------------------------------------------------------------

# The first description exactly as the live feed serves it: HTML-escaped, a floor
# suffix on the topic URL, and an onclick attribute. Assembled from pieces only
# because one source line would exceed the line-length limit; the value is
# unchanged, and no whitespace the real feed does not have is introduced.
ITEM_ONE_DESCRIPTION = (
    "&lt;p&gt;分享一个开源小工具&lt;/p&gt;"
    "&lt;a href=&quot;https://linux.do/t/topic/2837720/1&quot; "
    "target=&quot;_blank&quot; rel=&quot;noopener&quot; "
    "onclick=&quot;return confirm('open?')&quot;&gt;linux.do&lt;/a&gt;"
)

# Same shape, a different topic and a different floor suffix.
ITEM_TWO_DESCRIPTION = "&lt;a href=&quot;https://linux.do/t/topic/2837721/3&quot;&gt;x&lt;/a&gt;"

# The real channel-feed shape: RSS 2.0, five elements per item, `link` pointing at
# Telegram, and the linux.do topic URL only reachable through the description HTML.
CHANNEL_FEED = f"""<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0">
  <channel>
    <title>Linux.do Channel</title>
    <link>https://t.me/s/linux_do_channel</link>
    <ttl>5</ttl>
    <item>
      <title>@Ammdjs 在 分享一个开源小工具 中发帖</title>
      <description>{ITEM_ONE_DESCRIPTION}</description>
      <link>https://t.me/linux_do_channel/492492</link>
      <guid isPermaLink="false">https://t.me/linux_do_channel/492492</guid>
      <pubDate>Mon, 31 Aug 2026 15:18:11 GMT</pubDate>
    </item>
    <item>
      <title>linmao 在 另一个主题 中发帖</title>
      <description>{ITEM_TWO_DESCRIPTION}</description>
      <link>https://t.me/linux_do_channel/492493</link>
      <guid isPermaLink="false">https://t.me/linux_do_channel/492493</guid>
      <pubDate>Mon, 31 Aug 2026 16:00:00 GMT</pubDate>
    </item>
  </channel>
</rss>
"""

# Namespaced RSS: content:encoded as the body, dc:creator as an unmapped element,
# dc:date as the timestamp. No prefix appears in the parser — matching is by local
# name — so a generator renaming `content:` to `c:` changes nothing.
NAMESPACED_RSS = """<?xml version="1.0"?>
<rss version="2.0"
     xmlns:content="http://purl.org/rss/1.0/modules/content/"
     xmlns:dc="http://purl.org/dc/elements/1.1/">
  <channel>
    <item>
      <title>namespaced item</title>
      <dc:creator>someone</dc:creator>
      <content:encoded>&lt;p&gt;https://github.com/owner/repo&lt;/p&gt;</content:encoded>
      <dc:date>Mon, 31 Aug 2026 15:18:11 GMT</dc:date>
      <guid>tag:example,2026:1</guid>
    </item>
  </channel>
</rss>
"""

# Atom. Every element is in the Atom namespace, `link` carries its target in an
# attribute, `id` is the guid, `published` is the date and `content type="xhtml"`
# holds real child elements rather than escaped text.
ATOM_FEED = """<?xml version="1.0"?>
<feed xmlns="http://www.w3.org/2005/Atom"
      xmlns:xh="http://www.w3.org/1999/xhtml">
  <title>channel level title, not an item field</title>
  <entry>
    <title>first post</title>
    <link rel="alternate" href="https://linux.do/t/topic/2837720/1"/>
    <id>tag:linux.do,2026:/t/topic/2837720/1</id>
    <published>2026-08-31T15:18:11+00:00</published>
    <summary type="html">&lt;p&gt;see https://github.com/owner/repo&lt;/p&gt;</summary>
  </entry>
  <entry>
    <title>reply</title>
    <link rel="alternate" href="https://linux.do/t/topic/2837720/2"/>
    <id>tag:linux.do,2026:/t/topic/2837720/2</id>
    <updated>2026-08-31T16:00:00Z</updated>
    <content type="xhtml"><xh:div>nested <b>markup</b> body</xh:div></content>
  </entry>
</feed>
"""


def items_of(document: str) -> list[RssItem]:
    return parse_items(parse_feed(document))


# ----------------------------------------------------------------------
# The real channel-feed shape
# ----------------------------------------------------------------------


def test_channel_items_are_returned_in_document_order() -> None:
    items = items_of(CHANNEL_FEED)

    assert [item.guid for item in items] == [
        "https://t.me/linux_do_channel/492492",
        "https://t.me/linux_do_channel/492493",
    ]


def test_channel_item_fields_are_taken_verbatim() -> None:
    item = items_of(CHANNEL_FEED)[0]

    assert item.title == "@Ammdjs 在 分享一个开源小工具 中发帖"
    # The verified feed points `link` at Telegram. Discovery must not rely on it.
    assert item.link == "https://t.me/linux_do_channel/492492"
    assert item.guid == "https://t.me/linux_do_channel/492492"
    # Raw RFC 822, unconverted: conversion is a separate, testable step.
    assert item.pub_date == "Mon, 31 Aug 2026 15:18:11 GMT"
    # The channel feed has exactly the five known elements, so nothing is left over.
    assert item.extras == ()


def test_topic_url_survives_inside_the_description_html() -> None:
    """The whole point of scanning `description`: the linux.do link lives there, with
    a floor suffix, and nowhere else."""
    item = items_of(CHANNEL_FEED)[0]

    assert item.description is not None
    assert 'href="https://linux.do/t/topic/2837720/1"' in item.description


def test_description_is_not_sanitized_here() -> None:
    """Hostile attributes are preserved deliberately.

    This module normalizes; it does not clean. HTML-to-text belongs to the topic
    reader, and a parser that silently strips things is a parser whose output nobody
    can compare against the source.
    """
    item = items_of(CHANNEL_FEED)[0]

    assert item.description is not None
    assert "onclick=" in item.description


def test_channel_level_elements_are_not_item_fields() -> None:
    """`<channel><title>` must not leak into an item."""
    items = items_of(CHANNEL_FEED)

    assert all(item.title != "Linux.do Channel" for item in items)
    assert all(item.link != "https://t.me/s/linux_do_channel" for item in items)


# ----------------------------------------------------------------------
# Missing, empty and duplicated fields
# ----------------------------------------------------------------------

SPARSE_ITEMS = [
    # (document body inside <channel>, expected field tuple as
    #  (title, link, description, guid, pub_date))
    ("<item><title>only a title</title></item>", ("only a title", None, None, None, None)),
    ("<item></item>", (None, None, None, None, None)),
    ("<item><title></title><link></link></item>", (None, None, None, None, None)),
    ("<item><title>   \n\t </title></item>", (None, None, None, None, None)),
    (
        "<item><guid>g1</guid><pubDate>Mon, 31 Aug 2026 15:18:11 GMT</pubDate></item>",
        (None, None, None, "g1", "Mon, 31 Aug 2026 15:18:11 GMT"),
    ),
    (
        "<item><description>body</description><link>https://example.com/a</link></item>",
        (None, "https://example.com/a", "body", None, None),
    ),
    # Whitespace around a value is stripped; the value itself is untouched.
    ("<item><title>  padded  </title></item>", ("padded", None, None, None, None)),
]


@pytest.mark.parametrize(("body", "expected"), SPARSE_ITEMS)
def test_missing_and_empty_fields_become_none(body: str, expected: tuple[str | None, ...]) -> None:
    """Absent, empty and whitespace-only all collapse to None, so no caller has to
    check for two kinds of nothing."""
    item = items_of(f"<rss><channel>{body}</channel></rss>")[0]

    assert (item.title, item.link, item.description, item.guid, item.pub_date) == expected


def test_a_document_with_no_items_yields_no_items() -> None:
    assert items_of("<rss><channel><title>empty feed</title></channel></rss>") == []


def test_duplicate_field_elements_keep_the_first_and_retain_the_rest() -> None:
    """First in document order wins the field; the loser becomes an extra rather
    than being discarded, so a URL hidden in it is still scanned."""
    document = (
        "<rss><channel><item>"
        "<description>first body</description>"
        "<description>second body https://github.com/owner/repo</description>"
        "<title>t</title><title>ignored title</title>"
        "</item></channel></rss>"
    )

    item = items_of(document)[0]

    assert item.description == "first body"
    assert item.title == "t"
    assert item.extras == (
        ("description", "second body https://github.com/owner/repo"),
        ("title", "ignored title"),
    )
    assert "second body https://github.com/owner/repo" in item_text_fields(item)


def test_unknown_elements_are_kept_as_extras() -> None:
    """A generator change must not silently stop topic discovery."""
    document = (
        "<rss><channel><item>"
        "<title>t</title>"
        "<source>https://linux.do/t/topic/2837720/1</source>"
        "<category></category>"
        "</item></channel></rss>"
    )

    item = items_of(document)[0]

    # An element with no text is not an extra — there is nothing to scan.
    assert item.extras == (("source", "https://linux.do/t/topic/2837720/1"),)


def test_html_inside_a_description_does_not_become_fields() -> None:
    """`<description>` legitimately contains a `<link>`. Only direct children of the
    item are read, so escaped markup cannot forge a field."""
    document = (
        "<rss><channel><item>"
        "<title>t</title>"
        "<description>&lt;link&gt;https://evil.example/&lt;/link&gt;</description>"
        "</item></channel></rss>"
    )

    item = items_of(document)[0]

    assert item.link is None


# ----------------------------------------------------------------------
# Namespaces
# ----------------------------------------------------------------------


def test_namespaced_elements_are_matched_by_local_name() -> None:
    item = items_of(NAMESPACED_RSS)[0]

    assert item.title == "namespaced item"
    # content:encoded fills the body slot; dc:date fills the date slot.
    assert item.description == "<p>https://github.com/owner/repo</p>"
    assert item.pub_date == "Mon, 31 Aug 2026 15:18:11 GMT"
    assert item.guid == "tag:example,2026:1"
    # dc:creator maps to no field, so it is kept with its prefix stripped.
    assert item.extras == (("creator", "someone"),)


def test_uppercase_and_odd_case_element_names_are_accepted() -> None:
    """Leniency on input only: `pubDate` is the correct spelling, but reading a
    sloppy generator's `PUBDATE` costs nothing."""
    document = (
        "<rss><channel><item>"
        "<TITLE>t</TITLE><PUBDATE>Mon, 31 Aug 2026 15:18:11 GMT</PUBDATE>"
        "</item></channel></rss>"
    )

    item = items_of(document)[0]

    assert item.title == "t"
    assert item.pub_date == "Mon, 31 Aug 2026 15:18:11 GMT"


# ----------------------------------------------------------------------
# Atom
# ----------------------------------------------------------------------


def test_atom_entries_are_found() -> None:
    items = items_of(ATOM_FEED)

    assert len(items) == 2
    assert [item.title for item in items] == ["first post", "reply"]
    # The feed-level <title> is not an entry.
    assert all(item.title != "channel level title, not an item field" for item in items)


def test_atom_link_target_comes_from_the_href_attribute() -> None:
    item = items_of(ATOM_FEED)[0]

    assert item.link == "https://linux.do/t/topic/2837720/1"


def test_atom_id_and_published_fill_guid_and_pub_date() -> None:
    first, second = items_of(ATOM_FEED)

    assert first.guid == "tag:linux.do,2026:/t/topic/2837720/1"
    assert first.pub_date == "2026-08-31T15:18:11+00:00"
    # `updated` is accepted where `published` is absent.
    assert second.pub_date == "2026-08-31T16:00:00Z"


def test_atom_summary_and_xhtml_content_fill_the_description() -> None:
    """Atom stores a body two different ways; both land in one field."""
    first, second = items_of(ATOM_FEED)

    assert first.description == "<p>see https://github.com/owner/repo</p>"
    # `content type="xhtml"` holds real elements, so the subtree text is joined.
    assert second.description == "nested markup body"


# ----------------------------------------------------------------------
# Dates
# ----------------------------------------------------------------------

# Measured against CPython 3.13's `email.utils`, not assumed.
PARSEABLE_DATES = [
    # The verified channel-feed format.
    ("Mon, 31 Aug 2026 15:18:11 GMT", "2026-08-31T15:18:11.000Z"),
    ("Mon, 31 Aug 2026 15:18:11 +0000", "2026-08-31T15:18:11.000Z"),
    # RFC 5322 reads `-0000` as "zone unknown"; UTC is the only interpretation the
    # format offers, and `email.utils` hands it back as a naive datetime.
    ("Mon, 31 Aug 2026 15:18:11 -0000", "2026-08-31T15:18:11.000Z"),
    ("Mon, 31 Aug 2026 15:18:11", "2026-08-31T15:18:11.000Z"),
    # Numeric offsets are converted, not recorded.
    ("Mon, 31 Aug 2026 23:18:11 +0800", "2026-08-31T15:18:11.000Z"),
    ("Mon, 31 Aug 2026 10:18:11 -0500", "2026-08-31T15:18:11.000Z"),
    # An offset that moves the date backwards across midnight — the case that breaks
    # any implementation that formats the local date and appends "Z".
    ("Mon, 31 Aug 2026 01:18:11 +0800", "2026-08-30T17:18:11.000Z"),
    ("Mon, 31 Aug 2026 15:18:11 EST", "2026-08-31T20:18:11.000Z"),
    ("Mon, 31 Aug 2026 15:18:11 UT", "2026-08-31T15:18:11.000Z"),
    # Optional weekday, and a wrong weekday, are both tolerated by RFC 822 readers.
    ("31 Aug 2026 15:18:11 GMT", "2026-08-31T15:18:11.000Z"),
    ("Tue, 31 Aug 2026 15:18:11 GMT", "2026-08-31T15:18:11.000Z"),
    # Two-digit year, per RFC 2822's mapping.
    ("Mon, 31 Aug 26 15:18:11 GMT", "2026-08-31T15:18:11.000Z"),
    ("   Mon, 31 Aug 2026 15:18:11 GMT   ", "2026-08-31T15:18:11.000Z"),
    # An unrecognised zone name degrades to UTC rather than to None: losing the
    # publication time entirely would be the worse outcome.
    ("Mon, 31 Aug 2026 15:18:11 XYZ", "2026-08-31T15:18:11.000Z"),
    # Atom / RFC 3339, reachable because Atom entries are parsed at all.
    ("2026-08-31T15:18:11+00:00", "2026-08-31T15:18:11.000Z"),
    ("2026-08-31T15:18:11Z", "2026-08-31T15:18:11.000Z"),
    ("2026-08-31T23:18:11+08:00", "2026-08-31T15:18:11.000Z"),
    # Sub-second precision truncates to milliseconds, which is the schema's format.
    ("2026-08-31T15:18:11.123456Z", "2026-08-31T15:18:11.123Z"),
    ("2026-08-31", "2026-08-31T00:00:00.000Z"),
]


@pytest.mark.parametrize(("value", "expected"), PARSEABLE_DATES)
def test_parseable_dates_convert_to_the_canonical_format(value: str, expected: str) -> None:
    result = rss_datetime_to_iso(value)

    assert result == expected
    # Belt and braces: the result must satisfy the schema's own format check, since
    # D1 compares these as TEXT.
    assert is_iso_utc(result)


UNPARSEABLE_DATES = [
    None,
    "",
    "   ",
    "not a date",
    "Mon, 99 Xxx 2026 99:99:99 GMT",
    "2026-13-45T99:99:99Z",
    "1234567890",
    "0",
    "<script>alert(1)</script>",
    "Mon, 31 Aug 99999 15:18:11 GMT",
    # Year 9999 shifted west of UTC leaves the representable datetime range.
    "Fri, 31 Dec 9999 23:59:59 -0800",
]


@pytest.mark.parametrize("value", UNPARSEABLE_DATES)
def test_unparseable_dates_return_none_instead_of_raising(value: str | None) -> None:
    """A feed item with a broken date is ordinary input. The topic still has to be
    processed, and only the caller knows whether a missing time matters."""
    assert rss_datetime_to_iso(value) is None


def test_item_pub_date_round_trips_through_the_converter() -> None:
    """The two halves used together, which is how the readers will use them."""
    items = items_of(CHANNEL_FEED)

    assert [rss_datetime_to_iso(item.pub_date) for item in items] == [
        "2026-08-31T15:18:11.000Z",
        "2026-08-31T16:00:00.000Z",
    ]


# ----------------------------------------------------------------------
# item_text_fields
# ----------------------------------------------------------------------


def test_item_text_fields_covers_every_populated_field() -> None:
    item = RssItem(
        title="the title",
        link="https://t.me/linux_do_channel/1",
        description="https://linux.do/t/topic/1/2",
        guid="the guid",
        pub_date="Mon, 31 Aug 2026 15:18:11 GMT",
        extras=(("creator", "someone"), ("encoded", "https://github.com/owner/repo")),
    )

    fields = item_text_fields(item)

    assert fields == [
        "the title",
        "https://t.me/linux_do_channel/1",
        "https://linux.do/t/topic/1/2",
        "the guid",
        "someone",
        "https://github.com/owner/repo",
    ]


def test_item_text_fields_excludes_pub_date() -> None:
    """An RFC 822 date cannot contain a URL, so scanning it is pure cost."""
    item = RssItem(
        title=None,
        link=None,
        description=None,
        guid=None,
        pub_date="Mon, 31 Aug 2026 15:18:11 GMT",
        extras=(),
    )

    assert item_text_fields(item) == []


def test_item_text_fields_skips_empty_fields() -> None:
    item = items_of("<rss><channel><item><title>t</title></item></channel></rss>")[0]

    assert item_text_fields(item) == ["t"]


def test_item_text_fields_finds_the_topic_url_in_the_real_feed_shape() -> None:
    """The end-to-end reason this function exists: with the verified feed, the only
    field carrying the linux.do URL is `description`."""
    item = items_of(CHANNEL_FEED)[0]

    fields = item_text_fields(item)

    assert any("https://linux.do/t/topic/2837720/1" in field for field in fields)
    assert not any("linux.do/t/topic" in field for field in (item.title, item.link, item.guid))
