"""Write repository against a real database.

D1 is SQLite, so every guarantee this module claims — constraint-based
idempotency, the claim lease under contention, chunking under the bound-parameter
ceiling, byte clamping, the state machine — is provable with stdlib `sqlite3` and
the real migration files. No Worker, no wrangler, no network.

`FakeD1` is the whole trick. It puts D1's surface on top of a sqlite3 connection:

    prepare(sql) -> statement
    statement.bind(*params) -> statement
    await statement.all()   -> result.results -> [row.to_py()]
    await statement.first() -> row.to_py() | None
    await statement.run()   -> result.meta.changes / result.meta.last_row_id
    await db.batch([...])   -> [result, ...] in statement order

What it deliberately does NOT model, and what therefore still belongs in the
`worker`-marked layer:

  * **Batch atomicity.** Statements are executed one by one and nothing rolls
    back. A test here cannot prove that a failed batch leaves no partial write.
  * **D1's own limits.** The 100-bound-parameter ceiling, the 100 KB statement
    size and the subrequest budget are not enforced by sqlite3. The parameter
    ceiling is instead asserted directly against the statement log below.
  * **The JsProxy boundary.** `to_py()` here returns a dict because the row was
    always a dict. Only the real runtime can prove nothing leaks.
  * **D1's SQLite build.** Locally 3.x from CPython; D1 pins its own. Anything
    version-sensitive is resolved in Python by the module under test rather than
    left to the database — see the deduplication in `_post_rows`.

Async functions are driven with `asyncio.run` rather than a plugin, so the file
runs under bare `pytest` with no `pytest-asyncio` installed.
"""

from __future__ import annotations

import asyncio
import sqlite3
from collections.abc import Coroutine
from pathlib import Path
from typing import Any

import pytest

from linuxdo_oss.persistence.d1 import MAX_BOUND_PARAMS, MAX_TEXT_BYTES
from linuxdo_oss.persistence.read_queries import TOPIC_PROJECTS_SQL, TOPICS_PAGE_FIRST_SQL
from linuxdo_oss.persistence.write_repository import (
    MAX_ERROR_BYTES,
    MAX_EXCERPT_BYTES,
    TOPIC_URL_TEMPLATE,
    MentionInsert,
    PostRow,
    ProjectUpsert,
    WriteError,
    claim_topic,
    close_run,
    list_due_topics,
    mark_failed,
    mark_not_relevant,
    open_run,
    publish_topic_result,
    reclaim_expired_leases,
    save_topic_posts,
    upsert_discovered_topics,
)

MIGRATIONS_DIR = Path(__file__).resolve().parents[2] / "migrations"

NOW = "2026-08-31T15:00:00.000Z"
LEASE_UNTIL = "2026-08-31T15:05:00.000Z"
LATER = "2026-08-31T16:00:00.000Z"
PUBLISHED_AT = "2026-08-31T14:30:00.000Z"

TOPIC_ID = 2837720
OTHER_TOPIC_ID = 2837721
REPO_URL = "https://github.com/octocat/hello-world"


# ----------------------------------------------------------------------
# The D1 fake
# ----------------------------------------------------------------------


class _FakeRow:
    """A D1 row. Real ones are JsProxy and only reveal themselves via `to_py()`."""

    def __init__(self, mapping: dict[str, Any]) -> None:
        self._mapping = mapping

    def to_py(self) -> dict[str, Any]:
        return dict(self._mapping)


class _FakeMeta:
    def __init__(self, changes: int, last_row_id: int | None) -> None:
        self.changes = changes
        self.last_row_id = last_row_id


class _FakeResult:
    def __init__(self, rows: list[_FakeRow], meta: _FakeMeta) -> None:
        self.results = rows
        self.meta = meta


class _FakeStatement:
    """`prepare()` returns one of these; `bind()` returns a new bound copy.

    Binding returns a copy rather than mutating, because D1 prepared statements
    are reusable — `stmt.bind(a)` and `stmt.bind(b)` in one batch is the shape
    Cloudflare's own example uses.
    """

    def __init__(self, db: FakeD1, sql: str, params: tuple[Any, ...] = ()) -> None:
        self._db = db
        self.sql = sql
        self.params = params

    def bind(self, *params: Any) -> _FakeStatement:
        return _FakeStatement(self._db, self.sql, params)

    async def all(self) -> _FakeResult:
        return self._db.execute(self)

    async def first(self) -> _FakeRow | None:
        rows = self._db.execute(self).results
        return rows[0] if rows else None

    async def run(self) -> _FakeResult:
        return self._db.execute(self)


