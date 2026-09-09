"""Write repository — every mutation one Cron run performs, behind a narrow door.

The orchestrator hands over plain Python values and gets plain Python values back.
It never sees SQL, never sees a surrogate database id, and never sees a `JsProxy`:
rows cross the boundary through `d1.py`'s helpers, and D1 `meta` fields are read
into `int` here and nowhere else.

Three platform facts shape every function below. They are not preferences.

  * **There are no interactive transactions.** Statements auto-commit and
    `batch()` is the only rollback boundary, so each logical unit is exactly one
    `batch()` call. Claiming work is a single conditional UPDATE whose
    `meta.changes` reports whether this run won the row — a read-then-write would
    race with an overlapping Cron invocation.
  * **At most 100 bound parameters per statement.** Every multi-row insert goes
    through `d1.chunk_rows`, and `_statement` re-checks the result, which is what
    catches a miscounted row template rather than letting D1 reject it in
    production.
  * **Idempotency comes from constraints, not from checking first.** Every write
    is `ON CONFLICT ... DO UPDATE` or `DO NOTHING` against a real unique
    constraint in `migrations/0001_initial_schema.sql`. Re-running a topic is a
    no-op, which is what makes an expired lease safe to reclaim.

---

## The sequencing problem, and how `publish_topic_result` solves it

A mention needs a `post_id` and a `project_id`, but the projects it points at are
upserted as part of the same publish. There is no way to read a generated id back
inside a batch, so the naive shape is "upsert projects, commit, read the ids,
insert mentions, commit" — two commits, and a run that dies between them leaves
`projects` rows that no mention points at.

That is avoided entirely. The publish is **one** `batch()`:

  1. `INSERT INTO projects ... ON CONFLICT (canonical_url) DO UPDATE` (chunked).
  2. `INSERT INTO project_mentions` whose `project_id` column is a scalar
     sub-select on `canonical_url` — resolved *while the batch runs*, after step 1
     has taken effect. The id never travels through Python, so no read-back and no
     intermediate commit are needed.
  3. The `topics` row flips to `published`, last, so a partially applied batch
     could never expose a half-written mention set.

`post_id` does not use the same trick: posts were written by an earlier call, so
resolving them costs one indexed SELECT and gives a typed error for a missing
post, where a sub-select would only produce a NOT NULL violation. That read is
*not* a read-then-write race — the claim lease makes this run the topic's
exclusive writer, and post identity is `(topic_id, post_number)`, supplied by the
caller.

Both reference sets are validated in Python before the batch is built: a mention
naming a `post_number` that was never saved, or a `canonical_url` that is not in
this call's project list, raises `WriteError`. Requiring every mention to name a
project in the same call is what makes the sub-select in step 2 provably non-NULL.

**If the run dies anyway**, at any point: the batch rolled back or never ran, the
`topics` row is still `classifying` holding a lease, and `reclaim_expired_leases`
returns it to `ready` once that lease expires. The next run redoes the topic from
the start, and every step of that redo is an upsert against a unique constraint,
so the second attempt produces the same rows as the first — no duplicate posts,
projects or mentions.

---

## States this module writes

`ready` (save_discovered_topics) -> `classifying` (claim_topic) ->
`published` | `not_relevant` | `failed`.

Four states, not the original six. `discovered` and `fetching` named the gap
between knowing a topic exists and holding its text; a Discourse tag feed delivers
both in one item, so the gap closed and the two statuses that described it have
nothing left to mean. They remain legal values in the schema and are simply never
written, which is why dropping them cost no migration.

`ready` therefore means "text stored, not yet judged" rather than the former
"fetched, not yet judged", and it is where a row is born. Its selection in
`DUE_TOPICS_SQL` and its literal in `UPSERT_DISCOVERED_TOPIC_SQL` are one decision
written in two files: change either alone and every row lands in a status nothing
selects, which is silent — no error, no rows, no classification, forever.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any

from linuxdo_oss.domain.timestamps import is_iso_utc
from linuxdo_oss.persistence.d1 import (
    MAX_BOUND_PARAMS,
    MAX_TEXT_BYTES,
    chunk_rows,
    clamp_text,
    execute,
    query_all,
    query_one,
)

__all__ = [
    "MAX_ERROR_BYTES",
    "MAX_EXCERPT_BYTES",
    "DiscoveredTopic",
    "MentionInsert",
    "PostRow",
    "ProjectUpsert",
    "StoredPost",
    "WriteError",
    "claim_topic",
    "close_run",
    "list_due_topics",
    "load_stored_posts",
    "mark_failed",
    "mark_not_relevant",
    "open_run",
    "publish_topic_result",
    "reclaim_expired_leases",
    "save_discovered_topics",
    "topic_attempts",
]

# `topics.canonical_url` arrives already built by `feeds/tag_feed.py`, from the
# host in `TAG_FEED_URL` and an id validated out of the item's guid. It is not
# assembled here from a hard-coded host any more: the pipeline reads whichever
# Discourse instance is configured, and a constant would silently mislabel every
# row the moment that is not linux.do.

# Bounded diagnostics. `topics.last_error` shares the 2000-byte budget that
# `RunCounters.summary()` uses for `sync_runs.error_summary`, so one oversized
# upstream message cannot become a large row on either table.
MAX_ERROR_BYTES = 2000

# The PRD calls the mention evidence a "short excerpt". This is that word as a
# number, applied at the boundary rather than trusted from the classifier.
MAX_EXCERPT_BYTES = 2000


class WriteError(RuntimeError):
    """A write referenced a row that does not exist.

    Carries `retryable` for the same reason `FetchError` does: the orchestrator
    classifies a failure from the exception, not by inspecting which layer raised
    it. A dangling reference is permanent — the same inputs produce the same gap —
    so it is recorded without a retry time.
    """

    def __init__(self, message: str, *, retryable: bool = False) -> None:
        super().__init__(message)
        self.retryable = retryable


# ----------------------------------------------------------------------
# Input records
# ----------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class PostRow:
    """One retained post, as the tag feed reader produced it.

    `post_number` is the identity within the topic and the fallback identity when
    the feed carries no GUID, so it is required while everything descriptive is
    not.
    """

    post_number: int
    source_url: str
    cleaned_text: str
    is_first_post: bool
    guid: str | None = None
    author: str | None = None
    published_at: str | None = None


@dataclass(frozen=True, slots=True)
class DiscoveredTopic:
    """One topic and its text, as the tag feed reader produced it.

    Declared here rather than reusing `feeds/tag_feed.py::TopicFeed` so that
    `persistence/` keeps its back to `feeds/`: the write layer is told what to
    store, it does not learn where rows come from. `sync.py` owns the conversion,
    which is the same arrangement `PostRow` already had.
    """

    topic_id: int
    canonical_url: str
    title: str | None
    author: str | None
    published_at: str | None
    posts: Sequence[PostRow]


@dataclass(frozen=True, slots=True)
class StoredPost:
    """A post read back for classification — only the two fields that decide it."""

    post_number: int
    text: str


@dataclass(frozen=True, slots=True)
class ProjectUpsert:
    """One globally deduplicated repository. `canonical_url` is the identity."""

    canonical_url: str
    owner: str
    repo: str
    display_name: str
    summary: str


@dataclass(frozen=True, slots=True)
class MentionInsert:
    """Evidence that one post referenced one project.

    Both references are natural keys, not database ids: `post_number` within the
    topic, and the project's `canonical_url`. That is what keeps surrogate ids
    inside this module.
    """

    post_number: int
    canonical_url: str
    evidence_url: str
    evidence_excerpt: str | None = None
    confidence: float | None = None


# ----------------------------------------------------------------------
# SQL
# ----------------------------------------------------------------------

# `{values}` is filled by `_values_clause`. Each row template carries its own
# parameter count (`_params_per_row` counts the placeholders), so a column added
# to one of these cannot silently break the chunk arithmetic.

_TOPIC_ROW = "(?, ?, ?, ?, ?, 'ready', ?, ?)"

# A topic is born 'ready', not 'discovered'. The tag feed delivers the first post's
# full text in the same response that announces the topic, so there is no state in
# which a row is known but its text is not — the two former statuses 'discovered'
# and 'fetching' described a fetch step that no longer exists. `DUE_TOPICS_SQL`
# selects on 'ready', and changing one of these without the other silently parks
# every row: a status nothing selects is a topic nothing ever classifies.
#
# Metadata is written here rather than by a later UPDATE for the same reason. It
# arrives with the text, and `published_at` is the public feed's sort key, so a row
# that existed without it would be orderable only by accident.
#
# DO NOTHING, never DO UPDATE: the feed is ordered by last activity, so an old
# topic reappears at the top whenever anyone replies to it. DO UPDATE would rewrite
# a published topic's text on every such bump — re-running the classifier's inputs
# under a settled result — and resetting the status would reprocess the entire
# backlog on every run, forever.
UPSERT_DISCOVERED_TOPIC_SQL = """
INSERT INTO topics
  (topic_id, canonical_url, title, author, published_at, status, discovered_at, updated_at)
