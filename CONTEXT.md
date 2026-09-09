# Domain Context

## Glossary

### Tag Feed

The one configured Discourse list feed (`/tag/<slug>/<id>.rss`, or the identically shaped `/latest.rss` and `/c/<slug>.rss`) that is the pipeline's sole input. Each item announces a Topic *and* carries that Topic's first post in full, so there is no separate fetch per Topic. Ordered by last activity, capped at ~30 items, with no pagination — a Topic that falls off the window is unreachable. The host every item must belong to is derived from this URL, never hard-coded, which is what allows the whole pipeline to run against any Discourse instance. See `docs/adr/0007-single-source-tag-feed.md`.

### Topic

A Linux.do discussion identified by its numeric topic ID. A topic is the primary grouping shown in the frontend.

### Topic Post

The opening post of a Topic, as plain text with Discourse's two generated closing paragraphs (the post/participant count and the "read full topic" link) removed. Exactly one exists per Topic: the Tag Feed carries no replies, so a repository first linked in a reply is not visible to this project. The row shape still keeps `post_number` and `is_first_post` because mentions point at a post and the schema is keyed on `(topic_id, post_number)`.

### Project

A globally deduplicated GitHub repository identified by canonical `https://github.com/<owner>/<repo>` URL.

### Project Mention

The evidence-bearing relationship between a Project and the Topic Post that referenced it. It preserves the original GitHub URL and source context.

### Published Project

A Project candidate that the LLM classified as `include` using only an explicit repository URL found in the source post and whose structured output passed validation.

### Classifier Endpoint

The single HTTPS API **root** derived from `LLM_BASE_URL` that the classifier talks to. It is a root, never a full endpoint path — the path suffix is decided by the LLM Protocol, not by configuration. Compatibility with the configured protocol is a required capability, never a provider-name assumption.

"Derived from" rather than "configured by": the configured value may carry the endpoint path of *its own* protocol, which is stripped to reach the root and reported by one WARNING per Sync Run. Carrying *another* protocol's endpoint path is a rejected configuration — it means the LLM Protocol is the wrong one, and stripping it would hide that. See `docs/adr/0006-llm-base-url-normalization.md`.

Supersedes the earlier term "Responses-Compatible Endpoint", which assumed a single protocol. See `docs/adr/0005-llm-multi-protocol.md`.

### LLM Protocol

The wire protocol the Classifier Endpoint is expected to speak, selected by `LLM_PROTOCOL` and never probed at runtime. One of `responses` (OpenAI Responses API, the default), `chat_completions` (OpenAI Chat Completions), `anthropic` (Anthropic Messages API). Each protocol owns its URL suffix, auth header, message shape, schema wrapper and output extraction; the classification prompt and the JSON Schema body are protocol-agnostic and shared.

### Schema Mode

How much of the JSON Schema actually reaches the model, selected by `LLM_SCHEMA_MODE`. `strict` delivers the schema as an enforced constraint, making the `canonical_url` `enum` unreachable to violate under all three LLM Protocols: `responses` and `chat_completions` carry `strict: true` beside the schema, and `anthropic` carries the same flag on the forced tool definition, which is what gates its grammar constraint — a forced tool call without it would bind only the field names. `json_object` only asks for valid JSON and moves the schema into the prompt; `none` sends no output-format field at all, for endpoints that reject one.

Degrading the Schema Mode is a configuration decision, never a runtime reaction to a failure: a request that fails is never retried under a weaker mode. Degrading costs more rejected decisions, not corrupted data, because the Published Project guarantee rests on the `allowed_urls` re-check rather than on the schema.

### Sync Run

One Cron-triggered ingestion attempt that claims at most 20 due topics and records bounded processing results.

### Claim Lease

The time window a Sync Run owns a Topic for. Taken by a single conditional UPDATE whose row count decides who won, so two overlapping runs can never process the same Topic. It lasts 15 minutes — the Worker wall-clock ceiling — because a lease shorter than one invocation would let the next run reclaim a Topic the current one is still working on. A run that dies leaves its lease to expire, and the next run returns the Topic to `ready`.

### Topic Status

The four states a Topic passes through: `ready` (row and text stored, awaiting judgement) → `classifying` (a Sync Run holds the Claim Lease) → `published` or `not_relevant`, with `failed` for a retriable error. `discovered` and `fetching` were removed when the Tag Feed collapsed discovery and fetching into one request; both remain legal schema values that are never written, so their removal needed no migration. A Topic is *born* `ready`, which is why `ready` is the status a claim accepts rather than an in-flight one it rejects.

### Retry Budget

How many times a Topic may be claimed before it is abandoned: 5. Each failure that is worth repeating schedules the next attempt 5, 10, 20 then 40 minutes out, doubling from the cron interval because nothing shorter can be observed — no run happens between two firings to pick the Topic up. A failure that cannot be repeated (a malformed feed, a schema violation) and an exhausted budget are recorded the same way, as no scheduled retry at all, so a permanently broken Topic stops consuming subrequests without needing a status of its own.

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
