# API Contract

The single coupling surface between frontend and backend.

- Frontend side: `frontend/src/api/types.ts`
- Backend side: `backend/src/linuxdo_oss/api/schemas.py`

**Changing one side without the other is a defect.** Update this file too when the contract moves.

---

## Conventions

- **snake_case on the wire, snake_case in TypeScript.** There is no camelCase conversion layer. One naming convention end to end; a mapping layer would be a second source of truth.
- All timestamps are **ISO 8601 UTC strings**.
- All text fields are **plain text**, already converted from HTML by the backend. The frontend never renders API text as HTML.
- Read-only. There are no write endpoints, which is why `src/api/client.ts` exposes only `apiGet` and takes no method parameter.

---

## Endpoints

| Method | Path | Response |
|--------|------|----------|
| GET | `/api/topics?cursor=&limit=` | `CursorPage<TopicSummary>` |
| GET | `/api/topics/{topic_id}` | `TopicDetail` |
| GET | `/api/projects/{owner}/{repo}` | `ProjectDetail` |
| GET | `/api/health` | `HealthStatus` |

Ordering: `/api/topics` is newest-first by topic `published_at`.

## Pagination envelope

```ts
type CursorPage<T> = { items: T[]; next_cursor: string | null };
```

`next_cursor === null` means last page. It is the only end-of-list signal — see `.trellis/spec/frontend/state-management.md`.

## Identity rules

- A **project**'s identity is `canonical_url` = `https://github.com/<owner>/<repo>`. Globally unique.
- `evidence_url` is the original URL as it appeared in the post (may be a subpath such as `/issues/12`). It is kept as proof and must never be used as an identity.
- A **topic**'s identity is the numeric `topic_id`. `canonical_url` carries no floor suffix.
- Inside a topic, the render key for a project row is `${canonical_url}#${post_number}` — the same repository may legitimately appear in more than one post of the same topic.

## What the API must never expose

Per the PRD:

- Internal processing fields: topic fetch/classification status, attempt counts, retry times, error payloads.
- `uncertain` LLM decisions. Only `include` decisions are published.
- Raw LLM output.
- Secrets, in responses or in `/api/health`.

`/api/health` returns deployment health only.

## Error contract

Non-2xx bodies are not parsed. `src/api/client.ts` maps them to `ApiError`:

| Condition | `ApiError.status` | Retried? |
|-----------|-------------------|----------|
| Transport failure or timeout | `0` | yes |
| 404 | `404` | no |
| Other 4xx | as returned | no |
| 5xx | as returned | yes |

The frontend distinguishes "not found" from "backend is down" — a bare `Error` cannot express that, which is why `ApiError` carries `status`.

## Drift protection

Currently manual: this file plus the comment headers in `frontend/src/api/types.ts`.

A generated path exists if drift becomes a real problem (`openapi-typescript` against FastAPI's `/openapi.json`, consumed by `openapi-fetch`) — see the closing section of `docs/adr/0003-data-layer-tanstack-query.md`. Do not adopt it speculatively.
