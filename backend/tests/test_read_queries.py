"""The read API's SQL and cursor codec, verified against stdlib sqlite3.

D1 is SQLite, so keyset pagination, newest-first ordering, the published-only
filter and the project join are all provable here — no Worker, no wrangler, no
network. What is NOT covered: the JsProxy conversion in `persistence/d1.py`, which
needs the real runtime.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from linuxdo_oss.persistence.read_queries import (
    PROJECT_BY_OWNER_REPO_SQL,
    PROJECT_SOURCES_SQL,
    TOPIC_BY_ID_SQL,
    TOPIC_POSTS_SQL,
    TOPIC_PROJECTS_SQL,
    TOPICS_PAGE_FIRST_SQL,
    TOPICS_PAGE_NEXT_SQL,
    Cursor,
    decode_cursor,
    encode_cursor,
)

MIGRATIONS_DIR = Path(__file__).resolve().parents[2] / "migrations"


@pytest.fixture()
def db() -> sqlite3.Connection:
    connection = sqlite3.connect(":memory:")
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys = ON")

    for migration in sorted(MIGRATIONS_DIR.glob("*.sql")):
        connection.executescript(migration.read_text(encoding="utf-8"))

    yield connection
    connection.close()


def add_topic(
    db: sqlite3.Connection,
    topic_id: int,
    published_at: str,
    *,
    status: str = "published",
    title: str | None = None,
) -> None:
    db.execute(
        "INSERT INTO topics (topic_id, canonical_url, title, author, published_at, status,"
        " discovered_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (
            topic_id,
            f"https://linux.do/t/topic/{topic_id}",
            title or f"主题 {topic_id}",
            "someone",
            published_at,
            status,
            published_at,
            published_at,
        ),
    )


def add_post(db: sqlite3.Connection, topic_id: int, post_number: int) -> int:
    return db.execute(
        "INSERT INTO topic_posts (topic_id, post_number, guid, source_url, cleaned_text,"
        " is_first_post, created_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
        (
            topic_id,
            post_number,
            f"guid-{topic_id}-{post_number}",
            f"https://linux.do/t/topic/{topic_id}/{post_number}",
            f"帖子 {post_number} 的正文",
            int(post_number == 1),
            "2026-08-31T00:00:00.000Z",
        ),
    ).lastrowid


def add_project(db: sqlite3.Connection, owner: str, repo: str) -> int:
    return db.execute(
        "INSERT INTO projects (canonical_url, owner, repo, display_name, summary, created_at,"
        " updated_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
        (
            f"https://github.com/{owner}/{repo}",
            owner,
            repo,
            repo.title(),
            f"{repo} 的中文摘要",
            "2026-08-31T00:00:00.000Z",
            "2026-08-31T00:00:00.000Z",
        ),
    ).lastrowid


def add_mention(db: sqlite3.Connection, topic_id: int, post_id: int, project_id: int) -> None:
    db.execute(
        "INSERT INTO project_mentions (topic_id, post_id, project_id, evidence_url,"
        " evidence_excerpt, confidence, prompt_version, detected_at)"
        " VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (
            topic_id,
            post_id,
            project_id,
            f"https://github.com/x/y/issues/{project_id}",
            "节选",
            0.9,
            "1",
            "2026-08-31T00:00:00.000Z",
        ),
    )


def topics_page(db: sqlite3.Connection, cursor: Cursor | None, page_size: int) -> list[sqlite3.Row]:
    """Mirrors the repository's paging call: fetch page_size + 1 to detect a next page."""
    limit = page_size + 1

    if cursor is None:
        return db.execute(TOPICS_PAGE_FIRST_SQL, (limit,)).fetchall()

    return db.execute(
        TOPICS_PAGE_NEXT_SQL,
        (cursor.published_at, cursor.published_at, cursor.topic_id, limit),
    ).fetchall()


# ----------------------------------------------------------------------
# Cursor codec
# ----------------------------------------------------------------------


def test_cursor_round_trips() -> None:
    original = Cursor(published_at="2026-08-31T15:18:11.000Z", topic_id=2837720)

    assert decode_cursor(encode_cursor(original)) == original


def test_cursor_token_is_opaque() -> None:
    """A client must not be able to hand-build one and probe unpublished rows."""
    token = encode_cursor(Cursor(published_at="2026-08-31T15:18:11.000Z", topic_id=1))

    assert "2026" not in token
    assert "|" not in token


