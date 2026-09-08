# Linux.do GitHub 开源项目聚合器 PRD

## Problem Statement

Linux.do Channel 会持续发布包含 Linux.do 主题链接的 Telegram RSS 信息，但其中真正涉及 GitHub 开源项目的内容分散在不同主题和回复里。用户目前需要手动打开主题、阅读正文、识别仓库并整理项目，重复劳动多，也无法稳定追踪同一仓库在不同主题中的多次提及。

本项目需要自动完成 RSS 增量采集、Linux.do 主题正文提取、GitHub 仓库链接识别、LLM 分类整理和前端聚合展示。系统必须适配 Cloudflare 的运行边界，保证来源可追溯、重复执行安全，并且不能允许 LLM 猜测不存在或原文未出现的仓库。

## Solution

构建一个公开只读的 Web 应用。Cloudflare Cron Trigger 每 5 分钟触发一次同步，每轮最多处理 20 个待处理主题：

1. 读取 `https://rsshub.rssforever.com/telegram/channel/linux_do_channel`。
2. 从 RSS 条目的链接和 HTML 内容中提取并规范化 Linux.do topic ID。
3. 读取 `https://linux.do/t/topic/<topic-id>.rss`。
4. 始终处理主题首帖；回复只有在原文明确包含 GitHub 链接时才处理。
5. 从帖子原文提取 GitHub 仓库候选链接，将仓库子路径规范化到 `github.com/<owner>/<repo>`，同时保留原始链接作为证据。
6. 仅在存在候选仓库时调用 LLM。LLM 对候选仓库逐一判断是否属于帖子讨论的开源项目，并生成结构化摘要；任何不在输入候选集合中的仓库 URL 都被拒绝。
7. 将主题、帖子、全局去重后的项目、项目提及关系和处理状态写入 D1。
8. React 前端按 Linux.do 主题分组展示项目集合，并提供 GitHub 仓库和原帖链接。

## User Stories

1. 作为访客，我希望看到最近发现的开源项目主题，以便快速了解 Linux.do 社区正在讨论什么。
2. 作为访客，我希望每个主题下展示对应的项目集合，以便理解项目出现的讨论上下文。
3. 作为访客，我希望看到项目名称和简短摘要，以便判断是否值得进一步了解。
4. 作为访客，我希望能直接打开 GitHub 仓库，以便查看项目代码和文档。
5. 作为访客，我希望能返回 Linux.do 原帖，以便核对项目介绍和社区讨论。
6. 作为访客，我希望看到主题发布时间和来源信息，以便判断内容的新旧。
7. 作为访客，我希望同一主题中的多个仓库都能显示，以免遗漏相关项目。
8. 作为访客，我希望相同仓库不会产生相互冲突的项目记录，以便获得一致的信息。
9. 作为访客，我希望项目在不同主题中被提及时保留全部来源，以便看到更多讨论上下文。
10. 作为访客，我希望页面在手机和桌面端都可正常浏览，以便在不同设备上使用。
11. 作为访客，我希望页面有明确的加载、空数据和请求失败状态，以便理解当前发生了什么。
12. 作为访客，我希望页面默认按主题发布时间从新到旧排列，以便优先查看最新内容。
13. 作为维护者，我希望系统每 5 分钟自动检查 RSS，以便无需人工触发同步。
14. 作为维护者，我希望单轮最多处理 20 个待处理主题，以便约束 Cron、网络和 LLM 成本。
15. 作为维护者，我希望积压主题留到后续轮次，以便单次同步不会无限延长。
16. 作为维护者，我希望重复出现的 topic ID 不会被重复采集，以便避免浪费调用和产生脏数据。
17. 作为维护者，我希望同一仓库 URL 的不同子路径能归一化为一个项目，以便可靠去重。
18. 作为维护者，我希望原始 GitHub URL 被保留为证据，以便检查规范化结果。
19. 作为维护者，我希望 LLM 只能从帖子已有的候选仓库中选择，以便避免仓库幻觉。
20. 作为维护者，我希望 LLM 响应经过 Pydantic 校验，以便无效输出不会污染数据库。
21. 作为维护者，我希望单个主题失败不会阻塞同一轮中的其他主题，以便同步任务具有故障隔离。
22. 作为维护者，我希望网络失败和 LLM 失败可安全重试，以便临时故障能自动恢复。
23. 作为维护者，我希望每次同步有可查询的结果和错误摘要，以便定位采集问题。
24. 作为维护者，我希望所有外部请求都有限时和响应大小限制，以便避免 Worker 资源被耗尽。
25. 作为维护者，我希望所有密钥都通过 Cloudflare secrets 注入，以便仓库不包含敏感凭据。
26. 作为维护者，我希望 LLM 提供商通过配置切换，以便不被特定 SDK 或供应商锁定。
27. 作为维护者，我希望 RSS 中提取出的抓取地址经过严格白名单校验，以便阻止 SSRF。
28. 作为维护者，我希望未涉及明确 GitHub 仓库的主题不调用 LLM，以便降低成本。

