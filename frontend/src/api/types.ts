/**
 * Public read API contract.
 *
 * This file is the ONLY place the frontend encodes the backend's wire shape.
 * It must stay in lockstep with the backend Pydantic response models in
 * `backend/src/linuxdo_oss/api/schemas.py`. When one side changes, change both
 * and update `.trellis/spec/frontend/api-contract.md`.
 *
 * Field names are snake_case because they come straight off the wire — we do not
 * camelCase-convert at the boundary. One naming convention, no mapping layer.
 */

// ----------------------------------------------------------------------
// Pagination
// ----------------------------------------------------------------------

/**
 * Cursor-paginated envelope. `next_cursor` is null on the last page — that is
 * the ONLY signal for "no more data". Never infer it from `items.length < limit`.
 */
export type CursorPage<T> = {
  items: T[];
  next_cursor: string | null;
};

// ----------------------------------------------------------------------
// Project
// ----------------------------------------------------------------------

/** A globally deduplicated GitHub repository. Identity is `canonical_url`. */
export type ProjectSummary = {
  /** Canonical `https://github.com/<owner>/<repo>` — the global dedup key. */
  canonical_url: string;
  owner: string;
  repo: string;
  /** LLM-generated display name. */
  display_name: string;
  /** LLM-generated Chinese summary. */
  summary: string;
};

/**
 * A project as it appears inside one topic, carrying the provenance that proves
 * where it came from. `evidence_url` is the ORIGINAL url found in the post (may
 * be a subpath such as `/issues/12`); `canonical_url` is the normalized identity.
 */
export type TopicProject = ProjectSummary & {
  evidence_url: string;
  /** Permalink to the linux.do post the repository was found in. */
  post_url: string;
  post_number: number;
};

// ----------------------------------------------------------------------
// Topic
// ----------------------------------------------------------------------

/** One linux.do discussion. The primary visual grouping in the UI. */
export type TopicSummary = {
  topic_id: number;
  /** Canonical `https://linux.do/t/topic/<id>` — no floor suffix. */
  canonical_url: string;
  title: string;
  author: string | null;
  /** ISO 8601 UTC. */
  published_at: string;
  /** Published projects only. `include` decisions that passed validation. */
  projects: TopicProject[];
};

/** One retained post inside a topic. */
export type TopicPost = {
  post_number: number;
  author: string | null;
  published_at: string;
  source_url: string;
  /** Backend-converted plain text. Never HTML — do not render with dangerouslySetInnerHTML. */
  text: string;
  is_first_post: boolean;
};

export type TopicDetail = TopicSummary & {
  posts: TopicPost[];
};

// ----------------------------------------------------------------------
// Project detail
// ----------------------------------------------------------------------

/** Where a project was mentioned, for the project-detail provenance list. */
export type ProjectSource = {
  topic_id: number;
  topic_title: string;
  topic_url: string;
  topic_published_at: string;
  evidence_url: string;
  post_url: string;
};

export type ProjectDetail = ProjectSummary & {
  sources: ProjectSource[];
};

// ----------------------------------------------------------------------
// Health
// ----------------------------------------------------------------------

/** Deployment health only. Must never carry secrets or internal error payloads. */
export type HealthStatus = {
  status: 'ok' | 'degraded';
  version: string;
};