VALUES {values}
ON CONFLICT (topic_id) DO NOTHING
"""

# The ids of topics already known, so a bump does not rewrite settled rows. One
# query for the whole feed rather than one per topic: D1 round trips count against
# the subrequest budget, and thirty of them would buy nothing over a single IN.
EXISTING_TOPIC_IDS_SQL = """
SELECT topic_id FROM topics WHERE topic_id IN ({placeholders})
"""

# `post_number` -> `post_id`, for resolving mention targets at publish time.
# Deliberately not merged with STORED_POSTS_SQL below: publishing does not need
# the text, and `cleaned_text` is by far the widest column on this table.
POST_IDS_SQL = """
SELECT post_number, post_id
  FROM topic_posts
 WHERE topic_id = ?
 ORDER BY post_number ASC
"""

# What `_process_topic` classifies. Read back from D1 rather than carried in memory
# from the feed read, and that is deliberate: a topic retried after a failure may
# have long fallen out of the thirty-item window, so an in-memory path would work
# for fresh topics and quietly break for retried ones. One code path, one source.
STORED_POSTS_SQL = """
SELECT post_number, cleaned_text
  FROM topic_posts
 WHERE topic_id = ?
 ORDER BY post_number ASC
"""

_POST_ROW = "(?, ?, ?, ?, ?, ?, ?, ?, ?)"

# `created_at` is deliberately absent from the DO UPDATE list: it records when the
# post was first seen, and a re-run must not move it.
UPSERT_TOPIC_POST_SQL = """
INSERT INTO topic_posts
  (topic_id, post_number, guid, author, published_at, source_url,
   cleaned_text, is_first_post, created_at)
