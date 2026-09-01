"""Worker entrypoint.

`wrangler.jsonc` points `main` at this file. Two rules the runtime enforces and
that are easy to get wrong:

  1. The class MUST be named `Default` and MUST extend `WorkerEntrypoint`.
     Module-level handlers (`on_fetch`, `on_scheduled`) have been disabled by
     default since 2025-08-14.
  2. `asgi.entrypoint(app)` alone is NOT enough here. It returns a
     WorkerEntrypoint subclass whose only method is `fetch`, so a Worker that
     also needs a Cron Trigger must carry both handlers itself — as below.

Signatures differ between the two handlers and both forms are mandatory:
`fetch(self, request)` takes only the request, while
`scheduled(self, controller, env, ctx)` takes all four parameters.

Imports here are ROOT-RELATIVE to this file's directory (`backend/src/`), which
is why it is `from linuxdo_oss...` and never `from src.linuxdo_oss...`.

Keep this module's imports minimal. Everything imported at module scope runs at
deploy time and is baked into the memory snapshot, and the Worker startup limit
is 1 second.
"""

from workers import WorkerEntrypoint, asgi

from linuxdo_oss.api.app import app
from linuxdo_oss.sync import run_sync


class Default(WorkerEntrypoint):
    async def fetch(self, request):
        """Serve /api/*.

        Only worker-first paths reach here: `assets.run_worker_first` in
        wrangler.jsonc routes everything else to the static asset router, so
        ordinary page loads never pay Python's cold start.
        """
        return await asgi.fetch(app, request, self.env, self.ctx)

    async def scheduled(self, controller, env, ctx):
        """One bounded sync run, every 5 minutes.

        `env` arrives as a parameter here — there is no request and therefore no
        ASGI scope to read it from. That is why `run_sync` takes `env` explicitly
        instead of reaching for a global.
        """
        await run_sync(env)
