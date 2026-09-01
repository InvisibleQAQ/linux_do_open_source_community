"""Sync orchestrator — one bounded Cron run.

Shape of a run:

  1. Open a `sync_runs` row.
  2. Read the channel feed, extract and upsert topic candidates (idempotent).
  3. Claim at most `SYNC_BATCH_SIZE` due topics with a conditional UPDATE each.
  4. Process claimed topics concurrently, bounded, isolating per-topic failures.
  5. Close the `sync_runs` row with counts and a bounded error summary.

Constraints this shape exists to satisfy, all of them platform facts rather than
preferences:

  * **Concurrency <= 6.** A Worker invocation may have at most six connections
    simultaneously waiting for response headers.
  * **Subrequests.** Paid gives 10,000 per invocation, and D1 queries count too.
    Free gives 50, which 20 topics cannot fit in — see the limits table in the
    root CLAUDE.md.
  * **CPU 30 s, wall clock 15 min** for a `*/5` cron on Paid. Network waiting does
    not count toward CPU, so the binding constraint here is CPU spent parsing.
  * **No transactions.** Claiming is a single conditional UPDATE whose
    `meta.changes` says whether this run won the row; per-topic writes are one
    `batch()` call.
  * **asyncio only.** `threading` and `multiprocessing` import but do not work in
    the wasm VM.

`env` arrives as a parameter because `scheduled(self, controller, env, ctx)` has
no request and therefore no ASGI scope to read it from.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from linuxdo_oss.config import Settings, load_settings
from linuxdo_oss.persistence.d1 import clamp_text

logger = logging.getLogger(__name__)

__all__ = ["run_sync"]


# One conditional UPDATE per candidate. Verified against the schema in
# backend/tests/test_schema.py: the first caller gets changes == 1, a concurrent
# caller gets 0. There is no transaction, so read-then-write would race.
CLAIM_TOPIC_SQL = """
UPDATE topics
   SET status = 'fetching',
       lease_expires_at = ?,
       attempts = attempts + 1,
       updated_at = ?
 WHERE topic_id = ?
   AND (status = 'discovered' OR (status = 'failed' AND retry_after <= ?))
   AND (lease_expires_at IS NULL OR lease_expires_at < ?)
"""

# Candidates for this run: newly discovered first, then retriable failures whose
# backoff has elapsed. Uses idx_topics_claim.
DUE_TOPICS_SQL = """
SELECT topic_id
  FROM topics
 WHERE (status = 'discovered' OR (status = 'failed' AND retry_after <= ?))
   AND (lease_expires_at IS NULL OR lease_expires_at < ?)
 ORDER BY CASE status WHEN 'discovered' THEN 0 ELSE 1 END, topic_id ASC
 LIMIT ?
"""

OPEN_RUN_SQL = """
INSERT INTO sync_runs (scheduled_at, started_at, status) VALUES (?, ?, 'running')
"""

CLOSE_RUN_SQL = """
UPDATE sync_runs
   SET finished_at = ?, status = ?, discovered_count = ?, processed_count = ?,
       published_count = ?, failed_count = ?, error_summary = ?
 WHERE run_id = ?
"""


class RunCounters:
    """Mutable tally for one run. Written to `sync_runs` when the run closes."""

    def __init__(self) -> None:
        self.discovered = 0
        self.processed = 0
        self.published = 0
        self.failed = 0
        # Bounded, category-level. Never a full LLM payload, never a secret.
        self.errors: list[str] = []

    def record_failure(self, topic_id: int, category: str) -> None:
        self.failed += 1
        if len(self.errors) < 20:
            self.errors.append(f"{topic_id}:{category}")

    def summary(self) -> str | None:
        return clamp_text("; ".join(self.errors), 2000) if self.errors else None


async def run_sync(env: Any) -> None:
    """Entrypoint for the Cron Trigger. Never raises.

    A run that fails must not take down the cron: the next invocation is five
    minutes away and the state machine is designed to resume from whatever the
    database says.
    """
    try:
        settings = load_settings(env)
    except RuntimeError:
        # Configuration errors are logged as a category, never with values —
        # a misconfigured LLM_API_KEY must not end up in the log.
        logger.exception("sync aborted: invalid configuration")
        return

    try:
        await _run(env.DB, settings)
    except Exception:
        logger.exception("sync run failed")


async def _run(db: Any, settings: Settings) -> None:
    raise NotImplementedError(
        "Sync orchestration is not implemented yet.\n"
        "\n"
        "Implement in this order, each behind its own port so the orchestrator "
        "stays testable with fakes:\n"
        "  1. linuxdo_oss.feeds.channel  — read CHANNEL_FEED_URL, extract topic ids\n"
        "  2. linuxdo_oss.feeds.topic    — read one topic RSS, select first post + "
        "GitHub-bearing replies\n"
        "  3. linuxdo_oss.classifier     — Responses-API adapter, candidate allowlist\n"
        "  4. linuxdo_oss.persistence.write_repository — batch() upserts\n"
        "\n"
        "The claim/lease and counter machinery above is already fixed and verified; "
        "wire the ports into it rather than restructuring it.\n"
        "\n"
        "BLOCKED ON: the deployed-Worker egress spike (spikes/egress). If Linux.do "
        "or RSSHub reject Cloudflare Worker egress, none of this can work — the PRD "
        "makes that spike a release prerequisite, not an optional check."
    )


async def _process_claimed_topics(
    db: Any,
    settings: Settings,
    topic_ids: list[int],
    counters: RunCounters,
) -> None:
    """Process claimed topics concurrently, isolating per-topic failures.

    The semaphore is the only thing standing between this and the six-connection
    ceiling. Failure isolation is per task, inside `process`, rather than via
    `gather(return_exceptions=True)`: catching at the task means the failure is
    counted and categorised where the topic id is still in scope, and `gather`
    never sees an exception to propagate.
    """
    semaphore = asyncio.Semaphore(settings.max_concurrency)

    async def process(topic_id: int) -> None:
        async with semaphore:
            try:
                await _process_topic(db, settings, topic_id)
                counters.processed += 1
            except Exception as error:
                logger.warning("topic %s failed: %s", topic_id, type(error).__name__)
                counters.record_failure(topic_id, type(error).__name__)

    await asyncio.gather(*(process(topic_id) for topic_id in topic_ids))


async def _process_topic(db: Any, settings: Settings, topic_id: int) -> None:
    raise NotImplementedError("see the plan in _run()")
