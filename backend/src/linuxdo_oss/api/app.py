"""Public read API.

Read-only, unauthenticated, no write endpoints. Every response is a Pydantic
model from `schemas.py`, which is one half of the contract with
`frontend/src/api/types.ts`.

No CORS middleware, deliberately: one Worker serves both the SPA and `/api/*`
from a single origin, so there is no cross-origin request to permit. See
`docs/adr/0002-single-worker-topology.md`. Do not add CORS "just in case" — an
allow-list nobody needs is an allow-list nobody maintains.
"""

from __future__ import annotations

from typing import Annotated, Any

from fastapi import Depends, FastAPI, HTTPException, Path, Query

from linuxdo_oss.api.deps import get_db
from linuxdo_oss.api.schemas import (
    DEFAULT_PAGE_SIZE,
    MAX_PAGE_SIZE,
    CursorPage,
    HealthStatus,
    ProjectDetail,
    TopicDetail,
    TopicSummary,
)
from linuxdo_oss.persistence import read_repository

APP_VERSION = "0.1.0"

app = FastAPI(
    title="Linux.do 开源项目聚合器",
    version=APP_VERSION,
    # The SPA owns the root; docs stay under /api so the asset router's
    # run_worker_first("/api/*") rule actually reaches them.
    docs_url="/api/docs",
    openapi_url="/api/openapi.json",
    redoc_url=None,
)

DbDep = Annotated[Any, Depends(get_db)]


@app.get("/api/health", response_model=HealthStatus)
async def health() -> HealthStatus:
    """Deployment health only.

    Deliberately does NOT touch D1 or report configuration: the PRD forbids
    exposing secrets or internal error payloads here, and a health check that
    queries the database turns a database blip into a deploy-looks-broken signal.
    """
    return HealthStatus(status="ok", version=APP_VERSION)


@app.get("/api/topics", response_model=CursorPage[TopicSummary])
async def list_topics(
    db: DbDep,
    cursor: Annotated[str | None, Query(max_length=256)] = None,
    limit: Annotated[int, Query(ge=1, le=MAX_PAGE_SIZE)] = DEFAULT_PAGE_SIZE,
) -> CursorPage[TopicSummary]:
    """Newest-first topics, each with its published project collection.

    Pagination bounds are validated by FastAPI rather than trusted — the PRD
    requires it, and an unbounded `limit` is a trivial way to blow past D1's
    row-read budget.
    """
    return await read_repository.fetch_topics_page(db, cursor_token=cursor, page_size=limit)


@app.get("/api/topics/{topic_id}", response_model=TopicDetail)
async def get_topic(
    db: DbDep,
    topic_id: Annotated[int, Path(ge=1)],
) -> TopicDetail:
    topic = await read_repository.fetch_topic(db, topic_id)

    if topic is None:
        # 404 for both "never seen" and "not published". The distinction is
        # internal state and must not be observable.
        raise HTTPException(status_code=404, detail="topic not found")

    return topic


@app.get("/api/projects/{owner}/{repo}", response_model=ProjectDetail)
async def get_project(
    db: DbDep,
    owner: Annotated[str, Path(min_length=1, max_length=39, pattern=r"^[A-Za-z0-9-]+$")],
    repo: Annotated[str, Path(min_length=1, max_length=100, pattern=r"^[A-Za-z0-9._-]+$")],
) -> ProjectDetail:
    """One globally deduplicated project and its Linux.do source topics.

    The path patterns mirror GitHub's own naming rules, so a malformed request is
    rejected at the edge of the app instead of reaching a query.
    """
    project = await read_repository.fetch_project(db, owner, repo)

    if project is None:
        raise HTTPException(status_code=404, detail="project not found")

    return project
