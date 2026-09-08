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

### Classifier Endpoint

The single HTTPS API **root** configured by `LLM_BASE_URL` that the classifier talks to. It is a root, never a full endpoint path — the path suffix is decided by the LLM Protocol, not by configuration. Compatibility with the configured protocol is a required capability, never a provider-name assumption.

Supersedes the earlier term "Responses-Compatible Endpoint", which assumed a single protocol. See `docs/adr/0005-llm-multi-protocol.md`.

### LLM Protocol

The wire protocol the Classifier Endpoint is expected to speak, selected by `LLM_PROTOCOL` and never probed at runtime. One of `responses` (OpenAI Responses API, the default), `chat_completions` (OpenAI Chat Completions), `anthropic` (Anthropic Messages API). Each protocol owns its URL suffix, auth header, message shape, schema wrapper and output extraction; the classification prompt and the JSON Schema body are protocol-agnostic and shared.

### Schema Mode

How much of the JSON Schema actually reaches the model, selected by `LLM_SCHEMA_MODE`. `strict` delivers the schema as an enforced constraint, making the `canonical_url` `enum` unreachable to violate under all three LLM Protocols: `responses` and `chat_completions` carry `strict: true` beside the schema, and `anthropic` carries the same flag on the forced tool definition, which is what gates its grammar constraint — a forced tool call without it would bind only the field names. `json_object` only asks for valid JSON and moves the schema into the prompt; `none` sends no output-format field at all, for endpoints that reject one.

Degrading the Schema Mode is a configuration decision, never a runtime reaction to a failure: a request that fails is never retried under a weaker mode. Degrading costs more rejected decisions, not corrupted data, because the Published Project guarantee rests on the `allowed_urls` re-check rather than on the schema.

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
