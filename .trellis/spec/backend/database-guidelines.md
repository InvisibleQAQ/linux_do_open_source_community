# Database Guidelines

Cloudflare D1 (SQLite semantics), reached through `linuxdo_oss/persistence/`.

---

## What D1 does not give you

**No interactive transactions.** Statements auto-commit. `batch()` is the only
rollback boundary. Everything below follows from this one fact.

**At most 100 bound parameters per statement.** Enforced by `d1.py`'s
`MAX_BOUND_PARAMS` and `chunk_rows()`; the helpers raise before D1 does, so a
violation surfaces in a test rather than in production.

**D1 queries count against the per-invocation subrequest budget.** Free plan is 50
total; Paid is 10,000. One query per page, never one per row.

**Row and statement size limits.** `clamp_text()` applies a 100 KB byte budget to
anything arriving from outside, truncating on a UTF-8 character boundary.

---

## Consequences you must follow

### Claiming work is one conditional UPDATE

Never read-then-write — there is no transaction to protect it, and two overlapping
cron runs would both claim the same topic.

```sql
UPDATE topics SET status = 'fetching', lease_expires_at = ?, attempts = attempts + 1
 WHERE topic_id = ? AND (status = 'discovered' OR (status = 'failed' AND retry_after <= ?))
   AND (lease_expires_at IS NULL OR lease_expires_at < ?)
```

Then inspect `meta.changes`: 1 means this run won the row, 0 means another run did.
Verified in `backend/tests/test_sync_sql.py`.

A lease must expire. A run that crashes mid-topic otherwise parks that topic
forever.

### Writes are idempotent by constraint, not by checking first

Every write is `ON CONFLICT ... DO UPDATE` or `DO NOTHING` against a real unique
constraint. "SELECT then INSERT if absent" is a race here.

The constraints that make re-running safe — drop one only by replacing the
guarantee:

| Table | Constraint |
|-------|-----------|
| `topics` | `topic_id` PRIMARY KEY |
| `topic_posts` | `UNIQUE (topic_id, post_number)`, plus a **partial** unique index on `guid WHERE guid IS NOT NULL` |
| `projects` | `UNIQUE (canonical_url)` |
| `project_mentions` | `UNIQUE (topic_id, post_id, project_id)` |

The partial index matters: SQLite treats NULLs as distinct, so a plain
`UNIQUE (guid)` would let rows with no GUID pile up.

### One topic's writes are one `batch()` call

It is the only atomicity available, and it collapses N round trips into one against
the subrequest budget. Cap the statement list yourself — there is no documented
limit — and remember the cron CPU budget covers the whole call.

---

## Timestamps

TEXT, ISO 8601 UTC, milliseconds, trailing `Z`: `2026-08-31T15:18:11.000Z`.

The schema compares timestamps with `<=` and orders by them as TEXT, and keyset
pagination compares them across page boundaries. Those comparisons are only correct
because the format is fixed-width, zero-padded and always UTC. One writer emitting
`2026-8-31T15:18:11Z` or a `+08:00` offset corrupts ordering silently.

So: never format a timestamp by hand. `domain/timestamps.py` is the only
implementation, and `to_iso_utc` refuses a naive datetime rather than assuming UTC.

---

## Pagination

Keyset, never OFFSET. The feed has new topics inserted at its head every five
minutes; OFFSET would skip and repeat rows as the head shifts, and it cannot use
`idx_topics_feed`.

Fetch `page_size + 1` rows: the extra row's presence is what says another page
exists. **Never infer that from `len(rows) == page_size`** — a full page can be the
last one, and there is a test asserting exactly that.

The cursor is opaque (base64) so clients cannot construct one to probe unpublished
rows, and its timestamp half is validated against the canonical format on decode —
without that, a token decoding to `"||1"` yields a cursor whose `published_at` is
`"|"`, which then compares as TEXT against real timestamps with silently wrong
results. A malformed cursor degrades to page one rather than erroring.

---

## Indexes

Every unindexed scan on D1 is billed as rows read. Two indexes are load-bearing:

- `idx_topics_feed` — the newest-first published feed
- `idx_topics_claim` — the due-topics claim query

Adding a query pattern means checking an index covers it.

---

## Migrations

Versioned SQL in `migrations/`, sequential numeric prefix, applied with
`uv run pywrangler d1 migrations apply linuxdo-oss --local|--remote`.
`migrations_dir` and `migrations_table` are declared in `wrangler.jsonc`.

Migrations are forward-only and never edited after being applied anywhere. Add a
new file.

Because D1 is SQLite, the pure test suite applies the real migration files to an
in-memory `sqlite3` database and asserts the constraints — a new migration should
come with the tests that prove what it guarantees.