@pytest.mark.parametrize(
    "token",
    ["", "not-base64!!", "Zm9v", "fHwx", "MjAyNnwxMjM0NTY3ODkwfG5vdC1hbi1pbnQ="],
)
def test_malformed_cursors_degrade_to_page_one(token: str) -> None:
    """A stale or edited cursor must fall back to the first page, not 500."""
    assert decode_cursor(token) is None


# ----------------------------------------------------------------------
# Feed: ordering, published-only, keyset pagination
# ----------------------------------------------------------------------


def test_feed_is_newest_first(db: sqlite3.Connection) -> None:
    add_topic(db, 1, "2026-08-29T00:00:00.000Z")
    add_topic(db, 2, "2026-08-31T00:00:00.000Z")
    add_topic(db, 3, "2026-08-30T00:00:00.000Z")

    rows = topics_page(db, None, 10)

    assert [row["topic_id"] for row in rows] == [2, 3, 1]


def test_feed_hides_every_non_published_status(db: sqlite3.Connection) -> None:
    add_topic(db, 1, "2026-08-31T00:00:00.000Z", status="published")
    for index, status in enumerate(
        ["discovered", "fetching", "ready", "classifying", "not_relevant", "failed"], start=2
    ):
        add_topic(db, index, "2026-08-31T00:00:00.000Z", status=status)

    rows = topics_page(db, None, 50)

    assert [row["topic_id"] for row in rows] == [1]


def test_keyset_pagination_walks_every_topic_exactly_once(db: sqlite3.Connection) -> None:
    for topic_id in range(1, 12):
        add_topic(db, topic_id, f"2026-08-{topic_id:02d}T00:00:00.000Z")

    page_size = 4
    seen: list[int] = []
    cursor: Cursor | None = None

    for _ in range(10):  # bounded so a broken cursor cannot loop forever
        rows = topics_page(db, cursor, page_size)
        has_more = len(rows) > page_size
        rows = rows[:page_size]

        seen.extend(row["topic_id"] for row in rows)

        if not has_more:
            cursor = None
            break

        last = rows[-1]
        cursor = Cursor(published_at=last["published_at"], topic_id=last["topic_id"])

    assert cursor is None, "pagination did not terminate"
    assert seen == sorted(range(1, 12), reverse=True)
    assert len(seen) == len(set(seen)), "a topic appeared on two pages"


def test_topics_sharing_a_timestamp_are_not_skipped(db: sqlite3.Connection) -> None:
    """The tie-break on topic_id is what makes this work. With `published_at < ?`
    alone, every topic sharing the boundary timestamp would be dropped."""
    same_time = "2026-08-31T00:00:00.000Z"
    for topic_id in (10, 11, 12, 13):
        add_topic(db, topic_id, same_time)

    page_size = 2
    seen: list[int] = []
    cursor: Cursor | None = None

    for _ in range(5):
        rows = topics_page(db, cursor, page_size)
        has_more = len(rows) > page_size
        rows = rows[:page_size]
        seen.extend(row["topic_id"] for row in rows)
        if not has_more:
            break
        last = rows[-1]
        cursor = Cursor(published_at=last["published_at"], topic_id=last["topic_id"])

    assert seen == [13, 12, 11, 10]


def test_a_full_page_can_still_be_the_last_page(db: sqlite3.Connection) -> None:
    """Exactly page_size rows: the fetch of page_size + 1 returns page_size, so
    there is no next page. This is why the API must not infer `has_more` from
    `len(items) == limit`."""
    for topic_id in range(1, 5):
        add_topic(db, topic_id, f"2026-08-{topic_id:02d}T00:00:00.000Z")

    rows = topics_page(db, None, 4)

    assert len(rows) == 4, "no extra row means no next page"


# ----------------------------------------------------------------------
# Projects within topics
# ----------------------------------------------------------------------


def test_topic_projects_returns_every_repository_in_the_topic(db: sqlite3.Connection) -> None:
    add_topic(db, 1, "2026-08-31T00:00:00.000Z")
    first_post = add_post(db, 1, 1)
    reply = add_post(db, 1, 4)

    alpha = add_project(db, "acme", "alpha")
    beta = add_project(db, "acme", "beta")

    add_mention(db, 1, first_post, alpha)
    add_mention(db, 1, reply, beta)

    sql = TOPIC_PROJECTS_SQL.format(placeholders="?")
    rows = db.execute(sql, (1,)).fetchall()

    assert [row["repo"] for row in rows] == ["alpha", "beta"]
    assert [row["post_number"] for row in rows] == [1, 4]
    assert rows[0]["post_url"].endswith("/1")
    assert rows[1]["post_url"].endswith("/4")


