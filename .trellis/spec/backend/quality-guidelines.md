# Quality Guidelines

---

## Before you commit

```bash
uv run pytest backend/tests
uv run ruff check backend/
uv run ruff format --check backend/
```

Baseline at skeleton completion (2026-08-31): 177 tests in 0.54 s, ruff clean, all
23 files formatted.

`uv run pytest backend/tests -m "not worker"` skips anything that spawns a
`pywrangler dev` subprocess. Use it while iterating.

---

## Forbidden

| Pattern | Why |
|---------|-----|
| A runtime import (`workers`, `js`, `pyodide`) anywhere in `domain/`'s import graph | Makes the whole pure suite unimportable on CPython. Import inside the function instead — see `adapters/http.py::_abort_signal`. |
| A `JsProxy` escaping `persistence/d1.py` | Cloudflare's own example that does this is marked `xfail`. Convert with `.to_py()`. |
| `env["KEY"]` | The env wrapper has no `__getitem__`. Attribute access only. |
| `self.env` inside a FastAPI route | Does not exist in ASGI. Use `api/deps.py`. |
| Inline SQL in a route or in the orchestrator | Untestable without a Worker. Module constant, imported by tests. |
| `threading`, `multiprocessing` | Import but do not work in the wasm VM. `asyncio.gather` + `asyncio.Semaphore`. |
| Concurrency above 6 | Hard platform ceiling on connections waiting for response headers. |
| Creating `requirements.txt` | `pywrangler` aborts with exit 1 before doing any work. |
| A module-level import used by only one branch | Executed at deploy time and baked into the memory snapshot, against a 1 s startup limit. |
| `fetch()` at module scope | Throws outside a handler. |
| Read-then-write on D1 | No transaction to protect it. Conditional UPDATE + `meta.changes`. |
| Hand-formatting a timestamp | `domain/timestamps.py` is the only implementation. |
| Bumping `compatibility_date` as routine hygiene | Past 2026-09-08 it silently switches CPython and the wasm ABI. |
| Relying on the filesystem for state | Ephemeral per isolate. |

---

## Testing bar

Two layers, and the split is forced by there being no official Python Workers test
harness (`@cloudflare/vitest-pool-workers` is JS/TS only).

**Pure layer — where the logic lives.** Plain CPython pytest. Because D1 is SQLite,
the real migration files are applied to an in-memory `sqlite3` database and the
guarantees are asserted directly: constraint-based idempotency, the claim lease
under contention, keyset pagination, the published-only filter.

Rules:

- Tests **import the SQL constants from the code** (`from linuxdo_oss.sync import
  CLAIM_TOPIC_SQL`) rather than restating them. A copied query drifts and the test
  starts protecting nothing.
- Assert externally observable behavior — returned rows, `changes()`, the resulting
  model — never that a private helper was called.
- Table-driven for anything with a large input space. The canonicalizer has ~115
  cases across accepted roots, subpaths, case variants, rejected non-repository
  paths and deceptive hosts, because a false accept there becomes bad published
  data.
- A new migration ships with the tests that prove what it guarantees.

**Worker layer — `@pytest.mark.worker`, not yet written.** Spawns `pywrangler dev`
as a subprocess and asserts over HTTP with `requests`. Reserve it for what genuinely
needs the runtime: JsProxy conversion, `batch()` atomicity, ASGI-and-cron
coexistence, and the end-to-end fixture-backed sync.

---

## Comments

Comment the **why**. The load-bearing comments in this codebase all explain a
decision a reader would otherwise reverse:

- why `asgi.entrypoint(app)` is not used (`main.py`)
- why `pydantic` is pinned to 2.10.6 (`pyproject.toml`)
- why `Generic[T]` instead of PEP 695 (`api/schemas.py`)
- why the URL body regex is an allowlist and not an exclusion set
  (`domain/github_url.py`)
- why the cursor validates its timestamp half (`persistence/read_queries.py`)
- why `compatibility_date` is frozen (`wrangler.jsonc`)

If a reviewer would ask "why is it done this way?", answer it in place.

---

## Dependencies

Adding one costs deploy-time snapshot size and startup time, and it must exist as a
pure or PyEmscripten wasm wheel.

Before adding: check it resolves on the Pyodide index for the pinned Python
version, and prefer the standard library. `xml.etree.ElementTree` and `html.parser`
are available and are the intended parsers — `defusedxml`, `feedparser`, `lxml` and
`beautifulsoup4` are all either unverified on this runtime or heavier than the job
needs.

`httpx` is deliberately absent: `from workers import fetch` covers every outbound
call through `adapters/http.py`.