VALUES {values}
ON CONFLICT (topic_id, post_number) DO UPDATE SET
  guid          = excluded.guid,
  author        = excluded.author,
  published_at  = excluded.published_at,
  source_url    = excluded.source_url,
  cleaned_text  = excluded.cleaned_text,
  is_first_post = excluded.is_first_post
"""

_PROJECT_ROW = "(?, ?, ?, ?, ?, ?, ?)"

# The summary is the current value and history is not kept (see the schema), so
# the most recent classification wins — including when a later topic mentions a
# project an earlier topic already published. `created_at` stays at first sight.
UPSERT_PROJECT_SQL = """
INSERT INTO projects
  (canonical_url, owner, repo, display_name, summary, created_at, updated_at)
VALUES {values}
ON CONFLICT (canonical_url) DO UPDATE SET
  display_name = excluded.display_name,
  summary      = excluded.summary,
  updated_at   = excluded.updated_at
"""

# The scalar sub-select is the whole trick: `project_id` is resolved inside the
# batch, after the project upsert above has run, so no generated id has to make a
# round trip through Python and the publish stays one rollback boundary.
_MENTION_ROW = "(?, ?, (SELECT project_id FROM projects WHERE canonical_url = ?), ?, ?, ?, ?, ?)"

INSERT_MENTION_SQL = """
INSERT INTO project_mentions
  (topic_id, post_id, project_id, evidence_url, evidence_excerpt,
   confidence, prompt_version, detected_at)
