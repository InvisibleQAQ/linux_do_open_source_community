"""The orchestrator's own SQL constants, run against stdlib sqlite3.

Imports the constants from `linuxdo_oss.sync` rather than restating them, so the
test cannot drift away from the code it is protecting. Importing `sync` is safe
here: it pulls in no Cloudflare runtime module.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from linuxdo_oss.sync import (
    CLAIM_TOPIC_SQL,
    CLOSE_RUN_SQL,
    DUE_TOPICS_SQL,
    OPEN_RUN_SQL,
    RunCounters,
)

MIGRATIONS_DIR = Path(__file__).resolve().parents[2] / "migrations"

NOW = "2026-08-31T15:00:00.000Z"
LEASE_UNTIL = "2026-08-31T15:05:00.000Z"


@pytest.fixture()
def db() -> sqlite3.Connection:
    connection = sqlite3.connect(":memory:")
    connection.row_factory = sqlite3.Row
    for migration in sorted(MIGRATIONS_DIR.glob("*.sql")):
        connection.executescript(migration.read_text(encoding="utf-8"))
    yield connection
    connection.close()


def add_topic(
    db: sqlite3.Connection,
    topic_id: int,
    *,
    status: str = "ready",
    retry_after: str | None = None,
    lease_expires_at: str | None = None,
) -> None:
    db.execute(
        "INSERT INTO topics (topic_id, canonical_url, status, retry_after, lease_expires_at,"
        " discovered_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
        (
            topic_id,
            f"https://linux.do/t/topic/{topic_id}",
            status,
            retry_after,
            lease_expires_at,
            NOW,
            NOW,
        ),
    )


def claim(db: sqlite3.Connection, topic_id: int, *, now: str = NOW) -> int:
    db.execute(CLAIM_TOPIC_SQL, (LEASE_UNTIL, now, topic_id, now, now))
    return db.execute("SELECT changes()").fetchone()[0]


def due(db: sqlite3.Connection, limit: int, *, now: str = NOW) -> list[int]:
    return [row["topic_id"] for row in db.execute(DUE_TOPICS_SQL, (now, now, limit)).fetchall()]


# ----------------------------------------------------------------------
# Batch bound
# ----------------------------------------------------------------------


def test_due_query_honours_the_batch_limit(db: sqlite3.Connection) -> None:
    """PRD: no run claims more than 20 due topics."""
    for topic_id in range(1, 31):
        add_topic(db, topic_id)

    assert len(due(db, 20)) == 20


def test_backlog_is_left_for_later_runs(db: sqlite3.Connection) -> None:
    for topic_id in range(1, 26):
        add_topic(db, topic_id)

    first_batch = due(db, 20)
    for topic_id in first_batch:
        assert claim(db, topic_id) == 1

    # The claimed rows now hold an unexpired lease, so the next run sees only the
    # remainder — the backlog carries over instead of being reprocessed.
    assert len(due(db, 20)) == 5


def test_newly_discovered_topics_are_claimed_before_retries(db: sqlite3.Connection) -> None:
    add_topic(db, 1, status="failed", retry_after="2026-08-31T14:00:00.000Z")
    add_topic(db, 2, status="ready")

    assert due(db, 10) == [2, 1]


# ----------------------------------------------------------------------
# Claim lease
# ----------------------------------------------------------------------


def test_a_second_run_cannot_claim_the_same_topic(db: sqlite3.Connection) -> None:
    add_topic(db, 1)

    assert claim(db, 1) == 1
    assert claim(db, 1) == 0


def test_claim_increments_attempts_and_sets_the_lease(db: sqlite3.Connection) -> None:
    add_topic(db, 1)
    claim(db, 1)

    row = db.execute("SELECT status, attempts, lease_expires_at, updated_at FROM topics").fetchone()

    assert row["status"] == "classifying"
    assert row["attempts"] == 1
    assert row["lease_expires_at"] == LEASE_UNTIL
    assert row["updated_at"] == NOW


def test_an_expired_lease_is_claimable_again(db: sqlite3.Connection) -> None:
    """A run that crashed mid-topic must not park it forever."""
    add_topic(db, 1, status="ready", lease_expires_at="2026-08-31T14:00:00.000Z")

    assert claim(db, 1) == 1


def test_an_unexpired_lease_blocks_a_claim(db: sqlite3.Connection) -> None:
    add_topic(db, 1, status="ready", lease_expires_at="2026-08-31T15:04:00.000Z")

    assert claim(db, 1) == 0


def test_a_failed_topic_waits_for_its_backoff(db: sqlite3.Connection) -> None:
    add_topic(db, 1, status="failed", retry_after="2026-08-31T16:00:00.000Z")

    assert claim(db, 1) == 0
    assert claim(db, 1, now="2026-08-31T16:00:00.000Z") == 1


@pytest.mark.parametrize("status", ["classifying", "published", "not_relevant"])
def test_in_flight_and_settled_statuses_are_never_claimed(
    db: sqlite3.Connection, status: str
) -> None:
    """'ready' is deliberately absent: it is now where a topic is BORN, not a state
    it reaches mid-run, so it is the one status a claim must accept."""
    add_topic(db, 1, status=status)

    assert claim(db, 1) == 0
    assert due(db, 10) == []


# ----------------------------------------------------------------------
# Run bookkeeping
# ----------------------------------------------------------------------


def test_a_run_can_be_opened_and_closed(db: sqlite3.Connection) -> None:
    run_id = db.execute(OPEN_RUN_SQL, (NOW, NOW)).lastrowid

    assert (
        db.execute("SELECT status FROM sync_runs WHERE run_id = ?", (run_id,)).fetchone()["status"]
        == "running"
    )

    db.execute(CLOSE_RUN_SQL, (LEASE_UNTIL, "succeeded", 5, 4, 3, 1, "1:FetchError", run_id))
    row = db.execute("SELECT * FROM sync_runs WHERE run_id = ?", (run_id,)).fetchone()

    assert row["status"] == "succeeded"
    assert (row["discovered_count"], row["processed_count"]) == (5, 4)
    assert (row["published_count"], row["failed_count"]) == (3, 1)
    assert row["error_summary"] == "1:FetchError"


# ----------------------------------------------------------------------
# Counters
# ----------------------------------------------------------------------


def test_counters_start_empty() -> None:
    counters = RunCounters()

    assert (counters.discovered, counters.processed, counters.published, counters.failed) == (
        0,
        0,
        0,
        0,
    )
    assert counters.summary() is None


def test_error_summary_is_bounded() -> None:
    """The PRD requires a bounded summary: an unbounded one is how a 2 MB payload
    ends up in a row and how a secret ends up in a log."""
    counters = RunCounters()

    for topic_id in range(100):
        counters.record_failure(topic_id, "FetchError")

    assert counters.failed == 100
    assert len(counters.errors) == 20

    summary = counters.summary()
    assert summary is not None
    assert len(summary.encode("utf-8")) <= 2000