## Implementation Decisions

### Technology stack

- Frontend: React, TypeScript, Vite, MUI, React Router and TanStack Query.
- Backend: Cloudflare Python Workers, FastAPI and Pydantic.
- Database: Cloudflare D1 with parameterized SQL and versioned SQL migrations.
- Scheduler: Cloudflare Cron Trigger, every 5 minutes.
- LLM: one configured endpoint speaking one of three wire protocols — OpenAI Responses,
  OpenAI Chat Completions, or Anthropic Messages — selected by `LLM_PROTOCOL`, with
  configurable base URL and model. Amended 2026-09-08; see
  `docs/adr/0005-llm-multi-protocol.md`.
- Deployment: frontend on Cloudflare Pages or Workers Assets; backend deployed with `pywrangler`.
- Queues are not part of the MVP. Add Cloudflare Queues only when backlog, independent retry or throughput requirements exceed the bounded Cron design.

### Deep module boundaries

- **Channel feed reader**: fetches the configured RSSHub feed and returns normalized Linux.do topic candidates. It owns RSS field differences, embedded HTML scanning and topic URL allowlisting.
- **Topic feed reader**: fetches a canonical Linux.do topic RSS URL and returns a normalized topic with posts. It owns XML parsing, HTML-to-text conversion and first-post/reply selection rules.
- **GitHub URL canonicalizer**: extracts explicit GitHub URLs, rejects non-repository paths, returns canonical `owner/repo` identities and preserves original evidence URLs. This is a pure module with no network or database access.
- **Project classifier**: accepts topic text plus an explicit candidate-repository allowlist and returns validated per-repository decisions. It owns the LLM prompt, OpenAI Responses-compatible HTTP adapter and Pydantic output contract.
- **Sync orchestrator**: coordinates one bounded run, claims at most 20 pending topics, invokes the readers/classifier, persists state transitions and isolates per-topic failures.
- **Persistence layer**: exposes narrow repositories for sync runs, topics, posts, projects and mentions. D1 SQL and transaction/batch details stay behind these interfaces.
- **Read API**: exposes public, cursor-paginated topic collections without exposing internal processing fields or database access.
- **Frontend application**: renders topic groups and project summaries, owns navigation and user-visible loading/empty/error states, and never parses RSS or calls the LLM.

### Ingestion and selection rules

- The only channel source in the MVP is `https://rsshub.rssforever.com/telegram/channel/linux_do_channel`.
- Topic candidates may appear in RSS `link`, `title`, `description` or content fields; the parser must inspect all supported fields.
- Only HTTPS URLs on host `linux.do` with path `/t/topic/<numeric-id>` are accepted. Floor suffixes and query/fragment data are removed.
- The canonical topic RSS URL is constructed from the validated numeric ID; arbitrary user-controlled fetch targets are never accepted.
- The first topic post is always retained. A reply is retained only if its original content contains at least one explicit GitHub URL.
- If no retained post contains a valid GitHub repository candidate, the topic is marked as not relevant without an LLM call.
- Repository URLs under `issues`, `pull`, `tree`, `blob`, `releases` and similar subpaths are normalized to `https://github.com/<owner>/<repo>`.
- GitHub profile, organization, search, marketplace, gist and non-repository URLs are not project candidates.

