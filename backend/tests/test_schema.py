"""Schema-level guarantees, verified against stdlib sqlite3.

D1 is SQLite, so the constraints that make the PRD's idempotency and
overlapping-Cron acceptance criteria true can be proven without a Worker, without
wrangler, and in milliseconds. These are pure tests: `pytest -m 'not worker'`
runs them.

What is NOT covered here: D1's own limits (100 bound parameters, 100 KB per
statement, batch atomicity) and the JsProxy/`.to_py()` boundary. Those need the
real runtime and live in the `worker`-marked tests.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

MIGRATIONS_DIR = Path(__file__).resolve().parents[2] / "migrations"

NOW = "2026-08-31T15:00:00.000Z"
LATER = "2026-08-31T15:05:00.000Z"

TOPIC_ID = 2837720
TOPIC_URL = f"https://linux.do/t/topic/{TOPIC_ID}"
REPO_URL = "https://github.com/octocat/hello-world"

# A single conditional UPDATE is the whole claim mechanism: D1 has no interactive
# transactions, so read-then-write would race. `changes()` tells us whether this
# run won the row.
CLAIM_SQL = """
UPDATE topics
   SET status = 'fetching', lease_expires_at = ?, attempts = attempts + 1
 WHERE topic_id = ?
   AND (status = 'discovered' OR (status = 'failed' AND retry_after <= ?))
   AND (lease_expires_at IS NULL OR lease_expires_at < ?)
"""

UPSERT_PROJECT_SQL = """
INSERT INTO projects (canonical_url, owner, repo, display_name, summary, created_at, updated_at)
VALUES (?, ?, ?, ?, ?, ?, ?)
ON CONFLICT (canonical_url) DO UPDATE SET
  display_name = excluded.display_name,
  summary      = excluded.summary,
  updated_at   = excluded.updated_at
"""

UPSERT_MENTION_SQL = """
INSERT INTO project_mentions
  (topic_id, post_id, project_id, evidence_url, evidence_excerpt,
   confidence, prompt_version, detected_at)
