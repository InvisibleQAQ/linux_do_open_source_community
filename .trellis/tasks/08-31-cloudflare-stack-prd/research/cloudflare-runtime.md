# Cloudflare Runtime Research

Verified 2026-08-31 against official docs and the `cloudflare/workers-py` /
`cloudflare/workerd` sources. Supersedes the earlier draft, which left the Python
version and the beta status as open questions.

## Sources

- [Python Workers](https://developers.cloudflare.com/workers/languages/python/)
- [Python Workers basics](https://developers.cloudflare.com/workers/languages/python/basics/)
- [FastAPI on Python Workers](https://developers.cloudflare.com/workers/languages/python/packages/fastapi/)
- [Python packages / pywrangler](https://developers.cloudflare.com/workers/languages/python/packages/)
- [Cron Triggers](https://developers.cloudflare.com/workers/configuration/cron-triggers/)
- [Workers limits](https://developers.cloudflare.com/workers/platform/limits/)
- [Compatibility flags](https://developers.cloudflare.com/workers/configuration/compatibility-flags/)
- [Static assets: SPA routing](https://developers.cloudflare.com/workers/static-assets/routing/single-page-application/)
- `cloudflare/workers-py` — `packages/cli/src/pywrangler/{metadata,sync,resolve,utils}.py`
- `cloudflare/workerd` — `src/workerd/io/compatibility-date.capnp`

## Runtime and toolchain

- Python Workers are in **open beta**. The `python_workers` compatibility flag has
  no `compatEnableDate`, so it must stay declared explicitly.
- **The Python version is chosen by `compatibility_date` + flags**, and this is the
  single most dangerous configuration in the project:
  | Configuration | Runtime |
  |---|---|
  | `python_workers` alone | CPython 3.12 |
  | + date >= 2025-09-29 | CPython 3.13, Pyodide 0.28.1 — **current** |
  | + date >= 2026-09-08 | CPython 3.14, Pyodide 314.0.6, new wheel ABI |
  `compatibility_date` is frozen at `2026-08-31` with a comment in
  `wrangler.jsonc`. Bumping it past 2026-09-08 re-resolves every wasm dependency.
- The CLI is **`pywrangler`**, from the PyPI package `workers-py`. Its only own
  subcommands are `sync` and `types`; everything else is proxied to
  `npx wrangler`. Minimum tool versions are enforced: **uv >= 0.12.3**,
  wrangler >= 4.127.1.
- Project files: `pyproject.toml` (searched upward from cwd) plus a wrangler
  config. **`requirements.txt` is rejected** — `check_requirements_txt()` exits 1
  if present.
- Generated artifacts: `pylock.toml` (**commit it** — the only reproducible pin of
  the wasm wheel set), `python_modules/`, `.venv-workers/` (both gitignored).
- Cold start: ~10 s without memory snapshots, ~1 s with them (applied
  automatically). The Worker startup limit is 1 s, so global-scope imports are
  right at the ceiling — keep them minimal.

## Handlers

- The entrypoint class **must** be named `Default` and extend `WorkerEntrypoint`.
  Module-level `on_fetch` / `on_scheduled` have been disabled by default since
  2025-08-14.
- `asgi.entrypoint(app)` returns a `WorkerEntrypoint` subclass whose **only** method
  is `fetch`. A Worker needing both FastAPI and cron must carry both handlers
  itself, which is what `backend/src/main.py` does.
- Signatures differ and both forms are mandatory: `fetch(self, request)`,
  `scheduled(self, controller, env, ctx)`.
- `env` is **attribute-access only** — `env["KEY"]` raises. Inside a FastAPI route
  there is no `self.env`; the ASGI adapter injects it as `request.scope["env"]`.
- `main`'s directory is the import root; `src/` itself is not on `sys.path`.

## Packages

- Resolution runs `uv pip compile` against `https://index.pyodide.org/0.28.3` with
  the `cpython-3.13.2-emscripten-wasm32-musl` interpreter. Pure-Python and
  PyEmscripten wheels work; native extensions generally do not.
- `fastapi` and `pydantic` are supported; **pydantic is pinned to 2.10.6**, the
  wasm ceiling on the 3.13 target. An unpinned `pydantic` makes uv backtrack and
  emit confusing errors.
- `httpx` and `aiohttp` are named as supported, but `httpx`'s sync client blocks
  the isolate. **This project uses `from workers import fetch`** and does not carry
  httpx at all — one less module-level import against the 1 s startup limit.
- Standard library `xml.etree.ElementTree` and `html.parser` are available and are
  the intended parsers. `defusedxml`, `feedparser`, `lxml` and `beautifulsoup4`
  are all either unverified on this runtime or heavier than the job needs.
- `threading` and `multiprocessing` import but do not function. Concurrency is
  `asyncio.gather` + `asyncio.Semaphore`.

## Cron

- Config is `triggers.crons`, an array. `*/5 * * * *` is valid; the minimum
  interval is 1 minute. Times are UTC.
- **On deploy, `triggers.crons` replaces all existing triggers.** `"crons": []`
  removes them; commenting the key out leaves the deployed triggers in place.
- Config changes take up to 15 minutes to propagate.
- Local trigger: `curl "http://localhost:8787/cdn-cgi/handler/scheduled"`.
- Weekday field is 1=Sunday..7=Saturday, unlike standard cron.

## Limits that shape the design

CPU time and wall clock are **separate**; waiting on `fetch()` or D1 does not count
toward CPU.

| Limit | Free | Paid |
|---|---|---|
| CPU per Cron Trigger | 10 ms | 30 s (interval < 1 h) |
| Cron wall clock | 15 min | 15 min |
| Subrequests per invocation | 50 | 10,000 |
| Connections awaiting response headers | 6 | 6 |
| Worker size (gzip) | 3 MB | 10 MB |
| Startup time | 1 s | 1 s |

**D1 queries count as subrequests.** 20 topics × (2 RSS + 1 LLM + writes) does not
fit in 50, and 10 ms of cron CPU is not enough to parse RSS. **The PRD's design
requires Workers Paid.**

## D1

- Binding declared under `d1_databases` with `binding` / `database_name` /
  `database_id`; `binding`'s value is the Python attribute name (`env.DB`).
- Call chain: `await db.prepare(sql).bind(*params).all()` — pass a Python tuple and
  splat it; do not reach for `pyodide.ffi.to_js`. Rows are `JsProxy`; convert with
  `.to_py()`. Use `.first()` for one row (returns None when absent) and read
  `.meta` from `.run()`.
- **No interactive transactions.** `batch()` is the only rollback boundary.
- At most **100 bound parameters** per statement.
- Migrations: `wrangler d1 migrations create/apply`, with `migrations_dir` and
  `migrations_table` declared in the config.
- Never hand a `JsProxy` to a serializer: Cloudflare's own `query-d1` example does
  and is marked `xfail`.

## Deployment topology

- Cloudflare steers new projects to Workers over Pages ("all of our investment ...
  dedicated to improving Workers").
- One Python Worker can serve static assets **and** FastAPI **and** cron. Only one
  asset collection per Worker.
- **`@cloudflare/vite-plugin` cannot be used**: it resolves only
  `.js/.mjs/.ts/.mts/.jsx/.tsx` entrypoints, so a `.py` `main` is rejected.
- `assets.run_worker_first: ["/api/*"]` keeps every static request off Python,
  which also keeps the ~1 s cold start off ordinary page loads. Array form needs
  Wrangler >= 4.20.0.

## Constraints applied

- Bounded concurrency <= 6, `asyncio` only.
- Keep global-scope imports minimal; lazy-import anything a handler does not always
  need.
- Store all state in D1; the isolate filesystem is ephemeral.
- Every dependency validated against the Pyodide index for the pinned Python
  version before being added.

## Still open

- `[UNKNOWN]` **Bounding `workers.fetch` wall clock.** There is no timeout option.
  `adapters/http.py` stacks `signal=AbortSignal.timeout(ms)` (kwargs are forwarded
  into the JS `RequestInit`, and the API exists in workerd, but no Cloudflare doc or
  example shows it from Python) with an outer `asyncio.wait_for`. The first layer
  must be verified on a **deployed** Worker.
- `[UNKNOWN]` **Deployed-Worker egress to linux.do and RSSHub.** `spikes/egress/`
  exists to answer this and must be run against its deployed URL, not
  `pywrangler dev`, which may egress from the developer's machine and mask a block.
