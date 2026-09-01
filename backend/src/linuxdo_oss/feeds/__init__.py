"""RSS/XML reading layer.

Four modules, split along the one line that matters — whether a file can be
imported by plain CPython pytest:

  xml_safe.py   pure. Byte cap + DTD/entity rejection + one exception type.
  rss.py        pure. RSS 2.0 / Atom item walking and RFC 822 date conversion.
  channel.py    reads the configured RSSHub feed, returns topic candidates.
  topic.py      reads one linux.do topic feed, returns the retained posts.

The two readers do network I/O, but neither imports `workers`: they take
`fetch_text` — the callable in `adapters/http.py` — as a parameter. That is what
lets the whole layer be exercised with a fake transport under CPython, and it is
why `adapters/` stays the single place that knows the runtime exists.
"""