VALUES {values}
ON CONFLICT (topic_id, post_id, project_id) DO NOTHING
"""

# Every settling transition clears the lease. A settled row holding an unexpired
# lease would be invisible to `DUE_TOPICS_SQL` for no reason, and a 'failed' row
# would sit out its own backoff twice.
PUBLISH_TOPIC_SQL = """
UPDATE topics
   SET status = 'published', lease_expires_at = NULL, retry_after = NULL,
       last_error = NULL, updated_at = ?
 WHERE topic_id = ?
"""

MARK_NOT_RELEVANT_SQL = """
UPDATE topics
   SET status = 'not_relevant', lease_expires_at = NULL, retry_after = NULL,
       last_error = NULL, updated_at = ?
 WHERE topic_id = ?
"""

# `retry_after = NULL` means "never again": `DUE_TOPICS_SQL` compares
# `retry_after <= ?`, and NULL never satisfies it. That is how the error-handling
# spec's "permanent validation failure, recorded without an immediate retry" is
# expressed — as data, not as a second status value.
MARK_FAILED_SQL = """
UPDATE topics
   SET status = 'failed', retry_after = ?, last_error = ?,
       lease_expires_at = NULL, updated_at = ?
 WHERE topic_id = ?
"""

# The counterpart to the claim lease. Without this a run that died mid-topic would
# park its rows forever: `DUE_TOPICS_SQL` only ever looks at 'ready' and 'failed',
# so an in-flight status with an expired lease is unreachable. Attempts are not
# decremented — a topic that keeps killing runs must still run out of them.
#
# 'classifying' is now the only in-flight status. 'fetching' no longer exists, and
# 'ready' is where a claim *starts* rather than somewhere it can stall: with the
# text already stored, claiming leads straight into classification. Listing 'ready'
# here would make this statement clear the lease of a row a live run just took.
RECLAIM_EXPIRED_LEASES_SQL = """
UPDATE topics
   SET status = 'ready', lease_expires_at = NULL, updated_at = ?
 WHERE lease_expires_at IS NOT NULL
   AND lease_expires_at < ?
   AND status = 'classifying'
