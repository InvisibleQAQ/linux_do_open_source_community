"""Safe XML parsing — the only entry point for parsing a feed.

Three hazards, all named by the PRD's security section ("RSS/XML parsers reject
DTD/external entities and enforce response-size limits"):

  * **Entity expansion (billion laughs).** `xml.etree.ElementTree` really is
    vulnerable. Measured on CPython 3.13 / libexpat 2.7.3, a four-level nested
    internal entity expands to 30,000 characters and the parse succeeds. The
    stdlib offers no public switch to turn DTD processing off.
  * **External entities (XXE).** Measured safe on the same build: expat is given
    no external-entity handler, so `<!ENTITY x SYSTEM "file:///etc/passwd">`
    raises ParseError "undefined entity" instead of reading the file. Rejected
    here anyway — "currently safe because a handler happens not to be installed"
    is not a property to build a security control on.
  * **Response size.** The real channel feed is ~20 KB. Anything near the cap is
    broken or hostile, and buffering it costs isolate memory that the Worker does
    not have to spare.

Why not `defusedxml`, which solves the first two: it is absent from Cloudflare's
Python Workers documentation and from the Pyodide package index, and it has had no
release since 2021 — `[UNKNOWN]` whether it even loads on this runtime. Every
dependency is also deploy-time snapshot size against a 1 s startup limit. The
stdlib parsers are the ones this project is committed to; see the dependency rule
in .trellis/spec/backend/quality-guidelines.md.

Exactly one exception type leaves this module. A caller classifying a feed failure
(.trellis/spec/backend/error-handling.md: retryable / permanent / configuration)
files all of these under "permanent" and never needs to know whether the cause was
the byte cap, a doctype or an unclosed tag.
"""

from __future__ import annotations

from xml.etree.ElementTree import Element, ParseError, fromstring

__all__ = [
    "DOCTYPE_SCAN_BYTES",
    "MAX_FEED_BYTES",
    "XmlSafetyError",
    "parse_feed",
]

# 2 MiB, matching `Settings.max_response_bytes` so the transport cap and the parse
# cap agree. They are separate numbers on purpose: `parse_feed` is also reachable
# from tests and from a future non-HTTP source, and must not depend on someone
# else having already checked the size.
MAX_FEED_BYTES = 2 * 1024 * 1024

# A DTD is only legal in the prolog, so a real document declares one within the
# first few hundred bytes. 4 KiB is generous room for an XML declaration,
# stylesheet PIs and comments.
#
# Applied as a count of *characters* of the decoded string. Since UTF-8 uses at
# least one byte per character, a 4096-character window always covers at least
# 4096 source bytes — the scan is therefore never narrower than the name promises.
DOCTYPE_SCAN_BYTES = 4096

# Lowercased; the input window is lowercased once before matching. `<!DOCTYPE` and
# `<!ENTITY` are the only two markup declarations that can introduce an entity,
# and XML forbids whitespace between `<!` and the keyword, so a literal substring
# search is exact rather than approximate.
_FORBIDDEN_MARKERS = ("<!doctype", "<!entity")

# Stripped from the front before anything else. A BOM is legitimate; leading
# whitespace is common from proxies and rewriters. Both matter here — expat accepts
# a BOM but rejects whitespace in front of an XML declaration ("XML or text
# declaration not at start of entity"), so without this a padded but otherwise
# valid feed would look malformed.
_LEADING_NOISE = "\ufeff\t\n\r "


class XmlSafetyError(ValueError):
    """A document was refused: over the byte cap, declares a DTD/entity, or is
    malformed.

    One type for all three because every one of them is a permanent failure for
    the same caller. ValueError rather than a fresh hierarchy: the input was bad,
    and `except ValueError` around a parse already means the right thing.
    """


def parse_feed(text: str, *, max_bytes: int = MAX_FEED_BYTES) -> Element:
    """Parse `text` as XML and return its root element.

    Raises XmlSafetyError and nothing else. Both guards run *before* the parser
    sees the input, which is the point: rejecting a billion-laughs payload after
    expat has already expanded it is not a rejection.
    """
    # Cheap guard first. Every character is at least one UTF-8 byte, so a string
    # longer than the cap in characters is already over the cap in bytes and needs
    # no encoding — which keeps a hostile 50 MB body from costing a 50 MB copy on
    # its way to being refused.
    if len(text) > max_bytes:
        raise XmlSafetyError(f"feed exceeds the {max_bytes} byte cap")

    size = len(text.encode("utf-8"))
    if size > max_bytes:
        raise XmlSafetyError(f"feed is {size} bytes, over the {max_bytes} byte cap")

    # Strip before scanning, not after. Otherwise 4 KiB of spaces in front of
    # `<!DOCTYPE` pushes the declaration out of the window and defeats the check.
    document = text.lstrip(_LEADING_NOISE)

    marker = _find_forbidden_marker(document)
    if marker is not None:
        raise XmlSafetyError(f"input declares {marker} in the first {DOCTYPE_SCAN_BYTES} bytes")

    try:
        return fromstring(document)
    except ParseError as error:
        # Also the second line of defence: libexpat's own input-amplification guard
        # (default since 2.4.0) surfaces as ParseError, so the pathological cases a
        # prefix scan cannot see still land in this one exception type.
        raise XmlSafetyError(f"malformed XML: {error}") from error


def _find_forbidden_marker(document: str) -> str | None:
    """The first DTD/entity marker in the scan window, or None.

    A pre-parse substring scan rather than a parser option, because the stdlib has
    no parser option to use. `ElementTree.XMLParser` exposes no DTD switch; the
    only real hook is reaching into the private `parser.parser` pyexpat object and
    installing handlers (`EntityDeclHandler`, `ExternalEntityRefHandler`) the way
    defusedxml does. That is undocumented API on a build of libexpat we do not
    control — Pyodide compiles its own — and this Worker's failures only show up
    after deploy. A `str.find` over 4 KiB behaves identically on every build, costs
    microseconds, and refuses the whole class of input instead of individual
    handlers.

    The cost of that choice: a DOCTYPE pushed past the window by more than 4 KiB
    of leading comments is not seen here (verified — such a document parses and its
    entities expand). Accepted, because a feed generator does not emit 4 KiB of
    prolog comments, the amplification such a payload can reach is capped by
    libexpat's own guard, and this parser is only ever pointed at two fixed,
    configured hosts.
    """
    window = document[:DOCTYPE_SCAN_BYTES].lower()

    for marker in _FORBIDDEN_MARKERS:
        if marker in window:
            return marker

    return None
