"""HTML to plain text, plus link recovery. Pure: stdlib only.

Everything a Linux.do post says arrives as HTML inside an RSS `<description>`, and
two rules from the PRD collide there:

  * "RSS HTML is converted to text on the backend and never injected as raw HTML" —
    so `topic_posts.cleaned_text` must be text, and the frontend renders it as text.
  * "A reply is retained only if its original content contains at least one explicit
    GitHub URL", and the classifier may only choose from repositories found in the
    post's own text — so a URL that the conversion drops is a project that can never
    be published.

The second rule is what makes this module more than a tag stripper. In Discourse
markup a repository is very often *only* in an attribute:

    <a href="https://github.com/owner/repo">仓库地址</a>

A naive "keep the text, drop the tags" pass turns that into `仓库地址` and the
project is gone with no error anywhere. So an absolute `http(s)` href is appended to
the text whenever the anchor's own text does not already contain it. The condition
matters in both directions: appending unconditionally would double every bare link
(Discourse renders those as `<a href="U">U</a>`), and appending nothing would lose
the linked ones.

Only absolute `http`/`https` hrefs are appended, because that is exactly the set
`domain/github_url.py` can accept — a relative `/t/topic/1` or a
`javascript:void(0)` can never become a repository candidate, so putting it in the
stored text would be noise with no upside.

`extract_links` exists for a narrower reason, and it is not redundant with the
above: `html.parser` resolves character references *in attribute values*, so
`href="https://github.com&#x2F;owner&#x2F;repo"` becomes a usable URL here, while
the same bytes in the raw feed defeat any regex reading the markup directly. The
topic reader feeds both the raw HTML and these links to the retention predicate for
that reason.

Why `html.parser` and not beautifulsoup4/lxml: they are extra deploy-time snapshot
weight against a 1 s Worker startup limit, and lxml is a native extension whose
availability on this runtime is unverified. See the dependency rule in
.trellis/spec/backend/quality-guidelines.md. `html.parser` is also non-raising on
malformed input (strict mode was removed in 3.5), which is the right shape for
hostile input: a broken tag degrades the text, it does not fail the topic.
"""

from __future__ import annotations

import re
from html.parser import HTMLParser

__all__ = ["extract_links", "html_to_text"]

# ----------------------------------------------------------------------
# Tag vocabulary
# ----------------------------------------------------------------------

# Content of these elements is not text at all — it is code. Their character data
# must be dropped entirely rather than merely unwrapped, or a script body and CSS
# selectors end up in `cleaned_text` and then in an LLM prompt.
_SKIPPED_TAGS = frozenset({"script", "style"})

# A newline before AND after: consecutive siblings therefore end up separated by a
# blank line, which is what preserves paragraph structure through the whitespace
# collapse below.
_PARAGRAPH_TAGS = frozenset(
    {
        "address",
        "article",
        "aside",  # Discourse wraps quotes and oneboxes in <aside>
        "blockquote",
        "details",
        "div",
        "dl",
        "figcaption",
        "figure",
        "footer",
        "form",
        "h1",
        "h2",
        "h3",
        "h4",
        "h5",
        "h6",
        "header",
        "hr",
        "main",
        "nav",
        "ol",
        "p",
        "pre",
        "section",
        "summary",
        "table",
        "ul",
    }
)

# A newline BEFORE only. A list of items or a table row should be one line each; the
# blank line that `_PARAGRAPH_TAGS` produces would space a five-item list out to
# nine lines.
_LINE_TAGS = frozenset({"br", "dd", "dt", "li", "td", "th", "tr"})

# Tags whose self-closing spelling must be balanced by an explicit end-tag call.
# `<script/>` is invalid HTML, but hostile input can contain it, and without this the
# skip counter would never come back down and every remaining word in the post would
# be discarded. `<a/>` is here so a self-closed anchor still flushes its href.
_CLOSING_STARTEND_TAGS = _SKIPPED_TAGS | {"a"}

# ----------------------------------------------------------------------
# Whitespace
# ----------------------------------------------------------------------

# Horizontal whitespace only — `\n` is deliberately excluded from the class so the
# line structure built above (and any newline the source markup already had) is not
# flattened away. `\xa0` from `&nbsp;` is whitespace to `\s` and collapses too.
_HORIZONTAL_WS_RE = re.compile(r"[^\S\n]+")
_AROUND_NEWLINE_RE = re.compile(r" *\n *")
_BLANK_LINES_RE = re.compile(r"\n{3,}")


def _collapse(raw: str) -> str:
    """Runs of whitespace to one space, at most one blank line, trimmed.

    Capping blank lines at one rather than removing them is the whole point: the
    paragraph break is information (it separates a description from a code block),
    while a run of eight newlines from nested block elements is not.
    """
    text = _HORIZONTAL_WS_RE.sub(" ", raw)
    text = _AROUND_NEWLINE_RE.sub("\n", text)
    text = _BLANK_LINES_RE.sub("\n\n", text)

    return text.strip()


def _is_absolute_http(url: str) -> bool:
    """Whether `url` is the kind of URL a repository candidate could be built from.

    Matches `domain/github_url.py::canonicalize`, which accepts nothing else. Keeping
    the two in agreement is why the href filter is a scheme test and not a
    "looks like a link" heuristic.
    """
    return url.lower().startswith(("http://", "https://"))


