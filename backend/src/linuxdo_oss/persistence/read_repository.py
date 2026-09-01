"""Read repository for the public API.

The only place the read API touches D1. Callers get Pydantic models; SQL, cursors
and JsProxy conversion stay here.

`db` is the D1 binding (`env.DB`), passed in rather than looked up so the same
repository serves both the request path and the cron path.
"""

from __future__ import annotations

from typing import Any

from linuxdo_oss.api.schemas import (
    CursorPage,
    ProjectDetail,
    ProjectSource,
    TopicDetail,
    TopicPost,
    TopicProject,
    TopicSummary,
)
from linuxdo_oss.persistence import read_queries as q
from linuxdo_oss.persistence.d1 import MAX_BOUND_PARAMS, query_all, query_one

__all__ = ["fetch_project", "fetch_topic", "fetch_topics_page"]


def _topic_summary(row: dict[str, Any], projects: list[TopicProject]) -> TopicSummary:
    return TopicSummary(
        topic_id=int(row["topic_id"]),
        canonical_url=str(row["canonical_url"]),
        title=str(row["title"] or ""),
        author=row["author"],
        published_at=str(row["published_at"] or ""),
        projects=projects,
    )


def _topic_project(row: dict[str, Any]) -> TopicProject:
    return TopicProject(
        canonical_url=str(row["canonical_url"]),
        owner=str(row["owner"]),
        repo=str(row["repo"]),
        display_name=str(row["display_name"]),
        summary=str(row["summary"]),
        evidence_url=str(row["evidence_url"]),
        post_url=str(row["post_url"]),
        post_number=int(row["post_number"]),
    )


async def _projects_by_topic(db: Any, topic_ids: list[int]) -> dict[int, list[TopicProject]]:
    """All projects for a page of topics, in one query.

    One query per page rather than one per topic: D1 queries count against the
    per-invocation subrequest budget, so a page of 20 would otherwise cost 20
    round trips instead of 1.
    """
    if not topic_ids:
        return {}

    if len(topic_ids) > MAX_BOUND_PARAMS:
        raise ValueError(
            f"{len(topic_ids)} topic ids exceeds the {MAX_BOUND_PARAMS} bound-parameter limit"
        )

    sql = q.TOPIC_PROJECTS_SQL.format(placeholders=", ".join("?" * len(topic_ids)))
    rows = await query_all(db, sql, tuple(topic_ids))

    grouped: dict[int, list[TopicProject]] = {topic_id: [] for topic_id in topic_ids}
    for row in rows:
        grouped[int(row["topic_id"])].append(_topic_project(row))

    return grouped


async def fetch_topics_page(
    db: Any, *, cursor_token: str | None, page_size: int
) -> CursorPage[TopicSummary]:
    """One page of the newest-first feed.

    A malformed cursor degrades to page one rather than erroring — a stale token
    in a bookmarked URL should still show content.
    """
    cursor = q.decode_cursor(cursor_token) if cursor_token else None

    # Fetch one extra row: its presence is what tells us a next page exists.
    # Never infer that from `len(rows) == page_size` — a full page can be the last.
    limit = page_size + 1

    if cursor is None:
        rows = await query_all(db, q.TOPICS_PAGE_FIRST_SQL, (limit,))
    else:
        rows = await query_all(
            db,
            q.TOPICS_PAGE_NEXT_SQL,
            (cursor.published_at, cursor.published_at, cursor.topic_id, limit),
        )

    has_more = len(rows) > page_size
    rows = rows[:page_size]

    projects = await _projects_by_topic(db, [int(row["topic_id"]) for row in rows])

    items = [_topic_summary(row, projects.get(int(row["topic_id"]), [])) for row in rows]

    next_cursor = None
    if has_more and rows:
        last = rows[-1]
        next_cursor = q.encode_cursor(
            q.Cursor(published_at=str(last["published_at"]), topic_id=int(last["topic_id"]))
        )

    return CursorPage[TopicSummary](items=items, next_cursor=next_cursor)


async def fetch_topic(db: Any, topic_id: int) -> TopicDetail | None:
    """One published topic with its retained posts and published projects."""
    row = await query_one(db, q.TOPIC_BY_ID_SQL, (topic_id,))
    if row is None:
        return None

    projects = (await _projects_by_topic(db, [topic_id])).get(topic_id, [])
    post_rows = await query_all(db, q.TOPIC_POSTS_SQL, (topic_id,))

    posts = [
        TopicPost(
            post_number=int(post["post_number"]),
            author=post["author"],
            published_at=str(post["published_at"] or ""),
            source_url=str(post["source_url"]),
            text=str(post["cleaned_text"] or ""),
            is_first_post=bool(post["is_first_post"]),
        )
        for post in post_rows
    ]

    summary = _topic_summary(row, projects)

    return TopicDetail(**summary.model_dump(), posts=posts)


async def fetch_project(db: Any, owner: str, repo: str) -> ProjectDetail | None:
    """One globally deduplicated project and every published topic that mentioned it.

    `owner` and `repo` are lowercased because the canonicalizer stores them that
    way — `github.com/Owner/Repo` and `github.com/owner/repo` are one project.
    """
    row = await query_one(db, q.PROJECT_BY_OWNER_REPO_SQL, (owner.lower(), repo.lower()))
    if row is None:
        return None

    source_rows = await query_all(db, q.PROJECT_SOURCES_SQL, (int(row["project_id"]),))

    return ProjectDetail(
        canonical_url=str(row["canonical_url"]),
        owner=str(row["owner"]),
        repo=str(row["repo"]),
        display_name=str(row["display_name"]),
        summary=str(row["summary"]),
        sources=[
            ProjectSource(
                topic_id=int(source["topic_id"]),
                topic_title=str(source["topic_title"] or ""),
                topic_url=str(source["topic_url"]),
                topic_published_at=str(source["topic_published_at"] or ""),
                evidence_url=str(source["evidence_url"]),
                post_url=str(source["post_url"]),
            )
            for source in source_rows
        ],
    )
