"""RSS/XML reading layer.

Four modules, split along the one line that matters — whether a file can be
imported by plain CPython pytest:

  xml_safe.py   pure. Byte cap + DTD/entity rejection + one exception type.
  rss.py        pure. RSS 2.0 / Atom item walking and RFC 822 date conversion.
  html_text.py  pure. Cooked HTML to plain text, keeping absolute anchor hrefs.
  tag_feed.py   reads the one configured Discourse tag feed, returns whole topics.

`tag_feed.py` is the only reader, and the only module here that does network I/O
— it still does not import `workers`, taking `fetch_text` (the callable in
`adapters/http.py`) as a parameter. That is what lets the whole layer be exercised
with a fake transport under CPython, and it is why `adapters/` stays the single
place that knows the runtime exists.

There used to be two readers, `channel.py` and `topic.py`, because discovering a
topic and fetching its text were separate requests. A Discourse list feed carries
the first post's full text inline, so both collapsed into `tag_feed.py`. See
`docs/adr/0007-single-source-tag-feed.md`.
"""
