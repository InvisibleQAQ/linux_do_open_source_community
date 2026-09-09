"""Sync orchestrator — one bounded Cron run.

Shape of a run:

  1. Open a `sync_runs` row.
  2. Read the tag feed once; every topic arrives with its first post's text, so
     discovery and persistence of that text are one idempotent write.
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

`env` arrives as an explicit argument because `scheduled` has no request and
therefore no ASGI scope to read it from. It comes from `self.env`, NOT from the
handler's own `env` parameter: that parameter is measured to arrive as `None`, so
passing it here made `load_settings` raise on every run. See `main.py`.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Sequence
from typing import TYPE_CHECKING, Any

from linuxdo_oss.config import Settings, load_settings
from linuxdo_oss.persistence.d1 import clamp_text

if TYPE_CHECKING:
    # Type-only, and that is the point: `from __future__ import annotations` makes
    # every annotation a string, so this block never executes and costs nothing at
    # startup. Naming these modules at real module scope WOULD cost — `main.py`
    # imports this module eagerly, which is why every import below is
    # function-local — so they may appear here and nowhere else.
    from linuxdo_oss.classifier import CandidateRepository, Decision
    from linuxdo_oss.feeds.tag_feed import FetchTextPort, TopicFeed
    from linuxdo_oss.persistence.write_repository import (
        DiscoveredTopic,
        MentionInsert,
        ProjectUpsert,
        StoredPost,
    )

logger = logging.getLogger(__name__)

__all__ = ["run_sync"]


# Stored posts are joined with a blank line before going to the model. A topic
# currently holds exactly one post, so this joins nothing — it stays because the
# model input is defined over a post sequence, and a single-element sequence is
# not a special case worth writing a second code path for.
POST_SEPARATOR = "\n\n"

# Retry budget. `spec/backend/error-handling.md` requires a cap and a backoff but
# fixes neither number; these are that decision.
#
# `attempts` is incremented by CLAIM_TOPIC_SQL, so it counts claims rather than
# failures — the topic being processed right now is already at its own attempt
# number. A topic that has burned MAX_ATTEMPTS claims settles with
# `retry_after = NULL`, which DUE_TOPICS_SQL can never match again.
MAX_ATTEMPTS = 5

# Backoff starts at the cron interval because anything shorter cannot be observed:
# no run happens between two `*/5` firings to pick the topic up. Doubling from there
# spreads the four retriable attempts over 5 / 10 / 20 / 40 minutes.
RETRY_BASE_SECONDS = 300

# Longest category token `_failure_label` will copy out of an exception. Every
# value in `llm/errors.py::ERROR_CATEGORIES` is far shorter; this only bounds
# something that reached the label without coming from there.
MAX_CATEGORY_CHARS = 64

# The claim lease must outlive the whole invocation, not one topic. A shorter lease
# would let the NEXT cron run reclaim a topic this run is still working on — the
# 15 min wall-clock ceiling for a Worker is therefore the only safe value, and it
# doubles as the recovery delay when a run dies outright.
LEASE_SECONDS = 900


# One conditional UPDATE per candidate. Verified against the schema in
# backend/tests/test_schema.py: the first caller gets changes == 1, a concurrent
# caller gets 0. There is no transaction, so read-then-write would race.
# Claiming leads straight into classification: the text is already stored, so
# there is nothing between winning the row and calling the model. That is why the
# claimed status is 'classifying' and not the former 'fetching', which named a step
# that no longer exists.
CLAIM_TOPIC_SQL = """
UPDATE topics
   SET status = 'classifying',
       lease_expires_at = ?,
       attempts = attempts + 1,
       updated_at = ?
 WHERE topic_id = ?
   AND (status = 'ready' OR (status = 'failed' AND retry_after <= ?))
   AND (lease_expires_at IS NULL OR lease_expires_at < ?)