class FakeD1:
    """D1's `prepare`/`bind`/`all`/`first`/`run`/`batch` surface over sqlite3.

    Every executed statement is logged in `statements` and every `batch()` call in
    `batches`, which is how the tests assert the two things a query result cannot
    show: that a multi-row insert was chunked under the parameter ceiling, and
    that one topic's publish is a single batch.
    """

    def __init__(self, connection: sqlite3.Connection) -> None:
        self._connection = connection
        self.statements: list[tuple[str, tuple[Any, ...]]] = []
        self.batches: list[list[str]] = []

    def prepare(self, sql: str) -> _FakeStatement:
        return _FakeStatement(self, sql)

    def execute(self, statement: _FakeStatement) -> _FakeResult:
        self.statements.append((statement.sql, statement.params))

        cursor = self._connection.execute(statement.sql, statement.params)
        rows = [_FakeRow(dict(row)) for row in cursor.fetchall()]

        # `SELECT changes()` rather than `cursor.rowcount`: it is what D1's
        # `meta.changes` reports, and it counts an ON CONFLICT DO NOTHING skip as
        # zero, which is what makes the "how many were new" answer correct.
        changes = self._connection.execute("SELECT changes()").fetchone()[0]

        return _FakeResult(rows, _FakeMeta(int(changes), cursor.lastrowid))

    async def batch(self, statements: list[_FakeStatement]) -> list[_FakeResult]:
        self.batches.append([statement.sql for statement in statements])
        return [self.execute(statement) for statement in statements]


def run(coro: Coroutine[Any, Any, Any]) -> Any:
    return asyncio.run(coro)


# ----------------------------------------------------------------------
# Fixtures and builders
# ----------------------------------------------------------------------


@pytest.fixture()
def connection() -> sqlite3.Connection:
    """In-memory database with every real migration applied, in order.

    `isolation_level=None` mirrors D1: statements auto-commit, and the only
    grouping is `batch()`.
    """
    connection = sqlite3.connect(":memory:", isolation_level=None)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys = ON")

    migrations = sorted(MIGRATIONS_DIR.glob("*.sql"))
    assert migrations, f"no migrations found in {MIGRATIONS_DIR}"

    for migration in migrations:
        connection.executescript(migration.read_text(encoding="utf-8"))

    yield connection
    connection.close()


@pytest.fixture()
def db(connection: sqlite3.Connection) -> FakeD1:
    return FakeD1(connection)


def rows(
    connection: sqlite3.Connection, sql: str, params: tuple[Any, ...] = ()
) -> list[sqlite3.Row]:
    return connection.execute(sql, params).fetchall()


def count(connection: sqlite3.Connection, table: str) -> int:
    return connection.execute(f"SELECT count(*) FROM {table}").fetchone()[0]


def status_of(connection: sqlite3.Connection, topic_id: int) -> str:
    return connection.execute(
        "SELECT status FROM topics WHERE topic_id = ?", (topic_id,)
    ).fetchone()["status"]


def make_post(post_number: int, **overrides: Any) -> PostRow:
    values: dict[str, Any] = {
        "post_number": post_number,
        "source_url": f"https://linux.do/t/topic/{TOPIC_ID}/{post_number}",
        "cleaned_text": f"第 {post_number} 楼的正文 {REPO_URL}",
        "is_first_post": post_number == 1,
        "guid": f"guid-{post_number}",
        "author": "Ammdjs",
        "published_at": PUBLISHED_AT,
    }
    values.update(overrides)
    return PostRow(**values)


def make_project(**overrides: Any) -> ProjectUpsert:
    values: dict[str, Any] = {
        "canonical_url": REPO_URL,
        "owner": "octocat",
        "repo": "hello-world",
        "display_name": "Hello World",
        "summary": "一个示例仓库。",
    }
    values.update(overrides)
    return ProjectUpsert(**values)


def make_mention(post_number: int = 1, **overrides: Any) -> MentionInsert:
    values: dict[str, Any] = {
        "post_number": post_number,
        "canonical_url": REPO_URL,
        "evidence_url": f"{REPO_URL}/issues/12",
        "evidence_excerpt": "节选",
        "confidence": 0.91,
    }
    values.update(overrides)
    return MentionInsert(**values)


