# Error Handling

---

## Every failure is one of three kinds

| Kind | Example | What to do |
|------|---------|-----------|
| Retryable | timeout, 5xx, 429, transport failure | record, back off, let a later cron run retry |
| Permanent | 400, malformed XML, schema validation failure | record, do **not** retry immediately |
| Configuration | missing `LLM_API_KEY`, non-HTTPS `LLM_BASE_URL`, unknown `LLM_PROTOCOL` | abort the run, log a category, never log the value |

Classification is carried on the exception, not decided by the caller:
`FetchError.retryable` in `adapters/http.py` is set where the status is known.
`config.py` raises `RuntimeError` for the third kind, and `run_sync` catches it
before doing any work.

**Anything raised out of `config.py` must be a `RuntimeError`.** `run_sync` wraps
`load_settings` in `except RuntimeError` only, so a `ValueError` from a helper —
`llm/protocol.py` raises those — escapes a function that promises never to raise.
`load_settings` converts at its own boundary.

### A 4xx that means "misconfigured" is not a transport failure

`401`, `403` and `404` from the LLM endpoint get their own category,
`endpoint_config`, and the status goes into the message.

The reason is diagnostic, not behavioural — `adapters/http.py` already marks 4xx
non-retryable, so this was never a retry storm. It is that "this endpoint does not
speak the configured `LLM_PROTOCOL`" (404) and "the network blinked" (timeout) used
to render identically in the log, with the status discarded. The two demand
opposite operator responses and only one of them is fixed by waiting.

A status code is a fixed small integer, so naming it leaks nothing. Everything else
about the failure is still reduced to the exception's type name, because a fetch
error's text can carry a URL or a provider body.

---

## The cron boundary swallows everything

`run_sync` never raises. A run that fails must not take down the cron: the next
invocation is five minutes away, and the state machine resumes from whatever the
database says.

That is the **only** place a bare `except Exception` is acceptable at the top
level, and it logs with `logger.exception` so the traceback is not lost.

---

## Per-topic isolation

The PRD requires that one topic's failure not prevent others in the claimed batch
from completing.

Isolation is per task, inside the worker coroutine — not
`gather(return_exceptions=True)`:

```python
async def process(topic_id: int) -> None:
    async with semaphore:
        try:
            if await _process_topic(db, settings, topic_id):
                counters.published += 1
            counters.processed += 1
        except Exception as error:
            logger.warning("topic %s failed: %s", topic_id, type(error).__name__)
            counters.record_failure(topic_id, _failure_label(error))
            await _record_topic_failure(db, topic_id, error)
```

Catching at the task means the failure is counted and categorised while the topic
id is still in scope, and `gather` never sees an exception to propagate.

The PRD says a per-topic failure is *persisted*, not merely counted, so
`_record_topic_failure` runs inside this block — and therefore may not raise. A D1
write that failed there would escape `process` and take the rest of the claimed
batch with it, destroying the guarantee this structure exists to provide. It logs
and returns instead; the topic keeps its lease and is reclaimed when the lease
expires, so nothing is lost permanently.

---

## Retry and backoff

Retries are capped and spread across later cron runs — never a loop inside one
invocation, which would burn the CPU budget and the connection ceiling on a broken
endpoint.

State lives in the database (`attempts`, `retry_after`, `lease_expires_at`), not in
memory: the isolate does not survive between runs.

A permanent validation failure is recorded without a `retry_after`, so it is not
picked up again.

### The numbers

`sync.py` owns them as `MAX_ATTEMPTS = 5`, `RETRY_BASE_SECONDS = 300` and
`LEASE_SECONDS = 900`. `CONTEXT.md` names the two concepts (Retry Budget, Claim
Lease).

| | Value | Why this value |
|---|---|---|
| Attempt cap | 5 claims | Counts *claims*, not failures: `CLAIM_TOPIC_SQL` increments `attempts`, so the topic being processed is already at its own number |
| Backoff | 300s x 2^(attempts-1) → 5/10/20/40 min | Starts at the cron interval because nothing shorter is observable — no run fires in between to pick the topic up |
| Lease | 900s | The Worker wall-clock ceiling. A shorter lease lets the *next* cron run reclaim a topic this run is still processing |

Retryability is read off the exception (`FetchError.retryable`,
`ClassifierError.retryable`), never decided by the caller. An exception carrying no
such flag is treated as permanent: those are code or data defects, and a retry
reproduces them while spending budget.

Both "not retryable" and "budget exhausted" are written the same way — no
`retry_after` at all. `DUE_TOPICS_SQL` compares `retry_after <= ?` and NULL never
satisfies it, so a permanently broken topic stops costing subrequests without
needing a status value of its own.

The backoff is computed in Python, not in SQL. SQLite's `datetime()` emits no
milliseconds, and every timestamp in this schema is compared as TEXT in one ISO
8601 millisecond format — see `database-guidelines.md`.

---

## API errors

Public routes return the smallest true thing:

- 404 for both "never seen" and "not published". The distinction is internal state
  and must not be observable.
- 422 from FastAPI for out-of-bounds pagination or a malformed path parameter —
  validated declaratively at the route, so a bad request never reaches a query.
- Never an internal error payload, a stack trace, or a configuration value.

`/api/health` reports deployment health only. It deliberately does not touch D1: a
health check that queries the database turns a database blip into a
deploy-looks-broken signal.

---

## What not to do

- Do not catch an exception to return a default. A silent default here becomes
  published data that nobody can trace.
- Do not retry a failed LLM request under a weaker `LLM_SCHEMA_MODE`. Degrading is
  a deployment decision, declared in configuration and logged once per run; doing
  it in reaction to a failure is the silent fallback `docs/adr/0005-llm-multi-protocol.md`
  forbids, and it would hide an endpoint that misreported its own capabilities.
- Do not raise a bare `Exception` or `assert` for a runtime condition. Use a typed
  error the caller can classify.
- Do not put a value in an error message that could be a secret or an unbounded
  payload. Error summaries are category-level and byte-bounded — see
  `RunCounters.summary()`.