"""

# Candidates for this run: newly discovered first, then retriable failures whose
# backoff has elapsed. Uses idx_topics_claim.
#
# 'ready' is where a topic is born now — `save_discovered_topics` writes the row
# and its text together. This selection and that INSERT's status literal are one
# decision in two places: change either alone and every row becomes unreachable,
# sitting in a status nothing selects.
DUE_TOPICS_SQL = """
SELECT topic_id
  FROM topics
 WHERE (status = 'ready' OR (status = 'failed' AND retry_after <= ?))
   AND (lease_expires_at IS NULL OR lease_expires_at < ?)
 ORDER BY CASE status WHEN 'ready' THEN 0 ELSE 1 END, topic_id ASC
 LIMIT ?
"""

# Read only when a topic has already failed, so the successful path pays nothing.
# CLAIM_TOPIC_SQL cannot return this: the claim/lease machinery is verified as it
# stands and adding RETURNING to it would restructure what the plan says to wire.
TOPIC_ATTEMPTS_SQL = """
SELECT attempts FROM topics WHERE topic_id = ?
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
        # `logger.exception` also emits the exception message, so what
        # `load_settings` is allowed to put in one is what gets logged here.
        # Today that is: the offending variable's NAME, the accepted values, the
        # base-URL rule that was broken, and — only for LLM_PROTOCOL and
        # LLM_SCHEMA_MODE — the rejected spelling itself, truncated. Never a URL,
        # a model id or a key. Widening a message in `config.py` widens this log
        # line; see `.trellis/spec/backend/logging-guidelines.md`.
        logger.exception("sync aborted: invalid configuration")
        return

    try:
        # The ONE place the real transport is bound. It is imported here, not at
        # module scope, because `adapters/http.py` does `from workers import fetch`
        # — a module that does not exist on CPython — and it is then passed down as
        # a port, so every function below is exercisable with a fake transport in
        # the pure test layer. Same reason `read_tag_feed` and `classify` both
        # take it as their first argument.
        from linuxdo_oss.adapters.http import fetch_text

        await _run(env.DB, settings, fetch_text)
    except Exception:
        logger.exception("sync run failed")


async def _run(db: Any, settings: Settings, fetch_text: FetchTextPort) -> None:
    """One bounded run, in the five steps this module's docstring names.

    Every import is function-local. The Worker's startup budget is 1 s, `main.py`
    imports this module eagerly, and this path only executes on the cron.

    `close_run` sits in a `finally` so a run that dies still records why. The
    exception then propagates to `run_sync`, which is the only place allowed to
    swallow it — a failed run must leave a closed `sync_runs` row AND a traceback
    in the log, not one or the other.
    """
    from datetime import UTC, datetime, timedelta

    from linuxdo_oss.domain.timestamps import to_iso_utc
    from linuxdo_oss.feeds.tag_feed import read_tag_feed
    from linuxdo_oss.persistence import write_repository as repo

    started = datetime.now(UTC)
    # `scheduled_at` is this clock reading rather than the controller's
    # `scheduledTime`: `run_sync` takes only `env`, and the gap between a cron
    # firing and this line is milliseconds. Widening the signature to carry the
    # controller would buy that precision and cost every caller and test.
    now = to_iso_utc(started)
    lease_until = to_iso_utc(started + timedelta(seconds=LEASE_SECONDS))

    run_id = await repo.open_run(db, scheduled_at=now, started_at=now)
    counters = RunCounters()
    status = "succeeded"
    run_error: str | None = None

    try:
        # Before claiming, never after: this is the only thing that frees a topic
        # whose previous run died between the claim and a settling transition.
        await repo.reclaim_expired_leases(db, now=now)

        discovered = await read_tag_feed(
            fetch_text,
            settings.tag_feed_url,
            timeout_seconds=settings.http_timeout_seconds,
            max_bytes=settings.max_response_bytes,
        )
        counters.discovered = await repo.save_discovered_topics(
            db, [_discovered_topic(topic) for topic in discovered], now=now
        )

        # Being listed as due is not a claim. An overlapping run may take a row
        # between the SELECT and the UPDATE, and only the UPDATE's own WHERE
        # clause settles who won it.
        due = await repo.list_due_topics(db, now=now, limit=settings.sync_batch_size)
        claimed = [
            topic_id
            for topic_id in due
            if await repo.claim_topic(db, topic_id, now=now, lease_until=lease_until)
        ]

        logger.info(
            "run %s: discovered %s new, %s due, claimed %s",
            run_id,
            counters.discovered,
            len(due),
            len(claimed),
        )

        await _process_claimed_topics(db, settings, fetch_text, claimed, counters)
    except Exception as error:
        # A run-level failure has no topic id to hang a category on, so it goes
        # into the summary under `run:`. Without it the `sync_runs` row says
        # "failed" and nothing else, and the PRD requires every run to leave a
        # queryable error summary.
        status = "failed"
        run_error = f"run:{_failure_label(error)}"
        raise
    finally:
        await repo.close_run(
            db,
            run_id,
            finished_at=to_iso_utc(datetime.now(UTC)),
            status=status,
            discovered=counters.discovered,
            processed=counters.processed,
            published=counters.published,
            failed=counters.failed,
            error_summary=_error_summary(counters, run_error),
        )
        logger.info(
            "run %s closed: status=%s discovered=%s processed=%s published=%s failed=%s",
            run_id,
            status,
            counters.discovered,
            counters.processed,
            counters.published,
            counters.failed,
        )