### LLM classification contract

> **Amended 2026-09-08 by `docs/adr/0005-llm-multi-protocol.md`.** This section originally
> fixed the contract to a single protocol. Three are now supported, selected by
> configuration and never probed at runtime. Everything below about the *classification*
> — the input, the per-candidate decisions, the allowlist, the Pydantic re-check — is
> unchanged and protocol-independent.

- The LLM integration calls the endpoint's REST API directly rather than depending on a vendor SDK that may not run in Python Workers. One module per protocol lives in `linuxdo_oss/llm/`.
- `LLM_PROTOCOL` selects the wire protocol: `responses` (default), `chat_completions` or `anthropic`. It is a deployment declaration, not a runtime discovery.
- `LLM_BASE_URL` is configurable and represents the API root, for example `https://api.openai.com/v1`. Each protocol appends its own path suffix (`/responses`, `/chat/completions`, `/v1/messages`), so the value must be a root: no endpoint path, no query string. The `/v1` convention differs per protocol and `config.py` enforces the difference at startup.
- `LLM_API_KEY`, `LLM_MODEL`, `LLM_SCHEMA_MODE`, request timeout, maximum output tokens and prompt version are configuration values. The model identifier is not hard-coded.
- Which header carries the key is the protocol's business: `Authorization: Bearer` for the two OpenAI shapes, `x-api-key` plus `anthropic-version` for Anthropic.
- The input contains cleaned post text, original source references and an explicit candidate repository list.
- The output contains one decision per candidate repository: `include`, `exclude` or `uncertain`.
- An included project contains canonical repository URL, display name, Chinese summary, short evidence excerpt and confidence score.
- Only `include` decisions are published. `uncertain` and invalid responses remain internal and do not appear in the public list.
- Under `LLM_SCHEMA_MODE=strict` each protocol delivers the JSON Schema structurally in its own spelling: `text.format` for Responses, `response_format.json_schema` for Chat Completions, a forced `tool_choice` over `tools[].input_schema` for Anthropic. A protocol must never be sent another protocol's spelling — it is accepted and ignored.
- `LLM_SCHEMA_MODE` may be degraded to `json_object` or `none` for endpoints that reject a strict schema (DeepSeek and ZhiPu accept only `json_object`; some local runtimes reject the output-format field itself). The schema then travels in the prompt. Degrading is a configuration decision, logged once per run, and gives up only the structural guard.
- Each adapter parses its protocol's typed output items and handles that protocol's refusal, truncation and failure spellings explicitly. Output text must never be read at a fixed array index: reasoning models emit other items ahead of the message.
- Pydantic validates the extracted structured payload again at the application boundary. Repository URLs absent from the input allowlist cause validation failure.
- A custom base URL is accepted only when it implements the request/response shape of the configured `LLM_PROTOCOL` at the configured `LLM_SCHEMA_MODE`. Unsupported or partial compatibility is a configuration error, reported as `endpoint_config` with the HTTP status when the endpoint says so. The system **never falls back at runtime**: a failed request is not retried under a different protocol or a weaker schema mode. Choosing a weaker mode is an explicit configuration change.
- Raw LLM output is retained only when required for failure diagnosis and must have a bounded size; validated structured output is authoritative.

### Data model