def seed_topic(db: FakeD1, topic_id: int, posts: list[PostRow]) -> dict[int, int]:
    """Discover, claim and save one topic — everything before classification."""
    run(upsert_discovered_topics(db, [topic_id], now=NOW))
    assert run(claim_topic(db, topic_id, now=NOW, lease_until=LEASE_UNTIL)) is True
    return run(
        save_topic_posts(
            db,
            topic_id,
            posts,
            now=NOW,
            title="分享一个开源小工具",
            author="Ammdjs",
            published_at=PUBLISHED_AT,
        )
    )


# ----------------------------------------------------------------------
# The happy path, end to end
# ----------------------------------------------------------------------


def test_a_full_topic_publish_lands_in_the_public_read_shape(
    db: FakeD1, connection: sqlite3.Connection
) -> None:
    """The write side is only correct if the read side can see it, so this asserts
    through `read_queries`' own SQL rather than through the tables directly."""
    post_ids = seed_topic(db, TOPIC_ID, [make_post(1), make_post(4)])

    assert sorted(post_ids) == [1, 4]
    assert status_of(connection, TOPIC_ID) == "ready"

    run(
        publish_topic_result(
            db,
            TOPIC_ID,
            projects=[make_project()],
            mentions=[make_mention(1), make_mention(4)],
            now=NOW,
            prompt_version="v1",
        )
    )

    assert status_of(connection, TOPIC_ID) == "published"

    feed = rows(connection, TOPICS_PAGE_FIRST_SQL, (10,))
    assert [row["topic_id"] for row in feed] == [TOPIC_ID]
    assert feed[0]["canonical_url"] == TOPIC_URL_TEMPLATE.format(topic_id=TOPIC_ID)
    assert feed[0]["title"] == "分享一个开源小工具"
    assert feed[0]["published_at"] == PUBLISHED_AT

    projects_sql = TOPIC_PROJECTS_SQL.format(placeholders="?")
    listed = rows(connection, projects_sql, (TOPIC_ID,))

    assert [row["post_number"] for row in listed] == [1, 4]
    assert {row["canonical_url"] for row in listed} == {REPO_URL}
    assert listed[0]["evidence_url"] == f"{REPO_URL}/issues/12"
    assert listed[0]["summary"] == "一个示例仓库。"


def test_publishing_a_topic_is_a_single_batch(db: FakeD1) -> None:
    """`batch()` is D1's only rollback boundary. Projects, mentions and the status
    flip must therefore share one call, or a dead run can publish half a result."""
    seed_topic(db, TOPIC_ID, [make_post(1)])
    before = len(db.batches)

    run(
        publish_topic_result(
            db,
            TOPIC_ID,
            projects=[make_project()],
            mentions=[make_mention(1)],
            now=NOW,
            prompt_version="v1",
        )
    )

    assert len(db.batches) == before + 1

    published = db.batches[-1]
    assert "INTO projects" in published[0]
    assert "INTO project_mentions" in published[1]
    # The status flip is last: a partially applied batch must never leave a topic
    # visible with an incomplete mention set.
    assert "status = 'published'" in published[-1]


# ----------------------------------------------------------------------
# PRD: "Reprocessing the same RSS data produces no duplicate topics, posts,
#       projects or mentions."
# ----------------------------------------------------------------------


def test_reprocessing_the_same_topic_produces_no_duplicates(
    db: FakeD1, connection: sqlite3.Connection
) -> None:
    posts = [make_post(1), make_post(4)]

    for _ in range(2):
        seed_topic(db, TOPIC_ID, posts)
        run(
            publish_topic_result(
                db,
                TOPIC_ID,
                projects=[make_project()],
                mentions=[make_mention(1), make_mention(4)],
                now=NOW,
                prompt_version="v1",
            )
        )

    assert count(connection, "topics") == 1
    assert count(connection, "topic_posts") == 2
    assert count(connection, "projects") == 1
    assert count(connection, "project_mentions") == 2