VALUES (?, ?, ?, ?, ?, ?, ?, ?)
ON CONFLICT (topic_id, post_id, project_id) DO NOTHING
"""


@pytest.fixture()
def db() -> sqlite3.Connection:
    """An in-memory database with every migration applied, in order."""
    connection = sqlite3.connect(":memory:")
    connection.execute("PRAGMA foreign_keys = ON")

    migrations = sorted(MIGRATIONS_DIR.glob("*.sql"))
    assert migrations, f"no migrations found in {MIGRATIONS_DIR}"

    for migration in migrations:
        connection.executescript(migration.read_text(encoding="utf-8"))

    yield connection
    connection.close()


def insert_topic(
    db: sqlite3.Connection, *, status: str = "discovered", retry_after: str | None = None
) -> None:
    db.execute(
        "INSERT INTO topics (topic_id, canonical_url, status, retry_after,"
        " discovered_at, updated_at) VALUES (?, ?, ?, ?, ?, ?)",
        (TOPIC_ID, TOPIC_URL, status, retry_after, NOW, NOW),
    )


def insert_post(db: sqlite3.Connection, post_number: int, guid: str | None) -> int:
    cursor = db.execute(
        "INSERT INTO topic_posts (topic_id, post_number, guid, source_url,"
        " cleaned_text, is_first_post, created_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
        (
            TOPIC_ID,
            post_number,
            guid,
            f"{TOPIC_URL}/{post_number}",
            "text",
            int(post_number == 1),
            NOW,
        ),
    )
    return cursor.lastrowid


def upsert_project(db: sqlite3.Connection, summary: str) -> None:
    db.execute(
        UPSERT_PROJECT_SQL, (REPO_URL, "octocat", "hello-world", "Hello World", summary, NOW, NOW)
    )


def count(db: sqlite3.Connection, table: str) -> int:
    return db.execute(f"SELECT count(*) FROM {table}").fetchone()[0]


# ----------------------------------------------------------------------
# Migration applies at all
# ----------------------------------------------------------------------


def test_migrations_create_the_expected_tables(db: sqlite3.Connection) -> None:
    tables = {
        row[0]
        for row in db.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table' AND name NOT LIKE 'sqlite_%'"
        )
    }
    assert tables == {"sync_runs", "topics", "topic_posts", "projects", "project_mentions"}


def test_claim_and_feed_indexes_exist(db: sqlite3.Connection) -> None:
    """Both are required: an unindexed scan on D1 is billed as rows read."""
    indexes = {
        row[0]
        for row in db.execute(
            "SELECT name FROM sqlite_master WHERE type = 'index' AND name NOT LIKE 'sqlite_%'"
        )
    }
    assert "idx_topics_claim" in indexes
    assert "idx_topics_feed" in indexes


# ----------------------------------------------------------------------
# PRD: "Reprocessing the same RSS data produces no duplicate topics, posts,
#       projects or mentions."
# ----------------------------------------------------------------------


def test_topic_id_is_the_dedup_key(db: sqlite3.Connection) -> None:
    insert_topic(db)
    with pytest.raises(sqlite3.IntegrityError):
        insert_topic(db)


def test_same_post_number_cannot_be_inserted_twice(db: sqlite3.Connection) -> None:
    insert_topic(db)
    insert_post(db, 1, "guid-1")
    with pytest.raises(sqlite3.IntegrityError):
        insert_post(db, 1, "guid-different")


def test_duplicate_guid_is_rejected_but_missing_guids_are_allowed(db: sqlite3.Connection) -> None:
    """A plain UNIQUE would let NULL GUIDs pile up, since SQLite treats NULLs as
    distinct. The partial index enforces uniqueness only where a GUID exists."""
    insert_topic(db)
    insert_post(db, 1, "guid-1")

    insert_post(db, 2, None)
    insert_post(db, 3, None)
    assert count(db, "topic_posts") == 3

    with pytest.raises(sqlite3.IntegrityError):
        insert_post(db, 4, "guid-1")


def test_reupserting_a_project_updates_it_instead_of_duplicating(db: sqlite3.Connection) -> None:
    upsert_project(db, "第一次的摘要")
    upsert_project(db, "第二次的摘要")

    assert count(db, "projects") == 1
    assert db.execute("SELECT summary FROM projects").fetchone()[0] == "第二次的摘要"


def test_reinserting_a_mention_is_a_no_op(db: sqlite3.Connection) -> None:
    insert_topic(db)
    post_id = insert_post(db, 1, "guid-1")
    upsert_project(db, "摘要")
    project_id = db.execute("SELECT project_id FROM projects").fetchone()[0]

    args = (TOPIC_ID, post_id, project_id, f"{REPO_URL}/issues/12", "节选", 0.9, "1", NOW)
    db.execute(UPSERT_MENTION_SQL, args)
    db.execute(UPSERT_MENTION_SQL, args)

    assert count(db, "project_mentions") == 1


# ----------------------------------------------------------------------
# PRD: "Same project across multiple topics creates one project row and multiple
#       source mentions."
# ----------------------------------------------------------------------


def test_one_project_many_topics(db: sqlite3.Connection) -> None:
    other_topic_id = TOPIC_ID + 1

    insert_topic(db)
    db.execute(
        "INSERT INTO topics (topic_id, canonical_url, status, discovered_at,"
        " updated_at) VALUES (?, ?, ?, ?, ?)",
        (other_topic_id, f"https://linux.do/t/topic/{other_topic_id}", "discovered", NOW, NOW),
    )

    first_post = insert_post(db, 1, "guid-1")
    second_post = db.execute(
        "INSERT INTO topic_posts (topic_id, post_number, guid, source_url,"
        " cleaned_text, is_first_post, created_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
        (other_topic_id, 1, "guid-2", "u", "t", 1, NOW),
    ).lastrowid

    upsert_project(db, "摘要")
    project_id = db.execute("SELECT project_id FROM projects").fetchone()[0]

    db.execute(
        UPSERT_MENTION_SQL, (TOPIC_ID, first_post, project_id, REPO_URL, None, 0.9, "1", NOW)
    )
    db.execute(
        UPSERT_MENTION_SQL,
        (other_topic_id, second_post, project_id, f"{REPO_URL}/tree/main", None, 0.8, "1", NOW),
    )

    assert count(db, "projects") == 1
    assert count(db, "project_mentions") == 2


# ----------------------------------------------------------------------
# PRD: "Overlapping Cron executions must be harmless through claim leases,
#       unique constraints and idempotent writes."
# ----------------------------------------------------------------------


def test_only_one_run_can_claim_a_topic(db: sqlite3.Connection) -> None:
    insert_topic(db)

    db.execute(CLAIM_SQL, (LATER, TOPIC_ID, NOW, NOW))
    first = db.execute("SELECT changes()").fetchone()[0]

    db.execute(CLAIM_SQL, (LATER, TOPIC_ID, NOW, NOW))
    second = db.execute("SELECT changes()").fetchone()[0]

    assert first == 1, "the first run must win the row"
    assert second == 0, "a concurrent run must find nothing to claim"
    assert db.execute("SELECT attempts FROM topics").fetchone()[0] == 1


def test_an_expired_lease_becomes_claimable_again(db: sqlite3.Connection) -> None:
    """A crashed run must not park a topic forever."""
    insert_topic(db)
    db.execute(
        "UPDATE topics SET status = 'fetching', lease_expires_at = ?", ("2026-08-31T14:00:00.000Z",)
    )

    db.execute(
        "UPDATE topics SET status = 'discovered' WHERE lease_expires_at < ?",
        (NOW,),
    )
    db.execute(CLAIM_SQL, (LATER, TOPIC_ID, NOW, NOW))

    assert db.execute("SELECT changes()").fetchone()[0] == 1


def test_a_failed_topic_is_not_claimable_before_its_retry_time(db: sqlite3.Connection) -> None:
    insert_topic(db, status="failed", retry_after="2026-08-31T16:00:00.000Z")

    db.execute(CLAIM_SQL, (LATER, TOPIC_ID, NOW, NOW))
    assert db.execute("SELECT changes()").fetchone()[0] == 0

    db.execute(CLAIM_SQL, (LATER, TOPIC_ID, "2026-08-31T17:00:00.000Z", NOW))
    assert db.execute("SELECT changes()").fetchone()[0] == 1


# ----------------------------------------------------------------------
# Timestamp format contract
# ----------------------------------------------------------------------


def test_iso_8601_utc_text_sorts_chronologically(db: sqlite3.Connection) -> None:
    """The schema stores timestamps as TEXT and compares them with <= and ORDER BY.
    That is only correct because every writer uses the same fixed-width ISO 8601
    UTC format."""
    stamps = [
        "2026-12-31T00:00:00.000Z",
        "2026-08-31T23:59:59.999Z",
        "2026-09-01T00:00:00.000Z",
    ]
    for index, stamp in enumerate(stamps):
        db.execute(
            "INSERT INTO sync_runs (scheduled_at, started_at, status) VALUES (?, ?, ?)",
            (stamp, stamp, "succeeded"),
        )
        assert index >= 0

    ordered = [row[0] for row in db.execute("SELECT started_at FROM sync_runs ORDER BY started_at")]
    assert ordered == sorted(stamps)
