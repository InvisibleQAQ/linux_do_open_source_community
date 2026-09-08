# Logging Guidelines

`print()` and the `logging` module both work and both surface through
`observability.enabled: true` in `wrangler.jsonc`. Use `logging`.

```python
import logging
logger = logging.getLogger(__name__)
```

---

## What may be logged

Identifiers and bounded categories:

- `topic_id`, `run_id`, `project_id`
- an exception **class name** (`type(error).__name__`)
- counts, durations, statuses
- a bounded, category-level error summary — see `RunCounters.summary()`

```python
logger.warning("topic %s failed: %s", topic_id, type(error).__name__)
```

Lazy `%s` formatting, not f-strings: the arguments are not rendered when the level
is disabled.

---

## What must never be logged

| Never | Why |
|-------|-----|
| `env.LLM_API_KEY`, or any value from `env` that could be a secret | It ends up in a log aggregator and stays there |
| A full LLM request or response payload | Unbounded, and it can contain arbitrary post content |
| Full RSS or HTTP response bodies | Unbounded |
| A raw exception message from an external service | May embed a URL with credentials |

The rule applied to configuration errors: `run_sync` logs
"sync aborted: invalid configuration" — but with `logger.exception`, so the
message `config.py` raised is in the log too. That makes the message a log line,
and it is bounded accordingly:

| May appear | Must not |
|------------|----------|
| the offending variable's NAME | its value, for anything that could be a secret |
| the accepted values of an enum | `LLM_BASE_URL`, `LLM_MODEL`, `LLM_API_KEY`, ever |
| the base-URL rule that was broken, and the suffix that broke it | the base URL itself — a gateway root can carry a key in a query string |
| the rejected spelling of `LLM_PROTOCOL` / `LLM_SCHEMA_MODE`, truncated | anything longer or free-form |

The last row is the one exception and it is deliberate: those two variables hold a
fixed enum an operator typed, a hyphen-for-underscore typo is otherwise invisible,
and `test_config.py` asserts the spelling is named. Widening any message in
`config.py` widens this log line, so a new value in one is a decision about both.

---

## Retained diagnostic output

The PRD allows keeping raw LLM output **only** when required to diagnose a failure,
and only with a bounded size. If it is retained:

- it goes in a database column, not a log line
- it passes through `clamp_text()` first
- validated structured output remains authoritative; the raw copy is never read
  back as data

---

## Levels

| Level | Use |
|-------|-----|
| `exception` | Only at the cron boundary and other places where a traceback is the point |
| `warning` | A single topic failed; the run continues |
| `info` | Run opened/closed, counts, claim batch size |
| `debug` | Off in production; never leave a payload dump behind one |

A per-topic failure is a `warning`, not an `error`: it is an expected, isolated
outcome that the state machine handles. Reserve louder levels for something that
needs a human.