def test_a_rerun_refreshes_content_but_keeps_first_seen_times(
    db: FakeD1, connection: sqlite3.Connection
) -> None:
    """`created_at` records when a row was first seen; a re-run must not move it,
    or the audit trail becomes "whenever the cron last ran"."""
    seed_topic(db, TOPIC_ID, [make_post(1, cleaned_text="第一版")])
    run(
        publish_topic_result(
            db,
            TOPIC_ID,
            projects=[make_project(summary="第一版摘要")],
            mentions=[make_mention(1)],
            now=NOW,
            prompt_version="v1",
        )
    )

    seed_topic(db, TOPIC_ID, [make_post(1, cleaned_text="第二版")])
    run(
        publish_topic_result(
            db,
            TOPIC_ID,
            projects=[make_project(summary="第二版摘要")],
            mentions=[make_mention(1)],
            now=LATER,
            prompt_version="v2",
        )
    )

    post = rows(connection, "SELECT cleaned_text, created_at FROM topic_posts")[0]
    assert post["cleaned_text"] == "第二版"
    assert post["created_at"] == NOW

    project = rows(connection, "SELECT summary, created_at, updated_at FROM projects")[0]
    assert project["summary"] == "第二版摘要"
    assert project["created_at"] == NOW
    assert project["updated_at"] == LATER

    # The mention is DO NOTHING, so the first detection stands as the record of
    # when this project was first tied to this post.
    mention = rows(connection, "SELECT prompt_version, detected_at FROM project_mentions")[0]
    assert mention["prompt_version"] == "v1"
    assert mention["detected_at"] == NOW


def test_a_topic_seen_again_in_the_channel_feed_is_not_reset(
    db: FakeD1, connection: sqlite3.Connection
) -> None:
    """DO NOTHING, not DO UPDATE: re-discovering a published topic must not send it
    back through the whole pipeline on every run, forever."""
    assert run(upsert_discovered_topics(db, [TOPIC_ID], now=NOW)) == 1

    seed_topic(db, TOPIC_ID, [make_post(1)])
    run(publish_topic_result(db, TOPIC_ID, projects=[], mentions=[], now=NOW, prompt_version="v1"))

    assert run(upsert_discovered_topics(db, [TOPIC_ID], now=LATER)) == 0
    assert status_of(connection, TOPIC_ID) == "published"


def test_duplicate_topic_ids_in_one_call_are_collapsed(
    db: FakeD1, connection: sqlite3.Connection
) -> None:
    """The same topic legitimately appears in several channel items."""
    new_rows = run(upsert_discovered_topics(db, [TOPIC_ID, TOPIC_ID, OTHER_TOPIC_ID], now=NOW))

    assert new_rows == 2
    assert count(connection, "topics") == 2


# ----------------------------------------------------------------------
# PRD: "Same project across multiple topics creates one project row and multiple
#       source mentions."
# ----------------------------------------------------------------------


def test_one_project_two_topics_one_row_two_mentions(
    db: FakeD1, connection: sqlite3.Connection
) -> None:
    for topic_id in (TOPIC_ID, OTHER_TOPIC_ID):
        seed_topic(db, topic_id, [make_post(1)])
        run(
            publish_topic_result(
                db,
                topic_id,
                projects=[make_project()],
                mentions=[make_mention(1)],
                now=NOW,
                prompt_version="v1",
            )
        )

    assert count(connection, "projects") == 1
    assert count(connection, "project_mentions") == 2

    mention_topics = [
        row["topic_id"] for row in rows(connection, "SELECT topic_id FROM project_mentions")
    ]
    assert sorted(mention_topics) == sorted([TOPIC_ID, OTHER_TOPIC_ID])

    # Each mention points at its own topic's post, which is what the project detail
    # page needs to show two distinct provenance links.
    mention_posts = {
        row["post_id"] for row in rows(connection, "SELECT post_id FROM project_mentions")
    }
    assert len(mention_posts) == 2


def test_a_mention_resolves_its_project_id_inside_the_batch(
    db: FakeD1, connection: sqlite3.Connection
) -> None:
    """The mention insert names the project by canonical URL, never by an id that
    made a round trip through Python — that is what keeps the publish atomic."""
    seed_topic(db, TOPIC_ID, [make_post(1)])
    run(
        publish_topic_result(
            db,
            TOPIC_ID,
            projects=[make_project()],
            mentions=[make_mention(1)],
            now=NOW,
            prompt_version="v1",
        )
    )

    project_id = rows(connection, "SELECT project_id FROM projects")[0]["project_id"]
    mention = rows(connection, "SELECT project_id FROM project_mentions")[0]

    assert mention["project_id"] == project_id


# ----------------------------------------------------------------------
# The 100 bound-parameter ceiling
# ----------------------------------------------------------------------


def _widest_statement(db: FakeD1) -> int:
    return max(len(params) for _, params in db.statements)