# ----------------------------------------------------------------------
# Parser
# ----------------------------------------------------------------------


class _TextExtractor(HTMLParser):
    """Walks HTML once, building plain text and the ordered anchor href list.

    One parser for both outputs so the two can never disagree about which anchors
    exist — in particular, a link inside `<script>` is invisible to both.
    """

    def __init__(self) -> None:
        # convert_charrefs is the default and is wanted: `&amp;` in the body must
        # become `&` exactly once, here, rather than being left for a caller to
        # unescape a second time.
        super().__init__(convert_charrefs=True)
        self._parts: list[str] = []
        # A counter, not a flag: `<style>` inside `<script>` is legal-looking input
        # and a flag would be cleared by the first end tag.
        self._skip_depth = 0
        # (href, index into _parts where the anchor's text starts).
        self._anchors: list[tuple[str, int]] = []
        self._links: list[str] = []

    # -- output ---------------------------------------------------------

    def text(self) -> str:
        return _collapse("".join(self._parts))

    def links(self) -> list[str]:
        return list(self._links)

    def finish(self) -> None:
        """Flush anchors left open by unbalanced markup.

        `<a href="U">text` with no `</a>` is ordinary broken HTML; without this the
        href — and therefore possibly a project — would be silently dropped.
        """
        while self._anchors:
            self._close_anchor()

    # -- HTMLParser hooks -----------------------------------------------

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag in _SKIPPED_TAGS:
            self._skip_depth += 1
            return

        if self._skip_depth:
            return

        if tag == "a":
            href = _attribute(attrs, "href")
            if href:
                self._record_link(href)
            self._anchors.append((href, len(self._parts)))

        if tag in _PARAGRAPH_TAGS or tag in _LINE_TAGS:
            self._parts.append("\n")

    def handle_endtag(self, tag: str) -> None:
        if tag in _SKIPPED_TAGS:
            # Clamped: a stray `</script>` must not drive the counter negative and
            # then swallow a later real `<script>` block.
            self._skip_depth = max(0, self._skip_depth - 1)
            return

        if self._skip_depth:
            return

        if tag == "a" and self._anchors:
            self._close_anchor()

        if tag in _PARAGRAPH_TAGS:
            self._parts.append("\n")

    def handle_startendtag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        """`<br/>` is ONE line break.

        The inherited implementation calls start *and* end, which for `<p/>` would
        emit two newlines and read as a paragraph break the markup never contained.
        Only the tags in `_CLOSING_STARTEND_TAGS` need their end half.
        """
        self.handle_starttag(tag, attrs)

        if tag in _CLOSING_STARTEND_TAGS:
            self.handle_endtag(tag)

    def handle_data(self, data: str) -> None:
        if self._skip_depth:
            return

        # Appended verbatim, whitespace included: the whitespace is what keeps
        # `a <b>b</b>` from becoming `ab`. It is normalized once, at the end.
        self._parts.append(data)

    # -- private --------------------------------------------------------

    def _record_link(self, href: str) -> None:
        # Deduplicated, first occurrence wins, mirroring
        # `extract_repository_candidates`. A post quoting the same link ten times is
        # one link, and the list stays bounded on hostile input.
        if href not in self._links:
            self._links.append(href)

    def _close_anchor(self) -> None:
        href, start = self._anchors.pop()

        if not href or not _is_absolute_http(href):
            return

        shown = "".join(self._parts[start:])
        if href in shown:
            # Already a bare link — Discourse's own rendering of `https://x/y`. A
            # second copy would just be noise in the LLM prompt.
            return

        # Spaces on both sides so the URL cannot be glued to adjacent CJK text: the
        # extractor's regex refuses a URL preceded by a word character.
        self._parts.append(f" {href} ")


def _attribute(attrs: list[tuple[str, str | None]], name: str) -> str:
    """One attribute value, stripped; "" when absent or valueless.

    `html.parser` reports a bare attribute (`<a href>`) as None, so the two "no
    usable value" cases are collapsed here rather than at every call site.
    """
    for key, value in attrs:
        if key == name:
            return (value or "").strip()

    return ""


def _parse(html: str) -> _TextExtractor:
    parser = _TextExtractor()
    parser.feed(html)
    parser.close()
    parser.finish()

    return parser


# ----------------------------------------------------------------------
# Public API
# ----------------------------------------------------------------------


def html_to_text(html: str) -> str:
    """Plain text for `html`, with absolute anchor hrefs kept recoverable.

    Never raises: `html.parser` does not reject malformed input, and a post whose
    markup is broken still has to be stored and classified.
    """
    if not html:
        return ""

    return _parse(html).text()


def extract_links(html: str) -> list[str]:
    """Every distinct `<a href>` value, in document order, entity-decoded.

    Verbatim — relative and non-http hrefs included — because this module does not
    get to decide what a caller may accept; `domain/github_url.py` owns that and
    rejects everything that is not a repository. Values are only stripped of
    surrounding whitespace, and empty ones are dropped.

    Deliberately only `<a href>`: an `<img src>` on github.com is a badge or a raw
    asset, not a project the author linked, and admitting it would manufacture
    repository candidates the post never mentioned.

    Anchors inside `<script>`/`<style>` are not collected — the same skip that keeps
    code out of the text keeps injected markup out of the candidate set.
    """
    if not html:
        return []

    return _parse(html).links()