def _error_summary(counters: RunCounters, run_error: str | None) -> str | None:
    """The run's bounded error summary: per-topic categories, plus the category
    that killed the run itself when one did.

    Not clamped here — `close_run` puts the whole value through `clamp_text` with
    `MAX_ERROR_BYTES`, and duplicating the byte budget in a second place is how the
    two drift apart.
    """
    parts = [part for part in (run_error, counters.summary()) if part]

    return "; ".join(parts) if parts else None


async def _process_claimed_topics(
    db: Any,
    settings: Settings,
    fetch_text: FetchTextPort,
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
                if await _process_topic(db, settings, fetch_text, topic_id):
                    counters.published += 1
                counters.processed += 1
            except Exception as error:
                logger.warning("topic %s failed: %s", topic_id, type(error).__name__)
                counters.record_failure(topic_id, _failure_label(error))
                await _record_topic_failure(db, topic_id, error)

    await asyncio.gather(*(process(topic_id) for topic_id in topic_ids))


def _failure_label(error: BaseException) -> str:
    """A bounded, category-level name for one failure. Never a payload or a secret.

    Three sources, each already bounded: the exception's own type name, the
    `category` a `ClassifierError` was raised with (a fixed enum in
    `llm/errors.py`), and the `status` a `FetchError` carries. The
    error-handling spec makes the status explicit — a small integer names nothing
    — and it is the one field that separates "this endpoint does not speak the
    configured protocol" from "the network blinked".

    The exception's *message* is deliberately never used: a fetch error's text can
    carry a URL and a provider body.
    """
    parts = [type(error).__name__]

    category = getattr(error, "category", None)
    if isinstance(category, str) and category:
        # Sliced because `getattr` cannot promise this came from `ERROR_CATEGORIES`
        # — some third-party exception may carry an unrelated `category` — and this
        # string reaches both `topics.last_error` and `sync_runs.error_summary`.
        # Both columns are clamped downstream, but a category is a short fixed
        # token by definition, so bounding it here keeps one oversized value from
        # crowding out the other 19 topics in a run's summary.
        parts.append(category[:MAX_CATEGORY_CHARS])

    status = getattr(error, "status", None)
    if isinstance(status, int) and status:
        parts.append(str(status))

    return ":".join(parts)


async def _record_topic_failure(db: Any, topic_id: int, error: BaseException) -> None:
    """Persist one topic's failure with its retry decision. Never raises.

    The PRD requires a per-topic failure to be *persisted*, not merely counted, so
    this runs inside the isolation block. It therefore must not raise: a D1 write
    that fails here would escape `process` and take the rest of the claimed batch
    down with it, which is the exact guarantee the isolation exists to provide.

    Retryability is read off the exception rather than decided here — `FetchError`
    and `ClassifierError` both carry it, set where the cause was known. An
    exception with no such flag is treated as permanent: those are code or data
    defects, and a retry reproduces them while consuming the budget.
    """
    from datetime import UTC, datetime, timedelta

    from linuxdo_oss.domain.timestamps import to_iso_utc
    from linuxdo_oss.persistence import write_repository as repo

    try:
        now_dt = datetime.now(UTC)
        attempts = await repo.topic_attempts(db, topic_id)
        retryable = bool(getattr(error, "retryable", False))

        # `attempts` counts claims, and this topic's own claim is already in it, so
        # the budget is spent when it reaches MAX_ATTEMPTS.
        if not retryable or attempts >= MAX_ATTEMPTS:
            retry_after = None
        else:
            backoff = RETRY_BASE_SECONDS * 2 ** max(attempts - 1, 0)
            retry_after = to_iso_utc(now_dt + timedelta(seconds=backoff))

        await repo.mark_failed(
            db,
            topic_id,
            now=to_iso_utc(now_dt),
            retry_after=retry_after,
            error=_failure_label(error),
        )
    except Exception:
        # The topic keeps its claim lease and is reclaimed once the lease expires,
        # so nothing is lost permanently — but the run must not die over it.
        logger.exception("could not record failure for topic %s", topic_id)


async def _process_topic(
    db: Any, settings: Settings, fetch_text: FetchTextPort, topic_id: int
) -> bool:
    """Classify and settle one claimed topic. True when it published.

    No network read happens here any more. The text was stored when the tag feed
    announced the topic, so this reads it back from D1 — including for a topic
    being retried long after it fell out of the feed's thirty-item window, which is
    exactly the case an in-memory hand-off could not serve.

    The return value feeds `published_count`, which counts topics rather than
    projects so all four numbers on a `sync_runs` row share one unit.

    Two settlings are `not_relevant` rather than `published`, and both are the
    PRD's rule that only `include` decisions are published:

      * no repository candidate in the stored text — settled WITHOUT an LLM call,
        which is where most of the subrequest budget is saved;
      * candidates existed but every decision came back `exclude`/`uncertain`.

    The second one matters more than it looks. `read_queries.py` exposes topics by
    `status = 'published'` alone, so publishing a topic with zero mentions would
    put an empty card in the public list.
    """
    from datetime import UTC, datetime

    from linuxdo_oss.classifier import classify, publishable_decisions
    from linuxdo_oss.domain.timestamps import to_iso_utc
    from linuxdo_oss.persistence import write_repository as repo

    posts = await repo.load_stored_posts(db, topic_id)

    if not posts:
        # Unreachable through this module: a topic row and its posts are written by
        # one batch, and D1 batches are all-or-nothing. Raised rather than settled
        # as `not_relevant` precisely because it means the invariant broke —
        # `not_relevant` is terminal and would bury the inconsistency as a result.
        raise repo.WriteError(f"topic {topic_id} has no stored posts")

    now = to_iso_utc(datetime.now(UTC))

    candidates = _candidates_of(posts)

    if not candidates:
        await repo.mark_not_relevant(db, topic_id, now=now)
        return False

    decisions = await classify(
        fetch_text,
        settings=settings,
        topic_text=POST_SEPARATOR.join(post.text for post in posts),
        candidates=candidates,
    )

    included = publishable_decisions(decisions)

    if not included:
        await repo.mark_not_relevant(db, topic_id, now=now)
        return False

    projects, mentions = _publishable_rows(included, candidates)

    await repo.publish_topic_result(
        db,
        topic_id,
        projects=projects,
        mentions=mentions,
        now=now,
        prompt_version=settings.llm_prompt_version,
    )

    return True


def _discovered_topic(topic: TopicFeed) -> DiscoveredTopic:
    """Translate one parsed topic into the write layer's record.

    The conversion lives here, in the orchestrator, so that `persistence/` never
    imports `feeds/`: the write layer is told what to store and stays ignorant of
    where rows come from.
    """
    from linuxdo_oss.persistence.write_repository import DiscoveredTopic, PostRow

    return DiscoveredTopic(
        topic_id=topic.topic_id,
        canonical_url=topic.canonical_url,
        title=topic.title,
        author=topic.author,
        published_at=topic.published_at,
        posts=[
            PostRow(
                post_number=post.post_number,
                source_url=post.source_url,
                cleaned_text=post.text,
                is_first_post=post.is_first_post,
                guid=post.guid,
                author=post.author,
                published_at=post.published_at,
            )
            for post in topic.posts
        ],
    )


def _candidates_of(posts: Sequence[StoredPost]) -> list[CandidateRepository]:
    """Every repository the stored posts link, tagged with the post that did it.

    One entry per (post, repository) pair, so the same repository linked twice
    appears twice. That is what `classifier.py` expects — it derives the allowlist
    through `_distinct_canonical_urls` — and it is what lets one `include` decision
    become one `project_mentions` row per citing post.

    The list is NOT truncated at `MAX_CANDIDATES`. `classify` refuses an oversized
    batch as a permanent failure, and silently dropping candidates here would
    publish a topic while hiding that some of its projects were never judged.
    """
    from linuxdo_oss.classifier import CandidateRepository
    from linuxdo_oss.domain.github_url import extract_repository_candidates

    return [
        CandidateRepository(
            canonical_url=candidate.canonical_url,
            owner=candidate.owner,
            repo=candidate.repo,
            evidence_url=candidate.evidence_url,
            post_number=post.post_number,
        )
        for post in posts
        for candidate in extract_repository_candidates(post.text)
    ]


def _publishable_rows(
    included: list[Decision], candidates: list[CandidateRepository]
) -> tuple[list[ProjectUpsert], list[MentionInsert]]:
    """Turn `include` decisions into project rows and their provenance rows.

    One project per canonical URL — that is the global identity — and one mention
    per post that cited it, which is the PRD's "same project across multiple
    topics creates one project row and multiple source mentions", applied within a
    topic as well.

    `owner` and `repo` come from the candidate rather than from the decision: the
    model is never the source of an identity. Every decision is guaranteed to have
    a matching candidate because `classifier._reject_unknown_and_duplicate` fails
    validation for a URL outside the allowlist.
    """
    from linuxdo_oss.persistence.write_repository import MentionInsert, ProjectUpsert, WriteError

    by_url: dict[str, list[CandidateRepository]] = {}
    for candidate in candidates:
        by_url.setdefault(candidate.canonical_url, []).append(candidate)

    projects: list[ProjectUpsert] = []
    mentions: list[MentionInsert] = []

    for decision in included:
        citing = by_url.get(decision.canonical_url)
        if not citing:
            # Unreachable: the allowlist re-check already fails the whole response
            # for a URL no candidate carried. Raised rather than skipped because
            # skipping would publish a subset of the judged projects with nothing
            # recording that the rest vanished — the untraceable published data
            # `spec/backend/error-handling.md` rates worse than a failure.
            # `WriteError` is what `publish_topic_result` raises for the same class
            # of dangling reference, and it is permanent: a retry reproduces it.
            raise WriteError(
                f"decision names {decision.canonical_url!r}, which no retained post cited"
            )

        # `Decision`'s validator rejects an `include` missing either field, so
        # neither is ever empty. Read into locals so the type narrows here.
        display_name = decision.display_name or ""
        summary = decision.summary or ""

        projects.append(
            ProjectUpsert(
                canonical_url=decision.canonical_url,
                owner=citing[0].owner,
                repo=citing[0].repo,
                display_name=display_name,
                summary=summary,
            )
        )

        mentions.extend(
            MentionInsert(
                post_number=candidate.post_number,
                canonical_url=decision.canonical_url,
                evidence_url=candidate.evidence_url,
                evidence_excerpt=decision.evidence_excerpt,
                confidence=decision.confidence,
            )
            for candidate in citing
        )

    return projects, mentions
