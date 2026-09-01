"""Egress connectivity spike — a release prerequisite, not an optional check.

The PRD flags it as `[UNKNOWN]` whether Linux.do and RSSHub accept Cloudflare
Worker egress. If either rejects it, every line of ingestion code downstream is
void, so this runs FIRST.

Deploy it and hit the deployed URL:

    cd spikes/egress
    uv run pywrangler deploy
    curl https://<worker>.workers.dev/

Do NOT judge the result from `pywrangler dev`. A local dev server may egress from
the developer's machine and happily return 200 for an origin that blocks
Cloudflare's network — which would mask exactly the failure this spike exists to
find.

What to look for in the JSON:
  * status 200 on both, with a sensible content-length -> proceed.
  * 403 / 429 / an HTML challenge page -> the PRD's release blocker is real.
    Report the status and the body preview before writing any reader code.
"""

import json

from workers import Response, WorkerEntrypoint, fetch

CHANNEL_FEED_URL = "https://rsshub.rssforever.com/telegram/channel/linux_do_channel"

# Any real topic id works. Replace if this one is deleted.
TOPIC_FEED_URL = "https://linux.do/t/topic/2837720.rss"


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
        results = [await probe(CHANNEL_FEED_URL), await probe(TOPIC_FEED_URL)]

        payload = {
            "all_ok": all(result["ok"] for result in results),
            "results": results,
        }

        return Response(
            json.dumps(payload, ensure_ascii=False, indent=2),
            headers={"content-type": "application/json; charset=utf-8"},
        )