"""


# ----------------------------------------------------------------------
# Statement plumbing
# ----------------------------------------------------------------------


def _params_per_row(row_template: str) -> int:
    return row_template.count("?")


def _values_clause(row_template: str, rows: int) -> str:
    return ", ".join([row_template] * rows)


def _statement(db: Any, sql: str, params: Sequence[Any]) -> Any:
    """One bound statement, ready for a batch.

    `d1.execute` cannot serve here because a batch needs the statement *built*
    rather than run, so the bound-parameter ceiling is re-checked at this single
    construction point. It fires when a row template's placeholder count and the
    tuple actually assembled disagree — the one mistake `chunk_rows` cannot catch.
    """
    if len(params) > MAX_BOUND_PARAMS:
        raise ValueError(
            f"{len(params)} bound parameters exceeds D1's limit of {MAX_BOUND_PARAMS}; "
            "use chunk_rows()"
        )
    return db.prepare(sql).bind(*params)


def _insert_statements(
    db: Any, sql: str, row_template: str, rows: list[tuple[Any, ...]]
) -> list[Any]:
    """Chunked multi-row insert statements, all destined for one batch."""
    if not rows:
        return []

    statements = []
    for chunk in chunk_rows(rows, _params_per_row(row_template)):
        bound = sql.format(values=_values_clause(row_template, len(chunk)))
        statements.append(_statement(db, bound, [value for row in chunk for value in row]))

    return statements


async def _run_batch(db: Any, statements: list[Any]) -> list[int]:
    """Run one batch and return each statement's `changes`.

    A plain Python list is passed through: that is the form in Cloudflare's own
    Python `D1Database::batch` example, and `d1.py` already records that D1 handles
    the conversion — reaching for `pyodide.ffi.to_js` here would be an undocumented
    guess that could only fail after deploy.

    `meta.changes` becomes an `int` immediately, so no JsProxy leaves.
    """
    results = await db.batch(statements)
    return [int(result.meta.changes) for result in results]


def _require_iso(value: str, field: str) -> str:
    """Reject a timestamp that is not in the one canonical format.

    The schema compares timestamps as TEXT (`retry_after <= ?`, `ORDER BY
    published_at DESC`), which is only correct while every writer emits the same
    fixed-width UTC format. A hand-formatted value produces no error and no
    warning — just silently wrong ordering — so it is refused at the door.
    """
    if not is_iso_utc(value):
        raise ValueError(f"{field} must be an ISO 8601 UTC millisecond timestamp, got {value!r}")
    return value


def _optional_iso(value: str | None, field: str) -> str | None:
    return None if value is None else _require_iso(value, field)


def _clamped(value: str, limit: int = MAX_TEXT_BYTES) -> str:
    """`clamp_text` for a NOT NULL column: bounded, and never None."""
    return clamp_text(value, limit) or ""


# ----------------------------------------------------------------------
# Run bookkeeping
# ----------------------------------------------------------------------

# The claim, due-list and run-bookkeeping SQL live in `sync.py` and are imported
# inside the functions that use them, not at module scope. `sync.py` will import
# this module at module scope, and the two module-level imports together would be
# a cycle that fails at deploy time — the same reason `adapters/http.py` imports
# `js` inside `_abort_signal`. The constants are still imported rather than
# restated, so `test_sync_sql.py` and this module cannot drift apart.


async def open_run(db: Any, *, scheduled_at: str, started_at: str) -> int:
    """Open a `sync_runs` row and return its id.

    The id comes from `meta.last_row_id` rather than a follow-up SELECT: with no
    transaction, `SELECT max(run_id)` could return a concurrent run's row.
    """
    _require_iso(scheduled_at, "scheduled_at")
    _require_iso(started_at, "started_at")

    from linuxdo_oss.sync import OPEN_RUN_SQL

    meta = await execute(db, OPEN_RUN_SQL, (scheduled_at, started_at))
    return int(meta.last_row_id)


async def close_run(
    db: Any,
    run_id: int,
    *,
    finished_at: str,
    status: str,
    discovered: int,
    processed: int,
    published: int,
    failed: int,
    error_summary: str | None,
) -> None:
    """Record the outcome of a run. The summary is category-level and bounded."""
    _require_iso(finished_at, "finished_at")

    from linuxdo_oss.sync import CLOSE_RUN_SQL

    await execute(
        db,
        CLOSE_RUN_SQL,
        (
            finished_at,
            status,
            int(discovered),
            int(processed),
            int(published),
            int(failed),
            clamp_text(error_summary, MAX_ERROR_BYTES),
            int(run_id),
        ),
    )


# ----------------------------------------------------------------------
# Discovery and claiming
# ----------------------------------------------------------------------


async def _post_ids(db: Any, topic_id: int) -> dict[int, int]:
    """`post_number` -> `post_id` for one topic.

    Reading this without a transaction is safe because the claim lease makes this
    run the topic's exclusive writer for the duration.
    """
    rows = await query_all(db, POST_IDS_SQL, (int(topic_id),))
    return {int(row["post_number"]): int(row["post_id"]) for row in rows}


async def _existing_topic_ids(db: Any, topic_ids: Sequence[int]) -> set[int]:
    """Which of `topic_ids` are already rows. Chunked to respect the bind limit."""
    found: set[int] = set()

    for chunk in chunk_rows([(topic_id,) for topic_id in topic_ids], 1):
        ids = [row[0] for row in chunk]
        sql = EXISTING_TOPIC_IDS_SQL.format(placeholders=", ".join("?" * len(ids)))
        found.update(int(row["topic_id"]) for row in await query_all(db, sql, tuple(ids)))

    return found


async def save_discovered_topics(db: Any, topics: Sequence[DiscoveredTopic], *, now: str) -> int:
    """Register new topics together with their text. Returns how many were new.

    One call does what discovery and fetching used to split between them, because
    the tag feed no longer splits them: the first post's text arrives in the same
    item that announces the topic. A row and its evidence are therefore created
    together, and `topics.status` never passes through a state where one exists
    without the other.

    **Topics already known are skipped entirely, text included.** The feed is
    ordered by last activity, so every reply to an old topic puts it back in the
    window; rewriting `cleaned_text` on each of those bumps would keep changing the
    inputs under a result the classifier has already settled, and re-running a
    published topic is explicitly out of scope. The membership test is one query
    for the whole feed — `ON CONFLICT DO NOTHING` alone could not express this,
    since it cannot stop the accompanying `topic_posts` write.

    That leaves the conflict clause as the guard against a genuinely concurrent
    run inserting the same topic between the membership query and this batch. The
    count returned is the batch's own `changes`, so a topic lost to that race is
    correctly not counted as discovered by this run.
    """
    _require_iso(now, "now")

    unique = {topic.topic_id: topic for topic in topics}
    if not unique:
        return 0

    known = await _existing_topic_ids(db, list(unique))
    fresh = [topic for topic_id, topic in unique.items() if topic_id not in known]
    if not fresh:
        return 0

    topic_rows = [
        (
            int(topic.topic_id),
            topic.canonical_url,
            clamp_text(topic.title),
            clamp_text(topic.author),
            _optional_iso(topic.published_at, "published_at"),
            now,
            now,
        )
        for topic in fresh
    ]

    post_rows: list[tuple[Any, ...]] = []
    for topic in fresh:
        post_rows.extend(_post_rows(int(topic.topic_id), topic.posts, now))

    # Topic statements first, and all of them: `topic_posts.topic_id` is a foreign
    # key, so a post chunk running before the last topic chunk would reference a
    # row that does not exist yet.
    statements = _insert_statements(db, UPSERT_DISCOVERED_TOPIC_SQL, _TOPIC_ROW, topic_rows)
    inserted = len(statements)
    statements.extend(_insert_statements(db, UPSERT_TOPIC_POST_SQL, _POST_ROW, post_rows))

    changes = await _run_batch(db, statements)

    return sum(changes[:inserted])


async def load_stored_posts(db: Any, topic_id: int) -> list[StoredPost]:
    """The persisted posts for one topic, ordered by post number.

    This is what the classifier reads, and it comes from D1 rather than from the
    feed the same run just parsed. A topic being retried after a failure may have
    fallen out of the thirty-item window entirely, so a memory path would serve
    fresh topics and quietly starve retried ones — two code paths where the
    slower, uniform one costs a single indexed read.
    """
    rows = await query_all(db, STORED_POSTS_SQL, (int(topic_id),))

    return [
        StoredPost(post_number=int(row["post_number"]), text=str(row["cleaned_text"] or ""))
        for row in rows
    ]


async def list_due_topics(db: Any, *, now: str, limit: int) -> list[int]:
    """Topics eligible for this run: new ones first, then elapsed retries."""
    _require_iso(now, "now")

    if limit <= 0:
        return []

    from linuxdo_oss.sync import DUE_TOPICS_SQL

    rows = await query_all(db, DUE_TOPICS_SQL, (now, now, int(limit)))
    return [int(row["topic_id"]) for row in rows]


async def claim_topic(db: Any, topic_id: int, *, now: str, lease_until: str) -> bool:
    """Take the lease on one topic. True when this run won it.

    One conditional UPDATE, and `meta.changes` is the answer. Being listed as due
    is not a claim: between the list and the claim an overlapping Cron run may have
    taken the row, and only the UPDATE's own WHERE clause can settle that.
    """
    _require_iso(now, "now")
    _require_iso(lease_until, "lease_until")

    from linuxdo_oss.sync import CLAIM_TOPIC_SQL

    meta = await execute(db, CLAIM_TOPIC_SQL, (lease_until, now, int(topic_id), now, now))
    return int(meta.changes) > 0


async def topic_attempts(db: Any, topic_id: int) -> int:
    """How many times this topic has been claimed. 0 when the row is gone.

    Read on the failure path only, to decide whether the retry budget in
    `sync.py` is spent. `CLAIM_TOPIC_SQL` increments the column but a D1 UPDATE
    reports only `meta.changes`, so the value needs its own SELECT — and adding
    `RETURNING` to the claim would rework machinery the plan says to leave alone.
    """
    from linuxdo_oss.sync import TOPIC_ATTEMPTS_SQL

    row = await query_one(db, TOPIC_ATTEMPTS_SQL, (int(topic_id),))

    return 0 if row is None else int(row["attempts"])


async def reclaim_expired_leases(db: Any, *, now: str) -> int:
    """Return abandoned `classifying` topics to `ready`. Returns how many.

    Run this before claiming. It is the only thing that recovers a topic whose run
    died between the claim and a settling transition.
    """
    _require_iso(now, "now")

    meta = await execute(db, RECLAIM_EXPIRED_LEASES_SQL, (now, now))
    return int(meta.changes)


# ----------------------------------------------------------------------
# Topic content
# ----------------------------------------------------------------------


def _post_rows(topic_id: int, posts: Sequence[PostRow], now: str) -> list[tuple[Any, ...]]:
    """Bound rows for the post upsert, with both unique constraints respected.

    Two collapses happen here rather than in the database:

      * A repeated `post_number` keeps its first occurrence. The upsert's conflict
        target is `(topic_id, post_number)`, and the same target twice in one
        VALUES list is version-dependent behaviour.
      * A GUID repeated within the call is stored as NULL on the later post.
        `idx_topic_posts_guid` is a unique index and a collision would fail the
        whole statement; the schema calls `(topic_id, post_number)` the fallback
        identity, so dropping the duplicate GUID keeps the post and loses only a
        redundant key. A GUID already used by a *different* topic is a malformed
        feed and surfaces as a write failure — SQLite offers no second conflict
        target to absorb it.
    """
    rows: list[tuple[Any, ...]] = []
    seen_numbers: set[int] = set()
    seen_guids: set[str] = set()

    for post in posts:
        number = int(post.post_number)
        if number in seen_numbers:
            continue
        seen_numbers.add(number)

        guid = post.guid
        if guid is not None:
            if guid in seen_guids:
                guid = None
            else:
                seen_guids.add(guid)

        rows.append(
            (
                topic_id,
                number,
                guid,
                clamp_text(post.author),
                _optional_iso(post.published_at, "post.published_at"),
                _clamped(post.source_url),
                _clamped(post.cleaned_text),
                int(bool(post.is_first_post)),
                now,
            )
        )

    return rows


# ----------------------------------------------------------------------
# Settling a topic
# ----------------------------------------------------------------------


def _project_rows(projects: Sequence[ProjectUpsert], now: str) -> list[tuple[Any, ...]]:
    """Bound rows for the project upsert, deduplicated by canonical URL."""
    rows: list[tuple[Any, ...]] = []
    seen: set[str] = set()

    for project in projects:
        if project.canonical_url in seen:
            continue
        seen.add(project.canonical_url)

        rows.append(
            (
                project.canonical_url,
                project.owner,
                project.repo,
                _clamped(project.display_name),
                _clamped(project.summary),
                now,
                now,
            )
        )

    return rows


def _mention_rows(
    topic_id: int,
    mentions: Sequence[MentionInsert],
    post_ids: dict[int, int],
    project_urls: set[str],
    now: str,
    prompt_version: str,
) -> list[tuple[Any, ...]]:
    """Bound rows for the mention insert, with both references pre-validated.

    Validating here turns a dangling reference into a typed `WriteError` while the
    offending value is still in scope. Left to the database it would arrive as a
    NOT NULL violation on `project_id` — a message that names a column instead of
    the topic, the post and the repository that caused it.
    """
    rows: list[tuple[Any, ...]] = []
    seen: set[tuple[int, str]] = set()

    for mention in mentions:
        key = (int(mention.post_number), mention.canonical_url)
        if key in seen:
            continue
        seen.add(key)

        if mention.canonical_url not in project_urls:
            raise WriteError(
                f"mention names project {mention.canonical_url!r}, which is not in this publish"
            )

        post_id = post_ids.get(int(mention.post_number))
        if post_id is None:
            raise WriteError(
                f"mention names post {mention.post_number} of topic {topic_id}, which is not saved"
            )

        rows.append(
            (
                topic_id,
                post_id,
                mention.canonical_url,
                mention.evidence_url,
                clamp_text(mention.evidence_excerpt, MAX_EXCERPT_BYTES),
                None if mention.confidence is None else float(mention.confidence),
                prompt_version,
                now,
            )
        )

    return rows


async def publish_topic_result(
    db: Any,
    topic_id: int,
    *,
    projects: Sequence[ProjectUpsert],
    mentions: Sequence[MentionInsert],
    now: str,
    prompt_version: str,
) -> None:
    """Publish one topic's classification result. One batch, one rollback boundary.

    Projects, mentions and the status flip go into a single `batch()` in that
    order; see this module's docstring for why the mention's `project_id` is a
    sub-select and what happens when the run dies. Every mention must name a
    project present in `projects` — that requirement is what makes the sub-select
    provably non-NULL.
    """
    _require_iso(now, "now")

    topic = int(topic_id)
    project_rows = _project_rows(projects, now)
    post_ids = await _post_ids(db, topic)

    mention_rows = _mention_rows(
        topic,
        mentions,
        post_ids,
        {row[0] for row in project_rows},
        now,
        prompt_version,
    )

    statements = _insert_statements(db, UPSERT_PROJECT_SQL, _PROJECT_ROW, project_rows)
    statements.extend(_insert_statements(db, INSERT_MENTION_SQL, _MENTION_ROW, mention_rows))
    statements.append(_statement(db, PUBLISH_TOPIC_SQL, (now, topic)))

    await _run_batch(db, statements)


async def mark_not_relevant(db: Any, topic_id: int, *, now: str) -> None:
    """Settle a topic that held no repository candidate. Terminal, no retry."""
    _require_iso(now, "now")

    await execute(db, MARK_NOT_RELEVANT_SQL, (now, int(topic_id)))


async def mark_failed(
    db: Any,
    topic_id: int,
    *,
    now: str,
    retry_after: str | None,
    error: str | None,
) -> None:
    """Record a per-topic failure without stopping the run.

    `retry_after` carries the whole retry decision: a timestamp for a retryable
    failure, None for a permanent one. There is no separate "permanent" status
    because `DUE_TOPICS_SQL` already never matches a NULL `retry_after`.

    `error` is a bounded, category-level string. It is stored, so it must never
    contain a secret or an unbounded upstream payload.
    """
    _require_iso(now, "now")

    await execute(
        db,
        MARK_FAILED_SQL,
        (
            _optional_iso(retry_after, "retry_after"),
            clamp_text(error, MAX_ERROR_BYTES),
            now,
            int(topic_id),
        ),
    )