def _statements_matching(db: FakeD1, fragment: str) -> int:
    return sum(1 for sql, _ in db.statements if fragment in sql)


def test_a_large_post_insert_is_chunked_under_the_parameter_ceiling(
    db: FakeD1, connection: sqlite3.Connection
) -> None:
    """40 posts is far more parameters than one statement may carry. All of them
    must still land, and no statement may exceed the ceiling."""
    posts = [make_post(number) for number in range(1, 41)]
    post_ids = seed_topic(db, TOPIC_ID, posts)

    assert len(post_ids) == 40
    assert count(connection, "topic_posts") == 40
    assert _widest_statement(db) <= MAX_BOUND_PARAMS
    assert _statements_matching(db, "INTO topic_posts") > 1


def test_a_large_discovery_batch_is_chunked(db: FakeD1, connection: sqlite3.Connection) -> None:
    topic_ids = list(range(1, 61))

    assert run(upsert_discovered_topics(db, topic_ids, now=NOW)) == 60
    assert count(connection, "topics") == 60
    assert _widest_statement(db) <= MAX_BOUND_PARAMS
    assert _statements_matching(db, "INTO topics") > 1


def test_many_mentions_are_chunked_and_still_one_batch(
    db: FakeD1, connection: sqlite3.Connection
) -> None:
    posts = [make_post(number) for number in range(1, 21)]
    seed_topic(db, TOPIC_ID, posts)

    projects = [
        make_project(
            canonical_url=f"https://github.com/octocat/repo-{index}",
            repo=f"repo-{index}",
            display_name=f"Repo {index}",
        )
        for index in range(20)
    ]
    mentions = [
        make_mention(index + 1, canonical_url=projects[index].canonical_url) for index in range(20)
    ]

    before = len(db.batches)
    run(
        publish_topic_result(
            db, TOPIC_ID, projects=projects, mentions=mentions, now=NOW, prompt_version="v1"
        )
    )

    assert len(db.batches) == before + 1
    assert count(connection, "projects") == 20
    assert count(connection, "project_mentions") == 20
    assert _widest_statement(db) <= MAX_BOUND_PARAMS
    assert _statements_matching(db, "INTO project_mentions") > 1


# ----------------------------------------------------------------------
# Claim contention and leases
# ----------------------------------------------------------------------


def test_only_one_run_can_claim_a_topic(db: FakeD1, connection: sqlite3.Connection) -> None:
    run(upsert_discovered_topics(db, [TOPIC_ID], now=NOW))

    assert run(claim_topic(db, TOPIC_ID, now=NOW, lease_until=LEASE_UNTIL)) is True
    assert run(claim_topic(db, TOPIC_ID, now=NOW, lease_until=LEASE_UNTIL)) is False

    row = rows(connection, "SELECT status, attempts, lease_expires_at FROM topics")[0]
    assert row["status"] == "fetching"
    assert row["attempts"] == 1
    assert row["lease_expires_at"] == LEASE_UNTIL


def test_due_topics_respects_the_batch_bound_and_the_lease(db: FakeD1) -> None:
    run(upsert_discovered_topics(db, list(range(1, 26)), now=NOW))

    first_batch = run(list_due_topics(db, now=NOW, limit=20))
    assert len(first_batch) == 20

    for topic_id in first_batch:
        assert run(claim_topic(db, topic_id, now=NOW, lease_until=LEASE_UNTIL)) is True

    # The backlog carries over to a later run instead of being reprocessed.
    assert len(run(list_due_topics(db, now=NOW, limit=20))) == 5


def test_a_topic_abandoned_mid_run_is_reclaimed_once_its_lease_expires(
    db: FakeD1, connection: sqlite3.Connection
) -> None:
    """`DUE_TOPICS_SQL` only ever looks at 'discovered' and 'failed', so without the
    reaper a run that died after claiming would park its topics forever."""
    seed_topic(db, TOPIC_ID, [make_post(1)])
    assert status_of(connection, TOPIC_ID) == "ready"
    assert run(list_due_topics(db, now=LATER, limit=20)) == []

    assert run(reclaim_expired_leases(db, now=LATER)) == 1
    assert status_of(connection, TOPIC_ID) == "discovered"
    assert run(list_due_topics(db, now=LATER, limit=20)) == [TOPIC_ID]