- **sync_runs**: scheduled time, start/end time, status, discovered/processed/published/failed counts and bounded error summary.
- **topics**: numeric Linux.do topic ID, canonical URL, title, author, published time, fetch/classification status, attempt count, retry time and last bounded error.
- **topic_posts**: stable RSS GUID or `(topic_id, post_number)` identity, author, published time, source URL, cleaned text and whether it is the first post.
- **projects**: canonical GitHub repository URL as global unique identity, owner, repository name, display name, current summary and timestamps.
- **project_mentions**: topic, post, project, original evidence URL, evidence excerpt, confidence, prompt version and detection time.
- A topic can mention many projects; a project can appear in many topics. Project arrays are not copied into topic rows.
- All writes use unique constraints and idempotent upserts. Re-running the same topic must not duplicate posts, projects or mentions.

### Processing states and retries

- Topic processing states are `discovered`, `fetching`, `ready`, `classifying`, `published`, `not_relevant` and `failed`.
- Each run claims at most 20 due work items, including retriable failures, using a conditional state transition to prevent duplicate processing.
- External RSS and LLM calls have explicit timeouts, response-size limits and bounded concurrency.
- Retryable failures use capped retry attempts and backoff across later Cron runs. Permanent validation failures are recorded without immediate retry.
- A failure for one topic is caught and persisted; processing continues for the rest of the claimed batch.
- Overlapping Cron executions must be harmless through claim leases, unique constraints and idempotent writes.

### API contract

- `GET /api/topics`: cursor-paginated topics ordered newest first, each containing its published project collection and source metadata.
- `GET /api/topics/{topic_id}`: one topic, retained source posts and all published project mentions.
- `GET /api/projects/{owner}/{repo}`: one globally deduplicated project and its Linux.do source topics.
- `GET /api/health`: deployment health only; it must not expose secrets or internal error payloads.
- Public endpoints are read-only, validate pagination bounds and return stable Pydantic response models.

### Frontend behavior

- The first screen is the actual project feed, not a marketing landing page.
- Topics are the primary visual grouping; each topic shows its project collection without nested decorative cards.
- Each project shows name, concise summary, canonical GitHub link and source evidence link.
- The UI provides responsive layouts and explicit loading, empty, partial-data and API-error states.
- External links open safely with appropriate `rel` attributes. RSS HTML is converted to text on the backend and never injected as raw HTML.

### Security and operations

- RSS fetch destinations use fixed configuration and strict host/path allowlists.
- RSS/XML parsers reject DTD/external entities and enforce response-size limits.
- LLM keys and any future GitHub token are Cloudflare secrets and are never returned to the browser.
- `LLM_BASE_URL` must be an HTTPS URL from deployment configuration. The API key is sent only to that configured origin and must never be accepted from a public request.
- Public API CORS is restricted to configured frontend origins.
- Logs use topic IDs, run IDs and bounded error categories; they must not contain secrets or full unbounded LLM payloads.
- A deployed-worker connectivity spike must verify that both RSSHub and Linux.do RSS endpoints accept Cloudflare Worker egress before full implementation proceeds.

## Testing Decisions

- Tests assert externally observable behavior, not private function calls or implementation details.
- The Channel feed reader is tested with saved RSS fixtures covering links embedded in different fields, duplicate topic URLs, floor suffixes and malicious hosts.
- The Topic feed reader is tested with fixtures covering first-post selection, GitHub-link replies, replies without GitHub links, malformed XML and oversized responses.
- The GitHub URL canonicalizer receives exhaustive table-driven tests for repository roots, subpaths, case normalization, invalid profile/org URLs and deceptive hosts.
- The Project classifier is tested with a fake LLM transport for valid output, unknown repository injection, malformed JSON, uncertain results, timeouts and provider errors.
- Responses adapter contract tests cover strict `text.format` requests, typed output extraction, refusals, incomplete responses and a custom `LLM_BASE_URL` with and without a trailing slash.
- The Sync orchestrator is tested with fake ports for the 20-item cap, idempotent reruns, per-topic isolation, retry transitions and overlapping claims.
- D1 integration tests cover migrations, unique constraints, upserts and topic-project many-to-many queries using the local Worker/D1 runtime.
- FastAPI contract tests cover pagination, validation, empty results, topic details and project provenance.
- Frontend tests cover successful topic grouping, multiple projects, repeated projects across topics, loading, empty and error states.
- One end-to-end smoke test runs a fixture-backed sync and verifies that the resulting API payload renders in the frontend.
- Live RSS and LLM tests are opt-in integration checks and are not required for deterministic unit-test runs.

