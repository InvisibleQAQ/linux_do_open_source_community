"""Public API response models.

**This file is one half of a contract.** The other half is
`frontend/src/api/types.ts`. Changing one without the other is a defect; see
`.trellis/spec/frontend/api-contract.md`.

Field names are snake_case and go on the wire as-is — there is no camelCase
conversion layer, so there is no second source of truth for names.

What must never appear in any model here, per the PRD:
  * internal processing fields (status, attempts, retry_after, last_error)
  * `uncertain` LLM decisions — only `include` is published
  * raw LLM output
  * anything derived from a secret
"""

from __future__ import annotations

from typing import Generic, TypeVar

from pydantic import BaseModel, Field

__all__ = [
    "CursorPage",
    "HealthStatus",
    "ProjectDetail",
    "ProjectSource",
    "ProjectSummary",
    "TopicDetail",
    "TopicPost",
    "TopicProject",
    "TopicSummary",
]

# Pagination bounds. The PRD requires public endpoints to validate them rather
# than trusting the caller.
DEFAULT_PAGE_SIZE = 20
MAX_PAGE_SIZE = 50

# Classic Generic rather than PEP 695 `class CursorPage[T]`: the runtime pins
# pydantic 2.10.6, and this form is unambiguously supported there.
T = TypeVar("T")


class ProjectSummary(BaseModel):
    canonical_url: str
    owner: str
    repo: str
    display_name: str
    summary: str


class TopicProject(ProjectSummary):
    """A project as it appears inside one topic, with its provenance."""

    # The URL exactly as written in the post. Proof, never an identity.
    evidence_url: str
    post_url: str
    post_number: int


class TopicSummary(BaseModel):
    topic_id: int
    canonical_url: str
    title: str
    author: str | None = None
    published_at: str
    projects: list[TopicProject] = Field(default_factory=list)


class TopicPost(BaseModel):
    post_number: int
    author: str | None = None
    published_at: str
    source_url: str
    # Plain text, converted from HTML by the topic reader. The frontend renders
    # it as text; raw RSS HTML must never reach a response.
    text: str
    is_first_post: bool


class TopicDetail(TopicSummary):
    posts: list[TopicPost] = Field(default_factory=list)


class ProjectSource(BaseModel):
    topic_id: int
    topic_title: str
    topic_url: str
    topic_published_at: str
    evidence_url: str
    post_url: str


class ProjectDetail(ProjectSummary):
    sources: list[ProjectSource] = Field(default_factory=list)


class CursorPage(BaseModel, Generic[T]):
    """Cursor-paginated envelope.

    `next_cursor` is null on the last page and that is the ONLY end-of-list
    signal — a full page may still be the last one. The frontend depends on this;
    see `frontend/src/api/hooks.ts`.
    """

    items: list[T] = Field(default_factory=list)
    next_cursor: str | None = None


class HealthStatus(BaseModel):
    """Deployment health only. Never a secret, never an internal error payload."""

    status: str
    version: str
