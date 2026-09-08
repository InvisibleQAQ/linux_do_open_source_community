# RSS Pipeline Findings

## Verified source shape (2026-08-31, real HTTP response)

Fetched `https://rsshub.rssforever.com/telegram/channel/linux_do_channel` and read the body.

- HTTP 200, `Content-Type: application/xml; charset=utf-8`, ~20 KB, 20 items,
  `ttl 5`, `cache-control: public, max-age=300`.
- **RSS 2.0.** An `<item>` carries exactly: `title`, `description`, `link`,
  `guid isPermaLink="false"`, `pubDate`. There is **no** `content:encoded` and
  **no** `author` / `dc:creator` element.
- `<link>` points at **Telegram** (`https://t.me/linux_do_channel/492492`), not at
  linux.do.
- The linux.do topic URL lives inside the HTML-escaped `<description>`, as
  `<a href="https://linux.do/t/topic/2837720/1" target="_blank" rel="noopener"
  onclick="return confirm(...)">` — **with a floor suffix**.
- `<title>` reads `@Ammdjs 在 <topic title> 中发帖` or `linmao 在 <topic title> 中发帖`.
  The author is recoverable from it but the format is fragile.
- `pubDate` is RFC 822 in GMT: `Mon, 31 Aug 2026 15:18:11 GMT`.
- The description HTML carries `onclick` attributes. It is hostile input.

Every topic URL observed in that snapshot had the `/1` floor suffix, and all 20
ids were distinct.

## Design consequences

- Topic discovery **cannot** rely on the RSS `link` field. All text-bearing fields
  must be scanned, and the `description` HTML parsed structurally for `href`.
- The topic id is the stable discovery identity. Telegram item URLs are not the key.
- **A URL found in the feed is never used as a fetch target.** Only the numeric id
  is extracted and validated; the fetch URL is then *constructed* from it. This is
  the PRD's SSRF rule, and building it this way makes the rule structural rather
  than a check someone can forget.
- RSS HTML is converted to text on the backend and never rendered as HTML.

## Topic feed access

- The URL form is `https://linux.do/t/topic/<id>.rss` (confirmed by the project
  owner).
- **It serves public content only.** A permission-gated topic is not accessible.
  That is acceptable for the MVP — but it means a non-200 on a topic feed is a
  **normal outcome**, not necessarily a defect, and the topic reader must classify
  it as such rather than treating every failure as retryable.

## Egress spike — what was and was not established

The spike Worker (`spikes/egress/`) was run through `pywrangler dev`.

**Established:** RSSHub is reachable from a Python Worker's `workers.fetch` —
HTTP 200, `application/xml`, 21,731 bytes of real RSS. The `from workers import
fetch` call path, the `Default(WorkerEntrypoint)` entrypoint shape and
`bundle → workerd` all work.

Incidental but useful: the first two requests took **27.6 s** and **25.0 s**, then
551 ms and 832 ms warm. That is the documented Python Worker cold start
(~10 s without memory snapshots; local dev has none) plus two sequential fetches.
It is the concrete reason `assets.run_worker_first` is scoped to `/api/*` — keeping
static page loads off Python.

**NOT established:** whether linux.do accepts Cloudflare Worker egress.

The local run returned 403 with a Cloudflare `Just a moment...` challenge page for
`https://linux.do/t/topic/<id>.rss`. That result is **not evidence about Worker
egress**, because `pywrangler dev` egresses from the developer's machine, and this
machine's IP is being challenged: three separate User-Agent variants (browser,
absent, curl default) all returned the same 403, and an earlier direct `curl` to
RSSHub from the same network timed out. The failure is network-level, not
header-level.

`[UNKNOWN]` — **the deployed-Worker egress check has not been run.** The project
owner states linux.do RSS is reachable from their environment, and the pipeline is
being built on that basis. Closing this properly requires deploying the spike and
hitting the deployed URL. There is no Cloudflare login on this machine; wrangler
itself offers `wrangler deploy --temporary`, which deploys to a temporary preview
account with no login, so the check does not need account access:

```bash
cd spikes/egress
uv run pywrangler deploy --temporary
curl https://<returned-url>/
```

Interpretation: `all_ok: true` closes the PRD's release prerequisite. A 403 with a
`Just a moment...` body from the *deployed* Worker would mean Cloudflare Worker
egress is genuinely challenged by linux.do — a blocker that no amount of
application code can fix, and which would need a different fetch path.

## Toolchain note

`pywrangler` 1.17.1 enforces **uv >= 0.12.3**. This machine had uv 0.11.16
installed via pip into the miniconda base environment, so `uv self update` refused
(standalone-installer only); `python -m pip install --upgrade uv` took it to
0.12.8, which works.