## Acceptance Criteria

- [ ] Cron is configured for every 5 minutes and no run claims more than 20 due topics.
- [ ] Valid Linux.do topic URLs embedded in the channel RSS are extracted, canonicalized and globally deduplicated.
- [ ] Each topic's first post and only GitHub-link-containing replies are retained from the topic RSS.
- [ ] Topics without explicit repository candidates skip the LLM and settle as `not_relevant`.
- [ ] LLM output passes Pydantic validation and cannot introduce a repository absent from the source text.
- [ ] The configured endpoint's protocol and structured-output capability are verified by hand against the real endpoint before production ingestion is enabled, and the result is recorded in `LLM_PROTOCOL` / `LLM_SCHEMA_MODE`. **There is no automated capability probe**: a Worker is stateless so a probe result has nowhere to be cached, it would cost a subrequest every run, and "probe, then downgrade" is the silent fallback ADR 0005 forbids.
- [ ] Same-repository subpaths normalize to one global project while original evidence URLs remain available.
- [ ] Same project across multiple topics creates one project row and multiple source mentions.
- [ ] Public API returns newest-first topic groups with project collections and provenance links.
- [ ] Frontend renders responsive loading, empty, error and populated states and links to GitHub/Linux.do.
- [ ] Reprocessing the same RSS data produces no duplicate topics, posts, projects or mentions.
- [ ] One topic's fetch/classification failure does not prevent other claimed topics from completing.
- [ ] Secrets are absent from the repository, API responses and normal logs.
- [ ] Frontend lint/typecheck/build and backend tests/local Worker checks pass.
- [ ] Deployed Worker connectivity to both RSS sources is verified.

## Out of Scope

- Searching GitHub by project name or allowing the LLM to invent/complete repository URLs.
- Fetching GitHub README, stars, language, license or other GitHub API metadata.
- Processing all topic replies that do not contain an explicit GitHub URL.
- Historical crawling beyond items visible through the configured RSS feeds.
- Manual moderation dashboards and publishing uncertain LLM decisions.
- User accounts, favorites, comments, voting and personalized recommendations.
- Full-text search, project categories and recommendation ranking.
- Cloudflare Queues, Celery and other background-job systems in the MVP.
- Images, video, attachments and other binary processing.
- PostgreSQL, SSR and multi-region consistency optimization.

## Further Notes

- The MVP prioritizes verifiability over recall: projects without explicit GitHub repository URLs will be missed intentionally.
- The LLM contract is fixed at the *classification* level — the prompt, the JSON Schema body, the per-candidate decisions and the allowlist re-check are identical under every protocol. The wire shape is chosen by deployment through `LLM_PROTOCOL`, `LLM_SCHEMA_MODE`, `LLM_BASE_URL` and `LLM_MODEL`.
- The anti-hallucination guarantee does not rest on the schema. It rests on `_reject_unknown_and_duplicate()`, which re-checks every returned repository against the candidate allowlist under every protocol and every schema mode. The `enum` is defence in depth; degrading the schema mode costs rejected decisions, not data integrity.
- `[UNKNOWN]` Linux.do or RSSHub may rate-limit/block Cloudflare Worker egress. The connectivity spike is a release prerequisite, not an optional check.
- Python Workers package compatibility must be verified before adding dependencies. Prefer runtime APIs and small pure-Python components over native extensions.
- If a five-minute run regularly leaves growing backlog, or per-topic retry isolation becomes operationally important, introduce Cloudflare Queues rather than increasing Cron work without bounds.