def test_topic_projects_batches_many_topics_in_one_query(db: sqlite3.Connection) -> None:
    """One query per page, not one per topic — otherwise D1 round trips scale with
    page size and count against the per-invocation query ceiling."""
    for topic_id in (1, 2, 3):
        add_topic(db, topic_id, f"2026-08-{topic_id:02d}T00:00:00.000Z")
        post_id = add_post(db, topic_id, 1)
        project_id = add_project(db, "acme", f"repo{topic_id}")
        add_mention(db, topic_id, post_id, project_id)

    sql = TOPIC_PROJECTS_SQL.format(placeholders="?, ?, ?")
    rows = db.execute(sql, (1, 2, 3)).fetchall()

    assert {row["topic_id"] for row in rows} == {1, 2, 3}
    assert len(rows) == 3


def test_topic_detail_and_posts(db: sqlite3.Connection) -> None:
    add_topic(db, 1, "2026-08-31T00:00:00.000Z", title="标题")
    add_post(db, 1, 1)
    add_post(db, 1, 7)

    topic = db.execute(TOPIC_BY_ID_SQL, (1,)).fetchone()
    posts = db.execute(TOPIC_POSTS_SQL, (1,)).fetchall()

    assert topic["title"] == "标题"
    assert [row["post_number"] for row in posts] == [1, 7]
    assert posts[0]["is_first_post"] == 1
    assert posts[1]["is_first_post"] == 0


def test_topic_detail_hides_unpublished_topics(db: sqlite3.Connection) -> None:
    add_topic(db, 1, "2026-08-31T00:00:00.000Z", status="classifying")

    assert db.execute(TOPIC_BY_ID_SQL, (1,)).fetchone() is None


# ----------------------------------------------------------------------
# Project detail: one project, many topics
# ----------------------------------------------------------------------


def test_project_sources_lists_every_topic_that_mentioned_it(db: sqlite3.Connection) -> None:
    project_id = add_project(db, "acme", "tool")

    for topic_id, day in ((1, "29"), (2, "31"), (3, "30")):
        add_topic(db, topic_id, f"2026-08-{day}T00:00:00.000Z")
        post_id = add_post(db, topic_id, 1)
        add_mention(db, topic_id, post_id, project_id)

    project = db.execute(PROJECT_BY_OWNER_REPO_SQL, ("acme", "tool")).fetchone()
    sources = db.execute(PROJECT_SOURCES_SQL, (project["project_id"],)).fetchall()

    # One project row, three provenance entries, newest topic first.
    assert db.execute("SELECT count(*) FROM projects").fetchone()[0] == 1
    assert [row["topic_id"] for row in sources] == [2, 3, 1]


def test_project_sources_excludes_unpublished_topics(db: sqlite3.Connection) -> None:
    """An unpublished topic must not leak through the project detail page."""
    project_id = add_project(db, "acme", "tool")

    add_topic(db, 1, "2026-08-31T00:00:00.000Z", status="published")
    add_topic(db, 2, "2026-08-31T00:00:00.000Z", status="classifying")

    for topic_id in (1, 2):
        post_id = add_post(db, topic_id, 1)
        add_mention(db, topic_id, post_id, project_id)

    project = db.execute(PROJECT_BY_OWNER_REPO_SQL, ("acme", "tool")).fetchone()
    sources = db.execute(PROJECT_SOURCES_SQL, (project["project_id"],)).fetchall()

    assert [row["topic_id"] for row in sources] == [1]


def test_project_lookup_is_by_lowercase_owner_repo(db: sqlite3.Connection) -> None:
    """The canonicalizer lowercases, so stored values are lowercase and the route's
    path parameters must be lowercased before lookup."""
    add_project(db, "acme", "tool")

    assert db.execute(PROJECT_BY_OWNER_REPO_SQL, ("acme", "tool")).fetchone() is not None
    assert db.execute(PROJECT_BY_OWNER_REPO_SQL, ("ACME", "Tool")).fetchone() is None
