-- Initial schema for the Linux.do GitHub 开源项目聚合器.
--
-- Target: Cloudflare D1 (SQLite semantics).
--
-- Conventions, all of them load-bearing:
--
--  1. Timestamps are TEXT holding ISO 8601 UTC with a trailing 'Z' and millisecond
--     precision, e.g. '2026-08-31T15:18:11.000Z'. That format sorts and compares
--     lexicographically, which is why `retry_after <= ?` and `ORDER BY
--     published_at DESC` work on TEXT. Every writer MUST use that exact format.
--  2. Booleans are INTEGER 0/1 (SQLite has no boolean type).
--  3. Column names are plain snake_case and deliberately avoid identifiers that
--     collide with JS Object / JsProxy members (`order`, `keys`, `get`, `length`,
--     `constructor`, `name`). Rows come back from D1 as JsProxy and are read via
--     attribute access before `.to_py()`; a colliding name forces quoting in every
--     statement and produces dirty dicts.
--  4. D1 has NO interactive transactions. Atomicity comes from `batch()` (one
--     rollback boundary) plus the UNIQUE constraints below and ON CONFLICT upserts.
--     Every unique constraint here is what makes a re-run idempotent — do not drop
--     one without replacing the guarantee.
--  5. D1 allows at most 100 bound parameters per statement. Multi-row inserts must
--     be chunked; see the MAX_BOUND_PARAMS constant in the persistence layer.

-- ----------------------------------------------------------------------
-- sync_runs — one Cron-triggered ingestion attempt.
-- ----------------------------------------------------------------------

CREATE TABLE sync_runs (
  run_id            INTEGER PRIMARY KEY AUTOINCREMENT,
  scheduled_at      TEXT    NOT NULL,
  started_at        TEXT    NOT NULL,
  finished_at       TEXT,
  -- 'running' | 'succeeded' | 'failed'
  status            TEXT    NOT NULL,
  discovered_count  INTEGER NOT NULL DEFAULT 0,
  processed_count   INTEGER NOT NULL DEFAULT 0,
  published_count   INTEGER NOT NULL DEFAULT 0,
  failed_count      INTEGER NOT NULL DEFAULT 0,
  -- Bounded, category-level summary. Never a full LLM payload, never a secret.
  error_summary     TEXT
);

CREATE INDEX idx_sync_runs_started_at ON sync_runs (started_at DESC);

-- ----------------------------------------------------------------------
-- topics — a Linux.do discussion. The numeric topic id IS the identity.
-- ----------------------------------------------------------------------

CREATE TABLE topics (
  -- Linux.do's own numeric topic id. Natural key: discovery dedup depends on it,
  -- so it is the primary key rather than a surrogate.
  topic_id          INTEGER PRIMARY KEY,
  -- https://linux.do/t/topic/<id> — floor suffix stripped.
  canonical_url     TEXT    NOT NULL,
  title             TEXT,
  author            TEXT,
  published_at      TEXT,

  -- 'ready' | 'classifying' | 'published' | 'not_relevant' | 'failed'
  --
  -- A topic is BORN 'ready': the tag feed delivers the row and the first post's
  -- text in one item, so there is no state in which a topic is known and its text
  -- is not. That is why the default is the claimable status rather than a
  -- pre-claim one — a row inserted without an explicit status still means
  -- "stored, awaiting judgement", which is the only thing it can mean.
  --
  -- The default is load-bearing in the other direction too: `DUE_TOPICS_SQL`
  -- selects on 'ready', so a default naming any other status would put every such
  -- row in a state nothing selects — no error, no classification, forever.
  status            TEXT    NOT NULL DEFAULT 'ready',
  attempts          INTEGER NOT NULL DEFAULT 0,
  -- Earliest time a 'failed' row may be claimed again. NULL means "not scheduled".
  retry_after       TEXT,
  -- Claim lease. A row is claimable when the lease is NULL or already expired,
  -- which is what makes overlapping Cron runs harmless without a transaction.
  lease_expires_at  TEXT,
  last_error        TEXT,

  discovered_at     TEXT    NOT NULL,
  updated_at        TEXT    NOT NULL
);

