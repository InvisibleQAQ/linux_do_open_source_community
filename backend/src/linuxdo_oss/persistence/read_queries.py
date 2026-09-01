"""SQL for the public read API, plus the cursor codec.

The statements live here as module constants rather than inline in the repository
so they can be executed against a plain stdlib sqlite3 database in tests. D1 is
SQLite, so pagination correctness, ordering and the join shape are all provable
without a Worker — see `backend/tests/test_read_queries.py`.

Only `status = 'published'` rows are ever visible. Internal processing state
never leaves this module.
"""

from __future__ import annotations

import base64
import binascii
from dataclasses import dataclass

from linuxdo_oss.domain.timestamps import is_iso_utc

__all__ = [
    "PROJECT_BY_OWNER_REPO_SQL",
    "PROJECT_SOURCES_SQL",
    "TOPICS_PAGE_FIRST_SQL",
    "TOPICS_PAGE_NEXT_SQL",
    "TOPIC_BY_ID_SQL",
    "TOPIC_POSTS_SQL",
    "TOPIC_PROJECTS_SQL",
    "Cursor",
    "decode_cursor",
    "encode_cursor",
]


# ----------------------------------------------------------------------
# Cursor
# ----------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Cursor:
    """Keyset position in the newest-first feed.

    Keyset, not OFFSET: the feed has new topics inserted at its head every five
    minutes, and OFFSET pagination would silently skip or repeat rows as the head
    shifts. It is also the only form that can use the `idx_topics_feed` index.
    """

    published_at: str
    topic_id: int


def encode_cursor(cursor: Cursor) -> str:
    """Opaque token. Opaque so its internals are not a public API — a client must
    not be able to construct one and probe unpublished rows."""
    raw = f"{cursor.published_at}|{cursor.topic_id}".encode()
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def decode_cursor(token: str) -> Cursor | None:
    """Parse a cursor token. Returns None for anything malformed.

    None means "start from the beginning" rather than an error: a stale or
    hand-edited cursor should degrade to page one, not 500.
    """
    if not token:
        return None

    padded = token + "=" * (-len(token) % 4)

    try:
        raw = base64.urlsafe_b64decode(padded.encode("ascii")).decode("utf-8")
    except (binascii.Error, UnicodeDecodeError, ValueError):
        return None

    published_at, separator, topic_id = raw.rpartition("|")
    if not separator:
        return None

    # The timestamp half is validated against the one canonical format, not merely
    # checked for non-emptiness. Without this, a token decoding to "||1" would
    # produce a Cursor whose published_at is "|" — accepted, then compared as TEXT
    # against real timestamps, with silently wrong results.
    if not is_iso_utc(published_at):
        return None

    try:
        parsed_topic_id = int(topic_id)
    except ValueError:
        return None

    if parsed_topic_id < 0:
        return None

    return Cursor(published_at=published_at, topic_id=parsed_topic_id)


# ----------------------------------------------------------------------
# Topic feed
# ----------------------------------------------------------------------

# `limit` is bound as `page_size + 1`: one extra row tells us whether another page
# exists without a second COUNT query.
TOPICS_PAGE_FIRST_SQL = """
SELECT topic_id, canonical_url, title, author, published_at
  FROM topics
 WHERE status = 'published'
 ORDER BY published_at DESC, topic_id DESC
 LIMIT ?
"""

# The comparison is expanded rather than written as a row-value
# `(published_at, topic_id) < (?, ?)` so it works on every SQLite version D1 might
# be running.
TOPICS_PAGE_NEXT_SQL = """
SELECT topic_id, canonical_url, title, author, published_at
  FROM topics
 WHERE status = 'published'
   AND (published_at < ? OR (published_at = ? AND topic_id < ?))
 ORDER BY published_at DESC, topic_id DESC
 LIMIT ?
"""

TOPIC_BY_ID_SQL = """
SELECT topic_id, canonical_url, title, author, published_at
  FROM topics
 WHERE topic_id = ? AND status = 'published'
"""

# ----------------------------------------------------------------------
# Projects within topics
# ----------------------------------------------------------------------

# One query for every topic on the page. The alternative — one query per topic —
# would multiply D1 round trips by the page size and count against the
# per-invocation query ceiling. The `?` placeholder list is built by the caller
# and must respect MAX_BOUND_PARAMS.
TOPIC_PROJECTS_SQL = """
SELECT m.topic_id,
       p.canonical_url,
       p.owner,
       p.repo,
       p.display_name,
       p.summary,
       m.evidence_url,
       tp.post_number,
       tp.source_url AS post_url
  FROM project_mentions AS m
  JOIN projects        AS p  ON p.project_id = m.project_id
  JOIN topic_posts     AS tp ON tp.post_id   = m.post_id
 WHERE m.topic_id IN ({placeholders})
 ORDER BY m.topic_id DESC, tp.post_number ASC, p.canonical_url ASC
"""

TOPIC_POSTS_SQL = """
SELECT post_number, author, published_at, source_url, cleaned_text, is_first_post
  FROM topic_posts
 WHERE topic_id = ?
 ORDER BY post_number ASC
"""

# ----------------------------------------------------------------------
# Project detail
# ----------------------------------------------------------------------

PROJECT_BY_OWNER_REPO_SQL = """
SELECT project_id, canonical_url, owner, repo, display_name, summary
  FROM projects
 WHERE owner = ? AND repo = ?
"""

# Only published topics are exposed as sources, so an unpublished topic cannot
# leak through the project detail page.
PROJECT_SOURCES_SQL = """
SELECT t.topic_id,
       t.title        AS topic_title,
       t.canonical_url AS topic_url,
       t.published_at AS topic_published_at,
       m.evidence_url,
       tp.source_url  AS post_url
  FROM project_mentions AS m
  JOIN topics       AS t  ON t.topic_id = m.topic_id
  JOIN topic_posts  AS tp ON tp.post_id = m.post_id
 WHERE m.project_id = ? AND t.status = 'published'
 ORDER BY t.published_at DESC, t.topic_id DESC
"""
