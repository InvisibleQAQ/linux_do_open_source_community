# Domain Context

## Glossary

### Channel Feed

The configured RSSHub Telegram feed used only to discover Linux.do topic URLs.

### Topic

A Linux.do discussion identified by its numeric topic ID. A topic is the primary grouping shown in the frontend.

### Topic Post

One RSS item inside a Linux.do topic feed. The first post is always retained; a reply is retained only when it contains an explicit GitHub URL.

### Project

A globally deduplicated GitHub repository identified by canonical `https://github.com/<owner>/<repo>` URL.

### Project Mention

The evidence-bearing relationship between a Project and the Topic Post that referenced it. It preserves the original GitHub URL and source context.

### Published Project

A Project candidate that the LLM classified as `include` using only an explicit repository URL found in the source post and whose structured output passed validation.

### Responses-Compatible Endpoint

An HTTPS API root configured by `LLM_BASE_URL` whose `/responses` endpoint implements the OpenAI Responses API request/output shape and strict JSON Schema Structured Outputs. Compatibility is a required capability, not a provider-name assumption.

### Sync Run

One Cron-triggered ingestion attempt that claims at most 20 due topics and records bounded processing results.

### Single Worker

The whole application is one Cloudflare Worker: static assets, the read API and the 5-minute cron live in the same deployment, described by the root `wrangler.jsonc`. There is no separate frontend deployment and no Cloudflare Pages project. See `docs/adr/0002-single-worker-topology.md`.

### Worker-First Path

A path matched by `assets.run_worker_first` and therefore routed to Python instead of the static asset router. The MVP has exactly one: `/api/*`. Every other path is served by the edge asset router with no Worker invocation.

### API Contract

The single coupling surface between frontend and backend: `frontend/src/api/types.ts` and the backend Pydantic response models. Field names are snake_case on both sides — there is no camelCase conversion layer. Changing one side without the other is a defect.

### Cursor

The opaque pagination token returned as `next_cursor`. `next_cursor === null` is the ONLY end-of-list signal; a full page may still be the last page.

### Whitelist Port

The adoption strategy for the Minimal v7.7.0 design system: copy only `theme/`, `layouts/core/` and an enumerated set of low-coupling components, and write all business code fresh. Distinguished from copying the template wholesale. See `docs/adr/0004-minimal-template-whitelist-port.md`.

### Four States

The loading / empty / error / partial-data set that every data surface must render explicitly, per the PRD. Implemented in `frontend/src/components/states/`. "Partial data" means data is on screen while a background refetch runs — distinct from the first load, and the reason `isPending` and `isFetching` must not be conflated.