def test_an_unexpired_lease_is_not_reclaimed(db: FakeD1, connection: sqlite3.Connection) -> None:
    seed_topic(db, TOPIC_ID, [make_post(1)])

    assert run(reclaim_expired_leases(db, now=NOW)) == 0
    assert status_of(connection, TOPIC_ID) == "ready"


def test_settled_topics_are_never_reclaimed(db: FakeD1, connection: sqlite3.Connection) -> None:
    seed_topic(db, TOPIC_ID, [make_post(1)])
    run(mark_not_relevant(db, TOPIC_ID, now=NOW))

    assert run(reclaim_expired_leases(db, now=LATER)) == 0
    assert status_of(connection, TOPIC_ID) == "not_relevant"
    assert run(list_due_topics(db, now=LATER, limit=20)) == []


def test_a_settled_topic_releases_its_lease(db: FakeD1, connection: sqlite3.Connection) -> None:
    """A settled row holding an unexpired lease would be invisible to the due query
    for no reason — and a failed one would sit out its backoff twice."""
    seed_topic(db, TOPIC_ID, [make_post(1)])
    run(mark_failed(db, TOPIC_ID, now=NOW, retry_after=LATER, error="FetchError"))

    assert rows(connection, "SELECT lease_expires_at FROM topics")[0]["lease_expires_at"] is None


# ----------------------------------------------------------------------
# Failure recording and retry
# ----------------------------------------------------------------------


def test_a_retryable_failure_comes_back_after_its_backoff(
    db: FakeD1, connection: sqlite3.Connection
) -> None:
    seed_topic(db, TOPIC_ID, [make_post(1)])
    run(mark_failed(db, TOPIC_ID, now=NOW, retry_after=LATER, error="FetchError"))

    assert status_of(connection, TOPIC_ID) == "failed"
    assert run(list_due_topics(db, now=NOW, limit=20)) == []
    assert run(list_due_topics(db, now=LATER, limit=20)) == [TOPIC_ID]
    assert run(claim_topic(db, TOPIC_ID, now=LATER, lease_until=LEASE_UNTIL)) is True


def test_a_permanent_failure_without_a_retry_time_is_never_claimed_again(
    db: FakeD1, connection: sqlite3.Connection
) -> None:
    """NULL `retry_after` is the whole "do not retry" mechanism: `retry_after <= ?`
    never matches NULL, so no second status value is needed."""
    seed_topic(db, TOPIC_ID, [make_post(1)])
    run(mark_failed(db, TOPIC_ID, now=NOW, retry_after=None, error="ValidationError"))

    assert rows(connection, "SELECT retry_after FROM topics")[0]["retry_after"] is None
    assert run(list_due_topics(db, now="2099-01-01T00:00:00.000Z", limit=20)) == []
    assert run(claim_topic(db, TOPIC_ID, now=LATER, lease_until=LEASE_UNTIL)) is False


def test_a_not_relevant_topic_is_terminal_and_invisible(
    db: FakeD1, connection: sqlite3.Connection
) -> None:
    """PRD: a topic with no repository candidate settles without an LLM call, and
    never appears in the public feed."""
    seed_topic(db, TOPIC_ID, [make_post(1)])
    run(mark_not_relevant(db, TOPIC_ID, now=NOW))

    assert status_of(connection, TOPIC_ID) == "not_relevant"
    assert run(list_due_topics(db, now=LATER, limit=20)) == []
    assert rows(connection, TOPICS_PAGE_FIRST_SQL, (10,)) == []


def test_publishing_clears_a_previous_failure(db: FakeD1, connection: sqlite3.Connection) -> None:
    seed_topic(db, TOPIC_ID, [make_post(1)])
    run(mark_failed(db, TOPIC_ID, now=NOW, retry_after=LATER, error="FetchError"))

    seed_topic(db, TOPIC_ID, [make_post(1)])
    run(
        publish_topic_result(
            db,
            TOPIC_ID,
            projects=[make_project()],
            mentions=[make_mention(1)],
            now=LATER,
            prompt_version="v1",
        )
    )

    row = rows(connection, "SELECT status, retry_after, last_error FROM topics")[0]
    assert row["status"] == "published"
    assert row["retry_after"] is None
    assert row["last_error"] is None


# ----------------------------------------------------------------------
# Byte bounds
# ----------------------------------------------------------------------


