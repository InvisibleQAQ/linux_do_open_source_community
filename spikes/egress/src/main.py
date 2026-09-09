"""Egress connectivity spike — a release prerequisite, not an optional check.

It is `[UNKNOWN]` whether linux.do accepts Cloudflare Worker egress. Since
ADR 0007 the tag feed is the pipeline's ONLY input, so if linux.do rejects
Worker egress, every line of ingestion code downstream is void. This runs FIRST.

Two URLs are probed, and the SECOND one is what makes the result readable:

  * the real tag feed on linux.do — the thing that must work;
  * the same kind of feed on `meta.discourse.org`, a Discourse instance with no
    Cloudflare challenge in front of it.

Without the control probe a failure is ambiguous. Both failing points at Worker
egress or at this spike itself; only the first failing means linux.do is
specifically refusing Cloudflare's network, which is the release blocker and is
not something application code can work around.

Deploy it and hit the deployed URL:

    cd spikes/egress
    uv run pywrangler deploy
    curl https://<worker>.workers.dev/

Do NOT judge the result from `pywrangler dev`. A local dev server may egress from
the developer's machine and happily return 200 for an origin that blocks
Cloudflare's network — which would mask exactly the failure this spike exists to
find.

Read the `verdict` field; `results.target` and `results.control` carry the
evidence behind it. A `Just a moment...` body preview with status 403 on the
target is the challenge page this spike exists to detect.
"""

import json

from workers import Response, WorkerEntrypoint, fetch

# Keep in sync with `TAG_FEED_URL` in the root wrangler.jsonc.
TAG_FEED_URL = "https://linux.do/tag/2234-tag/2234.rss"

# The control. Reachable from an ordinary network (verified 2026-09-08: HTTP 200,
# application/rss+xml, 81,541 bytes), so a failure HERE means the problem is not
# linux.do.
CONTROL_FEED_URL = "https://meta.discourse.org/tag/rss.rss"


async def probe(url: str) -> dict:
    """Fetch one URL and report what came back, never raising."""
    try:
        response = await fetch(url, method="GET", headers={"Accept": "application/rss+xml, */*"})
    except Exception as error:
        return {"url": url, "ok": False, "error": type(error).__name__, "detail": str(error)[:200]}

    try:
        body = await response.text()
    except Exception as error:
        body = ""
        read_error = type(error).__name__
    else:
        read_error = None

    return {
        "url": url,
        "ok": 200 <= int(response.status) < 300,
        "status": int(response.status),
        "content_type": response.headers.get("content-type"),
        "content_length": response.headers.get("content-length"),
        "cf_ray": response.headers.get("cf-ray"),
        # Enough to recognise a challenge page or an error body, not enough to
        # dump a whole feed into the log.
        "body_preview": body[:200],
        "body_bytes": len(body.encode("utf-8")),
        "read_error": read_error,
    }


class Default(WorkerEntrypoint):
    async def fetch(self, request):
        # Sequential, not gathered: two probes, and a sequential run makes it
        # obvious which origin produced which result if one hangs.
        target = await probe(TAG_FEED_URL)
        control = await probe(CONTROL_FEED_URL)

        payload = {
            "all_ok": target["ok"] and control["ok"],
            # The whole point of the control probe: name the conclusion rather
            # than leaving the reader to infer it from two status codes.
            "verdict": (
                "tag feed reachable from Worker egress"
                if target["ok"]
                else "linux.do refuses Cloudflare Worker egress — release blocker"
                if control["ok"]
                else "both failed: suspect Worker egress or this spike, not linux.do"
            ),
            "results": {"target": target, "control": control},
        }

        return Response(
            json.dumps(payload, ensure_ascii=False, indent=2),
            headers={"content-type": "application/json; charset=utf-8"},
        )
