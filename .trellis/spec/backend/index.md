# Backend Development Guidelines

Cloudflare Python Worker: FastAPI read API + a 5-minute cron sync.

**Read `backend/CLAUDE.md` first.** It carries the ten runtime rules that will bite
you after deploy if you get them wrong (entrypoint class name, `env` attribute-only
access, no `self.env` in ASGI routes, the frozen `compatibility_date`, the
`requirements.txt` trap, and so on). These guideline files assume you have read it
and do not repeat it.

---

## Guidelines Index

| Guide | Covers |
|-------|--------|
| [Directory Structure](./directory-structure.md) | Layering, the boundaries you must not cross |
| [Environment Configuration](./environment-configuration.md) | Local and production LLM variable ownership |
| [Database Guidelines](./database-guidelines.md) | D1 without transactions, parameter/byte limits, migrations |
| [Error Handling](./error-handling.md) | Retryable vs permanent, per-topic isolation, the cron boundary |
| [Quality Guidelines](./quality-guidelines.md) | Commands, forbidden patterns, the testing bar |
| [Logging Guidelines](./logging-guidelines.md) | What may be logged, and what must never be |

Cross-cutting decisions are in `docs/adr/`:

- `0002` single-Worker topology — why one Worker, why no `@cloudflare/vite-plugin`, why no CORS

---

## The three things that decide everything else

1. **The runtime is a wasm sandbox, not a server.** No threads, no persistent
   filesystem, 1 s startup, 30 s cron CPU, at most 6 connections waiting on
   response headers.
2. **D1 has no interactive transactions.** Atomicity is `batch()` plus unique
   constraints plus idempotent upserts. Every design decision in the persistence
   layer follows from this.
3. **Anything that touches the runtime cannot be unit-tested.** So the pure
   domain is kept strictly separate and carries the logic worth testing; the
   runtime layer stays thin enough to verify with a handful of black-box tests.

---

**Language**: guideline documents in Chinese or English as suits the reader; code,
identifiers and code comments in English.