def test_over_long_post_text_is_clamped(db: FakeD1, connection: sqlite3.Connection) -> None:
    seed_topic(db, TOPIC_ID, [make_post(1, cleaned_text="x" * (MAX_TEXT_BYTES * 2))])

    stored = rows(connection, "SELECT cleaned_text FROM topic_posts")[0]["cleaned_text"]
    assert len(stored.encode("utf-8")) == MAX_TEXT_BYTES


def test_over_long_error_and_excerpt_are_clamped(
    db: FakeD1, connection: sqlite3.Connection
) -> None:
    """These strings come from upstream failures and from the LLM. Unbounded, they
    are how a multi-megabyte payload ends up in a row."""
    seed_topic(db, TOPIC_ID, [make_post(1)])
    run(
        publish_topic_result(
            db,
            TOPIC_ID,
            projects=[make_project()],
            mentions=[make_mention(1, evidence_excerpt="节" * MAX_EXCERPT_BYTES)],
            now=NOW,
            prompt_version="v1",
        )
    )
    run(mark_failed(db, TOPIC_ID, now=NOW, retry_after=None, error="e" * (MAX_ERROR_BYTES * 3)))

    excerpt = rows(connection, "SELECT evidence_excerpt FROM project_mentions")[0]
    assert len(excerpt["evidence_excerpt"].encode("utf-8")) <= MAX_EXCERPT_BYTES

    error = rows(connection, "SELECT last_error FROM topics")[0]["last_error"]
    assert len(error.encode("utf-8")) == MAX_ERROR_BYTES


def test_clamping_never_splits_a_utf8_character(db: FakeD1, connection: sqlite3.Connection) -> None:
    """The budget is bytes but the column is text: a truncated multi-byte character
    would be stored as a replacement character or fail to decode."""
    seed_topic(db, TOPIC_ID, [make_post(1, cleaned_text="链" * MAX_TEXT_BYTES)])

    stored = rows(connection, "SELECT cleaned_text FROM topic_posts")[0]["cleaned_text"]
    assert set(stored) == {"链"}
    assert len(stored.encode("utf-8")) <= MAX_TEXT_BYTES


# ----------------------------------------------------------------------
# Feed-shaped hazards
# ----------------------------------------------------------------------


def test_a_repeated_post_number_keeps_its_first_occurrence(
    db: FakeD1, connection: sqlite3.Connection
) -> None:
    """Two rows with the same conflict target in one VALUES list is
    version-dependent behaviour in SQLite, so the collapse happens in Python."""
    seed_topic(
        db,
        TOPIC_ID,
        [make_post(1, cleaned_text="第一次"), make_post(1, cleaned_text="第二次", guid="guid-x")],
    )

    stored = rows(connection, "SELECT cleaned_text FROM topic_posts")
    assert len(stored) == 1
    assert stored[0]["cleaned_text"] == "第一次"


def test_a_guid_repeated_within_a_topic_degrades_to_null(
    db: FakeD1, connection: sqlite3.Connection
) -> None:
    """`idx_topic_posts_guid` is unique, so a duplicate GUID would fail the whole
    statement. The schema names `(topic_id, post_number)` the fallback identity —
    keep the post, drop the redundant key."""
    seed_topic(db, TOPIC_ID, [make_post(1, guid="same"), make_post(2, guid="same")])

    stored = rows(connection, "SELECT post_number, guid FROM topic_posts ORDER BY post_number")
    assert [row["post_number"] for row in stored] == [1, 2]
    assert [row["guid"] for row in stored] == ["same", None]


def test_duplicate_mentions_in_one_call_produce_one_row(
    db: FakeD1, connection: sqlite3.Connection
) -> None:
    seed_topic(db, TOPIC_ID, [make_post(1)])
    run(
        publish_topic_result(
            db,
            TOPIC_ID,
            projects=[make_project(), make_project(summary="重复")],
            mentions=[make_mention(1), make_mention(1)],
            now=NOW,
            prompt_version="v1",
        )
    )

    assert count(connection, "projects") == 1
    assert count(connection, "project_mentions") == 1


def test_a_topic_can_publish_with_no_projects(db: FakeD1, connection: sqlite3.Connection) -> None:
    seed_topic(db, TOPIC_ID, [make_post(1)])
    run(publish_topic_result(db, TOPIC_ID, projects=[], mentions=[], now=NOW, prompt_version="v1"))

    assert status_of(connection, TOPIC_ID) == "published"
    assert count(connection, "project_mentions") == 0