-- The public feed: newest first, published only.
CREATE INDEX idx_topics_feed ON topics (published_at DESC, topic_id DESC)
  WHERE status = 'published';

-- The claim query. Unindexed scans are billed as rows read, so this is not optional.
CREATE INDEX idx_topics_claim ON topics (status, retry_after, lease_expires_at);

-- ----------------------------------------------------------------------
-- topic_posts — one retained RSS item inside a topic.
-- ----------------------------------------------------------------------

CREATE TABLE topic_posts (
  post_id        INTEGER PRIMARY KEY AUTOINCREMENT,
  topic_id       INTEGER NOT NULL REFERENCES topics (topic_id) ON DELETE CASCADE,
  post_number    INTEGER NOT NULL,
  -- Stable RSS GUID when the feed provides one. Nullable because
  -- (topic_id, post_number) is the fallback identity.
  guid           TEXT,
  author         TEXT,
  published_at   TEXT,
  source_url     TEXT    NOT NULL,
  -- HTML already converted to plain text by the backend. The frontend renders it
  -- as text; raw RSS HTML must never reach the database or the browser.
  cleaned_text   TEXT    NOT NULL,
  is_first_post  INTEGER NOT NULL DEFAULT 0,
  created_at     TEXT    NOT NULL,

  -- Re-running the same topic must not duplicate posts.
  UNIQUE (topic_id, post_number)
);

-- Partial unique index: enforced only where a GUID exists, so NULL GUIDs (which
-- SQLite treats as distinct) cannot accumulate duplicates under a plain UNIQUE.
CREATE UNIQUE INDEX idx_topic_posts_guid ON topic_posts (guid) WHERE guid IS NOT NULL;

CREATE INDEX idx_topic_posts_topic ON topic_posts (topic_id, post_number);

-- ----------------------------------------------------------------------
-- projects — a globally deduplicated GitHub repository.
-- ----------------------------------------------------------------------

CREATE TABLE projects (
  project_id     INTEGER PRIMARY KEY AUTOINCREMENT,
  -- https://github.com/<owner>/<repo>, normalized. THE global dedup key: the same
  -- repository reached via /issues/12 or /tree/main collapses onto this row.
  canonical_url  TEXT    NOT NULL,
  owner          TEXT    NOT NULL,
  repo           TEXT    NOT NULL,
  display_name   TEXT    NOT NULL,
  -- Chinese summary from the LLM. Current value; history is not kept.
  summary        TEXT    NOT NULL,
  created_at     TEXT    NOT NULL,
  updated_at     TEXT    NOT NULL,

  UNIQUE (canonical_url)
);

-- Serves GET /api/projects/{owner}/{repo}.
CREATE UNIQUE INDEX idx_projects_owner_repo ON projects (owner, repo);

-- ----------------------------------------------------------------------
-- project_mentions — evidence linking a project to the post that referenced it.
-- ----------------------------------------------------------------------

CREATE TABLE project_mentions (
  mention_id        INTEGER PRIMARY KEY AUTOINCREMENT,
  topic_id          INTEGER NOT NULL REFERENCES topics (topic_id) ON DELETE CASCADE,
  post_id           INTEGER NOT NULL REFERENCES topic_posts (post_id) ON DELETE CASCADE,
  project_id        INTEGER NOT NULL REFERENCES projects (project_id) ON DELETE CASCADE,
  -- The URL exactly as it appeared in the post, subpath and all. Proof that the
  -- canonicalization was not invented; never used as an identity.
  evidence_url      TEXT    NOT NULL,
  evidence_excerpt  TEXT,
  confidence        REAL,
  prompt_version    TEXT    NOT NULL,
  detected_at       TEXT    NOT NULL,

  -- Re-running the same topic must not duplicate mentions.
  UNIQUE (topic_id, post_id, project_id)
);

CREATE INDEX idx_mentions_topic ON project_mentions (topic_id);
CREATE INDEX idx_mentions_project ON project_mentions (project_id);