# ----------------------------------------------------------------------
# Dangling references and malformed timestamps
# ----------------------------------------------------------------------


def test_a_mention_for_an_unsaved_post_is_refused(
    db: FakeD1, connection: sqlite3.Connection
) -> None:
    """Caught in Python, where the topic and post number are still in scope. Left
    to the database it would arrive as a NOT NULL violation naming a column."""
    seed_topic(db, TOPIC_ID, [make_post(1)])

    with pytest.raises(WriteError) as error:
        run(
            publish_topic_result(
                db,
                TOPIC_ID,
                projects=[make_project()],
                mentions=[make_mention(99)],
                now=NOW,
                prompt_version="v1",
            )
        )

    assert error.value.retryable is False
    assert "99" in str(error.value)
    assert status_of(connection, TOPIC_ID) == "ready", "nothing may publish on a refused write"


def test_a_mention_for_a_project_outside_this_publish_is_refused(db: FakeD1) -> None:
    """Every mention must name a project in the same call — that requirement is
    what makes the `project_id` sub-select provably non-NULL inside the batch."""
    seed_topic(db, TOPIC_ID, [make_post(1)])

    with pytest.raises(WriteError):
        run(
            publish_topic_result(
                db,
                TOPIC_ID,
                projects=[make_project()],
                mentions=[make_mention(1, canonical_url="https://github.com/other/repo")],
                now=NOW,
                prompt_version="v1",
            )
        )


@pytest.mark.parametrize(
    "moment",
    [
        "2026-08-31 15:00:00",
        "2026-08-31T15:00:00Z",
        "2026-08-31T15:00:00.000+08:00",
        "2026-8-31T15:00:00.000Z",
        "",
    ],
)
def test_a_timestamp_outside_the_one_format_is_refused(db: FakeD1, moment: str) -> None:
    """The schema compares timestamps as TEXT. A different format produces no
    error and no warning downstream — just silently wrong ordering."""
    with pytest.raises(ValueError):
        run(upsert_discovered_topics(db, [TOPIC_ID], now=moment))

    with pytest.raises(ValueError):
        run(mark_not_relevant(db, TOPIC_ID, now=moment))

    with pytest.raises(ValueError):
        run(mark_failed(db, TOPIC_ID, now=NOW, retry_after=moment, error=None))


# ----------------------------------------------------------------------
# Run bookkeeping
# ----------------------------------------------------------------------


def test_a_run_is_opened_and_closed_with_its_counts(
    db: FakeD1, connection: sqlite3.Connection
) -> None:
    run_id = run(open_run(db, scheduled_at=NOW, started_at=NOW))

    opened = rows(connection, "SELECT * FROM sync_runs WHERE run_id = ?", (run_id,))[0]
    assert opened["status"] == "running"
    assert opened["finished_at"] is None

    run(
        close_run(
            db,
            run_id,
            finished_at=LATER,
            status="succeeded",
            discovered=5,
            processed=4,
            published=3,
            failed=1,
            error_summary="2837720:FetchError",
        )
    )

    closed = rows(connection, "SELECT * FROM sync_runs WHERE run_id = ?", (run_id,))[0]
    assert closed["status"] == "succeeded"
    assert closed["finished_at"] == LATER
    assert (closed["discovered_count"], closed["processed_count"]) == (5, 4)
    assert (closed["published_count"], closed["failed_count"]) == (3, 1)
    assert closed["error_summary"] == "2837720:FetchError"


def test_concurrent_runs_get_distinct_ids(db: FakeD1) -> None:
    """The id comes from `meta.last_row_id`, not from `SELECT max(run_id)` — with
    no transaction, the latter could hand two runs the same row."""
    first = run(open_run(db, scheduled_at=NOW, started_at=NOW))
    second = run(open_run(db, scheduled_at=NOW, started_at=NOW))

    assert first != second


def test_a_run_error_summary_is_bounded(db: FakeD1, connection: sqlite3.Connection) -> None:
    run_id = run(open_run(db, scheduled_at=NOW, started_at=NOW))
    run(
        close_run(
            db,
            run_id,
            finished_at=LATER,
            status="failed",
            discovered=0,
            processed=0,
            published=0,
            failed=1,
            error_summary="e" * (MAX_ERROR_BYTES * 2),
        )
    )

    summary = rows(connection, "SELECT error_summary FROM sync_runs")[0]["error_summary"]
    assert len(summary.encode("utf-8")) == MAX_ERROR_BYTES
